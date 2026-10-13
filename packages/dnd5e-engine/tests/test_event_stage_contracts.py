"""R34 local candidate strictness, references and nested value isolation."""

import os
import subprocess
import sys
from copy import deepcopy

import pytest
from pydantic import ValidationError

from dnd5e_engine.evaluation_stages import (
    EventStageFacts,
    EventStageReference,
    MechanicalStage,
    StageCombatFacts,
    actor_facts,
    combat_change,
    combat_facts,
)
from dnd5e_engine.evaluation_state import ConditionRecord
from dnd5e_engine.events import IntentSubmitted
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import request_and_live


def candidate():
    request, _, _ = request_and_live()
    snapshot = request.state_snapshot
    event = IntentSubmitted(actor_id=HERO, intent_type="attack", target_id=FOE)
    value = EventStageFacts(
        schema_version="engine-stage-facts/2",
        input_reference="request.state_snapshot",
        projection_scope="bounded_participant_mechanics",
        world_coverage="outside_snapshot_unknown",
        coverage="complete",
        reason=None,
        participant_ids=tuple(sorted((HERO, FOE))),
        stages=(
            MechanicalStage(
                sequence=0,
                kind="input",
                sample_point="input_reference",
                event_cursor=0,
                actor_updates=(),
                combat_update=None,
            ),
            MechanicalStage(
                sequence=1,
                kind="event",
                sample_point="after_event_fold",
                event_cursor=1,
                actor_updates=(),
                combat_update=None,
            ),
            MechanicalStage(
                sequence=2,
                kind="operation_completed",
                sample_point="operation_end",
                event_cursor=1,
                actor_updates=(),
                combat_update=None,
            ),
        ),
        event_references=(
            EventStageReference(event_index=0, stage_sequences=(1,), event_role="mechanical_audit"),
        ),
        final_stage=2,
    )
    return snapshot, (event,), value


def test_candidate_roundtrip_input_reference_and_no_world_completeness_claim():
    snapshot, events, value = candidate()
    value.verify_input(snapshot, events)
    assert EventStageFacts.model_validate_json(value.model_dump_json()) == value
    assert value.world_coverage == "outside_snapshot_unknown"
    assert all(not s.actor_updates for s in value.stages)
    encoded = value.model_dump(mode="json")
    assert not {"session_id", "command_id", "event_id", "event_seq", "snapshot"} & encoded.keys()


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "extra",
        "type",
        "duplicate_event",
        "stage_ref",
        "duplicate_stage_ref",
        "phase",
        "final",
        "participant",
    ],
)
def test_strict_structural_refusals_python_and_json(fault):
    _, _, value = candidate()
    data = value.model_dump(mode="json")
    if fault == "missing":
        del data["world_coverage"]
    elif fault == "extra":
        data["event_id"] = "authoritative"
    elif fault == "type":
        data["stages"][1]["event_cursor"] = True
    elif fault == "duplicate_event":
        data["event_references"].append(deepcopy(data["event_references"][0]))
    elif fault == "stage_ref":
        data["event_references"][0]["stage_sequences"] = [99]
    elif fault == "duplicate_stage_ref":
        data["event_references"][0]["stage_sequences"] = [1, 1]
    elif fault == "phase":
        data["stages"][1]["sample_point"] = "after_budget_reset"
    elif fault == "final":
        data["final_stage"] = 0
    else:
        data["participant_ids"] *= 2
    import json

    with pytest.raises(ValidationError):
        EventStageFacts.model_validate_json(json.dumps(data))
    # Match the strict Python tuple shape so this refusal cannot be caused
    # merely by feeding JSON lists to the Python boundary.
    data["participant_ids"] = tuple(data["participant_ids"])
    for stage in data["stages"]:
        stage["actor_updates"] = tuple(stage["actor_updates"])
    data["stages"] = tuple(data["stages"])
    for reference in data["event_references"]:
        reference["stage_sequences"] = tuple(reference["stage_sequences"])
    data["event_references"] = tuple(data["event_references"])
    with pytest.raises(ValidationError):
        EventStageFacts.model_validate(data)


def test_references_are_bound_to_provided_input_and_returned_events():
    snapshot, events, value = candidate()
    altered = value.model_copy(update={"participant_ids": ("absent",)})
    with pytest.raises(ValueError, match="input closure"):
        altered.verify_input(snapshot, events)
    with pytest.raises(ValueError, match="proposed_events"):
        value.verify_input(snapshot, ())


