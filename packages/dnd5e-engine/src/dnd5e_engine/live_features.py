"""Live feature targeting/commit adapters. All legality precedes payment."""

from __future__ import annotations

from typing import TYPE_CHECKING

from dnd5e_engine.activities.dice import validate_expression
from dnd5e_engine.events import CastFailed, ConditionRemoved
from dnd5e_engine.feature_runtime import FeaturePreflightError
from dnd5e_engine.rules.conditions import Condition, is_condition_active

if TYPE_CHECKING:
    from dnd5e_engine.feature_runtime import FeatureInvocation
    from dnd5e_engine.orchestrator import PlayerIntent, _LiveCombat
    from dnd5e_engine.types.combat import Combatant


def feature_target_failure(
    live: _LiveCombat, actor: Combatant, intent: PlayerIntent, invocation: FeatureInvocation
) -> CastFailed | None:
    from dnd5e_engine import orchestrator as orch

    activity = invocation.activities[0]
    failure = CastFailed(actor_id=actor.entity_id, spell_id="", reason="target_invalid")
    plan = orch._area_plan(intent, invocation.activities)
    if plan is not None and plan.template is None:
        return failure.model_copy(update={"reason": "unsupported_feature"})
    if plan is not None:
        return None  # Shared area legality runs before commit.
    if intent.target_ids is not None:
        return failure  # No silent truncation to the primary target.
    target = orch._find_combatant(live, intent.target_id) if intent.target_id else None
    if intent.target_id is not None and target is None:
        return failure
    if invocation.operation == "flurry":
        return None  # Targets are chosen on the paid Unarmed Strikes, not the grant.
    if activity.target.affects.type == "self":
        if target is not None and target.entity_id != actor.entity_id:
            return failure
        target = actor
    if target is None and activity.kind in ("heal", "check"):
        target = actor
    if target is None and invocation.operation == "remove_poison":
        target = actor
    if target is None and activity.target.affects.type not in ("", "self"):
        return failure
    if target is None and activity.kind in ("save", "damage", "heal"):
        return failure
    if target is not None:
        if invocation.target_rule == "other_visible_creature" and (
            target.entity_id == actor.entity_id or not orch._combatant_can_see(live, actor, target)
        ):
            return failure
        if invocation.target_rule == "perceives_caster" and (
            is_condition_active(Condition.UNCONSCIOUS, orch._condition_names(target))
            or (
                is_condition_active(Condition.DEAFENED, orch._condition_names(target))
                and not orch._combatant_can_see(live, target, actor)
            )
        ):
            return failure
        if activity.range.units == "touch":
            distance = 5
        elif activity.range.units == "ft" and activity.range.value:
            try:
                distance = int(activity.range.value)
            except ValueError:
                return failure.model_copy(update={"reason": "unsupported_feature"})
        else:
            distance = None
        if distance is not None:
            start, end = live.actor_zone.get(actor.entity_id), live.actor_zone.get(target.entity_id)
            in_range = (
                live.topology.within_range(start, end, distance)
                if start is not None
                and end is not None
                and invocation.target_rule == "perceives_caster"
                else start is not None
                and end is not None
                and orch._in_range_with_los(live.topology, start, end, distance)
            )
            if not in_range:
                return failure.model_copy(update={"reason": "out_of_range"})
    return None


def feature_state_failure(
    live: _LiveCombat, actor: Combatant, intent: PlayerIntent
) -> CastFailed | None:
    from dnd5e_engine import orchestrator as orch

    if (
        intent.intent_type in ("cast_spell", "ready")
        and intent.spell_id
        and orch._rage_effect(live, actor.entity_id) is not None
    ):
        return CastFailed(actor_id=actor.entity_id, spell_id=intent.spell_id, reason="raging")
    return None


def validate_feature_sidecars(live: _LiveCombat, actor: Combatant) -> None:
    """Validate live bonus expressions read by context construction/resolution."""
    from dnd5e_engine import orchestrator as orch

    payload = orch._build_hydration_payload(live, caster=actor)
    for bucket in ("passive_damage_modifiers", "save_modifiers", "check_modifiers"):
        for entry in payload.get(bucket, {}).values():
            for key, value in entry.items():
                if key.startswith("passive_") and key.endswith("_bonus") and isinstance(value, str):
                    try:
                        validate_expression(value)
                    except ValueError as error:
                        raise FeaturePreflightError(f"invalid live {key}: {error}") from error


def commit_feature_operation(
    live: _LiveCombat, actor_id: str, intent: PlayerIntent, invocation: FeatureInvocation | None
) -> None:
    from dnd5e_engine import orchestrator as orch

    if invocation is None:
        return
    if invocation.operation in ("disengage", "dodge_disengage"):
        orch._set_disengaging(live, actor_id)
        if invocation.operation == "dodge_disengage":
            orch._set_dodging(live, actor_id)
    elif invocation.operation == "dash_heal":
        actor = orch._find_combatant(live, actor_id)
        assert actor is not None
        orch._apply_dash(live, actor, "bonus_action")
    elif invocation.operation == "remove_poison":
        target_id = intent.target_id or actor_id
        if "poisoned" in live.active_conditions.get(target_id, set()):
            orch._emit(
                live, ConditionRemoved(target_id=target_id, condition="poisoned", all_sources=True)
            )
