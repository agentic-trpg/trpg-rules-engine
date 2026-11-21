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
from dnd5e_engine.evaluation_region_effect import supports_fixed_combat_regions
from dnd5e_engine.evaluation_rng import RNGTransition
from dnd5e_engine.evaluation_speed_effects import (
    SpeedEffectComputation,
    has_speed_effects,
    speed_effect_support_failure,
    speed_with_effects,
    without_speed_effect,
)
from dnd5e_engine.evaluation_state import CharacterStateV2, CombatSnapshot, MovementLedgerState
from dnd5e_engine.evaluation_turn import TurnEvaluation, _refuse
from dnd5e_engine.evaluation_world_spell_modifiers import world_spell_combat_modifiers
from dnd5e_engine.events import ActorMoved, DashTaken, IntentSubmitted
from dnd5e_engine.movement import (
    can_enter_creature_space,
    creature_space_is_difficult,
    project_speeds,
    step_cost,
)
from dnd5e_engine.rules.conditions import active_condition_names, exhaustion_level_of
from dnd5e_engine.spatial import GridTopology
from dnd5e_engine.specs import GridScene
from dnd5e_engine.turn_rules import ordinary_action_payment
from dnd5e_engine.types.combat import Combatant


def timed_walk_bonuses(snapshot: CombatSnapshot) -> dict[str, int]:
    """Project walking bonuses from supported elapsed spells by combatant.

    A zero entry identifies a target whose elapsed effects are reviewed and do
    not change walking speed. Other effects and reaction graphs raise ValueError.
    The host owns the deadlines and supplies only active effects.
    """
    if not snapshot.effect_states:
        return {}
    state = snapshot.combat_state
    if (
        snapshot.snapshot_schema_version != "engine-snapshot/20"
        or state.concentration_chain
        or state.conditions_by_effect
        or state.effect_lifecycles
        or state.pending_reactions
        or state.active_reaction_responses
        or state.reaction_effects_pending_expiry
    ):
        raise ValueError("timed walking requires passive spells and no reaction or effect graph")
    return {
        target: walk
        for target, (walk, _) in world_spell_combat_modifiers(snapshot.effect_states).items()
    }


def walk_speed(actor: CharacterStateV2 | Combatant, bonus: int = 0) -> int:
    """Project walking speed with the admitted additive bonus and condition locks."""
    return project_speeds(
        actor.base_speed + bonus,
        actor.movement_modes,
        condition_names=active_condition_names(actor.conditions),
        exhaustion_level=exhaustion_level_of(actor.conditions),
    ).walk


def _movement_effects(
    snapshot: CombatSnapshot, loader: AssetLoader, *, walking: bool
) -> tuple[bool, dict[str, int]]:
    """Admit the relevant movement effect profile or explain its refusal."""
    if has_speed_effects(snapshot):
        if not walking:
            raise ValueError("speed effect currently admits walking only")
        if failure := speed_effect_support_failure(snapshot, loader):
            raise ValueError(failure)
        return True, {}
    return False, timed_walk_bonuses(snapshot) if walking else {}


def _walk_step_cost(
    grid: GridTopology,
    occupied: dict[str, CharacterStateV2],
    allied: set[str],
    actor: CharacterStateV2 | Combatant,
    start: str,
    end: str,
) -> int | None:
    """Resolve a single walking edge with terrain and creature occupancy rules."""
    edge = grid.edge_distance(start, end)
    if edge is None:
        return None
    occupant = occupied.get(end)
    difficult = False
    if occupant is not None:
        friend = occupant.entity_id in allied
        if not can_enter_creature_space(actor.creature_size, occupant.creature_size, allied=friend):
            return None
        difficult = creature_space_is_difficult(occupant.creature_size, allied=friend)
    return step_cost(
        grid.cell_size_ft,
        difficult_terrain=edge > grid.cell_size_ft,
        creature_space_difficult=difficult,
    )


