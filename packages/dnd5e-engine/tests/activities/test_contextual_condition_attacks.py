"""Contextual attack migration preserves events, damage and the seeded stream."""

from __future__ import annotations

import random

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.common import AttackActivity
from dnd5e_srd_data.schema.condition import ConditionEffectKind

from dnd5e_engine import PlayerIntent
from dnd5e_engine.activities import attack as attack_rules
from dnd5e_engine.activities.context import ActivityResolutionContext
from dnd5e_engine.activities.resolver import resolve_activity
from dnd5e_engine.events import AttackRolled, DamageApplied
from dnd5e_engine.orchestrator import (
    _find_combatant,
    _frightened_approach_blocked,
    _get_live,
    _resolve_opportunity_attack,
    start_combat,
    submit_player_intent,
)
from dnd5e_engine.rules import conditions as rules
from dnd5e_engine.specs import EncounterMemberSpec, PartyMemberSpec
from dnd5e_engine.types.combat import Combatant
from dnd5e_engine.types.conditions import ActiveCondition
from tests.e2e.harness import cell, grid_scene, run_async

_CURRENT_HELPER = rules.conditions_grant_advantage_on_attack


def _legacy_context_rules(
    attacker_conditions,
    target_conditions,
    *,
    distance_ft=None,
    grappler_id=None,
    target_id=None,
    attacker_invisibility_pierced=False,
    target_invisibility_pierced=False,
    fear_source_in_sight=True,
):
    """Frozen pre-migration rules for the four clauses; other clauses are unchanged."""
    contextual = {"invisible", "frightened", "grappled"}
    advantage, disadvantage = _CURRENT_HELPER(
        [c for c in attacker_conditions if c.lower() not in contextual],
        [c for c in target_conditions if c.lower() not in contextual],
        distance_ft=distance_ft,
    )
    attacker = {c.lower() for c in attacker_conditions}
    target = {c.lower() for c in target_conditions}
    advantage |= "invisible" in attacker and not attacker_invisibility_pierced
    disadvantage |= "invisible" in target and not target_invisibility_pierced
    disadvantage |= "frightened" in attacker and fear_source_in_sight
    disadvantage |= "grappled" in attacker and grappler_id is not None and target_id != grappler_id
    return advantage, disadvantage


class _RecordingRandom(random.Random):
    def __init__(self, seed=7):
        super().__init__(seed)
        self.draws = []

    def randint(self, a, b):
        value = super().randint(a, b)
        self.draws.append((a, b, value))
        return value


