"""Target condition migration preserves live attack events and exact RNG state."""

from __future__ import annotations

import random

import pytest
from dnd5e_srd_data.schema.common import AttackActivity
from dnd5e_srd_data.schema.condition import ConditionEffect, ConditionEffectKind

from dnd5e_engine import PlayerIntent
from dnd5e_engine.activities.context import ActivityResolutionContext
from dnd5e_engine.activities.resolver import resolve_activity
from dnd5e_engine.events import AttackRolled, CombatEvent, DamageApplied
from dnd5e_engine.orchestrator import _get_live, start_combat, submit_player_intent
from dnd5e_engine.rules import conditions as condition_rules
from dnd5e_engine.rules.effects import project_condition_effects
from dnd5e_engine.specs import EncounterMemberSpec, PartyMemberSpec
from dnd5e_engine.types.combat import Combatant
from dnd5e_engine.types.effects import ActiveEffect, ActiveEffectChange
from tests.e2e.harness import cell, grid_scene, run_async

_TARGET_CONDITIONS = ("blinded", "restrained", "paralyzed", "stunned", "petrified", "unconscious")


def _swing(
    target_conditions: list[str],
    *,
    attacker_conditions: list[str] | None = None,
    seed: int = 7,
    distance_ft: int | None = None,
    target_invisibility_pierced: bool = False,
) -> tuple[AttackRolled, random.Random]:
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
    events: list[CombatEvent] = []
    ctx = ActivityResolutionContext(
        rng=rng,
        caster=hero,
        targets=[foe],
        event_emitter=events.append,
        caster_abilities={"str": 16},
        caster_proficiency_bonus=0,
        attacker_conditions=attacker_conditions or [],
        target_conditions={foe.entity_id: target_conditions},
        target_distance_ft={} if distance_ft is None else {foe.entity_id: distance_ft},
        target_invisibility_pierced={foe.entity_id: target_invisibility_pierced},
    )
    # No damage or effect riders: all RNG consumption belongs to the d20 roll.
    resolve_activity(AttackActivity(kind="attack", attack={"ability": "str"}), ctx)
    assert len(events) == 1
    assert isinstance(events[0], AttackRolled)
    return events[0], rng


def _mirror_roll(rng: random.Random, mode: str) -> int:
    first = rng.randint(1, 20)
    if mode == "normal":
        return first
    second = rng.randint(1, 20)
    return max(first, second) if mode == "advantage" else min(first, second)


def _assert_roll(
    rolled: AttackRolled,
    rng: random.Random,
    *,
    seed: int = 7,
    mode: str,
    advantage_sources: list[str],
    disadvantage_sources: list[str],
) -> None:
    mirror = random.Random(seed)
    assert rolled.natural == _mirror_roll(mirror, mode)
    assert rolled.modifier == 3
    assert rolled.roll_total == rolled.natural + 3
    assert rolled.advantage == mode
    assert rolled.sources == advantage_sources + disadvantage_sources
    assert rolled.advantage_sources == advantage_sources
    assert rolled.disadvantage_sources == disadvantage_sources
    assert rolled.attacker_id == "char:hero"
    assert rolled.target_id == "mon:foe"
    assert rolled.is_hit is (rolled.natural == 20)
    assert rolled.is_crit is (rolled.natural == 20)
    assert rng.getstate() == mirror.getstate()


@pytest.mark.parametrize("condition", _TARGET_CONDITIONS)
@pytest.mark.parametrize("seed", [1, 7, 9])
@pytest.mark.parametrize("poisoned_attacker", [False, True])
def test_target_advantage_and_attacker_disadvantage_preserve_roll_and_rng(
    condition: str, seed: int, poisoned_attacker: bool
) -> None:
    rolled, rng = _swing(
        [condition], attacker_conditions=["poisoned"] if poisoned_attacker else [], seed=seed
    )
    _assert_roll(
        rolled,
        rng,
        seed=seed,
        mode="normal" if poisoned_attacker else "advantage",
        advantage_sources=["condition:target"],
        disadvantage_sources=["condition:attacker"] if poisoned_attacker else [],
    )


