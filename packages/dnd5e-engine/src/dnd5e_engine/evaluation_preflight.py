"""Conservative admission and shared, read-only legality for basic attacks."""

from dataclasses import dataclass
from typing import Literal, get_args

from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.common import ActivationBlock, AttackActivity

from dnd5e_engine import action_policy
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.activities.dice import validate_expression
from dnd5e_engine.evaluation_context import execution_context
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, PreflightChoice, RuleError
from dnd5e_engine.evaluation_state import CombatSnapshot
from dnd5e_engine.events import AttackFailed, CastFailed, DamageType
from dnd5e_engine.spatial import parse_cell


@dataclass(frozen=True)
class AttackAdmission:
    status: Literal["accepted", "rejected", "needs_choice", "unsupported"]
    error: RuleError | None = None
    choice: PreflightChoice | None = None
    context: orch._LiveCombat | None = None


def refused(status: Literal["rejected", "unsupported"], code: str, reason: str) -> AttackAdmission:
    return AttackAdmission(status=status, error=RuleError(code=code, reason=reason))


def snapshot_support_failure(snapshot: CombatSnapshot) -> str | None:
    state = snapshot.combat_state
    # Full dependencies remain in the DTO, even when this slice cannot execute them.
    unsupported = (
        snapshot.effect_states,
        state.timed_activities.pending,
        state.persistent_areas.areas,
        state.persistent_areas.next_turn_start_effects,
        state.combat_objects.objects,
        state.concentration_chain,
        state.concentration_rounds_remaining,
        state.conditions_by_effect,
        state.effect_lifecycles,
        state.pending_reactions,
        state.active_reaction_responses,
        state.rider_uses,
        state.reaction_effects_pending_expiry,
        state.help_grants,
        state.help_check_grants,
        state.hidden_entities,
        state.vex_grants,
        state.rage_bonus_extensions,
        state.sap_marks,
        state.slow_marks,
        state.legendary_windows_used,
        state.legendary_resistance_armed,
        state.legendary_resistance_applied_event_indices,
        state.constructs,
        state.transforms,
        state.summons,
        state.summon_counts,
        state.departed_actor_id,
        state.monster_slug_by_entity,
        any(state.monster_action_uses_by_entity.values()),
    )
    if any(unsupported):
        return "complex effects, reactions, timing or sidecar state are not migrated"
    if state.party_ids | state.encounter_ids != set(state.initiative_ids):
        return "every actor requires an explicit combat side"
    grid = snapshot.scene_state.grid
    if (
        grid.blocked_cells
        or grid.wall_segments
        or grid.cover_cells
        or grid.difficult_terrain_cells
        or grid.lighting
        or grid.obscurement_cells
        or grid.default_lighting != "bright"
        or grid.sunlight
    ):
        return "only a plain, brightly lit grid is admitted"
    for cell in state.actor_zone.values():
        col, row = parse_cell(cell)
        if cell != f"{col},{row}" or not (0 <= col < grid.width and 0 <= row < grid.height):
            return "actor positions must be canonical cells within the supplied grid"
    for actor in snapshot.character_states:
        if (
            actor.conditions
            or actor.concentration_effect_id
            or actor.trait_mechanics
            or actor.class_slug
            or actor.classes
            or actor.subclass_slug
            or actor.species_slug
            or actor.granted_features
            or actor.fighting_styles
            or actor.legendary_actions_max
            or actor.legendary_resistances_max
            or actor.extra_actions_remaining
            or actor.flurry_strikes_remaining
            or actor.attacks_remaining > 1
        ):
            return "actor effects, features or extended attack budgets are not migrated"
        if actor.hp_current <= 0 or not actor.is_alive or actor.entity_id in state.dead_ids:
            return "pre-existing dying/dead actors require lifecycle migration"
    return None


def activity_support_failure(activity: AttackActivity) -> str | None:
    basic = AttackActivity(id=activity.id, activation=ActivationBlock(type="action", value=1))
    metadata = {"id", "name", "img", "sort", "description"}
    if any(
        getattr(activity, name) != getattr(basic, name)
        for name in type(activity).model_fields
        if name not in metadata
    ):
        return "only one ordinary weapon AttackActivity without additional mechanics is admitted"
    return None


