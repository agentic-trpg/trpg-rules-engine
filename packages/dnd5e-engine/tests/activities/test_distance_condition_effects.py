"""Distance clauses feed the existing attack, critical damage and RNG paths."""

from __future__ import annotations

import random

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.common import AttackActivity, DamagePart
from dnd5e_srd_data.schema.condition import ConditionEffect
from dnd5e_srd_data.schema.condition import ConditionEffectKind as K

from dnd5e_engine import PlayerIntent
from dnd5e_engine.activities.context import ActivityResolutionContext
from dnd5e_engine.activities.resolver import resolve_activity
from dnd5e_engine.events import AttackRolled, CombatEvent, DamageApplied
from dnd5e_engine.orchestrator import _get_live, start_combat, submit_player_intent
from dnd5e_engine.rules import conditions as rules
from dnd5e_engine.rules.effects import attack_distance_flag_applies, project_condition_effects
from dnd5e_engine.specs import EncounterMemberSpec, PartyMemberSpec
from dnd5e_engine.types.combat import Combatant
from dnd5e_engine.types.effects import ActiveEffect, ActiveEffectChange
from tests.e2e.harness import cell, grid_scene, run_async

_SCOPED = (
    (K.ADVANTAGE_ATTACKS_AGAINST, "flags.advantage.attack.within_ft"),
    (K.DISADVANTAGE_ATTACKS_AGAINST, "flags.disadvantage.attack.beyond_ft"),
    (K.AUTO_CRIT_WITHIN_5FT, "flags.auto_crit.attack.within_ft"),
)


class _RecordingRandom(random.Random):
    def __init__(self, seed: int = 7) -> None:
        super().__init__(seed)
        self.draws: list[tuple[int, int, int]] = []

    def randint(self, a: int, b: int) -> int:
        value = super().randint(a, b)
        self.draws.append((a, b, value))
        return value


def _swing(
    conditions: list[str],
    distance: int | None,
    *,
    ac: int = 1,
    forced: int | None = None,
    poisoned: bool = False,
) -> tuple[list[CombatEvent], _RecordingRandom]:
    hero = Combatant(
        entity_id="char:hero",
        entity_type="Character",
        name="Hero",
        initiative=10,
        hp_current=20,
        hp_max=20,
    )
    foe = hero.model_copy(update={"entity_id": "mon:foe", "ac": ac, "hp_current": 500})
    weapon = BundledAssetLoader().get_weapon("dagger")
    assert weapon is not None
    weapon = weapon.model_copy(
        update={"damage_parts": [DamagePart(dice="1d6+4", damage_type="piercing")]}
    )
    activity = AttackActivity(
        id="distance-swing",
        attack={"ability": "str"},
        damage={
            "include_base": True,
            "parts": [{"number": 1, "denomination": 4, "bonus": "2", "types": ["fire"]}],
            "critical": {"bonus": "7"},
        },
    )
    events: list[CombatEvent] = []
    rng = _RecordingRandom()
    ctx = ActivityResolutionContext(
        rng=rng,
        caster=hero,
        targets=[foe],
        event_emitter=events.append,
        caster_abilities={"str": 16, "dex": 10},
        caster_proficiency_bonus=2,
        attacker_conditions=["poisoned"] if poisoned else [],
        target_conditions={foe.entity_id: conditions},
        target_distance_ft={} if distance is None else {foe.entity_id: distance},
        passive_attack_bonus={hero.entity_id: "1d4"},
        variables={} if forced is None else {"force_d20": forced},
    )
    resolve_activity(activity, ctx, weapon=weapon)
    return events, rng