def evaluate_movement(request: RuleEvaluationRequest, loader: AssetLoader) -> TurnEvaluation:
    """Admit movement and compute its route, costs and candidate consequences.

    Validate actor/turn, explicit geometry and supported effect/reaction dependencies
    before resolution. Walking and bound actors require the supported monster movement
    profile. Dash and Disengage by an unbound actor do not use other monsters' reach.
    Execute in isolated working state and project positions, expenditure, events and RNG
    transition into an uncommitted result. Invalid or unsupported input returns a refusal.
    """
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
    if (intent.intent_type == "move" or actor_id in state.monster_slug_by_entity) and (
        failure := monster_movement_failure(snapshot, loader)
    ):
        return _refuse("unsupported", "movement.template", failure)
    if state.movement_ledgers[actor_id].active_mode != "walk":
        return _refuse("unsupported", "movement.mode", "only an active walking ledger is admitted")
    reviewed_regions = supports_fixed_combat_regions(snapshot, loader)
    try:
        speed_effect, timed = (
            (False, {})
            if reviewed_regions
            else _movement_effects(snapshot, loader, walking=intent.intent_type == "move")
        )
    except ValueError as error:
        return _refuse("unsupported", "movement.capability", str(error))
    bonus = timed.get(actor_id, 0)
    speed = (
        speed_with_effects(actor, snapshot.effect_states)
        if speed_effect
        else walk_speed(actor, bonus)
        if intent.intent_type == "move"
        else actor.base_speed
    )
    dependencies = without_speed_effect(snapshot) if speed_effect else snapshot
    opportunity, failure = opportunity_dependencies(snapshot, loader)
    failure = (
        failure
        or operation_dependencies(
            dependencies,
            loader,
            movement=True,
            attack=bool(state.pending_reactions),
            reviewed_reactions=bool(state.pending_reactions),
            reviewed_world_effects=bool(timed),
            reviewed_region_conditions=reviewed_regions,
            equipped=True,
        )
        or template_support_failure(snapshot, loader)
    )
    if failure:
        return _refuse("unsupported", "movement.capability", failure)
    # Explicit occupancy includes every living actor; no hidden map/encounter state.
    grid = GridTopology(GridScene.model_validate(snapshot.scene_state.grid.model_dump()))
    occupied = {
        state.actor_zone[actor.entity_id]: actor
        for actor in actors.values()
        if actor.entity_id != actor_id and actor.is_alive and actor.entity_id not in state.dead_ids
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
    if actor.movement_remaining != ledger.remaining(speed):
        return _refuse("unsupported", "movement.ledger", "walk budget and explicit ledger disagree")
    path: list[str] = []
    if intent.intent_type == "move":
        planned = _move_plan(snapshot, actor, intent, grid, occupied, loader, opportunity, speed)
        if isinstance(planned, dict):
            return planned
        path = planned
    elif intent.intent_type not in {"dash", "disengage"}:
        return _refuse("unsupported", "movement.intent", "unsupported movement action")
    elif not actor.action_available:
        return _refuse("rejected", "no_action_economy", "ordinary Action is unavailable")
    rng = request.rng_context.state.restore()
    computation = (
        SpeedEffectComputation(snapshot, rng) if speed_effect else CombatComputation(snapshot, rng)
    )
    if intent.intent_type == "move":
        computation.enable_stages()
    current = computation.actors[actor_id]
    if intent.intent_type == "move":
        path = _execute_walk(
            computation, path, grid, occupied, loader, opportunity, actor_id, bonus
        )
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
    computed = computation.result()
    after = computed.candidate_state
    return dict(
        status="accepted",
        state_delta=attack_delta(
            snapshot, after, position_paths={actor_id: tuple(path)} if len(path) > 1 else {}
        ),
        proposed_events=computed.proposed_events,
        rng_transition=RNGTransition.between(request.rng_context, rng),
        choice=None,
        error=None,
        stage_facts=computed.stage_facts,
    )


def _move_plan(
    snapshot: CombatSnapshot,
    actor: CharacterStateV2,
    intent: CombatIntentPayload,
    grid: GridTopology,
    occupied: dict[str, CharacterStateV2],
    loader: AssetLoader,
    opportunity: OpportunityPlan | None,
    speed: int,
) -> list[str] | TurnEvaluation:
    """Prepare the admitted route and its movement prerequisites."""
    state = snapshot.combat_state
    actor_id = actor.entity_id
    ledger = state.movement_ledgers[actor_id]
    start, destination = state.actor_zone[actor_id], intent.target_zone_id
    if speed <= 0:
        return _refuse("rejected", "movement.speed", "Speed is zero")
    if destination is None or destination == start or not grid.is_valid_cell(destination):
        return _refuse("rejected", "movement.destination", "destination must be another legal cell")
    if destination in occupied:
        return _refuse("rejected", "movement.occupied", "destination is occupied")
    if grid.is_adjacent(start, destination) and grid.edge_distance(start, destination) is None:
        return _refuse("rejected", "movement.blocked", "adjacent step is blocked")
    allied = state.party_ids if actor_id in state.party_ids else state.encounter_ids

    def price(a: str, b: str) -> int | None:
        """Compute movement cost for the next candidate step."""
        return _walk_step_cost(grid, occupied, allied, actor, a, b)

    path = grid.lowest_cost_path(start, destination, step_cost=price)
    if not path:
        return _refuse("rejected", "movement.unreachable", "no legal route")
    cost = sum(price(a, b) or 0 for a, b in pairwise(path))
    if cost > ledger.remaining(speed):
        return _refuse("rejected", "movement.insufficient", "route exceeds movement allowance")
    for a, b in pairwise(path):
        _, failure = step_reactors(snapshot, actor_id, a, b, loader, opportunity, grid=grid)
        if failure:
            return _refuse("unsupported", "movement.opportunity_attack", failure)
    return path


def _execute_walk(
    computation: CombatComputation,
    path: list[str],
    grid: GridTopology,
    occupied: dict[str, CharacterStateV2],
    loader: AssetLoader,
    opportunity: OpportunityPlan | None,
    actor_id: str,
    bonus: int,
) -> list[str]:
    """Execute the validated route in isolated candidate state."""
    completed, run, run_cost = [path[0]], [path[0]], 0

    def flush() -> None:
        """Emit accumulated movement evidence at the enclosing resolution boundary."""
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
        if opportunity is not None:
            fires, failure = step_reactors(
                computation.candidate_state(), actor_id, a, b, loader, opportunity, grid=grid
            )
            if failure:
                raise RuntimeError("preflight omitted a reaction dependency")
            if fires:
                flush()
                resolve_opportunity(computation, opportunity, actor_id)
                if walk_stopped(computation, computation.actors[actor_id], a):
                    break
        actor = computation.actors[actor_id]
        allied = (
            computation.state.party_ids
            if actor_id in computation.state.party_ids
            else computation.state.encounter_ids
        )
        cost = _walk_step_cost(grid, occupied, allied, actor, a, b)
        if cost is None:
            raise RuntimeError("preflight omitted an illegal movement step")
        speed = walk_speed(actor, bonus) if bonus else computation.speed(actor)
        ledger = computation.state.movement_ledgers[actor_id].spend(
            cost_ft=cost,
            distance_ft=grid.cell_size_ft,
            mode="walk",
            effective_speed=speed,
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
            update={"movement_remaining": ledger.remaining(speed)}
        )
        computation.sample_stage("movement_step", (actor_id,))
        completed.append(b)
        run.append(b)
        run_cost += cost
    flush()
    if computation.damage:
        raise RuntimeError("movement left an incomplete damage instance")
    return completed
