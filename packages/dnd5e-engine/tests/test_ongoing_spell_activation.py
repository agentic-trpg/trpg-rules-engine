"""Public ongoing Magic actions and moving environmental source acceptance."""

import asyncio
from copy import deepcopy

import pytest
from dnd5e_srd_data import BundledAssetLoader
from pydantic import ValidationError

from dnd5e_engine import orchestrator as orch
from dnd5e_engine.events import CastFailed, DamageApplied, EffectApplied, SaveRolled, SpellCast
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.specs import GridScene
from tests.c21_support import act, combatant, events, foe, pc
from tests.test_execution_integrity import snapshot
from tests.test_spell_effect_selection import take_turn

HERO = "char:hero"
FOE = "mon:foe"
INITIAL = "o3kffaumfNGjVvg9"
REPEAT = "Y0cJvZuD7EfwGqkf"


@pytest.fixture(autouse=True)
def loader():
    set_lib_loader_for_tests(BundledAssetLoader())
    yield
    set_lib_loader_for_tests(None)


def table(**kwargs):
    result = asyncio.run(
        orch.start_combat(
            session_id="ongoing",
            party=[
                pc(
                    class_slug="sorcerer",
                    character_level=13,
                    charisma=20,
                    spells_known=["sunbeam", "fog-cloud", "moonbeam"],
                    spell_slots={1: 2, 2: 2, 6: 2, 7: 2},
                    **kwargs,
                )
            ],
            encounter=[foe(zone_id="10,0")],
            grid_scene=GridScene(width=40, height=30, default_lighting="dark"),
            rng_seed=19,
        )
    )
    live = orch._get_live(result.handle)
    orch._update_combatant(live, FOE, constitution=-30)
    return result.handle, live


def cast(handle, level=6):
    act(
        handle,
        HERO,
        intent_type="cast_spell",
        spell_id="sunbeam",
        slot_level=level,
        direction=(1, 0),
    )


def activate(handle, source="area:0", activity=REPEAT, **kwargs):
    act(
        handle,
        HERO,
        intent_type="activate_spell",
        source_id=source,
        activity_id=activity,
        direction=(1, 0),
        **kwargs,
    )


def test_initial_beam_registers_moving_sunlight_and_keeps_beam_payload():
    handle, live = table()
    cast(handle)
    assert len(events(live, SaveRolled)) == len(events(live, DamageApplied)) == 1
    assert len(live.persistent_areas.areas) == 1
    assert live.topology.light_on_cell("6,0") == "bright"
    assert live.topology.light_on_cell("7,0") == "dim"
    assert live.topology.sunlight_on_cell("12,0")
    assert not live.topology.sunlight_on_cell("13,0")


def test_public_repeat_pays_action_without_slot_cast_or_concentration_reset():
    handle, live = table()
    cast(handle)
    take_turn(live, HERO)
    before_slots = dict(live.spell_slots_by_entity[HERO])
    before_duration = dict(live.concentration_rounds_remaining)
    activate(handle)
    assert len(events(live, SaveRolled)) == len(events(live, DamageApplied)) == 2
    assert len(events(live, SpellCast)) == 1
    assert not combatant(live).action_available
    assert live.spell_slots_by_entity[HERO] == before_slots
    assert live.concentration_rounds_remaining == before_duration
    assert len(live.persistent_areas.areas) == 1
    view = orch.get_live(handle).ongoing_spells[0]
    assert (
        view.source_id,
        view.owner_id,
        view.activation.activity_ids,
        view.slot_level,
        view.save_dc,
    ) == ("area:0", HERO, (REPEAT,), 6, 18)


