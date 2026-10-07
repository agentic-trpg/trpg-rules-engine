"""Immediate feature movement shares physical steps and owns its own cost cap."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.monster import CreatureSize
from pydantic import ValidationError

from dnd5e_engine import live_movement as movement
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.events import ActorMoved, AttackRolled, CombatantMoved, Death, SaveRolled
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.movement import MovementChoice, MovementGrant, MovementLedger
from dnd5e_engine.persistent_areas import register_area
from dnd5e_engine.specs import GridScene
from dnd5e_engine.types.effects import ActiveEffect
from tests.c20_support import act, combatant, events, foe, pc, start

HERO, TARGET = "char:hero", "mon:foe"


@pytest.fixture(autouse=True)
def _fresh_loader():
    set_lib_loader_for_tests(BundledAssetLoader())
    yield
    set_lib_loader_for_tests(None)


def _start(*, party=None, encounter=None, grid=None, effects=(), **fields):
    return start(
        party or [pc(**fields)],
        seed=7,
        encounter=encounter or [foe(zone_id="8,0")],
        grid_scene=grid or GridScene(width=10, height=4),
        active_effects=effects,
    )


def _grant(*, cap=15, direction="any", **fields):
    return MovementGrant(
        max_distance_ft=cap,
        source_id="canonical:grant",
        direction=direction,
        target_id=TARGET if direction != "any" else None,
        **fields,
    )


def _snapshot(live):
    return (
        deepcopy(live.actor_zone),
        deepcopy(live.movement_ledgers),
        [actor.model_dump(mode="json") for actor in live.initiative],
        deepcopy(live.active_effects),
        list(live.event_log),
        live.rng.getstate(),
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"distance_ft": True},
        {"distance_ft": 1.5},
        {"distance_ft": "5"},
        {"distance_ft": -1},
        {"destination_cell": "nonsense"},
        {"destination_cell": "1,0", "distance_ft": 0},
        {"max_distance_ft": 15},
        {"provokes_opportunity_attacks": False},
    ],
)
def test_movement_choice_rejects_malformed_or_host_authored_rules(fields):
    with pytest.raises(ValidationError):
        MovementChoice(**fields)


def test_movement_choice_is_frozen_and_canonicalizes_cell_aliases():
    choice = MovementChoice(destination_cell=" 01, 00 ")
    assert choice.destination_cell == "1,0"
    with pytest.raises(ValidationError):
        choice.distance_ft = 5


@pytest.mark.parametrize(
    "choice",
    [None, MovementChoice(), MovementChoice(distance_ft=0), MovementChoice(destination_cell="0,0")],
)
def test_zero_optional_movement_is_valid_and_draw_free(choice):
    _, live = _start()
    before = _snapshot(live)
    movement.preflight_movement_grant(live, HERO, _grant(cap=0), choice)
    movement.execute_movement_grant(live, HERO, _grant(cap=0), choice)
    assert _snapshot(live) == before


@pytest.mark.parametrize("destination", ["20,0", "-1,0", "4,0", "8,0"])
def test_unrestricted_preflight_rejects_outside_cap_or_grid_without_mutation(destination):
    _, live = _start()
    before = _snapshot(live)
    with pytest.raises(ValueError):
        movement.preflight_movement_grant(
            live, HERO, _grant(), MovementChoice(destination_cell=destination)
        )
    assert _snapshot(live) == before


def test_unrestricted_nonzero_distance_without_destination_is_rejected():
    _, live = _start()
    with pytest.raises(ValueError, match="requires a destination"):
        movement.preflight_movement_grant(live, HERO, _grant(), MovementChoice(distance_ft=5))


def test_independent_weighted_allowance_works_with_exhausted_ordinary_ledger():
    _, live = _start(grid=GridScene(width=10, height=1, difficult_terrain_cells=["1,0"]))
    live.movement_ledgers[HERO] = MovementLedger(spent_ft=30, distance_ft=30)
    movement.project_remaining(live, HERO)
    before = live.movement_ledgers[HERO], combatant(live).movement_remaining, live.rng.getstate()
    choice = MovementChoice(destination_cell="2,0")
    movement.preflight_movement_grant(live, HERO, _grant(), choice)
    movement.execute_movement_grant(live, HERO, _grant(), choice)
    assert live.actor_zone[HERO] == "2,0"
    assert [event.movement_cost_ft for event in events(live, ActorMoved)] == [10, 5]
    assert (
        live.movement_ledgers[HERO],
        combatant(live).movement_remaining,
        live.rng.getstate(),
    ) == before
    assert not combatant(live).disengaging_this_turn


def test_terrain_cost_can_refuse_route_that_geometric_half_speed_would_reach():
    _, live = _start(grid=GridScene(width=10, height=1, difficult_terrain_cells=["1,0", "2,0"]))
    before = _snapshot(live)
    with pytest.raises(ValueError, match="route cost"):
        movement.preflight_movement_grant(
            live, HERO, _grant(), MovementChoice(destination_cell="2,0")
        )
    assert _snapshot(live) == before


def test_dash_cannot_expand_grant_cap_and_ordinary_ledger_is_preserved():
    _, live = _start()
    live.movement_ledgers[HERO] = MovementLedger(spent_ft=5, distance_ft=5, dash_count=2)
    movement.project_remaining(live, HERO)
    before = live.movement_ledgers[HERO], combatant(live).movement_remaining
    with pytest.raises(ValueError, match="exceeds"):
        movement.preflight_movement_grant(
            live, HERO, _grant(), MovementChoice(destination_cell="4,0")
        )
    movement.execute_movement_grant(live, HERO, _grant(), MovementChoice(destination_cell="3,0"))
    assert live.actor_zone[HERO] == "3,0"
    assert (live.movement_ledgers[HERO], combatant(live).movement_remaining) == before


def test_unrestricted_route_can_pass_allied_space_but_cannot_end_there():
    _, live = _start(party=[pc(), pc("char:ally", zone_id="1,0", initiative=15)])
    with pytest.raises(ValueError, match="legal route"):
        movement.preflight_movement_grant(
            live, HERO, _grant(), MovementChoice(destination_cell="1,0")
        )
    choice = MovementChoice(destination_cell="2,0")
    movement.preflight_movement_grant(live, HERO, _grant(), choice)
    movement.execute_movement_grant(live, HERO, _grant(), choice)
    assert live.actor_zone[HERO] == "2,0"
    assert live.actor_zone["char:ally"] == "1,0"


def test_scoped_oa_exemption_does_not_suppress_later_ordinary_movement():
    handle, live = _start(encounter=[foe(zone_id="1,0", monster_template_slug="zombie")])
    before = live.rng.getstate()
    movement.execute_movement_grant(live, HERO, _grant(), MovementChoice(destination_cell="0,2"))
    assert live.actor_zone[HERO] == "0,2"
    assert not events(live, AttackRolled)
    assert live.rng.getstate() == before
    assert combatant(live, TARGET).reaction_available
    assert not combatant(live).disengaging_this_turn
    act(handle, HERO, intent_type="move", target_zone_id="0,1")
    act(handle, HERO, intent_type="move", target_zone_id="0,2")
    attacks = events(live, AttackRolled)
    assert len(attacks) == 1
    assert attacks[0].is_opportunity_attack
    assert not combatant(live, TARGET).reaction_available


def _drag_combat(*, difficult=False):
    return _start(
        zone_id="2,0",
        creature_size=CreatureSize.MEDIUM,
        encounter=[foe(zone_id="1,0", creature_size=CreatureSize.MEDIUM)],
        grid=GridScene(width=10, height=1, difficult_terrain_cells=["3,0"] if difficult else []),
        effects=[
            ActiveEffect(
                id="grapple:seed",
                name="Grappled",
                target_id=TARGET,
                origin=f"grapple:unarmed-strike:{HERO}",
                statuses={"grappled"},
            )
        ],
    )


def test_drag_and_terrain_share_grant_cost_and_victim_never_provokes_oa():
    _, live = _drag_combat(difficult=True)
    before = deepcopy(live.movement_ledgers)
    choice = MovementChoice(destination_cell="3,0")
    movement.preflight_movement_grant(live, HERO, _grant(), choice)
    movement.execute_movement_grant(live, HERO, _grant(), choice)
    [moved] = events(live, ActorMoved)
    [dragged] = events(live, CombatantMoved)
    assert moved.movement_cost_ft == 15
    assert dragged.dragged_by == HERO
    assert dragged.forced
    assert live.actor_zone[HERO] == "3,0"
    assert live.actor_zone[TARGET] == "2,0"
    assert live.movement_ledgers == before
    assert not events(live, AttackRolled)


def _area(live, slug, origin, *, massive_damage=False):
    item = BundledAssetLoader().get_item(slug)
    assert item is not None
    activity = next(
        activity for activity in item.activities if activity.persistent_area is not None
    ).model_copy(deep=True)
    if massive_damage:
        part = activity.damage.parts[0]
        part = part.model_copy(update={"custom": part.custom.model_copy(update={"formula": "100"})})
        activity = activity.model_copy(
            update={"damage": activity.damage.model_copy(update={"parts": [part]})}
        )
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
    register_area(live, activity, ctx, source_id=item.slug, origin=origin)


def test_dragged_victim_still_triggers_real_persistent_area_entry():
    _, live = _drag_combat()
    orch._update_combatant(live, TARGET, dexterity=-30)
    _area(live, "ball-bearings", "2,0")
    movement.execute_movement_grant(live, HERO, _grant(), MovementChoice(destination_cell="3,0"))
    [dragged] = events(live, CombatantMoved)
    save = next(event for event in events(live, SaveRolled) if event.target_id == TARGET)
    assert live.event_log.index(dragged) < live.event_log.index(save)
    assert "prone" in live.active_conditions[TARGET]
    assert "grappled" in live.active_conditions[TARGET]


@pytest.mark.parametrize("massive_damage", [False, True])
def test_actual_area_speed_zero_or_death_keeps_completed_step_then_stops(massive_damage):
    _, live = _start(dexterity=-30, hp_current=5, hp_max=5)
    _area(live, "caltrops", "1,0", massive_damage=massive_damage)
    before = deepcopy(live.movement_ledgers)
    movement.execute_movement_grant(live, HERO, _grant(), MovementChoice(destination_cell="3,0"))
    assert live.actor_zone[HERO] == "1,0"
    assert len(events(live, ActorMoved)) == 1
    assert live.movement_ledgers == before
    if massive_damage:
        assert events(live, Death)
    else:
        assert movement.effective_speed(combatant(live), "walk", live) == 0


def test_forced_relocation_during_entry_stops_remaining_original_route(monkeypatch):
    _, live = _start()
    original = movement.post_position_steps
    triggered = False

    def relocate(combat, previous):
        nonlocal triggered
        original(combat, previous)
        if not triggered:
            triggered = True
            orch.push_combatant(combat, HERO, origin_cell="0,0", distance_ft=5)

    monkeypatch.setattr(movement, "post_position_steps", relocate)
    movement.execute_movement_grant(live, HERO, _grant(), MovementChoice(destination_cell="3,0"))
    assert len(events(live, ActorMoved)) == 1
    assert live.actor_zone[HERO] == "2,0"
    assert events(live, CombatantMoved)


@pytest.mark.parametrize(
    "choice", [MovementChoice(distance_ft=15), MovementChoice(destination_cell="3,0")]
)
def test_directed_movement_reads_actual_post_push_target_cell(choice):
    _, live = _start(encounter=[foe(zone_id="1,0")])
    grant = _grant(direction="toward_target")
    movement.preflight_movement_grant(live, HERO, grant, choice)
    orch.push_combatant(live, TARGET, origin_cell="0,0", distance_ft=15)
    assert live.actor_zone[TARGET] == "4,0"
    movement.execute_movement_grant(live, HERO, grant, choice)
    assert live.actor_zone[HERO] == "3,0"
    assert not events(live, AttackRolled)


def test_partial_push_reference_and_target_occupancy_limit_follow():
    _, live = _start(
        encounter=[foe(zone_id="1,0")], grid=GridScene(width=10, height=1, blocked_cells=["3,0"])
    )
    grant, choice = _grant(direction="toward_target"), MovementChoice(distance_ft=15)
    movement.preflight_movement_grant(live, HERO, grant, choice)
    orch.push_combatant(live, TARGET, origin_cell="0,0", distance_ft=15)
    assert live.actor_zone[TARGET] == "2,0"
    movement.execute_movement_grant(live, HERO, grant, choice)
    assert live.actor_zone[HERO] == "1,0"


@pytest.mark.parametrize("blocking", ["wall", "ally", "enemy"])
def test_directed_dynamic_obstruction_stops_without_detour_or_exception(blocking):
    party = [pc()]
    encounter = [foe(zone_id="4,0")]
    if blocking == "ally":
        party.append(pc("char:blocker", zone_id="2,0", initiative=15))
    elif blocking == "enemy":
        encounter.append(foe(entity_id="mon:blocker", zone_id="2,0"))
    grid = GridScene(width=10, height=3, blocked_cells=["2,0"] if blocking == "wall" else [])
    _, live = _start(party=party, encounter=encounter, grid=grid)
    choice, grant = MovementChoice(distance_ft=15), _grant(direction="toward_target")
    movement.preflight_movement_grant(live, HERO, grant, choice)
    movement.execute_movement_grant(live, HERO, grant, choice)
    assert live.actor_zone[HERO] == "1,0"


def test_directed_allowance_charges_terrain_and_executes_only_complete_steps():
    _, live = _start(
        encounter=[foe(zone_id="4,0")],
        grid=GridScene(width=10, height=1, difficult_terrain_cells=["1,0", "2,0"]),
    )
    movement.execute_movement_grant(
        live, HERO, _grant(direction="toward_target"), MovementChoice(distance_ft=15)
    )
    assert live.actor_zone[HERO] == "1,0"
    [moved] = events(live, ActorMoved)
    assert moved.movement_cost_ft == 10


@pytest.mark.parametrize("target,destination", [("4,0", "2,1"), ("4,2", None)])
def test_non_collinear_destination_or_ray_has_no_legal_straight_grid_movement(target, destination):
    _, live = _start(encounter=[foe(zone_id=target)])
    before = _snapshot(live)
    choice = (
        MovementChoice(destination_cell=destination)
        if destination
        else MovementChoice(distance_ft=15)
    )
    movement.execute_movement_grant(live, HERO, _grant(direction="toward_target"), choice)
    assert _snapshot(live) == before


def test_diagonal_ray_and_fractional_cell_distance_use_complete_collinear_steps():
    _, live = _start(encounter=[foe(zone_id="3,3")])
    movement.execute_movement_grant(
        live, HERO, _grant(direction="toward_target"), MovementChoice(distance_ft=12)
    )
    assert live.actor_zone[HERO] == "2,2"
    assert [event.path for event in events(live, ActorMoved)] == [("0,0", "1,1"), ("1,1", "2,2")]


def test_scoped_movement_replays_events_positions_ledger_and_rng_byte_equivalently():
    def replay():
        _, live = _drag_combat(difficult=True)
        movement.execute_movement_grant(
            live, HERO, _grant(), MovementChoice(destination_cell="3,0")
        )
        return json.dumps(
            {
                "positions": live.actor_zone,
                "ledger": {
                    actor_id: ledger.model_dump(mode="json")
                    for actor_id, ledger in live.movement_ledgers.items()
                },
                "actors": [actor.model_dump(mode="json") for actor in live.initiative],
                "events": [event.model_dump(mode="json") for event in live.event_log],
                "rng": live.rng.getstate(),
            },
            sort_keys=True,
        ).encode()

    assert replay() == replay()
