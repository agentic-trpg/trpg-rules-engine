"""Typed creature sight relationships supplied by the authority for fixed rule checks."""

from typing import Annotated

from pydantic import Field

from dnd5e_engine.evaluation_base import EvaluationModel


class CreatureSightNeed(EvaluationModel):
    """Identify the observer and source creature of a required sight relationship."""

    observer_id: Annotated[str, Field(min_length=1)]
    source_id: Annotated[str, Field(min_length=1)]


class CreatureSightFact(EvaluationModel):
    """Record a known sight result for one exact pair during snapshot preparation."""

    need: CreatureSightNeed
    visible: bool
