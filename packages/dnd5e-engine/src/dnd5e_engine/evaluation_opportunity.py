"""Bounded authorized weapon OA; draw-free admission and shared resolution."""

from dataclasses import dataclass, replace

from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.common import AttackActivity
from dnd5e_srd_data.schema.item import Weapon

from dnd5e_engine import attack_rules
from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.resolver import resolve_activity
from dnd5e_engine.evaluation_actor import combatant
from dnd5e_engine.evaluation_computation import CombatComputation
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd5e_engine.evaluation_preflight import stat_block_admission, weapon_admission
from dnd5e_engine.evaluation_reaction_state import (
    OpportunityAttackState,
    StatBlockOpportunityAttackState,
)
from dnd5e_engine.evaluation_state import CombatSnapshot
from dnd5e_engine.events import IntentSubmitted
from dnd5e_engine.reactions import leaves_melee_reach
from dnd5e_engine.spatial import GridTopology
from dnd5e_engine.specs import GridScene
from dnd5e_engine.spell_delivery import activity_source_identity
from dnd5e_engine.types.combat import Combatant


@dataclass(frozen=True)
class OpportunityPlan:
    declaration: OpportunityAttackState | StatBlockOpportunityAttackState
    weapon: Weapon | None
    activity: AttackActivity
    reach_ft: int


def opportunity_dependencies(
    snapshot: CombatSnapshot, loader: AssetLoader
) -> tuple[OpportunityPlan | None, str | None]:
    """Validate the complete declaration even if budget/geometry prevent firing."""
    pending = snapshot.combat_state.pending_reactions
    if not pending:
        return None, None
    if len(pending) != 1 or not isinstance(
        pending[0], (OpportunityAttackState, StatBlockOpportunityAttackState)
    ):
        return None, "only one explicit ordinary opportunity declaration is reviewed"
    declaration = pending[0]
    if any(
        actor.granted_features is None
        or (not actor.class_slug and not actor.classes and actor.custom_counters)
        for actor in snapshot.character_states
    ):
        return None, "complete reviewed grants and counters are required for reactive movement"
    actors = {a.entity_id: a for a in snapshot.character_states}
    owner, mover = actors[declaration.owner_id], actors.get(declaration.mover_id)
    state = snapshot.combat_state
    if isinstance(declaration, StatBlockOpportunityAttackState):
        from dnd5e_engine.evaluation_monster_movement import monster_movement_failure

        failure = monster_movement_failure(snapshot, loader)
        if failure:
            return None, failure
        if (
            mover is None
            or owner.entity_id == mover.entity_id
            or (owner.entity_id in state.party_ids) == (mover.entity_id in state.party_ids)
            or state.monster_slug_by_entity.get(owner.entity_id) != declaration.monster_slug
        ):
            return None, "stat-block declaration requires a bound opposing owner and mover"
        intent = CombatIntentPayload(
            intent_type="attack",
            target_id=mover.entity_id,
            stat_block_action_id=declaration.stat_block_action_id,
        )
        refusal = stat_block_admission(snapshot, combatant(owner), intent, loader)
        if refusal is not None:
            return None, refusal.error.reason if refusal.error else "incomplete stat-block choice"
        monster = loader.get_monster(declaration.monster_slug)
        assert monster is not None
        action = next(a for a in monster.actions if a.slug == declaration.stat_block_action_id)
        activity = action.activities[0]
        assert isinstance(activity, AttackActivity)
        if activity.attack.type.value != "melee":
            return None, "opportunity attack requires an explicit melee stat-block action"
        reach = attack_rules.monster_attack_range_ft([activity], owner.melee_reach_ft)
        assert reach is not None
        return OpportunityPlan(declaration, None, activity, reach), None
    if (
        mover is None
        or owner.entity_type != "Character"
        or owner.entity_id == mover.entity_id
        or (owner.entity_id in state.party_ids) == (mover.entity_id in state.party_ids)
        or state.opportunity_attack_weapons.get(owner.entity_id) != declaration.weapon_id
        or owner.granted_features is None
    ):
        return None, "declaration requires explicit opposing mover and selected Character weapon"
    if not any(
        entry.owner_id == owner.entity_id
        and entry.item_slug == declaration.weapon_id
        and entry.accessible
        and entry.quantity > 0
        for entry in snapshot.inventory_state.entries
    ):
        return None, "reaction weapon requires an accessible owned inventory instance"
    intent = CombatIntentPayload(
        intent_type="attack",
        weapon_id=declaration.weapon_id,
        target_id=mover.entity_id,
        two_handed=declaration.two_handed,
    )
    refusal = weapon_admission(combatant(owner), intent, loader, common_weapons=True)
    if refusal is not None:
        return None, refusal.error.reason if refusal.error else "incomplete weapon choice"
    weapon = loader.get_weapon(declaration.weapon_id)
    assert weapon is not None
    if not weapon.weapon_category.endswith("melee"):
        return None, "opportunity attack requires a melee weapon"
    activity = (
        weapon.activities[0]
        if weapon.activities
        else attack_rules.synthesize_attack_from_weapon(weapon)
    )
    assert isinstance(activity, AttackActivity)
    reach = (
        owner.melee_reach_ft
        if weapon.slug == "unarmed-strike"
        else attack_rules.weapon_melee_reach_ft(weapon)
    )
    return OpportunityPlan(declaration, weapon, activity, reach), None


