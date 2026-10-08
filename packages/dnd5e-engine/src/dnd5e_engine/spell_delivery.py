"""Immutable delivery declarations and draw-free activity target planning."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from dnd5e_engine.areas import (
    AreaTemplate,
    area_activity,
    area_cells,
    area_template,
    creature_count,
    has_line_of_effect,
    is_choice,
    is_harmful,
    select_affected,
)
from dnd5e_engine.spatial import GridTopology, SpatialTopology, cell_id, parse_cell

if TYPE_CHECKING:
    from dnd5e_srd_data.schema.common import Activity

    from dnd5e_engine.types.combat import Combatant


class SpellDeliverySpec(BaseModel):
    """Decisions only: canonical activities retain geometry and rule values."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    primary_target_id: str | None = None
    selected_target_ids: tuple[str, ...] | None = None
    origin_cell: str | None = None
    direction: tuple[int, int] | None = None
    excluded_target_ids: tuple[str, ...] | None = None
    source_kind: Literal["direct_spell", "item_cast", "monster_cast", "reaction_cast"] = (
        "direct_spell"
    )
    source_item_id: str | None = None
    source_activity_id: str | None = None


class DeliveryPlanningError(ValueError):
    def __init__(self, message: str, reason: str = "target_invalid") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class ActivityDeliveryPlan:
    activity_id: str
    target_ids: tuple[str, ...]
    template: AreaTemplate | None = None
    origin: str | None = None
    direction: tuple[int, int] | None = None
    cells: frozenset[str] = frozenset()
    spared_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeliveryPlan:
    activities: tuple[ActivityDeliveryPlan, ...]

    @property
    def target_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(i for plan in self.activities for i in plan.target_ids))


def _origin(
    topology: SpatialTopology,
    positions: Mapping[str, str],
    actor_id: str,
    spec: SpellDeliverySpec,
    template: AreaTemplate,
    range_ft: int | None,
) -> str:
    actor_cell = positions.get(actor_id)
    if actor_cell is None:
        raise DeliveryPlanningError("source has no position")
    if template.anchor == "actor":
        return actor_cell
    origin = spec.origin_cell or positions.get(spec.primary_target_id or "") or actor_cell
    try:
        valid = (
            cell_id(*parse_cell(origin)) == origin
            and topology.distance_ft(origin, origin) is not None
        )
    except ValueError:
        valid = False
    if not valid:
        raise DeliveryPlanningError("area origin is not a legal canonical cell")
    if (
        range_ft is not None and not topology.within_range(actor_cell, origin, range_ft)
    ) or not has_line_of_effect(topology, actor_cell, origin):
        raise DeliveryPlanningError(
            "area origin is outside range or line of effect", "out_of_range"
        )
    return origin


def _direction(
    positions: Mapping[str, str],
    actor_id: str,
    spec: SpellDeliverySpec,
    template: AreaTemplate,
) -> tuple[int, int] | None:
    if not template.directional:
        return None
    direction = spec.direction
    if direction is None and spec.primary_target_id:
        start, end = positions.get(actor_id), positions.get(spec.primary_target_id)
        if start and end:
            a, b = parse_cell(start), parse_cell(end)
            direction = ((b[0] > a[0]) - (b[0] < a[0]), (b[1] > a[1]) - (b[1] < a[1]))
    # A placed Cube has a deterministic grid orientation when only its point
    # was declared. Actor-origin cones/lines/cubes still require an aim.
    if direction is None and template.anchor == "target" and template.grid_shape == "cube":
        direction = (1, 0)
    if direction is None or direction == (0, 0) or any(i not in (-1, 0, 1) for i in direction):
        raise DeliveryPlanningError("directional area requires an aim")
    return direction


def _filtered(activity: Activity, creatures: Sequence[Combatant]) -> list[Combatant]:
    rule = activity.target.creature_filter
    if rule is None:
        if area_activity([activity]) is not None and activity.target.affects.special.strip():
            raise DeliveryPlanningError(
                "area target filter has no typed carrier", "unsupported_area"
            )
        return list(creatures)
    if rule.deferred_reason:
        raise DeliveryPlanningError(
            "creature targeting rule has no reviewed carrier", "unsupported_area"
        )
    return [
        c
        for c in creatures
        if (
            (not rule.include_creature_types or c.creature_type in rule.include_creature_types)
            and c.creature_type not in rule.exclude_creature_types
        )
    ]