_SCENARIOS = [
    pytest.param([], [], {}, [], [], id="baseline"),
    pytest.param(["invisible"], [], {}, ["condition:attacker"], [], id="invisible-attacker"),
    pytest.param(
        ["invisible"],
        [],
        {"attacker_invisibility_pierced_by": {"mon:0123456789ab": True}},
        [],
        [],
        id="attacker-pierced",
    ),
    pytest.param([], ["invisible"], {}, [], ["condition:target"], id="invisible-target"),
    pytest.param(
        [],
        ["invisible"],
        {"target_invisibility_pierced": {"mon:0123456789ab": True}},
        [],
        [],
        id="target-pierced",
    ),
    pytest.param(
        ["invisible"],
        ["invisible"],
        {},
        ["condition:attacker"],
        ["condition:target"],
        id="both-invisible-cancel",
    ),
    pytest.param(
        ["invisible"],
        ["invisible"],
        {"attacker_invisibility_pierced_by": {"mon:0123456789ab": True}},
        [],
        ["condition:target"],
        id="only-attacker-pierced",
    ),
    pytest.param(
        ["invisible"],
        ["invisible"],
        {"target_invisibility_pierced": {"mon:0123456789ab": True}},
        ["condition:attacker"],
        [],
        id="only-target-pierced",
    ),
    pytest.param(
        ["invisible"],
        ["invisible"],
        {
            "target_invisibility_pierced": {"mon:0123456789ab": True},
            "attacker_invisibility_pierced_by": {"mon:0123456789ab": True},
        },
        [],
        [],
        id="both-pierced",
    ),
    pytest.param(["frightened"], [], {}, [], ["condition:attacker"], id="fear-default"),
    pytest.param(
        ["frightened"],
        [],
        {"attacker_fear_source_in_sight": True},
        [],
        ["condition:attacker"],
        id="fear-visible",
    ),
    pytest.param(
        ["frightened"], [], {"attacker_fear_source_in_sight": False}, [], [], id="fear-hidden"
    ),
    pytest.param(["grappled"], [], {}, [], [], id="grappler-unknown"),
    pytest.param(
        ["grappled"], [], {"attacker_grappler_id": "mon:0123456789ab"}, [], [], id="attack-grappler"
    ),
    pytest.param(
        ["grappled"],
        [],
        {"attacker_grappler_id": "mon:abcdefabcdef"},
        [],
        ["condition:attacker"],
        id="other-target",
    ),
    pytest.param(
        ["invisible", "frightened"],
        [],
        {},
        ["condition:attacker"],
        ["condition:attacker"],
        id="invisible-fear-cancel",
    ),
    pytest.param(
        ["invisible", "grappled"],
        [],
        {"attacker_grappler_id": "mon:abcdefabcdef"},
        ["condition:attacker"],
        ["condition:attacker"],
        id="invisible-grapple-cancel",
    ),
    pytest.param(
        ["Invisible", "INVISIBLE", "Grappled", "grappled", "FRIGHTENED"],
        ["Invisible", "invisible"],
        {"attacker_grappler_id": "mon:abcdefabcdef"},
        ["condition:attacker"],
        ["condition:attacker", "condition:target"],
        id="duplicates",
    ),
    pytest.param(
        ["invisible"],
        ["invisible"],
        {
            "attacker_unseen_by": {"mon:0123456789ab": True},
            "target_unseen": {"mon:0123456789ab": True},
        },
        ["condition:attacker", "unseen"],
        ["condition:target", "unseen"],
        id="geometry-independent",
    ),
]


def _swing(attacker_conditions, target_conditions, context, seed, path):
    loader = BundledAssetLoader()
    hero = Combatant(
        entity_id="char:hero",
        entity_type="Character",
        name="Hero",
        initiative=10,
        hp_current=30,
        hp_max=30,
    )
    target = Combatant(
        entity_id="mon:0123456789ab",
        entity_type="Monster",
        name="Target",
        initiative=1,
        hp_current=500,
        hp_max=500,
        ac=10,
    )
    weapon = loader.get_weapon("dagger") if path == "weapon" else None
    if weapon is not None:
        activity = next(a for a in weapon.activities if a.kind == "attack")
    elif path == "spell":
        spell = loader.get_spell("fire-bolt")
        assert spell is not None
        activity = next(a for a in spell.activities if a.kind == "attack")
    else:
        hero = hero.model_copy(update={"entity_type": "Monster"})
        activity = AttackActivity(
            name="Claw",
            attack={"flat": True, "bonus": "5"},
            damage={"parts": [{"number": 1, "denomination": 6, "types": ["slashing"]}]},
        )
    rng = _RecordingRandom(seed)
    events = []
    ctx = ActivityResolutionContext(
        rng=rng,
        caster=hero,
        targets=[target],
        event_emitter=events.append,
        caster_abilities={"str": 16, "dex": 10, "int": 16},
        caster_proficiency_bonus=2,
        caster_level=1,
        spellcasting_ability="int",
        attacker_conditions=attacker_conditions,
        target_conditions={target.entity_id: target_conditions},
        target_distance_ft={target.entity_id: 5},
        passive_attack_bonus={hero.entity_id: "1d4"},
        **context,
    )
    resolve_activity(activity, ctx, weapon=weapon)
    return events, rng


