"""Shared condition attack migration parity and Poisoned check parity."""

from __future__ import annotations

import random

import pytest
from dnd5e_srd_data.schema.common import AttackActivity, CheckActivity

from dnd5e_engine import PlayerIntent
from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.context import ActivityResolutionContext
from dnd5e_engine.activities.resolver import resolve_activity
from dnd5e_engine.check import CheckSpec, resolve_check
from dnd5e_engine.events import AttackRolled, CheckRolled, CombatEvent, DamageApplied
from dnd5e_engine.orchestrator import (
    _build_hydration_payload,
    _get_live,
    start_combat,
    submit_player_intent,
)
from dnd5e_engine.rules.conditions import conditions_grant_disadvantage_on_ability_checks
from dnd5e_engine.specs import EncounterMemberSpec, PartyMemberSpec
from dnd5e_engine.types.combat import Combatant
from dnd5e_engine.types.effects import ActiveEffect, ActiveEffectChange
from tests.e2e.harness import cell, grid_scene, run_async


def _effect(*, statuses: tuple[str, ...] = (), flag: str | None = None) -> ActiveEffect:
    return ActiveEffect(
        id="effect:parity",
        name="Parity",
        origin="test",
        target_id="char:hero",
        statuses=set(statuses),
        changes=[] if flag is None else [ActiveEffectChange(key=flag, mode="override", value=True)],
    )


def _mirror_roll(rng: random.Random, mode: str) -> int:
    first = rng.randint(1, 20)
    if mode == "normal":
        return first
    second = rng.randint(1, 20)
    return min(first, second) if mode == "disadvantage" else max(first, second)


@pytest.mark.parametrize("seed", [1, 7, 9])
@pytest.mark.parametrize(
    ("conditions", "flag", "mode", "sources"),
    [
        ([], None, "normal", []),
        (["poisoned"], None, "disadvantage", ["condition:attacker"]),
        (["poisoned"], "flags.advantage.attack", "normal", ["flag", "condition:attacker"]),
        (["poisoned"], "flags.disadvantage.attack", "disadvantage", ["flag", "condition:attacker"]),
        (["poisoned", "restrained"], None, "disadvantage", ["condition:attacker"]),
        (["POISONED", "poisoned"], None, "disadvantage", ["condition:attacker"]),
        (["restrained"], None, "disadvantage", ["condition:attacker"]),
        (["restrained"], "flags.advantage.attack", "normal", ["flag", "condition:attacker"]),
        (
            ["restrained"],
            "flags.disadvantage.attack",
            "disadvantage",
            ["flag", "condition:attacker"],
        ),
        (["restrained", "frightened"], None, "disadvantage", ["condition:attacker"]),
        (["RESTRAINED", "restrained"], None, "disadvantage", ["condition:attacker"]),
        (["blinded"], None, "disadvantage", ["condition:attacker"]),
        (["blinded"], "flags.advantage.attack", "normal", ["flag", "condition:attacker"]),
        (["blinded"], "flags.disadvantage.attack", "disadvantage", ["flag", "condition:attacker"]),
        (["blinded", "frightened"], None, "disadvantage", ["condition:attacker"]),
        (["blinded", "poisoned"], None, "disadvantage", ["condition:attacker"]),
        (["blinded", "restrained"], None, "disadvantage", ["condition:attacker"]),
        (["BLINDED", "blinded"], None, "disadvantage", ["condition:attacker"]),
        (["prone"], None, "disadvantage", ["condition:attacker"]),
        (["prone"], "flags.advantage.attack", "normal", ["flag", "condition:attacker"]),
        (["prone"], "flags.disadvantage.attack", "disadvantage", ["flag", "condition:attacker"]),
        (["prone", "frightened"], None, "disadvantage", ["condition:attacker"]),
        (["prone", "poisoned"], None, "disadvantage", ["condition:attacker"]),
        (["PRONE", "prone"], None, "disadvantage", ["condition:attacker"]),
        (["unconscious"], None, "disadvantage", ["condition:attacker"]),
        (
            ["poisoned", "restrained", "blinded", "prone"],
            None,
            "disadvantage",
            ["condition:attacker"],
        ),
        (
            ["poisoned", "restrained", "blinded", "prone"],
            "flags.advantage.attack",
            "normal",
            ["flag", "condition:attacker"],
        ),
    ],
)
def test_attack_roll_and_rng_parity(
    seed: int, conditions: list[str], flag: str | None, mode: str, sources: list[str]
) -> None:
    hero = Combatant(
        entity_id="char:hero",
        entity_type="Character",
        name="Hero",
        initiative=10,
        hp_current=20,
        hp_max=20,
    )
    foe = hero.model_copy(update={"entity_id": "mon:foe", "ac": 100})
    rng = random.Random(seed)
    mirror = random.Random(seed)
    events: list[CombatEvent] = []
    ctx = ActivityResolutionContext(
        rng=rng,
        caster=hero,
        targets=[foe],
        event_emitter=events.append,
        caster_abilities={"str": 10},
        caster_proficiency_bonus=0,
        attacker_conditions=conditions,
        active_effects=() if flag is None else (_effect(flag=flag),),
    )

    resolve_activity(AttackActivity(kind="attack", attack={"ability": "str"}), ctx)

    assert len(events) == 1
    rolled = events[0]
    assert isinstance(rolled, AttackRolled)
    assert rolled.natural == _mirror_roll(mirror, mode)
    assert rolled.roll_total == rolled.natural
    assert rolled.advantage == mode
    assert rolled.sources == sources
    assert rolled.is_hit is False
    assert rolled.is_crit is False
    assert rng.getstate() == mirror.getstate()


