"""Bounded movement and authorized weapon OA; no reactive runtime."""

from itertools import pairwise

from dnd5e_srd_data.loader import AssetLoader

from dnd5e_engine.evaluation_class_dependencies import operation_dependencies
from dnd5e_engine.evaluation_computation import CombatComputation
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, RuleEvaluationRequest
from dnd5e_engine.evaluation_monster_movement import monster_movement_failure
from dnd5e_engine.evaluation_opportunity import (
    OpportunityPlan,
    opportunity_dependencies,
    resolve_opportunity,
    step_reactors,
    walk_stopped,
)
from dnd5e_engine.evaluation_preflight import template_support_failure
from dnd5e_engine.evaluation_projection import attack_delta
from dnd5e_engine.evaluation_rng import RNGTransition
from dnd5e_engine.evaluation_state import CharacterStateV2, CombatSnapshot, MovementLedgerState
from dnd5e_engine.evaluation_turn import TurnEvaluation, _refuse
from dnd5e_engine.events import ActorMoved, DashTaken, IntentSubmitted
from dnd5e_engine.movement import can_enter_creature_space, creature_space_is_difficult, step_cost
from dnd5e_engine.spatial import GridTopology
from dnd5e_engine.specs import GridScene
from dnd5e_engine.turn_rules import ordinary_action_payment


def evaluate_movement(request: RuleEvaluationRequest, loader: AssetLoader) -> TurnEvaluation:
    snapshot, intent = request.state_snapshot, request.payload
    assert isinstance(snapshot, CombatSnapshot)
    assert isinstance(intent, CombatIntentPayload)
    basic = CombatIntentPayload(
        intent_type=intent.intent_type,
        source_id=intent.source_id,
        target_zone_id=intent.target_zone_id if intent.intent_type == "move" else None,
    )
    if intent != basic:
        return _refuse(
            "unsupported",
            "movement.intent",
            "only ordinary walking / Action Dash / Disengage are migrated",
        )
    state, actor_id = snapshot.combat_state, request.actor_id
    if state.ended:
        return _refuse("rejected", "combat_ended", "combat has ended")
    actors = {a.entity_id: a for a in snapshot.character_states}
    if actor_id not in actors or actor_id != state.initiative_ids[state.current_turn_index]:
        return _refuse("rejected", "not_actor_turn", "actor does not own this turn")
    actor = actors[actor_id]
    if actor_id in state.dead_ids or not actor.is_alive or actor.hp_current <= 0:
        return _refuse("rejected", "actor_incapacitated", "actor cannot move or act")
    if failure := monster_movement_failure(snapshot, loader):
        return _refuse("unsupported", "movement.template", failure)
    if state.movement_ledgers[actor_id].active_mode != "walk":
        return _refuse("unsupported", "movement.mode", "only an active walking ledger is admitted")
    opportunity, failure = opportunity_dependencies(snapshot, loader)
    failure = (
        failure
        or operation_dependencies(
            snapshot,
            loader,
            movement=True,
            attack=bool(state.pending_reactions),
            reviewed_reactions=bool(state.pending_reactions),
            equipped=True,
        )
        or template_support_failure(snapshot, loader)
    )
    if failure:
        return _refuse("unsupported", "movement.capability", failure)
    # Explicit occupancy includes every living actor; no hidden map/encounter state.
    grid = GridTopology(GridScene.model_validate(snapshot.scene_state.grid.model_dump()))
    occupied = {
        state.actor_zone[a.entity_id]: a
        for a in actors.values()
        if a.entity_id != actor_id and a.is_alive and a.entity_id not in state.dead_ids
    }
    if len(
        {
            state.actor_zone[a.entity_id]
            for a in actors.values()
            if a.is_alive and a.entity_id not in state.dead_ids
        }
    ) != sum(a.is_alive and a.entity_id not in state.dead_ids for a in actors.values()):
        return _refuse("unsupported", "movement.occupancy", "living occupancy must be unique")
    if any(not grid.is_valid_cell(cell) for cell in state.actor_zone.values()):
        return _refuse("unsupported", "movement.position", "actors require legal unblocked cells")
    ledger = state.movement_ledgers[actor_id]
    if actor.movement_remaining != ledger.remaining(actor.base_speed):
        return _refuse("unsupported", "movement.ledger", "walk budget and explicit ledger disagree")
    path: list[str] = []
    if intent.intent_type == "move":
        planned = _move_plan(snapshot, actor_id, intent, grid, occupied, loader, opportunity)
        if isinstance(planned, dict):
            return planned
        path, _cost, _distance = planned
    elif intent.intent_type not in {"dash", "disengage"}:
        return _refuse("unsupported", "movement.intent", "unsupported movement action")
    elif not actor.action_available:
        return _refuse("rejected", "no_action_economy", "ordinary Action is unavailable")
    rng = request.rng_context.state.restore()
    computation = CombatComputation(snapshot, rng)
    current = computation.actors[actor_id]
    if intent.intent_type == "move":
        path = _execute_walk(computation, path, grid, occupied, loader, opportunity, actor_id)
    else:
        current = current.model_copy(update=ordinary_action_payment(current))
        if intent.intent_type == "dash":
            updated = ledger.add_dash()
            remaining = updated.remaining(actor.base_speed)
            computation.state = state.model_copy(
                update={
                    "movement_ledgers": {
                        **state.movement_ledgers,
                        actor_id: MovementLedgerState.model_validate(updated.model_dump()),
                    }
                }
            )
            current = current.model_copy(update={"movement_remaining": remaining})
            computation.emit(
                DashTaken(
                    actor_id=actor_id,
                    budget_consumed="action",
                    doubled_movement_remaining=remaining,
                )
            )
        else:
            current = current.model_copy(update={"disengaging_this_turn": True})
            computation.emit(IntentSubmitted(actor_id=actor_id, intent_type="disengage"))
        computation.actors[actor_id] = current
    after = computation.result()
    return dict(
        status="accepted",
        state_delta=attack_delta(
            snapshot, after, position_paths={actor_id: tuple(path)} if len(path) > 1 else {}
        ),
        proposed_events=tuple(computation.events),
        rng_transition=RNGTransition.between(request.rng_context, rng),
        choice=None,
        error=None,
    )


