"""Verify bounded stage proofs against their request, events and typed final Delta.

No action execution, damage/resource reconstruction, dice or world reads occur.
This checks consistency of trusted RE output, not all world Evidence or noticing.
"""

from collections import Counter
from collections.abc import Iterable
from typing import TYPE_CHECKING

from dnd5e_engine import evaluation_delta as delta
from dnd5e_engine.activities.passive_stats import CombatantMovementModes
from dnd5e_engine.evaluation_stage_capture import stage_profile_reason
from dnd5e_engine.evaluation_stages import (
    EventStageFacts,
    MechanicalStage,
    StageActorFacts,
    StageCombatFacts,
    actor_facts,
    combat_facts,
)
from dnd5e_engine.evaluation_state import CharacterStateV2, CombatSnapshot
from dnd5e_engine.events import (
    ActorMoved,
    AttackRolled,
    CombatEvent,
    ConditionApplied,
    ConditionRemoved,
    DamageApplied,
    Death,
    DeathSaveRolled,
    DeathSaveStarted,
    IntentSubmitted,
    RoundStarted,
    Stabilized,
    TurnEnded,
    TurnPhase,
    TurnStarted,
    Unconscious,
)
from dnd5e_engine.movement import (
    creature_space_is_difficult,
    project_speeds,
    speed_for_mode,
    step_cost,
)
from dnd5e_engine.spatial import GridTopology
from dnd5e_engine.specs import GridScene

if TYPE_CHECKING:
    from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest


def _require(value: bool, reason: str) -> None:
    """Raise an evaluation invariant error when the supplied condition fails."""
    if not value:
        raise ValueError(reason)


def _initial(snapshot: CombatSnapshot) -> dict[str, StageActorFacts]:
    """Project initial actor facts from the fixed snapshot."""
    state = snapshot.combat_state
    return {
        a.entity_id: actor_facts(
            a, state.actor_zone[a.entity_id], state.movement_ledgers[a.entity_id]
        )
        for a in snapshot.character_states
    }


def _fields(old: StageActorFacts, new: StageActorFacts) -> set[str]:
    """Read the field set relevant to stage validation."""
    return {
        name for name in StageActorFacts.model_fields if getattr(old, name) != getattr(new, name)
    }


def _bounded_actor(
    previous: StageActorFacts,
    update: StageActorFacts,
    fields: set[str],
    budget_fields: set[str] | None = None,
) -> None:
    """Check actor facts against the audited stage profile."""
    _require(
        not (_fields(previous, update) - fields),
        "stage boundary contains unrelated actor mutations",
    )
    if budget_fields is not None:
        before = previous.budget
        changed = {
            name
            for name in type(before).model_fields
            if getattr(before, name) != getattr(update.budget, name)
        }
        _require(not changed - budget_fields, "stage boundary contains unrelated budget mutations")


def _actor_changes(
    stage: MechanicalStage,
    old: dict[str, StageActorFacts],
    identities: set[str],
    fields: set[str],
    budget_fields: set[str] | None = None,
) -> None:
    """Validate the allowed actor fact changes between stages."""
    for update in stage.actor_updates:
        _require(
            update.actor_id in identities, "stage boundary contains unrelated participant mutations"
        )
        _bounded_actor(old[update.actor_id], update, fields, budget_fields)


def _consistent(
    sources: dict[str, CharacterStateV2],
    modes: dict[str, CombatantMovementModes],
    actors: dict[str, StageActorFacts],
    combat: StageCombatFacts,
    identities: Iterable[str],
) -> None:
    """Check agreement between stage facts and their corresponding mechanical state."""
    for identity in identities:
        source = sources[identity]
        actor = actors[source.entity_id]
        _require(
            all(
                c.condition in {"unconscious", "prone"}
                and c.source_entity_id in {"implied:event", "implied:revive"}
                for c in actor.conditions
            ),
            "complete stage condition references an unaudited source or mechanic",
        )
        if source.entity_id in combat.dead_ids:
            _require(
                not actor.death.is_alive and actor.hp_current == 0,
                "stage dead membership contradicts life/HP",
            )
        _require(
            actor.death.is_alive or source.entity_id in combat.dead_ids,
            "stage life state lacks dead membership",
        )
        saves = actor.death.death_saves
        if actor.death.is_alive and saves is not None:
            _require(
                saves.is_stable == (saves.successes == 3) and saves.failures < 3,
                "stage death-save stability/counters contradict life state",
            )
        speeds = project_speeds(
            source.base_speed,
            modes[identity],
            condition_names=tuple(c.condition for c in actor.conditions),
            exhaustion_level=max(
                (c.exhaustion_level for c in actor.conditions if c.condition == "exhaustion"),
                default=0,
            ),
        )
        _require(
            actor.budget.movement_remaining
            == actor.movement.remaining(speed_for_mode(speeds, actor.movement.active_mode)),
            "stage movement ledger contradicts remaining budget",
        )


