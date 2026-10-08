"""Public execution failures restore the complete authoritative combat transaction."""

import asyncio
from copy import deepcopy

import pytest
from dnd5e_srd_data import BundledAssetLoader

from dnd5e_engine import live_spell_delivery as delivery
from dnd5e_engine import orchestrator as orch
from dnd5e_engine import timed_activities as timed
from dnd5e_engine.events import CastFailed, DamageApplied, HealingApplied, SaveRolled, SpellCast
from dnd5e_engine.feature_runtime import FeaturePreflightError
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from tests.c21_support import act, combatant, events, foe, monster_turn, pc, start

HERO = "char:hero"
FOE = "mon:foe"


@pytest.fixture(autouse=True)
def _loader():
    set_lib_loader_for_tests(BundledAssetLoader())
    yield
    set_lib_loader_for_tests(None)


def snapshot(live):
    excluded = {"event_queue", "event_listeners", "rng", "topology", "ruleset_loader", "lifecycle"}
    return (
        deepcopy({key: value for key, value in vars(live).items() if key not in excluded}),
        live.rng.getstate(),
        list(live.event_queue._queue),
        {
            phase: [(key, hook.__qualname__) for key, hook in hooks]
            for phase, hooks in live.lifecycle._hooks.items()
        },
    )


def setup():
    return start(
        [
            pc(
                class_slug="cleric",
                classes={"cleric": 5, "fighter": 2},
                character_level=7,
                wisdom=18,
                hp_current=400,
                hp_max=500,
                spells_known=["bless", "healing-word", "fireball"],
                spell_slots={1: 3, 3: 2},
                equipment=("longsword", "wand-of-fireballs", "potion-of-healing"),
            )
        ],
        seed=19,
        encounter=[foe(dexterity=-30)],
    )


CASES = [
    {"intent_type": "cast_spell", "spell_id": "bless", "slot_level": 1, "target_id": HERO},
    {"intent_type": "cast_spell", "spell_id": "healing-word", "slot_level": 1, "target_id": HERO},
    {"intent_type": "use_item", "item_id": "potion-of-healing", "target_id": HERO},
    {"intent_type": "use_item", "item_id": "wand-of-fireballs", "target_zone_id": "5,5"},
    {"intent_type": "use_feature", "feature_id": "second-wind"},
    {"intent_type": "attack", "weapon_id": "longsword", "target_id": FOE},
]


@pytest.mark.parametrize(
    "intent", CASES, ids=["action-spell", "bonus-spell", "item", "delegated", "feature", "attack"]
)
@pytest.mark.parametrize("error_type", [ValueError, RuntimeError, asyncio.CancelledError])
def test_post_payment_failure_restores_all_live_fields_rng_events_and_listeners(
    monkeypatch, intent, error_type
):
    handle, live = setup()
    before = snapshot(live)
    observed = []
    live.event_listeners.append(observed.append)
    original_rng = live.rng
    original = delivery.resolve_activity
    reached = []

    def broken(activity, ctx, **kwargs):
        # Run real resolution first: payment, effects/damage and RNG have already happened.
        original(activity, ctx, **kwargs)
        reached.append(activity.kind)
        assert (
            combatant(live).action_available is False
            or combatant(live).bonus_action_available is False
        )
        ctx.rng.randint(1, 20)
        ctx.event_emitter(HealingApplied(target_id=HERO, amount=1))
        raise error_type("injected post-payment failure")

    monkeypatch.setattr(delivery, "resolve_activity", broken)
    monkeypatch.setattr(timed, "resolve_activity", broken)
    monkeypatch.setattr(orch, "resolve_activity", broken)
    with pytest.raises(error_type, match="injected post-payment failure"):
        act(handle, HERO, **intent)
    assert reached
    assert snapshot(live) == before
    assert live.rng is original_rng
    assert observed == []
    assert live.event_listeners == [observed.append]


