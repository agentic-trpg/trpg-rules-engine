"""R37 public wire/Python checks, one-call stages and independent replay contexts."""

import asyncio
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from dnd5e_engine.evaluation import evaluate
from dnd5e_engine.evaluation_contracts import (
    CombatIntentPayload,
    RuleEvaluationRequest,
    RuleEvaluationResult,
)
from dnd5e_engine.evaluation_stages import EventStageFacts
from tests.evaluation_support import FOE, HERO, synthetic_loader
from tests.test_event_stage_attack import views
from tests.test_npc_availability import npc_case
from tests.test_stateless_attack import execute, request_and_live
from tests.test_stateless_movement import move_case
from tests.test_stateless_turn import turn_case


def verify(data, request, mode):
    restored = (
        RuleEvaluationResult.model_validate_json(json.dumps(data))
        if mode == "json"
        else RuleEvaluationResult.model_validate(data)
    )
    restored.verify_request(request)


@pytest.mark.parametrize("factory", [request_and_live, move_case, turn_case])
def test_public_evaluate_returns_nonempty_stages_without_a_capture_helper(factory):
    request, _, _ = factory()
    result = asyncio.run(evaluate(request, loader=synthetic_loader()))
    assert result.schema_version == "engine-evaluation/24"
    assert result.stage_facts.schema_version == "engine-stage-facts/2"
    assert result.stage_facts.coverage == "complete"
    assert result.stage_facts.stages
    for restored in (
        RuleEvaluationResult.model_validate(result.model_dump(mode="python")),
        RuleEvaluationResult.model_validate_json(result.model_dump_json()),
    ):
        restored.verify_request(request)
        assert restored == result


@pytest.mark.parametrize("mode", ["python", "json"])
@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "extra",
        "type",
        "old_envelope",
        "old_stages",
        "duplicate_event",
        "event_range",
        "stage_range",
        "duplicate_stage_ref",
        "wrong_participant",
        "wrong_known_actor",
        "wrong_event_entity",
        "phase",
        "contradict_life",
        "contradict_budget",
        "final_mismatch",
        "command",
        "session",
        "binding",
        "version",
    ],
)
def test_result_negative_boundaries_use_real_strict_shapes_and_original_request(mode, fault):
    request, _, _ = request_and_live(ac=1)
    result = execute(request)
    data = result.model_dump(mode=mode)
    facts = data["stage_facts"]
    stages = facts["stages"]
    boundary = next(s for s in stages if s["kind"] == "attack_execution")
    damage_index = next(
        i for i, e in enumerate(data["proposed_events"]) if e["type"] == "damage_applied"
    )
    damage = stages[facts["event_references"][damage_index]["stage_sequences"][-1]]
    if fault == "missing":
        del data["stage_facts"]
    elif fault == "extra":
        facts["world_complete"] = True
    elif fault == "type":
        boundary["event_cursor"] = True
    elif fault == "old_envelope":
        data["schema_version"] = "engine-evaluation/23"
    elif fault == "old_stages":
        facts["schema_version"] = "engine-stage-facts/1"
    elif fault == "duplicate_event":
        facts["event_references"] += facts["event_references"][:1]
    elif fault == "event_range":
        facts["event_references"][-1]["event_index"] = 999
    elif fault in {"stage_range", "duplicate_stage_ref"}:
        ref = facts["event_references"][0]
        ref["stage_sequences"] = type(ref["stage_sequences"])(
            [999] if fault == "stage_range" else [1, 1]
        )
    elif fault == "wrong_participant":
        boundary["actor_updates"][0]["actor_id"] = "absent:actor"
    elif fault == "wrong_known_actor":
        boundary["actor_updates"][0]["actor_id"] = FOE
    elif fault == "wrong_event_entity":
        next(e for e in data["proposed_events"] if e["type"] == "attack_rolled")["target_id"] = HERO
    elif fault == "phase":
        boundary["sample_point"] = "after_budget_reset"
    elif fault == "contradict_life":
        damage["actor_updates"][0]["death"]["is_alive"] = False
    elif fault == "contradict_budget":
        boundary["actor_updates"][0]["budget"]["movement_remaining"] += 1
    elif fault == "final_mismatch":
        # A valid, in-range immutable projection, wrong only against the final Delta.
        projection = views(request.state_snapshot, result.stage_facts)[-1][0][HERO].model_dump(
            mode=mode
        )
        projection["hp_current"] -= 1
        stages[-1]["actor_updates"] = type(stages[-1]["actor_updates"])([projection])
    elif fault == "command":
        data["command_id"] = "other:command"
    elif fault == "session":
        data["session_id"] = "other:session"
    elif fault == "binding":
        data["input_ruleset_binding"]["evaluator_version"] = "dnd5e-evaluation/36"
    else:
        data["input_world_version"] += 1
    with pytest.raises((ValidationError, ValueError)):
        verify(data, request, mode)


