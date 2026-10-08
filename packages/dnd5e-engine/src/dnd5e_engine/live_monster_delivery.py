"""Deterministic monster aims over the shared activity delivery planner."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from dnd5e_srd_data.schema.common import Activity
from dnd5e_srd_data.schema.spell import Spell

from dnd5e_engine.areas import AreaSelection, AreaTemplate, area_activity, area_template
from dnd5e_engine.spatial import parse_cell
from dnd5e_engine.spell_delivery import (
    ActivityDeliveryPlan,
    DeliveryPlanningError,
    SpellDeliverySpec,
    plan_delivery,
)

if TYPE_CHECKING:
    from dnd5e_engine.orchestrator import _LiveCombat
    from dnd5e_engine.types import Combatant


@dataclass(frozen=True)
class MonsterAreaPlacement:
    """An aim whose affected creatures were selected by common delivery rules."""

    template: AreaTemplate
    origin: str
    direction: tuple[int, int] | None
    selection: AreaSelection
    targets: tuple[Combatant, ...]
    spec: SpellDeliverySpec
    activity_plan: ActivityDeliveryPlan


# Retain the established directional tie break after enemy target priorities.
MONSTER_AREA_DIRECTIONS: Final = (
    (0, -1),
    (1, 0),
    (0, 1),
    (-1, 0),
    (1, -1),
    (1, 1),
    (-1, 1),
    (-1, -1),
)


def named_activity_delivery(
    live: _LiveCombat,
    actor: Combatant,
    activity: Activity,
    targets: Sequence[Combatant],
    *,
    check_range: bool = True,
) -> tuple[ActivityDeliveryPlan, SpellDeliverySpec] | None:
    """Validate the AI's chosen targets without changing its targeting policy.

    Ranking omits range so the existing movement gambit can approach a legal
    target. Execution retains that gambit's per-child reach/LoS contract.
    All other named-target eligibility comes from the shared pure planner.
    """
    from dnd5e_engine import orchestrator as orch

    ids = (
        (actor.entity_id,)
        if activity.target.affects.type == "self"
        else tuple(target.entity_id for target in targets)
    )
    spec = SpellDeliverySpec(
        selected_target_ids=ids,
        source_kind="monster_cast",
        source_activity_id=activity.id,
    )
    creatures = [c for c in live.initiative if c.is_alive and c.entity_id not in live.dead_ids]
    if check_range and activity.target.affects.type != "self":
        creatures = [
            c for c in creatures if orch._monster_execution_in_range(live, actor, [c], [activity])
        ]
    try:
        plan = plan_delivery(
            [activity],
            spec,
            actor_id=actor.entity_id,
            topology=live.topology,
            positions=live.actor_zone,
            creatures=creatures,
            enemy_ids=frozenset(
                c.entity_id for c in creatures if orch._is_enemy(live, actor.entity_id, c.entity_id)
            ),
            ally_ids=frozenset(orch._allied_ids(live, actor.entity_id)),
            execution=True,
        ).activities[0]
    except DeliveryPlanningError:
        return None
    return (plan, spec) if plan.target_ids else None


def area_range_ft(activity: Activity, spell: Spell | None = None) -> int | None:
    """Canonical range to an origin, honoring the activity's range override."""
    rng = spell.range if spell is not None and not activity.range.override else activity.range
    if rng.units == "self":
        return 0
    if rng.units == "touch":
        return 5
    if rng.units == "ft":
        try:
            return max(0, int(rng.value)) if rng.value is not None else None
        except (TypeError, ValueError):
            return None
    return None


def area_placement(
    live: _LiveCombat,
    actor: Combatant,
    activities: Sequence[Activity],
    *,
    spell: Spell | None = None,
) -> MonsterAreaPlacement | None:
    """Choose a legal aim without events, resource payment, or random draws.

    Point origins include every in-range cell, occupied or empty. The shared
    delivery planner owns geometry, type filters, counted choices and LoE.
    Preserve the existing safety and enemy-count priorities, then prefer fewer
    allies, the established low-HP enemy priority, proximity and numeric cells.
    """
    from dnd5e_engine import orchestrator as orch
    from dnd5e_engine.live_spell_delivery import plan_activity_delivery

    activity = area_activity(activities)
    if activity is None or (template := area_template(activity)) is None:
        return None
    actor_cell = live.actor_zone.get(actor.entity_id)
    if actor_cell is None:
        return None
    enemies = sorted(orch._select_monster_targets(live, actor), key=lambda c: c.hp_current)
    if not enemies:
        return None
    enemy_ids = {c.entity_id for c in enemies}
    allies = orch._allied_ids(live, actor.entity_id)
    if template.anchor == "target":
        reach = area_range_ft(activity, spell)
        if reach is None:
            return None
        origins = sorted(
            live.topology.cells_in_template(actor_cell, "sphere", reach), key=parse_cell
        )
    else:
        origins = [actor_cell]
    directions = MONSTER_AREA_DIRECTIONS if template.directional else (None,)
    best: MonsterAreaPlacement | None = None
    best_key: tuple[bool, int, int, tuple[int, ...], int, tuple[int, int], int] | None = None
    for origin in origins:
        for direction_index, direction in enumerate(directions):
            spec = SpellDeliverySpec(
                primary_target_id=enemies[0].entity_id,
                origin_cell=origin if template.anchor == "target" else None,
                direction=direction,
                source_kind="monster_cast",
            )
            try:
                plan = plan_activity_delivery(
                    live,
                    actor,
                    activities,
                    spec,
                    range_spec=spell.range if spell is not None else None,
                )
            except DeliveryPlanningError:
                continue
            placed = next((p for p in plan.activities if p.activity_id == activity.id), None)
            if placed is None or placed.template is None or placed.origin is None:
                continue
            affected = set(placed.target_ids)
            priorities = tuple(i for i, enemy in enumerate(enemies) if enemy.entity_id in affected)
            if not priorities:
                continue
            # Charmed still prohibits harming a charmer through area delivery.
            if any(
                orch._is_enemy(live, actor.entity_id, other) and other not in enemy_ids
                for other in affected
            ):
                continue
            ally_count = len(affected & allies)
            distance = live.topology.distance_ft(actor_cell, origin)
            if distance is None:
                continue
            key = (
                bool(ally_count),
                -len(priorities),
                ally_count,
                priorities,
                distance,
                parse_cell(origin),
                direction_index,
            )
            if best_key is None or key < best_key:
                best_key = key
                best = MonsterAreaPlacement(
                    placed.template,
                    placed.origin,
                    placed.direction,
                    AreaSelection(placed.target_ids, placed.spared_ids),
                    tuple(c for c in live.initiative if c.entity_id in affected),
                    spec,
                    placed,
                )
    return best
