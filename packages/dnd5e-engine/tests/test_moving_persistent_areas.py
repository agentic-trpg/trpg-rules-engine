"""SRD 5.2.1 Moonbeam through natural public turns and authoritative carriers."""

import asyncio
from copy import deepcopy

import pytest
from dnd5e_srd_data import BundledAssetLoader

from dnd5e_engine import orchestrator as orch
from dnd5e_engine.events import (
    AreaCreated,
    AreaExpired,
    AreaRelocated,
    CastFailed,
    ConcentrationCheck,
    DamageApplied,
    SaveRolled,
    SpellCast,
)
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.specs import GridScene
from tests.c21_support import act, combatant, events, foe, pc
from tests.test_execution_integrity import snapshot

HERO = "char:hero"
TARGET = "char:target"
SECOND = "char:second"
ACTIVITY = "dnd5eactivity000"


@pytest.fixture(autouse=True)
def loader():
    set_lib_loader_for_tests(BundledAssetLoader())
    yield
    set_lib_loader_for_tests(None)


def table(*, scene=None, target=None, second=None, hero=None, seed=19):
    result = asyncio.run(
        orch.start_combat(
            session_id="moonbeam",
            rng_seed=seed,
            party=[
                pc(
                    **(
                        {
                            "initiative": 40,
                            "class_slug": "druid",
                            "character_level": 9,
                            "wisdom": 30,
                            "constitution": 210,
                            "hp_current": 500,
                            "hp_max": 500,
                            "spells_known": ["moonbeam", "darkness", "fog-cloud", "thunderwave"],
                            "spell_slots": {1: 2, 2: 3, 3: 3},
                        }
                        | (hero or {})
                    )
                ),
                pc(
                    TARGET,
                    **(
                        {
                            "initiative": 20,
                            "class_slug": "druid",
                            "character_level": 6,
                            "zone_id": "10,5",
                            "constitution": -30,
                            "hp_current": 500,
                            "hp_max": 500,
                            "spells_known": ["moonbeam", "darkness"],
                            "spell_slots": {2: 2, 3: 2},
                        }
                        | (target or {})
                    ),
                ),
                pc(
                    SECOND,
                    **(
                        {
                            "initiative": 10,
                            "zone_id": "10,6",
                            "constitution": 210,
                            "hp_current": 500,
                            "hp_max": 500,
                        }
                        | (second or {})
                    ),
                ),
            ],
            encounter=[foe(zone_id="29,29")],
            grid_scene=scene or GridScene(width=40, height=40, default_lighting="dark"),
        )
    )
    return result.handle, orch._get_live(result.handle)


def until(handle, live, actor):
    for _ in range(12):
        current = live.initiative[live.current_turn_index].entity_id
        if current == actor:
            return
        if current in live.party_ids:
            act(handle, current, intent_type="pass")
        else:
            asyncio.run(orch.advance_monster_turn(handle))
    raise AssertionError("initiative did not reach actor")


def next_turn(handle, live, actor=HERO):
    act(handle, actor, intent_type="pass")
    until(handle, live, actor)


def cast(handle, *, actor=HERO, point="10,5", level=2, spell="moonbeam"):
    act(
        handle,
        actor,
        intent_type="cast_spell",
        spell_id=spell,
        slot_level=level,
        target_zone_id=point,
    )


def relocate(handle, *, actor=HERO, source="area:0", point="14,5", **kwargs):
    act(
        handle,
        actor,
        intent_type="activate_spell",
        source_id=source,
        activity_id=ACTIVITY,
        target_zone_id=point,
        **kwargs,
    )


def full(live):
    return snapshot(live), deepcopy(vars(live.topology))


