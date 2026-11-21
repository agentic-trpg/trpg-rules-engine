"""Closed spatial target facts and uncommitted environmental spell effects."""

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


EnvironmentalTarget = Annotated[
    EnvironmentalItemTarget | EnvironmentalPointTarget, Field(discriminator="kind")
]


class EnvironmentalItemFacts(EvaluationModel):
    """Supply the host's fixed geometry and qualification facts for an item target."""

    item_id: Annotated[str, Field(min_length=1)]
    size: Literal["tiny", "small", "medium", "large", "huge", "gargantuan"] | None
    worn_or_carried_by: Annotated[str, Field(min_length=1)] | None
    opaque_cover: bool


class ExplorationEnvironmentalSpellCastPayload(EvaluationModel):
    """Choose an exploration spell, ordinary slot or cantrip, and spatial target."""

    kind: Literal["world.cast_spell"]
    spell_id: Annotated[str, Field(min_length=1)]
    spell_source_id: Annotated[str, Field(min_length=1)]
    slot_level: Annotated[int, Field(ge=0, le=9)]
    target: EnvironmentalTarget


class WorldEnvironmentalEffect(EvaluationModel):
    """Describe illumination, its relative expiry and the host-owned spatial anchor."""

    source_id: Annotated[str, Field(min_length=1)]
    spell_source_id: Annotated[str, Field(min_length=1)]
    spell_id: Annotated[str, Field(min_length=1)]
    slot_level: Annotated[int, Field(ge=0, le=9)]
    duration_seconds: Annotated[int, Field(gt=0)]
    concentration: bool
    replace_same_caster_on_recast: bool
    anchor: EnvironmentalTarget
    environment: EnvironmentalSpec
    radius_ft: Annotated[int, Field(gt=0)]
    shape: Literal["sphere", "emanation"]


class WorldEnvironmentalEffectCreate(EvaluationModel):
    """Propose one spatial effect application for the host to adopt atomically."""

    kind: Literal["world.environmental_effect_create"]
    value: WorldEnvironmentalEffect
