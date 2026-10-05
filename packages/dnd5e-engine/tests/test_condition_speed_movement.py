"""Speed-zero projection preserves authoritative movement, events, and RNG."""

from __future__ import annotations

import pytest

from dnd5e_engine import PlayerIntent
from dnd5e_engine.events import (
    ActorMoved,
    CombatantMoved,
    ConditionApplied,
    ConditionRemoved,
    DashTaken,
    IntentSubmitted,
    MoveFailed,
    TurnPhase,
)
from dnd5e_engine.orchestrator import (
    IntentRejectedError,
    _effective_speed,
    _emit,
    _find_combatant,
    _get_live,
    advance_monster_turn,
    push_combatant,
    start_combat,
    submit_player_intent,
)
from dnd5e_engine.results import StartCombatResult
from dnd5e_engine.specs import EncounterMemberSpec, PartyMemberSpec
from dnd5e_engine.types.effects import ActiveEffect
from tests.e2e.harness import cell, grid_scene, run_async

_ZERO_SPEED_CONDITIONS = ("grappled", "restrained", "paralyzed", "petrified", "unconscious")


def _start(*conditions: str, monster_first: bool = False) -> StartCombatResult:
    target_id = "mon:foe" if monster_first else "char:hero"
    return run_async(
        start_combat(
            session_id="speed-projection-movement",
            party=[
                PartyMemberSpec(
                    entity_id="char:hero",
                    name="Hero",
                    initiative=1 if monster_first else 20,
                    hp_current=50,
                    hp_max=50,
                    base_speed=45,
                    zone_id=cell(0, 0),
                )
            ],
            encounter=[
                EncounterMemberSpec(
                    entity_id="mon:foe",
                    entity_type="Monster",
                    name="Foe",
                    initiative=20 if monster_first else 1,
                    hp_current=50,
                    hp_max=50,
                    zone_id=cell(5, 0),
                    monster_template_slug="zombie",
                )
            ],
            grid_scene=grid_scene(),
            rng_seed=7,
            active_effects=()
            if not conditions
            else (
                ActiveEffect(
                    id="effect:speed",
                    name="Movement conditions",
                    origin="test:speed",
                    target_id=target_id,
                    statuses=set(conditions),
                ),
            ),
        )
    )


@pytest.mark.parametrize("condition", _ZERO_SPEED_CONDITIONS)
def test_speed_zero_blocks_voluntary_movement_with_identical_events_and_rng(condition: str) -> None:
    start = _start(condition)
    live = _get_live(start.handle)
    before_rng = live.rng.getstate()
    event_count = len(live.event_log)
    before = _find_combatant(live, "char:hero")
    assert before is not None
    assert before.movement_remaining == 0
    run_async(
        submit_player_intent(
            start.handle,
            actor_id="char:hero",
            intent=PlayerIntent(intent_type="move", target_zone_id=cell(1, 0)),
        )
    )
    events = live.event_log[event_count:]
    assert [event.type for event in events] == ["move_failed"]
    assert isinstance(events[0], MoveFailed)
    assert events[0].actor_id == "char:hero"
    assert events[0].reason == "speed_zero"
    assert live.actor_zone["char:hero"] == cell(0, 0)
    assert _find_combatant(live, "char:hero") == before
    assert live.rng.getstate() == before_rng


@pytest.mark.parametrize("condition", ["grappled", "restrained"])
def test_zero_speed_dash_pays_action_without_adding_movement_or_drawing_dice(
    condition: str,
) -> None:
    start = _start(condition)
    live = _get_live(start.handle)
    before_rng = live.rng.getstate()
    event_count = len(live.event_log)
    run_async(
        submit_player_intent(
            start.handle, actor_id="char:hero", intent=PlayerIntent(intent_type="dash")
        )
    )
    events = live.event_log[event_count:]
    assert [event.type for event in events] == ["dash_taken"]
    assert isinstance(events[0], DashTaken)
    assert events[0].doubled_movement_remaining == 0
    assert events[0].budget_consumed == "action"
    hero = _find_combatant(live, "char:hero")
    assert hero is not None
    assert hero.movement_remaining == 0
    assert hero.action_available is False
    assert live.current_actor_id == hero.entity_id
    assert live.rng.getstate() == before_rng


@pytest.mark.parametrize("condition", ["grappled", "restrained"])
def test_midturn_condition_clamps_budget_and_release_keeps_existing_recovery(
    condition: str,
) -> None:
    start = _start()
    live = _get_live(start.handle)
    before_rng = live.rng.getstate()
    event_count = len(live.event_log)
    _emit(live, ConditionApplied(target_id="char:hero", condition=condition))
    hero = _find_combatant(live, "char:hero")
    assert hero is not None
    assert hero.movement_remaining == 0
    _emit(live, ConditionRemoved(target_id="char:hero", condition=condition))
    hero = _find_combatant(live, "char:hero")
    assert hero is not None
    assert _effective_speed(hero, live) == 45
    # Removal restores Speed, but never raises this turn's clamped budget.
    assert hero.movement_remaining == 0
    run_async(
        submit_player_intent(
            start.handle, actor_id="char:hero", intent=PlayerIntent(intent_type="dash")
        )
    )
    run_async(
        submit_player_intent(
            start.handle,
            actor_id="char:hero",
            intent=PlayerIntent(intent_type="move", target_zone_id=cell(1, 0)),
        )
    )
    events = live.event_log[event_count:]
    assert [event.type for event in events] == [
        "condition_applied",
        "condition_removed",
        "dash_taken",
        "actor_moved",
    ]
    assert isinstance(events[2], DashTaken)
    assert events[2].doubled_movement_remaining == 45
    assert isinstance(events[3], ActorMoved)
    assert events[3].distance_ft == 5
    hero = _find_combatant(live, "char:hero")
    assert hero is not None
    assert hero.movement_remaining == 40
    assert live.actor_zone[hero.entity_id] == cell(1, 0)
    assert live.rng.getstate() == before_rng


