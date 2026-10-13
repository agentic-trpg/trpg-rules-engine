"""R36 true turn boundaries and traversed cells, with original rule comparisons."""

import asyncio
from copy import deepcopy

import pytest

from dnd5e_engine import evaluation
from dnd5e_engine.evaluation_computation import CombatComputation
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd5e_engine.evaluation_state import MovementLedgerState
from tests.evaluation_support import FOE, HERO
from tests.test_event_stage_attack import views
from tests.test_stateless_attack import apply_delta, execute
from tests.test_stateless_effect_lifecycle import effect_case
from tests.test_stateless_hold_turn import hold_case
from tests.test_stateless_movement import move_case, parity
from tests.test_stateless_opportunity import opportunity_case
from tests.test_stateless_shield import shield_case
from tests.test_stateless_turn import assert_parity, turn_case


def capture(monkeypatch, request, loader=None):
    name = "evaluate_turn" if request.payload.intent_type == "pass" else "evaluate_movement"
    original = getattr(evaluation, name)
    captured = []

    def run(*args, **kwargs):
        outcome = original(*args, **kwargs)
        captured.append(outcome)
        return outcome

    monkeypatch.setattr(evaluation, name, run)
    result = execute(request, loader)
    assert len(captured) == 1
    return result, captured[0].get("stage_facts")


@pytest.mark.parametrize("npc", [False, True])
def test_pass_preserves_pre_reset_turn_started_and_real_round_boundary(monkeypatch, npc):
    request, _, _ = turn_case(npc=npc)
    next_id = HERO if npc else FOE
    snapshot = request.state_snapshot
    actors = tuple(
        a.model_copy(
            update={
                "action_available": False,
                "attacks_remaining": 0,
                "reaction_available": False,
                "movement_remaining": 10,
            }
        )
        if a.entity_id == next_id
        else a
        for a in snapshot.character_states
    )
    state = snapshot.combat_state.model_copy(
        update={
            "movement_ledgers": {
                **snapshot.combat_state.movement_ledgers,
                next_id: MovementLedgerState(
                    spent_ft=20, distance_ft=20, active_mode="walk", dash_count=0
                ),
            }
        }
    )
    request = request.model_copy(
        update={
            "state_snapshot": snapshot.model_copy(
                update={
                    "character_states": actors,
                    "combat_state": state,
                }
            )
        }
    )
    original = deepcopy(request)
    result, facts = capture(monkeypatch, request)
    assert result.status == "accepted"
    assert facts.coverage == "complete"
    samples = views(request.state_snapshot, facts)
    for index, event in enumerate(result.proposed_events):
        seq = facts.event_references[index].stage_sequences[-1]
        actors_at, combat = samples[seq]
        if event.type == "turn_ended":
            assert combat.turn.current_turn_index == int(npc)
            assert combat.turn.round_number == 1
        elif event.type == "turn_started":
            assert combat.turn.current_turn_index == int(not npc)
            assert combat.turn.round_number == 1 + int(npc)
            assert not actors_at[next_id].budget.action_available
            assert not actors_at[next_id].budget.reaction_available
            assert actors_at[next_id].movement.spent_ft == 20
    reset = next(s for s in facts.stages if s.kind == "budget_reset")
    refreshed = samples[reset.sequence][0][next_id]
    assert refreshed.budget.action_available
    assert refreshed.budget.reaction_available
    assert refreshed.budget.movement_remaining == 30
    assert refreshed.movement.spent_ft == 0
    assert samples[reset.sequence][1].turn.turn_serial == state.turn_serial + 1
    assert any(e.type == "round_started" for e in result.proposed_events) == npc
    final = apply_delta(request.state_snapshot, result)
    assert final.combat_state.current_turn_index == int(not npc)
    assert request == original


@pytest.mark.parametrize("seed", [31, 5, 0, 1])
def test_ordinary_dying_pass_records_completed_counters_without_altering_parity(monkeypatch, seed):
    request, handle, live = turn_case(npc=True, dying=True, seed=seed)
    result, facts = capture(monkeypatch, request)
    assert facts.coverage == "complete"
    finished = next(s for s in facts.stages if s.kind == "death_save_completed")
    final = assert_parity(request, handle, live, result)
    actor = next(a for a in final.character_states if a.entity_id == HERO)
    saved = views(request.state_snapshot, facts)[finished.sequence][0][HERO]
    assert saved.hp_current == actor.hp_current
    assert saved.death.death_saves == actor.death_saves