def step_reactors(
    snapshot: CombatSnapshot,
    mover_id: str,
    from_cell: str,
    to_cell: str,
    loader: AssetLoader,
    plan: OpportunityPlan | None,
) -> tuple[bool, str | None]:
    """Reject an unrepresented choice; never elect an NPC/player to react."""
    state = snapshot.combat_state
    actors = {a.entity_id: a for a in snapshot.character_states}
    if actors[mover_id].disengaging_this_turn:
        return False, None
    grid = GridTopology(GridScene.model_validate(snapshot.scene_state.grid.model_dump()))
    fires = False
    for owner_id in state.initiative_ids:
        owner = actors[owner_id]
        if (
            (owner_id in state.party_ids) == (mover_id in state.party_ids)
            or owner_id in state.dead_ids
            or not owner.is_alive
            or owner.hp_current <= 0
            or not owner.reaction_available
        ):
            continue
        reach = owner.melee_reach_ft
        monster_slug = state.monster_slug_by_entity.get(owner_id)
        if monster_slug:
            monster = loader.get_monster(monster_slug)
            assert monster is not None  # Complete canonical closure ran before route planning.
            action = next(a for a in monster.actions if a.slug == "greatclub")
            reach = attack_rules.monster_attack_range_ft(action.activities, reach) or reach
        selected = state.opportunity_attack_weapons.get(owner_id)
        if selected and owner.entity_type == "Character":
            weapon = loader.get_weapon(selected)
            if weapon is None:
                return False, "unknown reaction weapon"
            reach = (
                reach
                if selected == "unarmed-strike"
                else attack_rules.weapon_melee_reach_ft(weapon)
            )
        if not leaves_melee_reach(grid, state.actor_zone[owner_id], from_cell, to_cell, reach):
            continue
        if (
            plan is None
            or plan.declaration.owner_id != owner_id
            or plan.declaration.mover_id != mover_id
        ):
            return (
                False,
                "route may provoke an opportunity attack without an authorized declaration",
            )
        fires = True
    return fires, None


def resolve_opportunity(
    computation: CombatComputation, plan: OpportunityPlan, mover_id: str
) -> None:
    """Pay only Reaction; use the original Attack/Damage Activity Resolver."""
    owner_id = plan.declaration.owner_id
    owner = computation.actors[owner_id]
    mover = computation.actors[mover_id]
    computation.emit(IntentSubmitted(actor_id=owner_id, intent_type="reaction", target_id=mover_id))
    owner = owner.model_copy(update={"reaction_available": False})
    computation.actors[owner_id] = owner
    computation.state = computation.state.model_copy(update={"pending_reactions": []})
    grid = GridTopology(
        GridScene.model_validate(computation.snapshot.scene_state.grid.model_dump())
    )
    distance = grid.distance_ft(
        computation.state.actor_zone[owner_id], computation.state.actor_zone[mover_id]
    )
    context = build_activity_context(
        owner,
        [mover],
        rng=computation.rng,
        event_emitter=computation.emit,
        slot_level=None,
        base_spell_level=None,
        spellcasting_ability=None,
        concentration=False,
        source_passive_effects=[],
        spell_book={},
        save_modifiers={},
        passive_damage_modifiers=attack_rules.damage_modifiers(computation.actors.values()),
        target_distance_ft={mover_id: distance} if distance is not None else {},
        target_cover={
            mover_id: grid.cover_between(
                computation.state.actor_zone[owner_id],
                computation.state.actor_zone[mover_id],
                occupied_cells={
                    computation.state.actor_zone[a.entity_id]
                    for a in computation.actors.values()
                    if a.entity_id not in {owner_id, mover_id}
                    and a.is_alive
                    and a.entity_id not in computation.state.dead_ids
                },
            )
        },
        target_dodging={
            mover_id: attack_rules.dodge_benefit_active(
                mover, effective_speed=computation.speed(mover)
            )
        },
        use_versatile_damage=isinstance(plan.declaration, OpportunityAttackState)
        and plan.declaration.two_handed
        and attack_rules.versatile_grip_applies(plan.weapon, distance),
        is_proficient_attack=attack_rules.is_proficient_with_weapon(owner, plan.weapon),
        stat_block_magnitudes=attack_rules.stat_block_magnitudes(owner)
        if isinstance(plan.declaration, StatBlockOpportunityAttackState)
        else None,
        is_opportunity_attack=True,
        attack_origin="opportunity",
        turn_serial=computation.state.turn_serial,
        damage_instance_id_provider=lambda target, source: computation.next_damage_id(
            target, source, actor_id=owner_id
        ),
        damage_instance_resolved=computation.damage_completed,
    )
    if isinstance(plan.declaration, StatBlockOpportunityAttackState):
        context = replace(
            context, activity_source_id=activity_source_identity("monster", "", plan.activity.id)
        )
    resolve_activity(plan.activity, context, weapon=plan.weapon)


def walk_stopped(computation: CombatComputation, mover: Combatant, from_cell: str) -> bool:
    return (
        mover.entity_id in computation.state.dead_ids
        or mover.hp_current <= 0
        or computation.speed(mover) == 0
        or computation.state.actor_zone[mover.entity_id] != from_cell
    )