def test_projection_is_nested_immutable_and_detached_from_later_input_mutation():
    snapshot, _, _ = candidate()
    actor = snapshot.character_states[0]
    actor = actor.model_copy(
        update={
            "conditions": [
                ConditionRecord(
                    condition="prone",
                    source_entity_id="implied:prone",
                    scope="session",
                    duration_rounds=None,
                    save_dc=None,
                    applied_round=0,
                    exhaustion_level=1,
                    source_effect_id=None,
                )
            ]
        }
    )
    facts = actor_facts(
        actor,
        snapshot.combat_state.actor_zone[actor.entity_id],
        snapshot.combat_state.movement_ledgers[actor.entity_id],
    )
    combat = combat_facts(snapshot.combat_state)
    before = facts.model_dump_json(), combat.model_dump_json()
    actor.conditions.clear()
    actor.custom_counters["later"] = {"current": 9, "max": 9}
    snapshot.combat_state.actor_zone[actor.entity_id] = "2,2"
    snapshot.combat_state.dead_ids.add(actor.entity_id)
    assert (facts.model_dump_json(), combat.model_dump_json()) == before
    with pytest.raises(ValidationError, match="frozen_instance"):
        facts.budget.movement_remaining = 1
    with pytest.raises(AttributeError):
        combat.dead_ids.add("later")
    assert len(facts.conditions) == 1
    with pytest.raises(ValidationError, match="frozen_instance"):
        facts.conditions[0].condition = "unconscious"
    assert not hasattr(facts, "custom_counters")


def test_uncovered_is_independent_of_rule_status_and_has_no_execution_records():
    value = EventStageFacts.uncovered("complex_effects_or_reactions")
    assert value.coverage == "uncovered"
    assert value.stages == ()
    with pytest.raises(ValidationError):
        EventStageFacts.model_validate(value.model_copy(update={"reason": None}))


@pytest.mark.parametrize("kind", ["event", "input", "operation_completed"])
def test_orphan_event_and_duplicate_internal_boundaries_are_rejected(kind):
    _, _, value = candidate()
    stage = value.stages[1].model_copy(
        update={
            "sequence": 2,
            "kind": kind,
            "sample_point": {
                "event": "after_event_fold",
                "input": "input_reference",
                "operation_completed": "operation_end",
            }[kind],
        }
    )
    bad = value.model_copy(
        update={
            "stages": (
                *value.stages[:2],
                stage,
                value.stages[-1].model_copy(update={"sequence": 3}),
            ),
            "final_stage": 3,
        }
    )
    with pytest.raises(ValidationError):
        EventStageFacts.model_validate(bad)


def test_departed_actor_reference_is_closed_against_input():
    snapshot, events, value = candidate()
    combat = combat_facts(snapshot.combat_state)
    combat = combat.model_copy(
        update={"turn": combat.turn.model_copy(update={"departed_actor_id": "absent"})}
    )
    value = value.model_copy(
        update={
            "stages": (
                value.stages[0],
                value.stages[1].model_copy(
                    update={
                        "combat_update": combat_change(combat_facts(snapshot.combat_state), combat)
                    }
                ),
                value.stages[2],
            )
        }
    )
    with pytest.raises(ValueError, match="input closure"):
        value.verify_input(snapshot, events)


def test_json_set_projection_is_stable_in_fresh_hash_seed_processes():
    snapshot, _, _ = candidate()
    value = combat_facts(snapshot.combat_state).model_copy(
        update={
            "dead_ids": frozenset({"actor:one", "actor:two", "actor:three"}),
            "processed_zero_hp_damage_instances": frozenset(
                {("actor:one", "damage:1"), ("actor:two", "damage:2")}
            ),
        }
    )
    value = StageCombatFacts.model_validate(value)
    outputs = []
    for seed in ("1", "42", "777"):
        script = (
            "import sys; from dnd5e_engine.evaluation_stages import StageCombatFacts; "
            "print(StageCombatFacts.model_validate_json(sys.argv[1]).model_dump_json())"
        )
        output = subprocess.run(
            [sys.executable, "-c", script, value.model_dump_json()],
            env={**os.environ, "PYTHONHASHSEED": seed},
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        outputs.append(output)
    assert outputs == [value.model_dump_json()] * 3
