"""Voluntary release clears one supplied source graph through public evaluation."""

from copy import deepcopy
from dataclasses import replace

import pytest

from dnd5e_engine.evaluation_contracts import CombatIntentPayload, RuleEvaluationResult
from dnd5e_engine.evaluation_delta import ActionBudgetUpdate, ConditionsUpdate
from dnd5e_engine.evaluation_rng import RNGState
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import execute
from tests.test_stateless_attack_concentration import ATTACKER
from tests.test_stateless_hold_cast import apply_cast, apply_existing, cast_case
from tests.test_stateless_hold_turn import apply_hold, hold_case, isolated


def _release(request, source=FOE, **payload):
    return request.model_copy(
        update={
            "actor_id": source,
            "payload": CombatIntentPayload(
                intent_type="drop_concentration", source_id=source, **payload
            ),
        }
    )


@pytest.mark.parametrize("source_turn", [True, False])
def test_release_clears_multitarget_graph_on_any_turn(source_turn, monkeypatch):
    request, _, _, loader = hold_case(multi=True, source_turn=source_turn)
    request = _release(request)
    before = deepcopy(request)
    isolated(monkeypatch)
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("release RNG"))
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert result == execute(request, loader)
    assert request == before
    assert RuleEvaluationResult.model_validate_json(result.model_dump_json()) == result
    assert result.rng_transition.next_state == before.rng_context.state
    assert not result.rng_transition.state_changed
    after = apply_hold(request.state_snapshot, result)
    state = before.state_snapshot.combat_state
    assert after.combat_state == state.model_copy(
        update={
            "concentration_chain": {},
            "concentration_rounds_remaining": {},
            "effect_lifecycles": (),
            "conditions_by_effect": (),
        }
    )
    assert not after.effect_states
    assert after.resource_state == before.state_snapshot.resource_state
    assert [e.type for e in result.proposed_events] == [
        "intent_submitted",
        "concentration_dropped",
        "effect_expired",
        "condition_removed",
        "concentration_dropped",
        "effect_expired",
        "condition_removed",
    ]
    for op in result.state_delta.operations:
        if isinstance(op, ActionBudgetUpdate):
            assert op.value == op.expected.model_copy(
                update={"movement_remaining": op.value.movement_remaining}
            )
    assert all(
        not a.conditions and a.concentration_effect_id is None for a in after.character_states
    )
    with pytest.raises(AssertionError):
        apply_hold(after, result)


def test_release_preserves_other_source_and_shared_condition(monkeypatch):
    request, _, _, loader = hold_case(source_turn=False)
    snapshot = request.state_snapshot
    state = snapshot.combat_state
    original = snapshot.effect_states[0]
    other_app = original.lifecycle.model_copy(update={"source_id": ATTACKER})
    other = original.model_copy(
        update={
            "origin": original.origin.removesuffix(FOE) + ATTACKER,
            "lifecycle": other_app,
        }
    )
    other_identity = (other.target_id, other.id, other.origin)
    other_clock = replace(
        state.effect_lifecycles[0].state, identity=other_identity, application=other_app
    )
    other_record = state.effect_lifecycles[0].model_copy(
        update={"identity": other_identity, "state": other_clock}
    )
    other_link = state.conditions_by_effect[0].model_copy(update={"identity": other_identity})
    actors = tuple(
        a.model_copy(update={"concentration_effect_id": other.id}) if a.entity_id == ATTACKER else a
        for a in snapshot.character_states
    )
    request = request.model_copy(
        update={
            "state_snapshot": snapshot.model_copy(
                update={
                    "character_states": actors,
                    "effect_states": (*snapshot.effect_states, other),
                    "combat_state": state.model_copy(
                        update={
                            "effect_lifecycles": (*state.effect_lifecycles, other_record),
                            "conditions_by_effect": (*state.conditions_by_effect, other_link),
                            "concentration_chain": {
                                **state.concentration_chain,
                                ATTACKER: [other_identity],
                            },
                            "concentration_rounds_remaining": {
                                **state.concentration_rounds_remaining,
                                ATTACKER: 4,
                            },
                        }
                    ),
                }
            )
        }
    )
    isolated(monkeypatch)
    result = execute(_release(request), loader)
    assert result.status == "accepted", result.error
    assert not any(isinstance(o, ConditionsUpdate) for o in result.state_delta.operations)
    assert not any(e.type == "condition_removed" for e in result.proposed_events)
    after = apply_hold(request.state_snapshot, result)
    assert after.effect_states == (other,)
    assert after.combat_state.concentration_chain == {ATTACKER: [other_identity]}
    assert after.combat_state.effect_lifecycles == (other_record,)
    assert after.combat_state.conditions_by_effect == (other_link,)
    assert next(a for a in after.character_states if a.entity_id == HERO).conditions


