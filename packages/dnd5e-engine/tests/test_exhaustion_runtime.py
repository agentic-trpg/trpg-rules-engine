"""Projected Exhaustion magnitudes preserve live D20 and movement consumers."""

from __future__ import annotations

import random
from dataclasses import replace

import pytest
from dnd5e_srd_data.schema.common import AttackActivity, CheckActivity, SaveActivity
from dnd5e_srd_data.schema.condition import ConditionEffectKind

from dnd5e_engine import PlayerIntent
from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.context import ActivityResolutionContext
from dnd5e_engine.activities.resolver import resolve_activity
from dnd5e_engine.events import (
    ActorMoved,
    AttackRolled,
    CheckRolled,
    CombatEvent,
    DashTaken,
    Death,
    MoveFailed,
    SaveRolled,
)
from dnd5e_engine.orchestrator import (
    _build_hydration_payload,
    _effective_speed,
    _find_combatant,
    _get_live,
    advance_monster_turn,
    start_combat,
    submit_player_intent,
)
from dnd5e_engine.results import StartCombatResult
from dnd5e_engine.rules import conditions as rules
from dnd5e_engine.specs import EncounterMemberSpec, PartyMemberSpec
from dnd5e_engine.types.conditions import ActiveCondition
from dnd5e_engine.types.effects import ActiveEffect, ActiveEffectChange
from tests.e2e.harness import cell, grid_scene, run_async


def _start(
    level: int,
    *,
    monster: bool = False,
    monster_column: int = 10,
    fleeing: bool = False,
    adjacent: bool = False,
    extra_conditions: tuple[str, ...] = (),
) -> StartCombatResult:
    start = run_async(
        start_combat(
            session_id="exhaustion-runtime",
            party=[
                PartyMemberSpec(
                    entity_id="char:hero",
                    name="Hero",
                    initiative=20 if monster else 1,
                    hp_current=50,
                    hp_max=50,
                    ac=100,
                    strength=16,
                    base_speed=30,
                    class_slug="fighter",
                    character_level=5,
                    skill_proficiencies=("athletics",),
                    zone_id=cell(0, 0),
                )
            ],
            encounter=[
                EncounterMemberSpec(
                    entity_id="mon:foe",
                    entity_type="Monster",
                    name="Foe",
                    initiative=1 if monster else 20,
                    hp_current=1 if fleeing else 100,
                    hp_max=100,
                    ac=100,
                    base_speed=30,
                    monster_template_slug="goblin-warrior" if monster else None,
                    zone_id=cell(1, 0) if adjacent else cell(monster_column, 0 if monster else 10),
                )
            ],
            grid_scene=grid_scene(width=14, height=14),
            rng_seed=7,
        )
    )
    live = _get_live(start.handle)
    actor_id = "mon:foe" if monster else "char:hero"
    actor = _find_combatant(live, actor_id)
    assert actor is not None
    if level:
        actor.conditions.append(
            ActiveCondition(
                condition="exhaustion",
                exhaustion_level=level,
                source_entity_id="implied:effect",
                scope="combat",
            )
        )
    actor.conditions.extend(
        ActiveCondition(condition=name, source_entity_id="implied:effect", scope="combat")
        for name in extra_conditions
    )
    # Establish the runtime state before this actor's first TurnStarted. The
    # existing turn-start handler must supply its authoritative movement budget.
    run_async(
        submit_player_intent(
            start.handle,
            actor_id="char:hero" if monster else "mon:foe",
            intent=PlayerIntent(intent_type="pass"),
        )
    )
    assert live.current_actor_id == actor_id
    return start


def _context(
    level: int, *, self_target: bool = False, auto_fail: bool = False
) -> tuple[ActivityResolutionContext, list[CombatEvent]]:
    start = _start(level, extra_conditions=("paralyzed",) if auto_fail else ())
    live = _get_live(start.handle)
    hero = _find_combatant(live, "char:hero")
    foe = _find_combatant(live, "mon:foe")
    assert hero is not None
    assert foe is not None
    payload = _build_hydration_payload(live, caster=hero)
    assert payload["d20_test_penalty"].get(hero.entity_id, 0) == rules.d20_test_penalty(
        hero.conditions
    )
    events: list[CombatEvent] = []
    ctx = build_activity_context(
        hero,
        [hero if self_target else foe],
        rng=live.rng,
        event_emitter=events.append,
        slot_level=None,
        base_spell_level=None,
        spellcasting_ability=None,
        concentration=False,
        source_passive_effects=[],
        spell_book={},
        passive_damage_modifiers=payload["passive_damage_modifiers"],
        save_modifiers=payload["save_modifiers"],
        check_modifiers=payload["check_modifiers"],
        d20_test_penalty=payload["d20_test_penalty"],
    )
    return ctx, events