@pytest.mark.parametrize("level", [2, 3, 5])
def test_appearance_shared_damage_half_upcast_geometry_dim_and_payment(level):
    handle, live = table(hero={"spell_slots": {level: 2}})
    expected = deepcopy(live.rng)
    damage = sum(expected.randint(1, 10) for _ in range(level))
    expected.randint(1, 20)
    expected.randint(1, 20)
    cast(handle, level=level)
    assert not events(live, CastFailed)
    assert [(e.target_id, e.amount, e.damage_type) for e in events(live, DamageApplied)] == [
        (TARGET, damage, "radiant"),
        (SECOND, damage // 2, "radiant"),
    ]
    assert [s.succeeded for s in events(live, SaveRolled)] == [False, True]
    assert live.rng.getstate() == expected.getstate()
    area = live.persistent_areas.areas[0]
    assert (area.template.shape, area.template.size_ft, area.template.height_ft) == (
        "cylinder",
        5,
        40,
    )
    assert events(live, AreaCreated)[0].height_ft == 40
    assert live.topology.light_on_cell("10,5") == "dim"
    assert live.topology.light_on_cell("12,5") == "dark"
    assert not live.topology.sunlight_on_cell("10,5")
    assert live.spell_slots_by_entity[HERO][level] == 1
    assert not combatant(live).action_available
    assert orch.get_live(handle).ongoing_spells[0].height_ft == 40


def test_end_turn_not_start_once_per_combat_turn_and_entry_reentry():
    handle, live = table(target={"base_speed": 60})
    cast(handle)
    until(handle, live, TARGET)
    assert len(events(live, SaveRolled)) == 2  # No turn-start damage.
    act(handle, TARGET, intent_type="move", target_zone_id="12,5")
    act(handle, TARGET, intent_type="move", target_zone_id="10,5")
    assert live.actor_zone[TARGET] == "10,5"
    saves = len(events(live, SaveRolled))
    assert saves == 3  # New combat turn permits entry.
    act(handle, TARGET, intent_type="move", target_zone_id="12,5")
    act(handle, TARGET, intent_type="move", target_zone_id="10,5")
    act(handle, TARGET, intent_type="pass")
    assert len(events(live, SaveRolled)) == saves  # Re-entry and end share gate.
    act(handle, SECOND, intent_type="pass")
    assert len(events(live, SaveRolled)) == saves + 1
    until(handle, live, TARGET)
    saves = len(events(live, SaveRolled))
    act(handle, TARGET, intent_type="pass")
    assert len(events(live, SaveRolled)) == saves + 1


def test_relocation_destination_only_keeps_slot_dc_duration_and_concentration():
    handle, live = table(
        target={"zone_id": "14,5"}, second={"zone_id": "14,6", "constitution": -30}
    )
    cast(handle, point="4,5", level=3)
    next_turn(handle, live)
    area = live.persistent_areas.areas[0]
    orch._update_combatant(live, HERO, wisdom=2)  # Captured DC survives a stat update.
    old = (
        dict(live.spell_slots_by_entity[HERO]),
        deepcopy(live.concentration_chain),
        dict(live.concentration_rounds_remaining),
        area.id,
        area.slot_level,
        area.save_dc,
    )
    relocate(handle, point="14,5")
    assert len(events(live, SpellCast)) == 1
    assert old == (
        live.spell_slots_by_entity[HERO],
        live.concentration_chain,
        live.concentration_rounds_remaining,
        area.id,
        area.slot_level,
        area.save_dc,
    )
    assert [s.dc for s in events(live, SaveRolled)] == [22, 22]
    assert len({d.amount for d in events(live, DamageApplied)}) == 1
    assert live.topology.light_on_cell("4,5") == "dark"
    assert live.topology.light_on_cell("14,5") == "dim"
    assert events(live, AreaRelocated)[0].from_origin == "4,5"
    assert not combatant(live).action_available


def test_relocation_does_not_hit_transit_cells_or_existing_overlap():
    handle, live = table(target={"zone_id": "8,5"}, second={"zone_id": "14,5"})
    cast(handle, point="4,5")
    next_turn(handle, live)
    relocate(handle, point="14,5")
    assert [s.target_id for s in events(live, SaveRolled)] == [SECOND]
    next_turn(handle, live)
    before = len(events(live, SaveRolled))
    relocate(handle, point="15,5")  # SECOND remains inside; no new arrival.
    assert len(events(live, SaveRolled)) == before


@pytest.mark.parametrize(
    "mode",
    [
        "same_turn",
        "owner",
        "missing",
        "far",
        "out",
        "cover",
        "transit_cover",
        "extra",
        "dead_source",
        "no_action",
    ],
)
def test_illegal_relocation_is_draw_free_and_preserves_every_field(mode):
    scene = GridScene(
        width=40,
        height=40,
        cover_cells={"14,5": "total"} if mode == "cover" else {},
        blocked_cells=["8,5"] if mode == "transit_cover" else [],
    )
    handle, live = table(scene=scene)
    cast(handle, point="4,5")
    if mode != "same_turn":
        next_turn(handle, live)
    kwargs = {}
    if mode == "owner":
        until(handle, live, TARGET)
        kwargs["actor"] = TARGET
    elif mode == "missing":
        kwargs["source"] = "area:missing"
    elif mode == "far":
        kwargs["point"] = "17,5"
    elif mode == "out":
        kwargs["point"] = "50,5"
    elif mode == "extra":
        kwargs["slot_level"] = 3
    elif mode == "dead_source":
        act(handle, HERO, intent_type="drop_concentration")
    elif mode == "no_action":
        act(handle, HERO, intent_type="dash")
    before = full(live)
    with pytest.raises(orch.IntentRejectedError):
        relocate(handle, **kwargs)
    assert full(live) == before


def test_exact_sixty_feet_and_occupied_destination_are_legal():
    handle, live = table(target={"zone_id": "16,5"})
    cast(handle, point="4,5")
    next_turn(handle, live)
    relocate(handle, point="16,5")
    assert live.persistent_areas.areas[0].origin(live.actor_zone) == "16,5"
    assert events(live, SaveRolled)[-1].target_id == TARGET


def test_public_intent_canonicalizes_destination_cell_alias():
    handle, live = table()
    cast(handle, point="4,5")
    next_turn(handle, live)
    relocate(handle, point="014,5")
    assert events(live, AreaRelocated)[-1].origin == "14,5"


def shape_table():
    handle, live = table(target={"zone_id": "10,5"}, second={"zone_id": "20,20"})
    until(handle, live, TARGET)
    act(handle, TARGET, intent_type="use_feature", feature_id="wild-shape", form_id="giant-badger")
    assert TARGET in live.transforms
    until(handle, live, HERO)
    return handle, live


def test_failed_transformed_target_reverts_authoritative_stats_and_locks_until_leaving():
    handle, live = shape_table()
    cast(handle)
    assert not events(live, SaveRolled)[-1].succeeded
    assert TARGET not in live.transforms
    assert combatant(live, TARGET).constitution == -30
    assert TARGET in live.persistent_areas.areas[0].shape_locked_ids
    until(handle, live, TARGET)
    before = deepcopy(live.custom_counters_by_entity), live.rng.getstate()
    act(handle, TARGET, intent_type="use_feature", feature_id="wild-shape", form_id="giant-badger")
    assert events(live, CastFailed)[-1].reason == "invalid_form"
    assert before == (live.custom_counters_by_entity, live.rng.getstate())
    assert combatant(live, TARGET).bonus_action_available
    act(handle, TARGET, intent_type="move", target_zone_id="12,5")
    assert not live.persistent_areas.areas[0].shape_locked_ids
    act(handle, TARGET, intent_type="use_feature", feature_id="wild-shape", form_id="giant-badger")
    assert TARGET in live.transforms


def test_ordinary_failed_target_is_not_shape_locked():
    handle, live = table()
    cast(handle)
    assert not live.persistent_areas.areas[0].shape_locked_ids
    until(handle, live, TARGET)
    act(handle, TARGET, intent_type="use_feature", feature_id="wild-shape", form_id="giant-badger")
    assert TARGET in live.transforms


@pytest.mark.parametrize("cleanup", ["move_area", "concentration", "combat_end", "duration"])
def test_shape_lock_ends_with_membership_or_source_lifetime(cleanup):
    handle, live = shape_table()
    cast(handle)
    assert TARGET in live.persistent_areas.areas[0].shape_locked_ids
    if cleanup == "move_area":
        next_turn(handle, live)
        relocate(handle, point="14,5")
    elif cleanup == "concentration":
        act(handle, HERO, intent_type="drop_concentration")
    elif cleanup == "duration":
        for _ in range(10):
            next_turn(handle, live)
    else:
        asyncio.run(orch.end_combat(handle))
    assert all(TARGET not in a.shape_locked_ids for a in live.persistent_areas.areas)
    if cleanup == "combat_end":
        assert not live.persistent_areas.areas
        assert not live.topology.environment_sources


@pytest.mark.parametrize("level", [2, 3])
@pytest.mark.parametrize("order", ["moon_first", "dark_first", "relocation"])
def test_magical_dim_light_shared_darkness_dispel_and_cleanup(level, order):
    handle, live = table(target={"zone_id": "20,5"}, second={"zone_id": "20,6"})
    if order == "dark_first":
        until(handle, live, TARGET)
        cast(handle, actor=TARGET, spell="darkness", point="10,5")
        until(handle, live, HERO)
    cast(handle, level=level, point="4,5" if order == "relocation" else "10,5")
    if order != "dark_first":
        until(handle, live, TARGET)
        cast(handle, actor=TARGET, spell="darkness", point="10,5")
        until(handle, live, HERO)
    if order == "relocation":
        relocate(handle, point="10,5")
    areas = {a.source_id for a in live.persistent_areas.areas}
    assert areas == ({"darkness"} if level == 2 else {"darkness", "moonbeam"})
    if level == 2:
        assert not live.concentration_chain.get(HERO)
        assert not orch.get_live(handle).ongoing_spells
        assert any(e.reason == "dispelled" for e in events(live, AreaExpired))
    else:
        assert live.topology.light_on_cell("10,5") == "dim"
        act(handle, HERO, intent_type="drop_concentration")
        assert live.topology.light_on_cell("10,5") == "dark"


def test_duration_does_not_reset_on_relocation_and_cleanup_is_exact():
    handle, live = table(target={"zone_id": "20,20"}, second={"zone_id": "20,21"})
    cast(handle, point="4,5")
    for turn in range(10):
        if turn:
            relocate(handle, point="4,5" if turn % 2 else "5,5")
        next_turn(handle, live)
    assert not live.persistent_areas.areas
    assert not live.topology.environment_sources
    assert len(events(live, SpellCast)) == 1
    assert len(events(live, AreaExpired)) == 1
    assert live.spell_slots_by_entity[HERO][2] == 2


@pytest.mark.parametrize(
    "fault", ["appearance", "relocation", "reversion", "environment", "cleanup"]
)
def test_public_fault_rollback_restores_all_authority_and_rng(monkeypatch, fault):
    from dnd5e_engine import environment, persistent_areas

    handle, live = shape_table() if fault == "reversion" else table()
    if fault not in ("appearance", "reversion"):
        cast(handle, point="4,5")
        next_turn(handle, live)
    before = full(live)
    observed = []
    live.event_listeners.append(observed.append)
    module, name = (
        (orch, "_end_transform")
        if fault == "reversion"
        else (environment, "reconcile_environment")
        if fault == "environment"
        else (persistent_areas.PersistentAreaState, "concentration_ended")
        if fault == "cleanup"
        else (persistent_areas, "resolve_activity")
    )
    original = getattr(module, name)

    def broken(*args, **kwargs):
        original(*args, **kwargs)
        live.rng.randint(1, 20)
        raise RuntimeError("moonbeam injected failure")

    monkeypatch.setattr(module, name, broken)

    def invoke():
        if fault in ("appearance", "reversion"):
            cast(handle)
        elif fault == "cleanup":
            act(handle, HERO, intent_type="drop_concentration")
        else:
            relocate(handle, point="10,5")

    with pytest.raises(RuntimeError, match="moonbeam injected failure"):
        invoke()
    assert full(live) == before
    assert observed == []


def test_public_cast_relocation_entry_reversion_cleanup_replay_is_exact():
    def run():
        handle, live = shape_table()
        cast(handle, level=3)
        next_turn(handle, live)
        relocate(handle, point="14,5")
        until(handle, live, TARGET)
        act(handle, TARGET, intent_type="move", target_zone_id="13,5")
        until(handle, live, HERO)
        act(handle, HERO, intent_type="drop_concentration")
        return full(live)

    assert run() == run()


@pytest.mark.parametrize("health", [500, 1])
def test_self_covered_initial_caster_checks_new_concentration_and_cannot_resurrect_source(health):
    handle, live = table(hero={"constitution": -30, "hp_current": health, "hp_max": 500})
    cast(handle, point="0,0")
    assert events(live, DamageApplied)[0].target_id == HERO
    assert not live.concentration_chain.get(HERO)
    assert not live.persistent_areas.areas
    assert not live.topology.environment_sources
    if health == 500:
        checks = events(live, ConcentrationCheck)
        assert len(checks) == 1
        assert not checks[0].succeeded
    else:
        assert live.tracked_hp[HERO] == 0


def test_transformed_successful_target_retains_form_without_lock():
    handle, live = table(target={"zone_id": "10,5"}, hero={"wisdom": -30})
    until(handle, live, TARGET)
    act(handle, TARGET, intent_type="use_feature", feature_id="wild-shape", form_id="giant-badger")
    until(handle, live, HERO)
    cast(handle)
    assert events(live, SaveRolled)[0].succeeded
    assert TARGET in live.transforms
    assert not live.persistent_areas.areas[0].shape_locked_ids


def test_forced_movement_into_cylinder_uses_shared_entry_hook_without_target_movement_cost():
    handle, live = table(
        hero={"zone_id": "7,5"}, target={"zone_id": "8,5"}, second={"zone_id": "20,20"}
    )
    cast(handle, point="10,5")
    next_turn(handle, live)
    before = combatant(live, TARGET).movement_remaining
    act(
        handle,
        HERO,
        intent_type="cast_spell",
        spell_id="thunderwave",
        slot_level=1,
        direction=(1, 0),
    )
    assert live.actor_zone[TARGET] == "10,5"
    assert combatant(live, TARGET).movement_remaining == before
    moon = [d for d in events(live, DamageApplied) if d.damage_type == "radiant"]
    assert len(moon) == 1
    assert moon[0].target_id == TARGET


@pytest.mark.parametrize("older_level,newer_level", [(2, 2), (3, 2), (2, 3)])
def test_overlapping_sources_project_strongest_latest_and_restore_surviving_source(
    older_level, newer_level
):
    handle, live = table(
        target={"zone_id": "18,5", "constitution": 210},
        second={"zone_id": "10,5", "constitution": -30},
    )
    cast(handle, level=older_level)
    until(handle, live, TARGET)
    cast(handle, actor=TARGET, level=newer_level)
    assert len(live.persistent_areas.areas) == 2
    before = len(events(live, SaveRolled))
    until(handle, live, SECOND)
    act(handle, SECOND, intent_type="pass")
    assert len(events(live, SaveRolled)) == before + 1
    older, newer = live.persistent_areas.areas
    winner = newer if newer_level >= older_level else older
    assert winner.last_trigger_turn[SECOND] == live.turn_serial - 1
    loser = older if winner is newer else newer
    assert loser.last_trigger_turn.get(SECOND) != winner.last_trigger_turn[SECOND]
    until(handle, live, winner.source_entity_id)
    act(handle, winner.source_entity_id, intent_type="drop_concentration")
    assert live.persistent_areas.areas == [loser]
    assert live.topology.light_on_cell("10,5") == "dim"
    until(handle, live, SECOND)
    before = len(events(live, SaveRolled))
    act(handle, SECOND, intent_type="pass")
    assert len(events(live, SaveRolled)) == before + 1


@pytest.mark.parametrize("counter", [True, False])
def test_counterspell_initial_window_and_no_new_window_on_relocation(counter):
    from tests.test_reaction_runtime import REACTOR, arm, reactor

    result = asyncio.run(
        orch.start_combat(
            session_id="moon-counter",
            rng_seed=19,
            party=[
                reactor(zone_id="5,5", initiative=50),
                pc(
                    class_slug="druid",
                    character_level=9,
                    constitution=-30,
                    wisdom=30,
                    spells_known=["moonbeam"],
                    spell_slots={2: 2},
                    hp_current=500,
                    hp_max=500,
                ),
            ],
            encounter=[foe(zone_id="29,29")],
            grid_scene=GridScene(width=40, height=40),
        )
    )
    handle, live = result.handle, orch._get_live(result.handle)
    if counter:
        arm(handle, REACTOR, "counterspell")
    until(handle, live, HERO)
    cast(handle, point="4,5")
    if counter:
        assert events(live, CastFailed)[-1].reason == "countered"
        assert not live.persistent_areas.areas
        assert live.spell_slots_by_entity[HERO][2] == 2
        assert not combatant(live).action_available
    else:
        until(handle, live, REACTOR)
        arm(handle, REACTOR, "counterspell")
        until(handle, live, HERO)
        relocate(handle, point="14,5")
        assert len(events(live, SpellCast)) == 1
        assert combatant(live, REACTOR).reaction_available
        assert live.spell_slots_by_entity[REACTOR][3] == 2


@pytest.mark.parametrize(
    "removal", ["target_death", "caster_death", "target_departure", "caster_departure"]
)
def test_death_and_departure_preserve_other_area_ownership_and_cleanup(removal):
    # Public Wild Shape and cast establish real effects; roster removal is a
    # separate Host boundary, exercised through its authoritative existing seam.
    from dnd5e_engine.events import Death

    handle, live = shape_table()
    cast(handle)
    if removal.endswith("departure"):
        orch._leave_roster(live, TARGET if removal.startswith("target") else HERO, "spell_ended")
    else:
        orch._emit(
            live, Death(target_id=TARGET if removal.startswith("target") else HERO, reason="damage")
        )
    if removal.startswith("target"):
        assert len(live.persistent_areas.areas) == 1
        assert not live.persistent_areas.areas[0].shape_locked_ids
        assert live.topology.environment_sources
    else:
        assert not live.persistent_areas.areas
        assert not live.topology.environment_sources


@pytest.mark.parametrize("temp_hp", [1, 500])
def test_existing_polymorph_carrier_reverts_and_locks_even_when_damage_already_ended_form(
    temp_hp, monkeypatch
):
    # Polymorph's complete spell admission is independent. Seat its existing
    # authoritative carrier as C21 tests do; Moonbeam still uses its public cast.
    from dnd5e_engine import ActiveEffect
    from dnd5e_engine.events import EffectExpired

    handle, live = table(second={"zone_id": "20,20"})
    form = BundledAssetLoader().get_monster("giant-badger")
    orch._apply_transform(
        live,
        TARGET,
        form,
        source="polymorph",
        effect=ActiveEffect(
            id="effect:polymorph",
            name="Polymorph",
            target_id=TARGET,
            origin=f"cast:polymorph:{SECOND}",
            flags={"concentration": True},
        ),
        temp_hp=temp_hp,
    )
    identity = (TARGET, "effect:polymorph", f"cast:polymorph:{SECOND}")
    live.concentration_chain[SECOND] = [identity]
    original = orch._end_transform
    called = []

    def observed(combat, target_id, reason):
        if reason == "remove_ieffect" and temp_hp == 1:
            assert TARGET not in combat.transforms  # Damage already reverted it.
            assert combat.tracked_temp_hp[TARGET] == 0
        called.append(reason)
        original(combat, target_id, reason)

    monkeypatch.setattr(orch, "_end_transform", observed)
    cast(handle)
    assert not events(live, SaveRolled)[0].succeeded
    assert TARGET not in live.transforms
    assert TARGET in live.persistent_areas.areas[0].shape_locked_ids
    assert not live.concentration_chain.get(SECOND)
    expired = [e for e in events(live, EffectExpired) if e.effect_id == "effect:polymorph"]
    assert len(expired) == 1
    assert expired[0].reason == ("temp_hp_depleted" if temp_hp == 1 else "remove_ieffect")
    assert called == (
        ["temp_hp_depleted", "remove_ieffect"] if temp_hp == 1 else ["remove_ieffect"]
    )


def test_other_source_expiry_keeps_moonbeam_lock_and_projection():
    handle, live = table(
        second={
            "class_slug": "druid",
            "character_level": 6,
            "constitution": -30,
            "zone_id": "10,5",
        },
        target={"zone_id": "20,5"},
    )
    until(handle, live, SECOND)
    act(handle, SECOND, intent_type="use_feature", feature_id="wild-shape", form_id="giant-badger")
    until(handle, live, HERO)
    cast(handle)
    assert SECOND in live.persistent_areas.areas[0].shape_locked_ids
    until(handle, live, TARGET)
    cast(handle, actor=TARGET, point="24,5")
    assert len(live.persistent_areas.areas) == 2
    act(handle, TARGET, intent_type="drop_concentration")
    assert len(live.persistent_areas.areas) == 1
    assert SECOND in live.persistent_areas.areas[0].shape_locked_ids
    assert live.topology.light_on_cell("10,5") == "dim"


def test_unrelated_persistent_area_preserves_its_lifetime_and_movement_triggers():
    handle, live = table(
        target={
            "zone_id": "15,5",
            "class_slug": "cleric",
            "character_level": 5,
            "wisdom": 30,
            "spells_known": ["spirit-guardians"],
            "spell_slots": {3: 2},
        }
    )
    cast(handle, point="4,5")
    until(handle, live, TARGET)
    act(handle, TARGET, intent_type="cast_spell", spell_id="spirit-guardians", slot_level=3)
    until(handle, live, HERO)
    relocate(handle, point="8,5")
    assert len(live.persistent_areas.areas) == 2
    act(handle, HERO, intent_type="drop_concentration")
    assert [a.source_id for a in live.persistent_areas.areas] == ["spirit-guardians"]
    assert live.concentration_chain[TARGET]


@pytest.mark.parametrize("phase", ["appearance", "relocation"])
def test_damage_reaction_cannot_flush_pending_appearance_twice(phase):
    from tests.test_reaction_runtime import REACTOR, arm, reactor

    result = asyncio.run(
        orch.start_combat(
            session_id="moon-reaction",
            rng_seed=19,
            party=[
                reactor(zone_id="14,6", initiative=50),
                pc(
                    zone_id="4,0",
                    class_slug="druid",
                    character_level=9,
                    initiative=40,
                    constitution=210,
                    wisdom=30,
                    spells_known=["moonbeam"],
                    spell_slots={2: 2},
                    hp_current=500,
                    hp_max=500,
                ),
                pc(
                    TARGET,
                    initiative=30,
                    zone_id="14,5",
                    constitution=-30,
                    hp_current=500,
                    hp_max=500,
                ),
            ],
            encounter=[foe(zone_id="29,29")],
            grid_scene=GridScene(width=40, height=40),
        )
    )
    handle, live = result.handle, orch._get_live(result.handle)
    if phase == "appearance":
        arm(handle, REACTOR, "hellish-rebuke")
    until(handle, live, HERO)
    cast(handle, point="14,5" if phase == "appearance" else "4,5")
    if phase == "relocation":
        until(handle, live, REACTOR)
        arm(handle, REACTOR, "hellish-rebuke")
        until(handle, live, HERO)
        relocate(handle, point="14,5")
    assert [s.spell_id for s in events(live, SpellCast)] == ["moonbeam", "hellish-rebuke"]
    radiant = [d for d in events(live, DamageApplied) if d.damage_type == "radiant"]
    assert [d.target_id for d in radiant] == [REACTOR, TARGET]
    assert len([s for s in events(live, SaveRolled) if s.ability == "con"]) == 2
    assert not live.persistent_areas.areas[0].appearance_pending


def test_failed_form_reverts_after_its_damage_before_the_next_target_resolves():
    from dnd5e_engine.events import EffectExpired

    handle, live = table()
    until(handle, live, TARGET)
    act(handle, TARGET, intent_type="use_feature", feature_id="wild-shape", form_id="giant-badger")
    until(handle, live, HERO)
    cast(handle)
    reversion_index = next(
        i
        for i, e in enumerate(live.event_log)
        if isinstance(e, EffectExpired)
        and e.target_id == TARGET
        and e.effect_id == "effect:wild-shape"
    )
    next_save_index = next(
        i
        for i, e in enumerate(live.event_log)
        if isinstance(e, SaveRolled) and e.target_id == SECOND
    )
    assert reversion_index < next_save_index


def test_radiant_immunity_does_not_prevent_failed_save_reversion_or_lock():
    handle, live = shape_table()
    orch._update_combatant(live, TARGET, damage_immunities=["radiant"])
    health = live.tracked_hp[TARGET]
    cast(handle)
    assert events(live, DamageApplied)[0].amount == 0
    assert live.tracked_hp[TARGET] == health
    assert not events(live, SaveRolled)[0].succeeded
    assert TARGET not in live.transforms
    assert TARGET in live.persistent_areas.areas[0].shape_locked_ids