def _assert_swing(
    events: list[CombatEvent],
    rng: _RecordingRandom,
    *,
    mode: str,
    adv: bool,
    dis: bool,
    crit: bool,
    forced: int | None = None,
    hit: bool = True,
    poisoned: bool = False,
) -> None:
    mirror = _RecordingRandom()
    if forced is None:
        first = mirror.randint(1, 20)
        if mode == "normal":
            natural = first
        else:
            second = mirror.randint(1, 20)
            natural = max(first, second) if mode == "advantage" else min(first, second)
    else:
        natural = forced
    attack = events[0]
    assert isinstance(attack, AttackRolled)
    assert attack.natural == natural
    assert attack.modifier == 5
    assert attack.roll_total == natural + 5 + mirror.randint(1, 4)
    assert attack.advantage == mode
    assert attack.advantage_sources == (["condition:target"] if adv else [])
    assert attack.disadvantage_sources == (
        (["condition:attacker"] if poisoned else []) + (["condition:target"] if dis else [])
    )
    assert attack.sources == attack.advantage_sources + attack.disadvantage_sources
    assert (attack.is_hit, attack.is_crit) == (hit, crit)
    if hit:
        assert [event.type for event in events] == [
            "attack_rolled",
            "damage_applied",
            "damage_applied",
        ]
        piercing, fire = events[1:]
        assert isinstance(piercing, DamageApplied)
        assert isinstance(fire, DamageApplied)
        # Base formula +4, governing ability +3, critical bonus +7: each once.
        assert piercing.amount == sum(mirror.randint(1, 6) for _ in range(2 if crit else 1)) + (
            4 + 3 + (7 if crit else 0)
        )
        assert fire.amount == sum(mirror.randint(1, 4) for _ in range(2 if crit else 1)) + 2
        assert (piercing.damage_type, fire.damage_type) == ("piercing", "fire")
        assert piercing.is_crit is crit
        assert fire.is_crit is crit
    else:
        assert len(events) == 1
    assert rng.draws == mirror.draws
    assert rng.getstate() == mirror.getstate()


def test_generic_projection_preserves_order_values_and_ignores_qualifier() -> None:
    effects = [ConditionEffect(kind=K.ADVANTAGE_ATTACKS_AGAINST)] + [
        ConditionEffect(kind=kind, value=5, qualifier="arbitrary explanation")
        for kind, _ in _SCOPED
    ]
    before = [effect.model_dump() for effect in effects]
    expected = [ActiveEffectChange(key="flags.advantage.attack", mode="override", value=True)] + [
        ActiveEffectChange(key=key, mode="override", value=5) for _, key in _SCOPED
    ]
    assert project_condition_effects(iter(effects)) == expected
    assert (
        project_condition_effects(
            effect.model_copy(update={"qualifier": "otherwise within beyond 999 feet"})
            for effect in effects
        )
        == expected
    )
    assert all(type(change.value) is int for change in expected[1:])
    assert [effect.model_dump() for effect in effects] == before


@pytest.mark.parametrize(("kind", "key"), _SCOPED)
@pytest.mark.parametrize("value", [True, False, "5"])
def test_invalid_scalar_clauses_do_not_become_thresholds(kind: K, key: str, value: object) -> None:
    clause = ConditionEffect(kind=kind, value=5).model_copy(update={"value": value})
    assert project_condition_effects([clause]) == []


@pytest.mark.parametrize("kind", [K.DISADVANTAGE_ATTACKS_AGAINST, K.AUTO_CRIT_WITHIN_5FT])
def test_missing_threshold_does_not_become_an_unconditional_flag(kind: K) -> None:
    assert project_condition_effects([ConditionEffect(kind=kind)]) == []