def prepare_attack(
    snapshot: CombatSnapshot, actor_id: str, intent: CombatIntentPayload, loader: AssetLoader
) -> AttackAdmission:
    support = snapshot_support_failure(snapshot)
    if support:
        return refused("unsupported", "snapshot.capability", support)
    live = execution_context(snapshot, loader)
    try:
        current = orch._validate_intent_preconditions(
            live, orch.CombatHandle(live.handle_id), actor_id, intent=intent
        )
    except orch.IntentRejectedError as error:
        return refused("rejected", error.reason, str(error))
    if current.entity_type != "Character" or actor_id not in live.party_ids:
        return refused("unsupported", "actor.capability", "explicit NPC attack is not migrated yet")
    basic = CombatIntentPayload(intent_type="attack")
    allowed = {"intent_type", "source_id", "weapon_id", "target_id"}
    if intent.intent_type != "attack" or any(
        getattr(intent, name) != getattr(basic, name)
        for name in type(intent).model_fields
        if name not in allowed
    ):
        return refused(
            "unsupported", "intent.capability", "only a single ordinary Attack is admitted"
        )
    if not intent.weapon_id:
        choices = tuple(slug for slug in current.carried_item_slugs if loader.get_weapon(slug))
        if choices:
            return AttackAdmission(
                status="needs_choice",
                choice=PreflightChoice(
                    kind="attack.weapon",
                    actor_id=actor_id,
                    allowed_weapon_ids=tuple(dict.fromkeys(choices)),
                ),
            )
        return refused(
            "rejected", "action_unavailable", "no explicit weapon or carried weapon choice"
        )
    weapon = loader.get_weapon(intent.weapon_id)
    if weapon is None or weapon.slug not in current.carried_item_slugs:
        return refused(
            "rejected", "action_unavailable", "weapon is absent from actor's supplied equipment"
        )
    if orch._weapon_attack_range_ft(weapon) is None:
        return refused(
            "unsupported", "range.capability", "weapon requires an explicit range in feet"
        )
    if weapon.properties or weapon.mastery or weapon.passive_effects or weapon.uses:
        return refused(
            "unsupported",
            "weapon.capability",
            "weapon properties, mastery, effects or uses need migration",
        )
    activities = weapon.activities or [orch._synthesize_attack_from_weapon(weapon)]
    if len(activities) != 1 or not isinstance(activities[0], AttackActivity):
        return refused(
            "unsupported",
            "activity.capability",
            "attack must resolve exactly one typed AttackActivity",
        )
    support = activity_support_failure(activities[0])
    if support:
        return refused("unsupported", "activity.capability", support)
    if not weapon.damage_parts or any(
        part.damage_type not in get_args(DamageType) for part in weapon.damage_parts
    ):
        return refused(
            "unsupported", "damage.capability", "attack requires known typed damage parts"
        )
    for part in weapon.damage_parts:
        validate_expression(part.dice)
    target = orch._find_combatant(live, intent.target_id or "")
    if (
        target is None
        or target.entity_id == actor_id
        or not orch._is_enemy(live, actor_id, target.entity_id)
    ):
        return refused("rejected", "target_invalid", "target must be a present opposing actor")
    cost = orch._classify_action_cost(intent, None)
    funding = orch._classify_attack_funding(current, intent, weapon, repeats_construct=False)
    try:
        action_policy.validate_grant(live, current, intent)
        funding = action_policy.preflight_intent_policy(
            live, current, intent, cost, funding, weapon
        )
    except orch.IntentRejectedError as error:
        return refused("rejected", error.reason, str(error))
    failure = orch._intent_pre_resolution_failure(
        live, current, intent, None, weapon, funding, cost
    )
    if failure is not None:
        if not isinstance(failure, (AttackFailed, CastFailed)):
            raise RuntimeError("unexpected basic attack refusal event")
        return refused(
            "rejected", str(failure.reason), "shared attack preflight refused the intent"
        )
    return AttackAdmission(status="accepted", context=live)