@pytest.mark.parametrize(
    "conditions",
    [
        ["blinded", "restrained"],
        ["paralyzed", "stunned"],
        list(_TARGET_CONDITIONS),
        *[[condition.upper(), condition.title(), condition] for condition in _TARGET_CONDITIONS],
    ],
)
def test_multiple_and_case_duplicate_conditions_do_not_stack_dice(conditions: list[str]) -> None:
    rolled, rng = _swing(conditions)
    _assert_roll(
        rolled,
        rng,
        mode="advantage",
        advantage_sources=["condition:target"],
        disadvantage_sources=[],
    )


@pytest.mark.parametrize(
    ("condition", "distance_ft", "pierced", "mode", "has_advantage", "has_disadvantage"),
    [
        ("prone", None, False, "normal", False, False),
        ("prone", 0, False, "advantage", True, False),
        ("prone", 5, False, "advantage", True, False),
        ("prone", 6, False, "disadvantage", False, True),
        ("prone", 30, False, "disadvantage", False, True),
        ("invisible", None, False, "disadvantage", False, True),
        ("invisible", None, True, "normal", False, False),
        ("unconscious", None, False, "advantage", True, False),
        ("unconscious", 0, False, "advantage", True, False),
        ("unconscious", 5, False, "advantage", True, False),
        ("unconscious", 6, False, "normal", True, True),
        ("unconscious", 30, False, "normal", True, True),
    ],
)
def test_qualified_targets_and_implied_prone_preserve_sources_and_rng(
    condition: str,
    distance_ft: int | None,
    pierced: bool,
    mode: str,
    has_advantage: bool,
    has_disadvantage: bool,
) -> None:
    rolled, rng = _swing([condition], distance_ft=distance_ft, target_invisibility_pierced=pierced)
    _assert_roll(
        rolled,
        rng,
        mode=mode,
        advantage_sources=["condition:target"] if has_advantage else [],
        disadvantage_sources=["condition:target"] if has_disadvantage else [],
    )


def test_unconscious_attacker_keeps_implied_prone_own_attack_disadvantage() -> None:
    rolled, rng = _swing([], attacker_conditions=["UNCONSCIOUS", "prone", "PRONE"])
    _assert_roll(
        rolled,
        rng,
        mode="disadvantage",
        advantage_sources=[],
        disadvantage_sources=["condition:attacker"],
    )


