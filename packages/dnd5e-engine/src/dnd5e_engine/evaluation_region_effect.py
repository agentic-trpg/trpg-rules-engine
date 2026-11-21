"""Resolve fixed-area rules and propose effect edges for admitted spatial boundaries.

The host supplies the coverage edge and current presence for an exact identity.
It owns spatial coverage, persistence, and commit.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.common import PassiveEffect
from dnd5e_srd_data.schema.condition import ConditionEffectKind

from dnd5e_engine.activities.check_pipeline import validate_check_effect_change
from dnd5e_engine.activities.effects import (
    applicable_condition_statuses,
    passive_effect_to_active_effect,
)
from dnd5e_engine.effect_lifecycle import EffectIdentity
from dnd5e_engine.evaluation_effects import EffectState, effect_state
from dnd5e_engine.evaluation_ruleset import RulesetBinding, get_default_ruleset, verify_ruleset
from dnd5e_engine.rules.conditions import (
    Condition,
    conditions_auto_fail_check,
    conditions_cannot_move_toward_fear_source,
    conditions_unseen,
    project_speed,
    standalone_condition_matches_compiled,
)
from dnd5e_engine.types.effects import ActiveEffect, ActiveEffectChange

if TYPE_CHECKING:
    from dnd5e_engine.evaluation_state import CombatSnapshot, ConditionRecord

LEGACY_REGION_CONDITION_RULE = "condition:poisoned"
_ORIGIN_PREFIX = "region:"
_EFFECT_ID_PREFIX = "effect:region-"
_PASSIVE_PROFILE = "passive"
# Fixed coverage supplies no source actor, level ledger, or additional mutation.
_CONTEXTUAL_CONDITION_CLAUSES = frozenset(
    {
        ConditionEffectKind.CANT_ATTACK_CHARMER,
        ConditionEffectKind.CHARMER_SOCIAL_ADVANTAGE,
        ConditionEffectKind.CANT_MOVE_TOWARD_FEAR_SOURCE,
        ConditionEffectKind.DISADVANTAGE_ATTACKS_EXCEPT_GRAPPLER,
        ConditionEffectKind.MOVABLE_BY_GRAPPLER,
        ConditionEffectKind.D20_TEST_PENALTY_PER_LEVEL,
        ConditionEffectKind.SPEED_PENALTY_PER_LEVEL,
        ConditionEffectKind.DEATH_AT_LEVEL,
        ConditionEffectKind.DROPS_HELD_ITEMS,
        ConditionEffectKind.BREAKS_CONCENTRATION,
    }
)


def _region_origin(
    scene_id: str, source_id: str, profile: str, statuses: tuple[str, ...] = ()
) -> str:
    """Encode the full source reference, with scene-scoped source identity."""
    if not all(isinstance(value, str) and value.strip() for value in (scene_id, source_id)):
        raise ValueError("Region condition requires a scene and source identity")
    source = (
        (scene_id, source_id, statuses) if profile == _PASSIVE_PROFILE else (scene_id, source_id)
    )
    return f"{_ORIGIN_PREFIX}{profile}:" + json.dumps(source, separators=(",", ":"))


def _region_effect_id(origin: str, status: str) -> str:
    """Derive a stable effect ID that can be displayed without the source reference."""
    return f"{_EFFECT_ID_PREFIX}{status}:" + hashlib.sha256(origin.encode("ascii")).hexdigest()


def _region_identity(
    scene_id: str, source_id: str, target_id: str, profile: str, statuses: tuple[str, ...] = ()
) -> EffectIdentity:
    """Return an exact target/source identity for a validated region profile."""
    if not isinstance(target_id, str) or not target_id.strip():
        raise ValueError("Region effect requires a target identity")
    origin = _region_origin(scene_id, source_id, profile, statuses)
    return (target_id, _region_effect_id(origin, profile), origin)


def region_condition_identity(
    scene_id: str,
    source_id: str,
    target_id: str,
    *,
    rule_ref: str = LEGACY_REGION_CONDITION_RULE,
) -> EffectIdentity:
    """Return the source-scoped identity, retaining the existing Poisoned encoding."""
    return _region_identity(scene_id, source_id, target_id, _rule_status(rule_ref))


def region_effect_identity(
    scene_id: str, source_id: str, target_id: str, rule: str | PassiveEffect
) -> EffectIdentity:
    """Resolve the identity independently of target immunity or current presence."""
    if isinstance(rule, PassiveEffect):
        statuses = tuple(sorted(set(rule.statuses)))
        return _region_identity(scene_id, source_id, target_id, _PASSIVE_PROFILE, statuses)
    return region_condition_identity(scene_id, source_id, target_id, rule_ref=rule)


def _rule_status(rule_ref: str) -> str:
    """Decode a canonical RE condition slug from its exact reference."""
    prefix, sep, status = rule_ref.partition(":") if isinstance(rule_ref, str) else ("", "", "")
    if (prefix, sep) != ("condition", ":") or status not in Condition:
        raise ValueError("Unknown fixed-area condition rule")
    return status


def resolve_region_condition_rule(
    rule_ref: str,
    *,
    ruleset_binding: RulesetBinding,
    loader: AssetLoader | None = None,
) -> tuple[str, str]:
    """Validate the rule against the bound content and return its status and display name.

    The original Poisoned rule is fixed by the evaluator version when a reduced
    corpus omits the condition asset. Supplied definitions must agree with the
    compiled mechanics.
    """
    loader = loader if loader is not None else get_default_ruleset()
    verify_ruleset(ruleset_binding, loader)
    return _resolve_condition(rule_ref, loader, allow_omitted_legacy=True)


def _resolve_condition(
    rule_ref: str, loader: AssetLoader, *, allow_omitted_legacy: bool
) -> tuple[str, str]:
    """Validate a bound condition and its prerequisites for entering and leaving coverage."""
    status = _rule_status(rule_ref)
    definition = loader.get_condition(status)
    legacy = rule_ref == LEGACY_REGION_CONDITION_RULE
    if definition is None:
        if legacy and allow_omitted_legacy:
            return status, "Poisoned"
        raise ValueError("Fixed-area rule requires its named standalone condition asset")
    if definition.slug != status or not standalone_condition_matches_compiled(definition):
        raise ValueError("Fixed-area rule does not support modified condition mechanics")
    if any(
        effect.gate is not None or effect.kind in _CONTEXTUAL_CONDITION_CLAUSES
        for effect in definition.effects
    ):
        raise ValueError("Fixed-area condition needs additional rule context or state changes")
    if not region_conditions_preserve_movement(
        [status]
    ) or not region_conditions_preserve_visibility([status]):
        raise ValueError("Fixed-area condition would prevent a supported exploration exit")
    return status, "Poisoned" if legacy else definition.name


def claims_region_condition_identity(effect_id: str, origin: str) -> bool:
    """Recognize either reserved fixed-area identity prefix, including a malformed pair."""
    return effect_id.startswith(_EFFECT_ID_PREFIX) or origin.startswith(_ORIGIN_PREFIX)


def _region_profile(effect_id: str, origin: str) -> tuple[str, tuple[str, ...]] | None:
    """Return the profile encoded in an exact canonical fixed-area identity."""
    if not effect_id.startswith(_EFFECT_ID_PREFIX) or not origin.startswith(_ORIGIN_PREFIX):
        return None
    try:
        _, profile, encoded = origin.split(":", 2)
        source = json.loads(encoded)
        if not isinstance(source, list):
            return None
        if profile == _PASSIVE_PROFILE and len(source) == 3 and isinstance(source[2], list):
            if not all(isinstance(status, str) and status in Condition for status in source[2]):
                return None
            statuses = tuple(sorted(set(source[2])))
        elif profile in Condition and len(source) == 2:
            statuses = (profile,)
        else:
            return None
        canonical_origin = _region_origin(source[0], source[1], profile, statuses)
    except (ValueError, RecursionError):
        return None
    if canonical_origin == origin and _region_effect_id(canonical_origin, profile) == effect_id:
        return profile, statuses
    return None


def region_condition_status(effect_id: str, origin: str) -> str | None:
    """Return the linked status for an exact canonical condition identity."""
    resolved = _region_profile(effect_id, origin)
    if resolved is None or resolved[0] == _PASSIVE_PROFILE:
        return None
    return resolved[0]


def matches_region_effect_statuses(effect_id: str, origin: str, statuses: Collection[str]) -> bool:
    """Check the source lineage, allowing immunity to remove declared template statuses."""
    resolved = _region_profile(effect_id, origin)
    if resolved is None:
        return False
    profile, declared = resolved
    active = set(statuses)
    if len(active) != len(statuses):
        return False
    return active.issubset(declared) if profile == _PASSIVE_PROFILE else active == set(declared)


def region_conditions_preserve_movement(statuses: list[str]) -> bool:
    """Check for canonical movement dependencies absent from fixed Grid steps."""
    if project_speed(1, statuses) == 0 or conditions_cannot_move_toward_fear_source(statuses):
        return False
    for status in statuses:
        definition = get_default_ruleset().get_condition(status)
        if definition is None or any(
            clause.kind == ConditionEffectKind.RESTRICTED_MOVEMENT_CRAWL
            for clause in definition.effects
        ):
            return False
    return True


def region_conditions_preserve_visibility(statuses: list[str]) -> bool:
    """Check whether ordinary visual authorization still has its established premises."""
    return not (
        conditions_auto_fail_check(statuses, "sight")
        or conditions_unseen(statuses, observer_can_see_bearer=False)
    )


def is_region_condition(effect_id: str, origin: str) -> bool:
    """Recognize a paired region effect ID and canonical source origin."""
    return region_condition_status(effect_id, origin) is not None


def _validate_passive_changes(changes: Collection[ActiveEffectChange]) -> None:
    """Validate known check mechanics while preserving the open native modifier key space."""
    for change in changes:
        if not change.key.strip():
            raise ValueError("A passive effect change requires a key")
        validate_check_effect_change(change)


def is_fixed_region_effect(effect: EffectState) -> bool:
    """Check the canonical source identity and coverage-only lifetime of a fixed-area effect."""
    if (
        matches_region_effect_statuses(effect.id, effect.origin, effect.statuses)
        and not effect.disabled
        and not effect.transfer
        and effect.action_policy is None
        and not effect.flags
        and effect.lifecycle is None
        and not effect.end_effects
        and all(value is None for value in effect.duration.model_dump().values())
    ):
        if region_condition_status(effect.id, effect.origin) is not None:
            return not effect.changes
        return bool(effect.statuses or effect.changes) and all(
            change.key.strip() for change in effect.changes
        )
    return False


def is_fixed_region_condition_effect(effect: EffectState) -> bool:
    """Check the existing condition-only profile of a fixed-area effect."""
    return region_condition_status(effect.id, effect.origin) is not None and is_fixed_region_effect(
        effect
    )


def is_fixed_combat_poisoned_condition(condition: ConditionRecord) -> bool:
    """Recognize the exact condition projection after its region effect is verified."""
    return (
        condition.condition == "poisoned"
        and condition.source_entity_id == "implied:effect"
        and condition.scope == "session"
        and condition.source_effect_id is not None
        and condition.source_effect_id.startswith(_EFFECT_ID_PREFIX)
        and condition.duration_rounds is None
        and condition.save_dc is None
        and condition.applied_round == 0
        and condition.exhaustion_level == 1
    )


def supports_fixed_combat_regions(snapshot: CombatSnapshot, loader: AssetLoader) -> bool:
    """Admit only matching current-region Poisoned effects without clocks or other effects."""
    state = snapshot.combat_state
    if (
        snapshot.snapshot_schema_version != "engine-snapshot/20"
        or not snapshot.effect_states
        or state.effect_lifecycles
        or state.concentration_chain
        or state.concentration_rounds_remaining
        or state.pending_reactions
        or state.active_reaction_responses
        or state.reaction_effects_pending_expiry
        or state.sap_marks
    ):
        return False
    try:
        _resolve_condition(LEGACY_REGION_CONDITION_RULE, loader, allow_omitted_legacy=True)
    except ValueError:
        return False
    for effect in snapshot.effect_states:
        if (
            not is_fixed_region_condition_effect(effect)
            or region_condition_status(effect.id, effect.origin) != "poisoned"
            or json.loads(effect.origin.split(":", 2)[2])[0] != snapshot.scene_state.scene_id
        ):
            return False
    identities = {(effect.target_id, effect.id, effect.origin) for effect in snapshot.effect_states}
    lineage = {
        link.identity for link in state.conditions_by_effect if link.statuses == ["poisoned"]
    }
    if lineage != identities or len(state.conditions_by_effect) != len(identities):
        return False
    sources = {(effect.target_id, effect.id) for effect in snapshot.effect_states}
    records = [
        (actor.entity_id, condition.source_effect_id)
        for actor in snapshot.character_states
        for condition in actor.conditions
        if is_fixed_combat_poisoned_condition(condition)
    ]
    return len(records) == sum(len(a.conditions) for a in snapshot.character_states) and (
        len(records) == len(sources) == len(snapshot.effect_states) and set(records) == sources
    )


@dataclass(frozen=True, slots=True)
class RegionEffectTransition:
    """Carry the uncommitted operation and the effect payload for an addition."""

    operation: Literal["none", "add", "remove"]
    identity: EffectIdentity
    effect: EffectState | None = None


def _configured_region_effect(template: PassiveEffect, loader: AssetLoader) -> ActiveEffect:
    """Convert an authored passive modifier using the ordinary active-effect adapter."""
    if (
        not template.name.strip()
        or template.disabled
        or template.transfer
        or template.duration
        or template.action_policy is not None
        or not (template.statuses or template.changes)
    ):
        raise ValueError("Fixed-area effects require an enabled, untimed passive template")
    for status in dict.fromkeys(template.statuses):
        _resolve_condition(f"condition:{status}", loader, allow_omitted_legacy=False)
    active = passive_effect_to_active_effect(template, target_id="", caster_id="")
    _validate_passive_changes(active.changes)
    return active


def validate_region_effect_rule(
    rule: str | PassiveEffect,
    *,
    ruleset_binding: RulesetBinding,
    loader: AssetLoader | None = None,
) -> None:
    """Reject a source whose bound condition or passive modifier requires missing mechanics."""
    _resolve_region_effect(rule, ruleset_binding, loader)


def _resolve_region_effect(
    rule: str | PassiveEffect, binding: RulesetBinding, loader: AssetLoader | None
) -> ActiveEffect:
    """Resolve an enabled template under the supplied frozen ruleset binding."""
    assets = loader if loader is not None else get_default_ruleset()
    verify_ruleset(binding, assets)
    if isinstance(rule, PassiveEffect):
        return _configured_region_effect(rule, assets)
    status, name = _resolve_condition(rule, assets, allow_omitted_legacy=True)
    return ActiveEffect(id="", name=name, origin="", target_id="", statuses={status})


def plan_region_effect_transition(
    *,
    ruleset_binding: RulesetBinding,
    scene_id: str,
    source_id: str,
    target_id: str,
    entering: bool,
    present: bool,
    condition_immunities: Collection[str],
    loader: AssetLoader | None = None,
    rule: str | PassiveEffect,
) -> RegionEffectTransition:
    """Plan one edge using presence for this source and the target's immunities.

    The effect lasts until the target leaves coverage. Condition immunity
    prevents addition and never prevents removal of an existing link. Verify the
    binding before proposing any transition, including a no-op.
    """
    identity = region_effect_identity(scene_id, source_id, target_id, rule)
    template = _resolve_region_effect(rule, ruleset_binding, loader)
    if not entering:
        return RegionEffectTransition("remove" if present else "none", identity)
    statuses = applicable_condition_statuses(
        template.statuses, condition_immunities=condition_immunities
    )
    if present or not (statuses or template.changes):
        return RegionEffectTransition("none", identity)

    target, effect_id, origin = identity
    effect = template.model_copy(
        update={"id": effect_id, "origin": origin, "target_id": target, "statuses": set(statuses)}
    )
    return RegionEffectTransition("add", identity, effect_state(effect))


def plan_region_condition_transition(
    *,
    ruleset_binding: RulesetBinding,
    scene_id: str,
    source_id: str,
    target_id: str,
    entering: bool,
    present: bool,
    condition_immunities: Collection[str],
    loader: AssetLoader | None = None,
    rule_ref: str = LEGACY_REGION_CONDITION_RULE,
) -> RegionEffectTransition:
    """Plan a condition edge through the shared fixed-area effect ingress."""
    return plan_region_effect_transition(
        ruleset_binding=ruleset_binding,
        scene_id=scene_id,
        source_id=source_id,
        target_id=target_id,
        entering=entering,
        present=present,
        condition_immunities=condition_immunities,
        loader=loader,
        rule=rule_ref,
    )