def test_release_keeps_an_independent_condition_with_the_same_name():
    request, _, _, loader = hold_case(source_turn=False)
    snapshot = request.state_snapshot
    held = next(a for a in snapshot.character_states if a.entity_id == HERO)
    independent = held.conditions[0].model_copy(
        update={"source_effect_id": None, "source_entity_id": ATTACKER}
    )
    held = held.model_copy(update={"conditions": [*held.conditions, independent]})
    snapshot = snapshot.model_copy(
        update={
            "character_states": tuple(
                held if a.entity_id == HERO else a for a in snapshot.character_states
            )
        }
    )
    request = _release(request.model_copy(update={"state_snapshot": snapshot}))
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert not any(e.type == "condition_removed" for e in result.proposed_events)
    assert not any(isinstance(o, ActionBudgetUpdate) for o in result.state_delta.operations)
    after = apply_hold(snapshot, result)
    assert next(a for a in after.character_states if a.entity_id == HERO).conditions == [
        independent
    ]


def test_releases_clock_free_cast_anchor(monkeypatch):
    request, _, _, loader = cast_case(seed=5)
    cast = execute(request, loader)
    assert cast.status == "accepted", cast.error
    snapshot = apply_cast(request.state_snapshot, cast)
    assert snapshot.effect_states[0].flags["concentration_anchor"]
    request = _release(request.model_copy(update={"state_snapshot": snapshot}))
    isolated(monkeypatch)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert [e.type for e in result.proposed_events] == [
        "intent_submitted",
        "concentration_dropped",
        "effect_expired",
    ]
    assert len(result.state_delta.operations) == 2
    assert not apply_existing(snapshot, result).effect_states


@pytest.mark.parametrize(
    ("fault", "status", "code"),
    [
        ("different_actor", "rejected", "concentration.absent"),
        ("extra_payload", "unsupported", "concentration.intent"),
        ("missing_member", "unsupported", "concentration.graph"),
        ("shared_owner", "unsupported", "concentration.graph"),
    ],
)
def test_no_group_or_incomplete_association_refuses_atomically(fault, status, code):
    request, _, _, loader = hold_case(multi=True)
    request = _release(request)
    snapshot = request.state_snapshot
    combat = snapshot.combat_state
    chains = combat.concentration_chain
    if fault == "different_actor":
        request = _release(request, ATTACKER)
    elif fault == "extra_payload":
        request = _release(request, target_id=HERO)
    else:
        chains = (
            {FOE: chains[FOE][:-1]}
            if fault == "missing_member"
            else {**chains, ATTACKER: chains[FOE]}
        )
        snapshot = snapshot.model_copy(
            update={"combat_state": combat.model_copy(update={"concentration_chain": chains})}
        )
        request = request.model_copy(update={"state_snapshot": snapshot})
    result = execute(request, loader)
    assert (result.status, result.error.code) == (status, code)
    assert result.state_delta is None
    assert result.rng_transition is None
    assert not result.proposed_events
