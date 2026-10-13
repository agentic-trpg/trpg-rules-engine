"""Measured payload/projection counts; no latency or asymptotic claims."""

import json
import subprocess
import sys
from pathlib import Path

from dnd5e_engine import evaluation, evaluation_stage_capture, evaluation_stage_validation
from dnd5e_engine.evaluation_computation import CombatComputation
from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest, RuleEvaluationResult
from dnd5e_engine.evaluation_stages import actor_facts
from dnd5e_engine.evaluation_state import MechanicalGrid
from tests.evaluation_support import FOE
from tests.test_stateless_attack import execute, request_and_live
from tests.test_stateless_movement import move_case


def extra_participants(request, count):
    snapshot, state = request.state_snapshot, request.state_snapshot.combat_state
    foe = next(a for a in snapshot.character_states if a.entity_id == FOE)
    extra = tuple(foe.model_copy(update={"entity_id": f"npc:extra:{i}"}) for i in range(count))
    grid = snapshot.scene_state.grid.model_copy(update={"width": count + 12})
    state = state.model_copy(
        update={
            "initiative_ids": type(state.initiative_ids)(
                (*state.initiative_ids, *(a.entity_id for a in extra))
            ),
            "encounter_ids": state.encounter_ids | {a.entity_id for a in extra},
            "xp_value_by_entity": {
                **state.xp_value_by_entity,
                **{a.entity_id: state.xp_value_by_entity[FOE] for a in extra},
            },
            "actor_zone": {
                **state.actor_zone,
                **{a.entity_id: f"{i + 3},2" for i, a in enumerate(extra)},
            },
            "movement_ledgers": {
                **state.movement_ledgers,
                **{a.entity_id: state.movement_ledgers[FOE] for a in extra},
            },
        }
    )
    return request.model_copy(
        update={
            "state_snapshot": snapshot.model_copy(
                update={
                    "character_states": (*snapshot.character_states, *extra),
                    "combat_state": state,
                    "scene_state": snapshot.scene_state.model_copy(update={"grid": grid}),
                }
            )
        }
    )


def measure(monkeypatch, request):
    counts = dict(
        candidate_assemblies=0,
        capture_actor_projections=0,
        validation_actor_projections=0,
        grid_projections=0,
        request_deepcopies=0,
    )
    candidate = CombatComputation.candidate_state
    grid_dump = MechanicalGrid.model_dump
    deep_copy = evaluation.copy.deepcopy

    def candidate_count(self):
        counts["candidate_assemblies"] += 1
        return candidate(self)

    def grid_count(self, *args, **kwargs):
        counts["grid_projections"] += 1
        return grid_dump(self, *args, **kwargs)

    def copy_count(value, *args, **kwargs):
        counts["request_deepcopies"] += int(isinstance(value, RuleEvaluationRequest))
        return deep_copy(value, *args, **kwargs)

    def capture_count(*args, **kwargs):
        counts["capture_actor_projections"] += 1
        return actor_facts(*args, **kwargs)

    def validation_count(*args, **kwargs):
        counts["validation_actor_projections"] += 1
        return actor_facts(*args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(CombatComputation, "candidate_state", candidate_count)
        context.setattr(MechanicalGrid, "model_dump", grid_count)
        context.setattr(evaluation.copy, "deepcopy", copy_count)
        context.setattr(evaluation_stage_capture, "actor_facts", capture_count)
        context.setattr(evaluation_stage_validation, "actor_facts", validation_count)
        result = execute(request)
        staged = dict(counts)
        for key in counts:
            counts[key] = 0
        context.setattr(CombatComputation, "enable_stages", lambda self: None)
        baseline = execute(request)
        unstaged = dict(counts)
    assert result.status == baseline.status == "accepted"
    assert result.state_delta == baseline.state_delta
    assert result.proposed_events == baseline.proposed_events
    assert result.rng_transition == baseline.rng_transition
    assert staged["candidate_assemblies"] == unstaged["candidate_assemblies"]
    assert staged["request_deepcopies"] == unstaged["request_deepcopies"] == 1
    assert staged["grid_projections"] <= unstaged["grid_projections"] + 3
    facts = result.stage_facts
    return dict(
        actors=len(request.state_snapshot.character_states),
        events=len(result.proposed_events),
        stages=len(facts.stages),
        retained_actor_updates=sum(len(s.actor_updates) for s in facts.stages),
        request_bytes=len(request.model_dump_json().encode()),
        result_bytes=len(result.model_dump_json().encode()),
        stage_bytes=len(facts.model_dump_json().encode()),
        staged=staged,
        baseline=unstaged,
    ), result


def test_payload_and_actual_copy_measurements(monkeypatch):
    rows = {}
    for kind, factory in (
        ("attack", request_and_live),
        ("walk", lambda: move_case(destination="4,0")),
    ):
        request, _, _ = factory()
        for count in (0, 20):
            row, result = measure(monkeypatch, extra_participants(request, count))
            rows[f"{kind}_{row['actors']}_actors"] = row
            assert result.stage_facts.coverage == "complete"
            assert row["retained_actor_updates"] <= 5
            assert row["staged"]["validation_actor_projections"] == row["actors"]
            assert "event_log" not in result.model_dump_json()
    # Existing current dedup facts are input references, never repeated stage history.
    request, _, _ = request_and_live(ac=1)
    short, _ = measure(monkeypatch, request)
    state = request.state_snapshot.combat_state.model_copy(
        update={
            "processed_zero_hp_damage_instances": {(FOE, f"old-instance:{i}") for i in range(1000)},
            "damage_instance_sequence": 1000,
        }
    )
    request = request.model_copy(
        update={"state_snapshot": request.state_snapshot.model_copy(update={"combat_state": state})}
    )
    long, result = measure(monkeypatch, request)
    assert long["request_bytes"] > short["request_bytes"] + 20000
    assert long["stage_bytes"] < short["stage_bytes"] + 100
    assert "old-instance:" not in result.stage_facts.model_dump_json()
    rows["attack_1000_existing_dedup_facts"] = long
    sys.stdout.write("STAGE_MEASUREMENTS " + json.dumps(rows, sort_keys=True) + "\n")


def test_real_serialized_example_and_schema_export(tmp_path):
    root = Path(__file__).parents[3]
    output, schemas = tmp_path / "result.json", tmp_path / "schemas"
    subprocess.run(
        [
            sys.executable,
            str(root / "examples/event_stage_evaluation.py"),
            str(root / "examples/event_stage_request.json"),
            "--output",
            str(output),
            "--schema-dir",
            str(schemas),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    request = RuleEvaluationRequest.model_validate_json(
        (root / "examples/event_stage_request.json").read_text(encoding="utf-8")
    )
    result = RuleEvaluationResult.model_validate_json(output.read_text(encoding="utf-8"))
    result.verify_request(request)
    assert result.stage_facts.coverage == "complete"
    assert (
        json.loads((schemas / "result.schema.json").read_text(encoding="utf-8"))
        == RuleEvaluationResult.model_json_schema()
    )
