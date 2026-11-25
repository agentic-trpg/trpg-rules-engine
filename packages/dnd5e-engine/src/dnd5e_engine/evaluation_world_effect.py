"""Uncommitted world-time effect proposals and the legacy Longstrider planner.

The host admits the cast and owns game time, scheduling, effect persistence, and
transaction commit. The result is a proposal, not world state.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Annotated, Literal

from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.spell import Spell
from pydantic import Field, model_validator

from dnd5e_engine.activities.effects import passive_effect_to_active_effect
from dnd5e_engine.evaluation_base import EvaluationModel
from dnd5e_engine.evaluation_effects import EffectState, effect_state
from dnd5e_engine.evaluation_ruleset import (
    FixedSpellDuration,
    RulesetBinding,
    get_default_ruleset,
    spell_duration_policy,
    verify_ruleset,
)
from dnd5e_engine.turn_rules import ROUND_DURATION_SECONDS
from dnd5e_engine.types.effects import ActiveEffectDuration

EffectIdentity = tuple[str, str, str]
DURATION_OVERLAY_SPELLS = ("aid", "longstrider")


def reviewed_duration_seconds(spell: Spell) -> int:
    """Convert the reviewed canonical spell duration using the SRD units."""
    duration = spell.duration
    units = {"second": 1, "minute": 60, "hour": 3600}
    if duration.units not in units or not duration.value or duration.value <= 0:
        raise ValueError("duration overlay requires a positive elapsed base spell duration")
    return int(duration.value) * units[duration.units]


def world_spell_duration(spell: Spell, loader: AssetLoader) -> ActiveEffectDuration | None:
    """Resolve the covered duration at cast; None preserves the base application."""
    policy = spell_duration_policy(loader, spell.slug)
    if policy == "canonical":
        return None
    if spell.concentration:
        raise ValueError("duration overlay does not alter concentration")
    if policy == "until_long_rest":
        return ActiveEffectDuration(until_long_rest=True)
    assert isinstance(policy, FixedSpellDuration)
    return ActiveEffectDuration(seconds=policy.rounds * ROUND_DURATION_SECONDS, overlay_fixed=True)


def verify_bound_world_spell_duration(
    effect: EffectState, spell: Spell | None, loader: AssetLoader
) -> None:
    """Match the exact effective clock before interpreting a reviewed duration application."""
    if spell is None or spell.slug not in DURATION_OVERLAY_SPELLS:
        raise ValueError("application lacks a supported bound spell duration")
    duration = world_spell_duration(spell, loader)
    if duration is None:
        duration = ActiveEffectDuration(seconds=reviewed_duration_seconds(spell))
    if duration.model_dump() != effect.duration.model_dump():
        raise ValueError("effect clock differs from its bound duration policy")


def verify_duration_overlay_application(effect: EffectState, loader: AssetLoader) -> None:
    """Admit exact modified world passives where a consumer has the bound ruleset."""
    if not (effect.duration.until_long_rest or effect.duration.overlay_fixed):
        raise ValueError("effect has no configured duration overlay clock")
    _verify_overlay_mechanics(effect)
    verify_bound_world_spell_duration(
        effect, loader.get_spell(effect.id.removeprefix("effect:")), loader
    )


def _verify_overlay_mechanics(effect: EffectState) -> None:
    """Check the known passive mechanics independently of its captured clock."""
    from dnd5e_engine.evaluation_aid import aid_potency
    from dnd5e_engine.evaluation_timed_spells import is_reviewed_longstrider_application

    if effect.id == "effect:aid":
        aid_potency(effect)
    elif not is_reviewed_longstrider_application(effect):
        raise ValueError("duration overlay requires a reviewed spell effect")


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
    """Bind an effect's elapsed or Long Rest clock to its original invocation.

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
    def duration_seconds(self) -> int | None:
        """Return positive elapsed seconds, or None for the recipient's Long Rest."""
        if self.effect.duration.until_long_rest:
            return None
        seconds = self.effect.duration.seconds
        if seconds is None or seconds <= 0:
            raise ValueError("world effect has no finite positive seconds duration")
        return seconds

    @model_validator(mode="after")
    def finite_duration(self) -> WorldEffectApplication:
        """Require a positive elapsed deadline or a reviewed Long Rest clock."""
        duration = self.effect.duration
        _ = self.duration_seconds
        if duration.until_long_rest or duration.overlay_fixed:
            if self.concentration:
                raise ValueError("duration overlay cannot modify concentration")
            _verify_overlay_mechanics(self.effect)
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
    effect = passive_effect_to_active_effect(
        spell.passive_effects[0], target_id=target_id, caster_id=source_id
    )
    duration = world_spell_duration(spell, loader)
    if duration is not None:
        effect = effect.model_copy(update={"duration": duration})
    return effect_state(effect)


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
