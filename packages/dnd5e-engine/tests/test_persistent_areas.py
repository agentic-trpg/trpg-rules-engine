"""Persistent geometry, real movement/boundaries, cleanup and RNG invariants."""

from copy import deepcopy

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.common import PersistentAreaSpec
from pydantic import TypeAdapter

from dnd5e_engine import PlayerIntent
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.events import (
    ActorMoved,
    AreaCreated,
    AreaExpired,
    CastFailed,
    CombatEvent,
    DamageApplied,
    Death,
    EffectExpired,
    SaveRolled,
)
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.persistent_areas import (
    FollowSourceEmanation,
    StationaryArea,
    after_movement_step,
    before_turn_start,
    register_area,
    run_area_boundary,
)
from dnd5e_engine.specs import GridScene
from tests.c20_support import act, combatant, events, foe, pc, start

HERO = "char:hero"
FOE = "mon:foe"


@pytest.fixture(autouse=True)
def _loader():
    set_lib_loader_for_tests(BundledAssetLoader())
    yield
    set_lib_loader_for_tests(None)


def _combat(*, seed=7, enemy_cell="7,2", **fields):
    return start(
        [
            pc(
                class_slug="cleric",
                classes={"cleric": 5, "fighter": 2},
                character_level=7,
                wisdom=40,
                spell_slots={3: 3, 4: 2},
                equipment=("ball-bearings", "caltrops"),
                hp_current=500,
                hp_max=500,
                zone_id="2,2",
                **fields,
            )
        ],
        seed=seed,
        encounter=[foe(zone_id=enemy_cell)],
        grid_scene=GridScene(width=30, height=12),
    )


def _cast(handle, spell="spirit-guardians", **fields):
    act(handle, HERO, intent_type="cast_spell", spell_id=spell, **fields)


def _step(live, entity, cell):
    previous = live.actor_zone[entity]
    live.actor_zone[entity] = cell
    after_movement_step(live, entity, previous)


def _end(live):
    orch._end_turn_and_advance(live, live.current_actor_id)


def test_creation_captures_typed_source_geometry_magnitudes_and_exclusions():
    handle, live = _combat()
    _cast(handle, slot_level=4, excluded_target_ids=(FOE,))
    (area,) = live.persistent_areas.areas
    assert isinstance(area.geometry, FollowSourceEmanation)
    assert (area.source_entity_id, area.source_id, area.slot_level, area.base_spell_level) == (
        HERO,
        "spirit-guardians",
        4,
        3,
    )
    assert area.save_dc == 26
    assert area.excluded_ids == (FOE,)
    assert area.duration.rounds == 100
    assert area.concentration_identity in live.concentration_chain[HERO]
    (created,) = events(live, AreaCreated)
    assert created.origin == "2,2"
    assert created.placement == "follow-source"
    assert not events(live, SaveRolled)  # placement is not a movement entry
    assert TypeAdapter(CombatEvent).validate_json(created.model_dump_json()) == created


def test_source_follow_movement_hits_new_coverage_in_initiative_order_once_each_turn():
    handle, live = start(
        [
            pc(
                class_slug="cleric",
                classes={"cleric": 5, "fighter": 2},
                character_level=7,
                wisdom=40,
                spell_slots={3: 1},
                zone_id="2,2",
            )
        ],
        seed=7,
        encounter=[
            foe(entity_id="mon:b", initiative=3, zone_id="6,1"),
            foe(entity_id="mon:a", initiative=2, zone_id="6,3"),
        ],
    )
    _cast(handle)
    act(handle, HERO, intent_type="move", target_zone_id="3,2")
    assert [e.target_id for e in events(live, SaveRolled)] == ["mon:b", "mon:a"]
    assert live.event_log.index(events(live, ActorMoved)[0]) < live.event_log.index(
        events(live, SaveRolled)[0]
    )
    act(handle, HERO, intent_type="move", target_zone_id="2,2")
    act(handle, HERO, intent_type="move", target_zone_id="3,2")
    assert len(events(live, SaveRolled)) == 2
    act(handle, HERO, intent_type="pass")
    _end(live)
    assert [e.target_id for e in events(live, SaveRolled)] == ["mon:b", "mon:a", "mon:b"]


def test_enter_and_end_share_gate_but_the_next_creature_turn_has_a_fresh_gate():
    handle, live = _combat()
    _cast(handle)
    _step(live, FOE, "5,2")
    run_area_boundary(live, FOE, "turn-end-inside")
    _step(live, FOE, "6,2")
    _step(live, FOE, "5,2")
    assert len(events(live, SaveRolled)) == 1
    act(handle, HERO, intent_type="pass")
    assert len(events(live, SaveRolled)) == 1  # SG has no turn-start save
    _end(live)
    assert len(events(live, SaveRolled)) == 2


