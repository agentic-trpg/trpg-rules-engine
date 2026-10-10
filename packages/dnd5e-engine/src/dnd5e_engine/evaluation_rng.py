"""Explicit stdlib MT19937 state, without pickle or global RNG access.

Encoding v1 is an implementation choice. Stream version advancement belongs to
the State Machine, including accepted operations that make no random draws.
"""

import random
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from dnd5e_engine.evaluation_base import EvaluationModel

Word = Annotated[int, Field(ge=0, le=0xFFFFFFFF)]
Version = Annotated[int, Field(ge=0)]


class RNGState(EvaluationModel):
    encoding: Literal["stdlib-mt19937/1"]
    words: Annotated[tuple[Word, ...], Field(min_length=624, max_length=624)]
    index: Annotated[int, Field(ge=0, le=624)]
    gaussian: Annotated[float, Field(allow_inf_nan=False)] | None

    def restore(self) -> random.Random:
        rng = random.Random(0)
        rng.setstate((3, (*self.words, self.index), self.gaussian))
        return rng

    @classmethod
    def capture(cls, rng: random.Random) -> Self:
        version, internal, gaussian = rng.getstate()
        if version != 3:
            raise ValueError("unsupported stdlib RNG state version")
        return cls(
            encoding="stdlib-mt19937/1", words=internal[:-1], index=internal[-1], gaussian=gaussian
        )


class RNGContext(EvaluationModel):
    stream_id: Annotated[str, Field(min_length=1)]
    version: Version
    state: RNGState


class RNGTransition(EvaluationModel):
    stream_id: Annotated[str, Field(min_length=1)]
    input_version: Version
    input_state: RNGState
    next_state: RNGState
    state_changed: bool

    @model_validator(mode="after")
    def validate_change(self) -> Self:
        if self.state_changed != (self.input_state != self.next_state):
            raise ValueError("RNG state_changed contradicts explicit states")
        return self

    def verify_input(self, context: RNGContext) -> None:
        if (self.stream_id, self.input_version, self.input_state) != (
            context.stream_id,
            context.version,
            context.state,
        ):
            raise ValueError("RNG transition does not match the input context")

    @classmethod
    def between(cls, context: RNGContext, rng: random.Random) -> Self:
        successor = RNGState.capture(rng)
        return cls(
            stream_id=context.stream_id,
            input_version=context.version,
            input_state=context.state,
            next_state=successor,
            state_changed=context.state != successor,
        )
