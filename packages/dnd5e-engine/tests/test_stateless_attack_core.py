"""R9 structural isolation plus independent Legacy-entry differential execution."""

import asyncio
import random
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from dnd5e_engine import evaluation_attack, evaluation_context, evaluation_snapshot
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd5e_engine.evaluation_rng import RNGContext, RNGState
from tests.evaluation_support import FOE, HERO, synthetic_loader
from tests.test_evaluation_review_findings import recapture
from tests.test_npc_availability import npc_case
from tests.test_stateless_attack import apply_delta, execute, request_and_live
from tests.test_stateless_stabilization import OTHER, multi_enemy_case


def forbidden(*args, **kwargs):
    raise AssertionError("Stateless Attack touched Legacy execution/runtime")


class ForbiddenRegistry:
    __getitem__ = __setitem__ = __iter__ = __len__ = __contains__ = forbidden
    get = values = items = keys = clear = pop = setdefault = forbidden


def prohibit_legacy(monkeypatch):
    for name in (
        "_LiveCombat",
        "CombatHandle",
        "_submit_live_intent",
        "_get_live",
        "get_live",
        "submit_player_intent",
        "start_combat",
        "_register_default_turn_hooks",
        "_resolve_intent_activities",
        "_emit",
        "resolve_activity",
    ):
        monkeypatch.setattr(orch, name, forbidden)
    monkeypatch.setattr(orch, "_REGISTRY", ForbiddenRegistry())
    monkeypatch.setattr(evaluation_context, "execution_context", forbidden)
    monkeypatch.setattr(evaluation_snapshot, "capture_evaluation_snapshot", forbidden)
    monkeypatch.setattr(random, "randint", forbidden)
    monkeypatch.setattr(random, "seed", forbidden)


@pytest.mark.parametrize("npc", [False, True])
def test_successful_attack_forbids_entire_legacy_runtime_and_is_repeatable(monkeypatch, npc):
    if npc:
        request, _, live, loader = npc_case()
    else:
        request, _, live = request_and_live(ac=1)
        loader = synthetic_loader()
    original_request = deepcopy(request)
    original_live = deepcopy(live.initiative)
    original_rng = live.rng.getstate()
    registry = orch._REGISTRY
    observed = []
    live.event_listeners.append(observed.append)
    with monkeypatch.context() as isolated:
        prohibit_legacy(isolated)
        result = execute(request, loader)
        assert result.status == "accepted", result.error
        assert any(e.type == "attack_rolled" for e in result.proposed_events)
        assert any(e.type == "damage_applied" and e.amount > 0 for e in result.proposed_events)
        assert result == execute(request, loader)
        applied = apply_delta(request.state_snapshot, result)
        target = HERO if npc else FOE
        assert (
            next(a for a in applied.character_states if a.entity_id == target).hp_current
            < next(
                a for a in request.state_snapshot.character_states if a.entity_id == target
            ).hp_current
        )
    assert request == original_request
    assert live.initiative == original_live
    assert live.rng.getstate() == original_rng
    assert observed == []
    assert live.event_log == []
    assert orch._REGISTRY is registry


def test_concurrent_successful_attacks_are_isolated_from_legacy_and_each_other(monkeypatch):
    pc, _, _ = request_and_live(ac=1)
    npc, _, _, loader = npc_case(seed=5)
    loaders = [synthetic_loader(), loader]
    requests = [pc, npc]
    originals = deepcopy(requests)
    with monkeypatch.context() as isolated:
        prohibit_legacy(isolated)
        expected = [execute(r, assets) for r, assets in zip(requests, loaders, strict=True)]
        assert all(r.status == "accepted" for r in expected)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda i: execute(requests[i % 2], loaders[i % 2]), range(16)))
        assert results == [expected[i % 2] for i in range(16)]
    assert requests == originals


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_failure_after_successful_damage_has_no_external_side_effects(monkeypatch, failure):
    request, _, live = request_and_live(ac=1)
    before = deepcopy(request)
    original = evaluation_attack.resolve_activity
    observed = []
    live.event_listeners.append(observed.append)

    def interrupted(activity, ctx, **kwargs):
        original(activity, ctx, **kwargs)
        assert any(e.type == "damage_applied" for e in ctx.event_emitter.__self__.events)
        raise failure("R9 injected failure")

    with monkeypatch.context() as isolated:
        prohibit_legacy(isolated)
        isolated.setattr(evaluation_attack, "resolve_activity", interrupted)
        with pytest.raises(failure, match="R9 injected failure"):
            execute(request)
    assert request == before
    assert live.event_log == observed == []
    assert live.initiative[1].hp_current == 100
    assert RNGState.capture(live.rng) == request.rng_context.state


