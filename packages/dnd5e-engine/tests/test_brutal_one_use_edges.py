"""Canonical Brutal one-use clauses survive unrelated expiry and reaction edges."""

from __future__ import annotations

import dataclasses
import json
from random import Random

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader
from pydantic import BaseModel

from dnd5e_engine import orchestrator as orch
from dnd5e_engine.events import (
    AttackFailed,
    AttackRiderTriggered,
    AttackRolled,
    DamageApplied,
    DeathSaveRolled,
    EffectExpired,
    EffectModifiersConsumed,
    ReactionTriggered,
)
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.live_attack_riders import prevents_opportunity_attacks
from tests.c20_support import act, combatant, events, foe, pc, start
from tests.test_brutal_options_runtime import (
    ALLY,
    FOE,
    HERO,
    _attack,
    _barbarian,
    _combat,
    _effects,
    _to_next_hero_turn,
)


@pytest.fixture(autouse=True)
def _fresh_loader():
    set_lib_loader_for_tests(BundledAssetLoader())
    yield
    set_lib_loader_for_tests(None)


def test_staggering_canonical_death_save_consumes_only_next_save_clause():
    dying = "char:dying"
    handle, live = start(
        [
            _barbarian(),
            pc(dying, initiative=10, zone_id="1,0", ac=1, hp_current=500, hp_max=500),
        ],
        seed=7,
        encounter=[foe(zone_id="8,8")],
    )
    _attack(handle, ("staggering-blow",), target=dying)
    [effect] = _effects(live, dying)
    identity = (dying, effect.id, effect.origin)
    assert live.effect_lifecycles[identity].remaining_one_use_modifiers == (
        "next_save_disadvantage",
    )
    orch._emit(
        live,
        DamageApplied(
            target_id=dying,
            amount=combatant(live, dying).hp_current,
            damage_type="fire",
            is_overkill=False,
            source_actor_id=FOE,
        ),
    )
    assert combatant(live, dying).hp_current == 0
    reference = Random()
    reference.setstate(live.rng.getstate())
    kept = min(reference.randint(1, 20), reference.randint(1, 20))
    act(handle, HERO, intent_type="pass")
    [save] = events(live, DeathSaveRolled)
    assert save.target_id == dying
    assert save.roll_total == kept
    [consumed] = events(live, EffectModifiersConsumed)
    assert (consumed.target_id, consumed.effect_id, consumed.origin) == identity
    assert consumed.keys == ("flags.save.next_disadvantage",)
    assert live.event_log.index(consumed) < live.event_log.index(save)
    [remaining] = _effects(live, dying)
    assert [change.key for change in remaining.changes] == ["flags.cannot_make_opportunity_attacks"]
    assert not live.effect_lifecycles[identity].remaining_one_use_modifiers
    assert prevents_opportunity_attacks(live, dying)
    assert not [event for event in events(live, EffectExpired) if event.origin == effect.origin]
    assert live.rng.getstate() == reference.getstate()


def test_unused_sundering_expires_at_source_next_start_and_cannot_buff_later_attack():
    handle, live = _combat(seed=7)
    _attack(handle, ("sundering-blow",))
    [effect] = _effects(live)
    identity = (FOE, effect.id, effect.origin)
    assert live.effect_lifecycles[identity].remaining_one_use_modifiers == (
        "next_attack_bonus_other_creature",
    )
    _to_next_hero_turn(handle, live)
    assert not _effects(live)
    assert identity not in live.effect_lifecycles
    [expired] = [event for event in events(live, EffectExpired) if event.origin == effect.origin]
    assert (expired.target_id, expired.effect_id, expired.reason) == (FOE, effect.id, "duration")
    assert not events(live, EffectModifiersConsumed)
    act(handle, HERO, intent_type="pass")
    _attack(handle, actor=ALLY, reckless=False)
    assert events(live, AttackRolled)[-1].modifier == 0
    assert not events(live, EffectModifiersConsumed)


