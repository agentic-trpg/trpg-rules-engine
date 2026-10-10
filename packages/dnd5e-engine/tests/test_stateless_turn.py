"""R11 explicit lifecycle: independent expectations, Legacy parity and fault isolation."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from dnd5e_engine import evaluation_computation
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, RuleEvaluationResult
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_snapshot import capture_evaluation_snapshot
from dnd5e_engine.evaluation_state import DeathSaveRecord
from dnd5e_engine.events import Death, DeathSaveRolled, RoundStarted, TurnStarted
from dnd5e_engine.outcome import DeathRecord
from dnd5e_engine.specs import GridScene
from dnd5e_engine.types.conditions import ActiveCondition
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import apply_delta, execute, request_and_live
from tests.test_stateless_item_closure_core import isolate


def turn_case(*, npc=False, seed=0, dying=False, successes=0, failures=0, stable=False):
    request, handle, live = request_and_live(seed=seed)
    if npc:
        live.current_turn_index = 1
        live.current_actor_id = FOE
    if dying:
        actor = live.initiative[0]
        condition = ActiveCondition(
            condition="unconscious",
            source_entity_id="implied:event",
            scope="combat",
            applied_round=1,
        )
        saves = {"successes": successes, "failures": failures, "is_stable": stable}
        live.initiative[0] = actor.model_copy(
            update={
                "hp_current": 0,
                "conditions": [condition],
                "death_saves": saves,
                "movement_remaining": 0,
            }
        )
        live.tracked_hp[HERO] = 0
        live.active_conditions[HERO] = {"unconscious"}
    snapshot = capture_evaluation_snapshot(
        live,
        request.state_snapshot,
        GridScene.model_validate(request.state_snapshot.scene_state.grid.model_dump()),
    )
    return (
        request.model_copy(
            update={
                "actor_id": FOE if npc else HERO,
                "payload": CombatIntentPayload(intent_type="pass"),
                "state_snapshot": snapshot,
            }
        ),
        handle,
        live,
    )


def assert_parity(request, handle, live, result):
    before_events = len(live.event_log)
    asyncio.run(orch.submit_player_intent(handle, request.actor_id, request.payload))
    expected = capture_evaluation_snapshot(
        live,
        request.state_snapshot,
        GridScene.model_validate(request.state_snapshot.scene_state.grid.model_dump()),
    )
    assert result.proposed_events == tuple(live.event_log[before_events:])
    assert result.rng_transition.next_state == RNGState.capture(live.rng)
    assert apply_delta(request.state_snapshot, result) == expected
    return expected


@pytest.mark.parametrize("npc", [False, True])
@pytest.mark.parametrize("spent", [False, True])
def test_plain_pass_for_pc_and_npc_with_any_ordinary_action_budget(monkeypatch, npc, spent):
    request, handle, live = turn_case(npc=npc)
    if spent:
        index = int(npc)
        live.initiative[index] = live.initiative[index].model_copy(
            update={"action_available": False, "attacks_remaining": 0}
        )
        request = request.model_copy(
            update={
                "state_snapshot": capture_evaluation_snapshot(
                    live, request.state_snapshot, GridScene(width=3, height=3)
                )
            }
        )
    before = deepcopy(request)
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request)
        assert result.status == "accepted", result.error
        assert result == execute(request)
        assert not result.rng_transition.state_changed
    after = assert_parity(request, handle, live, result)
    assert after.combat_state.current_turn_index == int(not npc)
    assert after.combat_state.round_number == (2 if npc else 1)
    assert after.combat_state.turn_serial == 2
    assert request == before
    assert RuleEvaluationResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "seed,failures,successes,natural,outcome",
    [
        (31, 0, 0, 1, "crit_failure"),
        (31, 2, 0, 1, "crit_failure"),
        (5, 0, 0, 20, "crit_success"),
        (0, 0, 2, 13, "success"),
        (1, 0, 0, 5, "failure"),
    ],
)
def test_dying_next_pc_natural_outcomes_and_full_delta(
    monkeypatch, seed, failures, successes, natural, outcome
):
    request, handle, live = turn_case(
        npc=True, seed=seed, dying=True, failures=failures, successes=successes
    )
    before = deepcopy(request)
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request)
        assert result.status == "accepted", result.error
        assert execute(request) == result
    rolled = next(e for e in result.proposed_events if isinstance(e, DeathSaveRolled))
    assert rolled.roll_total == natural
    assert rolled.outcome == outcome
    after = assert_parity(request, handle, live, result)
    actor = after.character_states[0]
    if natural == 20:
        assert actor.hp_current == 1
        assert [c.condition for c in actor.conditions] == ["prone"]
        assert actor.movement_remaining == 30
    elif failures == 2:
        assert actor.death_saves.failures == 4
        assert HERO in after.combat_state.dead_ids
        assert after.combat_state.deaths_recorded[-1].reason == "death_saves"
        assert any(isinstance(e, Death) for e in result.proposed_events)
    elif successes == 2:
        assert actor.death_saves.is_stable
    else:
        assert actor.hp_current == 0
    assert request == before


def test_stable_pc_has_no_draw_and_dead_actor_is_skipped(monkeypatch):
    request, handle, live = turn_case(npc=True, dying=True, successes=3, stable=True)
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request)
    assert result.status == "accepted"
    assert not result.rng_transition.state_changed
    assert not any(isinstance(e, DeathSaveRolled) for e in result.proposed_events)
    assert_parity(request, handle, live, result)


def test_recorded_dead_next_actor_skips_and_wraps_to_living_actor(monkeypatch):
    request, handle, live = turn_case()
    live.initiative[1] = live.initiative[1].model_copy(update={"hp_current": 0, "is_alive": False})
    live.tracked_hp[FOE] = 0
    live.dead_ids.add(FOE)
    live.deaths_recorded.append(
        DeathRecord(
            target_id=FOE,
            target_kind="monster",
            location_id=live.scene_location_id,
            reason="damage",
            killer_id=HERO,
        )
    )
    snapshot = capture_evaluation_snapshot(
        live, request.state_snapshot, GridScene(width=3, height=3)
    )
    request = request.model_copy(update={"state_snapshot": snapshot})
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request)
    after = assert_parity(request, handle, live, result)
    assert after.combat_state.round_number == 2
    assert after.combat_state.current_turn_index == 0
    assert [e.actor_id for e in result.proposed_events if isinstance(e, TurnStarted)] == [HERO]
    assert len([e for e in result.proposed_events if isinstance(e, RoundStarted)]) == 1


def test_two_rounds_repeat_concurrently_and_guard_stale_budget(monkeypatch):
    request, handle, live = turn_case()
    for index in range(4):
        with monkeypatch.context() as scoped:
            isolate(scoped)
            result = execute(request)
            with ThreadPoolExecutor(max_workers=3) as pool:
                assert (
                    list(pool.map(lambda _, current=request: execute(current), range(6)))
                    == [result] * 6
                )
        after = assert_parity(request, handle, live, result)
        assert after.combat_state.round_number == 1 + (index + 1) // 2
        with pytest.raises(AssertionError):
            apply_delta(after, result)
        request = request.model_copy(
            update={
                "state_snapshot": after,
                "actor_id": after.combat_state.initiative_ids[
                    after.combat_state.current_turn_index
                ],
                "rng_context": request.rng_context.model_copy(
                    update={"state": result.rng_transition.next_state}
                ),
            }
        )


@pytest.mark.parametrize(
    "mutation",
    ["effect", "concentration", "reaction", "duration", "foreign_intent", "invalid_dying"],
)
def test_unsupported_lifecycle_dependencies_refused_before_rng(monkeypatch, mutation):
    from dnd5e_engine.evaluation_effects import effect_state
    from dnd5e_engine.types.effects import ActiveEffect

    request, _, _ = turn_case(npc=True, dying=True)
    snapshot = request.state_snapshot
    if mutation == "effect":
        snapshot = snapshot.model_copy(
            update={
                "effect_states": (
                    effect_state(
                        ActiveEffect(id="test", name="test", target_id=HERO, origin="synthetic")
                    ),
                )
            }
        )
    elif mutation == "concentration":
        actors = list(snapshot.character_states)
        actors[0] = actors[0].model_copy(update={"concentration_effect_id": "test"})
        snapshot = snapshot.model_copy(update={"character_states": tuple(actors)})
    elif mutation == "reaction":
        snapshot = snapshot.model_copy(
            update={
                "combat_state": snapshot.combat_state.model_copy(
                    update={
                        "reaction_effects_pending_expiry": {HERO: [(HERO, "test", "synthetic")]}
                    }
                )
            }
        )
    elif mutation == "duration":
        actors = list(snapshot.character_states)
        actors[0] = actors[0].model_copy(
            update={
                "conditions": [actors[0].conditions[0].model_copy(update={"duration_rounds": 1})]
            }
        )
        snapshot = snapshot.model_copy(update={"character_states": tuple(actors)})
    elif mutation == "foreign_intent":
        request = request.model_copy(
            update={"payload": request.payload.model_copy(update={"spell_id": "test"})}
        )
    else:
        actors = list(snapshot.character_states)
        actors[0] = actors[0].model_copy(
            update={"death_saves": DeathSaveRecord(successes=0, failures=3, is_stable=False)}
        )
        snapshot = snapshot.model_copy(update={"character_states": tuple(actors)})
    request = request.model_copy(update={"state_snapshot": snapshot})
    before = deepcopy(request)
    monkeypatch.setattr(
        RNGState, "restore", lambda self: pytest.fail("RNG restored before support gate")
    )
    result = execute(request)
    assert result.status == "unsupported"
    assert result.state_delta is result.rng_transition is None
    assert not result.proposed_events
    assert request == before


@pytest.mark.parametrize("cancel", [False, True])
def test_post_death_save_failure_or_cancel_cannot_leak(monkeypatch, cancel):
    request, _, live = turn_case(npc=True, dying=True, seed=5)
    before = deepcopy(request)
    original = evaluation_computation.roll_death_save
    fault = asyncio.CancelledError if cancel else RuntimeError

    def interrupted(actor, rng):
        result = original(actor, rng)
        assert result.combatant.hp_current == 1
        assert RNGState.capture(rng) != request.rng_context.state
        raise fault("post-death-save fault")

    isolate(monkeypatch)
    monkeypatch.setattr(evaluation_computation, "roll_death_save", interrupted)
    with pytest.raises(fault, match="post-death-save fault"):
        execute(request)
    assert request == before
    assert live.tracked_hp[HERO] == 0
    assert not live.event_log


def test_next_actor_resets_action_bonus_reaction_movement_and_ledger(monkeypatch):
    request, handle, live = turn_case()
    live.initiative[1] = live.initiative[1].model_copy(
        update={
            "action_available": False,
            "bonus_action_available": False,
            "reaction_available": False,
            "action_taken_this_turn": True,
            "bonus_action_taken_this_turn": True,
            "attacks_remaining": 0,
            "movement_remaining": 0,
            "dodging": True,
            "disengaging_this_turn": True,
        }
    )
    live.movement_ledgers[FOE] = live.movement_ledgers[FOE].model_copy(
        update={"spent_ft": 60, "distance_ft": 60, "dash_count": 1}
    )
    snapshot = capture_evaluation_snapshot(
        live, request.state_snapshot, GridScene(width=3, height=3)
    )
    request = request.model_copy(update={"state_snapshot": snapshot})
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request)
    after = assert_parity(request, handle, live, result)
    actor = after.character_states[1]
    assert actor.action_available
    assert actor.bonus_action_available
    assert actor.reaction_available
    assert not actor.action_taken_this_turn
    assert not actor.bonus_action_taken_this_turn
    assert actor.attacks_remaining == 1
    assert actor.movement_remaining == 30
    assert not actor.dodging
    assert not actor.disengaging_this_turn
    ledger = after.combat_state.movement_ledgers[FOE]
    assert (ledger.spent_ft, ledger.distance_ft, ledger.dash_count, ledger.active_mode) == (
        0,
        0,
        0,
        "walk",
    )


@pytest.mark.parametrize(
    "mutation,code",
    [("alien", "actor_invalid"), ("other", "not_actor_turn"), ("ended", "combat_ended")],
)
def test_turn_identity_and_phase_rejected_without_draw(monkeypatch, mutation, code):
    request, _, _ = turn_case()
    if mutation == "ended":
        request = request.model_copy(
            update={
                "state_snapshot": request.state_snapshot.model_copy(
                    update={
                        "combat_state": request.state_snapshot.combat_state.model_copy(
                            update={"ended": True}
                        )
                    }
                )
            }
        )
    else:
        request = request.model_copy(update={"actor_id": "alien" if mutation == "alien" else FOE})
    before = deepcopy(request)
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("draw before rejection"))
    result = execute(request)
    assert result.status == "rejected"
    assert result.error.code == code
    assert result.state_delta is result.rng_transition is None
    assert not result.proposed_events
    assert request == before
