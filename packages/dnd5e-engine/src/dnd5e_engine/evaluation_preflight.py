"""Conservative admission and shared, read-only legality for basic attacks."""

import re
from dataclasses import dataclass
from typing import Literal, get_args

from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.common import ActivationBlock, AttackActivity, DamageScalingBlock
from dnd5e_srd_data.schema.monster import MonsterActionKind

from dnd5e_engine import action_policy
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.activities.dice import validate_expression
from dnd5e_engine.evaluation_context import execution_context
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, PreflightChoice, RuleError
from dnd5e_engine.evaluation_state import CombatSnapshot
from dnd5e_engine.events import AttackFailed, CastFailed, DamageType
from dnd5e_engine.spatial import parse_cell
from dnd5e_engine.types.combat import Combatant


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
            or actor.light_weapon_swing_slug
            or actor.restricted_light_weapon_swing_slug
            or actor.action_grants_spent
            or actor.action_grant_groups_spent
        ):
            return "actor effects, features or extended attack budgets are not migrated"
        if actor.hp_current <= 0 or not actor.is_alive or actor.entity_id in state.dead_ids:
            return "pre-existing dying/dead actors require lifecycle migration"
    return None


def activity_support_failure(activity: AttackActivity, *, stat_block: bool = False) -> str | None:
    basic = AttackActivity(id=activity.id, activation=ActivationBlock(type="action", value=1))
    metadata = {"id", "name", "img", "sort", "description", "activation", "duration", "target"}
    if stat_block:
        metadata.update(("attack", "damage", "range"))
    if any(
        getattr(activity, name) != getattr(basic, name)
        for name in type(activity).model_fields
        if name not in metadata
    ):
        return "only one ordinary weapon AttackActivity without additional mechanics is admitted"
    if activity.activation.value not in (None, 1) or activity.activation != ActivationBlock(
        type="action", value=activity.activation.value
    ):
        return "attack requires an ordinary Action activation"
    if (
        activity.duration.value not in (None, "")
        or activity.duration.model_copy(update={"value": None}) != basic.duration
    ):
        return "non-instant activity duration is not migrated"
    target = activity.target
    if target.affects.count not in ("", "1") or target.affects.type not in ("", "creature"):
        return "multi-target or non-creature attack is not migrated"
    normalized = target.model_copy(
        update={
            "prompt": basic.target.prompt,
            "affects": target.affects.model_copy(
                update={"count": basic.target.affects.count, "type": basic.target.affects.type}
            ),
        }
    )
    if normalized != basic.target:
        return "target templates, filters or choices are not migrated"
    return _stat_activity_failure(activity) if stat_block else None


def _stat_activity_failure(activity: AttackActivity) -> str | None:
    attack, damage, reach = activity.attack, activity.damage, activity.range
    if (
        attack.type.value not in ("melee", "ranged")
        or attack.type.classification != "weapon"
        or attack.ability not in ("", "str", "dex", "con", "int", "wis", "cha")
        or attack.critical.threshold not in (None, 20)
        or (attack.bonus and re.fullmatch(r"-?[0-9]+", attack.bonus) is None)
    ):
        return "stat-block attack requires explicit weapon attack mechanics and numeric bonus"
    if (
        reach.units != "ft"
        or not reach.value
        or not reach.value.isdigit()
        or int(reach.value) <= 0
        or reach.special
        or reach.override
    ):
        return "stat-block attack requires explicit positive range in feet"
    # No Weapon is supplied on this path. include_base has no operand; all
    # actual stat-block damage must be present in the typed activity parts.
    if damage.critical.bonus or not damage.parts:
        return "stat-block damage requires explicit ordinary damage parts"
    for part in damage.parts:
        if (
            part.custom.enabled
            or part.custom.formula
            or part.scaling != DamageScalingBlock()
            or part.number is None
            or part.number <= 0
            or part.denomination not in (4, 6, 8, 10, 12)
            or len(part.types) != 1
            or part.types[0] not in get_args(DamageType)
            or (part.bonus and re.fullmatch(r"-?[0-9]+", part.bonus) is None)
        ):
            return "stat-block damage scaling, custom formulas or unknown types are not migrated"
    return None


