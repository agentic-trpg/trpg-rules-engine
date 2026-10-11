"""Explicit mechanical casting facts. Local candidate, not source authorization."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from dnd5e_engine.evaluation_base import EvaluationModel
from dnd5e_engine.events import Ability


class ClassSpellSource(EvaluationModel):
    source_id: Annotated[str, Field(min_length=1)]
    class_slug: Annotated[str, Field(min_length=1)]
    ability: Ability
    known_spells: tuple[str, ...]
    prepared_spells: tuple[str, ...]

    @model_validator(mode="after")
    def repertoire(self) -> Self:
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
    actor_id: Annotated[str, Field(min_length=1)]
    verbal_available: bool
    sources: tuple[ClassSpellSource, ...]
    materials: tuple[HoldPersonMaterial, ...]

    @model_validator(mode="after")
    def identities(self) -> Self:
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
    actors: tuple[ActorCastingState, ...]

    @model_validator(mode="after")
    def identities(self) -> Self:
        if len({a.actor_id for a in self.actors}) != len(self.actors):
            raise ValueError("duplicate casting actor")
        return self
