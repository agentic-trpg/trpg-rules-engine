"""Existing Hold Person: real full-class turns, independent clocks and Legacy parity."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from random import Random

import pytest
from pydantic import ValidationError
from pydantic_core import to_json

from dnd5e_engine import evaluation_effect_turn, live_effect_lifecycle
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, RuleEvaluationResult
from dnd5e_engine.evaluation_delta import ConcentrationUpdate, EffectLifecycleUpdate
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_snapshot import capture_evaluation_snapshot
from dnd5e_engine.evaluation_state import CombatSnapshot
from dnd5e_engine.events import EffectApplied, SaveRolled
from dnd5e_engine.lib_loader import scoped_lib_loader
from dnd5e_engine.specs import GridScene
from tests.evaluation_support import FOE, HERO, WEAPON
from tests.test_stateless_attack import apply_delta, execute
from tests.test_stateless_attack_concentration import ATTACKER, attack_concentration_case
from tests.test_stateless_item_closure_core import forbidden, isolate


def hold_case(*, seed=0, remaining=10, multi=False, source_turn=True, pc=False, hp=40):
    request, handle, live, loader = attack_concentration_case(seed=seed, pc=pc, hp=hp)
    index = 2 if source_turn else 1
    live.current_turn_index = index
    live.current_actor_id = live.initiative[index].entity_id
    live.turn_serial = index + 1
    live.concentration_rounds_remaining[FOE] = remaining
    if multi:
        effect = live.active_effects[HERO][0].model_copy(update={"target_id": ATTACKER})
        with scoped_lib_loader(loader):
            orch._emit(live, EffectApplied(effect=effect))
        live.concentration_chain[FOE].append((ATTACKER, effect.id, effect.origin))
    live.event_log.clear()
    while not live.event_queue.empty():
        live.event_queue.get_nowait()
    snapshot = capture_evaluation_snapshot(
        live, request.state_snapshot, GridScene(width=3, height=3)
    )
    request = request.model_copy(
        update={
            "actor_id": live.current_actor_id,
            "payload": CombatIntentPayload(intent_type="pass"),
            "state_snapshot": snapshot,
        }
    )
    return request, handle, live, loader


def apply_hold(snapshot, result, *, world_version=None):
    """Independent guarded consumer, preserving partial graphs and explicit values."""
    assert result.state_delta.expected_world_version == (
        snapshot.world_version if world_version is None else world_version
    )
    assert result.read_set[0].version == snapshot.world_version
    sources = [o for o in result.state_delta.operations if isinstance(o, ConcentrationUpdate)]
    effects = [o for o in result.state_delta.operations if isinstance(o, EffectLifecycleUpdate)]
    for op in sources:
        actor = next(a for a in snapshot.character_states if a.entity_id == op.source_id)
        assert op.combat_id == snapshot.combat_state.combat_id
        assert actor.concentration_effect_id == op.expected_effect_id
        assert tuple(snapshot.combat_state.concentration_chain[op.source_id]) == op.expected_chain
        assert (
            snapshot.combat_state.concentration_rounds_remaining[op.source_id] == op.expected_rounds
        )
    for op in effects:
        assert op.combat_id == snapshot.combat_state.combat_id
        assert op.expected_effect in snapshot.effect_states
        assert op.expected_lifecycle in snapshot.combat_state.effect_lifecycles
        assert op.expected_lineage in snapshot.combat_state.conditions_by_effect
    mechanical = result.model_copy(
        update={
            "state_delta": result.state_delta.model_copy(
                update={
                    "operations": tuple(
                        o
                        for o in result.state_delta.operations
                        if not isinstance(o, (ConcentrationUpdate, EffectLifecycleUpdate))
                    )
                }
            )
        }
    )
    after = apply_delta(snapshot, mechanical)
    actors = list(after.character_states)
    chains = dict(after.combat_state.concentration_chain)
    rounds = dict(after.combat_state.concentration_rounds_remaining)
    active = {(e.target_id, e.id, e.origin): e for e in after.effect_states}
    clocks = {r.identity: r for r in after.combat_state.effect_lifecycles}
    links = {r.identity: r for r in after.combat_state.conditions_by_effect}
    for op in sources:
        actors = [
            a.model_copy(update={"concentration_effect_id": op.effect_id})
            if a.entity_id == op.source_id
            else a
            for a in actors
        ]
        if op.chain:
            chains[op.source_id] = list(op.chain)
            rounds[op.source_id] = op.rounds
        else:
            chains.pop(op.source_id)
            rounds.pop(op.source_id)
    for op in effects:
        for mapping, value in ((active, op.effect), (clocks, op.lifecycle), (links, op.lineage)):
            if value is None:
                mapping.pop(op.identity)
            else:
                mapping[op.identity] = value
    after = after.model_copy(
        update={
            "character_states": tuple(actors),
            "effect_states": tuple(active.values()),
            "combat_state": after.combat_state.model_copy(
                update={
                    "concentration_chain": chains,
                    "concentration_rounds_remaining": rounds,
                    "effect_lifecycles": tuple(clocks.values()),
                    "conditions_by_effect": tuple(links.values()),
                }
            ),
        }
    )
    return CombatSnapshot.model_validate_json(after.model_dump_json())


def isolated(monkeypatch):
    isolate(monkeypatch)
    for name in ("run_repeats", "expire_at_boundary", "roll_live_save", "forget_effect"):
        monkeypatch.setattr(live_effect_lifecycle, name, forbidden)
    for name in ("_run_end_of_turn_saves", "_hook_concentration_expiry", "_drop_concentration"):
        monkeypatch.setattr(orch, name, forbidden)


def step(request, loader, handle, live, monkeypatch, actor=None, intent=None):
    current = request.model_copy(
        update={
            "actor_id": actor
            or request.state_snapshot.combat_state.initiative_ids[
                request.state_snapshot.combat_state.current_turn_index
            ],
            "payload": intent or CombatIntentPayload(intent_type="pass"),
        }
    )
    original = deepcopy(current)
    offset = len(live.event_log)
    asyncio.run(orch.submit_player_intent(handle, current.actor_id, current.payload))
    with monkeypatch.context() as scoped:
        isolated(scoped)
        result = execute(current, loader)
        assert result.status == "accepted", result.error
        assert execute(current, loader) == result
    after = apply_hold(current.state_snapshot, result)
    expected = capture_evaluation_snapshot(
        live, current.state_snapshot, GridScene(width=3, height=3)
    )
    assert after == expected
    assert result.proposed_events == tuple(live.event_log[offset:])
    assert result.rng_transition.next_state == RNGState.capture(live.rng)
    assert RuleEvaluationResult.model_validate_json(result.model_dump_json()) == result
    assert current == original
    return current.model_copy(
        update={
            "state_snapshot": after,
            "rng_context": current.rng_context.model_copy(
                update={"state": result.rng_transition.next_state}
            ),
        }
    ), result


@pytest.mark.parametrize("pc", [False, True])
@pytest.mark.parametrize("seed", [0, 1, 5, 31])
def test_source_pass_then_correct_target_repeat_full_parity(pc, seed, monkeypatch):
    request, handle, live, loader = hold_case(seed=seed, pc=pc)
    original = request.state_snapshot
    request, source = step(request, loader, handle, live, monkeypatch)
    assert not any(isinstance(e, SaveRolled) for e in source.proposed_events)
    assert not source.rng_transition.state_changed
    assert request.state_snapshot.combat_state.concentration_rounds_remaining == {FOE: 9}
    assert request.state_snapshot.effect_states == original.effect_states
    request, free = step(request, loader, handle, live, monkeypatch)
    assert not any(isinstance(e, SaveRolled) for e in free.proposed_events)
    request, target = step(request, loader, handle, live, monkeypatch)
    save = next(e for e in target.proposed_events if isinstance(e, SaveRolled))
    natural = Random(seed).randint(1, 20)
    assert (save.target_id, save.ability, save.dc, save.modifier, save.natural) == (
        HERO,
        "wis",
        15,
        0,
        natural,
    )
    assert save.succeeded is (natural >= 15)
    kinds = [e.type for e in target.proposed_events]
    assert kinds.index("turn_phase") < kinds.index("save_rolled") < kinds.index("turn_ended")
    if save.succeeded:
        assert request.state_snapshot.effect_states == ()
        assert request.state_snapshot.combat_state.concentration_chain == {}
        assert (
            kinds.index("save_rolled")
            < kinds.index("effect_expired")
            < kinds.index("condition_removed")
        )
        assert "concentration_dropped" not in kinds
    else:
        clock = request.state_snapshot.combat_state.effect_lifecycles[0].state
        assert clock.last_repeat_turn_serial == 5
        assert len(request.state_snapshot.effect_states) == 1
        assert request.state_snapshot.combat_state.concentration_rounds_remaining == {FOE: 9}
    assert request.state_snapshot.resource_state == original.resource_state


@pytest.mark.parametrize("remaining", [1, 2])
@pytest.mark.parametrize("multi", [False, True])
def test_source_duration_cleanup_and_legal_later_attack(remaining, multi, monkeypatch):
    request, handle, live, loader = hold_case(remaining=remaining, multi=multi)
    request, result = step(request, loader, handle, live, monkeypatch)
    if remaining == 2:
        for _ in range(3):
            request, result = step(request, loader, handle, live, monkeypatch)
    after = request.state_snapshot
    assert after.effect_states == ()
    assert not after.combat_state.concentration_chain
    assert not after.combat_state.concentration_rounds_remaining
    assert not after.combat_state.effect_lifecycles
    assert not after.combat_state.conditions_by_effect
    assert all(
        not a.conditions and a.concentration_effect_id is None for a in after.character_states
    )
    assert [e.reason for e in result.proposed_events if e.type == "effect_expired"] == [
        "duration"
    ] * (2 if multi else 1)
    assert not result.rng_transition.state_changed
    assert after.combat_state.initiative_ids[after.combat_state.current_turn_index] == ATTACKER
    request, attack = step(
        request,
        loader,
        handle,
        live,
        monkeypatch,
        intent=CombatIntentPayload(intent_type="attack", weapon_id=WEAPON, target_id=FOE),
    )
    assert any(e.type == "damage_applied" for e in attack.proposed_events)


@pytest.mark.parametrize("multi", [False, True])
def test_multiple_rounds_partial_and_final_success_cleanup(multi, monkeypatch):
    request, handle, live, loader = hold_case(multi=multi)
    saves = []
    partial = False
    for _ in range(20):
        request, result = step(request, loader, handle, live, monkeypatch)
        saves.extend(e.natural for e in result.proposed_events if isinstance(e, SaveRolled))
        if multi and len(request.state_snapshot.effect_states) == 1:
            partial = True
            source = next(a for a in request.state_snapshot.character_states if a.entity_id == FOE)
            assert source.concentration_effect_id is not None
            assert len(request.state_snapshot.combat_state.concentration_chain[FOE]) == 1
        if not request.state_snapshot.effect_states:
            break
    assert saves == ([13, 14, 2, 9, 17, 16] if multi else [13, 14, 2, 9, 17])
    assert partial is multi
    assert not request.state_snapshot.combat_state.concentration_chain
    assert not request.state_snapshot.combat_state.effect_lifecycles
    assert all(not a.conditions for a in request.state_snapshot.character_states)


@pytest.mark.parametrize(
    "intent",
    [
        CombatIntentPayload(intent_type="attack", weapon_id=WEAPON, target_id=FOE),
        CombatIntentPayload(
            intent_type="cast_spell",
            spell_id="hold-person",
            spell_source_id="class:wizard",
            target_id=FOE,
        ),
    ],
)
def test_paralyzed_actor_denied_before_rng(intent, monkeypatch):
    request, _, _, loader = hold_case(source_turn=False)
    request = request.model_copy(update={"payload": intent})
    before = deepcopy(request)
    isolated(monkeypatch)
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("incapacitated RNG"))
    result = execute(request, loader)
    assert result.status == "rejected"
    assert result.error.code == "actor_incapacitated"
    assert result.state_delta is None
    assert not result.proposed_events
    assert request == before


def test_replay_concurrent_stale_fault_and_old_envelope(monkeypatch):
    request, _, live, loader = hold_case(source_turn=False)
    original = deepcopy(request)
    isolated(monkeypatch)
    result = execute(request, loader)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(lambda _: execute(request, loader), range(8))) == [result] * 8
    after = apply_hold(request.state_snapshot, result)
    with pytest.raises(AssertionError):
        apply_hold(after, result)
    with pytest.raises(AssertionError):
        apply_hold(request.state_snapshot, result, world_version=8)
    monkeypatch.setattr(
        evaluation_effect_turn,
        "resolve_snapshot_save",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("save fault")),
    )
    with pytest.raises(RuntimeError, match="save fault"):
        execute(request, loader)
    assert request == original
    assert RNGState.capture(live.rng) == request.rng_context.state
    value = request.model_dump(mode="python")
    value["schema_version"] = "engine-evaluation/17"
    with pytest.raises(ValidationError):
        type(request).model_validate(value)


def test_actual_source_death_cleans_graph_then_released_target_passes(monkeypatch):
    request, handle, live, loader = attack_concentration_case(hp=1)
    request, result = step(
        request, loader, handle, live, monkeypatch, actor=ATTACKER, intent=request.payload
    )
    assert FOE in request.state_snapshot.combat_state.dead_ids
    assert not request.state_snapshot.effect_states
    assert any(e.type == "death" for e in result.proposed_events)
    request, _ = step(request, loader, handle, live, monkeypatch)
    assert (
        request.state_snapshot.combat_state.initiative_ids[
            request.state_snapshot.combat_state.current_turn_index
        ]
        == ATTACKER
    )


@pytest.mark.parametrize(
    "mutation", ["lineage", "clock", "duration", "source", "condition", "grant"]
)
def test_incomplete_hold_graph_refuses_before_rng(mutation, monkeypatch):
    request, _, _, loader = hold_case()
    value = request.state_snapshot.model_dump(mode="python")
    state = value["combat_state"]
    if mutation == "lineage":
        state["conditions_by_effect"] = ()
    elif mutation == "clock":
        state["effect_lifecycles"] = ()
    elif mutation == "duration":
        state["concentration_rounds_remaining"][FOE] = 11
    elif mutation == "source":
        value["effect_states"][0]["lifecycle"]["source_id"] = ATTACKER
    elif mutation == "condition":
        value["character_states"][1]["conditions"][0]["source_effect_id"] = "wrong"
    else:
        value["character_states"][2]["granted_features"] = ()
    request = request.model_copy(
        update={"state_snapshot": CombatSnapshot.model_validate_json(to_json(value))}
    )
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("invalid graph RNG"))
    result = execute(request, loader)
    assert result.status == "unsupported"
    assert result.state_delta is None
    assert result.rng_transition is None


@pytest.mark.parametrize(
    "change", ["increase", "replace", "reorder", "duplicate", "partial_null", "zero"]
)
def test_concentration_delta_contract_is_closed(change):
    request, _, _, _ = hold_case(multi=True)
    chain = tuple(request.state_snapshot.combat_state.concentration_chain[FOE])
    value = dict(
        kind="combat.concentration_update",
        combat_id="combat:synthetic",
        source_id=FOE,
        expected_effect_id=chain[0][1],
        expected_chain=chain,
        expected_rounds=10,
        effect_id=chain[0][1],
        chain=chain,
        rounds=9,
    )
    if change == "increase":
        value["rounds"] = 11
    elif change == "replace":
        value["chain"] = (("other", chain[0][1], chain[0][2]),)
    elif change == "reorder":
        value["chain"] = tuple(reversed(chain))
    elif change == "duplicate":
        value["chain"] = (chain[0], chain[0])
    elif change == "partial_null":
        value["effect_id"] = None
    else:
        value["rounds"] = 0
    with pytest.raises(ValidationError):
        ConcentrationUpdate.model_validate(value)