def _event(
    stage: MechanicalStage,
    event: CombatEvent,
    facts: EventStageFacts,
    actors: dict[str, StageActorFacts],
    combat: StageCombatFacts,
    old: dict[str, StageActorFacts],
    snapshot: CombatSnapshot,
    actor_id: str,
    target_id: str | None,
    intent: str,
    steps: dict[int, StageActorFacts],
    last_save: DeathSaveRolled | None,
) -> None:
    """Validate one event against its referenced stage facts."""
    for name in ("actor_id", "attacker_id", "target_id", "source_actor_id"):
        identity = getattr(event, name, None)
        _require(
            identity is None or identity in actors, "event entity is outside stage participants"
        )
    scopes: dict[str, tuple[set[str], set[str]]] = {
        "attack_rolled": ({actor_id}, {"budget"}),
        "damage_applied": ({target_id or ""}, {"hp_current", "temp_hp", "last_damaged_by"}),
        "death": ({getattr(event, "target_id", "")}, {"death"}),
        "condition_applied": ({getattr(event, "target_id", "")}, {"death", "conditions", "budget"}),
        "condition_removed": (
            {getattr(event, "target_id", "")},
            {"death", "conditions", "budget", "hp_current"},
        ),
        "healing_applied": ({getattr(event, "target_id", "")}, {"hp_current"}),
    }
    ids, fields = scopes.get(event.type, (set(), set()))
    for update in stage.actor_updates:
        _require(
            update.actor_id in ids and not (_fields(old[update.actor_id], update) - fields),
            "event stage contains unrelated actor mutations",
        )
    if isinstance(event, AttackRolled):
        _actor_changes(stage, old, {actor_id}, {"budget"}, {"attack_rolls_made_this_turn"})
    elif isinstance(event, (ConditionApplied, ConditionRemoved)):
        _actor_changes(stage, old, {event.target_id}, fields, {"movement_remaining"})
    if isinstance(event, IntentSubmitted):
        _require(
            event.actor_id == actor_id and event.intent_type == intent,
            "declaration differs from request",
        )
        _require(
            not stage.actor_updates and stage.combat_update is None,
            "declaration is not an execution boundary",
        )
    elif isinstance(event, AttackRolled):
        _require(
            intent == "attack"
            and event.attacker_id == actor_id
            and event.target_id == target_id
            and not event.is_opportunity_attack,
            "attack event differs from basic request",
        )
        refs = facts.event_references[stage.event_cursor - 1].stage_sequences
        _require(
            len(refs) == 2 and facts.stages[refs[0]].kind == "attack_execution",
            "attack roll lacks its real execution boundary",
        )
    elif isinstance(event, DamageApplied):
        _require(
            intent == "attack"
            and event.target_id == target_id
            and event.source_actor_id == actor_id,
            "damage event differs from request",
        )
        _require(
            event.damage_instance_id
            == f"damage:{combat.damage_instance_sequence}:{actor_id}:{target_id}",
            "damage event and sampled sequence binding disagree",
        )
    elif isinstance(event, Death):
        _require(
            event.target_id in combat.dead_ids and not actors[event.target_id].death.is_alive,
            "Death is not sampled after its fold",
        )
        _require(
            (
                event.reason in {"damage", "instant_kill"}
                and intent == "attack"
                and event.target_id == target_id
            )
            or (
                event.reason == "death_saves"
                and (
                    (last_save is not None and event.target_id == last_save.target_id)
                    or (intent == "attack" and event.target_id == target_id)
                )
            ),
            "Death lacks its actual damage/save cause",
        )
    elif isinstance(event, (DeathSaveStarted, DeathSaveRolled, Stabilized)):
        current = snapshot.combat_state.initiative_ids[combat.turn.current_turn_index]
        source = next(a for a in snapshot.character_states if a.entity_id == event.target_id)
        _require(
            event.target_id == current
            and source.entity_type == "Character"
            and actors[current].hp_current == 0
            and actors[current].death.is_alive,
            "death-save event is outside the current dying Character boundary",
        )
    elif isinstance(event, (ConditionApplied, ConditionRemoved)):
        present = event.condition in {c.condition for c in actors[event.target_id].conditions}
        _require(
            present == isinstance(event, ConditionApplied),
            "condition event and sampled conditions disagree",
        )
    elif isinstance(event, (TurnStarted, TurnEnded)):
        current = snapshot.combat_state.initiative_ids[combat.turn.current_turn_index]
        _require(event.actor_id == current, "turn event and sampled initiative disagree")
    elif isinstance(event, (RoundStarted, TurnPhase)):
        _require(
            event.round_number == combat.turn.round_number, "round event and sampled round disagree"
        )
        if isinstance(event, TurnPhase) and event.phase != "round_start":
            _require(
                event.actor_id
                == snapshot.combat_state.initiative_ids[combat.turn.current_turn_index],
                "phase actor and sampled initiative disagree",
            )
    elif isinstance(event, ActorMoved):
        refs = facts.event_references[stage.event_cursor - 1].stage_sequences
        points = refs[:-1]
        _require(
            intent == "move"
            and event.actor_id == actor_id
            and not event.forced
            and event.movement_mode == "walk",
            "movement event differs from ordinary request",
        )
        _require(
            bool(points) and all(i in steps for i in points),
            "movement summary lacks actual traversed steps",
        )
        start = snapshot.combat_state.actor_zone[actor_id]
        _require(
            event.path == (start, *(steps[i].cell for i in points)),
            "movement summary path differs from sampled steps",
        )
        _require(
            event.from_zone == start and event.to_zone == actors[actor_id].cell,
            "movement summary endpoints disagree",
        )
        _require(
            event.distance_ft == len(points) * snapshot.scene_state.grid.cell_size_ft,
            "movement summary distance disagrees",
        )
        initial = snapshot.combat_state.movement_ledgers[actor_id]
        _require(
            event.movement_cost_ft == actors[actor_id].movement.spent_ft - initial.spent_ft,
            "movement summary cost disagrees with sampled ledger",
        )