def test_excluded_creatures_stay_excluded_when_they_or_the_source_move():
    handle, live = _combat()
    _cast(handle, excluded_target_ids=(FOE,))
    _step(live, FOE, "5,2")
    assert orch._effective_speed(combatant(live, FOE), live) == 30
    _step(live, HERO, "3,2")
    run_area_boundary(live, FOE, "turn-end-inside")
    assert not events(live, SaveRolled)


def test_speed_and_remaining_distance_change_on_enter_leave_and_concentration_drop():
    handle, live = _combat()
    _cast(handle)
    assert orch._effective_speed(combatant(live, FOE), live) == 30
    _step(live, FOE, "5,2")
    assert orch._effective_speed(combatant(live, FOE), live) == 15
    assert combatant(live, FOE).movement_remaining == 15
    _step(live, FOE, "6,2")
    assert orch._effective_speed(combatant(live, FOE), live) == 30
    assert combatant(live, FOE).movement_remaining == 30
    _step(live, FOE, "5,2")
    act(handle, HERO, intent_type="drop_concentration")
    assert orch._effective_speed(combatant(live, FOE), live) == 30
    assert combatant(live, FOE).movement_remaining == 30


def test_trigger_reuses_save_resolver_and_captures_upcast_and_dc():
    handle, live = _combat()
    _cast(handle, slot_level=4)
    rng = deepcopy(live.rng)
    expected = sum(rng.randint(1, 8) for _ in range(4))
    rng.randint(1, 20)
    orch._update_combatant(live, HERO, wisdom=1)
    _step(live, FOE, "5,2")
    assert events(live, SaveRolled)[0].dc == 26
    assert events(live, DamageApplied)[0].amount == expected
    assert live.rng.getstate() == rng.getstate()


@pytest.mark.parametrize(
    "slug,origin,cells,dc",
    [
        ("ball-bearings", "4,2", {"4,2", "4,3", "5,2", "5,3"}, 10),
        ("caltrops", "3,2", {"3,2"}, 15),
    ],
)
def test_item_placement_is_stationary_and_does_not_resolve_a_save(slug, origin, cells, dc):
    handle, live = _combat()
    act(handle, HERO, intent_type="use_item", item_id=slug, target_zone_id=origin)
    (area,) = live.persistent_areas.areas
    assert isinstance(area.geometry, StationaryArea)
    assert area.cells(live.topology, live.actor_zone) == cells
    assert area.save_dc == dc
    assert area.concentration_identity is None
    assert not events(live, SaveRolled)
    _step(live, HERO, "1,2")
    assert area.origin(live.actor_zone) == origin


def test_bearings_fail_prone_and_reentry_draws_nothing_on_same_turn():
    handle, live = _combat(enemy_cell="6,2")
    orch._update_combatant(live, FOE, dexterity=-30)
    act(handle, HERO, intent_type="use_item", item_id="ball-bearings", target_zone_id="4,2")
    _step(live, FOE, "5,2")
    assert "prone" in live.active_conditions[FOE]
    rng = live.rng.getstate()
    _step(live, FOE, "6,2")
    _step(live, FOE, "5,2")
    assert live.rng.getstate() == rng
    assert len(events(live, SaveRolled)) == 1


@pytest.mark.parametrize("dexterity,failed", [(-30, True), (80, False)])
def test_caltrops_failure_stops_real_walk_until_next_start_success_does_not(dexterity, failed):
    handle, live = _combat()
    act(handle, HERO, intent_type="use_item", item_id="caltrops", target_zone_id="3,1")
    # The placer also qualifies as a creature entering its own hazard.
    orch._update_combatant(live, HERO, dexterity=dexterity)
    act(handle, HERO, intent_type="move", target_zone_id="5,2")
    assert len(events(live, SaveRolled)) == 1
    assert live.actor_zone[HERO] == ("3,1" if failed else "5,2")
    assert orch._effective_speed(combatant(live), live) == (0 if failed else 30)
    assert sum(e.amount for e in events(live, DamageApplied)) == (1 if failed else 0)
    act(handle, HERO, intent_type="pass")
    assert bool(live.persistent_areas.next_turn_start_effects) == failed
    _end(live)
    assert not live.persistent_areas.next_turn_start_effects
    assert combatant(live).movement_remaining == 30
    assert not live.active_effects.get(HERO)
    assert live.persistent_areas.areas


