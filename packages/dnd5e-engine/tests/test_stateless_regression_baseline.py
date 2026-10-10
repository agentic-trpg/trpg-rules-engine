"""Legacy mechanical baselines, independent of future evaluation contracts."""

import asyncio
import random
from copy import deepcopy

import pytest

from dnd5e_engine import orchestrator as orch
from dnd5e_engine.events import AttackFailed, AttackRolled, DamageApplied, IntentSubmitted
from tests.evaluation_support import FOE, HERO, WEAPON, synthetic_combat
from tests.test_execution_integrity import snapshot


def attack(handle, **fields):
    asyncio.run(
        orch.submit_player_intent(
            handle,
            HERO,
            orch.PlayerIntent(intent_type="attack", weapon_id=WEAPON, target_id=FOE, **fields),
        )
    )


@pytest.mark.parametrize("seed,ac", [(0, 1), (1, 99), (5, 1), (31, 1)])
def test_original_attack_hit_miss_crit_costs_and_explicit_rng(seed, ac):
    handle, live = synthetic_combat(seed=seed, ac=ac, temp_hp=2)
    initial = live.rng.getstate()
    replay = random.Random()
    replay.setstate(initial)
    natural = replay.randint(1, 20)
    attack(handle)
    rolled = next(e for e in live.event_log if isinstance(e, AttackRolled))
    assert rolled.natural == natural
    assert rolled.is_crit == (natural == 20)
    damage = [e for e in live.event_log if isinstance(e, DamageApplied)]
    assert bool(damage) == (natural == 20 or (natural != 1 and rolled.roll_total >= ac))
    assert isinstance(live.event_log[0], IntentSubmitted)
    actor = next(c for c in live.initiative if c.entity_id == HERO)
    assert not actor.action_available
    assert actor.attack_rolls_made_this_turn == 1
    assert live.rng.getstate() != initial
    target = next(c for c in live.initiative if c.entity_id == FOE)
    amount = sum(e.amount for e in damage)
    assert target.temp_hp == max(0, 2 - amount)
    assert target.hp_current == 100 - max(0, amount - 2)


def test_invalid_attack_rejects_before_payment_and_rng():
    handle, live = synthetic_combat()
    live.actor_zone[FOE] = "2,2"
    initial_rng = live.rng.getstate()
    initial_actors = deepcopy(live.initiative)
    attack(handle)
    assert [type(e) for e in live.event_log] == [AttackFailed]
    assert live.rng.getstate() == initial_rng
    assert live.initiative == initial_actors


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_original_attack_exception_rolls_back_every_sidecar(monkeypatch, failure):
    handle, live = synthetic_combat()
    before = snapshot(live)
    emitted = []
    live.event_listeners.append(emitted.append)
    original = orch.resolve_activity

    def broken(activity, ctx, **kwargs):
        original(activity, ctx, **kwargs)
        raise failure("synthetic fault after resolution")

    monkeypatch.setattr(orch, "resolve_activity", broken)
    with pytest.raises(failure, match="synthetic fault"):
        attack(handle)
    assert snapshot(live) == before
    assert emitted == []


def test_original_attack_replay_matches_events_mechanics_and_rng():
    def run():
        handle, live = synthetic_combat(seed=5)
        attack(handle)
        return live.initiative, live.event_log, live.rng.getstate()

    assert run() == run()
