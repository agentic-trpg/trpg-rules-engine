"""Resolve fixed-area rules and propose condition edges for admitted exploration moves.

The host supplies the coverage edge and current presence for an exact identity.
It owns spatial coverage, persistence, and commit.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from dnd5e_srd_data.loader import AssetLoader

from dnd5e_engine.activities.effects import applicable_condition_statuses
from dnd5e_engine.effect_lifecycle import EffectIdentity
from dnd5e_engine.evaluation_effects import EffectState, effect_state
from dnd5e_engine.evaluation_ruleset import RulesetBinding, get_default_ruleset, verify_ruleset
from dnd5e_engine.rules.conditions import Condition, standalone_condition_matches_compiled
from dnd5e_engine.types.effects import ActiveEffect

LEGACY_REGION_CONDITION_RULE = "condition:poisoned"
_ORIGIN_PREFIX = "region:"
_EFFECT_ID_PREFIX = "effect:region-"
_REGION_RULES = MappingProxyType(
    {
        LEGACY_REGION_CONDITION_RULE: Condition.POISONED.value,
        "condition:deafened": Condition.DEAFENED.value,
    }
)


def _region_origin(scene_id: str, source_id: str, status: str) -> str:
    """Encode the full source reference, with scene-scoped source identity."""
    if not all(isinstance(value, str) and value.strip() for value in (scene_id, source_id)):
        raise ValueError("Region condition requires a scene and source identity")
    return f"{_ORIGIN_PREFIX}{status}:" + json.dumps((scene_id, source_id), separators=(",", ":"))


def _region_effect_id(origin: str, status: str) -> str:
    """Derive a stable effect ID that can be displayed without the source reference."""
    return f"{_EFFECT_ID_PREFIX}{status}:" + hashlib.sha256(origin.encode("ascii")).hexdigest()


def region_condition_identity(
    scene_id: str,
    source_id: str,
    target_id: str,
    *,
    rule_ref: str = LEGACY_REGION_CONDITION_RULE,
) -> EffectIdentity:
    """Return the source-scoped identity, retaining the existing Poisoned encoding."""
    if not isinstance(target_id, str) or not target_id.strip():
        raise ValueError("Region condition requires a target identity")
    status = _rule_status(rule_ref)
    origin = _region_origin(scene_id, source_id, status)
    return (target_id, _region_effect_id(origin, status), origin)


def _rule_status(rule_ref: str) -> str:
    """Look up one supported fixed-area rule by its exact reference."""
    status = _REGION_RULES.get(rule_ref) if isinstance(rule_ref, str) else None
    if status is None:
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
    status = _rule_status(rule_ref)
    definition = loader.get_condition(status)
    legacy = rule_ref == LEGACY_REGION_CONDITION_RULE
    if definition is None:
        if legacy:
            return status, "Poisoned"
        raise ValueError("Fixed-area rule requires its named standalone condition asset")
    if definition.slug != status or not standalone_condition_matches_compiled(definition):
        raise ValueError("Fixed-area rule does not support modified condition mechanics")
    return status, "Poisoned" if legacy else definition.name


def claims_region_condition_identity(effect_id: str, origin: str) -> bool:
    """Recognize either reserved fixed-area identity prefix, including a malformed pair."""
    return effect_id.startswith(_EFFECT_ID_PREFIX) or origin.startswith(_ORIGIN_PREFIX)


def region_condition_status(effect_id: str, origin: str) -> str | None:
    """Return the linked status for an exact canonical fixed-area effect identity."""
    if not effect_id.startswith(_EFFECT_ID_PREFIX) or not origin.startswith(_ORIGIN_PREFIX):
        return None
    try:
        _, status, encoded = origin.split(":", 2)
        if f"condition:{status}" not in _REGION_RULES:
            return None
        source = json.loads(encoded)
        if not isinstance(source, list) or len(source) != 2:
            return None
        canonical_origin = _region_origin(source[0], source[1], status)
    except (ValueError, RecursionError):
        return None
    if canonical_origin == origin and _region_effect_id(canonical_origin, status) == effect_id:
        return status
    return None


def is_region_condition(effect_id: str, origin: str) -> bool:
    """Recognize a paired region effect ID and canonical source origin."""
    return region_condition_status(effect_id, origin) is not None


def is_fixed_region_condition_effect(effect: EffectState) -> bool:
    """Check the untimed, condition-only profile of a fixed-area effect."""
    status = region_condition_status(effect.id, effect.origin)
    return (
        status is not None
        and effect.statuses == {status}
        and not effect.disabled
        and not effect.transfer
        and not effect.changes
        and effect.action_policy is None
        and not effect.flags
        and effect.lifecycle is None
        and not effect.end_effects
        and all(value is None for value in effect.duration.model_dump().values())
    )


@dataclass(frozen=True, slots=True)
class RegionConditionTransition:
    """Carry the uncommitted operation and the effect payload for an addition."""

    operation: Literal["none", "add", "remove"]
    identity: EffectIdentity
    effect: EffectState | None = None


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
) -> RegionConditionTransition:
    """Plan one edge using presence for this source and the target's immunities.

    The linked condition lasts until the target leaves coverage. Immunity
    prevents addition and never prevents removal of an existing link. Verify the
    binding before proposing any transition, including a no-op.
    """
    status, name = resolve_region_condition_rule(
        rule_ref, ruleset_binding=ruleset_binding, loader=loader
    )
    identity = region_condition_identity(scene_id, source_id, target_id, rule_ref=rule_ref)
    if not entering:
        return RegionConditionTransition("remove" if present else "none", identity)
    statuses = (status,)
    if present or not applicable_condition_statuses(
        statuses, condition_immunities=condition_immunities
    ):
        return RegionConditionTransition("none", identity)

    target, effect_id, origin = identity
    effect = ActiveEffect(
        id=effect_id, name=name, origin=origin, target_id=target, statuses=set(statuses)
    )
    return RegionConditionTransition("add", identity, effect_state(effect))