def test_forced_movement_crosses_hazard_even_if_destination_is_outside():
    handle, live = _combat(enemy_cell="3,2")
    orch._update_combatant(live, FOE, dexterity=-30)
    act(handle, HERO, intent_type="use_item", item_id="ball-bearings", target_zone_id="4,2")
    orch.push_combatant(live, FOE, "2,2", 20)
    assert live.actor_zone[FOE] == "7,2"
    assert [e.target_id for e in events(live, SaveRolled)] == [FOE]
    assert "prone" in live.active_conditions[FOE]


@pytest.mark.parametrize("cleanup", ["drop", "duration", "anchor", "death", "departure"])
def test_area_cleanup_is_synchronous_typed_and_rng_free(cleanup):
    handle, live = _combat()
    _cast(handle)
    (area,) = live.persistent_areas.areas
    rng = live.rng.getstate()
    if cleanup == "drop":
        act(handle, HERO, intent_type="drop_concentration")
    elif cleanup == "duration":
        live.concentration_rounds_remaining[HERO] = 1
        act(handle, HERO, intent_type="pass")
    elif cleanup == "anchor":
        target, effect_id, origin = area.concentration_identity
        orch._emit(
            live,
            EffectExpired(target_id=target, effect_id=effect_id, origin=origin, reason="duration"),
        )
    elif cleanup == "death":
        orch._emit(live, Death(target_id=HERO, reason="damage"))
    else:
        orch._leave_roster(live, HERO, "zero_hp")
    assert not live.persistent_areas.areas
    (expired,) = events(live, AreaExpired)
    assert expired.area_id == area.id
    if cleanup == "duration":
        assert expired.reason == "duration"
    assert TypeAdapter(CombatEvent).validate_json(expired.model_dump_json()) == expired
    assert live.rng.getstate() == rng


@pytest.mark.parametrize("removal", ["death", "leave"])
def test_a_single_target_disappearing_preserves_the_area(removal):
    handle, live = _combat()
    _cast(handle)
    if removal == "death":
        orch._emit(
            live, DamageApplied(target_id=FOE, amount=2000, damage_type="force", is_overkill=False)
        )
    else:
        orch._leave_roster(live, FOE, "zero_hp")
    assert len(live.persistent_areas.areas) == 1
    assert not events(live, AreaExpired)


@pytest.mark.parametrize(
    "origin,reason",
    [
        ("-1,0", "target_invalid"),
        ("0100,0", "target_invalid"),
        ("100,0", "target_invalid"),
        ("broken", "target_invalid"),
        ("25,2", "out_of_range"),
    ],
)
def test_illegal_point_origin_spends_no_slot_action_or_rng(origin, reason):
    handle, live = _combat()
    rng = live.rng.getstate()
    if origin == "broken":
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="malformed cell id"):
            _cast(handle, "stinking-cloud", target_zone_id=origin)
        assert not events(live, CastFailed)
    else:
        _cast(handle, "stinking-cloud", target_zone_id=origin)
        assert [e.reason for e in events(live, CastFailed)] == [reason]
    assert live.spell_slots_by_entity[HERO][3] == 3
    assert combatant(live).action_available
    assert not live.persistent_areas.areas
    assert live.rng.getstate() == rng


def test_stinking_cloud_uses_empty_point_and_keeps_origin_when_caster_moves():
    handle, live = _combat(enemy_cell="8,2")
    _cast(handle, "stinking-cloud", target_zone_id="8,3")
    (area,) = live.persistent_areas.areas
    assert area.cast_origin == "8,3"
    _step(live, HERO, "1,1")
    act(handle, HERO, intent_type="pass")
    assert [e.target_id for e in events(live, SaveRolled)] == [FOE]
    assert area.origin(live.actor_zone) == "8,3"