def _move_plan(
    snapshot: CombatSnapshot,
    actor_id: str,
    intent: CombatIntentPayload,
    grid: GridTopology,
    occupied: dict[str, CharacterStateV2],
    loader: AssetLoader,
    opportunity: OpportunityPlan | None,
) -> tuple[list[str], int, int] | TurnEvaluation:
    state = snapshot.combat_state
    actors = {a.entity_id: a for a in snapshot.character_states}
    actor = actors[actor_id]
    ledger = state.movement_ledgers[actor_id]
    start, destination = state.actor_zone[actor_id], intent.target_zone_id
    if actor.base_speed <= 0:
        return _refuse("rejected", "movement.speed", "Speed is zero")
    if destination is None or destination == start or not grid.is_valid_cell(destination):
        return _refuse("rejected", "movement.destination", "destination must be another legal cell")
    if destination in occupied:
        return _refuse("rejected", "movement.occupied", "destination is occupied")
    if grid.is_adjacent(start, destination) and grid.edge_distance(start, destination) is None:
        return _refuse("rejected", "movement.blocked", "adjacent step is blocked")
    allied = state.party_ids if actor_id in state.party_ids else state.encounter_ids

    def price(a: str, b: str) -> int | None:
        edge = grid.edge_distance(a, b)
        if edge is None:
            return None
        occupant = occupied.get(b)
        difficult = False
        if occupant is not None:
            friend = occupant.entity_id in allied
            if not can_enter_creature_space(
                actor.creature_size, occupant.creature_size, allied=friend
            ):
                return None
            difficult = creature_space_is_difficult(occupant.creature_size, allied=friend)
        return step_cost(
            grid.cell_size_ft,
            difficult_terrain=edge > grid.cell_size_ft,
            creature_space_difficult=difficult,
        )

    path = grid.lowest_cost_path(start, destination, step_cost=price)
    if not path:
        return _refuse("rejected", "movement.unreachable", "no legal route")
    cost = sum(price(a, b) or 0 for a, b in pairwise(path))
    distance = (len(path) - 1) * grid.cell_size_ft
    if cost > ledger.remaining(actor.base_speed):
        return _refuse("rejected", "movement.insufficient", "route exceeds movement allowance")
    for a, b in pairwise(path):
        _, failure = step_reactors(snapshot, actor_id, a, b, loader, opportunity)
        if failure:
            return _refuse("unsupported", "movement.opportunity_attack", failure)
    return path, cost, distance


def _execute_walk(
    computation: CombatComputation,
    path: list[str],
    grid: GridTopology,
    occupied: dict[str, CharacterStateV2],
    loader: AssetLoader,
    opportunity: OpportunityPlan | None,
    actor_id: str,
) -> list[str]:
    completed, run, run_cost = [path[0]], [path[0]], 0

    def flush() -> None:
        nonlocal run, run_cost
        if len(run) > 1:
            computation.emit(
                ActorMoved(
                    actor_id=actor_id,
                    from_zone=run[0],
                    to_zone=run[-1],
                    distance_ft=(len(run) - 1) * grid.cell_size_ft,
                    movement_mode="walk",
                    movement_cost_ft=run_cost,
                    path=tuple(run),
                )
            )
            run, run_cost = [run[-1]], 0

    for a, b in pairwise(path):
        fires, failure = step_reactors(computation.result(), actor_id, a, b, loader, opportunity)
        if failure:
            raise RuntimeError("preflight omitted a reaction dependency")
        if fires:
            flush()
            assert opportunity is not None
            resolve_opportunity(computation, opportunity, actor_id)
            if walk_stopped(computation, computation.actors[actor_id], a):
                break
        actor = computation.actors[actor_id]
        occupant = occupied.get(b)
        allied = (
            computation.state.party_ids
            if actor_id in computation.state.party_ids
            else computation.state.encounter_ids
        )
        cost = step_cost(
            grid.cell_size_ft,
            difficult_terrain=(grid.edge_distance(a, b) or 0) > grid.cell_size_ft,
            creature_space_difficult=bool(
                occupant
                and creature_space_is_difficult(
                    occupant.creature_size, allied=occupant.entity_id in allied
                )
            ),
        )
        ledger = computation.state.movement_ledgers[actor_id].spend(
            cost_ft=cost,
            distance_ft=grid.cell_size_ft,
            mode="walk",
            effective_speed=computation.speed(actor),
        )
        computation.state = computation.state.model_copy(
            update={
                "actor_zone": {**computation.state.actor_zone, actor_id: b},
                "movement_ledgers": {
                    **computation.state.movement_ledgers,
                    actor_id: MovementLedgerState.model_validate(ledger.model_dump()),
                },
            }
        )
        computation.actors[actor_id] = actor.model_copy(
            update={"movement_remaining": ledger.remaining(computation.speed(actor))}
        )
        completed.append(b)
        run.append(b)
        run_cost += cost
    flush()
    if computation.damage:
        raise RuntimeError("movement left an incomplete damage instance")
    return completed
