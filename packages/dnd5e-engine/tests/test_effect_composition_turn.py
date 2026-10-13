"""R40 continuous typed-state lifecycle; independent expected phases and RNG."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from dnd5e_engine import evaluation_effect_turn
from dnd5e_engine.evaluation_composition import ComposedEffectComputation
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, RuleEvaluationRequest
from dnd5e_engine.evaluation_rng import RNGState
from tests.effect_composition_support import (
    SECOND_TARGET,
    apply_composed,
    composed_case,
    continued,
)
from tests.evaluation_support import FOE, HERO, WEAPON
from tests.test_stateless_attack import execute
from tests.test_stateless_attack_concentration import ATTACKER
from tests.test_stateless_hold_turn import isolated


def verified_step(request, loader):
    request = RuleEvaluationRequest.model_validate_json(request.model_dump_json())
    original = deepcopy(request)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    result.verify_request(request)
    after = apply_composed(request.state_snapshot, result)
    if request.payload.intent_type == "pass":
        assert after.resource_state == request.state_snapshot.resource_state
        assert (
            after.combat_state.expended_resources
            == request.state_snapshot.combat_state.expended_resources
        )
    assert request == original
    next_request = continued(request, result)
    assert next_request.state_snapshot.world_version == request.state_snapshot.world_version + 1
    assert next_request.rng_context.version == request.rng_context.version + 1
    return next_request, result, after


def test_actual_attack_pass_partial_and_all_target_release_independent_shield_expiry(monkeypatch):
    request, loader, _ = composed_case(seed=41, multi=True)
    assert len(request.state_snapshot.combat_state.effect_lifecycles) == 2
    isolated(monkeypatch)
    request, attack, after = verified_step(request, loader)
    assert next(e for e in attack.proposed_events if e.type == "attack_rolled").is_hit is False
    assert any(e.id == "effect:imperceptible_barrier" for e in after.effect_states)
    assert len(after.combat_state.effect_lifecycles) == 2
    initial_rounds = after.combat_state.concentration_rounds_remaining[FOE]
    expiries = []
    partial = False
    for _ in range(24):
        before = request.state_snapshot
        current = request.actor_id
        rng = request.rng_context.state.restore()
        clocks = {c.identity: c for c in before.combat_state.effect_lifecycles}
        own = [c for c in clocks.values() if c.identity[0] == current]
        expected_roll = rng.randint(1, 20) if own else None
        request, result, after = verified_step(request, loader)
        saves = [e for e in result.proposed_events if e.type == "save_rolled"]
        assert len(saves) == len(own)
        if own:
            assert saves[0].target_id == current
            assert saves[0].natural == expected_roll
            assert saves[0].roll_total == expected_roll
            assert saves[0].dc == 15
            assert saves[0].succeeded is (expected_roll >= 15)
            surviving = {c.identity: c for c in after.combat_state.effect_lifecycles}
            for identity, old in clocks.items():
                if identity[0] != current:
                    assert surviving[identity] == old
                elif saves[0].succeeded:
                    assert identity not in surviving
                else:
                    assert (
                        surviving[identity].state.last_repeat_turn_serial
                        == before.combat_state.turn_serial
                    )
        assert result.rng_transition.next_state == RNGState.capture(rng)
        expiries.extend(e for e in result.proposed_events if e.type == "effect_expired")
        partial |= len(after.combat_state.effect_lifecycles) == 1
        if current == SECOND_TARGET and before.combat_state.round_number == 1:
            kinds = [e.type for e in result.proposed_events]
            start_phase = next(
                i
                for i, e in enumerate(result.proposed_events)
                if e.type == "turn_phase" and e.phase == "turn_start"
            )
            shield_expiry = next(
                i
                for i, e in enumerate(result.proposed_events)
                if e.type == "effect_expired" and e.effect_id == "effect:imperceptible_barrier"
            )
            assert kinds[start_phase] == "turn_phase"
            assert start_phase < shield_expiry
            assert len(after.combat_state.effect_lifecycles) == 2
            assert after.combat_state.concentration_rounds_remaining[FOE] == initial_rounds
            owner = next(a for a in after.character_states if a.entity_id == FOE)
            assert owner.action_available
            assert owner.reaction_available
            assert not after.combat_state.active_reaction_responses
            assert not after.combat_state.reaction_effects_pending_expiry
        if current == FOE and after.combat_state.concentration_chain:
            assert (
                after.combat_state.concentration_rounds_remaining[FOE]
                == before.combat_state.concentration_rounds_remaining[FOE] - 1
            )
            assert after.combat_state.round_number == before.combat_state.round_number + 1
        if not after.combat_state.concentration_chain:
            break
    assert partial
    assert not after.combat_state.concentration_chain
    assert not after.effect_states
    assert not after.combat_state.effect_lifecycles
    assert not after.combat_state.conditions_by_effect
    assert len([e for e in expiries if e.effect_id == "effect:imperceptible_barrier"]) == 1
    assert len([e for e in expiries if e.reason == "save_succeeded"]) == 2
    for _ in range(2):
        request, result, _ = verified_step(request, loader)
        assert not any(
            e.type in {"effect_expired", "save_rolled", "concentration_dropped"}
            for e in result.proposed_events
        )
        assert result.stage_facts.coverage == "complete"


def test_real_caster_turn_cap_expires_without_per_call_or_per_event_decrement(monkeypatch):
    request, loader, _ = composed_case(seed=78, multi=True, target_wisdom=1)
    isolated(monkeypatch)
    request, _, _ = verified_step(request, loader)
    caster_ends = 0
    saves = 0
    expiries = []
    for _ in range(36):
        before = request.state_snapshot
        current = request.actor_id
        remaining = before.combat_state.concentration_rounds_remaining[FOE]
        request, result, after = verified_step(request, loader)
        for event in result.proposed_events:
            if event.type == "save_rolled":
                saves += 1
                assert event.roll_total == event.natural - 5
                assert not event.succeeded
            elif event.type == "effect_expired":
                expiries.append(event)
        if current == FOE:
            caster_ends += 1
            if remaining == 1:
                assert not after.combat_state.concentration_chain
                break
            assert after.combat_state.concentration_rounds_remaining[FOE] == remaining - 1
        else:
            assert after.combat_state.concentration_rounds_remaining[FOE] == remaining
        assert not any(e.type == "concentration_check" for e in result.proposed_events)
    assert caster_ends == 9
    assert saves == 18
    assert not after.effect_states
    assert not after.combat_state.conditions_by_effect
    assert (
        len([e for e in expiries if e.effect_id == "effect:paralyzed" and e.reason == "duration"])
        == 2
    )
    assert len([e for e in expiries if e.effect_id == "effect:imperceptible_barrier"]) == 1


def test_subsequent_attack_and_pending_shield_survive_plain_turns():
    request, loader, _ = composed_case(seed=7)
    request, first, _ = verified_step(request, loader)
    assert not any(e.type == "reaction_triggered" for e in first.proposed_events)
    original_pending = request.state_snapshot.combat_state.pending_reactions
    request, _, _ = verified_step(request, loader)
    request, _, after = verified_step(request, loader)
    assert request.actor_id == ATTACKER
    assert after.combat_state.pending_reactions == original_pending
    request = request.model_copy(
        update={
            "payload": CombatIntentPayload(intent_type="attack", weapon_id=WEAPON, target_id=FOE)
        }
    )
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    apply_composed(request.state_snapshot, result)


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
def test_error_after_real_repeat_save_does_not_publish_or_mutate(error, monkeypatch):
    request, loader, _ = composed_case(seed=41, multi=True)
    request, _, _ = verified_step(request, loader)
    original = deepcopy(request)
    resolve = evaluation_effect_turn.resolve_snapshot_save
    seen = []

    def fail(*args, **kwargs):
        seen.append(resolve(*args, **kwargs))
        raise error("after repeat save")

    monkeypatch.setattr(evaluation_effect_turn, "resolve_snapshot_save", fail)
    with pytest.raises(error, match="after repeat save"):
        execute(request, loader)
    assert len(seen) == 1
    assert request == original


@pytest.mark.parametrize("caster", [False, True])
def test_one_actual_turn_end_boundary_cannot_repeat_its_save_or_cap(caster):
    request, loader, _ = composed_case(seed=41, multi=True)
    rng = request.rng_context.state.restore()
    computation = ComposedEffectComputation(request.state_snapshot, rng, loader)
    actor = FOE if caster else HERO
    computation.concentration.turn_end(actor)
    state, events, successor = (
        deepcopy(computation.state),
        tuple(computation.events),
        RNGState.capture(rng),
    )
    computation.concentration.turn_end(actor)
    assert computation.state == state
    assert tuple(computation.events) == events
    assert RNGState.capture(rng) == successor


@pytest.mark.parametrize("pc", [False, True])
def test_actual_death_or_dying_cleanup_can_continue_without_repeating_hold_cleanup(pc):
    request, loader, _ = composed_case(seed=9, pc=pc, hp=1)
    request, first, after = verified_step(request, loader)
    assert not after.combat_state.concentration_chain
    assert any(e.type == "concentration_dropped" for e in first.proposed_events)
    request, result, after = verified_step(request, loader)
    assert not any(e.type == "concentration_dropped" for e in result.proposed_events)
    assert not any(
        e.type == "effect_expired" and e.effect_id != "effect:imperceptible_barrier"
        for e in result.proposed_events
    )
    if pc:
        assert not after.effect_states
        assert len([e for e in result.proposed_events if e.type == "death_save_rolled"]) == 1
    else:
        assert request.actor_id == ATTACKER
        assert len(after.effect_states) == 1
        assert not after.combat_state.active_reaction_responses
        assert FOE in after.combat_state.dead_ids


def test_interleaved_mixed_pass_is_deterministic_and_old_whole_delta_is_stale():
    request, loader, _ = composed_case(seed=41, multi=True)
    request, _, _ = verified_step(request, loader)
    other = request.model_copy(
        update={
            "session_id": "session:other",
            "state_snapshot": request.state_snapshot.model_copy(
                update={"session_id": "session:other"}
            ),
        }
    )
    expected = (execute(request, loader), execute(other, loader))
    with ThreadPoolExecutor(max_workers=4) as pool:
        actual = list(pool.map(lambda i: execute((request, other)[i % 2], loader), range(8)))
    assert actual == [expected[i % 2] for i in range(8)]
    next_request = continued(request, expected[0])
    with pytest.raises(AssertionError):
        apply_composed(next_request.state_snapshot, expected[0])