def _area_selection(
    activity: Activity,
    spec: SpellDeliverySpec,
    in_area: Sequence[Combatant],
    *,
    enemy_ids: frozenset[str],
    ally_ids: frozenset[str],
    execution: bool,
    live_ids: set[str],
    actor_id: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    candidates = _filtered(activity, in_area)
    eligible = {c.entity_id for c in candidates}
    all_ids = tuple(c.entity_id for c in in_area)
    exclusions = spec.excluded_target_ids
    count = creature_count(activity)
    template = area_template(activity)
    allowed_exclusions = live_ids if activity.persistent_area else set(all_ids)
    if template and template.anchor == "actor" and not template.includes_origin:
        allowed_exclusions = allowed_exclusions | {actor_id}
    if exclusions is not None and (
        not is_choice(activity) or (not execution and not set(exclusions) <= allowed_exclusions)
    ):
        raise DeliveryPlanningError("exclusions must name creatures in a choice area")
    named = spec.selected_target_ids
    if named is None and count == 1 and spec.primary_target_id:
        named = (spec.primary_target_id,)
    if count is not None and named is not None:
        if len(named) > count or len(set(named)) != len(named):
            raise DeliveryPlanningError("selected creatures exceed count or repeat")
        if not execution and not set(named) <= eligible:
            raise DeliveryPlanningError("selected creature is outside the legal area")
        if activity.target.affects.type == "enemy":
            eligible &= enemy_ids
        elif activity.target.affects.type == "ally":
            eligible &= ally_ids
        if not execution and not set(named) <= eligible:
            raise DeliveryPlanningError("selected creature fails allegiance restrictions")
        chosen = set(named) - set(exclusions or ())
        affected = tuple(
            c.entity_id for c in candidates if c.entity_id in chosen and c.entity_id in eligible
        )
    else:
        affected = select_affected(
            [c.entity_id for c in candidates],
            affects_type=activity.target.affects.type,
            choice=is_choice(activity),
            count=count,
            harmful=is_harmful([activity]),
            excluded_ids=exclusions,
            is_enemy=enemy_ids.__contains__,
            is_ally=ally_ids.__contains__,
        ).affected_ids
    return affected, tuple(i for i in all_ids if i not in affected)


def plan_delivery(
    activities: Sequence[Activity],
    spec: SpellDeliverySpec,
    *,
    actor_id: str,
    topology: SpatialTopology,
    positions: Mapping[str, str],
    creatures: Sequence[Combatant],
    enemy_ids: frozenset[str],
    ally_ids: frozenset[str],
    range_ft: int | None = None,
    default_target_ids: tuple[str, ...] = (),
    execution: bool = False,
    expand_areas: bool = True,
) -> DeliveryPlan:
    """Plan each activity independently in stable roster order, without I/O."""
    plans = []
    live_ids = {c.entity_id for c in creatures}
    for activity in activities:
        semantics = activity.target.area_semantics
        if (
            activity.target.template.type
            and (semantics is None or semantics.template_role != "effect_geometry")
            and area_template(activity) is None
        ):
            raise DeliveryPlanningError("activity geometry is unsupported", "unsupported_area")
        is_area = expand_areas and area_activity([activity]) is not None
        if is_area:
            if (
                not isinstance(topology, GridTopology)
                or (template := area_template(activity)) is None
            ):
                raise DeliveryPlanningError(
                    "activity area geometry is unsupported", "unsupported_area"
                )
            origin = _origin(topology, positions, actor_id, spec, template, range_ft)
            direction = _direction(positions, actor_id, spec, template)
            cells = area_cells(topology, template, origin, direction)
            inside = [c for c in creatures if positions.get(c.entity_id) in cells]
            targets, spared = _area_selection(
                activity,
                spec,
                inside,
                enemy_ids=enemy_ids,
                ally_ids=ally_ids,
                execution=execution,
                live_ids=live_ids,
                actor_id=actor_id,
            )
            plans.append(
                ActivityDeliveryPlan(
                    activity.id, targets, template, origin, direction, cells, spared
                )
            )
            continue
        rule = activity.target.creature_filter
        if rule is not None and rule.deferred_reason:
            raise DeliveryPlanningError("target filter is deferred", "unsupported_area")
        named = (
            spec.selected_target_ids
            if spec.selected_target_ids is not None
            else ((spec.primary_target_id,) if spec.primary_target_id else default_target_ids)
        )
        count = creature_count(activity)
        if (
            count is not None
            and activity.kind != "damage"
            and (len(named) > count or len(set(named)) != len(named))
        ):
            raise DeliveryPlanningError("named target count is invalid")
        if not execution and not set(named) <= live_ids:
            raise DeliveryPlanningError("named target is not a live creature")
        eligible = {c.entity_id for c in _filtered(activity, creatures)}
        actor_cell = positions.get(actor_id)
        if actor_cell is not None and range_ft is not None:
            eligible &= {
                i
                for i in live_ids
                if i in positions
                and topology.within_range(actor_cell, positions[i], range_ft)
                and has_line_of_effect(topology, actor_cell, positions[i])
            }
        if not execution and not set(named) <= eligible:
            raise DeliveryPlanningError("named target fails creature filter")
        plans.append(ActivityDeliveryPlan(activity.id, tuple(i for i in named if i in eligible)))
    return DeliveryPlan(tuple(plans))


__all__ = [
    "ActivityDeliveryPlan",
    "DeliveryPlan",
    "DeliveryPlanningError",
    "SpellDeliverySpec",
    "plan_delivery",
]
