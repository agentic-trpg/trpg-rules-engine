"""One admitted attack computation. No handles, live combat, registry or hooks.

Only actor updates, typed combat-field updates and a short event buffer live here.
The immutable input supplies all other facts. Admission excludes lifecycle systems
that this vertical slice cannot yet execute; this is not a general combat runtime.
"""

import random

from dnd5e_engine import attack_rules
from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.resolver import resolve_activity
from dnd5e_engine.evaluation_computation import CombatComputation as _AttackComputation
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd5e_engine.evaluation_preflight import AttackPlan
from dnd5e_engine.evaluation_state import CombatSnapshot
from dnd5e_engine.events import (
    IntentSubmitted,
)
from dnd5e_engine.spatial import GridTopology
from dnd5e_engine.specs import GridScene
from dnd5e_engine.turn_rules import (
    ordinary_action_payment,
    record_budget_changes,
    spend_attack_budget,
)


def execute_attack(
    snapshot: CombatSnapshot, intent: CombatIntentPayload, plan: AttackPlan, rng: random.Random
) -> CombatSnapshot:
    """Compute a private post-state for closed delta projection, never publication."""
    computation = _AttackComputation(snapshot, rng)
    before = plan.actor
    computation.emit(
        IntentSubmitted(
            actor_id=before.entity_id, intent_type="attack", target_id=plan.target.entity_id
        )
    )
    actor = spend_attack_budget(
        before,
        new_action=attack_rules.attack_action_is_spent(before),
        attacks=1,
        payment=ordinary_action_payment(before),
    )
    computation.actors[actor.entity_id] = actor
    topology = GridTopology(GridScene.model_validate(snapshot.scene_state.grid.model_dump()))
    state = snapshot.combat_state
    ranged_in_melee = attack_rules.hostile_adjacent_to_attacker(
        actor,
        computation.actors.values(),
        allies=state.party_ids if actor.entity_id in state.party_ids else state.encounter_ids,
        dead_ids=state.dead_ids,
        positions=state.actor_zone,
        topology=topology,
        can_see=lambda hostile, attacker: True,
    )  # admission requires bright, unobstructed geometry and no conditions
    bands = attack_rules.weapon_attack_range_ft(plan.weapon)
    ctx = build_activity_context(
        actor,
        [plan.target],
        rng=rng,
        event_emitter=computation.emit,
        slot_level=None,
        base_spell_level=None,
        spellcasting_ability=None,
        concentration=False,
        source_passive_effects=[],
        spell_book={},
        save_modifiers={},
        passive_damage_modifiers=attack_rules.damage_modifiers(computation.actors.values()),
        target_distance_ft={plan.target.entity_id: plan.distance_ft}
        if plan.distance_ft is not None
        else {},
        target_dodging={
            plan.target.entity_id: attack_rules.dodge_benefit_active(
                plan.target, effective_speed=computation.speed(plan.target)
            )
        },
        target_beyond_normal_range={
            plan.target.entity_id: bool(
                bands and plan.distance_ft is not None and plan.distance_ft > bands[0]
            )
        },
        attacker_ranged_in_melee=ranged_in_melee,
        use_versatile_damage=intent.two_handed
        and attack_rules.versatile_grip_applies(plan.weapon, plan.distance_ft),
        is_proficient_attack=attack_rules.is_proficient_with_weapon(actor, plan.weapon),
        stat_block_magnitudes=attack_rules.stat_block_magnitudes(actor)
        if intent.stat_block_action_id
        else None,
        attack_origin="monster" if intent.stat_block_action_id else "action",
        turn_serial=snapshot.combat_state.turn_serial,
        damage_instance_id_provider=computation.next_damage_id,
        damage_instance_resolved=computation.damage_completed,
    )
    resolve_activity(plan.activity, ctx, weapon=plan.weapon)
    computation.finish_turn(actor.entity_id)
    computation.actors[actor.entity_id] = record_budget_changes(
        before, computation.actors[actor.entity_id]
    )
    if computation.damage:
        raise RuntimeError("attack left an incomplete damage instance")
    return computation.result()