@pytest.mark.parametrize(
    "payload",
    [
        {"source_id": None},
        {"source_id": "unknown"},
        {"activity_id": INITIAL},
        {"activity_id": "unknown"},
        {"activity_id": None},
        {"direction": None},
        {"direction": (0, 0)},
        {"target_id": "unknown"},
        {"target_id": HERO},
        {"target_ids": [FOE, FOE]},
        {"target_zone_id": "99,0"},
        {"slot_level": 7},
        {"spell_id": "sunbeam"},
        {"item_id": "wand-of-fireballs"},
        {"charges_to_spend": 2},
    ],
)
def test_invalid_activation_is_rejected_before_all_state_events_and_rng(payload):
    handle, live = table()
    cast(handle)
    take_turn(live)
    before = snapshot(live), deepcopy(vars(live.topology))
    intent = dict(
        intent_type="activate_spell", source_id="area:0", activity_id=REPEAT, direction=(1, 0)
    )
    intent.update(payload)
    with pytest.raises((orch.IntentRejectedError, ValidationError)):
        act(handle, HERO, **intent)
    assert (snapshot(live), vars(live.topology)) == before


@pytest.mark.parametrize(
    "mode", ["no_action", "surge_only", "incapacitated", "wrong_turn", "expired", "rage"]
)
def test_activation_obeys_live_action_condition_turn_and_source_gates(mode):
    handle, live = table()
    cast(handle)
    take_turn(live)
    if mode in ("no_action", "surge_only"):
        orch._update_combatant(
            live, HERO, action_available=False, extra_actions_remaining=int(mode == "surge_only")
        )
    elif mode == "incapacitated":
        from dnd5e_engine.events import ConditionApplied

        orch._emit(live, ConditionApplied(target_id=HERO, condition="stunned"))
    elif mode == "wrong_turn":
        live.current_turn_index = 1
    elif mode == "expired":
        act(handle, HERO, intent_type="drop_concentration")
    else:
        # Rage's normal lifecycle forbids maintaining Concentration.
        orch._drop_concentration(live, HERO)
    before = snapshot(live)
    with pytest.raises(orch.IntentRejectedError):
        activate(handle)
    assert snapshot(live) == before


def test_activation_uses_base_action_and_preserves_restricted_surge_action():
    handle, live = table()
    cast(handle)
    take_turn(live)
    orch._update_combatant(live, HERO, extra_actions_remaining=1)
    activate(handle)
    assert not combatant(live).action_available
    assert combatant(live).extra_actions_remaining == 1


@pytest.mark.parametrize("level", [6, 7])
def test_reactivation_keeps_original_slot_dc_and_six_damage_dice(level):
    handle, live = table()
    cast(handle, level)
    take_turn(live)
    orch._update_combatant(live, HERO, charisma=1)
    expected = deepcopy(live.rng)
    damage = sum(expected.randint(1, 8) for _ in range(6))
    expected.randint(1, 20)
    activate(handle)
    assert events(live, SaveRolled)[-1].dc == 18
    assert events(live, DamageApplied)[-1].amount == damage
    assert live.rng.getstate() == expected.getstate()
    assert orch.get_live(handle).ongoing_spells[0].slot_level == level


def test_blinded_expires_at_caster_start_and_survives_concentration_drop():
    handle, live = table()
    cast(handle)
    assert "blinded" in live.active_conditions[FOE]
    blind = next(e.effect for e in events(live, EffectApplied) if e.effect.target_id == FOE)
    assert not blind.flags.get("concentration")
    act(handle, HERO, intent_type="drop_concentration")
    assert "blinded" in live.active_conditions[FOE]
    act(handle, HERO, intent_type="pass")
    assert "blinded" in live.active_conditions[FOE]
    asyncio.run(orch.advance_monster_turn(handle))
    assert "blinded" not in live.active_conditions.get(FOE, set())
    assert not live.topology.environment_sources


def test_successful_save_halves_damage_and_does_not_blind():
    handle, live = table()
    orch._update_combatant(live, FOE, constitution=210)
    expected = deepcopy(live.rng)
    damage = sum(expected.randint(1, 8) for _ in range(6))
    cast(handle)
    assert events(live, SaveRolled)[0].succeeded
    assert "blinded" not in live.active_conditions.get(FOE, set())
    assert events(live, DamageApplied)[0].amount == damage // 2