def test_nonconcentration_area_duration_and_trigger_order_are_deterministic():
    def run():
        _, live = _combat()
        spell = BundledAssetLoader().get_spell("stinking-cloud")
        spell = spell.model_copy(
            update={
                "concentration": False,
                "duration": spell.duration.model_copy(update={"units": "round", "value": 1}),
            }
        )
        activity = spell.activities[0].model_copy(
            update={"persistent_area": PersistentAreaSpec(triggers=("enter",))}
        )
        ctx = build_activity_context(
            combatant(live),
            [],
            rng=live.rng,
            event_emitter=lambda e: orch._emit(live, e),
            spellcasting_ability="wis",
            source_passive_effects=spell.passive_effects,
            slot_level=3,
            base_spell_level=3,
            concentration=False,
            spell_book={},
            passive_damage_modifiers={},
            save_modifiers={},
        )
        for _ in range(2):
            register_area(
                live,
                activity,
                ctx,
                source_id=spell.slug,
                spell=spell,
                intent=PlayerIntent(intent_type="cast_spell", target_zone_id="12,2"),
            )
        # Skip the timed activity's explicit not-before boundary.
        live.turn_serial += 1
        _step(live, FOE, "12,2")
        assert [e.target_id for e in events(live, SaveRolled)] == [FOE, FOE]
        run_area_boundary(live, HERO, "turn-end-inside")
        assert [e.area_id for e in events(live, AreaExpired)] == ["area:0", "area:1"]
        assert not live.persistent_areas.areas
        return [e.model_dump() for e in live.event_log], live.rng.getstate()

    assert run() == run()


@pytest.mark.parametrize("spell", ["fireball", "flame-strike"])
def test_immediate_sphere_and_cylinder_accept_an_empty_point_and_stay_immediate(spell):
    handle, live = _combat(enemy_cell="7,2", intelligence=40)
    live.spell_slots_by_entity[HERO][5] = 1
    _cast(handle, spell, target_zone_id="7,3", slot_level=5)
    assert events(live, SaveRolled)
    assert events(live, DamageApplied)
    assert not live.persistent_areas.areas
    assert not events(live, AreaCreated)


@pytest.mark.parametrize("seed", [1, 7, 64])
def test_same_state_intents_and_seed_reproduce_events_and_rng(seed):
    def run():
        handle, live = _combat(seed=seed, enemy_cell="6,2")
        _cast(handle, slot_level=4)
        act(handle, HERO, intent_type="move", target_zone_id="3,2")
        act(handle, HERO, intent_type="move", target_zone_id="2,2")
        act(handle, HERO, intent_type="move", target_zone_id="3,2")
        act(handle, HERO, intent_type="pass")
        _end(live)
        act(handle, HERO, intent_type="drop_concentration")
        return [e.model_dump() for e in live.event_log], live.rng.getstate()

    assert run() == run()


@pytest.mark.parametrize("removal", ["death", "leave"])
def test_stationary_hazard_is_removed_only_when_its_source_is_removed(removal):
    handle, live = _combat()
    act(handle, HERO, intent_type="use_item", item_id="caltrops", target_zone_id="3,2")
    if removal == "death":
        orch._emit(live, Death(target_id=HERO, reason="damage"))
    else:
        orch._leave_roster(live, HERO, "zero_hp")
    assert not live.persistent_areas.areas
    assert events(live, AreaExpired)[0].reason == "source_removed"


def test_caltrops_repeated_on_different_turns_before_target_start_leaves_no_stale_effect():
    handle, live = _combat()
    orch._update_combatant(live, FOE, dexterity=-30)
    act(handle, HERO, intent_type="use_item", item_id="caltrops", target_zone_id="3,2")
    _step(live, FOE, "3,2")
    _step(live, FOE, "4,2")
    live.turn_serial += 1  # Another actor's turn, before the victim's next start.
    _step(live, FOE, "3,2")
    assert len(events(live, SaveRolled)) == 2
    assert len(live.active_effects[FOE]) == 1
    before_turn_start(live, FOE)
    assert not live.active_effects.get(FOE)
    assert not live.persistent_areas.next_turn_start_effects
    assert orch._effective_speed(combatant(live, FOE), live) == 30


def test_dash_allowance_changes_with_speed_without_double_charging_or_refunding_distance():
    handle, live = _combat(enemy_cell="6,2")
    _cast(handle, excluded_target_ids=())
    act(handle, HERO, intent_type="pass")
    orch._handle_dash(live, combatant(live, FOE), PlayerIntent(intent_type="dash"))
    assert combatant(live, FOE).movement_remaining == 60
    _step(live, FOE, "5,2")
    assert combatant(live, FOE).movement_remaining == 30
    _step(live, FOE, "6,2")
    assert combatant(live, FOE).movement_remaining == 60
    _step(live, FOE, "5,2")
    # A second paid Dash while inside adds 15, not another reduction.
    orch._update_combatant(live, FOE, action_available=True)
    orch._handle_dash(live, combatant(live, FOE), PlayerIntent(intent_type="dash"))
    assert combatant(live, FOE).movement_remaining == 45
    _step(live, FOE, "6,2")
    assert combatant(live, FOE).movement_remaining == 90
