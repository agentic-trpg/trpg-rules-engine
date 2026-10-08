"""Derived spatial projection of authoritative persistent environmental areas.

No static scene writes. Source order never determines illumination: light uses
the brightest surviving source; fog stays opaque independently of lighting.
"""

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from dnd5e_srd_data.schema.environment import EnvironmentalSpec
from pydantic import BaseModel, ConfigDict, Field

from dnd5e_engine.areas import area_cells

if TYPE_CHECKING:
    from dnd5e_engine.orchestrator import CombatHandle, _LiveCombat


@dataclass(frozen=True)
class EnvironmentalSource:
    area_id: str
    source_id: str
    source_entity_id: str
    spell_level: int
    spec: EnvironmentalSpec
    cells: frozenset[str]
    origin: str
    radius_ft: int
    duration_rounds: int | None
    concentration_identity: tuple[str, str, str] | None
    dim_cells: frozenset[str] = frozenset()
    origin_object_id: str | None = None
    includes_origin_object: bool | None = None
    suppressed: bool = False

    @property
    def illuminated_cells(self) -> frozenset[str]:
        return self.cells | self.dim_cells if self.spec.kind == "light" else frozenset()


def refresh_environment(live: "_LiveCombat") -> None:
    from dnd5e_engine.persistent_areas import FollowObjectEmanation

    sources = []
    for area in live.persistent_areas.areas:
        spec = area.spec.environment
        if spec is None:
            continue
        geometry = area.geometry
        obj = (
            live.combat_objects.objects.get(geometry.object_id)
            if isinstance(geometry, FollowObjectEmanation)
            else None
        )
        suppressed = bool(obj and obj.opaque_cover)
        origin = area.origin(live.actor_zone, live.combat_objects.objects)
        if origin is None:
            continue
        cells = (
            area.cells(live.topology, live.actor_zone, live.combat_objects.objects)
            if not suppressed
            else frozenset()
        )
        dim: frozenset[str] = frozenset()
        if spec.dim_extension_ft and not suppressed:
            dim = (
                area_cells(
                    live.topology,
                    replace(area.template, size_ft=area.template.size_ft + spec.dim_extension_ft),
                    origin,
                    None,
                )
                - cells
            )
        sources.append(
            EnvironmentalSource(
                area.id,
                area.source_id,
                area.source_entity_id,
                area.slot_level or area.base_spell_level or 0,
                spec,
                cells,
                origin,
                area.template.size_ft,
                area.duration.rounds,
                area.concentration_identity,
                dim,
                geometry.object_id if isinstance(geometry, FollowObjectEmanation) else None,
                geometry.includes_origin_object
                if isinstance(geometry, FollowObjectEmanation)
                else None,
                suppressed,
            )
        )
    live.topology.environment_sources = tuple(sources)


def reconcile_environment(live: "_LiveCombat") -> None:
    """Apply reviewed cross-spell dispels on actual clipped footprints."""
    from dnd5e_engine import orchestrator as orch

    refresh_environment(live)
    sources = live.topology.environment_sources
    victims: dict[str, str] = {}
    for source in sources:
        for other in sources:
            if source.area_id == other.area_id:
                continue
            light_level = source.spec.dispels_light_through_level
            dark_level = source.spec.dispels_darkness_through_level
            if (
                light_level is not None
                and other.spec.kind == "light"
                and other.spell_level <= light_level
                and source.cells & other.illuminated_cells
            ) or (
                dark_level is not None
                and other.spec.kind == "magical_darkness"
                and other.spell_level <= dark_level
                and source.cells & other.cells
            ):
                victims.setdefault(other.area_id, source.area_id)
    for area in tuple(live.persistent_areas.areas):
        if area.id in victims:
            live.persistent_areas.expire(live, area, "dispelled", cause_id=victims[area.id])
            if area.concentration_identity:
                orch._drop_concentration(live, area.source_entity_id)


class StrongWind(BaseModel):
    """Host attestation of strong wind in canonical cells, not a wind spell.

    Contact with any fog cell disperses that source. The host owns weather and
    wind strength/coverage. This does not execute Gust of Wind or persistent weather.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: str = Field(min_length=1)
    cells: tuple[str, ...] = Field(min_length=1)


async def apply_strong_wind(handle: "CombatHandle", wind: StrongWind) -> None:
    """Atomically disperse susceptible sources; no actor action or RNG cost."""
    from dnd5e_engine import orchestrator as orch
    from dnd5e_engine.spatial import canonical_cell_id

    live = orch._get_live(handle)
    if live.ended:
        raise orch.IntentRejectedError("combat_ended", "combat has ended")
    try:
        valid = len(wind.cells) == len(set(wind.cells)) and all(
            canonical_cell_id(cell) == cell and live.topology.is_valid_cell(cell)
            for cell in wind.cells
        )
    except ValueError:
        valid = False
    if not valid:
        raise orch.IntentRejectedError(
            "target_invalid", "wind requires distinct legal canonical cells"
        )
    with orch._execution_transaction(live):
        for area in tuple(live.persistent_areas.areas):
            spec = area.spec.environment
            if (
                spec
                and spec.strong_wind_dispersal
                and area.cells(live.topology, live.actor_zone) & set(wind.cells)
            ):
                live.persistent_areas.expire(live, area, "strong_wind", cause_id=wind.source_id)
                if area.concentration_identity:
                    orch._drop_concentration(live, area.source_entity_id)


def expire_environment_round(live: "_LiveCombat", actor_id: str | None) -> None:
    """Nonconcentration sources age without requiring a living caster turn."""
    for area in tuple(live.persistent_areas.areas):
        if (
            area.environment_expires_round is not None
            and live.round_number >= area.environment_expires_round
        ):
            live.persistent_areas.expire(live, area, "duration")


__all__ = ["EnvironmentalSource", "StrongWind", "apply_strong_wind"]