def test_voluntary_move_refreshes_bright_dim_sunlight_and_beam_origin():
    handle, live = table(zone_id="0,12")
    cast(handle)
    assert live.topology.sunlight_on_cell("0,0")
    act(handle, HERO, intent_type="move", target_zone_id="0,18")
    source = orch.get_live(handle).environment_sources[0]
    assert source.origin == "0,18"
    assert live.topology.light_on_cell("0,12") == "bright"
    assert live.topology.light_on_cell("0,11") == "dim"
    assert live.topology.light_on_cell("0,5") == "dark"
    assert live.topology.sunlight_on_cell("0,6")
    assert not live.topology.sunlight_on_cell("0,0")
    take_turn(live)
    before = len(events(live, DamageApplied))
    activate(handle)
    assert len(events(live, DamageApplied)) == before  # Old line is not reused.


def test_forced_move_refreshes_following_light_via_shared_step_hook():
    handle, live = table(zone_id="3,3")
    cast(handle)
    orch.push_combatant(live, HERO, "2,3", 10)
    assert live.actor_zone[HERO] == "5,3"
    assert orch.get_live(handle).environment_sources[0].origin == "5,3"
    assert live.topology.light_on_cell("11,3") == "bright"
    assert live.topology.sunlight_on_cell("17,3")


@pytest.mark.parametrize(
    "fault",
    [
        "initial_resolver",
        "initial_projection",
        "repeat_resolver",
        "move_projection",
        "concentration_cleanup",
    ],
)
def test_public_faults_restore_entire_transaction_and_rng(monkeypatch, fault):
    from dnd5e_engine import environment, live_spell_delivery, timed_activities
    from dnd5e_engine.persistent_areas import PersistentAreaState

    handle, live = table()
    if not fault.startswith("initial"):
        cast(handle)
        take_turn(live)
    before = snapshot(live), deepcopy(vars(live.topology))
    observed = []
    live.event_listeners.append(observed.append)
    module, name = (
        (timed_activities, "resolve_activity")
        if fault == "initial_resolver"
        else (live_spell_delivery, "resolve_activity")
        if fault == "repeat_resolver"
        else (environment, "reconcile_environment")
        if fault in ("move_projection", "initial_projection")
        else (PersistentAreaState, "concentration_ended")
    )
    original = getattr(module, name)

    def broken(*args, **kwargs):
        original(*args, **kwargs)
        if "resolver" in fault:
            assert not combatant(live).action_available
            assert events(live, DamageApplied)
        if "projection" in fault:
            assert live.topology.environment_sources
            assert live.topology.environment_sources[0].origin == live.actor_zone[HERO]
        live.rng.randint(1, 20)
        raise RuntimeError("ongoing injected fault")

    monkeypatch.setattr(module, name, broken)

    def invoke():
        if fault.startswith("initial"):
            cast(handle)
        elif fault == "repeat_resolver":
            activate(handle)
        elif fault == "move_projection":
            act(handle, HERO, intent_type="move", target_zone_id="0,1")
        else:
            act(handle, HERO, intent_type="drop_concentration")

    with pytest.raises(RuntimeError, match="ongoing injected fault"):
        invoke()
    assert (snapshot(live), vars(live.topology)) == before
    assert observed == []


def test_duration_ends_source_without_refresh_from_repeated_magic_actions():
    handle, live = table(constitution=210, hp_current=500, hp_max=500)
    cast(handle)
    for turn in range(10):
        if turn:
            activate(handle)
        act(handle, HERO, intent_type="pass")
        asyncio.run(orch.advance_monster_turn(handle))
    assert not live.persistent_areas.areas
    assert not orch.get_live(handle).ongoing_spells
    assert not live.topology.environment_sources
    assert live.spell_slots_by_entity[HERO][6] == 1
    assert len(events(live, SpellCast)) == 1
    with pytest.raises(orch.IntentRejectedError):
        activate(handle)