@pytest.mark.parametrize("conditions", [(), ("stunned",), ("prone",)])
def test_normal_and_neighbour_movement_keep_budget_events_and_rng(
    conditions: tuple[str, ...],
) -> None:
    start = _start(*conditions)
    live = _get_live(start.handle)
    before_rng = live.rng.getstate()
    event_count = len(live.event_log)
    run_async(
        submit_player_intent(
            start.handle,
            actor_id="char:hero",
            intent=PlayerIntent(intent_type="move", target_zone_id=cell(1, 0)),
        )
    )
    events = live.event_log[event_count:]
    assert [event.type for event in events] == ["actor_moved"]
    assert isinstance(events[0], ActorMoved)
    assert events[0].distance_ft == 5
    hero = _find_combatant(live, "char:hero")
    assert hero is not None
    assert hero.movement_remaining == 40
    assert hero.action_available is True
    assert live.rng.getstate() == before_rng


@pytest.mark.parametrize("condition", _ZERO_SPEED_CONDITIONS)
def test_speed_zero_monsters_keep_pass_events_without_movement_dash_or_rng(condition: str) -> None:
    start = _start(condition, monster_first=True)
    live = _get_live(start.handle)
    before_rng = live.rng.getstate()
    event_count = len(live.event_log)
    run_async(advance_monster_turn(start.handle))
    events = live.event_log[event_count:]
    assert [event.type for event in events] == [
        "intent_submitted",
        "turn_phase",
        "turn_ended",
        "turn_started",
        "turn_phase",
    ]
    assert isinstance(events[0], IntentSubmitted)
    assert events[0].intent_type == "pass"
    assert isinstance(events[1], TurnPhase)
    assert (events[1].actor_id, events[1].phase) == ("mon:foe", "turn_end")
    assert isinstance(events[4], TurnPhase)
    assert (events[4].actor_id, events[4].phase) == ("char:hero", "turn_start")
    assert live.actor_zone["mon:foe"] == cell(5, 0)
    foe = _find_combatant(live, "mon:foe")
    assert foe is not None
    assert foe.movement_remaining == 0
    assert live.rng.getstate() == before_rng


@pytest.mark.parametrize("condition", _ZERO_SPEED_CONDITIONS)
def test_forced_push_still_moves_speed_zero_creatures_without_budget_or_rng(condition: str) -> None:
    start = _start(condition, monster_first=True)
    live = _get_live(start.handle)
    before_rng = live.rng.getstate()
    event_count = len(live.event_log)
    before = _find_combatant(live, "mon:foe")
    assert before is not None
    assert before.movement_remaining == 0
    push_combatant(live, "mon:foe", cell(4, 0), 10)
    events = live.event_log[event_count:]
    assert [event.type for event in events] == ["combatant_moved"]
    assert isinstance(events[0], CombatantMoved)
    assert events[0].from_zone == cell(5, 0)
    assert events[0].to_zone == cell(7, 0)
    assert events[0].distance_ft == 10
    assert events[0].forced is True
    assert live.actor_zone["mon:foe"] == cell(7, 0)
    assert _find_combatant(live, "mon:foe") == before
    assert live.rng.getstate() == before_rng


def test_prone_stand_up_keeps_half_speed_cost_and_zero_speed_gate() -> None:
    start = _start()
    live = _get_live(start.handle)
    _emit(live, ConditionApplied(target_id="char:hero", condition="prone"))
    before_rng = live.rng.getstate()
    event_count = len(live.event_log)
    run_async(
        submit_player_intent(
            start.handle, actor_id="char:hero", intent=PlayerIntent(intent_type="stand_up")
        )
    )
    assert [event.type for event in live.event_log[event_count:]] == ["condition_removed"]
    hero = _find_combatant(live, "char:hero")
    assert hero is not None
    assert hero.movement_remaining == 23  # 45 - floor(45 / 2).
    assert hero.action_available is True
    assert not any(condition.condition == "prone" for condition in hero.conditions)
    assert live.rng.getstate() == before_rng

    _emit(live, ConditionApplied(target_id="char:hero", condition="prone"))
    _emit(live, ConditionApplied(target_id="char:hero", condition="grappled"))
    event_count = len(live.event_log)
    with pytest.raises(IntentRejectedError) as exc:
        run_async(
            submit_player_intent(
                start.handle, actor_id="char:hero", intent=PlayerIntent(intent_type="stand_up")
            )
        )
    assert exc.value.reason == "speed_zero"
    assert len(live.event_log) == event_count
    assert live.rng.getstate() == before_rng