def _final_delta(
    snapshot: CombatSnapshot,
    state_delta: delta.StateDelta,
    actors: dict[str, StageActorFacts],
    combat: StageCombatFacts,
    initial: dict[str, StageActorFacts],
) -> None:
    """Check final stage facts against the proposed Delta."""
    expected = dict(initial)
    first_combat = combat_facts(snapshot.combat_state)
    final_combat = first_combat
    seen: set[tuple[str, str | None]] = set()
    for op in state_delta.operations:
        identity = getattr(op, "target_id", getattr(op, "actor_id", None))
        write_key = (op.kind, identity)
        _require(write_key not in seen, "duplicate covered Delta write")
        seen.add(write_key)
        combat_id = getattr(op, "combat_id", None)
        _require(
            combat_id is None or combat_id == snapshot.combat_state.combat_id,
            "Delta combat binding differs from stage input",
        )
        if identity is not None:
            _require(identity in initial, "Delta actor is outside stage input")
        if isinstance(op, delta.HPDelta):
            _require(
                initial[op.target_id].hp_current == op.expected_hp,
                "Delta HP pre-value differs from stage input",
            )
            expected[op.target_id] = expected[op.target_id].model_copy(
                update={"hp_current": op.resulting_hp}
            )
        elif isinstance(op, delta.TempHPSet):
            _require(
                initial[op.target_id].temp_hp == op.expected_temp_hp,
                "Delta temp HP pre-value differs",
            )
            expected[op.target_id] = expected[op.target_id].model_copy(
                update={"temp_hp": op.temp_hp}
            )
        elif isinstance(
            op,
            (
                delta.DeathStateUpdate,
                delta.ConditionsUpdate,
                delta.DamageAttributionUpdate,
                delta.ActionBudgetUpdate,
                delta.MovementLedgerUpdate,
            ),
        ):
            field = {
                delta.DeathStateUpdate: "death",
                delta.ConditionsUpdate: "conditions",
                delta.DamageAttributionUpdate: "last_damaged_by",
                delta.ActionBudgetUpdate: "budget",
                delta.MovementLedgerUpdate: "movement",
            }[type(op)]
            key = (
                op.target_id
                if isinstance(
                    op,
                    (delta.DeathStateUpdate, delta.ConditionsUpdate, delta.DamageAttributionUpdate),
                )
                else op.actor_id
            )
            _require(
                getattr(initial[key], field) == op.expected,
                "Delta actor pre-value differs from stage input",
            )
            expected[key] = expected[key].model_copy(update={field: op.value})
        elif isinstance(op, delta.PositionUpdate):
            _require(
                op.expected == initial[op.actor_id].cell
                and op.combat_id == snapshot.combat_state.combat_id
                and op.scene_id == snapshot.scene_state.scene_id,
                "Delta position binding differs from stage input",
            )
            expected[op.actor_id] = expected[op.actor_id].model_copy(update={"cell": op.value})
        elif isinstance(op, delta.TurnUpdate):
            _require(
                op.expected == first_combat.turn
                and op.combat_id == snapshot.combat_state.combat_id,
                "Delta turn pre-value differs",
            )
            final_combat = final_combat.model_copy(update={"turn": op.value})
        elif isinstance(op, delta.DamageSequenceUpdate):
            _require(
                op.expected == first_combat.damage_instance_sequence,
                "Delta damage sequence pre-value differs",
            )
            final_combat = final_combat.model_copy(update={"damage_instance_sequence": op.value})
        elif isinstance(op, delta.ProcessedDamageUpdate):
            _require(
                op.expected == first_combat.processed_zero_hp_damage_instances,
                "Delta completed instances pre-value differs",
            )
            final_combat = final_combat.model_copy(
                update={"processed_zero_hp_damage_instances": op.value}
            )
        elif isinstance(op, delta.DeathLedgerUpdate):
            _require(
                op.expected_dead_ids == first_combat.dead_ids
                and op.expected_records == tuple(snapshot.combat_state.deaths_recorded)
                and op.combat_id == snapshot.combat_state.combat_id,
                "Delta death ledger pre-value differs",
            )
            final_combat = final_combat.model_copy(update={"dead_ids": op.dead_ids})
        else:
            _require(False, "complete stage profile cannot carry this Delta operation")
    _require(
        expected == actors and final_combat == combat,
        "final stage projection differs from typed Delta",
    )


