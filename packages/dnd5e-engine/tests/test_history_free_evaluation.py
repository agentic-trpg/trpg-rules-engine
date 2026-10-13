"""History-free contracts, process isolation and explicit predecessor migration."""

import json
import os
import subprocess
import sys
from copy import deepcopy

import pytest
from pydantic import TypeAdapter, ValidationError

from dnd5e_engine import evaluation_context, evaluation_legacy_history, evaluation_migration
from dnd5e_engine.evaluation_availability import (
    ActionAvailabilityRequest,
    query_action_availability,
)
from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest, RuleEvaluationResult
from dnd5e_engine.evaluation_initialization import CombatInitializationCandidateRequest
from dnd5e_engine.evaluation_migration import migrate_snapshot_16
from dnd5e_engine.evaluation_ruleset import RulesetBindingError
from dnd5e_engine.evaluation_snapshot import capture_evaluation_snapshot
from dnd5e_engine.evaluation_state import StateSnapshot
from dnd5e_engine.events import CombatEnded, EffectApplied, IntentSubmitted, ReactionTriggered
from dnd5e_engine.specs import GridScene
from dnd5e_engine.types.effects import ActiveEffect
from tests.evaluation_support import HERO, synthetic_loader
from tests.test_npc_availability import query_for
from tests.test_stateless_attack import execute, request_and_live
from tests.test_stateless_closure import consume, terminal_request


def predecessor(request):
    raw = json.loads(request.state_snapshot.model_dump_json())
    raw["snapshot_schema_version"] = "engine-snapshot/16"
    raw["combat_state"]["event_log"] = [
        IntentSubmitted(actor_id=HERO, intent_type="pass").model_dump(mode="json")
    ]
    raw["combat_state"]["legendary_resistance_applied_event_indices"] = []
    binding = request.ruleset_binding.model_copy(
        update={"evaluator_version": "dnd5e-evaluation/35"}
    )
    return raw, binding


def test_new_serialized_contracts_do_not_contain_audit_or_event_indices():
    request, _, _ = request_and_live()
    for schema in (
        TypeAdapter(StateSnapshot).json_schema(),
        RuleEvaluationRequest.model_json_schema(),
        RuleEvaluationResult.model_json_schema(),
        ActionAvailabilityRequest.model_json_schema(),
        CombatInitializationCandidateRequest.model_json_schema(),
    ):
        encoded = json.dumps(schema)
        assert '"event_log"' not in encoded
        assert '"legendary_resistance_applied_event_indices"' not in encoded
    assert RuleEvaluationRequest.model_validate_json(request.model_dump_json()) == request
    for name, value in (("event_log", []), ("legendary_resistance_applied_event_indices", [])):
        raw = json.loads(request.model_dump_json())
        raw["state_snapshot"]["combat_state"][name] = value
        with pytest.raises(ValidationError, match="extra_forbidden"):
            RuleEvaluationRequest.model_validate_json(json.dumps(raw))