@pytest.mark.parametrize("terrain", [(), ("1,0", "1,1")])
def test_walk_records_only_actual_intermediate_cells_bound_to_one_summary(monkeypatch, terrain):
    request, handle, live = move_case(destination="4,0", terrain=terrain)
    original = deepcopy(request)
    result, facts = capture(monkeypatch, request)
    assert result.status == "accepted"
    assert facts.coverage == "complete"
    event = next(e for e in result.proposed_events if e.type == "actor_moved")
    steps = [s for s in facts.stages if s.kind == "movement_step"]
    samples = views(request.state_snapshot, facts)
    assert [samples[s.sequence][0][HERO].cell for s in steps] == list(event.path[1:])
    assert all([a.actor_id for a in s.actor_updates] == [HERO] for s in steps)
    assert all(samples[s.sequence][0][FOE].cell == "10,3" for s in steps)
    assert len(result.proposed_events) == 1
    assert facts.event_references[0].stage_sequences == (
        *(s.sequence for s in steps),
        len(facts.stages) - 2,
    )
    # An earlier immutable cell/cost sample cannot be replaced by the final cell.
    assert samples[steps[0].sequence][0][HERO].cell != event.to_zone
    assert (
        samples[steps[0].sequence][0][HERO].movement.spent_ft
        < samples[-1][0][HERO].movement.spent_ft
    )
    final = parity(request, handle, live, result)
    assert samples[-1][0][HERO].cell == final.combat_state.actor_zone[HERO] == event.to_zone
    assert samples[-1][0][HERO].movement.spent_ft == event.movement_cost_ft
    assert request == original


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_failed_walk_discards_stages_without_input_or_publication_side_effects(
    monkeypatch, failure
):
    request, _, live = move_case(destination="4,0")
    original = deepcopy(request)
    sample = CombatComputation.sample_stage

    def interrupt(self, kind, actor_ids=None):
        sample(self, kind, actor_ids)
        if kind == "movement_step":
            raise failure("after actual intermediate step")

    monkeypatch.setattr(CombatComputation, "sample_stage", interrupt)
    with pytest.raises(failure, match="intermediate step"):
        execute(request)
    assert request == original
    assert live.event_log == []
    assert live.actor_zone[HERO] == "0,0"


@pytest.mark.parametrize("kind", ["dash", "disengage"])
def test_dash_and_disengage_keep_mechanics_without_claiming_walk_coverage(monkeypatch, kind):
    request, handle, live = move_case(kind=kind)
    result, facts = capture(monkeypatch, request)
    assert result.status == "accepted"
    assert facts.coverage == "uncovered"
    assert facts.reason == "operation_not_in_stage_profile"
    parity(request, handle, live, result)


def test_opportunity_path_still_executes_and_is_explicitly_uncovered(monkeypatch):
    request, _, _ = opportunity_case()
    result, facts = capture(monkeypatch, request)
    assert result.status == "accepted"
    assert any(
        e.type == "attack_rolled" and e.is_opportunity_attack for e in result.proposed_events
    )
    assert facts.coverage == "uncovered"
    assert facts.reason == "complex_effects_or_reactions"


def test_rejected_movement_carries_no_private_stage_records(monkeypatch):
    request, _, _ = move_case(destination="0,0")
    result, facts = capture(monkeypatch, request)
    assert result.status == "rejected"
    assert facts is None


@pytest.mark.parametrize("factory", [effect_case, hold_case, shield_case])
def test_effect_and_reaction_pass_keep_rules_without_base_complete_claim(monkeypatch, factory):
    request, _, _, loader = factory()
    request = request.model_copy(update={"payload": CombatIntentPayload(intent_type="pass")})
    result, facts = capture(monkeypatch, request, loader)
    assert result.status == "accepted", result.error
    assert facts.coverage == "uncovered"
    assert facts.reason == "complex_effects_or_reactions"
    assert not facts.stages
