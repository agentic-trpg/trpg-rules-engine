"""Public monster and reaction execution restore paid state on unexpected defects."""

from __future__ import annotations

import copy

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader

from dnd5e_engine import orchestrator as orch
from dnd5e_engine.events import CastFailed, DamageApplied, SaveRolled, SpellCast
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from tests.c21_support import act, combatant, events, foe, start
from tests.e2e.harness import run_async
from tests.test_monster_spell_delivery import _spell_setup
from tests.test_reaction_runtime import ATTACKER, REACTOR, arm, attacker, reactor


@pytest.fixture(autouse=True)
def loader():
    bundled = BundledAssetLoader()
    set_lib_loader_for_tests(bundled)
    yield bundled
    set_lib_loader_for_tests(None)


def _snapshot(live):
    excluded = {"event_queue", "event_listeners", "rng", "topology", "lifecycle", "ruleset_loader"}
    return copy.deepcopy(
        (
            {key: value for key, value in vars(live).items() if key not in excluded},
            live.lifecycle._hooks,
            live.rng.getstate(),
            list(live.event_queue._queue),
        )
    )


@pytest.mark.parametrize("legendary", [False, True])
def test_public_monster_cast_defect_restores_complete_state_rng_and_events(
    loader, monkeypatch, legendary
):
    import dnd5e_engine.timed_activities as timing

    live, actor, _, monster, action, spell = _spell_setup(loader, enemies=((6, 5),))
    if legendary:
        action = action.model_copy(update={"legendary_cost": 1})
        monster = monster.model_copy(update={"legendary_actions": [action]})
        actor.legendary_actions_max = 1
        actor.legendary_actions_remaining = 1
        live.last_ended_turn = (live.round_number, "char:0")
    # This fixture finishes assembling the synthetic ruleset after combat hydration.
    live.ruleset_loader = MemoryAssetLoader(monsters=[monster], spells=[spell])
    observed = []
    live.event_listeners.append(observed.append)
    ruleset = live.ruleset_loader
    before = _snapshot(live)
    original = timing.resolve_spell_activities

    def fail_after_resolution(live_arg, spell_arg, ctx, **kwargs):
        assert live_arg is live
        if legendary:
            assert combatant(live, actor.entity_id).legendary_actions_remaining == 0
            assert live.legendary_windows_used
        else:
            assert not combatant(live, actor.entity_id).action_available
        original(live_arg, spell_arg, ctx, **kwargs)
        assert events(live, DamageApplied)
        assert live.rng.getstate() != before[2]
        raise RuntimeError("injected monster spell defect")

    monkeypatch.setattr(timing, "resolve_spell_activities", fail_after_resolution)
    with pytest.raises(RuntimeError, match="injected monster spell defect"):
        run_async(
            orch.advance_monster_turn(
                orch.CombatHandle(live.handle_id), legendary=legendary, actor_id=actor.entity_id
            )
        )
    assert _snapshot(live) == before
    assert live.ruleset_loader is ruleset
    assert observed == []


@pytest.mark.parametrize("error", [ValueError, RuntimeError])
@pytest.mark.parametrize("parent", ["pc", "monster"])
def test_public_counterspell_defect_restores_both_casters_and_pending_declaration(
    monkeypatch, error, parent
):
    import dnd5e_engine.live_spell_delivery as delivery

    if parent == "pc":
        handle, live = start([reactor(), attacker()], seed=7)
        caster_id = ATTACKER
    else:
        handle, live = start(
            [reactor()], seed=1, encounter=[foe(monster_template_slug="mage", zone_id="2,0")]
        )
        caster_id = "mon:foe"
    arm(handle, REACTOR, "counterspell")
    observed = []
    live.event_listeners.append(observed.append)
    ruleset = live.ruleset_loader
    before = _snapshot(live)
    original = delivery.execute_activity_delivery

    def fail_after_reaction(live_arg, ctx, activity, plan, spec, source_id):
        assert source_id == "counterspell"
        assert live_arg is live
        assert not combatant(live, caster_id).action_available
        assert not combatant(live, REACTOR).reaction_available
        assert not live.pending_reactions
        assert live.spell_slots_by_entity[REACTOR][3] == 1
        original(live_arg, ctx, activity, plan, spec, source_id)
        assert events(live, SaveRolled)
        assert live.rng.getstate() != before[2]
        raise error("injected reaction spell defect")

    def trigger_cast():
        if parent == "pc":
            act(
                handle,
                ATTACKER,
                intent_type="cast_spell",
                spell_id="magic-missile",
                target_id=REACTOR,
            )
        else:
            run_async(orch.advance_monster_turn(handle))

    monkeypatch.setattr(delivery, "execute_activity_delivery", fail_after_reaction)
    with pytest.raises(error, match="injected reaction spell defect"):
        trigger_cast()
    assert _snapshot(live) == before
    assert live.ruleset_loader is ruleset
    assert observed == []


def test_legal_counterspell_commits_action_and_reaction_costs_and_replays():
    def run():
        handle, live = start([reactor(), attacker()], seed=7)
        arm(handle, REACTOR, "counterspell")
        observed = []
        live.event_listeners.append(observed.append)
        event_count = len(live.event_log)
        act(
            handle,
            ATTACKER,
            intent_type="cast_spell",
            spell_id="magic-missile",
            target_id=REACTOR,
        )
        assert [event.reason for event in events(live, CastFailed)] == ["countered"]
        assert not combatant(live, ATTACKER).action_available
        assert not combatant(live, REACTOR).reaction_available
        assert live.spell_slots_by_entity[REACTOR][3] == 1
        assert live.spell_slots_by_entity[ATTACKER][1] == 2
        assert not live.pending_reactions
        assert not events(live, DamageApplied)
        assert [event.spell_id for event in events(live, SpellCast)] == ["counterspell"]
        assert observed == live.event_log[event_count:]
        state, _, rng, queued = _snapshot(live)
        return state, rng, queued

    assert run() == run()