@pytest.mark.parametrize("seed", [1, 7])
@pytest.mark.parametrize(
    ("condition", "distance_ft", "mode"),
    [
        ("restrained", None, "advantage"),
        ("blinded", None, "advantage"),
        ("prone", 0, "advantage"),
        ("prone", 5, "advantage"),
        ("prone", 6, "disadvantage"),
        ("prone", 30, "disadvantage"),
        ("prone", None, "normal"),
    ],
)
def test_legacy_target_conditions_preserve_sources_and_rng(
    seed: int, condition: str, distance_ft: int | None, mode: str
) -> None:
    hero = Combatant(
        entity_id="char:hero",
        entity_type="Character",
        name="Hero",
        initiative=10,
        hp_current=20,
        hp_max=20,
    )
    foe = hero.model_copy(update={"entity_id": "mon:foe", "ac": 100})
    rng = random.Random(seed)
    mirror = random.Random(seed)
    events: list[CombatEvent] = []
    ctx = ActivityResolutionContext(
        rng=rng,
        caster=hero,
        targets=[foe],
        event_emitter=events.append,
        caster_abilities={"str": 10},
        caster_proficiency_bonus=0,
        target_conditions={foe.entity_id: [condition]},
        target_distance_ft={} if distance_ft is None else {foe.entity_id: distance_ft},
    )

    resolve_activity(AttackActivity(kind="attack", attack={"ability": "str"}), ctx)

    assert len(events) == 1
    rolled = events[0]
    assert isinstance(rolled, AttackRolled)
    assert rolled.natural == _mirror_roll(mirror, mode)
    assert rolled.roll_total == rolled.natural
    assert rolled.advantage == mode
    assert rolled.sources == ([] if mode == "normal" else ["condition:target"])
    assert rng.getstate() == mirror.getstate()


