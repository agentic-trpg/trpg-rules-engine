"""One admitted attack computation. No handles, live combat, registry or hooks.

Only actor updates, typed combat-field updates and a short event buffer live here.
The immutable input supplies all other facts. Admission excludes lifecycle systems
that this vertical slice cannot yet execute; this is not a general combat runtime.
"""

import random
from collections.abc import Callable, Collection
from dataclasses import dataclass

from dnd5e_srd_data.loader import AssetLoader

from dnd5e_engine import attack_rules
from dnd5e_engine.action_policy import grant_payment_for_effect
from dnd5e_engine.activities.attack import (
    _attack_roll_flat_bonus,
    _attack_roll_sources,
    _governing_ability,
    _resolve_hit_outcome,
    _target_effective_ac,
    _weapon_heavy_disadvantage,
)
from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.context import (
    ActivityResolutionContext,
    AttackHitContext,
    DamageInstanceContext,
)
from dnd5e_engine.activities.d20 import resolve_mode
from dnd5e_engine.activities.resolver import resolve_activity
from dnd5e_engine.evaluation_computation import CombatComputation as _AttackComputation
from dnd5e_engine.evaluation_computation import CombatComputationResult
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd5e_engine.evaluation_haste_delta import EffectFollowUpCreate
from dnd5e_engine.evaluation_preflight import AttackPlan
from dnd5e_engine.evaluation_state import CombatSnapshot
from dnd5e_engine.events import (
    AdvantageMode,
    AdvantageSource,
    CombatEvent,
    IntentSubmitted,
)
from dnd5e_engine.movement import project_speeds
from dnd5e_engine.reaction_effect_rules import effective_armor_class
from dnd5e_engine.rules.conditions import active_condition_names, exhaustion_level_of
from dnd5e_engine.sap_rules import apply_sap, consume_sap
from dnd5e_engine.spatial import GridTopology
from dnd5e_engine.specs import GridScene
from dnd5e_engine.turn_rules import (
    ordinary_action_payment,
    record_budget_changes,
    spend_attack_budget,
)
from dnd5e_engine.types.combat import Combatant


def _attack_context(
    snapshot: CombatSnapshot,
    intent: CombatIntentPayload,
    plan: AttackPlan,
    actor: Combatant,
    actors: Collection[Combatant],
    rng: random.Random,
    emit: Callable[[CombatEvent], None],
    speed: Callable[[Combatant], int],
    *,
    ac_bonus: int = 0,
    hit_reaction: Callable[[AttackHitContext], int] | None = None,
    next_damage_id: Callable[..., str] | None = None,
    damage_completed: Callable[[DamageInstanceContext], None] | None = None,
) -> ActivityResolutionContext:
    """Build the same ordinary attack roll inputs for execution and preview."""
    state = snapshot.combat_state
    topology = GridTopology(GridScene.model_validate(snapshot.scene_state.grid.model_dump()))
    ranged_in_melee = attack_rules.hostile_adjacent_to_attacker(
        actor,
        actors,
        allies=state.party_ids if actor.entity_id in state.party_ids else state.encounter_ids,
        dead_ids=state.dead_ids,
        positions=state.actor_zone,
        topology=topology,
        can_see=lambda hostile, attacker: True,
    )
    bands = attack_rules.weapon_attack_range_ft(plan.weapon)
    dodge_speed_actor = plan.target
    if plan.target_walk_bonus:
        dodge_speed_actor = plan.target.model_copy(
            update={"base_speed": plan.target.base_speed + plan.target_walk_bonus}
        )
    return build_activity_context(
        actor,
        [plan.target],
        rng=rng,
        event_emitter=emit,
        slot_level=None,
        base_spell_level=None,
        spellcasting_ability=None,
        concentration=False,
        source_passive_effects=[],
        spell_book={},
        passive_damage_modifiers=attack_rules.damage_modifiers(actors),
        target_distance_ft={plan.target.entity_id: plan.distance_ft}
        if plan.distance_ft is not None
        else {},
        target_dodging={
            plan.target.entity_id: attack_rules.dodge_benefit_active(
                plan.target, effective_speed=speed(dodge_speed_actor)
            )
        },
        target_beyond_normal_range={
            plan.target.entity_id: bool(
                bands and plan.distance_ft is not None and plan.distance_ft > bands[0]
            )
        },
        target_ac_floor={plan.target.entity_id: plan.target_ac_floor}
        if plan.target_ac_floor is not None
        else None,
        attacker_ranged_in_melee=ranged_in_melee,
        attacker_sapped=actor.entity_id in state.sap_marks,
        use_versatile_damage=intent.two_handed
        and attack_rules.versatile_grip_applies(plan.weapon, plan.distance_ft),
        is_proficient_attack=attack_rules.is_proficient_with_weapon(actor, plan.weapon),
        stat_block_magnitudes=attack_rules.stat_block_magnitudes(actor)
        if intent.stat_block_action_id
        else None,
        attack_origin="monster" if intent.stat_block_action_id else "action",
        turn_serial=state.turn_serial,
        damage_instance_id_provider=next_damage_id,
        damage_instance_resolved=damage_completed,
        attack_hit_reaction=hit_reaction,
        save_modifiers={plan.target.entity_id: {"passive_ac_bonus": str(ac_bonus)}}
        if ac_bonus
        else {},
    )