def test_moonbeam_now_registers_its_reviewed_relocatable_source():
    handle, live = table()
    act(
        handle,
        HERO,
        intent_type="cast_spell",
        spell_id="moonbeam",
        slot_level=2,
        target_zone_id="3,0",
    )
    assert not events(live, CastFailed)
    assert len(live.persistent_areas.areas) == 1
    assert orch.get_live(handle).ongoing_spells[0].activation.relocation.max_distance_ft == 60
    assert live.spell_slots_by_entity[HERO][2] == 1
    assert not combatant(live).action_available


def test_public_cast_move_activate_cleanup_replay_is_exact():
    def run():
        handle, live = table()
        cast(handle, 7)
        act(handle, HERO, intent_type="move", target_zone_id="0,1")
        act(handle, HERO, intent_type="pass")
        asyncio.run(orch.advance_monster_turn(handle))
        activate(handle)
        act(handle, HERO, intent_type="drop_concentration")
        return snapshot(live), vars(live.topology)

    assert run() == run()


def reaction_table():
    from tests.test_reaction_runtime import reactor

    result = asyncio.run(
        orch.start_combat(
            session_id="ongoing-reactions",
            party=[
                reactor(zone_id="3,0", constitution=-30),
                pc(
                    class_slug="sorcerer",
                    character_level=13,
                    charisma=20,
                    constitution=210,
                    hp_current=500,
                    hp_max=500,
                    spells_known=["sunbeam"],
                    spell_slots={6: 2},
                ),
            ],
            encounter=[foe(zone_id="8,8")],
            grid_scene=GridScene(width=30, height=30),
            rng_seed=7,
        )
    )
    return result.handle, orch._get_live(result.handle)


def test_first_cast_counterspell_keeps_cost_semantics_without_registering_source():
    from tests.test_reaction_runtime import REACTOR, arm

    handle, live = reaction_table()
    orch._update_combatant(live, HERO, constitution=-30)
    arm(handle, REACTOR, "counterspell")
    cast(handle)
    assert events(live, CastFailed)[-1].reason == "countered"
    assert live.spell_slots_by_entity[HERO][6] == 2
    assert not combatant(live).action_available
    assert not orch.get_live(handle).ongoing_spells
    assert not live.topology.environment_sources


@pytest.mark.parametrize("reaction", ["counterspell", "hellish-rebuke"])
def test_activation_skips_cast_window_and_keeps_damage_reactions(reaction):
    from tests.test_reaction_runtime import REACTOR, arm

    handle, live = reaction_table()
    act(handle, REACTOR, intent_type="pass")
    cast(handle)
    take_turn(live, REACTOR)
    arm(handle, REACTOR, reaction)
    before = len(events(live, DamageApplied))
    activate(handle)
    casts = [e.spell_id for e in events(live, SpellCast)]
    if reaction == "counterspell":
        assert casts == ["sunbeam"]
        assert combatant(live, REACTOR).reaction_available
        assert live.spell_slots_by_entity[REACTOR][3] == 2
        assert len(events(live, DamageApplied)) == before + 1
    else:
        assert casts == ["sunbeam", "hellish-rebuke"]
        assert not combatant(live, REACTOR).reaction_available
        assert any(e.target_id == HERO for e in events(live, DamageApplied)[before:])


def test_multiple_casters_have_independent_sources_owner_checks_and_cleanup():
    handle, live = table()
    ally_id = "char:ally"
    ally = combatant(live).model_copy(update={"entity_id": ally_id, "initiative": 19})
    orch._insert_into_roster(live, ally, 1, zone_id="0,15")
    live.party_ids.add(ally_id)
    live.spell_slots_by_entity[ally_id] = {6: 2}
    live.spells_known_by_entity[ally_id] = ["sunbeam"]
    cast(handle)
    take_turn(live, ally_id)
    act(
        handle,
        ally_id,
        intent_type="cast_spell",
        spell_id="sunbeam",
        slot_level=6,
        direction=(1, 0),
    )
    assert len(orch.get_live(handle).ongoing_spells) == 2
    take_turn(live, ally_id)
    before = snapshot(live)
    with pytest.raises(orch.IntentRejectedError):
        act(
            handle,
            ally_id,
            intent_type="activate_spell",
            source_id="area:0",
            activity_id=REPEAT,
            direction=(1, 0),
        )
    assert snapshot(live) == before
    act(
        handle,
        ally_id,
        intent_type="activate_spell",
        source_id="area:1",
        activity_id=REPEAT,
        direction=(1, 0),
    )
    take_turn(live)
    act(handle, HERO, intent_type="drop_concentration")
    assert [s.owner_id for s in orch.get_live(handle).ongoing_spells] == [ally_id]
    assert live.topology.light_on_cell("0,0") == "dark"
    assert live.topology.light_on_cell("0,15") == "bright"


