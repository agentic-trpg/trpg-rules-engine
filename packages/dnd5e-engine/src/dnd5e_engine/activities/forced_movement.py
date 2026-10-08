"""Typed movement requests emitted by pure activity resolvers.

Distances and triggers come from the canonical activity. This module neither
selects rules by source identity nor reads live spatial state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from dnd5e_srd_data.schema.common import Activity

    from .context import ActivityResolutionContext


@dataclass(frozen=True)
class ForcedMovementRequest:
    source_actor_id: str
    target_id: str
    distance_ft: int
    direction: Literal["away_from_source", "toward_source"]
    source_activity_id: str


def request_forced_movement(
    activity: Activity,
    ctx: ActivityResolutionContext,
    target_id: str,
    *,
    trigger: Literal["failed_save", "successful_save", "hit"],
) -> None:
    """Record the canonical movement only after its triggering outcome."""
    spec = activity.forced_movement
    if spec is None or spec.trigger != trigger:
        return
    ctx.forced_movement_requests.append(
        ForcedMovementRequest(
            source_actor_id=ctx.caster.entity_id,
            target_id=target_id,
            distance_ft=spec.distance_ft,
            direction=spec.direction,
            source_activity_id=activity.id,
        )
    )
