"""Candidate mechanics and call-local events are independent computation outputs."""

from copy import deepcopy

import pytest

from dnd5e_engine.evaluation_computation import CombatComputation
from dnd5e_engine.evaluation_projection import EvaluationInvariantError, attack_delta
from dnd5e_engine.events import IntentSubmitted
from tests.evaluation_support import HERO
from tests.test_stateless_attack import execute, request_and_live


def historical_request(count):
    request, _, _ = request_and_live()
    history = tuple(IntentSubmitted(actor_id=HERO, intent_type="pass") for _ in range(count))
    state = request.state_snapshot.combat_state.model_copy(update={"event_log": history})
    return request.model_copy(
        update={"state_snapshot": request.state_snapshot.model_copy(update={"combat_state": state})}
    )


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
    assert result.candidate_state.combat_state.event_log == snapshot.combat_state.event_log
    assert attack_delta(snapshot, result.candidate_state).operations == ()
    computation.emit(IntentSubmitted(actor_id=HERO, intent_type="pass"))
    assert result.proposed_events == (event,)
    assert computation.result().proposed_events == tuple(computation.events)
    assert snapshot == before
    assert rng.getstate() == initial_rng


def test_projector_rejects_even_an_append_to_candidate_audit_history():
    request = historical_request(2)
    snapshot = request.state_snapshot
    altered = snapshot.model_copy(
        update={
            "combat_state": snapshot.combat_state.model_copy(
                update={
                    "event_log": (
                        *snapshot.combat_state.event_log,
                        IntentSubmitted(actor_id=HERO, intent_type="attack"),
                    )
                }
            )
        }
    )
    with pytest.raises(EvaluationInvariantError, match="history"):
        attack_delta(snapshot, altered)


@pytest.mark.parametrize("count", [1, 19, 1000])
def test_external_audit_prefix_does_not_change_state_delta_events_or_rng(count):
    request = historical_request(0)
    expected = execute(request)
    historical = historical_request(count)
    before = deepcopy(historical)
    assert execute(historical) == expected
    assert historical == before