def test_recast_replaces_source_and_old_activation_identity_cannot_be_reused():
    handle, live = table()
    cast(handle)
    take_turn(live)
    cast(handle)
    assert [s.source_id for s in orch.get_live(handle).ongoing_spells] == ["area:1"]
    take_turn(live)
    with pytest.raises(orch.IntentRejectedError):
        activate(handle, "area:0")
    activate(handle, "area:1")


def test_real_rage_drops_source_and_refuses_activation():
    handle, live = table(classes={"sorcerer": 12, "barbarian": 1})
    cast(handle)
    take_turn(live)
    act(handle, HERO, intent_type="use_feature", feature_id="rage")
    assert orch._rage_effect(live, HERO) is not None
    assert not live.topology.environment_sources
    with pytest.raises(orch.IntentRejectedError):
        activate(handle)


def test_real_action_surge_cannot_fund_another_ongoing_magic_action():
    handle, live = table(classes={"sorcerer": 11, "fighter": 2})
    cast(handle)
    act(handle, HERO, intent_type="use_feature", feature_id="action-surge")
    assert combatant(live).extra_actions_remaining == 1
    assert not combatant(live).action_available
    before = snapshot(live)
    with pytest.raises(orch.IntentRejectedError, match="no_action_economy"):
        activate(handle)
    assert snapshot(live) == before


@pytest.mark.parametrize("event", ["death", "departure", "combat_end"])
def test_source_lifecycle_removes_light_and_activation_projection(event):
    from dnd5e_engine.events import Death

    handle, live = table()
    cast(handle)
    if event == "death":
        orch._emit(live, Death(target_id=HERO, reason="damage"))
    elif event == "departure":
        orch._leave_roster(live, HERO, reason="spell_ended")
    else:
        asyncio.run(orch.end_combat(handle))
    assert not orch.get_live(handle).ongoing_spells
    assert not live.topology.environment_sources


def test_activation_rechecks_mechanical_identity_against_review():
    from dnd5e_srd_data import MemoryAssetLoader

    handle, live = table()
    cast(handle)
    take_turn(live)
    spell = BundledAssetLoader().get_spell("sunbeam").model_copy(deep=True)
    part = spell.activities[1].damage.parts[0].model_copy(update={"number": 100})
    spell.activities[1].damage = spell.activities[1].damage.model_copy(update={"parts": [part]})
    live.ruleset_loader = MemoryAssetLoader(spells=[spell])
    before = snapshot(live)
    with pytest.raises(orch.IntentRejectedError, match="admitted execution contract"):
        activate(handle)
    assert snapshot(live) == before


def test_light_footprint_never_becomes_a_damage_entry_area():
    handle, live = table()
    cast(handle)
    take_turn(live, FOE)
    before = len(events(live, DamageApplied))
    act(handle, FOE, intent_type="move", target_zone_id="10,1")
    assert len(events(live, DamageApplied)) == before
    assert live.topology.light_on_cell("10,1") == "dim"


