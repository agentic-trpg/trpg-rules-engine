"""Petrified resistance and Poisoned immunity reach the existing live seams."""

from __future__ import annotations

import random
from dataclasses import replace
from typing import get_args

import pytest
from dnd5e_srd_data.schema.common import DamageActivity, PassiveEffect, UtilityActivity
from dnd5e_srd_data.schema.condition import ConditionEffectKind

from dnd5e_engine import PlayerIntent
from dnd5e_engine.activities.apply import apply_damage
from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.context import ActivityResolutionContext
from dnd5e_engine.activities.effects import apply_activity_effects
from dnd5e_engine.activities.mastery import _resolve_topple
from dnd5e_engine.activities.resolver import resolve_activity
from dnd5e_engine.events import (
    AttackRolled,
    ConditionApplied,
    ConditionRemoved,
    DamageApplied,
    DamageType,
    EffectApplied,
    HealingApplied,
    SaveRolled,
)
from dnd5e_engine.orchestrator import (
    _build_hydration_payload,
    _emit,
    _find_combatant,
    _fold_condition_onto_combatant,
    _get_live,
    _seed_active_effects,
    start_combat,
    submit_player_intent,
)
from dnd5e_engine.rules import conditions as rules
from dnd5e_engine.specs import EncounterMemberSpec, PartyMemberSpec
from dnd5e_engine.types.combat import Combatant
from dnd5e_engine.types.conditions import ActiveCondition
from dnd5e_engine.types.effects import ActiveEffect
from tests.e2e.harness import cell, grid_scene, run_async

TARGET = "mon:target"
HERO = "char:hero"


class _RecordingRandom(random.Random):
    def __init__(self, seed=7):
        super().__init__(seed)
        self.draws = []

    def randint(self, a, b):
        result = super().randint(a, b)
        self.draws.append((a, b, result))
        return result


def _active(name):
    return ActiveCondition(condition=name, source_entity_id="implied:test", scope="combat")


def _effect(status, target=TARGET):
    return ActiveEffect(
        id=f"effect:{status}",
        name=status,
        target_id=target,
        origin="test:defenses",
        statuses={status},
    )


def _start(
    *,
    petrified=True,
    immunities=(),
    hero_immunities=(),
    existing_poison=False,
    seed=7,
    target_stats=None,
):
    effects = ([_effect("poisoned")] if existing_poison else []) + (
        [_effect("petrified")] if petrified else []
    )
    result = run_async(
        start_combat(
            session_id="petrified-defenses",
            rng_seed=seed,
            grid_scene=grid_scene(),
            party=[
                PartyMemberSpec(
                    entity_id=HERO,
                    name="Hero",
                    initiative=20,
                    hp_current=100,
                    hp_max=100,
                    strength=16,
                    attack_bonus=5,
                    condition_immunities=list(hero_immunities),
                    zone_id=cell(0, 0),
                )
            ],
            encounter=[
                EncounterMemberSpec(
                    entity_id=TARGET,
                    entity_type="Monster",
                    name="Target",
                    initiative=1,
                    hp_current=500,
                    hp_max=500,
                    ac=1,
                    condition_immunities=list(immunities),
                    zone_id=cell(1, 0),
                    **(target_stats or {}),
                )
            ],
            active_effects=tuple(effects),
        )
    )
    live = _get_live(result.handle)
    rng = _RecordingRandom(seed)
    rng.setstate(live.rng.getstate())
    live.rng = rng
    return result, live, rng


def _context(live, *, effects=(), spell=False):
    caster = _find_combatant(live, HERO)
    target = _find_combatant(live, TARGET)
    payload = _build_hydration_payload(live, caster)
    return build_activity_context(
        caster,
        [target],
        rng=live.rng,
        event_emitter=lambda e: _emit(live, e),
        slot_level=None,
        base_spell_level=0 if spell else None,
        spellcasting_ability="int" if spell else None,
        concentration=False,
        source_passive_effects=list(effects),
        spell_book={},
        passive_damage_modifiers=payload["passive_damage_modifiers"],
        save_modifiers=payload["save_modifiers"],
    )