@pytest.mark.parametrize(("attacker", "target", "context", "adv", "dis"), _SCENARIOS)
@pytest.mark.parametrize("seed", [7, 5, 31])
@pytest.mark.parametrize("path", ["weapon", "monster", "spell"])
def test_attack_event_damage_and_rng_match_pre_migration(
    attacker, target, context, adv, dis, seed, path, monkeypatch
):
    events, rng = _swing(attacker, target, context, seed, path)
    attack = next(e for e in events if isinstance(e, AttackRolled))
    mode = "normal" if bool(adv) == bool(dis) else "advantage" if adv else "disadvantage"
    mirror = _RecordingRandom(seed)
    first = mirror.randint(1, 20)
    natural = first
    if mode != "normal":
        second = mirror.randint(1, 20)
        natural = max(first, second) if mode == "advantage" else min(first, second)
    assert attack.natural == natural
    assert attack.advantage == mode
    assert attack.advantage_sources == adv
    assert attack.disadvantage_sources == dis
    assert attack.sources == adv + dis
    assert attack.roll_total == natural + attack.modifier + mirror.randint(1, 4)
    assert sum(a == 1 and b == 20 for a, b, value in rng.draws) == (1 if mode == "normal" else 2)
    monkeypatch.setattr(attack_rules, "conditions_grant_advantage_on_attack", _legacy_context_rules)
    legacy_events, legacy_rng = _swing(attacker, target, context, seed, path)
    assert events == legacy_events  # Includes exact hit/crit and all damage events.
    assert rng.draws == legacy_rng.draws
    assert rng.getstate() == legacy_rng.getstate()


def _start(*, candidate=False, monster=False):
    result = run_async(
        start_combat(
            session_id="contextual-attacks",
            party=[
                PartyMemberSpec(
                    entity_id="char:hero",
                    name="Hero",
                    initiative=20,
                    hp_current=100,
                    hp_max=100,
                    ac=1,
                    strength=16,
                    class_slug="fighter",
                    character_level=5,
                    equipment=("dagger",),
                    zone_id=cell(0, 0),
                )
            ],
            encounter=[
                EncounterMemberSpec(
                    entity_id=eid,
                    entity_type="Monster",
                    name=eid,
                    initiative=init,
                    hp_current=500,
                    hp_max=500,
                    ac=1,
                    monster_template_slug="goblin-warrior" if monster else None,
                    zone_id=zone,
                )
                for eid, init, zone in [("mon:0123456789ab", 10, cell(0, 1))]
                + ([("mon:candidate", 9, cell(1, 1))] if candidate else [])
            ],
            grid_scene=grid_scene(),
            rng_seed=7,
        )
    )
    live = _get_live(result.handle)
    rng = _RecordingRandom()
    rng.setstate(live.rng.getstate())
    live.rng = rng
    return result, live, rng


def _condition(live, entity_id, name, source="implied:test"):
    combatant = _find_combatant(live, entity_id)
    assert combatant is not None
    combatant.conditions.append(
        ActiveCondition(condition=name, source_entity_id=source, scope="combat")
    )


@pytest.mark.parametrize("condition", ["invisible", "frightened", "grappled"])
def test_player_intent_uses_contextual_rules_with_exact_rng(condition):
    result, live, rng = _start()
    _condition(live, "char:hero", condition, "mon:abcdefabcdef")
    mirror = _RecordingRandom()
    mirror.setstate(rng.getstate())
    offset = len(live.event_log)
    run_async(
        submit_player_intent(
            result.handle,
            actor_id="char:hero",
            intent=PlayerIntent(
                intent_type="attack", weapon_id="dagger", target_id="mon:0123456789ab"
            ),
        )
    )
    events = live.event_log[offset:]
    assert [e.type for e in events] == ["intent_submitted", "attack_rolled", "damage_applied"]
    attack, damage = events[1:]
    assert isinstance(attack, AttackRolled)
    assert isinstance(damage, DamageApplied)
    first, second = mirror.randint(1, 20), mirror.randint(1, 20)
    assert attack.natural == (
        max(first, second) if condition == "invisible" else min(first, second)
    )
    assert attack.advantage == ("advantage" if condition == "invisible" else "disadvantage")
    assert attack.advantage_sources == (["condition:attacker"] if condition == "invisible" else [])
    assert attack.disadvantage_sources == (
        [] if condition == "invisible" else ["condition:attacker"]
    )
    assert attack.roll_total == attack.natural + attack.modifier
    assert attack.is_hit
    assert not attack.is_crit
    assert damage.amount == mirror.randint(1, 4) + 3
    assert rng.draws == mirror.draws
    assert rng.getstate() == mirror.getstate()