def template_support_failure(snapshot: CombatSnapshot, loader: AssetLoader) -> str | None:
    for slug in snapshot.combat_state.monster_slug_by_entity.values():
        monster = loader.get_monster(slug)
        if monster is None:
            return "snapshot refers to an absent stat block"
        if (
            monster.special_abilities
            or monster.legendary_actions
            or monster.lair_actions
            or monster.legendary_action_uses
            or monster.legendary_resistance_uses
            or monster.spellcasting_ability
        ):
            return "template passive, legendary or spellcasting mechanics are not migrated"
        if len({action.slug for action in monster.actions}) != len(monster.actions):
            return "ambiguous canonical stat-block action identity"
        if any(action.recharge or action.uses_per_day for action in monster.actions):
            return "template recharge and limited-use turn lifecycle is not migrated"
    return None


def stat_block_admission(
    live: orch._LiveCombat, current: Combatant, intent: CombatIntentPayload, loader: AssetLoader
) -> AttackAdmission | None:
    if (
        current.entity_type not in ("Monster", "NPC")
        or current.entity_id not in live.encounter_ids
        or intent.weapon_id
    ):
        return refused(
            "rejected",
            "action_unavailable",
            "stat-block attack requires a bound NPC and one action reference",
        )
    slug = live.monster_slug_by_entity.get(current.entity_id)
    monster = loader.get_monster(slug) if slug else None
    action = (
        next((a for a in monster.actions if a.slug == intent.stat_block_action_id), None)
        if monster
        else None
    )
    if action is None:
        return refused(
            "rejected",
            "action_unavailable",
            "action ID is absent from the actor's bound stat block",
        )
    # This reserved legacy action dispatch invokes prose-based multiattack planning.
    # Keep it outside the explicit typed subset even if a malformed asset adds an attack.
    if (
        action.slug == "multiattack"
        or action.kind != MonsterActionKind.ACTION
        or action.legendary_cost
        or action.mechanic
        or len(action.activities) != 1
        or not isinstance(action.activities[0], AttackActivity)
    ):
        return refused(
            "unsupported",
            "action.capability",
            "only one typed ordinary NPC attack activity is migrated",
        )
    support = activity_support_failure(action.activities[0], stat_block=True)
    if support:
        return refused("unsupported", "activity.capability", support)
    return None


def weapon_admission(
    current: Combatant, intent: CombatIntentPayload, loader: AssetLoader
) -> AttackAdmission | None:
    if not intent.weapon_id:
        known = tuple(slug for slug in current.carried_item_slugs if loader.get_weapon(slug))
        choices = tuple(
            slug
            for slug in known
            if weapon_admission(current, intent.model_copy(update={"weapon_id": slug}), loader)
            is None
        )
        if choices:
            return AttackAdmission(
                status="needs_choice",
                choice=PreflightChoice(
                    kind="attack.weapon",
                    actor_id=current.entity_id,
                    allowed_weapon_ids=tuple(dict.fromkeys(choices)),
                ),
            )
        if known:
            return refused(
                "unsupported",
                "weapon.capability",
                "no carried weapon is admitted by this evaluator",
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
    return None


def prepare_attack(
    snapshot: CombatSnapshot, actor_id: str, intent: CombatIntentPayload, loader: AssetLoader
) -> AttackAdmission:
    support = snapshot_support_failure(snapshot)
    if support:
        return refused("unsupported", "snapshot.capability", support)
    support = template_support_failure(snapshot, loader)
    if support:
        return refused("unsupported", "template.capability", support)
    live = execution_context(snapshot, loader)
    try:
        current = orch._validate_intent_preconditions(
            live, orch.CombatHandle(live.handle_id), actor_id, intent=intent
        )
    except orch.IntentRejectedError as error:
        return refused("rejected", error.reason, str(error))
    basic = CombatIntentPayload(intent_type="attack")
    allowed = {"intent_type", "source_id", "weapon_id", "target_id", "stat_block_action_id"}
    if intent.intent_type != "attack" or any(
        getattr(intent, name) != getattr(basic, name)
        for name in type(intent).model_fields
        if name not in allowed
    ):
        return refused(
            "unsupported", "intent.capability", "only a single ordinary Attack is admitted"
        )
    if intent.stat_block_action_id is not None:
        input_admission = stat_block_admission(live, current, intent, loader)
        weapon = None
    else:
        input_admission = weapon_admission(current, intent, loader)
        weapon = loader.get_weapon(intent.weapon_id) if intent.weapon_id else None
    if input_admission is not None:
        return input_admission
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
