"""Closed, immutable effect lifecycle contracts and combat clock state.

No events, source prose, RNG, or mutable combat state are inspected here.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from dnd5e_srd_data.schema.lifecycle import EffectLifecycleSpec, OneUseModifier
from pydantic import BaseModel, ConfigDict, model_validator

EffectIdentity = tuple[str, str, str]
EffectSourceKind = Literal["spell", "feature", "item", "monster"]


class EffectLifecycleApplication(BaseModel):
    """Apply-time capture, carried by the authoritative EffectApplied event."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    spec: EffectLifecycleSpec
    source_id: str
    source_kind: EffectSourceKind
    source_slug: str
    activity_id: str
    save_ability: Literal["str", "dex", "con", "int", "wis", "cha"] | None = None
    save_dc: int | None = None
    is_magical: bool = False

    @model_validator(mode="after")
    def _repeat_has_captured_save(self) -> EffectLifecycleApplication:
        if self.spec.repeat_save is not None and (
            self.save_ability is None or self.save_dc is None
        ):
            raise ValueError("repeat save requires an original save ability and DC")
        return self


@dataclass(frozen=True)
class OngoingEffectLifecycle:
    """One full-identity registration. Repeat and duration clocks are separate.

    A finite N-round cap expires at target turn-end in application round + N.
    This combat rounding convention never counts the partial application round
    as a full round, and survives a non-concentration source leaving combat.
    """

    identity: EffectIdentity
    application: EffectLifecycleApplication
    applied_turn_serial: int
    applied_round: int
    expires_round: int | None = None
    expiry_actor_id: str | None = None
    last_repeat_turn_serial: int | None = None
    remaining_one_use_modifiers: tuple[OneUseModifier, ...] = ()

    @classmethod
    def from_application(
        cls,
        identity: EffectIdentity,
        application: EffectLifecycleApplication,
        turn_serial: int,
        round_number: int,
    ) -> OngoingEffectLifecycle:
        spec = application.spec
        boundary = spec.expiry_boundary
        return cls(
            identity=identity,
            application=application,
            applied_turn_serial=turn_serial,
            applied_round=round_number,
            expires_round=round_number + spec.maximum_rounds if spec.maximum_rounds else None,
            remaining_one_use_modifiers=spec.one_use_modifiers,
            expiry_actor_id=(
                application.source_id if boundary.startswith("source_") else identity[0]
            )
            if boundary
            else None,
        )

    def repeats_at(self, actor_id: str, turn_serial: int) -> bool:
        return (
            self.identity[0] == actor_id
            and self.application.spec.repeat_save is not None
            and self.last_repeat_turn_serial != turn_serial
        )

    def repeated(self, turn_serial: int) -> OngoingEffectLifecycle:
        return replace(self, last_repeat_turn_serial=turn_serial)

    def expires_at(
        self, actor_id: str, phase: Literal["start", "end"], turn_serial: int, round_number: int
    ) -> bool:
        boundary = self.application.spec.expiry_boundary
        if (
            boundary is not None
            and self.expiry_actor_id == actor_id
            and boundary.endswith(f"turn_{phase}")
            and turn_serial > self.applied_turn_serial
        ):
            return True
        return (
            phase == "end"
            and actor_id == self.identity[0]
            and self.expires_round is not None
            and round_number >= self.expires_round
        )
