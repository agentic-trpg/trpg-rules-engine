"""Candidate mechanics and call-local events are independent computation outputs."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from dnd5e_engine.evaluation_computation import CombatComputation
from dnd5e_engine.evaluation_projection import EvaluationInvariantError, attack_delta
from dnd5e_engine.evaluation_snapshot import capture_evaluation_snapshot
from dnd5e_engine.evaluation_state import CombatSnapshot
from dnd5e_engine.events import IntentSubmitted
from dnd5e_engine.specs import GridScene
from tests.evaluation_support import HERO
from tests.test_stateless_attack import execute, request_and_live


def historical_request(count):
    request, _, live = request_and_live()
    live.event_log = [IntentSubmitted(actor_id=HERO, intent_type="pass") for _ in range(count)]
    snapshot = capture_evaluation_snapshot(
        live, request.state_snapshot, GridScene(width=3, height=3)
    )
    return request.model_copy(update={"state_snapshot": snapshot})


def test_result_separates_mechanics_from_new_ordered_events():
    request = historical_request(9)
    snapshot = request.state_snapshot
    before = deepcopy(snapshot)
    rng = request.rng_context.state.restore()
    initial_rng = rng.getstate()
    computation = CombatComputation(snapshot, rng)
    event = IntentSubmitted(actor_id=HERO, intent_type="attack")
    computation.emit(event)
    result = computation.result()
    assert result.candidate_state == snapshot
    assert result.proposed_events == (event,)
    assert "event_log" not in result.candidate_state.combat_state.model_dump()
    assert attack_delta(snapshot, result.candidate_state).operations == ()
    computation.emit(IntentSubmitted(actor_id=HERO, intent_type="pass"))
    assert result.proposed_events == (event,)
    assert computation.result().proposed_events == tuple(computation.events)
    assert snapshot == before
    assert rng.getstate() == initial_rng


def test_schema_rejects_candidate_audit_history_and_projector_keeps_mechanical_guard():
    request = historical_request(2)
    snapshot = request.state_snapshot
    encoded = snapshot.model_dump(mode="json")
    encoded["combat_state"]["event_log"] = []
    with pytest.raises(ValidationError, match="extra_forbidden"):
        CombatSnapshot.model_validate(encoded)
    altered = snapshot.model_copy(
        update={
            "combat_state": snapshot.combat_state.model_copy(update={"combat_id": "unexpressed"})
        }
    )
    with pytest.raises(EvaluationInvariantError):
        attack_delta(snapshot, altered)


@pytest.mark.parametrize("count", [1, 19, 1000])
def test_external_audit_prefix_does_not_change_state_delta_events_or_rng(count):
    request = historical_request(0)
    expected = execute(request)
    historical = historical_request(count)
    before = deepcopy(historical)
    assert execute(historical) == expected
    assert historical == before