@pytest.mark.parametrize("damage_type", get_args(DamageType))
@pytest.mark.parametrize("amount", [10, 11])
@pytest.mark.parametrize("magical", [False, True])
def test_every_damage_type_halves_once_without_rng(damage_type, amount, magical):
    target = Combatant(
        entity_id=TARGET,
        entity_type="Monster",
        name="Stone",
        initiative=1,
        hp_current=500,
        conditions=[_active(n) for n in ["petrified", "PETRIFIED", "petrified"]],
        damage_resistances=["bludgeoning", "piercing", "slashing"],
        physical_resistances_nonmagical_only=True,
    )
    rng = _RecordingRandom()
    before = rng.getstate()
    events = []
    ctx = ActivityResolutionContext(
        caster=target,
        caster_abilities={},
        targets=[target],
        rng=rng,
        event_emitter=events.append,
        passive_damage_modifiers={
            TARGET: rules.project_passive_damage_modifiers([c.condition for c in target.conditions])
        },
    )
    assert ctx.passive_damage_modifiers[TARGET]["immunities"] == []
    assert apply_damage(target, {damage_type: amount}, ctx, magical=magical) == amount // 2
    assert len(events) == 1
    assert isinstance(events[0], DamageApplied)
    assert events[0].damage_type == damage_type
    assert events[0].amount == amount // 2
    assert rng.draws == []
    assert rng.getstate() == before


@pytest.mark.parametrize(
    ("static", "sidecar", "expected"),
    [
        ({}, {"vulnerabilities": ["poison"]}, 11),
        ({"damage_vulnerabilities": ["poison"]}, {}, 11),
        ({"damage_immunities": ["poison"]}, {}, 0),
        ({"damage_vulnerabilities": ["poison"], "damage_immunities": ["poison"]}, {}, 0),
        ({}, {"immunities": ["poison"]}, 0),
        ({"damage_resistances": ["all"]}, {"resistances": ["all", "all"]}, 5),
    ],
)
def test_existing_modifier_order_and_nonstacking(static, sidecar, expected):
    _result, live, rng = _start(target_stats=static)
    target = _find_combatant(live, TARGET)
    ctx = _context(live)
    modifiers = ctx.passive_damage_modifiers[TARGET]
    for key, values in sidecar.items():
        modifiers[key].extend(values)
    state = rng.getstate()
    offset = len(live.event_log)
    assert apply_damage(target, {"poison": 11}, ctx) == expected
    assert live.event_log[offset].amount == expected
    assert live.tracked_hp[TARGET] == 500 - expected
    assert rng.draws == []
    assert rng.getstate() == state


@pytest.mark.parametrize("seed", [7, 5, 31])
def test_weapon_intent_damage_hp_order_and_rng(seed):
    result, live, rng = _start(seed=seed)
    mirror = _RecordingRandom(seed)
    mirror.setstate(rng.getstate())
    offset = len(live.event_log)
    run_async(
        submit_player_intent(
            result.handle,
            actor_id=HERO,
            intent=PlayerIntent(intent_type="attack", weapon_id="dagger", target_id=TARGET),
        )
    )
    events = live.event_log[offset:]
    assert [e.type for e in events] == ["intent_submitted", "attack_rolled", "damage_applied"]
    _, attack, damage = events
    assert isinstance(attack, AttackRolled)
    assert attack.natural == max(mirror.randint(1, 20), mirror.randint(1, 20))
    assert attack.advantage == "advantage"
    assert attack.advantage_sources == ["condition:target"]
    assert attack.disadvantage_sources == []
    assert attack.roll_total == attack.natural + attack.modifier
    assert attack.is_hit
    assert attack.is_crit == (attack.natural == 20)
    raw = sum(mirror.randint(1, 4) for _ in range(2 if attack.is_crit else 1)) + 3
    assert damage.amount == raw // 2
    assert damage.is_crit == attack.is_crit
    assert live.tracked_hp[TARGET] == 500 - damage.amount
    assert _find_combatant(live, TARGET).hp_current == 500 - damage.amount
    assert rng.draws == mirror.draws
    assert rng.getstate() == mirror.getstate()