@pytest.mark.parametrize(("kind", "key"), _SCOPED)
@pytest.mark.parametrize(
    "invalid",
    [{"value": True}, {"value": False}, {"value": "5"}, {"mode": "add"}, {"key": "other"}],
)
def test_scoped_consumers_reject_wrong_key_mode_and_scalar_type(
    kind: K, key: str, invalid: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    flag = ActiveEffectChange(key=key, mode="override", value=5)
    distance = 6 if kind == K.DISADVANTAGE_ATTACKS_AGAINST else 5
    assert attack_distance_flag_applies([flag, flag], kind, distance)
    assert not attack_distance_flag_applies([flag], kind, None)
    assert not attack_distance_flag_applies([flag], K.SPEED_ZERO, distance)
    malformed = flag.model_copy(update=invalid)
    assert not attack_distance_flag_applies([malformed], kind, distance)
    monkeypatch.setattr(
        rules, "_project_condition_changes", lambda names: [malformed] if names else []
    )
    assert rules.conditions_grant_advantage_on_attack([], ["fixture"], distance_ft=distance) == (
        False,
        False,
    )
    assert not rules.conditions_auto_crit_within_5ft(["fixture"], distance_ft=distance)


@pytest.mark.parametrize(
    ("condition", "distance", "mode", "adv", "dis", "crit"),
    [
        ("prone", None, "normal", False, False, False),
        ("prone", 0, "advantage", True, False, False),
        ("prone", 5, "advantage", True, False, False),
        ("prone", 6, "disadvantage", False, True, False),
        ("prone", 30, "disadvantage", False, True, False),
        ("paralyzed", None, "advantage", True, False, False),
        ("paralyzed", 5, "advantage", True, False, True),
        ("paralyzed", 6, "advantage", True, False, False),
        ("unconscious", None, "advantage", True, False, False),
        ("unconscious", 0, "advantage", True, False, True),
        ("unconscious", 5, "advantage", True, False, True),
        ("unconscious", 6, "normal", True, True, False),
        ("unconscious", 30, "normal", True, True, False),
    ],
)
def test_distance_matrix_hit_damage_sources_draw_order_and_determinism(
    condition: str, distance: int | None, mode: str, adv: bool, dis: bool, crit: bool
) -> None:
    events, rng = _swing([condition], distance)
    _assert_swing(events, rng, mode=mode, adv=adv, dis=dis, crit=crit)
    repeated_events, repeated_rng = _swing([condition], distance)
    assert repeated_events == events
    assert repeated_rng.draws == rng.draws
    assert repeated_rng.getstate() == rng.getstate()


@pytest.mark.parametrize("condition", ["paralyzed", "unconscious"])
@pytest.mark.parametrize("forced", [None, 1, 20])
def test_nearby_auto_crit_never_overrides_miss_and_natural_twenty_still_crits(
    condition: str, forced: int | None
) -> None:
    events, rng = _swing([condition], 5, ac=100, forced=forced)
    _assert_swing(
        events,
        rng,
        mode="advantage",
        adv=True,
        dis=False,
        crit=forced == 20,
        hit=forced == 20,
        forced=forced,
    )


def test_natural_twenty_crits_without_any_condition_or_distance() -> None:
    events, rng = _swing([], None, ac=100, forced=20)
    _assert_swing(events, rng, mode="normal", adv=False, dis=False, crit=True, forced=20)


@pytest.mark.parametrize("distance", [5, 6])
def test_poisoned_attacker_cancellation_still_consumes_one_d20(distance: int) -> None:
    events, rng = _swing(["paralyzed"], distance, poisoned=True)
    _assert_swing(
        events, rng, mode="normal", adv=True, dis=False, crit=distance == 5, poisoned=True
    )


@pytest.mark.parametrize(
    "conditions",
    [
        ["paralyzed", "unconscious"],
        ["PARALYZED", "paralyzed"],
        ["UNCONSCIOUS", "unconscious"],
        ["unconscious", "paralyzed", "PRONE", "prone"],
    ],
)
def test_multiple_auto_crit_conditions_match_single_condition_events_and_rng(
    conditions: list[str],
) -> None:
    events, rng = _swing(conditions, 5)
    expected, baseline_rng = _swing(["paralyzed"], 5)
    assert events == expected
    assert rng.draws == baseline_rng.draws
    assert rng.getstate() == baseline_rng.getstate()
    _assert_swing(events, rng, mode="advantage", adv=True, dis=False, crit=True)


def _remove_clause(slug: str, kind: K, source: str, monkeypatch: pytest.MonkeyPatch) -> None:
    if source == "allowlist":
        monkeypatch.setitem(
            rules._DECLARATIVE_CONDITION_MIGRATIONS,
            slug,
            rules._DECLARATIVE_CONDITION_MIGRATIONS[slug] - {kind},
        )
    else:
        monkeypatch.setitem(
            rules._DECLARATIVE_CONDITION_EFFECTS,
            slug,
            tuple(
                effect
                for effect in rules._DECLARATIVE_CONDITION_EFFECTS[slug]
                if effect.kind != kind
            ),
        )


@pytest.mark.parametrize("kind", [K.ADVANTAGE_ATTACKS_AGAINST, K.DISADVANTAGE_ATTACKS_AGAINST])
@pytest.mark.parametrize("source", ["allowlist", "canonical"])
def test_prone_distance_clauses_are_independent_of_each_other_and_own_disadvantage(
    kind: K, source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _remove_clause("prone", kind, source, monkeypatch)
    for distance in (5, 6):
        adv = distance == 5 and kind != K.ADVANTAGE_ATTACKS_AGAINST
        dis = distance == 6 and kind != K.DISADVANTAGE_ATTACKS_AGAINST
        events, rng = _swing(["prone"], distance)
        _assert_swing(
            events,
            rng,
            mode="advantage" if adv else "disadvantage" if dis else "normal",
            adv=adv,
            dis=dis,
            crit=False,
        )
    assert rules.conditions_grant_advantage_on_attack(["prone"], [], distance_ft=5) == (False, True)


@pytest.mark.parametrize("slug", ["paralyzed", "unconscious"])
@pytest.mark.parametrize("source", ["allowlist", "canonical"])
def test_auto_crit_removal_preserves_advantage_saves_speed_actions_and_implications(
    slug: str, source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    own = rules.conditions_grant_advantage_on_attack([slug], [])
    changes = rules._project_condition_changes([slug])
    saves = rules.project_passive_save_modifiers([slug])
    _remove_clause(slug, K.AUTO_CRIT_WITHIN_5FT, source, monkeypatch)
    assert rules._project_condition_changes([slug]) == [
        change for change in changes if change.key != "flags.auto_crit.attack.within_ft"
    ]
    assert not rules.conditions_auto_crit_within_5ft([slug], distance_ft=5)
    assert rules.conditions_grant_advantage_on_attack([slug], []) == own
    assert rules.project_passive_save_modifiers([slug]) == saves
    assert rules.project_speed(30, [slug]) == 0
    assert rules.conditions_block_actions([slug])
    events, rng = _swing([slug], 5)
    _assert_swing(events, rng, mode="advantage", adv=True, dis=False, crit=False)


@pytest.mark.parametrize("slug", ["poisoned", "grappled"])
@pytest.mark.parametrize(("kind", "key"), _SCOPED)
def test_generic_scoped_support_does_not_opt_in_a_condition(
    slug: str, kind: K, key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clause = ConditionEffect(kind=kind, value=5)
    assert project_condition_effects([clause]) == [
        ActiveEffectChange(key=key, mode="override", value=5)
    ]
    before = rules._project_condition_changes([slug])
    monkeypatch.setitem(
        rules._DECLARATIVE_CONDITION_EFFECTS,
        slug,
        (*rules._DECLARATIVE_CONDITION_EFFECTS[slug], clause),
    )
    assert rules._project_condition_changes([slug]) == before
    for distance in (5, 6):
        events, rng = _swing([slug], distance)
        _assert_swing(events, rng, mode="normal", adv=False, dis=False, crit=False)


@pytest.mark.parametrize("slug", ["prone", "paralyzed", "unconscious"])
def test_runtime_threshold_comes_from_value_and_never_qualifier(
    slug: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        rules._DECLARATIVE_CONDITION_EFFECTS,
        slug,
        tuple(
            effect.model_copy(update={"value": 10, "qualifier": "arbitrary prose, 1 foot"})
            if effect.kind in {kind for kind, _ in _SCOPED} and effect.value is not None
            else effect
            for effect in rules._DECLARATIVE_CONDITION_EFFECTS[slug]
        ),
    )
    for distance in (10, 11):
        # Unconscious's independently declared implied Prone threshold stays 5.
        adv = slug != "prone" or distance == 10
        dis = slug == "unconscious" or (slug == "prone" and distance == 11)
        events, rng = _swing([slug], distance)
        _assert_swing(
            events,
            rng,
            mode="normal" if adv and dis else "advantage" if adv else "disadvantage",
            adv=adv,
            dis=dis,
            crit=slug != "prone" and distance == 10,
        )


@pytest.mark.parametrize(
    ("condition", "distance", "threshold", "mode", "crit"),
    [
        ("prone", 5, 5, "advantage", False),
        ("prone", 10, 5, "disadvantage", False),
        ("prone", 10, 10, "advantage", False),
        ("paralyzed", 5, 5, "advantage", True),
        ("paralyzed", 10, 5, "advantage", False),
        ("paralyzed", 10, 10, "advantage", True),
        ("unconscious", 5, 5, "advantage", True),
        ("unconscious", 10, 5, "normal", False),
        ("unconscious", 10, 10, "normal", True),
    ],
)
def test_player_intent_cleave_consumes_candidate_distance_conditions_and_exact_rng(
    condition: str,
    distance: int,
    threshold: int,
    mode: str,
    crit: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        rules._DECLARATIVE_CONDITION_EFFECTS,
        condition,
        tuple(
            effect.model_copy(update={"value": threshold}) if type(effect.value) is int else effect
            for effect in rules._DECLARATIVE_CONDITION_EFFECTS[condition]
        ),
    )
    start = run_async(
        start_combat(
            session_id="distance-cleave",
            party=[
                PartyMemberSpec(
                    entity_id="char:hero",
                    name="Hero",
                    initiative=20,
                    hp_current=20,
                    hp_max=20,
                    strength=16,
                    class_slug="fighter",
                    character_level=5,
                    zone_id=cell(0, 0),
                )
            ],
            encounter=[
                EncounterMemberSpec(
                    entity_id=eid,
                    entity_type="Monster",
                    name=eid,
                    initiative=initiative,
                    hp_current=500,
                    hp_max=500,
                    ac=1,
                    zone_id=zone,
                )
                for eid, initiative, zone in [
                    ("mon:primary", 10, cell(0, 1)),
                    ("mon:candidate", 9, cell(1, 1) if distance == 5 else cell(0, 2)),
                ]
            ],
            grid_scene=grid_scene(),
            rng_seed=7,
            active_effects=(
                ActiveEffect(
                    id="effect:candidate",
                    name="Candidate condition",
                    origin="test",
                    target_id="mon:candidate",
                    statuses={condition},
                ),
            ),
        )
    )
    live = _get_live(start.handle)
    rng = _RecordingRandom()
    rng.setstate(live.rng.getstate())
    live.rng = rng
    mirror = _RecordingRandom()
    mirror.setstate(rng.getstate())
    offset = len(live.event_log)
    run_async(
        submit_player_intent(
            start.handle,
            actor_id="char:hero",
            intent=PlayerIntent(intent_type="attack", weapon_id="halberd", target_id="mon:primary"),
        )
    )
    events = live.event_log[offset:]
    assert [event.type for event in events] == [
        "intent_submitted",
        "attack_rolled",
        "damage_applied",
        "attack_rolled",
        "damage_applied",
    ]
    _, primary, primary_damage, chain, chain_damage = events
    assert isinstance(primary, AttackRolled)
    assert isinstance(primary_damage, DamageApplied)
    assert isinstance(chain, AttackRolled)
    assert isinstance(chain_damage, DamageApplied)
    assert primary.natural == mirror.randint(1, 20)
    assert primary.is_hit
    assert not primary.is_crit
    assert primary_damage.amount == mirror.randint(1, 10) + 3
    first = mirror.randint(1, 20)
    if mode == "normal":
        natural = first
    else:
        second = mirror.randint(1, 20)
        natural = max(first, second) if mode == "advantage" else min(first, second)
    assert chain.target_id == "mon:candidate"
    assert chain.natural == natural
    assert chain.roll_total == natural + chain.modifier
    assert chain.advantage == mode
    assert chain.is_hit
    assert chain.is_crit is crit
    assert chain.advantage_sources == (
        ["condition:target"] if condition != "prone" or distance <= threshold else []
    )
    assert chain.disadvantage_sources == (
        ["condition:target"]
        if (condition == "unconscious" and distance > 5)
        or (condition == "prone" and distance > threshold)
        else []
    )
    assert chain_damage.source_id == "mastery:cleave"
    assert chain_damage.is_crit is crit
    assert chain_damage.amount == sum(mirror.randint(1, 10) for _ in range(2 if crit else 1))
    assert rng.draws == mirror.draws
    assert rng.getstate() == mirror.getstate()