@pytest.mark.parametrize("mode", ["python", "json"])
@pytest.mark.parametrize("fault", ["non_adjacent", "path", "summary_cost", "reset", "set_overlap"])
def test_movement_turn_and_set_edit_contradictions_are_refused(mode, fault):
    request, _, _ = (
        turn_case() if fault in {"reset", "set_overlap"} else move_case(destination="4,0")
    )
    result = execute(request)
    data = result.model_dump(mode=mode)
    stages = data["stage_facts"]["stages"]
    if fault == "non_adjacent":
        next(s for s in stages if s["kind"] == "movement_step")["actor_updates"][0]["cell"] = "5,2"
    elif fault == "path":
        path = data["proposed_events"][0]["path"]
        data["proposed_events"][0]["path"] = type(path)([path[0], path[-1]])
    elif fault == "summary_cost":
        data["proposed_events"][0]["movement_cost_ft"] += 5
    else:
        reset = next(s for s in stages if s["kind"] == "budget_reset")
        if fault == "reset":
            projection = views(request.state_snapshot, result.stage_facts)[reset["sequence"]][0][
                FOE
            ].model_dump(mode=mode)
            projection["budget"]["action_available"] = False
            reset["actor_updates"] = type(reset["actor_updates"])([projection])
        else:
            change = reset["combat_update"]
            change["dead_ids_added"] = type(change["dead_ids_added"])([HERO])
            change["dead_ids_removed"] = type(change["dead_ids_removed"])([HERO])
    with pytest.raises((ValidationError, ValueError)):
        verify(data, request, mode)


@pytest.mark.parametrize("status", ["rejected", "needs_choice", "unsupported"])
def test_nonaccepted_evaluate_has_no_stage_records_and_cannot_accept_them(status):
    request, _, _ = request_and_live()
    payload = (
        CombatIntentPayload(intent_type="dodge")
        if status == "unsupported"
        else request.payload.model_copy(
            update={"weapon_id": None} if status == "needs_choice" else {"target_id": "missing"}
        )
    )
    request = request.model_copy(update={"payload": payload})
    result = execute(request)
    assert result.status == status
    assert result.stage_facts is None
    altered = result.model_copy(
        update={"stage_facts": EventStageFacts.uncovered("operation_not_in_stage_profile")}
    )
    with pytest.raises(ValidationError, match="non-accepted"):
        RuleEvaluationResult.model_validate(altered)


def test_old_request_and_old_evaluator_binding_fail_without_execution(monkeypatch):
    request, _, _ = request_and_live()
    old = request.model_dump(mode="json")
    old["schema_version"] = "engine-evaluation/23"
    with pytest.raises(ValidationError):
        RuleEvaluationRequest.model_validate_json(json.dumps(old))
    from dnd5e_engine import evaluation
    from dnd5e_engine.evaluation_ruleset import RulesetBindingError

    monkeypatch.setattr(
        evaluation, "execute_attack", lambda *a, **k: pytest.fail("old binding executed")
    )
    old_request = request.model_copy(
        update={
            "ruleset_binding": request.ruleset_binding.model_copy(
                update={"evaluator_version": "dnd5e-evaluation/36"}
            )
        }
    )
    with pytest.raises(RulesetBindingError):
        execute(old_request)


