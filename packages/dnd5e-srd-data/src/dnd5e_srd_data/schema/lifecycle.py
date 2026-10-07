"""Closed, reviewed contracts for one exact applied effect's lifecycle."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    PositiveInt,
    SerializerFunctionWrapHandler,
    model_serializer,
)

EffectExpiryBoundary = Literal[
    "source_next_turn_start",
    "source_next_turn_end",
    "target_next_turn_start",
    "target_next_turn_end",
]
OneUseModifier = Literal["next_save_disadvantage", "next_attack_bonus_other_creature"]


class RepeatSaveSpec(BaseModel):
    """Capture the applying activity's save ability and DC at registration.

    Each target turn end is eligible, including the applying turn's end. The
    source's magical provenance is captured by the runtime application, never
    inferred from the effect's name, condition, or concentration state.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    phase: Literal["target_turn_end"] = "target_turn_end"
    save_source: Literal["triggering_activity"] = "triggering_activity"
    on_success: Literal["expire_effect"] = "expire_effect"


class EffectLifecycleSpec(BaseModel):
    """Independent, typed causes that may expire or consume one effect.

    ``maximum_rounds`` expires at the target's turn end once the combat round
    reaches ``applied_round + maximum_rounds``. The application round does not
    count as a complete round; a repeat save resolves before duration expiry.
    Exact next boundaries are separate from finite round counts. The one-use modifier
    ``next_save_disadvantage`` binds ``flags.save.next_disadvantage`` and
    consumes only that change on the next actual saving throw.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    repeat_save: RepeatSaveSpec | None = None
    maximum_rounds: PositiveInt | None = None
    expiry_boundary: EffectExpiryBoundary | None = None
    expire_on_positive_damage: bool = False
    one_use_modifiers: tuple[OneUseModifier, ...] = ()
    stacking: Literal["latest_only"] | None = None
    stacking_group: str | None = None
    next_attack_scope: Literal["other_creature"] | None = None
    next_attack_bonus_group: str | None = None

    @model_serializer(mode="wrap")
    def _serialize_lifecycle(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        for key in ("stacking", "stacking_group", "next_attack_scope", "next_attack_bonus_group"):
            if getattr(self, key) is None:
                data.pop(key, None)
        return data
