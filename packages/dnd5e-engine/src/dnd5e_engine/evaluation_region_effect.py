"""Propose Poisoned transitions for a host-admitted fixed exploration region.

The host supplies the coverage edge and current presence for an exact identity.
It owns spatial coverage, persistence, and commit.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

from dnd5e_srd_data.loader import AssetLoader

from dnd5e_engine.activities.effects import applicable_condition_statuses
from dnd5e_engine.effect_lifecycle import EffectIdentity
from dnd5e_engine.evaluation_effects import EffectState, effect_state
from dnd5e_engine.evaluation_ruleset import RulesetBinding, get_default_ruleset, verify_ruleset
from dnd5e_engine.rules.conditions import Condition
from dnd5e_engine.types.effects import ActiveEffect

_ORIGIN_PREFIX = "region:poisoned:"
_EFFECT_ID_PREFIX = "effect:region-poisoned:"


def _region_origin(scene_id: str, source_id: str) -> str:
    """Encode the full source reference, with scene-scoped source identity."""
    if not all(isinstance(value, str) and value.strip() for value in (scene_id, source_id)):
        raise ValueError("Region condition requires a scene and source identity")
    return _ORIGIN_PREFIX + json.dumps((scene_id, source_id), separators=(",", ":"))


def _region_effect_id(origin: str) -> str:
    """Derive a stable effect ID that can be displayed without the source reference."""
    return _EFFECT_ID_PREFIX + hashlib.sha256(origin.encode("ascii")).hexdigest()


def region_condition_identity(scene_id: str, source_id: str, target_id: str) -> EffectIdentity:
    """Return the full effect identity for one region source and target."""
    if not isinstance(target_id, str) or not target_id.strip():
        raise ValueError("Region condition requires a target identity")
    origin = _region_origin(scene_id, source_id)
    return (target_id, _region_effect_id(origin), origin)


def is_region_condition(effect_id: str, origin: str) -> bool:
    """Recognize a paired region effect ID and canonical source origin."""
    if not effect_id.startswith(_EFFECT_ID_PREFIX) or not origin.startswith(_ORIGIN_PREFIX):
        return False
    try:
        source = json.loads(origin.removeprefix(_ORIGIN_PREFIX))
        if not isinstance(source, list) or len(source) != 2:
            return False
        canonical_origin = _region_origin(source[0], source[1])
    except (ValueError, RecursionError):
        return False
    return canonical_origin == origin and _region_effect_id(canonical_origin) == effect_id


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
) -> RegionConditionTransition:
    """Plan one edge using presence for this source and the target's immunities.

    A linked Poisoned effect lasts until the target leaves coverage. Immunity
    prevents addition and never prevents removal of an existing link. Verify the
    binding before proposing any transition, including a no-op.
    """
    identity = region_condition_identity(scene_id, source_id, target_id)
    verify_ruleset(ruleset_binding, loader if loader is not None else get_default_ruleset())
    if not entering:
        return RegionConditionTransition("remove" if present else "none", identity)
    statuses = (Condition.POISONED.value,)
    if present or not applicable_condition_statuses(
        statuses, condition_immunities=condition_immunities
    ):
        return RegionConditionTransition("none", identity)

    target, effect_id, origin = identity
    effect = ActiveEffect(
        id=effect_id, name="Poisoned", origin=origin, target_id=target, statuses=set(statuses)
    )
    return RegionConditionTransition("add", identity, effect_state(effect))
