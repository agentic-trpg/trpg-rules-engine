"""Closed target facts and uncommitted spatial effect lifecycle proposals."""

from typing import Annotated, Literal

from dnd5e_srd_data.schema.environment import EnvironmentalSpec
from pydantic import Field

from dnd5e_engine.evaluation_base import EvaluationModel


class EnvironmentalItemTarget(EvaluationModel):
    """Select an item by its stable World instance identity."""

    kind: Literal["item"]
    item_id: Annotated[str, Field(min_length=1)]


class EnvironmentalPointTarget(EvaluationModel):
    """Select a fixed cell in the spell request's scene."""

    kind: Literal["point"]
    cell_id: Annotated[str, Field(min_length=1)]


class EnvironmentalMagicEffectTarget(EvaluationModel):
    """Select one known active spell by its spatial effect identity."""

    kind: Literal["effect"]
    effect_id: Annotated[str, Field(min_length=1)]


class EnvironmentalCreatureTarget(EvaluationModel):
    """Select a visible creature and all eligible ongoing spells on it."""

    kind: Literal["creature"]
    target_id: Annotated[str, Field(min_length=1)]


EnvironmentalPlacementTarget = Annotated[
    EnvironmentalItemTarget | EnvironmentalPointTarget, Field(discriminator="kind")
]
EnvironmentalTarget = Annotated[
    EnvironmentalItemTarget
    | EnvironmentalPointTarget
    | EnvironmentalMagicEffectTarget
    | EnvironmentalCreatureTarget,
    Field(discriminator="kind"),
]


class EnvironmentalItemFacts(EvaluationModel):
    """Supply the host's fixed geometry and qualification facts for an item target."""

    item_id: Annotated[str, Field(min_length=1)]
    size: Literal["tiny", "small", "medium", "large", "huge", "gargantuan"] | None
    worn_or_carried_by: Annotated[str, Field(min_length=1)] | None
    opaque_cover: bool


class EnvironmentalMagicEffectFacts(EvaluationModel):
    """Capture a spatial spell's authoritative provenance, anchor and current cell."""

    effect_id: Annotated[str, Field(min_length=1)]
    caster_id: Annotated[str, Field(min_length=1)]
    spell_source_id: Annotated[str, Field(min_length=1)]
    spell_id: Annotated[str, Field(min_length=1)]
    slot_level: Annotated[int, Field(ge=0, le=9)]
    cell_id: Annotated[str, Field(min_length=1)]
    expires_tick: Annotated[int, Field(ge=0)]
    concentration_id: Annotated[str, Field(min_length=1)] | None
    anchor_item_id: Annotated[str, Field(min_length=1)] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class VisualTargetFact(EvaluationModel):
    """Fix one occupant and its cell at a placed area spell's cast instant."""

    target_kind: Literal["creature", "object"]
    target_id: Annotated[str, Field(min_length=1)]
    cell_id: Annotated[str, Field(min_length=1)]


class VisualTargetEffect(EvaluationModel):
    """One source-linked selected target, independent of its later position."""

    target_kind: Literal["creature", "object"]
    target_id: Annotated[str, Field(min_length=1)]
    dim_radius_ft: Annotated[int, Field(ge=0)]
    suppresses_invisible: bool
    visible_attack_advantage: bool


class WorldVisualEffectCreate(EvaluationModel):
    """Propose a bounded visual effect group without committing World facts."""

    kind: Literal["world.visual_effect_create"]
    source_id: Annotated[str, Field(min_length=1)]
    spell_source_id: Annotated[str, Field(min_length=1)]
    spell_id: Annotated[str, Field(min_length=1)]
    slot_level: Annotated[int, Field(ge=0, le=9)]
    duration_seconds: Annotated[int, Field(gt=0)]
    concentration: bool
    targets: tuple[VisualTargetEffect, ...]


class ExplorationEnvironmentalSpellCastPayload(EvaluationModel):
    """Choose an exploration spell and a spatial placement or existing spell target."""

    kind: Literal["world.cast_spell"]
    spell_id: Annotated[str, Field(min_length=1)]
    spell_source_id: Annotated[str, Field(min_length=1)]
    slot_level: Annotated[int, Field(ge=0, le=9)]
    target: EnvironmentalTarget


class WorldEnvironmentalEffect(EvaluationModel):
    """Describe the fixed spatial mechanics, relative expiry and host-owned anchor."""

    source_id: Annotated[str, Field(min_length=1)]
    spell_source_id: Annotated[str, Field(min_length=1)]
    spell_id: Annotated[str, Field(min_length=1)]
    slot_level: Annotated[int, Field(ge=0, le=9)]
    duration_seconds: Annotated[int, Field(gt=0)]
    concentration: bool
    replace_same_caster_on_recast: bool
    anchor: EnvironmentalPlacementTarget
    environment: EnvironmentalSpec
    radius_ft: Annotated[int, Field(gt=0)]
    shape: Literal["sphere", "emanation"]


class WorldEnvironmentalEffectCreate(EvaluationModel):
    """Propose one spatial effect application for the host to adopt atomically."""

    kind: Literal["world.environmental_effect_create"]
    value: WorldEnvironmentalEffect


class WorldEnvironmentalEffectEnd(EvaluationModel):
    """End one expected spatial spell, retaining the original provenance for adoption."""

    kind: Literal["world.environmental_effect_end"]
    expected: EnvironmentalMagicEffectFacts
    reason: Literal["dispelled"]
