"""Movement preflight and OA revalidation at authoritative step boundaries."""

from __future__ import annotations

import asyncio
import random
from typing import Any

import pytest

from dnd5e_engine import live_movement as movement
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.events import ActorMoved, CombatantMoved, ConditionRemoved, DashTaken
from dnd5e_engine.movement import MovementLedger
from dnd5e_engine.spatial import GridTopology
from dnd5e_engine.specs import GridScene, WallSegment
from dnd5e_engine.types.combat import Combatant
from dnd5e_engine.types.conditions import ActiveCondition

HERO = "char:aaaaaaaaaaaa"
FOE = "mon:bbbbbbbbbbbb"
ALLY = "char:cccccccccccc"
SECOND = "mon:dddddddddddd"


def _actor(entity_id: str, **fields: Any) -> Combatant:
    return Combatant(
        **(
            dict(
                entity_id=entity_id,
                entity_type="Character" if entity_id.startswith("char:") else "Monster",
                name=entity_id,
                initiative=20,
                hp_current=30,
                hp_max=30,
                base_speed=30,
                movement_remaining=30,
            )
            | fields
        )
    )


def _condition(name: str, source: str) -> ActiveCondition:
    return ActiveCondition(condition=name, source_entity_id=source, scope="combat")


def _live(
    actors: list[Combatant], positions: dict[str, str], grid: GridScene | None = None
) -> orch._LiveCombat:
    return orch._LiveCombat(
        handle_id="movement:boundaries",
        session_id="movement:boundaries",
        initiative=actors,
        party_ids={c.entity_id for c in actors if c.entity_id.startswith("char:")},
        encounter_ids={c.entity_id for c in actors if c.entity_id.startswith("mon:")},
        topology=GridTopology(grid or GridScene(width=10, height=10)),
        rng=random.Random(731),
        event_queue=asyncio.Queue(),
        scene_location_id="movement:scene",
        actor_zone=positions,
    )