def test_serialized_request_and_result_reproduce_in_fresh_hash_seed_process():
    request, _, _ = request_and_live(ac=1)
    expected = execute(request)
    script = """
import asyncio, sys
sys.path.insert(0, sys.argv[1])
from tests.evaluation_support import synthetic_loader
from dnd5e_engine.evaluation import evaluate
from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest, RuleEvaluationResult
request = RuleEvaluationRequest.model_validate_json(sys.stdin.read())
result = asyncio.run(evaluate(request, loader=synthetic_loader()))
RuleEvaluationResult.model_validate_json(result.model_dump_json()).verify_request(request)
print(result.model_dump_json())
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(Path(__file__).parents[1])],
        input=request.model_dump_json(),
        env={**os.environ, "PYTHONHASHSEED": "311"},
        text=True,
        capture_output=True,
        check=True,
    )
    restored = RuleEvaluationResult.model_validate_json(child.stdout)
    restored.verify_request(request)
    assert restored == expected


def test_interleaved_sessions_requests_rng_and_frozen_stage_values_are_isolated():
    first, _, _ = request_and_live(ac=1)
    second = first.model_copy(
        update={
            "session_id": "session:other",
            "command_id": "command:other",
            "state_snapshot": first.state_snapshot.model_copy(
                update={"session_id": "session:other"}
            ),
        }
    )
    originals = deepcopy((first, second))
    expected = (execute(first), execute(second))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda i: execute((first, second)[i % 2]), range(12)))
    assert results == [expected[i % 2] for i in range(12)]
    assert (first, second) == originals
    assert expected[0].stage_facts == expected[1].stage_facts
    update = next(
        s.combat_update for s in expected[0].stage_facts.stages if s.combat_update is not None
    )
    with pytest.raises(AttributeError):
        update.dead_ids_added.add(HERO)


@pytest.mark.parametrize("mode", ["validation", "serialization"])
def test_exported_schema_includes_required_stages_without_snapshot_availability_version_churn(mode):
    from dnd5e_engine.evaluation_availability import ActionAvailabilityRequest
    from dnd5e_engine.evaluation_state import CombatSnapshot

    schema = RuleEvaluationResult.model_json_schema(mode=mode)
    assert "stage_facts" in schema["required"]
    assert schema["properties"]["schema_version"]["const"] == "engine-evaluation/24"
    assert (
        schema["$defs"]["EventStageFacts"]["properties"]["schema_version"]["const"]
        == "engine-stage-facts/2"
    )
    assert schema["$defs"]["StageCombatUpdate"]["additionalProperties"] is False
    assert (
        CombatSnapshot.model_json_schema()["properties"]["snapshot_schema_version"]["const"]
        == "engine-snapshot/17"
    )
    assert (
        ActionAvailabilityRequest.model_json_schema()["properties"]["schema_version"]["const"]
        == "engine-availability/13"
    )


def test_existing_attack_with_unaligned_input_budget_keeps_rule_outcome_and_reports_gap():
    request, _, _ = request_and_live(ac=1)
    actors = tuple(
        a.model_copy(update={"movement_remaining": 0}) if a.entity_id == FOE else a
        for a in request.state_snapshot.character_states
    )
    request = request.model_copy(
        update={
            "state_snapshot": request.state_snapshot.model_copy(update={"character_states": actors})
        }
    )
    result = execute(request)
    assert result.status == "accepted"
    assert result.stage_facts.reason == "input_budget_ledger_not_aligned"
    assert result.stage_facts.coverage == "uncovered"


@pytest.mark.parametrize("mode", ["python", "json"])
@pytest.mark.parametrize(
    "fault",
    [
        "transient_walk_hp",
        "transient_walk_budget",
        "transient_walk_dash",
        "transient_walk_mode",
        "transient_walk_cost",
        "early_damage_sequence",
        "transient_turn_end",
        "transient_attack_budget",
        "duplicate_boundary",
        "wrong_combat",
        "duplicate_delta",
        "false_stable",
    ],
)
def test_independently_reviewed_stage_and_delta_defects(mode, fault):
    if fault.startswith("transient_walk"):
        request, _, _ = move_case(destination="4,0")
    elif fault == "transient_turn_end":
        request, _, _ = turn_case()
    elif fault == "false_stable":
        request, _, _ = turn_case(npc=True, dying=True, seed=0)
    else:
        request, _, _ = request_and_live(ac=1)
    data = execute(request).model_dump(mode=mode)
    if fault == "transient_walk_hp":
        step = next(s for s in data["stage_facts"]["stages"] if s["kind"] == "movement_step")
        step["actor_updates"][0]["hp_current"] -= 1
    elif fault == "transient_walk_budget":
        step = next(s for s in data["stage_facts"]["stages"] if s["kind"] == "movement_step")
        step["actor_updates"][0]["budget"]["bonus_action_available"] = False
    elif fault in {"transient_walk_dash", "transient_walk_mode"}:
        step = next(s for s in data["stage_facts"]["stages"] if s["kind"] == "movement_step")
        actor = step["actor_updates"][0]
        if fault == "transient_walk_dash":
            actor["movement"]["dash_count"] += 1
            actor["budget"]["movement_remaining"] += 30
        else:
            actor["movement"]["active_mode"] = "crawl"
    elif fault == "transient_walk_cost":
        step = next(s for s in data["stage_facts"]["stages"] if s["kind"] == "movement_step")
        step["actor_updates"][0]["movement"]["spent_ft"] -= 1
        step["actor_updates"][0]["budget"]["movement_remaining"] += 1
    elif fault == "transient_turn_end":
        next(s for s in data["stage_facts"]["stages"] if s["kind"] == "turn_advanced")[
            "combat_update"
        ]["turn"]["ended"] = True
    elif fault == "early_damage_sequence":
        stages = data["stage_facts"]["stages"]
        damage = next(
            s
            for s in stages
            if s["kind"] == "event"
            and data["proposed_events"][s["event_cursor"] - 1]["type"] == "damage_applied"
        )
        roll = next(
            s
            for s in stages
            if s["kind"] == "event"
            and data["proposed_events"][s["event_cursor"] - 1]["type"] == "attack_rolled"
        )
        roll["combat_update"], damage["combat_update"] = damage["combat_update"], None
    elif fault == "transient_attack_budget":
        step = next(s for s in data["stage_facts"]["stages"] if s["kind"] == "attack_execution")
        step["actor_updates"][0]["budget"]["bonus_action_available"] = False
    elif fault == "duplicate_boundary":
        facts = data["stage_facts"]
        stages = facts["stages"]
        completed = next(s for s in stages if s["kind"] == "damage_instance_completed")
        insertion = completed["sequence"] + 1
        facts["stages"] = type(stages)(
            (*stages[:insertion], deepcopy(completed), *stages[insertion:])
        )
        for index, stage in enumerate(facts["stages"]):
            stage["sequence"] = index
        for ref in facts["event_references"]:
            ref["stage_sequences"] = type(ref["stage_sequences"])(
                i + int(i >= insertion) for i in ref["stage_sequences"]
            )
        facts["final_stage"] += 1
    elif fault == "wrong_combat":
        next(
            o
            for o in data["state_delta"]["operations"]
            if o["kind"] == "combat.damage_sequence_update"
        )["combat_id"] = "combat:other"
    elif fault == "duplicate_delta":
        operations = data["state_delta"]["operations"]
        operation = next(o for o in operations if o["kind"] == "actor.hp_delta")
        data["state_delta"]["operations"] += type(operations)([deepcopy(operation)])
    else:
        completed = next(
            s for s in data["stage_facts"]["stages"] if s["kind"] == "death_save_completed"
        )
        completed["actor_updates"][0]["death"]["death_saves"]["is_stable"] = True
        next(
            o for o in data["state_delta"]["operations"] if o["kind"] == "actor.death_state_update"
        )["value"]["death_saves"]["is_stable"] = True
    with pytest.raises((ValidationError, ValueError)):
        verify(data, request, mode)


@pytest.mark.parametrize("mode", ["python", "json"])
def test_processed_instance_cannot_move_to_later_unrelated_event(mode):
    request, _, _, loader = npc_case(seed=0, hero_hp=1, hero_max=100)
    result = execute(request, loader)
    result.verify_request(request)
    data = result.model_dump(mode=mode)
    stages = data["stage_facts"]["stages"]
    source = next(
        s for s in stages if s["combat_update"] and s["combat_update"]["processed_instances_added"]
    )
    destination = next(
        s
        for s in stages
        if s["kind"] == "event"
        and data["proposed_events"][s["event_cursor"] - 1]["type"] == "turn_ended"
    )
    destination["combat_update"] = deepcopy(source["combat_update"])
    source["combat_update"] = None
    with pytest.raises((ValidationError, ValueError)):
        verify(data, request, mode)


@pytest.mark.parametrize("mode", ["python", "json"])
def test_complete_stage_cannot_introduce_external_condition_source(mode):
    request, _, _, loader = npc_case(seed=0, hero_hp=1, hero_max=100)
    data = execute(request, loader).model_dump(mode=mode)
    for stage in data["stage_facts"]["stages"]:
        for actor in stage["actor_updates"]:
            for condition in actor["conditions"]:
                condition["source_entity_id"] = "actor:absent"
    for op in data["state_delta"]["operations"]:
        if op["kind"] == "actor.conditions_update":
            for condition in op["value"]:
                condition["source_entity_id"] = "actor:absent"
    with pytest.raises((ValidationError, ValueError)):
        verify(data, request, mode)
