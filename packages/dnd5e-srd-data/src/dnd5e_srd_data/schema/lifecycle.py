"""Closed, reviewed contracts for one exact applied effect's lifecycle."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
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


class EffectEndFollowUp(BaseModel):
    """One reviewed effect template applied to the same target on actual expiry.

    The applying resolver captures the template; expiry never consults source
    prose or a mutable library. Follow-ups cannot recursively produce effects.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    effect_id: str = Field(min_length=1)
    expiry_boundary: EffectExpiryBoundary


class EffectLifecycleSpec(BaseModel):
    """Independent, typed causes that may expire or consume one effect.

    ``maximum_rounds`` expires at the target's turn end once the combat round
    reaches ``applied_round + maximum_rounds``. The application round does not
    count as a complete round; a repeat save resolves before duration expiry.
    Exact next boundaries are separate from finite round counts. The one-use modifier
    ``next_save_disadvantage`` binds ``flags.save.next_disadvantage`` and
    consumes only that change on the next actual saving throw.
    ``latest_applies`` is for reviewed equal-potency spell groups: older
    applications keep their ownership/clocks but are suppressed in projection.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    repeat_save: RepeatSaveSpec | None = None
    maximum_rounds: PositiveInt | None = None
    expiry_boundary: EffectExpiryBoundary | None = None
    expire_on_positive_damage: bool = False
    one_use_modifiers: tuple[OneUseModifier, ...] = ()
    stacking: Literal["latest_only", "latest_applies"] | None = None
    stacking_group: str | None = None
    next_attack_scope: Literal["other_creature"] | None = None
    next_attack_bonus_group: str | None = None
    on_end: tuple[EffectEndFollowUp, ...] = ()

    @model_validator(mode="after")
    def _projection_group_required(self) -> EffectLifecycleSpec:
        if self.stacking == "latest_applies" and not self.stacking_group:
            raise ValueError("latest_applies requires an explicit stacking group")
        if len({entry.effect_id for entry in self.on_end}) != len(self.on_end):
            raise ValueError("effect-end templates must be unique")
        return self

    @model_serializer(mode="wrap")
    def _serialize_lifecycle(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if not self.on_end:
            data.pop("on_end", None)
        for key in ("stacking", "stacking_group", "next_attack_scope", "next_attack_bonus_group"):
            if getattr(self, key) is None:
                data.pop(key, None)
        return data