@pytest.mark.parametrize("condition", _TARGET_CONDITIONS)
@pytest.mark.parametrize("remove_from", ["allowlist", "canonical"])
def test_target_advantage_requires_both_canonical_clause_and_explicit_opt_in(
    condition: str, remove_from: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    kind = ConditionEffectKind.ADVANTAGE_ATTACKS_AGAINST
    canonical = condition_rules._DECLARATIVE_CONDITION_EFFECTS[condition]
    clauses = [effect for effect in canonical if effect.kind == kind]
    assert len(clauses) == 1
    assert clauses[0].qualifier == ""
    assert clauses[0].value is None
    own_attack_before = condition_rules.conditions_grant_advantage_on_attack([condition], [])
    saves_before = condition_rules.project_passive_save_modifiers([condition])
    changes_before = condition_rules._project_condition_changes([condition])
    if remove_from == "allowlist":
        monkeypatch.setitem(
            condition_rules._DECLARATIVE_CONDITION_MIGRATIONS,
            condition,
            condition_rules._DECLARATIVE_CONDITION_MIGRATIONS[condition] - {kind},
        )
    else:
        monkeypatch.setitem(
            condition_rules._DECLARATIVE_CONDITION_EFFECTS,
            condition,
            tuple(effect for effect in canonical if effect.kind != kind),
        )

    rolled, rng = _swing([condition])
    _assert_roll(rolled, rng, mode="normal", advantage_sources=[], disadvantage_sources=[])
    assert condition_rules._project_condition_changes([condition]) == [
        change for change in changes_before if change.key != "flags.advantage.attack"
    ]
    assert (
        condition_rules.conditions_grant_advantage_on_attack([condition], []) == own_attack_before
    )
    assert condition_rules.project_passive_save_modifiers([condition]) == saves_before


@pytest.mark.parametrize("condition", ["poisoned", "prone"])
def test_generic_target_advantage_support_does_not_enable_unopted_clause(
    condition: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clause = ConditionEffect(kind=ConditionEffectKind.ADVANTAGE_ATTACKS_AGAINST)
    assert project_condition_effects([clause]) == [
        ActiveEffectChange(key="flags.advantage.attack", mode="override", value=True)
    ]
    before = condition_rules._project_condition_changes([condition])
    monkeypatch.setitem(
        condition_rules._DECLARATIVE_CONDITION_EFFECTS,
        condition,
        (*condition_rules._DECLARATIVE_CONDITION_EFFECTS[condition], clause),
    )
    assert condition_rules._project_condition_changes([condition]) == before
    rolled, rng = _swing([condition])
    _assert_roll(rolled, rng, mode="normal", advantage_sources=[], disadvantage_sources=[])


@pytest.mark.parametrize("condition", _TARGET_CONDITIONS)
@pytest.mark.parametrize("poisoned_attacker", [False, True])
def test_player_intent_keeps_event_order_and_d20_then_bonus_then_damage_draws(
    condition: str, poisoned_attacker: bool
) -> None:
    effects = [
        ActiveEffect(
            id="effect:target",
            name="Target condition",
            origin="test",
            target_id="mon:foe",
            statuses={condition},
        ),
        ActiveEffect(
            id="effect:attacker",
            name="Attack bonus",
            origin="test",
            target_id="char:hero",
            statuses={"poisoned"} if poisoned_attacker else set(),
            changes=[ActiveEffectChange(key="attack.roll.bonus", mode="add", value="1d4")],
        ),
    ]
    start = run_async(
        start_combat(
            session_id="target-condition-parity",
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
            active_effects=tuple(effects),
        )
    )
    live = _get_live(start.handle)
    mirror = random.Random()
    mirror.setstate(live.rng.getstate())
    event_count = len(live.event_log)

    run_async(
        submit_player_intent(
            start.handle,
            actor_id="char:hero",
            intent=PlayerIntent(intent_type="attack", weapon_id="dagger", target_id="mon:foe"),
        )
    )

    events = live.event_log[event_count:]
    assert [event.type for event in events] == [
        "intent_submitted",
        "attack_rolled",
        "damage_applied",
    ]
    _, attack, damage = events
    assert isinstance(attack, AttackRolled)
    assert isinstance(damage, DamageApplied)
    mode = "normal" if poisoned_attacker else "advantage"
    assert attack.natural == _mirror_roll(mirror, mode)
    assert attack.roll_total == attack.natural + attack.modifier + mirror.randint(1, 4)
    assert attack.advantage == mode
    assert attack.advantage_sources == ["condition:target"]
    assert attack.disadvantage_sources == (["condition:attacker"] if poisoned_attacker else [])
    assert attack.sources == attack.advantage_sources + attack.disadvantage_sources
    assert attack.is_hit is True
    # Existing nearby auto-crit remains independent of the migrated advantage.
    is_crit = condition in {"paralyzed", "unconscious"}
    assert attack.is_crit is is_crit
    assert damage.is_crit is is_crit
    raw_damage = sum(mirror.randint(1, 4) for _ in range(2 if is_crit else 1))
    assert damage.amount == (raw_damage // 2 if condition == "petrified" else raw_damage)
    assert live.rng.getstate() == mirror.getstate()
