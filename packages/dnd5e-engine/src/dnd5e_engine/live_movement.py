"""Authoritative adapter for deterministic physical movement.

Planning is draw-free and precedes payment. Each live step resolves reactions
before leaving, revalidates and reprices, then pays, relocates, emits, and runs
area/range hooks. Pure geometry and accounting live in :mod:`movement`.
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import pairwise
from typing import TYPE_CHECKING, Literal

from dnd5e_engine.events import ActorMoved, CombatantMoved, ConditionRemoved, MoveFailed
from dnd5e_engine.movement import (
    MovementLedger,
    MovementMode,
    can_enter_creature_space,
    creature_space_is_difficult,
    grapple_drag_extra_cost,
    step_cost,
)
from dnd5e_engine.spatial import cell_id, parse_cell

if TYPE_CHECKING:
    from dnd5e_engine.orchestrator import PlayerIntent, _LiveCombat
    from dnd5e_engine.types.combat import Combatant


def effective_speed(actor: Combatant, mode: str, live: _LiveCombat | None = None) -> int:
    """Apply the same condition, Slow, rider and area pipeline to every speed."""
    from dnd5e_engine import orchestrator as o
    from dnd5e_engine.live_attack_riders import rider_speed
    from dnd5e_engine.persistent_areas import area_speed
    from dnd5e_engine.rules.conditions import exhaustion_level_of, project_speed

    base = actor.base_speed
    if mode in ("climb", "swim", "fly", "burrow"):
        special = getattr(actor.movement_modes, mode)
        if special is not None:
            base = special
    speed = project_speed(base, o._condition_names(actor), exhaustion_level_of(actor.conditions))
    if live is not None:
        if live.slow_marks.get(actor.entity_id):
            speed = max(0, speed - 10)
        speed = rider_speed(live, actor.entity_id, speed)
        speed = area_speed(live, actor.entity_id, speed)
    return speed


def ledger_for(live: _LiveCombat, actor: Combatant) -> MovementLedger:
    """Read without mutation, including compatibility with pre-ledger snapshots."""
    ledger = live.movement_ledgers.get(actor.entity_id)
    if ledger is not None:
        return ledger
    return MovementLedger(
        spent_ft=max(0, effective_speed(actor, "walk", live) - actor.movement_remaining)
    )


def project_remaining(live: _LiveCombat, actor_id: str) -> int:
    from dnd5e_engine import orchestrator as o

    actor = o._find_combatant(live, actor_id)
    if actor is None:
        return 0
    ledger = ledger_for(live, actor)
    remaining = ledger.remaining(effective_speed(actor, ledger.active_mode, live))
    o._update_combatant(live, actor_id, movement_remaining=remaining)
    return remaining


def add_dash(live: _LiveCombat, actor: Combatant) -> int:
    live.movement_ledgers[actor.entity_id] = ledger_for(live, actor).add_dash()
    return project_remaining(live, actor.entity_id)


def pay_movement(
    live: _LiveCombat, actor: Combatant, cost: int, distance: int, mode: MovementMode
) -> None:
    live.movement_ledgers[actor.entity_id] = ledger_for(live, actor).spend(
        cost_ft=cost,
        distance_ft=distance,
        mode=mode,
        effective_speed=effective_speed(actor, mode, live),
    )
    project_remaining(live, actor.entity_id)


def occupants(live: _LiveCombat, *, exclude: Sequence[str] = ()) -> dict[str, Combatant]:
    return {
        live.actor_zone[c.entity_id]: c
        for c in live.initiative
        if c.entity_id not in exclude
        and c.is_alive
        and c.entity_id not in live.dead_ids
        and c.entity_id in live.actor_zone
    }


def grapple_victims(live: _LiveCombat, actor_id: str) -> list[Combatant]:
    from dnd5e_engine import orchestrator as o

    return [
        c
        for c in live.initiative
        if c.entity_id != actor_id
        and c.is_alive
        and c.entity_id not in live.dead_ids
        and c.entity_id in live.actor_zone
        and o._condition_source_entity(live, c, "grappled") == actor_id
    ]


def movement_cost(
    live: _LiveCombat, actor: Combatant, a: str, b: str, mode: MovementMode
) -> int | None:
    """The shared PC/monster route and step oracle, with additive extra feet."""
    from dnd5e_engine import orchestrator as o

    edge = live.topology.edge_distance(a, b)
    if edge is None or o._frightened_approach_blocked(live, actor, [a, b]):
        return None
    victims = grapple_victims(live, actor.entity_id)
    victim_ids = [v.entity_id for v in victims]
    from dnd5e_engine.rules.conditions import conditions_block_actions

    occupant = occupants(live, exclude=(actor.entity_id, *victim_ids)).get(b)
    difficult_space = False
    if occupant is not None:
        allied = occupant.entity_id in o._allied_ids(live, actor.entity_id)
        if not can_enter_creature_space(
            actor.creature_size,
            occupant.creature_size,
            allied=allied,
            incapacitated=conditions_block_actions(o._condition_names(occupant)),
        ):
            return None
        difficult_space = creature_space_is_difficult(occupant.creature_size, allied=allied)
    return step_cost(
        live.topology.cell_size_ft,
        mode=mode,
        difficult_terrain=edge > live.topology.cell_size_ft,
        creature_space_difficult=difficult_space,
        has_special_speed=mode in ("climb", "swim")
        and getattr(actor.movement_modes, mode) is not None
        and effective_speed(actor, mode, live) > 0,
        drag_extra=grapple_drag_extra_cost(actor.creature_size, [v.creature_size for v in victims]),
    )


def drag_positions(
    live: _LiveCombat, actor: Combatant, a: str, b: str, positions: dict[str, str]
) -> dict[str, str] | None:
    """Pack followers deterministically into legal vacated/adjacent cells."""
    victims = grapple_victims(live, actor.entity_id)
    if not victims:
        return {}
    blocked = {
        positions[c.entity_id]
        for c in live.initiative
        if c.entity_id != actor.entity_id
        and c not in victims
        and c.is_alive
        and c.entity_id not in live.dead_ids
        and c.entity_id in positions
    }
    blocked.add(b)
    result: dict[str, str] = {}
    for index, victim in enumerate(victims):
        previous = positions[victim.entity_id]
        col, row = parse_cell(previous)
        candidates = (
            [a]
            if index == 0
            else [cell_id(col + x, row + y) for x in (-1, 0, 1) for y in (-1, 0, 1)]
        )
        candidates.sort(
            key=lambda cell: (live.topology.distance_ft(a, cell) or 0, parse_cell(cell))
        )
        chosen = next(
            (
                cell
                for cell in candidates
                if cell not in blocked
                and live.topology.is_valid_cell(cell)
                and (cell == previous or live.topology.edge_distance(previous, cell) is not None)
                and live.topology.within_range(b, cell, actor.melee_reach_ft)
            ),
            None,
        )
        if chosen is None:
            return None
        result[victim.entity_id] = chosen
        blocked.add(chosen)
    return result


def reconcile_grapple_range(live: _LiveCombat) -> None:
    from dnd5e_engine import orchestrator as o

    for victim in list(live.initiative):
        source_id = o._condition_source_entity(live, victim, "grappled")
        if source_id is None:
            continue
        source = o._find_combatant(live, source_id)
        a, b = live.actor_zone.get(source_id), live.actor_zone.get(victim.entity_id)
        if (
            source is not None
            and source_id not in live.dead_ids
            and source.is_alive
            and a is not None
            and b is not None
            and live.topology.within_range(a, b, source.melee_reach_ft)
        ):
            continue
        for condition in list(victim.conditions):
            if condition.condition == "grappled":
                o._release_one_grapple_effect(live, victim.entity_id, condition.source_effect_id)
        o._emit(live, ConditionRemoved(target_id=victim.entity_id, condition="grappled"))


def post_position_steps(live: _LiveCombat, previous: dict[str, str]) -> None:

    from dnd5e_engine.persistent_areas import after_movement_step

    for actor_id, cell in previous.items():
        after_movement_step(live, actor_id, cell)
    reconcile_grapple_range(live)


def position_step(live: _LiveCombat, actor_id: str, destination: str) -> str:
    """Only positional writer for voluntary, drag and existing push geometry."""
    previous = live.actor_zone[actor_id]
    live.actor_zone[actor_id] = cell_id(*parse_cell(destination))
    return previous


def _step_plan(
    live: _LiveCombat,
    actor: Combatant,
    start: str,
    destination: str,
    mode: MovementMode,
    *,
    end_move: bool,
) -> tuple[int, dict[str, str]] | None:
    """Check one step without drawing dice, paying or changing positions."""
    from dnd5e_engine import orchestrator as o

    speed = effective_speed(actor, mode, live)
    if (
        not actor.is_alive
        or actor.hp_current <= 0
        or actor.entity_id in live.dead_ids
        or live.actor_zone.get(actor.entity_id) != start
        or speed <= 0
        or ("prone" in o._condition_names(actor) and mode != "crawl")
    ):
        return None
    if end_move and destination in occupants(live, exclude=(actor.entity_id,)):
        return None
    cost = movement_cost(live, actor, start, destination, mode)
    if cost is None or cost > ledger_for(live, actor).remaining(speed):
        return None
    dragged = drag_positions(live, actor, start, destination, live.actor_zone)
    if dragged is None:
        return None
    return cost, dragged


def _route_from_reached(reached: dict[str, tuple[int, str | None]], goal: str) -> list[str]:
    route = [goal]
    while (previous := reached[route[-1]][1]) is not None:
        route.append(previous)
    return route[::-1]


def _route_static_valid(
    live: _LiveCombat, actor: Combatant, route: Sequence[str], mode: MovementMode
) -> bool:
    """Preflight every follower position before a route spends resources."""
    from dnd5e_engine import orchestrator as o

    if (
        not route
        or route[0] != live.actor_zone.get(actor.entity_id)
        or not actor.is_alive
        or actor.hp_current <= 0
        or actor.entity_id in live.dead_ids
        or effective_speed(actor, mode, live) <= 0
        or ("prone" in o._condition_names(actor) and mode != "crawl")
        or route[-1] in occupants(live, exclude=(actor.entity_id,))
    ):
        return False
    simulated = dict(live.actor_zone)
    for a, b in pairwise(route):
        if movement_cost(live, actor, a, b, mode) is None:
            return False
        dragged = drag_positions(live, actor, a, b, simulated)
        if dragged is None:
            return False
        simulated[actor.entity_id] = b
        simulated.update(dragged)
    return True


def _walk_route(live: _LiveCombat, actor_id: str, route: Sequence[str]) -> None:
    for index, next_cell in enumerate(route[1:], start=1):
        if take_step(live, actor_id, next_cell, end_move=index == len(route) - 1) is None:
            return


def _affordable_prefix(
    live: _LiveCombat,
    actor: Combatant,
    route: Sequence[str],
    budget_ft: int,
) -> list[str]:
    """Stop on the last free affordable cell, never inside a pass-through space."""
    occupied = occupants(live, exclude=(actor.entity_id,))
    cost, last_free = 0, 0
    for index, (a, b) in enumerate(pairwise(route), start=1):
        step = movement_cost(live, actor, a, b, "walk")
        if step is None or cost + step > budget_ft:
            break
        cost += step
        if b not in occupied:
            last_free = index
    return list(route[: last_free + 1])


def take_step(
    live: _LiveCombat,
    actor_id: str,
    destination: str,
    mode: MovementMode = "walk",
    *,
    emit_actor: bool = True,
    end_move: bool = False,
) -> tuple[int, dict[str, str]] | None:
    from dnd5e_engine import orchestrator as o

    actor, start = o._find_combatant(live, actor_id), live.actor_zone.get(actor_id)
    if actor is None or start is None:
        return None
    if _step_plan(live, actor, start, destination, mode, end_move=end_move) is None:
        return None
    if o._fire_opportunity_attacks_on_step(
        live, mover_id=actor_id, from_cell=start, to_cell=destination
    ):
        return None
    actor = o._find_combatant(live, actor_id)
    if actor is None:
        return None
    planned = _step_plan(live, actor, start, destination, mode, end_move=end_move)
    if planned is None:
        return None
    cost, dragged = planned
    pay_movement(live, actor, cost, live.topology.cell_size_ft, mode)
    previous = {actor_id: position_step(live, actor_id, destination)}
    for victim_id, cell in dragged.items():
        previous[victim_id] = position_step(live, victim_id, cell)
    if emit_actor or dragged:
        o._emit(
            live,
            ActorMoved(
                actor_id=actor_id,
                from_zone=start,
                to_zone=destination,
                distance_ft=live.topology.cell_size_ft,
                movement_mode=mode,
                movement_cost_ft=cost,
                path=(start, destination),
            ),
        )
    for victim_id, cell in dragged.items():
        if cell != previous[victim_id]:
            o._emit(
                live,
                CombatantMoved(
                    actor_id=victim_id,
                    from_zone=previous[victim_id],
                    to_zone=cell,
                    distance_ft=live.topology.distance_ft(previous[victim_id], cell) or 0,
                    forced=True,
                    movement_mode=mode,
                    movement_cost_ft=0,
                    dragged_by=actor_id,
                ),
            )
    post_position_steps(live, previous)
    return cost, dragged


def handle_move(live: _LiveCombat, actor: Combatant, intent: PlayerIntent) -> None:
    from dnd5e_engine import orchestrator as o

    mode, actor_id = intent.movement_mode, actor.entity_id
    destination, start = intent.target_zone_id, live.actor_zone.get(actor_id)
    reason: (
        Literal["speed_zero", "prone", "not_adjacent", "occupied", "blocked_path", "frightened"]
        | None
    ) = None
    if effective_speed(actor, mode, live) <= 0:
        reason = "speed_zero"
    elif "prone" in o._condition_names(actor) and mode != "crawl":
        reason = "prone"
    elif destination is None or start is None or destination == start:
        reason = "not_adjacent"
    elif destination in occupants(live, exclude=(actor_id,)):
        reason = "occupied"
    elif o._frightened_approach_blocked(live, actor, [start, destination]):
        reason = "frightened"
    elif (
        live.topology.is_adjacent(start, destination)
        and live.topology.edge_distance(start, destination) is None
    ):
        reason = "blocked_path"
    if reason is not None:
        o._emit(live, MoveFailed(actor_id=actor_id, reason=reason))
        return
    assert start is not None
    assert destination is not None
    path = live.topology.lowest_cost_path(
        start, destination, step_cost=lambda a, b: movement_cost(live, actor, a, b, mode)
    )
    if not path:
        o._emit(live, MoveFailed(actor_id=actor_id, reason="unreachable"))
        return
    total = sum(movement_cost(live, actor, a, b, mode) or 0 for a, b in pairwise(path))
    if total > ledger_for(live, actor).remaining(effective_speed(actor, mode, live)):
        o._emit(live, MoveFailed(actor_id=actor_id, reason="insufficient_movement"))
        return
    if o._frightened_approach_blocked(live, actor, path):
        o._emit(live, MoveFailed(actor_id=actor_id, reason="frightened"))
        return
    if not _route_static_valid(live, actor, path, mode):
        o._emit(live, MoveFailed(actor_id=actor_id, reason="blocked_path"))
        return
    run_path, run_cost = [start], 0

    def flush() -> None:
        nonlocal run_path, run_cost
        if len(run_path) > 1:
            o._emit(
                live,
                ActorMoved(
                    actor_id=actor_id,
                    from_zone=run_path[0],
                    to_zone=run_path[-1],
                    distance_ft=(len(run_path) - 1) * live.topology.cell_size_ft,
                    movement_mode=mode,
                    movement_cost_ft=run_cost,
                    path=tuple(run_path),
                ),
            )
            run_path, run_cost = [run_path[-1]], 0

    for index, next_cell in enumerate(path[1:], start=1):
        previous = live.actor_zone.get(actor_id)
        if previous is None:
            break
        split = bool(live.persistent_areas.areas or grapple_victims(live, actor_id))
        if split or o._opportunity_attackers(
            live, mover_id=actor_id, from_cell=previous, to_cell=next_cell
        ):
            flush()
        result = take_step(
            live, actor_id, next_cell, mode, emit_actor=split, end_move=index == len(path) - 1
        )
        if result is None:
            break
        if split:
            run_path = [next_cell]
        else:
            run_path.append(next_cell)
            run_cost += result[0]
    flush()


def push(live: _LiveCombat, target_id: str, origin_cell: str, distance_ft: int) -> None:
    """Reuse GridTopology.push_path; forced steps share all position hooks."""
    from dnd5e_engine import orchestrator as o

    start = live.actor_zone.get(target_id)
    if start is None or target_id in live.dead_ids:
        return
    path = live.topology.push_path(
        cell_id(*parse_cell(origin_cell)),
        start,
        distance_ft,
        occupied_cells=o._occupied_cells(live, exclude=(target_id,)),
    )
    target = o._find_combatant(live, target_id)
    split = bool(
        live.persistent_areas.areas
        or grapple_victims(live, target_id)
        or (target is not None and o._condition_source_entity(live, target, "grappled") is not None)
    )
    previous, moved = start, 0
    for next_cell in path:
        if (
            live.actor_zone.get(target_id) != previous
            or target_id in live.dead_ids
            or live.topology.edge_distance(previous, next_cell) is None
            or next_cell in occupants(live, exclude=(target_id,))
        ):
            break
        position_step(live, target_id, next_cell)
        moved += live.topology.cell_size_ft
        if split:
            o._emit(
                live,
                CombatantMoved(
                    actor_id=target_id,
                    from_zone=previous,
                    to_zone=next_cell,
                    distance_ft=live.topology.cell_size_ft,
                    forced=True,
                ),
            )
        post_position_steps(live, {target_id: previous})
        previous = next_cell
        if target_id in live.dead_ids or o._find_combatant(live, target_id) is None:
            break
    if moved and not split:
        o._emit(
            live,
            CombatantMoved(
                actor_id=target_id,
                from_zone=start,
                to_zone=previous,
                distance_ft=moved,
                forced=True,
            ),
        )


def flee(live: _LiveCombat, actor: Combatant, enemies: Sequence[Combatant]) -> None:
    start = live.actor_zone.get(actor.entity_id)
    threats = [live.actor_zone[c.entity_id] for c in enemies if c.entity_id in live.actor_zone]
    if start is None or not threats:
        return

    def nearest(cell: str) -> int:
        return min(live.topology.distance_ft(cell, threat) or 0 for threat in threats)

    reached = live.topology.reachable_cells(
        start,
        ledger_for(live, actor).remaining(effective_speed(actor, "walk", live)),
        step_cost=lambda a, b: movement_cost(live, actor, a, b, "walk"),
    )
    occupied = occupants(live, exclude=(actor.entity_id,))
    candidates = [
        (-nearest(cell), cost, parse_cell(cell), cell)
        for cell, (cost, _) in reached.items()
        if cell != start and cell not in occupied and nearest(cell) > nearest(start)
    ]
    for _, _, _, goal in sorted(candidates):
        route = _route_from_reached(reached, goal)
        if _route_static_valid(live, actor, route, "walk"):
            _walk_route(live, actor.entity_id, route)
            return


def close_to_target(live: _LiveCombat, actor: Combatant, target: Combatant, range_ft: int) -> bool:
    """Plan to a free firing/reach cell using the shared weighted oracle."""
    from dnd5e_engine import orchestrator as o

    start, target_cell = live.actor_zone[actor.entity_id], live.actor_zone[target.entity_id]
    if o._in_range_with_los(live.topology, start, target_cell, range_ft):
        return False
    remaining = ledger_for(live, actor).remaining(effective_speed(actor, "walk", live))
    reached = live.topology.reachable_cells(
        start,
        None,
        step_cost=lambda a, b: movement_cost(live, actor, a, b, "walk"),
    )
    occupied = occupants(live, exclude=(actor.entity_id,))
    candidates = [
        (cost, parse_cell(cell), cell)
        for cell, (cost, _) in reached.items()
        if cell not in occupied and o._in_range_with_los(live.topology, cell, target_cell, range_ft)
    ]
    for cost, _, goal in sorted(candidates):
        route = _route_from_reached(reached, goal)
        dashed = actor.action_available and remaining < cost <= remaining + effective_speed(
            actor, "walk", live
        )
        if cost > remaining and not dashed:
            route = _affordable_prefix(live, actor, route, remaining)
        if len(route) < 2 or not _route_static_valid(live, actor, route, "walk"):
            continue
        if dashed:
            o._update_combatant(live, actor.entity_id, action_available=False)
            o._apply_dash(live, actor, "action")
        _walk_route(live, actor.entity_id, route)
        return dashed
    return False
