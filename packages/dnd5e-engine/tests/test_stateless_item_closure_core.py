"""R10 successful isolated execution, differential results, and post-compute faults."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from dnd5e_engine import evaluation_closure, evaluation_items
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.evaluation_delta import InventoryConsume
from dnd5e_engine.evaluation_rng import RNGState
from tests.evaluation_support import HERO, synthetic_loader
from tests.test_stateless_attack import execute, request_and_live
from tests.test_stateless_attack_core import forbidden, prohibit_legacy
from tests.test_stateless_checks import check_case
from tests.test_stateless_closure import terminal_request
from tests.test_stateless_items import item_case, legacy_intent


def isolate(monkeypatch):
    prohibit_legacy(monkeypatch)
    for name in (
        "_derive_ended_reason",
        "_project_outcome",
        "_record_item_charge_spend",
        "_item_charge_gate",
        "_validate_intent_preconditions",
        "_classify_action_cost",
        "_classify_attack_funding",
        "_intent_pre_resolution_failure",
    ):
        monkeypatch.setattr(orch, name, forbidden)


@pytest.mark.parametrize("hp", [1, 39, 40])
@pytest.mark.parametrize("last_resource", [False, True])
def test_real_item_success_without_legacy_matches_events_and_rng(monkeypatch, hp, last_resource):
    request, handle, live, loader = item_case(hp=hp, quantity=2)
    if last_resource:
        update = {"action_available": False, "attacks_remaining": 0, "movement_remaining": 0}
        actors = list(request.state_snapshot.character_states)
        actors[0] = actors[0].model_copy(update=update)
        request = request.model_copy(
            update={
                "state_snapshot": request.state_snapshot.model_copy(
                    update={"character_states": tuple(actors)}
                )
            }
        )
        live.initiative[0] = live.initiative[0].model_copy(update=update)
    before = deepcopy(request)
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request, loader)
        assert result.status == "accepted", result.error
        assert result == execute(request, loader)
        unit = result.state_delta.operations[0]
        assert isinstance(unit, InventoryConsume)
        assert unit.expected.quantity == 2
        assert unit.value.quantity == 1
        assert not live.event_log
        assert live.tracked_hp[HERO] == hp
    asyncio.run(orch.submit_player_intent(handle, HERO, legacy_intent()))
    assert result.proposed_events == tuple(live.event_log)
    assert result.rng_transition.next_state == RNGState.capture(live.rng)
    assert request == before


@pytest.mark.parametrize("defeat", [False, True])
def test_real_closure_success_without_legacy_matches_history_xp_events_rng(monkeypatch, defeat):
    request, handle, _ = terminal_request(defeat=defeat)
    before = deepcopy(request)
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request)
        assert result.status == "accepted", result.error
        assert result == execute(request)
        assert result.rng_transition.next_state == request.rng_context.state
        assert not result.rng_transition.state_changed
    legacy = asyncio.run(orch.end_combat(handle))
    closure = result.state_delta.operations[0]
    assert closure.reason == legacy.outcome.ended_reason
    assert closure.xp_increments == legacy.outcome.xp_awarded
    assert closure.xp_increments == ({} if defeat else {HERO: 50})
    assert [r.model_dump() for r in closure.historical.deaths] == [
        r.model_dump() for r in request.state_snapshot.combat_state.deaths_recorded
    ]
    assert closure.historical.residual_hp == legacy.outcome.residual_hp
    assert closure.historical.residual_temp_hp == legacy.outcome.residual_temp_hp
    assert closure.historical.expended_resources == legacy.outcome.expended_resources
    assert result.proposed_events == tuple(legacy.events)
    assert request == before


def test_all_four_admitted_operations_succeed_with_legacy_disabled(monkeypatch):
    attack, _, _ = request_and_live(ac=1)
    item, _, _, items = item_case()
    closure, _, _ = terminal_request()
    check = check_case(dc=1)
    cases = [
        (attack, synthetic_loader()),
        (item, items),
        (closure, synthetic_loader()),
        (check, synthetic_loader()),
    ]
    original = deepcopy([r for r, _ in cases])
    isolate(monkeypatch)
    expected = [execute(r, loader) for r, loader in cases]
    assert all(r.status == "accepted" for r in expected)
    with ThreadPoolExecutor(max_workers=4) as pool:
        actual = list(pool.map(lambda i: execute(*cases[i % 4]), range(16)))
    assert actual == [expected[i % 4] for i in range(16)]
    assert [r for r, _ in cases] == original


@pytest.mark.parametrize("cancel", [False, True])
def test_resolver_failure_after_heal_and_payment_keeps_inputs_unchanged(monkeypatch, cancel):
    request, _, live, loader = item_case()
    before = deepcopy(request)
    original = evaluation_items.resolve_activity
    fault = asyncio.CancelledError if cancel else RuntimeError

    def interrupted(activity, context, **kwargs):
        original(activity, context, **kwargs)
        assert not context.caster.bonus_action_available
        assert context.rng.getstate() != request.rng_context.state.restore().getstate()
        raise fault("post-heal fault")

    isolate(monkeypatch)
    monkeypatch.setattr(evaluation_items, "resolve_activity", interrupted)
    with pytest.raises(fault, match="post-heal fault"):
        execute(request, loader)
    assert request == before
    assert live.tracked_hp[HERO] == 10
    assert live.event_log == []


def test_closure_projector_failure_is_isolated_after_local_projection(monkeypatch):
    request, _, _ = terminal_request()
    before = deepcopy(request)
    original = evaluation_closure.project_outcome

    def interrupted(**facts):
        outcome = original(**facts)
        assert outcome.xp_awarded == {HERO: 50}
        facts["actors"].clear()
        raise RuntimeError("post-outcome fault")

    isolate(monkeypatch)
    monkeypatch.setattr(evaluation_closure, "project_outcome", interrupted)
    with pytest.raises(RuntimeError, match="post-outcome fault"):
        execute(request)
    assert request == before


def test_bonus_item_preserves_remaining_movement_window():
    request, handle, live, loader = item_case()
    changes = {"action_available": False, "attacks_remaining": 0}
    actors = list(request.state_snapshot.character_states)
    actors[0] = actors[0].model_copy(update=changes)
    request = request.model_copy(
        update={
            "state_snapshot": request.state_snapshot.model_copy(
                update={"character_states": tuple(actors)}
            )
        }
    )
    live.initiative[0] = live.initiative[0].model_copy(update=changes)
    result = execute(request, loader)
    assert result.status == "accepted"
    asyncio.run(orch.submit_player_intent(handle, HERO, legacy_intent()))
    assert result.proposed_events == tuple(live.event_log)
    assert not any(e.type == "turn_ended" for e in result.proposed_events)
    assert result.rng_transition.next_state == RNGState.capture(live.rng)


@pytest.mark.parametrize("mode", ["walk", "swim", "climb"])
def test_item_boundary_projects_the_explicit_active_movement_mode(mode):
    from dnd5e_engine.evaluation_state import MovementLedgerState

    request, handle, live, loader = item_case()
    modes = live.initiative[0].movement_modes.model_copy(update={"swim": 15, "climb": 10})
    update = {
        "action_available": False,
        "attacks_remaining": 0,
        "movement_remaining": 0,
        "movement_modes": modes,
    }
    live.initiative[0] = live.initiative[0].model_copy(update=update)
    live.movement_ledgers[HERO] = live.movement_ledgers[HERO].model_copy(
        update={"active_mode": mode, "spent_ft": 5, "distance_ft": 5}
    )
    actors = list(request.state_snapshot.character_states)
    actors[0] = actors[0].model_copy(
        update={
            **update,
            "movement_modes": actors[0].movement_modes.model_copy(update={"swim": 15, "climb": 10}),
        }
    )
    ledger = MovementLedgerState.model_validate(live.movement_ledgers[HERO].model_dump())
    request = request.model_copy(
        update={
            "state_snapshot": request.state_snapshot.model_copy(
                update={
                    "character_states": tuple(actors),
                    "combat_state": request.state_snapshot.combat_state.model_copy(
                        update={
                            "movement_ledgers": {
                                **request.state_snapshot.combat_state.movement_ledgers,
                                HERO: ledger,
                            }
                        }
                    ),
                }
            )
        }
    )
    result = execute(request, loader)
    assert result.status == "accepted"
    asyncio.run(orch.submit_player_intent(handle, HERO, legacy_intent()))
    expected = {"walk": 25, "swim": 10, "climb": 5}[mode]
    op = next(
        op
        for op in result.state_delta.operations
        if op.kind == "combat.action_budget_update" and op.actor_id == HERO
    )
    assert op.value.movement_remaining == expected
    assert live.initiative[0].movement_remaining == expected
    assert result.proposed_events == tuple(live.event_log)
    assert result.rng_transition.next_state == RNGState.capture(live.rng)