def _mutate_multiplier(
    kind: ConditionEffectKind, value: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        rules._DECLARATIVE_CONDITION_EFFECTS,
        "exhaustion",
        tuple(
            clause.model_copy(update={"value": value}) if clause.kind == kind else clause
            for clause in rules._DECLARATIVE_CONDITION_EFFECTS["exhaustion"]
        ),
    )


def _mirror(mode: str) -> random.Random:
    rng = random.Random(7)
    rng.randint(1, 20)
    if mode != "normal":
        rng.randint(1, 20)
    return rng


@pytest.mark.parametrize("level", [0, 1, 3])
@pytest.mark.parametrize("multiplier", [2, 3])
@pytest.mark.parametrize(
    "flags", [(), ("advantage",), ("disadvantage",), ("advantage", "disadvantage")]
)
def test_attack_penalty_changes_only_modifier_and_total_with_exact_d20_parity(
    level: int, multiplier: int, flags: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _mutate_multiplier(ConditionEffectKind.D20_TEST_PENALTY_PER_LEVEL, multiplier, monkeypatch)
    resolved: list[AttackRolled] = []
    for runtime_level in (0, level):
        ctx, events = _context(runtime_level)
        ctx = replace(
            ctx,
            active_effects=(
                ActiveEffect(
                    id="effect:mode",
                    name="Mode",
                    origin="test",
                    target_id=ctx.caster.entity_id,
                    changes=[
                        ActiveEffectChange(key=f"flags.{flag}.attack", mode="override", value=True)
                        for flag in flags
                    ],
                ),
            ),
        )
        resolve_activity(AttackActivity(attack={"ability": "str"}), ctx)
        assert len(events) == 1
        assert isinstance(events[0], AttackRolled)
        rolled = events[0]
        assert not rolled.is_hit
        assert ctx.rng.getstate() == _mirror(rolled.advantage).getstate()
        resolved.append(rolled)
    plain, tired = resolved
    assert tired.modifier == plain.modifier - multiplier * level
    assert tired.roll_total == plain.roll_total - multiplier * level
    assert tired.natural == plain.natural
    assert tired.advantage == plain.advantage
    assert tired.sources == plain.sources
    assert tired.advantage_sources == plain.advantage_sources
    assert tired.disadvantage_sources == plain.disadvantage_sources


@pytest.mark.parametrize("level", [1, 3])
@pytest.mark.parametrize("multiplier", [2, 3])
def test_player_intent_attack_uses_canonical_penalty_with_identical_events_and_rng(
    level: int, multiplier: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mutate_multiplier(ConditionEffectKind.D20_TEST_PENALTY_PER_LEVEL, multiplier, monkeypatch)
    rolls: list[AttackRolled] = []
    for runtime_level in (0, level):
        start = _start(runtime_level, adjacent=True)
        live = _get_live(start.handle)
        offset = len(live.event_log)
        run_async(
            submit_player_intent(
                start.handle,
                actor_id="char:hero",
                intent=PlayerIntent(intent_type="attack", weapon_id="dagger", target_id="mon:foe"),
            )
        )
        events = live.event_log[offset:]
        assert [event.type for event in events] == ["intent_submitted", "attack_rolled"]
        assert isinstance(events[1], AttackRolled)
        rolls.append(events[1])
        assert live.rng.getstate() == _mirror("normal").getstate()
    plain, tired = rolls
    assert tired == plain.model_copy(
        update={
            "modifier": plain.modifier - multiplier * level,
            "roll_total": plain.roll_total - multiplier * level,
        }
    )


def test_level_six_keeps_existing_numeric_rules_without_adding_death_behavior() -> None:
    start = _start(6)
    live = _get_live(start.handle)
    hero = _find_combatant(live, "char:hero")
    assert hero is not None
    assert hero.is_alive
    assert hero.hp_current == 50
    assert hero.movement_remaining == 0
    assert rules.d20_test_penalty(hero.conditions) == -12
    assert not any(isinstance(event, Death) for event in live.event_log)


@pytest.mark.parametrize("level", [0, 1, 3])
@pytest.mark.parametrize("multiplier", [2, 3])
@pytest.mark.parametrize(
    "flags", [(), ("advantage",), ("disadvantage",), ("advantage", "disadvantage")]
)
def test_save_penalty_flows_through_hydration_and_shared_primitive_without_extra_draws(
    level: int, multiplier: int, flags: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _mutate_multiplier(ConditionEffectKind.D20_TEST_PENALTY_PER_LEVEL, multiplier, monkeypatch)
    resolved: list[SaveRolled] = []
    for runtime_level in (0, level):
        ctx, events = _context(runtime_level, self_target=True)
        if "advantage" in flags:
            ctx.passive_save_adv[ctx.caster.entity_id] = ["WIS"]
        if "disadvantage" in flags:
            ctx.passive_save_dis[ctx.caster.entity_id] = ["WIS"]
        resolve_activity(
            SaveActivity(save={"ability": ["wis"], "dc": {"calculation": "flat", "formula": "10"}}),
            ctx,
        )
        assert len(events) == 1
        assert isinstance(events[0], SaveRolled)
        rolled = events[0]
        assert ctx.rng.getstate() == _mirror(rolled.advantage).getstate()
        assert rolled.succeeded is (rolled.roll_total >= rolled.dc)
        resolved.append(rolled)
    plain, tired = resolved
    assert tired.modifier == plain.modifier - multiplier * level
    assert tired.roll_total == plain.roll_total - multiplier * level
    assert tired.natural == plain.natural
    assert tired.advantage == plain.advantage
    assert tired.sources == plain.sources


@pytest.mark.parametrize("level", [0, 1, 3])
def test_auto_fail_save_with_exhaustion_still_draws_no_d20_or_bonus_dice(level: int) -> None:
    ctx, events = _context(level, self_target=True, auto_fail=True)
    before = ctx.rng.getstate()
    ctx.passive_save_bonus[ctx.caster.entity_id] = "1d4"
    resolve_activity(
        SaveActivity(save={"ability": ["str"], "dc": {"calculation": "flat", "formula": "10"}}), ctx
    )
    assert len(events) == 1
    rolled = events[0]
    assert isinstance(rolled, SaveRolled)
    assert (rolled.roll_total, rolled.natural, rolled.modifier) == (0, None, 0)
    assert not rolled.succeeded
    assert rolled.sources == []
    assert ctx.rng.getstate() == before


@pytest.mark.parametrize("level", [0, 1, 3])
@pytest.mark.parametrize("multiplier", [2, 3])
@pytest.mark.parametrize("skill", [False, True])
def test_ability_and_skill_checks_receive_flat_penalty_without_2014_disadvantage(
    level: int, multiplier: int, skill: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mutate_multiplier(ConditionEffectKind.D20_TEST_PENALTY_PER_LEVEL, multiplier, monkeypatch)
    resolved: list[CheckRolled] = []
    for runtime_level in (0, level):
        ctx, events = _context(runtime_level, self_target=True)
        resolve_activity(
            CheckActivity(check={"ability": "str", "associated": ["ath"] if skill else []}), ctx
        )
        assert len(events) == 1
        assert isinstance(events[0], CheckRolled)
        rolled = events[0]
        assert rolled.advantage == "normal"
        assert rolled.sources == []
        assert ctx.rng.getstate() == _mirror("normal").getstate()
        resolved.append(rolled)
    plain, tired = resolved
    assert tired.modifier == plain.modifier - multiplier * level
    assert tired.roll_total == plain.roll_total - multiplier * level
    assert tired.natural == plain.natural
    assert tired.skill == ("ath" if skill else None)


def test_player_movement_budget_accepts_twenty_feet_and_rejects_more_atomically() -> None:
    start = _start(2)
    live = _get_live(start.handle)
    hero = _find_combatant(live, "char:hero")
    assert hero is not None
    assert hero.movement_remaining == 20
    before_rng = live.rng.getstate()
    offset = len(live.event_log)
    run_async(
        submit_player_intent(
            start.handle,
            actor_id=hero.entity_id,
            intent=PlayerIntent(intent_type="move", target_zone_id=cell(5, 0)),
        )
    )
    failed = live.event_log[offset:]
    assert len(failed) == 1
    assert isinstance(failed[0], MoveFailed)
    assert failed[0].reason == "insufficient_movement"
    assert _find_combatant(live, hero.entity_id) == hero
    assert live.actor_zone[hero.entity_id] == cell(0, 0)
    offset = len(live.event_log)
    run_async(
        submit_player_intent(
            start.handle,
            actor_id=hero.entity_id,
            intent=PlayerIntent(intent_type="move", target_zone_id=cell(4, 0)),
        )
    )
    moved = live.event_log[offset:]
    assert len(moved) == 1
    assert isinstance(moved[0], ActorMoved)
    assert moved[0].distance_ft == 20
    after = _find_combatant(live, hero.entity_id)
    assert after is not None
    assert after.movement_remaining == 0
    assert live.actor_zone[hero.entity_id] == cell(4, 0)
    assert live.rng.getstate() == before_rng


@pytest.mark.parametrize("condition", [None, "grappled", "restrained"])
def test_dash_adds_projected_speed_and_cannot_restore_zero_speed(condition: str | None) -> None:
    start = _start(2, extra_conditions=(condition,) if condition else ())
    live = _get_live(start.handle)
    before_rng = live.rng.getstate()
    offset = len(live.event_log)
    run_async(
        submit_player_intent(
            start.handle, actor_id="char:hero", intent=PlayerIntent(intent_type="dash")
        )
    )
    events = live.event_log[offset:]
    assert len(events) == 1
    assert isinstance(events[0], DashTaken)
    assert events[0].doubled_movement_remaining == (0 if condition else 40)
    hero = _find_combatant(live, "char:hero")
    assert hero is not None
    assert hero.movement_remaining == (0 if condition else 40)
    assert not hero.action_available
    assert live.rng.getstate() == before_rng


@pytest.mark.parametrize(("multiplier", "speed", "distance"), [(5, 20, 20), (7, 16, 15)])
def test_monster_flee_uses_projected_speed_without_extra_rng(
    multiplier: int, speed: int, distance: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mutate_multiplier(ConditionEffectKind.SPEED_PENALTY_PER_LEVEL, multiplier, monkeypatch)
    start = _start(2, monster=True, monster_column=3, fleeing=True)
    live = _get_live(start.handle)
    foe = _find_combatant(live, "mon:foe")
    assert foe is not None
    assert foe.movement_remaining == speed
    before_rng = live.rng.getstate()
    offset = len(live.event_log)
    run_async(advance_monster_turn(start.handle))
    moves = [event for event in live.event_log[offset:] if isinstance(event, ActorMoved)]
    assert sum(event.distance_ft for event in moves) == distance
    after = _find_combatant(live, "mon:foe")
    assert after is not None
    assert after.has_fled
    assert after.movement_remaining == speed - distance
    assert live.rng.getstate() == before_rng


@pytest.mark.parametrize(
    ("multiplier", "speed", "distance", "dash_budget"), [(5, 20, 35, 40), (7, 16, 15, None)]
)
def test_monster_dash_and_partial_approach_keep_existing_policy_with_canonical_speed(
    multiplier: int,
    speed: int,
    distance: int,
    dash_budget: int | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mutate_multiplier(ConditionEffectKind.SPEED_PENALTY_PER_LEVEL, multiplier, monkeypatch)
    start = _start(2, monster=True, monster_column=8)
    live = _get_live(start.handle)
    before_rng = live.rng.getstate()
    offset = len(live.event_log)
    run_async(advance_monster_turn(start.handle))
    events = live.event_log[offset:]
    moves = [event for event in events if isinstance(event, ActorMoved)]
    dashes = [event for event in events if isinstance(event, DashTaken)]
    assert sum(event.distance_ft for event in moves) == distance
    assert [event.doubled_movement_remaining for event in dashes] == (
        [] if dash_budget is None else [dash_budget]
    )
    foe = _find_combatant(live, "mon:foe")
    assert foe is not None
    assert _effective_speed(foe, live) == speed
    assert foe.movement_remaining == (dash_budget if dash_budget is not None else speed) - distance
    assert live.rng.getstate() == before_rng