@pytest.mark.parametrize("seed", [1, 7, 9])
@pytest.mark.parametrize(
    ("conditions", "mode"),
    [
        ((), "normal"),
        (("poisoned",), "disadvantage"),
        (("poisoned", "frightened"), "disadvantage"),
        (("restrained",), "normal"),
        (("blinded",), "normal"),
        (("prone",), "normal"),
        (("poisoned", "restrained", "blinded", "prone"), "disadvantage"),
    ],
)
@pytest.mark.parametrize("skill", [False, True])
def test_check_projection_roll_and_rng_parity(
    seed: int, conditions: tuple[str, ...], mode: str, skill: bool
) -> None:
    start = run_async(
        start_combat(
            session_id="poisoned-parity",
            party=[
                PartyMemberSpec(
                    entity_id="char:hero",
                    name="Hero",
                    initiative=10,
                    hp_current=20,
                    hp_max=20,
                    zone_id=cell(0, 0),
                    wisdom=14,
                    skill_proficiencies=("perception",),
                )
            ],
            encounter=[
                EncounterMemberSpec(
                    entity_id="mon:foe",
                    entity_type="Monster",
                    name="Foe",
                    initiative=1,
                    hp_current=20,
                    hp_max=20,
                    zone_id=cell(1, 0),
                )
            ],
            grid_scene=grid_scene(),
            rng_seed=seed,
            active_effects=() if not conditions else (_effect(statuses=conditions),),
        )
    )
    live = _get_live(start.handle)
    state_before = live.rng.getstate()
    events_before = list(live.event_log)
    payload = _build_hydration_payload(live, caster=None)
    assert live.rng.getstate() == state_before
    assert live.event_log == events_before
    hero = next(c for c in live.initiative if c.entity_id == "char:hero")
    rng = random.Random(seed)
    mirror = random.Random(seed)
    events: list[CombatEvent] = []
    ctx = build_activity_context(
        hero,
        [],
        rng=rng,
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
    )
    activity = CheckActivity(
        kind="check",
        check={
            "ability": "wis",
            "associated": ["prc"] if skill else [],
            "dc": {"calculation": "flat", "formula": "12"},
        },
    )

    resolve_activity(activity, ctx)

    assert len(events) == 1
    rolled = events[0]
    assert isinstance(rolled, CheckRolled)
    assert rolled.natural == _mirror_roll(mirror, mode)
    assert rolled.modifier == (4 if skill else 2)
    assert rolled.roll_total == rolled.natural + rolled.modifier
    assert rolled.succeeded == (rolled.roll_total >= 12)
    assert rolled.advantage == mode
    assert rolled.sources == (["condition:attacker"] if mode == "disadvantage" else [])
    assert rng.getstate() == mirror.getstate()


@pytest.mark.parametrize("kind", ["ability", "skill"])
def test_existing_standalone_check_advantage_cancels_poisoned(kind: str) -> None:
    rng = random.Random(7)
    mirror = random.Random(7)
    spec = CheckSpec(
        kind=kind,
        ability="wisdom",
        skill="perception",
        ability_scores={"wisdom": 10},
        proficient_skills=(),
        proficient_saves=(),
        proficiency_bonus=0,
        advantage=True,
        disadvantage=conditions_grant_disadvantage_on_ability_checks(["poisoned"]),
        rng=rng,
    )

    rolled = resolve_check(spec)

    assert rolled.natural_roll == mirror.randint(1, 20)
    assert rng.getstate() == mirror.getstate()


