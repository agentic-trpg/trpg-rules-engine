"""Explicit mechanical casting facts. Local candidate, not source authorization."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from dnd5e_engine.evaluation_base import EvaluationModel
from dnd5e_engine.events import Ability


class ClassSpellSource(EvaluationModel):
    """Describe a class-owned spell repertoire and its casting resource source."""

    source_id: Annotated[str, Field(min_length=1)]
    class_slug: Annotated[str, Field(min_length=1)]
    ability: Ability
    known_spells: tuple[str, ...]
    prepared_spells: tuple[str, ...]

    @model_validator(mode="after")
    def repertoire(self) -> Self:
        """Validate the casting source's explicit spell repertoire."""
        if (
            any(not slug for slug in self.known_spells)
            or len(set(self.known_spells)) != len(self.known_spells)
            or len(set(self.prepared_spells)) != len(self.prepared_spells)
            or not set(self.prepared_spells) <= set(self.known_spells)
        ):
            raise ValueError("incomplete/duplicate casting repertoire")
        return self


class HoldPersonMaterial(EvaluationModel):
    """SM-attested nonconsumed iron in one owned component pouch, not prose stock."""

    spell_slug: Literal["hold-person"]
    source_id: Annotated[str, Field(min_length=1)]
    inventory_instance_id: Annotated[str, Field(min_length=1)]
    available: bool


class ActorCastingState(EvaluationModel):
    """Collect the explicit spell sources supplied for one actor."""

    actor_id: Annotated[str, Field(min_length=1)]
    verbal_available: bool
    sources: tuple[ClassSpellSource, ...]
    materials: tuple[HoldPersonMaterial, ...]

    @model_validator(mode="after")
    def identities(self) -> Self:
        """Validate unique identities and referenced ownership in the supplied component."""
        if len({s.source_id for s in self.sources}) != len(self.sources):
            raise ValueError("duplicate actor spell source")
        if len({s.class_slug for s in self.sources}) != len(self.sources):
            raise ValueError("one class spell source per class")
        if len({(m.source_id, m.spell_slug) for m in self.materials}) != len(self.materials):
            raise ValueError("duplicate material attestation")
        if any(m.source_id not in {s.source_id for s in self.sources} for m in self.materials):
            raise ValueError("material attestation requires a present casting source")
        return self


class CastingState(EvaluationModel):
    """Group actor casting inputs within the fixed mechanical snapshot."""

    actors: tuple[ActorCastingState, ...]

    @model_validator(mode="after")
    def identities(self) -> Self:
        """Validate unique identities and referenced ownership in the supplied component."""
        if len({a.actor_id for a in self.actors}) != len(self.actors):
            raise ValueError("duplicate casting actor")
        return self


class SpellMaterial(EvaluationModel):
    """Attest one owned, available or unavailable component pouch instance."""

    inventory_instance_id: Annotated[str, Field(min_length=1)]
    available: bool


class SpellSelection(EvaluationModel):
    """Bind one known spell to preparation order and its reviewed material fact."""

    spell_id: Annotated[str, Field(min_length=1)]
    prepared_order: Annotated[int, Field(strict=True, ge=0)] | None
    material: SpellMaterial | None


class ClassSpellSourceV20(EvaluationModel):
    """Supply one ordered, source-owned spell repertoire on snapshot /20."""

    source_id: Annotated[str, Field(min_length=1)]
    class_slug: Annotated[str, Field(min_length=1)]
    ability: Ability
    spells: tuple[SpellSelection, ...]

    @model_validator(mode="after")
    def repertoire(self) -> Self:
        """Reject duplicate spell identities, preparation positions and unsupported materials."""
        if len({s.spell_id for s in self.spells}) != len(self.spells):
            raise ValueError("duplicate spell in casting source")
        orders = [s.prepared_order for s in self.spells if s.prepared_order is not None]
        if len(orders) != len(set(orders)):
            raise ValueError("duplicate prepared order in casting source")
        if any(s.material is not None and s.spell_id != "hold-person" for s in self.spells):
            raise ValueError("material attestation is only reviewed for Hold Person")
        return self

    @property
    def known_spells(self) -> tuple[str, ...]:
        """Expose the canonical source's known spells to existing evaluators."""
        return tuple(s.spell_id for s in self.spells)

    @property
    def prepared_spells(self) -> tuple[str, ...]:
        """Expose prepared spells in their explicit source order to evaluators."""
        return tuple(
            s.spell_id
            for s in sorted(
                (s for s in self.spells if s.prepared_order is not None),
                key=lambda spell: spell.prepared_order or 0,
            )
        )


class ActorCastingStateV20(EvaluationModel):
    """Collect canonical source-owned spells for one actor without parallel material facts."""

    actor_id: Annotated[str, Field(min_length=1)]
    verbal_available: bool
    sources: tuple[ClassSpellSourceV20, ...]

    @model_validator(mode="after")
    def identities(self) -> Self:
        """Keep source identity and class ownership unique for this actor."""
        if len({s.source_id for s in self.sources}) != len(self.sources):
            raise ValueError("duplicate actor spell source")
        if len({s.class_slug for s in self.sources}) != len(self.sources):
            raise ValueError("one class spell source per class")
        return self

    @property
    def materials(self) -> tuple[HoldPersonMaterial, ...]:
        """Project the one reviewed inline material to the existing Hold Person check."""
        return tuple(
            HoldPersonMaterial(
                spell_slug="hold-person",
                source_id=source.source_id,
                inventory_instance_id=spell.material.inventory_instance_id,
                available=spell.material.available,
            )
            for source in self.sources
            for spell in source.spells
            if spell.material is not None
        )


class CastingStateV20(EvaluationModel):
    """Group canonical casting actors for the versioned /20 snapshot."""

    actors: tuple[ActorCastingStateV20, ...]

    @model_validator(mode="after")
    def identities(self) -> Self:
        """Reject duplicate casting actors."""
        if len({a.actor_id for a in self.actors}) != len(self.actors):
            raise ValueError("duplicate casting actor")
        return self