def verify_stage_result(
    facts: EventStageFacts,
    request: "RuleEvaluationRequest",
    events: tuple[CombatEvent, ...],
    state_delta: delta.StateDelta | None,
) -> None:
    """Verify audited stage evidence against fixed input, proposed events and final Delta.

    Check participant coverage and stage/reference ordering, replay the supported actor
    and combat fact transitions, and validate event-specific semantics and movement
    geometry. Compare the final projection with guarded Delta operations. Uncovered
    metadata is handled explicitly; this check neither executes the action nor redraws
    RNG.
    """
    if facts.coverage == "uncovered":
        return
    from dnd5e_engine.evaluation_contracts import CombatIntentPayload

    snapshot, payload = request.state_snapshot, request.payload
    _require(
        isinstance(snapshot, CombatSnapshot) and isinstance(payload, CombatIntentPayload),
        "complete stages require a combat intent",
    )
    assert isinstance(snapshot, CombatSnapshot)
    assert isinstance(payload, CombatIntentPayload)
    intent = payload.intent_type
    _require(
        request.operation_kind == "combat.intent"
        and intent in {"attack", "pass", "move"}
        and stage_profile_reason(snapshot) is None,
        "request is outside the complete stage profile",
    )
    facts.verify_input(snapshot, events)
    _require(
        facts.participant_ids == tuple(sorted(a.entity_id for a in snapshot.character_states)),
        "complete projection must reference every supplied participant",
    )
    initial = _initial(snapshot)
    actors = dict(initial)
    sources = {a.entity_id: a for a in snapshot.character_states}
    modes = {
        key: CombatantMovementModes.model_validate(a.movement_modes.model_dump())
        for key, a in sources.items()
    }
    grid = GridTopology(GridScene(**snapshot.scene_state.grid.model_dump()))
    occupied = {
        snapshot.combat_state.actor_zone[a.entity_id]: a
        for a in snapshot.character_states
        if a.entity_id != request.actor_id
        and a.is_alive
        and a.entity_id not in snapshot.combat_state.dead_ids
    }
    allied = (
        snapshot.combat_state.party_ids
        if request.actor_id in snapshot.combat_state.party_ids
        else snapshot.combat_state.encounter_ids
    )
    combat = combat_facts(snapshot.combat_state)
    steps: dict[int, StageActorFacts] = {}
    used_steps: set[int] = set()
    executions = 0
    rolls = 0
    last_save: DeathSaveRolled | None = None
    counts = Counter(stage.kind for stage in facts.stages)
    for stage in facts.stages:
        prior_combat = combat
        old = {a.actor_id: actors[a.actor_id] for a in stage.actor_updates}
        actors.update({a.actor_id: a for a in stage.actor_updates})
        if stage.combat_update is not None:
            combat = stage.combat_update.apply(combat)
            change = stage.combat_update
            event = events[stage.event_cursor - 1] if stage.kind == "event" else None
            if change.damage_instance_sequence is not None:
                _require(
                    isinstance(event, DamageApplied)
                    and change.damage_instance_sequence
                    == prior_combat.damage_instance_sequence + 1,
                    "damage sequence changed outside its actual packet boundary",
                )
            _require(
                change.turn is None or stage.kind in {"turn_advanced", "budget_reset"},
                "turn facts changed outside an audited boundary",
            )
            if change.turn is not None:
                allowed_turn_fields = (
                    {"current_turn_index", "round_number", "last_ended_turn"}
                    if stage.kind == "turn_advanced"
                    else {"turn_serial"}
                )
                changed_turn_fields = {
                    name
                    for name in type(combat.turn).model_fields
                    if getattr(combat.turn, name) != getattr(prior_combat.turn, name)
                }
                _require(
                    not changed_turn_fields - allowed_turn_fields,
                    "turn boundary contains unrelated clock/lifecycle mutations",
                )
                _require(
                    combat.turn.round_number - prior_combat.turn.round_number in {0, 1},
                    "turn advance skipped a round boundary",
                )
            _require(
                not change.dead_ids_removed and not change.processed_instances_removed,
                "complete profile cannot remove death/damage-instance facts",
            )
            if change.dead_ids_added:
                _require(
                    isinstance(event, Death) and change.dead_ids_added == {event.target_id},
                    "dead membership changed outside its actual Death fold",
                )
            allowed_instances = {
                (e.target_id, e.damage_instance_id)
                for e in events[: stage.event_cursor]
                if isinstance(e, DamageApplied)
            }
            _require(
                change.processed_instances_added <= allowed_instances,
                "completed instance reference is outside actual local damage events",
            )
            if change.processed_instances_added:
                _require(
                    stage.kind == "damage_instance_completed"
                    or isinstance(event, (Unconscious, Death)),
                    "completed instance changed outside its actual zero-HP fold",
                )
                _require(
                    all(
                        identity == payload.target_id
                        and actors[identity].hp_current == 0
                        and (
                            event is None
                            or (
                                isinstance(event, (Unconscious, Death))
                                and event.target_id == identity
                            )
                        )
                        for identity, _ in change.processed_instances_added
                    ),
                    "completed instance does not belong to the sampled zero-HP target",
                )
        _require(
            combat.damage_instance_sequence >= prior_combat.damage_instance_sequence,
            "stage damage sequence moves backwards",
        )
        _require(
            combat.turn.turn_serial >= prior_combat.turn.turn_serial
            and combat.turn.round_number >= prior_combat.turn.round_number,
            "stage turn clock moves backwards",
        )
        identities = set(actors) if stage.kind == "input" else set(old)
        if stage.combat_update is not None:
            identities |= stage.combat_update.dead_ids_added | stage.combat_update.dead_ids_removed
        _consistent(sources, modes, actors, combat, identities)
        if stage.kind == "event":
            event = events[stage.event_cursor - 1]
            _event(
                stage,
                event,
                facts,
                actors,
                combat,
                old,
                snapshot,
                request.actor_id,
                payload.target_id,
                intent,
                steps,
                last_save,
            )
            rolls += int(isinstance(event, AttackRolled))
            if isinstance(event, DeathSaveRolled):
                last_save = event
            if isinstance(event, ActorMoved):
                points = set(facts.event_references[stage.event_cursor - 1].stage_sequences[:-1])
                _require(not used_steps & points, "movement stage is referenced more than once")
                used_steps |= points
        elif stage.kind == "attack_execution":
            executions += 1
            _require(
                intent == "attack"
                and stage.event_cursor == 1
                and set(old) <= {request.actor_id}
                and not actors[request.actor_id].budget.action_available,
                "invalid attack execution stage",
            )
            _actor_changes(
                stage,
                old,
                {request.actor_id},
                {"budget"},
                {
                    "action_available",
                    "action_taken_this_turn",
                    "attack_action_engaged",
                    "loading_weapon_fired_this_action",
                    "attacks_remaining",
                    "attack_action_attacks_made",
                },
            )
            _require(stage.combat_update is None, "attack payment cannot mutate combat facts")
        elif stage.kind == "movement_step":
            _require(
                intent == "move"
                and tuple(old) == (request.actor_id,)
                and stage.combat_update is None,
                "invalid moving participant stage",
            )
            previous, current = old[request.actor_id], actors[request.actor_id]
            _actor_changes(
                stage,
                old,
                {request.actor_id},
                {"cell", "movement", "budget"},
                {"movement_remaining"},
            )
            _require(
                grid.is_adjacent(previous.cell, current.cell)
                and grid.edge_distance(previous.cell, current.cell) is not None,
                "movement step is not a real adjacent grid edge",
            )
            _require(
                current.movement.distance_ft - previous.movement.distance_ft == grid.cell_size_ft
                and current.movement.spent_ft > previous.movement.spent_ft,
                "movement step and ledger increments disagree",
            )
            occupant = occupied.get(current.cell)
            cost = step_cost(
                grid.cell_size_ft,
                difficult_terrain=(grid.edge_distance(previous.cell, current.cell) or 0)
                > grid.cell_size_ft,
                creature_space_difficult=bool(
                    occupant
                    and creature_space_is_difficult(
                        occupant.creature_size, allied=occupant.entity_id in allied
                    )
                ),
            )
            _require(
                current.movement.active_mode == previous.movement.active_mode == "walk"
                and current.movement.dash_count == previous.movement.dash_count
                and current.movement.spent_ft - previous.movement.spent_ft == cost
                and previous.budget.movement_remaining - current.budget.movement_remaining == cost,
                "walk step changed mode/Dash or disagrees with its shared edge cost",
            )
            steps[stage.sequence] = current
        elif stage.kind == "budget_reset":
            _require(
                intent in {"attack", "pass"}
                and stage.event_cursor > 0
                and isinstance(events[stage.event_cursor - 1], TurnStarted),
                "budget reset lacks the preceding real turn start",
            )
            owner = snapshot.combat_state.initiative_ids[combat.turn.current_turn_index]
            for update in stage.actor_updates:
                if update.actor_id == owner:
                    _bounded_actor(old[owner], update, {"budget", "movement"})
                else:
                    _bounded_actor(
                        old[update.actor_id], update, {"budget"}, {"sneak_attack_spent_this_turn"}
                    )
            refreshed = actors[owner]
            budget = refreshed.budget
            _require(
                all(
                    getattr(budget, key)
                    for key in ("action_available", "bonus_action_available", "reaction_available")
                ),
                "reset did not refresh ordinary availability",
            )
            _require(
                not any(
                    getattr(budget, key)
                    for key in (
                        "action_taken_this_turn",
                        "bonus_action_taken_this_turn",
                        "attack_action_attacks_made",
                        "attack_rolls_made_this_turn",
                        "action_grants_spent",
                        "action_grant_groups_spent",
                        "disengaging_this_turn",
                        "sneak_attack_spent_this_turn",
                        "attack_action_engaged",
                        "light_weapon_swing_slug",
                        "restricted_light_weapon_swing_slug",
                        "offhand_attack_spent",
                        "dodging",
                        "loading_weapon_fired_this_action",
                        "cleave_spent_this_turn",
                        "flurry_strikes_remaining",
                        "extra_actions_remaining",
                        "action_surge_used_this_turn",
                    )
                )
                and budget.attacks_remaining == 1
                and refreshed.movement.active_mode == "walk"
                and refreshed.movement.dash_count == 0,
                "reset retained an expired budget/ledger flag",
            )
            _require(
                combat.turn.turn_serial == prior_combat.turn.turn_serial + 1
                and refreshed.budget.action_available
                and refreshed.budget.reaction_available
                and refreshed.budget.attack_rolls_made_this_turn == 0
                and refreshed.movement.spent_ft == 0
                and refreshed.movement.distance_ft == 0,
                "budget reset sample contradicts reset boundary",
            )
        elif stage.kind == "turn_advanced":
            _actor_changes(stage, old, set(actors), {"budget"}, {"movement_remaining"})
            _require(
                intent in {"attack", "pass"}
                and stage.event_cursor > 0
                and isinstance(events[stage.event_cursor - 1], TurnEnded),
                "turn advance lacks the preceding real turn end",
            )
            _require(
                combat.turn.last_ended_turn == (prior_combat.turn.round_number, request.actor_id),
                "turn advance ended identity disagrees",
            )
        elif stage.kind == "damage_instance_completed":
            _actor_changes(
                stage,
                old,
                {payload.target_id or ""},
                {"death", "conditions", "budget"},
                {"movement_remaining"},
            )
            _require(
                intent == "attack"
                and set(old) <= {payload.target_id}
                and any(isinstance(e, DamageApplied) for e in events[: stage.event_cursor]),
                "damage completion lacks an actual damage instance",
            )
        elif stage.kind == "death_save_completed":
            _require(
                last_save is not None and set(old) <= {last_save.target_id}
                if last_save is not None
                else False,
                "death-save completion lacks an actual roll",
            )
            assert last_save is not None
            _actor_changes(
                stage,
                old,
                {last_save.target_id},
                {"death", "conditions", "hp_current", "budget"},
                {"movement_remaining"},
            )
            saved = actors[last_save.target_id]
            if last_save.outcome == "crit_success":
                _require(
                    saved.hp_current == 1
                    and (
                        saved.death.death_saves is None
                        or (
                            saved.death.death_saves.successes
                            == saved.death.death_saves.failures
                            == 0
                        )
                    ),
                    "completed critical save differs from recovery",
                )
            else:
                _require(
                    saved.death.death_saves is not None
                    and saved.death.death_saves.successes == last_save.running_successes
                    and saved.death.death_saves.failures == last_save.running_failures,
                    "completed death save counters differ from roll output",
                )
                assert saved.death.death_saves is not None
                saves = saved.death.death_saves
                _require(
                    saved.hp_current == 0
                    and saves.is_stable == (saves.successes == 3 and saves.failures < 3)
                    and saved.death.is_alive == (saves.failures < 3),
                    "completed death save outcome contradicts stability/life",
                )
        elif stage.kind == "operation_completed":
            _actor_changes(
                stage,
                old,
                {request.actor_id} if intent == "attack" else set(),
                {"budget"},
                {"action_taken_this_turn", "bonus_action_taken_this_turn"},
            )
            _require(
                stage.combat_update is None, "operation end cannot invent missed combat phases"
            )
    _require(
        (executions, rolls) == ((1, 1) if intent == "attack" else (0, 0)),
        "stage execution/roll boundaries differ from operation",
    )
    _require(used_steps == set(steps), "unreferenced movement step")
    _require(
        counts["turn_advanced"] == sum(isinstance(e, TurnEnded) for e in events)
        and counts["budget_reset"] == sum(isinstance(e, TurnStarted) for e in events)
        and counts["death_save_completed"] == sum(isinstance(e, DeathSaveRolled) for e in events),
        "duplicate or missing turn/save sampling boundary",
    )
    damage_ids = {e.damage_instance_id for e in events if isinstance(e, DamageApplied)}
    _require(
        counts["damage_instance_completed"] == len(damage_ids) and None not in damage_ids,
        "duplicate or missing damage completion boundary",
    )
    if intent == "move":
        _require(
            actors[request.actor_id].cell == payload.target_zone_id
            and len(events) == 1
            and isinstance(events[0], ActorMoved),
            "ordinary movement completion differs from request",
        )
        assert state_delta is not None
        assert isinstance(events[0], ActorMoved)
        positions = [op for op in state_delta.operations if isinstance(op, delta.PositionUpdate)]
        _require(
            len(positions) == 1 and positions[0].path == events[0].path,
            "Delta path differs from actual completed path",
        )
    _require(state_delta is not None, "complete stages require typed Delta")
    assert state_delta is not None
    _final_delta(snapshot, state_delta, actors, combat, initial)