class PreviewDiceDrawError(RuntimeError):
    """A variable attack input prevents an exact read-only preview."""


class _NoDiceRandom(random.Random):
    """Reject every attempted dice draw in a read-only preview."""

    def __init__(self) -> None:
        """Avoid even seeding an unused random stream."""

    def random(self) -> float:
        """Reject floating draws as well as d20 draws."""
        raise PreviewDiceDrawError("preview cannot draw dice")

    def getrandbits(self, k: int) -> int:
        """Reject integer draws used by range helpers."""
        raise PreviewDiceDrawError("preview cannot draw dice")


PreviewReason = AdvantageSource


def preview_attack_hit(
    snapshot: CombatSnapshot, intent: CombatIntentPayload, plan: AttackPlan
) -> tuple[int, AdvantageMode, tuple[PreviewReason, ...], tuple[PreviewReason, ...]] | None:
    """Count successful kept d20 outcomes using the executable attack's pure helpers.

    This deliberately admits only deterministic ordinary attacks. The RNG
    rejects draws during context or bonus preparation. Grant and reaction
    hooks are not called, so unsupported profiles return unknown.
    """
    if plan.effect_profile != "ordinary" or plan.action_grant is not None:
        return None
    actor, target = plan.actor, plan.target
    local_rng = _NoDiceRandom()

    def ordinary_speed(creature: Combatant) -> int:
        """Match the ordinary computation's condition and exhaustion speed."""
        return project_speeds(
            creature.base_speed,
            creature.movement_modes,
            condition_names=active_condition_names(creature.conditions),
            exhaustion_level=exhaustion_level_of(creature.conditions),
        ).walk

    ctx = _attack_context(
        snapshot,
        intent,
        plan,
        actor,
        plan.participants,
        local_rng,
        lambda event: None,
        ordinary_speed,
    )
    if ctx.passive_attack_bonus or ctx.passive_weapon_attack_bonus:
        return None
    governing_ability = _governing_ability(plan.activity, ctx, plan.weapon)
    sources, _ = _attack_roll_sources(
        plan.activity,
        ctx,
        target,
        plan.weapon,
        distance_ft=plan.distance_ft,
        target_conditions=ctx.target_conditions.get(target.entity_id, []),
        governing_ability=governing_ability,
        heavy_disadvantage=_weapon_heavy_disadvantage(plan.weapon, actor),
    )
    bonus = _attack_roll_flat_bonus(plan.activity, ctx, plan.weapon, governing_ability)
    ac = _target_effective_ac(ctx, target)
    mode = resolve_mode(sources)
    advantage_reasons = sources.advantage
    disadvantage_reasons = sources.disadvantage
    hits = 0
    for kept in range(1, 21):
        weight = 1 if mode == "normal" else (2 * kept - 1 if mode == "advantage" else 41 - 2 * kept)
        _, hit = _resolve_hit_outcome(kept, kept + bonus, ac, plan.activity)
        hits += weight * hit
    return hits, mode, advantage_reasons, disadvantage_reasons


