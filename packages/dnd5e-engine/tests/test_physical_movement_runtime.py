"""Authoritative physical movement: cost, occupancy, drag, and replay."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.monster import CreatureSize

from dnd5e_engine import orchestrator as orch
from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.passive_stats import CombatantMovementModes
from dnd5e_engine.events import (
    ActorMoved,
    AttackRolled,
    CombatantMoved,
    ConditionApplied,
    ConditionRemoved,
    DashTaken,
    EffectApplied,
    MoveFailed,
    SaveRolled,
)
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.live_movement import effective_speed
from dnd5e_engine.persistent_areas import register_area
from dnd5e_engine.specs import GridScene
from dnd5e_engine.types.conditions import ActiveCondition
from dnd5e_engine.types.effects import ActiveEffect, ActiveEffectChange
from tests.c20_support import act, combatant, events, foe, pc, start

HERO = "char:hero"
VICTIM = "mon:victim"


@pytest.fixture(autouse=True)
def _fresh_loader():
    set_lib_loader_for_tests(BundledAssetLoader())
    yield
    set_lib_loader_for_tests(None)


def _start(*, party=None, encounter=None, grid=None, effects=(), **fields):
    return start(
        party or [pc(**({"creature_size": CreatureSize.MEDIUM} | fields))],
        encounter=encounter or [foe(zone_id="9,9", creature_size=CreatureSize.MEDIUM)],
        grid_scene=grid or GridScene(width=12, height=12),
        active_effects=effects,
        seed=7,
    )


def _move(handle, destination, mode="walk"):
    act(handle, HERO, intent_type="move", target_zone_id=destination, movement_mode=mode)


def _snapshot(live):
    return (
        deepcopy(live.actor_zone),
        [c.model_dump(mode="json") for c in live.initiative],
        deepcopy(live.active_effects),
        deepcopy(live.conditions_by_effect),
        deepcopy(live.movement_ledgers),
        live.rng.getstate(),
    )


@pytest.mark.parametrize("mode", ["walk", "crawl", "climb", "swim"])
@pytest.mark.parametrize("difficult", [False, True])
def test_mode_and_difficult_terrain_costs_add_without_drawing_dice(mode, difficult):
    grid = GridScene(width=12, height=12, difficult_terrain_cells=["1,0"] if difficult else [])
    handle, live = _start(grid=grid)
    before_rng = live.rng.getstate()
    _move(handle, "1,0", mode)
    [event] = events(live, ActorMoved)
    expected = 5 * (1 + (mode != "walk") + difficult)
    assert event.movement_cost_ft == expected
    assert event.movement_mode == mode
    assert event.distance_ft == 5
    assert event.path[-1] == "1,0"
    assert combatant(live).movement_remaining == 30 - expected
    ledger = live.movement_ledgers[HERO]
    assert (ledger.spent_ft, ledger.distance_ft, ledger.active_mode) == (expected, 5, mode)
    assert combatant(live).action_available
    assert live.rng.getstate() == before_rng


@pytest.mark.parametrize("mode", ["climb", "swim"])
@pytest.mark.parametrize("difficult", [False, True])
def test_real_special_speed_removes_only_the_mode_extra_cost(mode, difficult):
    handle, live = _start(
        movement_modes=CombatantMovementModes(**{mode: 40}),
        grid=GridScene(width=12, height=12, difficult_terrain_cells=["1,0"] if difficult else []),
    )
    _move(handle, "1,0", mode)
    [event] = events(live, ActorMoved)
    cost = 10 if difficult else 5
    assert event.movement_cost_ft == cost
    assert combatant(live).movement_remaining == 40 - cost


def test_switching_modes_uses_one_ledger_and_dash_does_not_reset_distance_spent():
    handle, live = _start(movement_modes=CombatantMovementModes(swim=40, climb=20))
    _move(handle, "2,0")
    assert combatant(live).movement_remaining == 20
    _move(handle, "3,0", "swim")
    assert combatant(live).movement_remaining == 25
    _move(handle, "4,0", "climb")
    assert combatant(live).movement_remaining == 0
    before = _snapshot(live)
    _move(handle, "5,0", "climb")
    assert _snapshot(live) == before
    act(handle, HERO, intent_type="dash")
    assert combatant(live).movement_remaining == 20
    _move(handle, "5,0", "climb")
    ledger = live.movement_ledgers[HERO]
    assert (ledger.spent_ft, ledger.distance_ft, ledger.dash_count) == (25, 25, 1)
    assert combatant(live).movement_remaining == 15
    assert events(live, DashTaken)[0].budget_consumed == "action"


@pytest.mark.parametrize("mode", ["walk", "climb", "swim"])
def test_prone_rejects_non_crawl_before_any_state_or_rng_change(mode):
    handle, live = _start(
        effects=[
            ActiveEffect(
                id="prone", name="Prone", origin="test:movement", target_id=HERO, statuses={"prone"}
            )
        ]
    )
    before, offset = _snapshot(live), len(live.event_log)
    _move(handle, "1,0", mode)
    assert _snapshot(live) == before
    [event] = live.event_log[offset:]
    assert isinstance(event, MoveFailed)
    assert event.reason == "prone"


def test_prone_crawl_and_stand_up_charge_the_same_turn_ledger():
    handle, live = _start(
        effects=[
            ActiveEffect(
                id="prone", name="Prone", origin="test:movement", target_id=HERO, statuses={"prone"}
            )
        ]
    )
    _move(handle, "1,0", "crawl")
    assert combatant(live).movement_remaining == 20
    act(handle, HERO, intent_type="stand_up")
    assert combatant(live).movement_remaining == 5
    _move(handle, "2,0")
    assert combatant(live).movement_remaining == 0
    assert live.movement_ledgers[HERO].spent_ft == 30
    assert live.movement_ledgers[HERO].distance_ft == 10


@pytest.mark.parametrize(
    "condition", ["grappled", "restrained", "paralyzed", "petrified", "unconscious"]
)
@pytest.mark.parametrize("mode", ["walk", "climb", "swim"])
def test_speed_zero_conditions_stop_special_modes_as_well(condition, mode):
    handle, live = _start(
        movement_modes=CombatantMovementModes(climb=40, swim=50, fly=60, burrow=20),
        effects=[
            ActiveEffect(
                id="zero", name="Zero", origin="test:movement", target_id=HERO, statuses={condition}
            )
        ],
    )
    before = _snapshot(live)
    _move(handle, "1,0", mode)
    assert _snapshot(live) == before
    assert events(live, MoveFailed)[-1].reason == "speed_zero"


def _corridor():
    return GridScene(width=5, height=2, blocked_cells=[f"{col},1" for col in range(4)])


@pytest.mark.parametrize(
    "allied,mover_size,occupant_size,incapacitated,cost",
    [
        (True, CreatureSize.MEDIUM, CreatureSize.MEDIUM, False, 10),
        (False, CreatureSize.MEDIUM, CreatureSize.MEDIUM, True, 15),
        (False, CreatureSize.MEDIUM, CreatureSize.TINY, False, 10),
        (False, CreatureSize.TINY, CreatureSize.MEDIUM, False, 15),
        (False, CreatureSize.MEDIUM, CreatureSize.HUGE, False, 15),
        (False, CreatureSize.HUGE, CreatureSize.MEDIUM, False, 15),
    ],
)
def test_shared_occupancy_rules_allow_passage_and_price_creature_space(
    allied, mover_size, occupant_size, incapacitated, cost
):
    actor = pc(creature_size=mover_size)
    occupant_id = "char:ally" if allied else "mon:blocker"
    occupant = (
        pc(occupant_id, zone_id="1,0", initiative=10, creature_size=occupant_size)
        if allied
        else foe(entity_id=occupant_id, zone_id="1,0", creature_size=occupant_size)
    )
    far_foe = foe(entity_id="mon:far", zone_id="4,1")
    handle, live = _start(
        party=[actor, occupant] if allied else [actor],
        encounter=[far_foe] if allied else [occupant, far_foe],
        grid=_corridor(),
        effects=(
            [
                ActiveEffect(
                    id="incap",
                    name="Incapacitated",
                    origin="test:movement",
                    target_id=occupant_id,
                    statuses={"incapacitated"},
                )
            ]
            if incapacitated
            else []
        ),
    )
    before_rng = live.rng.getstate()
    _move(handle, "2,0")
    [event] = events(live, ActorMoved)
    assert event.movement_cost_ft == cost
    assert "1,0" in event.path
    assert live.actor_zone[HERO] == "2,0"
    assert live.actor_zone[occupant_id] == "1,0"
    assert live.rng.getstate() == before_rng


@pytest.mark.parametrize("destination", ["1,0", "2,0"])
def test_occupied_endpoint_or_impassable_enemy_refuses_whole_route_atomically(destination):
    handle, live = _start(
        encounter=[foe(zone_id="1,0"), foe(entity_id="mon:far", zone_id="4,1")],
        grid=_corridor(),
    )
    before, offset = _snapshot(live), len(live.event_log)
    _move(handle, destination)
    assert _snapshot(live) == before
    assert len(live.event_log[offset:]) == 1
    assert isinstance(live.event_log[offset], MoveFailed)


def test_weighted_route_prefers_cheaper_detour_and_preserves_rng():
    handle, live = _start(
        zone_id="0,1",
        grid=GridScene(width=12, height=12, difficult_terrain_cells=["1,1", "2,1", "3,1"]),
    )
    before_rng = live.rng.getstate()
    _move(handle, "4,1")
    [event] = events(live, ActorMoved)
    assert event.distance_ft == 20
    assert event.movement_cost_ft == 20
    assert not set(event.path).intersection({"1,1", "2,1", "3,1"})
    assert combatant(live).movement_remaining == 10
    assert live.rng.getstate() == before_rng


def test_start_and_destination_cell_aliases_are_canonical_authoritative_ids():
    handle, live = _start(zone_id=" 0 , 0 ", encounter=[foe(zone_id=" 9 , 9 ")])
    assert live.actor_zone == {HERO: "0,0", "mon:foe": "9,9"}
    _move(handle, " 2 , 0 ")
    [event] = events(live, ActorMoved)
    assert (event.from_zone, event.to_zone) == ("0,0", "2,0")
    assert all(" " not in cell for cell in event.path)


@pytest.mark.parametrize("cell_a,cell_b", [("0,0", "0,0"), (" 0 , 0 ", "0,0")])
def test_colliding_start_cells_fail_instead_of_reseating_combatants(cell_a, cell_b):
    with pytest.raises(ValueError, match=r"occup|colli|same cell"):
        _start(party=[pc(zone_id=cell_a), pc("char:ally", zone_id=cell_b, initiative=10)])


def _grapple(
    *,
    victim_size=CreatureSize.MEDIUM,
    mover_size=CreatureSize.MEDIUM,
    difficult=False,
    mode_speed=None,
):
    return _start(
        zone_id="2,0",
        creature_size=mover_size,
        movement_modes=mode_speed or CombatantMovementModes(),
        encounter=[
            foe(entity_id=VICTIM, zone_id="1,0", creature_size=victim_size),
            foe(entity_id="mon:far", zone_id="9,9"),
        ],
        effects=[
            ActiveEffect(
                id="grapple:seed",
                name="Grappled",
                target_id=VICTIM,
                origin=f"grapple:unarmed-strike:{HERO}",
                statuses={"grappled"},
            )
        ],
        grid=GridScene(width=12, height=12, difficult_terrain_cells=["3,0"] if difficult else []),
    )


@pytest.mark.parametrize(
    "victim_size,mover_size,extra",
    [
        (CreatureSize.MEDIUM, CreatureSize.MEDIUM, 1),
        (CreatureSize.TINY, CreatureSize.MEDIUM, 0),
        (CreatureSize.SMALL, CreatureSize.LARGE, 0),
    ],
)
@pytest.mark.parametrize("mode,difficult", [("walk", False), ("crawl", True), ("swim", True)])
def test_grapple_drag_adds_cost_and_moves_victim_into_vacated_cell(
    victim_size, mover_size, extra, mode, difficult
):
    handle, live = _grapple(victim_size=victim_size, mover_size=mover_size, difficult=difficult)
    # The grappler has Disengaged; isolate the drag cost and victim relocation.
    # A Grappled enemy can otherwise legally attack the departing grappler.
    orch._set_disengaging(live, HERO)
    assert orch._condition_source_entity(live, combatant(live, VICTIM), "grappled") == HERO
    victim_before = combatant(live, VICTIM).model_dump()
    before_rng = live.rng.getstate()
    _move(handle, "3,0", mode)
    [moved] = events(live, ActorMoved)
    assert moved.movement_cost_ft == 5 * (1 + (mode != "walk") + difficult + extra)
    [dragged] = events(live, CombatantMoved)
    assert (dragged.actor_id, dragged.from_zone, dragged.to_zone) == (VICTIM, "1,0", "2,0")
    assert dragged.forced
    assert dragged.dragged_by == HERO
    assert dragged.movement_cost_ft == 0
    assert combatant(live, VICTIM).model_dump() == victim_before
    assert live.actor_zone[HERO] == "3,0"
    assert live.actor_zone[VICTIM] == "2,0"
    assert not events(live, AttackRolled)
    assert live.rng.getstate() == before_rng


def test_forced_separation_releases_grapple_effect_and_condition_lineage():
    _, live = _grapple()
    orch.push_combatant(live, VICTIM, origin_cell="2,0", distance_ft=10)
    assert live.actor_zone[VICTIM] == "0,0"
    assert not any(c.condition == "grappled" for c in combatant(live, VICTIM).conditions)
    assert not live.active_effects.get(VICTIM)
    assert not any(key[0] == VICTIM for key in live.conditions_by_effect)
    assert any(
        e.target_id == VICTIM and e.condition == "grappled" for e in events(live, ConditionRemoved)
    )


def test_movement_replay_has_identical_routes_events_budget_and_rng():
    def replay():
        handle, live = _start(
            zone_id="0,1",
            movement_modes=CombatantMovementModes(swim=40),
            grid=GridScene(width=12, height=12, difficult_terrain_cells=["1,1", "2,1"]),
        )
        _move(handle, "3,1")
        _move(handle, "4,1", "swim")
        act(handle, HERO, intent_type="dash")
        _move(handle, "6,1", "swim")
        return _snapshot(live), json.dumps(
            [event.model_dump(mode="json") for event in live.event_log], sort_keys=True
        ).encode()

    assert replay() == replay()


@pytest.mark.parametrize(
    "mode,base", [("walk", 30), ("climb", 40), ("swim", 50), ("fly", 60), ("burrow", 20)]
)
@pytest.mark.parametrize("modifier", ["exhaustion", "slow", "half", "reduction", "zero"])
def test_live_speed_projection_applies_modifiers_to_every_available_speed(mode, base, modifier):
    _, live = _start(movement_modes=CombatantMovementModes(climb=40, swim=50, fly=60, burrow=20))
    if modifier == "exhaustion":
        combatant(live).conditions.append(
            ActiveCondition(
                condition="exhaustion",
                exhaustion_level=2,
                source_entity_id="implied:test",
                scope="combat",
            )
        )
        expected = base - 10
    elif modifier == "slow":
        live.slow_marks[HERO] = {"mon:foe"}
        expected = max(0, base - 10)
    elif modifier == "zero":
        orch._emit(live, ConditionApplied(target_id=HERO, condition="restrained"))
        expected = 0
    else:
        orch._emit(
            live,
            EffectApplied(
                effect=ActiveEffect(
                    id="speed:modifier",
                    name="Typed speed modifier",
                    origin="test:movement",
                    target_id=HERO,
                    changes=[
                        ActiveEffectChange(
                            key="speed.multiplier" if modifier == "half" else "speed.reduction",
                            mode="multiply" if modifier == "half" else "add",
                            value="0.5" if modifier == "half" else 15,
                        )
                    ],
                )
            ),
        )
        expected = base // 2 if modifier == "half" else max(0, base - 15)
    assert effective_speed(combatant(live), mode, live) == expected


def test_persistent_area_halves_all_special_speeds_on_actual_forced_entry():
    handle, live = _start(
        class_slug="cleric",
        character_level=5,
        wisdom=18,
        spells_known=["spirit-guardians"],
        spell_slots={3: 1},
        encounter=[foe(zone_id="6,0")],
    )
    orch._update_combatant(
        live, "mon:foe", movement_modes=CombatantMovementModes(climb=40, swim=50, fly=60, burrow=20)
    )
    act(handle, HERO, intent_type="cast_spell", spell_id="spirit-guardians", slot_level=3)
    orch.push_combatant(live, "mon:foe", origin_cell="7,0", distance_ft=15)
    assert live.actor_zone["mon:foe"] == "3,0"
    for mode, expected in [("walk", 15), ("climb", 20), ("swim", 25), ("fly", 30), ("burrow", 10)]:
        assert effective_speed(combatant(live, "mon:foe"), mode, live) == expected
    act(handle, "mon:foe", intent_type="pass")
    act(handle, HERO, intent_type="drop_concentration")
    assert effective_speed(combatant(live, "mon:foe"), "swim", live) == 50


def test_bonus_action_dash_shares_spent_cost_with_action_dash_and_turn_reset():
    handle, live = _start(
        class_slug="rogue", character_level=2, movement_modes=CombatantMovementModes(swim=40)
    )
    _move(handle, "2,0", "swim")
    act(handle, HERO, intent_type="dash", use_bonus_action=True)
    assert combatant(live).movement_remaining == 70
    assert combatant(live).action_available
    act(handle, HERO, intent_type="dash")
    assert combatant(live).movement_remaining == 110
    assert live.movement_ledgers[HERO].spent_ft == 10
    assert live.movement_ledgers[HERO].dash_count == 2
    act(handle, HERO, intent_type="pass")
    act(handle, "mon:foe", intent_type="pass")
    assert live.current_actor_id == HERO
    ledger = live.movement_ledgers[HERO]
    assert (ledger.spent_ft, ledger.distance_ft, ledger.dash_count, ledger.active_mode) == (
        0,
        0,
        0,
        "walk",
    )
    assert combatant(live).movement_remaining == 30


def test_monster_closing_uses_weighted_route_and_does_not_cross_other_enemy_space():
    handle, live = _start(
        party=[
            pc(zone_id="4,1", initiative=10),
            pc("char:blocker", zone_id="2,1", initiative=5, hp_current=200, hp_max=200),
        ],
        encounter=[foe(zone_id="0,1", initiative=30, monster_template_slug="zombie")],
        grid=GridScene(width=12, height=12, difficult_terrain_cells=["1,1", "3,1"]),
    )
    from tests.e2e.harness import run_async

    run_async(orch.advance_monster_turn(handle))
    moved = events(live, ActorMoved)
    assert moved
    assert all(e.actor_id == "mon:foe" for e in moved)
    assert all(e.to_zone not in {"2,1", "4,1"} for e in moved)
    assert sum(e.movement_cost_ft for e in moved) == 15
    assert live.topology.within_range(live.actor_zone["mon:foe"], "4,1", 5)


def test_multiple_grapple_victims_pack_without_stacking_or_double_drag_cost():
    def effect(target):
        return ActiveEffect(
            id=f"grapple:{target}",
            name="Grappled",
            target_id=target,
            origin=f"grapple:unarmed-strike:{HERO}",
            statuses={"grappled"},
        )

    handle, live = _start(
        zone_id="2,2",
        encounter=[
            foe(entity_id="mon:first", initiative=5, zone_id="1,2"),
            foe(entity_id="mon:second", initiative=4, zone_id="2,1"),
            foe(entity_id="mon:far", initiative=1, zone_id="9,9"),
        ],
        effects=[effect("mon:first"), effect("mon:second")],
    )
    _move(handle, "3,2")
    [moved] = events(live, ActorMoved)
    assert moved.movement_cost_ft == 10
    dragged = events(live, CombatantMoved)
    # Packing can legally keep a later victim in place when it remains in reach.
    assert [e.actor_id for e in dragged] == ["mon:first"]
    assert live.actor_zone["mon:first"] == "2,2"
    assert len(set(live.actor_zone.values())) == len(live.actor_zone)
    assert all(e.distance_ft <= 5 for e in dragged)
    assert all(
        live.topology.within_range("3,2", live.actor_zone[target], 5)
        for target in ("mon:first", "mon:second")
    )


def test_dragging_source_incapacitation_releases_each_victim_and_lineage():
    _, live = _grapple()
    orch._emit(live, ConditionApplied(target_id=HERO, condition="incapacitated"))
    assert not any(c.condition == "grappled" for c in combatant(live, VICTIM).conditions)
    assert not live.active_effects.get(VICTIM)
    assert not any(key[0] == VICTIM for key in live.conditions_by_effect)


def test_dragged_victim_leaving_enemy_reach_does_not_provoke_opportunity_attack():
    handle, live = _start(
        party=[pc(zone_id="2,0"), pc("char:watcher", zone_id="0,0", initiative=10)],
        encounter=[foe(entity_id=VICTIM, zone_id="1,0"), foe(entity_id="mon:far", zone_id="9,9")],
        effects=[
            ActiveEffect(
                id="grapple:seed",
                name="Grappled",
                target_id=VICTIM,
                origin=f"grapple:unarmed-strike:{HERO}",
                statuses={"grappled"},
            )
        ],
    )
    orch._set_disengaging(live, HERO)
    before_rng = live.rng.getstate()
    _move(handle, "3,0")
    assert live.actor_zone[VICTIM] == "2,0"
    assert combatant(live, "char:watcher").reaction_available
    assert combatant(live, VICTIM).reaction_available
    assert not events(live, AttackRolled)
    assert live.rng.getstate() == before_rng


def test_dragged_victim_runs_actual_persistent_area_entry_after_move_event():
    handle, live = _grapple()
    orch._set_disengaging(live, HERO)
    orch._update_combatant(live, VICTIM, dexterity=-30)
    item = BundledAssetLoader().get_item("ball-bearings")
    assert item is not None
    activity = next(a for a in item.activities if a.persistent_area is not None)
    ctx = build_activity_context(
        combatant(live),
        [],
        rng=live.rng,
        event_emitter=lambda event: orch._emit(live, event),
        spellcasting_ability=None,
        source_passive_effects=item.passive_effects,
        slot_level=None,
        base_spell_level=None,
        concentration=False,
        spell_book={},
        passive_damage_modifiers={},
        save_modifiers={},
    )
    register_area(live, activity, ctx, source_id=item.slug, origin="2,0")
    _move(handle, "3,0")
    [dragged] = events(live, CombatantMoved)
    save = next(e for e in events(live, SaveRolled) if e.target_id == VICTIM)
    assert live.event_log.index(dragged) < live.event_log.index(save)
    assert not save.succeeded
    assert "prone" in live.active_conditions[VICTIM]
    assert "grappled" in live.active_conditions[VICTIM]


def test_adrenaline_rush_uses_current_mode_dash_allowance_without_refunding_spent_cost():
    handle, live = _start(species_slug="orc", movement_modes=CombatantMovementModes(swim=40))
    _move(handle, "2,0", "swim")
    before_rng = live.rng.getstate()
    act(
        handle,
        HERO,
        intent_type="use_feature",
        feature_id="adrenaline-rush",
        activity_id="jN5Zo6mDXll3c4Cz",
    )
    ledger = live.movement_ledgers[HERO]
    assert (ledger.spent_ft, ledger.distance_ft, ledger.dash_count) == (10, 10, 1)
    assert combatant(live).movement_remaining == 70
    assert events(live, DashTaken)[0].budget_consumed == "bonus_action"
    assert live.rng.getstate() == before_rng


def test_monster_template_special_speed_is_hydrated_and_used_by_mode_cost():
    _, live = _start(encounter=[foe(zone_id="9,9", monster_template_slug="giant-spider")])
    spider = combatant(live, "mon:foe")
    assert spider.movement_modes.climb == 30
    assert effective_speed(spider, "climb", live) == 30


def test_actual_stunning_success_halves_every_special_speed():
    from tests.test_attack_rider_runtime import STUN, _attack, _monk, _request

    handle, live = _start(party=[_monk()], encounter=[foe(zone_id="1,0")])
    orch._update_combatant(
        live,
        "mon:foe",
        constitution=40,
        movement_modes=CombatantMovementModes(climb=40, swim=50, fly=60, burrow=20),
    )
    _attack(handle, _request(STUN))
    save = next(e for e in events(live, SaveRolled) if e.ability == "con")
    assert save.succeeded
    for mode, expected in [("walk", 15), ("climb", 20), ("swim", 25), ("fly", 30), ("burrow", 10)]:
        assert effective_speed(combatant(live, "mon:foe"), mode, live) == expected


def test_slow_opportunity_attack_reprices_swimming_and_keeps_completed_steps():
    handle, live = _start(
        party=[pc(zone_id="4,1", initiative=10, strength=16, attack_bonus=20, equipment=("club",))],
        encounter=[foe(zone_id="1,0", initiative=30)],
    )
    orch._update_combatant(live, "mon:foe", movement_modes=CombatantMovementModes(swim=40))
    act(handle, "mon:foe", intent_type="move", target_zone_id="9,0", movement_mode="swim")
    [attack] = events(live, AttackRolled)
    assert attack.is_opportunity_attack
    assert attack.is_hit
    moved = events(live, ActorMoved)
    assert moved[0].from_zone == "1,0"
    assert moved[0].to_zone == "5,0"
    assert live.event_log.index(moved[0]) < live.event_log.index(attack)
    assert live.actor_zone["mon:foe"] == "7,0"
    assert sum(e.movement_cost_ft for e in moved) == 30
    assert combatant(live, "mon:foe").movement_remaining == 0
    assert live.movement_ledgers["mon:foe"].spent_ft == 30


def test_cheapest_route_can_take_more_steps_than_the_geometric_shortest_route():
    handle, live = _start(
        zone_id="0,2",
        base_speed=60,
        grid=GridScene(
            width=12,
            height=12,
            difficult_terrain_cells=[f"{col},2" for col in range(1, 6)],
            blocked_cells=[f"{col},{row}" for row in (1, 3) for col in range(1, 6)],
        ),
    )
    _move(handle, "6,2")
    [event] = events(live, ActorMoved)
    assert event.distance_ft > 30
    assert event.movement_cost_ft < 55
    assert event.to_zone == "6,2"


@pytest.mark.parametrize("removed", [HERO, VICTIM])
def test_either_grapple_participant_departing_cleans_condition_effect_and_lineage(removed):
    _, live = _grapple()
    orch._leave_roster(live, removed, "zero_hp")
    assert not live.active_effects.get(VICTIM)
    assert not any(key[0] == VICTIM for key in live.conditions_by_effect)
    if removed == HERO:
        assert not any(c.condition == "grappled" for c in combatant(live, VICTIM).conditions)