@pytest.mark.parametrize(
    "seed,expected", [(22, "crit_failure"), (30, "crit_success"), (0, "failure")]
)
@pytest.mark.parametrize("immune", [False, True])
def test_attack_zero_hp_death_save_boundary_matches_independent_legacy_entry(
    seed, expected, immune
):
    request, handle, live, loader = npc_case(seed=seed, hero_hp=1, hero_max=100)
    if immune:
        live.initiative[0] = live.initiative[0].model_copy(
            update={"condition_immunities": ["unconscious"]}
        )
        request = recapture(request, live)
    before = deepcopy(request)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    asyncio.run(orch.submit_player_intent(handle, FOE, request.payload))
    assert result.proposed_events == tuple(live.event_log)
    assert result.rng_transition.next_state == RNGState.capture(live.rng)
    final = apply_delta(request.state_snapshot, result)
    assert final == recapture(request, live).state_snapshot
    saves = [e for e in result.proposed_events if e.type == "death_save_rolled"]
    assert len(saves) == 1
    assert saves[0].outcome == expected
    assert request == before
    if expected == "crit_success":
        assert final.character_states[0].hp_current == 1
        assert [c.condition for c in final.character_states[0].conditions] == ["prone"]


def test_multi_enemy_successive_commits_match_legacy_events_state_and_rng(monkeypatch):
    request, loader = multi_enemy_case()
    # Only the independent Legacy oracle uses the compatibility fixture factory.
    live = evaluation_context.execution_context(request.state_snapshot, loader)
    live.rng = request.rng_context.state.restore()
    handle = orch.CombatHandle(live.handle_id)
    orch._REGISTRY[live.handle_id] = live
    submit = orch.submit_player_intent
    for index, (actor, target) in enumerate(((HERO, FOE), (OTHER, HERO), (HERO, OTHER))):
        request = request.model_copy(
            update={
                "actor_id": actor,
                "command_id": f"r9:sequence:{index}",
                "payload": CombatIntentPayload(
                    intent_type="attack", weapon_id="mace", target_id=target
                ),
            }
        )

        before = deepcopy(request)
        with monkeypatch.context() as isolated:
            prohibit_legacy(isolated)
            result = execute(request, loader)
        assert result.status == "accepted", result.error
        asyncio.run(submit(handle, actor, request.payload))
        after = apply_delta(request.state_snapshot, result)
        assert after == recapture(request, live).state_snapshot
        assert after.combat_state.event_log == tuple(live.event_log)
        assert result.rng_transition.next_state == RNGState.capture(live.rng)
        assert request == before
        assert FOE in after.combat_state.dead_ids
        request = request.model_copy(
            update={
                "state_snapshot": after,
                "rng_context": RNGContext(
                    stream_id="main",
                    version=request.rng_context.version + 1,
                    state=result.rng_transition.next_state,
                ),
            }
        )


@pytest.mark.parametrize("npc", [False, True])
@pytest.mark.parametrize("speed", [0, 30])
@pytest.mark.parametrize("dodging", [False, True])
def test_dodge_uses_speed_not_unspent_movement_and_matches_legacy(npc, speed, dodging):
    if npc:
        request, handle, live, loader = npc_case()
        target_index, actor_id = 0, FOE
    else:
        request, handle, live = request_and_live(ac=1)
        loader = synthetic_loader()
        target_index, actor_id = 1, HERO
    live.initiative[target_index] = live.initiative[target_index].model_copy(
        update={"dodging": dodging, "base_speed": speed, "movement_remaining": 0}
    )
    request = recapture(request, live)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    asyncio.run(orch.submit_player_intent(handle, actor_id, request.payload))
    assert result.proposed_events == tuple(live.event_log)
    assert result.rng_transition.next_state == RNGState.capture(live.rng)
    assert apply_delta(request.state_snapshot, result) == recapture(request, live).state_snapshot