@pytest.mark.parametrize("petrified", [True, False])
def test_spell_damage_activity_uses_hydration_hp_and_exact_rng(petrified):
    _result, live, rng = _start(petrified=petrified)
    ctx = _context(live, spell=True)
    activity = DamageActivity(
        name="Poison burst",
        damage={"parts": [{"number": 2, "denomination": 6, "bonus": "3", "types": ["poison"]}]},
    )
    mirror = _RecordingRandom()
    mirror.setstate(rng.getstate())
    raw = mirror.randint(1, 6) + mirror.randint(1, 6) + 3
    offset = len(live.event_log)
    resolve_activity(activity, ctx)
    events = live.event_log[offset:]
    assert [e.type for e in events] == ["damage_applied"]
    assert events[0].amount == (raw // 2 if petrified else raw)
    assert live.tracked_hp[TARGET] == 500 - events[0].amount
    assert _find_combatant(live, TARGET).hp_current == 500 - events[0].amount
    assert rng.draws == mirror.draws
    assert rng.getstate() == mirror.getstate()


@pytest.mark.parametrize("petrified", [True, False])
@pytest.mark.parametrize("static_immune", [True, False])
def test_activity_status_respects_static_projected_union(petrified, static_immune):
    _result, live, rng = _start(
        petrified=petrified, immunities=["poisoned"] if static_immune else []
    )
    pe = PassiveEffect(
        id="poison",
        name="Poison",
        statuses=["poisoned"],
        changes=[{"key": "system.attributes.ac.bonus", "mode": 2, "value": "1"}],
    )
    ctx = _context(live, effects=[pe])
    before = rng.getstate()
    offset = len(live.event_log)
    apply_activity_effects(
        UtilityActivity(effects=[{"id": "poison"}]),
        ctx,
        _find_combatant(live, TARGET),
        save_succeeded=None,
        cast_level=0,
    )
    immune = petrified or static_immune
    assert [e.type for e in live.event_log[offset:]] == (
        ["effect_applied"] if immune else ["effect_applied", "condition_applied"]
    )
    assert any(c.condition == "poisoned" for c in _find_combatant(live, TARGET).conditions) is (
        not immune
    )
    assert ("poisoned" in live.active_conditions.get(TARGET, set())) is (not immune)
    assert live.active_effects[TARGET][-1].changes[0].value == "1"
    assert rng.draws == []
    assert rng.getstate() == before


@pytest.mark.parametrize("path", ["effect", "event", "fold", "seed"])
@pytest.mark.parametrize("dynamic", [True, False])
def test_every_attachment_seam_blocks_poisoned_on_both_stores(path, dynamic):
    _result, live, rng = _start(petrified=dynamic, immunities=[] if dynamic else ["poisoned"])
    offset = len(live.event_log)
    observed = []
    live.event_listeners.append(observed.append)
    queue_size = live.event_queue.qsize()
    state = rng.getstate()
    if path == "effect":
        _emit(live, EffectApplied(effect=_effect("poisoned")))
    elif path == "event":
        _emit(live, ConditionApplied(target_id=TARGET, condition="poisoned"))
    elif path == "fold":
        _fold_condition_onto_combatant(live, TARGET, "poisoned")
    else:
        _seed_active_effects(live, [_effect("poisoned")])
    assert not any(c.condition == "poisoned" for c in _find_combatant(live, TARGET).conditions)
    assert "poisoned" not in live.active_conditions.get(TARGET, set())
    assert [e.type for e in live.event_log[offset:]] == (
        ["effect_applied"] if path == "effect" else []
    )
    assert observed == live.event_log[offset:]
    assert live.event_queue.qsize() == queue_size + len(observed)
    assert not any("poisoned" in values for values in live.conditions_by_effect.values())
    assert rng.draws == []
    assert rng.getstate() == state


@pytest.mark.parametrize("remove_from", ["allowlist", "canonical"])
@pytest.mark.parametrize(
    "kind", [ConditionEffectKind.RESIST_ALL_DAMAGE, ConditionEffectKind.IMMUNE_TO_CONDITION]
)
def test_clause_removal_changes_real_damage_and_attachment(kind, remove_from, monkeypatch):
    if remove_from == "allowlist":
        monkeypatch.setitem(
            rules._DECLARATIVE_CONDITION_MIGRATIONS,
            "petrified",
            rules._DECLARATIVE_CONDITION_MIGRATIONS["petrified"] - {kind},
        )
    else:
        monkeypatch.setitem(
            rules._DECLARATIVE_CONDITION_EFFECTS,
            "petrified",
            tuple(e for e in rules._DECLARATIVE_CONDITION_EFFECTS["petrified"] if e.kind != kind),
        )
    _result, live, rng = _start()
    target = _find_combatant(live, TARGET)
    state = rng.getstate()
    ctx = _context(live)
    offset = len(live.event_log)
    apply_damage(target, {"poison": 10}, ctx)
    _emit(live, ConditionApplied(target_id=TARGET, condition="poisoned"))
    events = live.event_log[offset:]
    assert events[0].amount == (10 if kind == ConditionEffectKind.RESIST_ALL_DAMAGE else 5)
    can_attach = kind == ConditionEffectKind.IMMUNE_TO_CONDITION
    assert [e.type for e in events] == (
        ["damage_applied", "condition_applied"] if can_attach else ["damage_applied"]
    )
    assert ("poisoned" in live.active_conditions.get(TARGET, set())) is can_attach
    assert (
        any(c.condition == "poisoned" for c in _find_combatant(live, TARGET).conditions)
        is can_attach
    )
    assert rng.draws == []
    assert rng.getstate() == state


def test_typed_scope_mutation_controls_direct_events_and_shared_topple(monkeypatch):
    monkeypatch.setitem(
        rules._DECLARATIVE_CONDITION_EFFECTS,
        "petrified",
        tuple(
            e.model_copy(update={"condition_slugs": ["prone"]})
            if e.kind == ConditionEffectKind.IMMUNE_TO_CONDITION
            else e
            for e in rules._DECLARATIVE_CONDITION_EFFECTS["petrified"]
        ),
    )
    _result, live, _rng = _start()
    _emit(live, ConditionApplied(target_id=TARGET, condition="poisoned"))
    assert "poisoned" in live.active_conditions[TARGET]
    ctx = replace(_context(live), variables={"force_save_d20": 1})
    offset = len(live.event_log)
    _resolve_topple(ctx, _find_combatant(live, TARGET), "str")
    events = live.event_log[offset:]
    assert len(events) == 1
    assert isinstance(events[0], SaveRolled)
    assert not events[0].succeeded
    assert "prone" not in live.active_conditions[TARGET]
    assert not any(c.condition == "prone" for c in _find_combatant(live, TARGET).conditions)


def test_petrified_never_removes_poisoned_already_present():
    _result, live, _rng = _start(existing_poison=True)
    assert {c.condition for c in _find_combatant(live, TARGET).conditions} == {
        "poisoned",
        "petrified",
    }
    assert live.active_conditions[TARGET] == {"poisoned", "petrified"}
    offset = len(live.event_log)
    _emit(live, ConditionApplied(target_id=TARGET, condition="poisoned"))
    assert live.event_log[offset:] == []
    assert "poisoned" in live.active_conditions[TARGET]


@pytest.mark.parametrize("static_immune", [True, False])
def test_removing_petrified_releases_only_its_dynamic_immunity(static_immune):
    _result, live, _rng = _start(petrified=False, immunities=["poisoned"] if static_immune else [])
    _emit(live, ConditionApplied(target_id=TARGET, condition="petrified"))
    _emit(live, ConditionRemoved(target_id=TARGET, condition="petrified"))
    _emit(live, ConditionApplied(target_id=TARGET, condition="poisoned"))
    assert ("poisoned" in live.active_conditions[TARGET]) is (not static_immune)


def test_revive_status_fold_honors_existing_static_prone_immunity():
    _result, live, _rng = _start(petrified=False, hero_immunities=["prone"])
    _emit(live, DamageApplied(target_id=HERO, amount=100, damage_type="poison", is_overkill=False))
    _emit(live, HealingApplied(target_id=HERO, amount=5))
    assert "prone" not in live.active_conditions.get(HERO, set())
    assert not any(c.condition == "prone" for c in _find_combatant(live, HERO).conditions)