def test_fresh_process_and_interleaved_sessions_reproduce_delta_events_and_rng():
    request, _, _ = request_and_live(ac=1)
    original = deepcopy(request)
    script = """import asyncio, sys
from dnd5e_engine.evaluation import evaluate
from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest
from tests.evaluation_support import synthetic_loader
request = RuleEvaluationRequest.model_validate_json(sys.stdin.read())
print(asyncio.run(evaluate(request, loader=synthetic_loader())).model_dump_json())
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = (
        os.path.dirname(__file__) + os.pathsep + environment.get("PYTHONPATH", "")
    )
    # The package root exposes the test-only immutable loader in the new interpreter.
    environment["PYTHONPATH"] = (
        os.path.dirname(os.path.dirname(__file__)) + os.pathsep + environment["PYTHONPATH"]
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        input=request.model_dump_json(),
        capture_output=True,
        text=True,
        check=True,
        env=environment,
    )
    expected = execute(request)
    assert expected.status == "accepted", expected.error
    assert RuleEvaluationResult.model_validate_json(completed.stdout) == expected
    other = request.model_copy(
        update={
            "session_id": "session:other",
            "command_id": "other",
            "state_snapshot": request.state_snapshot.model_copy(
                update={"session_id": "session:other"}
            ),
        }
    )
    other_before = deepcopy(other)
    other_result = execute(other)
    assert other_result.status == "accepted", other_result.error
    assert other_result.state_delta == expected.state_delta
    assert other_result.proposed_events == expected.proposed_events
    assert other_result.rng_transition == expected.rng_transition
    assert execute(request) == expected
    assert request == original
    assert other == other_before


def test_evaluation_and_availability_never_call_legacy_conversion(monkeypatch):
    request, _, _ = request_and_live(ac=1)

    def forbidden(*args, **kwargs):
        pytest.fail("new request used a Legacy history boundary")

    monkeypatch.setattr(evaluation_context, "execution_context", forbidden)
    monkeypatch.setattr(evaluation_legacy_history, "validate_legacy_history", forbidden)
    monkeypatch.setattr(evaluation_migration, "migrate_snapshot_16", forbidden)
    assert execute(request).status == "accepted"
    assert (
        query_action_availability(query_for(request), loader=synthetic_loader()).status
        == "available"
    )


@pytest.mark.parametrize("field", ["ended", "turn_serial", "damage_instance_sequence", "sap_marks"])
def test_missing_current_facts_rejected_instead_of_defaulted(field):
    request, _, _ = request_and_live()
    raw = json.loads(request.model_dump_json())
    del raw["state_snapshot"]["combat_state"][field]
    with pytest.raises(ValidationError, match="missing"):
        RuleEvaluationRequest.model_validate_json(json.dumps(raw))


def test_old_envelopes_are_rejected_without_implicit_migration():
    request, _, _ = request_and_live()
    raw = json.loads(request.model_dump_json())
    raw["schema_version"] = "engine-evaluation/22"
    with pytest.raises(ValidationError, match="literal_error"):
        RuleEvaluationRequest.model_validate_json(json.dumps(raw))
    raw = json.loads(request.model_dump_json())
    raw["state_snapshot"]["snapshot_schema_version"] = "engine-snapshot/16"
    with pytest.raises(ValidationError, match="literal_error"):
        RuleEvaluationRequest.model_validate_json(json.dumps(raw))
    query = json.loads(query_for(request).model_dump_json())
    query["schema_version"] = "engine-availability/12"
    with pytest.raises(ValidationError, match="literal_error"):
        ActionAvailabilityRequest.model_validate_json(json.dumps(query))
    with pytest.raises(RulesetBindingError):
        execute(
            request.model_copy(
                update={
                    "ruleset_binding": request.ruleset_binding.model_copy(
                        update={"evaluator_version": "dnd5e-evaluation/35"}
                    )
                }
            )
        )


def test_explicit_complete_predecessor_migration_preserves_current_facts_and_result():
    request, _, _ = request_and_live(ac=1)
    raw, binding = predecessor(request)
    before = deepcopy(raw)
    migrated = migrate_snapshot_16(
        json.dumps(raw), source_binding=binding, loader=synthetic_loader()
    )
    assert migrated == request.state_snapshot
    assert execute(request.model_copy(update={"state_snapshot": migrated})) == execute(request)
    assert raw == before


def test_closed_predecessor_preserves_ended_and_repeat_close_rejection():
    request, _, _ = terminal_request()
    closure = execute(request)
    assert closure.status == "accepted", closure.error
    closed, _ = consume(request.state_snapshot, closure)
    request = request.model_copy(update={"state_snapshot": closed})
    raw, binding = predecessor(request)
    raw["combat_state"]["event_log"] = [e.model_dump(mode="json") for e in closure.proposed_events]
    migrated = migrate_snapshot_16(
        json.dumps(raw), source_binding=binding, loader=synthetic_loader()
    )
    assert migrated == closed
    result = execute(request.model_copy(update={"state_snapshot": migrated}))
    assert result.status == "rejected"
    assert result.state_delta is None
    assert result.rng_transition is None
    assert result.proposed_events == ()


@pytest.mark.parametrize("kind", ["trigger", "affected", "provisional", "effect", "nested_extra"])
def test_predecessor_nested_audit_references_and_fields_are_closed(kind):
    request, _, _ = request_and_live()
    raw, binding = predecessor(request)
    if kind in {"effect", "nested_extra"}:
        event = EffectApplied(
            effect=ActiveEffect(
                id="effect:legacy",
                name="Legacy effect",
                origin="cast:source",
                target_id="absent" if kind == "effect" else HERO,
            )
        ).model_dump(mode="json")
        if kind == "nested_extra":
            event["effect"]["unreviewed"] = True
    else:
        event = ReactionTriggered(actor_id=HERO, reaction_name="Shield").model_dump(mode="json")
        if kind == "trigger":
            event["triggering_actor_id"] = "absent"
        elif kind == "affected":
            event["affected_target_id"] = "absent"
        else:
            event["provisional_attack"] = {
                "attacker_id": "absent",
                "target_id": HERO,
                "source_activity_id": "attack:test",
                "natural": 10,
                "roll_total": 15,
                "effective_ac": 10,
                "is_crit": False,
                "is_opportunity_attack": False,
                "provisional_hit": True,
            }
    raw["combat_state"]["event_log"] = [event]
    with pytest.raises(ValueError):
        migrate_snapshot_16(json.dumps(raw), source_binding=binding, loader=synthetic_loader())


@pytest.mark.parametrize(
    "defect",
    [
        "missing_fact",
        "missing_history",
        "ended",
        "duplicate_close",
        "index",
        "event_type",
        "event_extra",
        "actor_reference",
        "binding",
        "data",
        "old_schema",
    ],
)
def test_unproven_or_contradictory_predecessor_is_rejected(defect):
    request, _, _ = request_and_live()
    raw, binding = predecessor(request)
    combat = raw["combat_state"]
    if defect == "missing_fact":
        del combat["turn_serial"]
    elif defect == "missing_history":
        del combat["event_log"]
    elif defect == "ended":
        combat["ended"] = True
    elif defect == "duplicate_close":
        combat["ended"] = True
        combat["event_log"] = [CombatEnded(reason="victory").model_dump(mode="json")] * 2
    elif defect == "index":
        combat["legendary_resistance_applied_event_indices"] = [0]
    elif defect == "event_type":
        combat["event_log"][0]["type"] = "unknown"
    elif defect == "event_extra":
        combat["event_log"][0]["hidden"] = True
    elif defect == "actor_reference":
        combat["event_log"][0]["actor_id"] = "absent"
    elif defect == "binding":
        binding = binding.model_copy(update={"evaluator_version": "dnd5e-evaluation/8"})
    elif defect == "data":
        binding = binding.model_copy(update={"data_revision": "sha256:" + "0" * 64})
    else:
        raw["snapshot_schema_version"] = "engine-snapshot/2"
    with pytest.raises(ValueError):
        migrate_snapshot_16(json.dumps(raw), source_binding=binding, loader=synthetic_loader())


@pytest.mark.parametrize("defect", ["ended", "index"])
def test_legacy_capture_requires_consistent_completed_boundary(defect):
    request, _, live = request_and_live()
    if defect == "ended":
        live.ended = True
    else:
        live.legendary_resistance_applied_event_indices.add(0)
    with pytest.raises(ValueError, match="Legacy"):
        capture_evaluation_snapshot(live, request.state_snapshot, GridScene(width=3, height=3))