def test_successful_spell_failed_save_costs_and_replay_are_preserved():
    def run():
        handle, live = setup()
        act(
            handle,
            HERO,
            intent_type="cast_spell",
            spell_id="fireball",
            slot_level=3,
            target_zone_id="1,0",
        )
        assert live.spell_slots_by_entity[HERO][3] == 1
        assert events(live, SpellCast)
        assert any(not event.succeeded for event in events(live, SaveRolled))
        assert events(live, DamageApplied)
        assert not events(live, CastFailed)
        return snapshot(live)

    assert run() == run()


@pytest.mark.parametrize("error_type", [RuntimeError, FeaturePreflightError])
def test_post_commit_observer_error_preserves_all_published_events_and_paid_costs(error_type):
    handle, live = setup()
    before = list(live.event_queue._queue)
    offset = len(live.event_log)

    def broken_observer(event):
        raise error_type("observer failure after commit")

    live.event_listeners.append(broken_observer)
    with pytest.raises(error_type, match="observer failure after commit"):
        act(handle, HERO, intent_type="use_feature", feature_id="second-wind")
    assert live.custom_counters_by_entity[HERO]["feature_use:second-wind"]["spent"] == 1
    assert events(live, HealingApplied)
    assert not events(live, CastFailed)
    assert list(live.event_queue._queue) == before + live.event_log[offset:]


def test_retry_after_rolled_back_delegated_cast_matches_clean_replay(monkeypatch):
    original = timed.resolve_activity
    handle, live = setup()

    def broken(activity, ctx):
        original(activity, ctx)
        raise RuntimeError("injected child failure")

    with monkeypatch.context() as patch:
        patch.setattr(timed, "resolve_activity", broken)
        with pytest.raises(RuntimeError, match="injected child failure"):
            act(
                handle,
                HERO,
                intent_type="use_item",
                item_id="wand-of-fireballs",
                target_zone_id="1,0",
            )
    act(handle, HERO, intent_type="use_item", item_id="wand-of-fireballs", target_zone_id="1,0")
    actual = snapshot(live)
    clean_handle, clean = setup()
    act(
        clean_handle,
        HERO,
        intent_type="use_item",
        item_id="wand-of-fireballs",
        target_zone_id="1,0",
    )
    assert events(live, SaveRolled)
    assert events(live, DamageApplied)
    assert live.custom_counters_by_entity[HERO]["item_use:wand-of-fireballs"]["spent"] == 1
    assert actual == snapshot(clean)


def test_failure_restores_populated_concentration_timed_work_and_persistent_areas(monkeypatch):
    second = "char:second"
    handle, live = start(
        [
            pc(class_slug="cleric", character_level=5, wisdom=18, spell_slots={3: 2}),
            pc(
                second,
                initiative=10,
                zone_id="0,1",
                class_slug="wizard",
                classes={"wizard": 17, "fighter": 2},
                character_level=19,
                intelligence=18,
                spells_known=["weird", "bless"],
                spell_slots={1: 2, 9: 2},
            ),
        ],
        seed=19,
        encounter=[foe()],
    )
    orch._update_combatant(live, FOE, wisdom=-30)
    act(handle, HERO, intent_type="cast_spell", spell_id="spirit-guardians", slot_level=3)
    act(handle, second, intent_type="cast_spell", spell_id="weird", slot_level=9, target_id=FOE)
    act(handle, second, intent_type="pass")
    monster_turn(handle)
    act(handle, HERO, intent_type="pass")
    assert live.persistent_areas.areas
    assert live.timed_activities.pending
    assert live.concentration_chain[HERO]
    assert live.concentration_chain[second]
    before = snapshot(live)
    original = timed.resolve_activity

    def broken(activity, ctx):
        original(activity, ctx)
        # The new legal concentration cast has already removed the old timed spell.
        assert not live.timed_activities.pending
        orch._drop_concentration(live, HERO)
        assert not live.persistent_areas.areas
        raise RuntimeError("injected lifecycle failure")

    monkeypatch.setattr(timed, "resolve_activity", broken)
    with pytest.raises(RuntimeError, match="injected lifecycle failure"):
        act(
            handle,
            second,
            intent_type="cast_spell",
            spell_id="bless",
            slot_level=1,
            target_id=second,
        )
    assert snapshot(live) == before
