"""Pure physical-movement costs, occupancy qualifiers and turn accounting.

Creatures occupy one cell regardless of size. These helpers decide whether
that cell may be traversed and what a legal step costs; live movement owns
destination occupancy, reactions and authoritative position changes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Literal

from dnd5e_srd_data.schema.monster import CreatureSize
from pydantic import BaseModel, ConfigDict, Field, model_validator

from dnd5e_engine.activities.passive_stats import CombatantMovementModes
from dnd5e_engine.size import size_rank, two_or_more_sizes_apart

MovementMode = Literal["walk", "crawl", "climb", "swim"]


class MovementLedger(BaseModel):
    """One turn's shared movement expenditure across every movement mode."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    spent_ft: int = Field(default=0, ge=0)
    distance_ft: int = Field(default=0, ge=0)
    active_mode: MovementMode = "walk"
    dash_count: int = Field(default=0, ge=0)

    def remaining(self, effective_speed: int) -> int:
        return max(0, effective_speed * (1 + self.dash_count) - self.spent_ft)

    def spend(
        self,
        *,
        cost_ft: int,
        distance_ft: int,
        mode: MovementMode,
        effective_speed: int,
    ) -> MovementLedger:
        """Return a new ledger, rejecting unaffordable or negative movement."""
        if cost_ft < 0 or distance_ft < 0:
            raise ValueError("movement cost and distance must be nonnegative")
        if cost_ft > self.remaining(effective_speed):
            raise ValueError("movement cost exceeds the remaining allowance")
        return self.model_copy(
            update={
                "spent_ft": self.spent_ft + cost_ft,
                "distance_ft": self.distance_ft + distance_ft,
                "active_mode": mode,
            }
        )

    def add_dash(self) -> MovementLedger:
        return self.model_copy(update={"dash_count": self.dash_count + 1})


class MovementGrant(BaseModel):
    """A scoped movement allowance; feature-specific execution is separate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_distance_ft: int = Field(ge=0)
    source_id: str = Field(min_length=1)
    direction: Literal["any", "toward_target", "away_from_target"] = "any"
    target_id: str | None = None
    provokes_opportunity_attacks: bool = False
    lifespan: Literal["immediate", "current_turn"] = "immediate"

    @model_validator(mode="after")
    def _direction_has_target(self) -> MovementGrant:
        if self.direction != "any" and not self.target_id:
            raise ValueError("directed movement requires a target_id")
        return self


def can_enter_creature_space(
    mover_size: CreatureSize,
    occupant_size: CreatureSize,
    *,
    allied: bool = False,
    incapacitated: bool = False,
) -> bool:
    """Pass-through qualifier only; an occupied destination remains illegal."""
    return (
        allied
        or incapacitated
        or occupant_size == CreatureSize.TINY
        or two_or_more_sizes_apart(mover_size, occupant_size)
    )


def creature_space_is_difficult(occupant_size: CreatureSize, *, allied: bool = False) -> bool:
    return not allied and occupant_size != CreatureSize.TINY


def grapple_drag_extra_cost(mover_size: CreatureSize, target_sizes: Sequence[CreatureSize]) -> bool:
    """Dragging adds one foot unless every victim qualifies for an exemption."""
    return any(
        size != CreatureSize.TINY and size_rank(mover_size) - size_rank(size) < 2
        for size in target_sizes
    )


def step_cost(
    distance_ft: int,
    *,
    mode: MovementMode = "walk",
    difficult_terrain: bool = False,
    creature_space_difficult: bool = False,
    has_special_speed: bool = False,
    drag_extra: bool = False,
) -> int:
    """Charge additive extra feet; duplicate Difficult Terrain never stacks."""
    if distance_ft < 0:
        raise ValueError("step distance must be nonnegative")
    mode_extra = mode == "crawl" or (mode in {"climb", "swim"} and not has_special_speed)
    return distance_ft * (
        1 + int(difficult_terrain or creature_space_difficult) + int(mode_extra) + int(drag_extra)
    )


@dataclass(frozen=True)
class SpeedModifier:
    """One ordered modifier, applied identically to every available speed."""

    operation: Literal["add", "reduce", "multiply"]
    value: int | Fraction

    def __post_init__(self) -> None:
        if self.operation not in {"add", "reduce", "multiply"}:
            raise ValueError("unsupported speed modifier operation")
        if self.operation != "add" and self.value < 0:
            raise ValueError("speed reductions and multipliers must be nonnegative")


class EffectiveSpeeds(BaseModel):
    """Projected speeds; None preserves an unavailable special speed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    walk: int = Field(ge=0)
    climb: int | None = Field(default=None, ge=0)
    swim: int | None = Field(default=None, ge=0)
    fly: int | None = Field(default=None, ge=0)
    burrow: int | None = Field(default=None, ge=0)


def _modified_speed(speed: int, modifiers: Sequence[SpeedModifier]) -> int:
    for modifier in modifiers:
        if modifier.operation == "multiply":
            speed = int(speed * modifier.value)
        elif modifier.operation == "reduce":
            speed -= int(modifier.value)
        else:
            speed += int(modifier.value)
        speed = max(0, speed)
    return speed


def project_speeds(
    base_speed: int,
    movement_modes: CombatantMovementModes,
    *,
    condition_names: Sequence[str] = (),
    exhaustion_level: int = 0,
    modifiers: Sequence[SpeedModifier] = (),
) -> EffectiveSpeeds:
    """Apply conditions, exhaustion and ordered modifiers to all speeds.

    A condition that overrides Speed to zero wins over every modifier. The
    caller provides live Slow, feature and area modifiers in their stable
    resolution order; this function does not inspect their prose or sources.
    """
    from dnd5e_engine.rules.conditions import project_speed

    names = list(condition_names)
    locked = project_speed(1, names) == 0

    def project(base: int | None) -> int | None:
        if base is None:
            return None
        if locked:
            return 0
        speed = project_speed(max(0, base), names, exhaustion_level)
        return _modified_speed(speed, modifiers)

    return EffectiveSpeeds(
        walk=project(base_speed) or 0,
        climb=project(movement_modes.climb),
        swim=project(movement_modes.swim),
        fly=project(movement_modes.fly),
        burrow=project(movement_modes.burrow),
    )


def speed_for_mode(speeds: EffectiveSpeeds, mode: MovementMode) -> int:
    if mode == "climb" and speeds.climb is not None:
        return speeds.climb
    if mode == "swim" and speeds.swim is not None:
        return speeds.swim
    return speeds.walk


def has_special_speed(speeds: EffectiveSpeeds, mode: MovementMode) -> bool:
    return (mode == "climb" and (speeds.climb or 0) > 0) or (
        mode == "swim" and (speeds.swim or 0) > 0
    )