def test_sundering_bonus_is_consumed_before_real_shield_miss_and_is_not_refunded():
    shielded = "char:shielded"
    handle, live = start(
        [
            _barbarian(initiative=30, attack_bonus=99),
            pc(
                shielded,
                initiative=20,
                zone_id="1,0",
                class_slug="wizard",
                character_level=5,
                ac=12,
                hp_current=500,
                hp_max=500,
                spells_known=["shield"],
                spell_slots={1: 2},
            ),
            pc(
                ALLY,
                initiative=10,
                zone_id="0,1",
                class_slug="fighter",
                character_level=5,
                strength=18,
                attack_bonus=4,
                equipment=("longsword",),
            ),
        ],
        seed=7,
        encounter=[foe(zone_id="8,8")],
    )
    _attack(handle, ("sundering-blow",), target=shielded)
    [effect] = _effects(live, shielded)
    identity = (shielded, effect.id, effect.origin)
    act(handle, HERO, intent_type="pass")
    act(handle, shielded, intent_type="ready", spell_id="shield")
    offset = len(live.event_log)
    reference = Random()
    reference.setstate(live.rng.getstate())
    natural = reference.randint(1, 20)
    assert 12 <= natural + 9 < 17
    _attack(handle, actor=ALLY, reckless=False, target=shielded)
    attack = events(live, AttackRolled)[-1]
    assert (attack.natural, attack.modifier, attack.roll_total, attack.is_hit) == (
        natural,
        9,
        natural + 9,
        False,
    )
    [consumed] = events(live, EffectModifiersConsumed)
    assert (consumed.target_id, consumed.effect_id, consumed.origin) == identity
    assert consumed.keys == ("attack.next_bonus",)
    [reaction] = events(live, ReactionTriggered)
    assert live.event_log.index(consumed) < live.event_log.index(reaction)
    assert not [event for event in live.event_log[offset:] if isinstance(event, DamageApplied)]
    assert live.rng.getstate() == reference.getstate()
    [remaining] = _effects(live, shielded)
    assert remaining.changes == []
    assert not live.effect_lifecycles[identity].remaining_one_use_modifiers
    assert not combatant(live, shielded).reaction_available
    _attack(handle, actor=ALLY, reckless=False, target=shielded)
    assert events(live, AttackRolled)[-1].modifier == 4
    assert len(events(live, EffectModifiersConsumed)) == 1
    assert len(events(live, ReactionTriggered)) == 1


def _normalize(value):
    if isinstance(value, BaseModel):
        return _normalize(value.model_dump(mode="json"))
    if dataclasses.is_dataclass(value):
        return _normalize(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((_normalize(item) for item in value), key=str)
    if isinstance(value, (tuple, list)):
        return [_normalize(item) for item in value]
    return value


def test_frenzy_same_seed_replays_events_resources_effects_state_and_rng_byte_exactly():
    runs = []
    for _ in range(2):
        handle, live = _combat(berserker=True, seed=7)
        act(handle, HERO, intent_type="use_feature", feature_id="rage")
        _attack(handle, ("hamstring-blow", "staggering-blow"))
        _attack(handle, reckless=False)
        assert not events(live, AttackFailed)
        frenzy = [
            event for event in events(live, AttackRiderTriggered) if event.feature_id == "frenzy"
        ]
        assert len(frenzy) == 1
        assert frenzy[0].damage_formula == "4d6"
        assert len(events(live, DamageApplied)) == 2
        assert live.rider_uses
        state = (
            live.event_log,
            live.initiative,
            live.active_effects,
            live.effect_lifecycles,
            live.custom_counters_by_entity,
            live.rider_uses,
            live.actor_zone,
            live.movement_ledgers,
            live.tracked_hp,
            live.tracked_temp_hp,
            live.concentration_chain,
            live.current_actor_id,
            live.turn_serial,
            live.round_number,
            live.rng.getstate(),
        )
        runs.append(json.dumps(_normalize(state), sort_keys=True).encode())
    assert runs[0] == runs[1]