@dataclass(frozen=True)
class HasteAttackResult(CombatComputationResult):
    """Retain the captured follow-up proposals alongside mechanical attack evidence."""

    follow_ups: tuple[EffectFollowUpCreate, ...]


def execute_attack(
    snapshot: CombatSnapshot,
    intent: CombatIntentPayload,
    plan: AttackPlan,
    rng: random.Random,
    *,
    loader: AssetLoader | None = None,
    before_hit: Callable[[AttackHitContext], None] | None = None,
) -> CombatComputationResult:
    """Compute attack costs and consequences while retaining the current turn.

    The private result leaves turn boundaries to explicit Pass, so movement and
    any remaining legal actions can still use the same turn.
    """
    from dnd5e_engine.evaluation_haste import HasteComputation
    from dnd5e_engine.evaluation_shield import ShieldComputation

    if plan.effect_profile == "shield":
        if loader is None:
            raise RuntimeError("Shield computation requires the explicit verified loader")
        computation: _AttackComputation = ShieldComputation(snapshot, rng, loader)
    elif plan.effect_profile == "haste":
        computation = HasteComputation(snapshot, rng)
    elif plan.effect_profile in {"concentration", "held_target"}:
        from dnd5e_engine.evaluation_concentration import ConcentrationComputation

        computation = ConcentrationComputation(snapshot, rng)
    else:
        computation = _AttackComputation(snapshot, rng)
    computation.enable_stages()
    before = plan.actor
    computation.emit(
        IntentSubmitted(
            actor_id=before.entity_id, intent_type="attack", target_id=plan.target.entity_id
        )
    )
    if plan.action_grant is None:
        actor = spend_attack_budget(
            before,
            new_action=attack_rules.attack_action_is_spent(before),
            attacks=1,
            payment=ordinary_action_payment(before),
        )
    else:
        actor = before.model_copy(update=grant_payment_for_effect(before, plan.action_grant))
    computation.actors[actor.entity_id] = actor
    computation.sample_stage("attack_execution", (actor.entity_id,))

    def react_to_hit(attack: AttackHitContext) -> int:
        """Reach the private checkpoint before applying the existing reactions."""
        if before_hit is not None:
            before_hit(attack)
        if isinstance(computation, ShieldComputation):
            return computation.hit_reaction(attack)
        return attack.effective_ac

    ac_bonus = 0
    if isinstance(computation, ShieldComputation):
        ac_bonus = computation.ac_bonus(plan.target.entity_id)
    elif isinstance(computation, HasteComputation):
        ac_bonus = effective_armor_class(
            0,
            [effect for effect in computation.effects if effect.target_id == plan.target.entity_id],
        )
    ctx = _attack_context(
        snapshot,
        intent,
        plan,
        actor,
        computation.actors.values(),
        rng,
        computation.emit,
        computation.speed,
        ac_bonus=ac_bonus,
        hit_reaction=react_to_hit
        if before_hit is not None or isinstance(computation, ShieldComputation)
        else None,
        next_damage_id=computation.next_damage_id,
        damage_completed=computation.damage_completed,
    )
    resolve_activity(plan.activity, ctx, weapon=plan.weapon)
    marks = consume_sap(computation.state.sap_marks, actor.entity_id, computation.events)
    for mastery, target in ctx.mastery_procs:
        if mastery != "sap":
            raise RuntimeError("unadmitted mastery proc")
        marks = apply_sap(marks, actor.entity_id, target)
    computation.state = computation.state.model_copy(update={"sap_marks": marks})
    computation.actors[actor.entity_id] = record_budget_changes(
        before, computation.actors[actor.entity_id]
    )
    if computation.damage:
        raise RuntimeError("attack left an incomplete damage instance")
    result = computation.result()
    if isinstance(computation, HasteComputation):
        return HasteAttackResult(
            result.candidate_state,
            result.proposed_events,
            result.stage_facts,
            tuple(computation.follow_ups),
        )
    return result
