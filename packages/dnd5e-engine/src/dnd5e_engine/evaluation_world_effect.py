"""Uncommitted world-time effect proposals and the legacy Longstrider planner.

The host admits the cast and owns game time, scheduling, effect persistence, and
transaction commit. The result is a proposal, not world state.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Annotated, Literal

from dnd5e_srd_data.loader import AssetLoader
from pydantic import Field, model_validator

from dnd5e_engine.activities.effects import passive_effect_to_active_effect
from dnd5e_engine.evaluation_base import EvaluationModel
from dnd5e_engine.evaluation_effects import EffectState, effect_state
from dnd5e_engine.evaluation_ruleset import (
    RulesetBinding,
    get_default_ruleset,
    verify_ruleset,
)

EffectIdentity = tuple[str, str, str]


class WorldEffectApplicationRequest(EvaluationModel):
    """A host-authorized Longstrider application without a supplied clock."""

    schema_version: Literal["engine-world-effect/1"]
    session_id: Annotated[str, Field(min_length=1)]
    command_id: Annotated[str, Field(min_length=1)]
    source_id: Annotated[str, Field(min_length=1)]
    target_id: Annotated[str, Field(min_length=1)]
    ruleset_binding: RulesetBinding


class EffectExpiryPolicy(EvaluationModel):
    """Deterministic terminal action when the host's duration reaches its end."""

    action: Literal["remove"]


class WorldEffectApplication(EvaluationModel):
    """Bind an elapsed-time effect and its terminal policy to its original invocation.

    Concentration requests a replacement of the caster's current concentration at
    cast adoption. The host owns the existing group's identity and World clock.
    """

    session_id: str
    command_id: str
    source_id: str
    effect: EffectState
    expiry: EffectExpiryPolicy
    concentration: bool = False
    ruleset_binding: RulesetBinding

    @property
    def identity(self) -> EffectIdentity:
        """Return the full identity of the one proposed effect."""
        return (self.effect.target_id, self.effect.id, self.effect.origin)

    @property
    def duration_seconds(self) -> int:
        """Return the finite duration carried by the converted canonical effect."""
        seconds = self.effect.duration.seconds
        if seconds is None or seconds <= 0:
            raise ValueError("world effect has no finite positive seconds duration")
        return seconds

    @model_validator(mode="after")
    def finite_duration(self) -> WorldEffectApplication:
        """Require a positive typed seconds duration on the proposed effect."""
        _ = self.duration_seconds
        if self.effect.flags.get("concentration", False) is not self.concentration:
            raise ValueError("world effect concentration differs from its application")
        return self

    def verify_request(self, request: WorldEffectApplicationRequest) -> None:
        """Check the host association before adopting this uncommitted result."""
        if (
            self.session_id != request.session_id
            or self.command_id != request.command_id
            or self.source_id != request.source_id
            or self.effect.target_id != request.target_id
            or self.ruleset_binding != request.ruleset_binding
        ):
            raise ValueError("world effect result does not match its request")


def _canonical_longstrider(loader: AssetLoader, source_id: str, target_id: str) -> EffectState:
    """Build the reviewed passive effect through the shared conversion seam."""
    spell = loader.get_spell("longstrider")
    original = get_default_ruleset().get_spell("longstrider")
    if spell is None or spell != original:
        raise ValueError("Longstrider differs from the reviewed canonical spell")
    if len(spell.passive_effects) != 1:
        raise ValueError("Longstrider has no unique reviewed passive effect")
    effect = effect_state(
        passive_effect_to_active_effect(
            spell.passive_effects[0], target_id=target_id, caster_id=source_id
        )
    )
    return effect


def plan_longstrider_application(
    request: WorldEffectApplicationRequest, *, loader: AssetLoader | None = None
) -> WorldEffectApplication:
    """Propose one canonical effect and duration after host cast admission."""
    request = WorldEffectApplicationRequest.model_validate(deepcopy(request))
    assets = loader if loader is not None else get_default_ruleset()
    verify_ruleset(request.ruleset_binding, assets)
    effect = _canonical_longstrider(assets, request.source_id, request.target_id)
    result = WorldEffectApplication(
        session_id=request.session_id,
        command_id=request.command_id,
        source_id=request.source_id,
        effect=effect,
        expiry=EffectExpiryPolicy(action="remove"),
        ruleset_binding=request.ruleset_binding,
    )
    result.verify_request(request)
    return result