def test_player_intent_cleave_uses_candidate_visibility_and_exact_rng():
    result, live, rng = _start(candidate=True)
    _condition(live, "mon:candidate", "invisible")
    mirror = _RecordingRandom()
    mirror.setstate(rng.getstate())
    offset = len(live.event_log)
    run_async(
        submit_player_intent(
            result.handle,
            actor_id="char:hero",
            intent=PlayerIntent(
                intent_type="attack", weapon_id="halberd", target_id="mon:0123456789ab"
            ),
        )
    )
    events = live.event_log[offset:]
    assert [e.type for e in events] == [
        "intent_submitted",
        "attack_rolled",
        "damage_applied",
        "attack_rolled",
        "damage_applied",
    ]
    _, primary, primary_damage, chain, chain_damage = events
    assert primary.natural == mirror.randint(1, 20)
    assert primary.advantage == "normal"
    assert primary_damage.amount == mirror.randint(1, 10) + 3
    first, second = mirror.randint(1, 20), mirror.randint(1, 20)
    assert chain.target_id == "mon:candidate"
    assert chain.natural == min(first, second)
    assert chain.advantage == "disadvantage"
    assert chain.sources == ["condition:target"]
    assert chain.advantage_sources == []
    assert chain.disadvantage_sources == ["condition:target"]
    assert chain.roll_total == chain.natural + chain.modifier
    assert chain.is_hit
    assert not chain.is_crit
    assert chain_damage.source_id == "mastery:cleave"
    assert chain_damage.amount == mirror.randint(1, 10)
    assert rng.draws == mirror.draws
    assert rng.getstate() == mirror.getstate()


@pytest.mark.parametrize("reactor_kind", ["character", "monster"])
@pytest.mark.parametrize("condition", ["invisible", "frightened", "grappled"])
def test_opportunity_attack_context_matches_pre_migration(reactor_kind, condition, monkeypatch):
    def run():
        _result, live, rng = _start(monster=True)
        hero = _find_combatant(live, "char:hero")
        foe = _find_combatant(live, "mon:0123456789ab")
        reactor, target = (hero, foe) if reactor_kind == "character" else (foe, hero)
        _condition(live, reactor.entity_id, condition, "mon:abcdefabcdef")
        offset = len(live.event_log)
        _resolve_opportunity_attack(live, reactor, target)
        return live.event_log[offset:], rng

    events, rng = run()
    rolled = next(e for e in events if isinstance(e, AttackRolled))
    assert rolled.is_opportunity_attack
    assert rolled.advantage == ("advantage" if condition == "invisible" else "disadvantage")
    assert rolled.sources == ["condition:attacker"]
    assert sum(a == 1 and b == 20 for a, b, value in rng.draws) == 2
    monkeypatch.setattr(attack_rules, "conditions_grant_advantage_on_attack", _legacy_context_rules)
    legacy_events, legacy_rng = run()
    assert events == legacy_events
    assert rng.draws == legacy_rng.draws
    assert rng.getstate() == legacy_rng.getstate()


@pytest.mark.parametrize("remove_from", ["allowlist", "canonical"])
def test_frightened_attack_clause_removal_preserves_checks_and_movement(remove_from, monkeypatch):
    _result, live, _rng = _start()
    hero = _find_combatant(live, "char:hero")
    assert hero is not None
    _condition(live, hero.entity_id, "frightened", "mon:0123456789ab")
    path = [cell(0, 0), cell(0, 1)]
    assert _frightened_approach_blocked(live, hero, path)
    before = rules.project_passive_check_modifiers(["frightened"])
    kind = ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS
    if remove_from == "allowlist":
        monkeypatch.setitem(
            rules._DECLARATIVE_CONDITION_MIGRATIONS,
            "frightened",
            rules._DECLARATIVE_CONDITION_MIGRATIONS["frightened"] - {kind},
        )
    else:
        monkeypatch.setitem(
            rules._DECLARATIVE_CONDITION_EFFECTS,
            "frightened",
            tuple(c for c in rules._DECLARATIVE_CONDITION_EFFECTS["frightened"] if c.kind != kind),
        )
    assert rules.conditions_grant_advantage_on_attack(["frightened"], []) == (False, False)
    assert rules.project_passive_check_modifiers(["frightened"]) == before
    assert _frightened_approach_blocked(live, hero, path)