@pytest.mark.parametrize(
    ("statuses", "flag", "mode", "sources"),
    [
        ((), None, "normal", []),
        (("poisoned",), None, "disadvantage", ["condition:attacker"]),
        (("poisoned",), "flags.advantage.attack", "normal", ["flag", "condition:attacker"]),
        (("poisoned", "restrained"), None, "disadvantage", ["condition:attacker"]),
        (("restrained",), None, "disadvantage", ["condition:attacker"]),
        (("restrained",), "flags.advantage.attack", "normal", ["flag", "condition:attacker"]),
        (
            ("restrained",),
            "flags.disadvantage.attack",
            "disadvantage",
            ["flag", "condition:attacker"],
        ),
        (("blinded",), None, "disadvantage", ["condition:attacker"]),
        (("blinded",), "flags.advantage.attack", "normal", ["flag", "condition:attacker"]),
        (("blinded",), "flags.disadvantage.attack", "disadvantage", ["flag", "condition:attacker"]),
        (("blinded", "frightened"), None, "disadvantage", ["condition:attacker"]),
        (("blinded", "poisoned"), None, "disadvantage", ["condition:attacker"]),
        (("blinded", "restrained"), None, "disadvantage", ["condition:attacker"]),
        (("BLINDED", "blinded"), None, "disadvantage", ["condition:attacker"]),
        (("prone",), None, "disadvantage", ["condition:attacker"]),
        (("prone",), "flags.advantage.attack", "normal", ["flag", "condition:attacker"]),
        (("prone",), "flags.disadvantage.attack", "disadvantage", ["flag", "condition:attacker"]),
        (("prone", "frightened"), None, "disadvantage", ["condition:attacker"]),
        (("prone", "poisoned"), None, "disadvantage", ["condition:attacker"]),
        (("PRONE", "prone"), None, "disadvantage", ["condition:attacker"]),
        (
            ("poisoned", "restrained", "blinded", "prone"),
            None,
            "disadvantage",
            ["condition:attacker"],
        ),
        (
            ("poisoned", "restrained", "blinded", "prone"),
            "flags.advantage.attack",
            "normal",
            ["flag", "condition:attacker"],
        ),
    ],
)
def test_player_intent_preserves_d20_bonus_damage_draw_order(
    statuses: tuple[str, ...],
    flag: str | None,
    mode: str,
    sources: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    effect = _effect(statuses=statuses, flag=flag)
    effect.changes.append(ActiveEffectChange(key="attack.roll.bonus", mode="add", value="1d4"))
    start = run_async(
        start_combat(
            session_id="poisoned-intent-parity",
            party=[
                PartyMemberSpec(
                    entity_id="char:hero",
                    name="Hero",
                    initiative=10,
                    hp_current=20,
                    hp_max=20,
                    zone_id=cell(0, 0),
                )
            ],
            encounter=[
                EncounterMemberSpec(
                    entity_id="mon:foe",
                    entity_type="Monster",
                    name="Foe",
                    initiative=1,
                    hp_current=500,
                    hp_max=500,
                    ac=1,
                    zone_id=cell(1, 0),
                )
            ],
            grid_scene=grid_scene(),
            rng_seed=7,
            active_effects=(effect,),
        )
    )
    live = _get_live(start.handle)
    mirror = random.Random()
    mirror.setstate(live.rng.getstate())
    event_count = len(live.event_log)
    draws: list[tuple[int, int]] = []
    randint = live.rng.randint

    def record_draw(a: int, b: int) -> int:
        draws.append((a, b))
        return randint(a, b)

    monkeypatch.setattr(live.rng, "randint", record_draw)

    run_async(
        submit_player_intent(
            start.handle,
            actor_id="char:hero",
            intent=PlayerIntent(intent_type="attack", weapon_id="dagger", target_id="mon:foe"),
        )
    )

    events = live.event_log[event_count:]
    assert [e.type for e in events] == ["intent_submitted", "attack_rolled", "damage_applied"]
    _, attack, damage = events
    assert isinstance(attack, AttackRolled)
    assert isinstance(damage, DamageApplied)
    assert attack.natural == _mirror_roll(mirror, mode)
    assert attack.roll_total == attack.natural + attack.modifier + mirror.randint(1, 4)
    assert attack.advantage == mode
    assert attack.sources == sources
    assert attack.attacker_id == "char:hero"
    assert attack.target_id == "mon:foe"
    assert attack.is_hit is True
    assert attack.is_crit is False
    assert attack.is_opportunity_attack is False
    assert attack.advantage_sources == (["flag"] if flag == "flags.advantage.attack" else [])
    assert attack.disadvantage_sources == (
        (["flag"] if flag == "flags.disadvantage.attack" else [])
        + (["condition:attacker"] if statuses else [])
    )
    assert damage.amount == mirror.randint(1, 4)
    assert damage.target_id == "mon:foe"
    assert damage.damage_type == "piercing"
    assert damage.source_id == "dagger"
    assert damage.is_crit is False
    assert damage.is_overkill is False
    assert draws == [(1, 20)] * (1 if mode == "normal" else 2) + [(1, 4), (1, 4)]
    assert live.rng.getstate() == mirror.getstate()