@pytest.mark.parametrize("blocked", ["zero_hp", "not_alive", "dead_id", "frightened", "drag"])
def test_static_step_failure_never_opens_an_opportunity_window(
    blocked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    hero = _actor(HERO)
    foe = _actor(FOE)
    live = _live([hero, foe], {HERO: "0,0", FOE: "2,0"})
    if blocked == "zero_hp":
        orch._update_combatant(live, HERO, hp_current=0)
    elif blocked == "not_alive":
        orch._update_combatant(live, HERO, is_alive=False)
    elif blocked == "dead_id":
        live.dead_ids.add(HERO)
    elif blocked == "frightened":
        orch._update_combatant(live, HERO, conditions=[_condition("frightened", FOE)])
    else:
        orch._update_combatant(live, HERO, melee_reach_ft=10)
        orch._update_combatant(live, FOE, conditions=[_condition("grappled", HERO)])
    calls: list[dict[str, Any]] = []

    def opportunity(_live: orch._LiveCombat, **kwargs: Any) -> bool:
        calls.append(kwargs)
        return False

    monkeypatch.setattr(orch, "_fire_opportunity_attacks_on_step", opportunity)
    rng_before = live.rng.getstate()
    assert movement.take_step(live, HERO, "1,0", end_move=True) is None
    assert calls == []
    assert live.actor_zone[HERO] == "0,0"
    assert live.movement_ledgers[HERO].spent_ft == 0
    assert live.event_log == []
    assert live.rng.getstate() == rng_before


def test_oa_speed_change_cancels_step_without_refunding_the_reaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hero, foe = _actor(HERO), _actor(FOE)
    live = _live([hero, foe], {HERO: "0,0", FOE: "0,1"})
    live.movement_ledgers[HERO] = MovementLedger(spent_ft=25, distance_ft=25)
    calls = 0

    def opportunity(_live: orch._LiveCombat, **_kwargs: Any) -> bool:
        nonlocal calls
        calls += 1
        orch._update_combatant(live, FOE, reaction_available=False)
        orch._update_combatant(live, HERO, base_speed=25)
        return False

    monkeypatch.setattr(orch, "_fire_opportunity_attacks_on_step", opportunity)
    assert movement.take_step(live, HERO, "1,0", end_move=True) is None
    assert calls == 1
    assert live.actor_zone[HERO] == "0,0"
    assert live.movement_ledgers[HERO].spent_ft == 25
    assert not orch._find_combatant(live, FOE).reaction_available
    assert live.event_log == []


def test_oa_new_grapple_reprices_and_relocates_the_victim_before_payment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hero, foe = _actor(HERO), _actor(FOE)
    live = _live([hero, foe], {HERO: "1,0", FOE: "0,0"})

    def opportunity(_live: orch._LiveCombat, **_kwargs: Any) -> bool:
        orch._update_combatant(live, FOE, conditions=[_condition("grappled", HERO)])
        return False

    monkeypatch.setattr(orch, "_fire_opportunity_attacks_on_step", opportunity)
    assert movement.take_step(live, HERO, "2,0", end_move=True) == (10, {FOE: "1,0"})
    assert live.actor_zone == {HERO: "2,0", FOE: "1,0"}
    assert live.movement_ledgers[HERO].spent_ft == 10
    assert [type(event) for event in live.event_log] == [ActorMoved, CombatantMoved]


def test_oa_new_ally_at_endpoint_cancels_final_step_but_passage_remains_legal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hero, ally = _actor(HERO), _actor(ALLY)
    live = _live([hero, ally], {HERO: "0,0", ALLY: "3,0"})

    def opportunity(_live: orch._LiveCombat, **_kwargs: Any) -> bool:
        live.actor_zone[ALLY] = "1,0"
        return False

    monkeypatch.setattr(orch, "_fire_opportunity_attacks_on_step", opportunity)
    assert movement.take_step(live, HERO, "1,0", end_move=True) is None
    assert live.movement_ledgers[HERO].spent_ft == 0
    assert live.actor_zone[HERO] == "0,0"
    assert movement.movement_cost(live, hero, "0,0", "1,0", "walk") == 5


def test_monster_without_an_action_does_not_end_inside_an_ally_pass_through_space() -> None:
    monster, ally, target = _actor(FOE, action_available=False), _actor(SECOND), _actor(HERO)
    live = _live(
        [monster, ally, target],
        {FOE: "0,0", SECOND: "1,0", HERO: "3,0"},
        GridScene(width=4, height=1),
    )
    live.movement_ledgers[FOE] = MovementLedger(spent_ft=25, distance_ft=25)
    rng_before = live.rng.getstate()
    assert not movement.close_to_target(live, monster, target, 5)
    assert live.actor_zone[FOE] == "0,0"
    assert live.movement_ledgers[FOE].spent_ft == 25
    assert not any(isinstance(event, DashTaken) for event in live.event_log)
    assert live.rng.getstate() == rng_before


def test_monster_follows_a_wall_detour_even_when_its_first_step_is_not_closer() -> None:
    monster, target = _actor(FOE, action_available=False), _actor(HERO)
    live = _live(
        [monster, target],
        {FOE: "0,1", HERO: "2,1"},
        GridScene(width=4, height=5, wall_segments=[WallSegment(x1=1, y1=0, x2=1, y2=3)]),
    )
    live.movement_ledgers[FOE] = MovementLedger(spent_ft=25, distance_ft=25)
    rng_before = live.rng.getstate()
    assert not movement.close_to_target(live, monster, target, 5)
    assert live.actor_zone[FOE] == "0,2"
    assert live.movement_ledgers[FOE].spent_ft == 30
    assert live.rng.getstate() == rng_before


def test_two_dragged_victims_keep_distinct_cells_and_the_same_extra_foot_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hero = _actor(HERO)
    first = _actor(FOE, conditions=[_condition("grappled", HERO)])
    second = _actor(SECOND, conditions=[_condition("grappled", HERO)])
    live = _live([hero, first, second], {HERO: "1,1", FOE: "0,1", SECOND: "1,0"})
    monkeypatch.setattr(orch, "_fire_opportunity_attacks_on_step", lambda *_a, **_k: False)
    assert movement.take_step(live, HERO, "2,1", end_move=True)[0] == 10
    assert movement.take_step(live, HERO, "3,1", end_move=True)[0] == 10
    assert len(set(live.actor_zone.values())) == 3
    assert all(live.topology.within_range("3,1", live.actor_zone[v], 5) for v in (FOE, SECOND))
    assert live.movement_ledgers[HERO].spent_ft == 20
    assert all(orch._condition_source_entity(live, v, "grappled") == HERO for v in (first, second))


def test_push_emits_the_position_change_before_grapple_range_cleanup() -> None:
    hero = _actor(HERO)
    victim = _actor(FOE, conditions=[_condition("grappled", HERO)])
    live = _live([hero, victim], {HERO: "0,0", FOE: "1,0"})
    rng_before = live.rng.getstate()
    movement.push(live, FOE, "0,0", 10)
    assert [type(event) for event in live.event_log] == [
        CombatantMoved,
        ConditionRemoved,
        CombatantMoved,
    ]
    assert live.actor_zone[FOE] == "3,0"
    assert not orch._find_combatant(live, FOE).conditions
    assert live.movement_ledgers[FOE].spent_ft == 0
    assert live.rng.getstate() == rng_before