def test_moving_light_changes_authoritative_vision_without_dispelling_darkness():
    handle, live = table()
    from dnd5e_engine.activities.passive_stats import CombatantSenses

    assert not live.topology.can_see("0,0", "10,0", CombatantSenses())
    cast(handle)
    assert live.topology.can_see("0,0", "10,0", CombatantSenses())
    ally_id = "char:dark"
    ally = combatant(live).model_copy(update={"entity_id": ally_id})
    orch._insert_into_roster(live, ally, 1, zone_id="0,1")
    live.party_ids.add(ally_id)
    live.spell_slots_by_entity[ally_id] = {2: 1}
    live.spells_known_by_entity[ally_id] = ["darkness"]
    take_turn(live, ally_id)
    act(
        handle,
        ally_id,
        intent_type="cast_spell",
        spell_id="darkness",
        slot_level=2,
        target_zone_id="10,0",
    )
    assert {s.source_id for s in live.topology.environment_sources} == {"sunbeam", "darkness"}
    assert live.topology.light_on_cell("10,0") == "dim"
    assert live.topology.can_see("0,0", "10,0", CombatantSenses())
    take_turn(live)
    act(handle, HERO, intent_type="drop_concentration")
    assert not live.topology.can_see("0,0", "10,0", CombatantSenses(darkvision=120))


def test_multiple_casters_blindness_expires_at_each_sources_own_turn_boundary():
    handle, live = table()
    ally_id = "char:ally"
    ally = combatant(live).model_copy(
        update={"entity_id": ally_id, "constitution": 210, "initiative": 19}
    )
    orch._insert_into_roster(live, ally, 1, zone_id="1,0")
    live.party_ids.add(ally_id)
    live.spell_slots_by_entity[ally_id] = {6: 1}
    live.spells_known_by_entity[ally_id] = ["sunbeam"]
    cast(handle)
    take_turn(live, ally_id)
    act(
        handle,
        ally_id,
        intent_type="cast_spell",
        spell_id="sunbeam",
        slot_level=6,
        direction=(1, 0),
    )
    assert len([e for e in live.active_effects[FOE] if "blinded" in e.statuses]) == 2
    act(handle, ally_id, intent_type="pass")
    asyncio.run(orch.advance_monster_turn(handle))
    assert "blinded" in live.active_conditions[FOE]
    assert len([e for e in live.active_effects[FOE] if "blinded" in e.statuses]) == 1
    act(handle, HERO, intent_type="pass")
    assert "blinded" not in live.active_conditions.get(FOE, set())


@pytest.mark.parametrize("fault", [False, True])
def test_public_forced_movement_refreshes_light_or_rolls_back_entire_spell(monkeypatch, fault):
    from dnd5e_engine import environment

    handle, live = table(zone_id="3,3", constitution=30, hp_current=500, hp_max=500)
    ally_id = "char:ally"
    ally = combatant(live).model_copy(update={"entity_id": ally_id, "initiative": 19})
    orch._insert_into_roster(live, ally, 1, zone_id="2,3")
    live.party_ids.add(ally_id)
    live.spell_slots_by_entity[ally_id] = {1: 1}
    live.spells_known_by_entity[ally_id] = ["thunderwave"]
    cast(handle)
    take_turn(live, ally_id)
    before = snapshot(live), deepcopy(vars(live.topology))
    observed = []
    live.event_listeners.append(observed.append)
    original = environment.reconcile_environment

    def broken(combat):
        original(combat)
        assert combat.actor_zone[HERO] != "3,3"
        assert combat.topology.environment_sources[0].origin == combat.actor_zone[HERO]
        raise RuntimeError("forced projection fault")

    def invoke():
        act(
            handle,
            ally_id,
            intent_type="cast_spell",
            spell_id="thunderwave",
            slot_level=1,
            direction=(1, 0),
        )

    if fault:
        monkeypatch.setattr(environment, "reconcile_environment", broken)
        with pytest.raises(RuntimeError, match="forced projection fault"):
            invoke()
        assert (snapshot(live), vars(live.topology)) == before
        assert observed == []
    else:
        invoke()
        assert live.actor_zone[HERO] == "5,3"
        assert orch.get_live(handle).environment_sources[0].origin == "5,3"
        assert live.topology.light_on_cell("11,3") == "bright"
        assert live.spell_slots_by_entity[ally_id][1] == 0
