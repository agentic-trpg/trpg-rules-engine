"""Canonical Initiative flags, attachment applicability and shared RNG contract."""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.condition import (
    ConditionEffect,
    ConditionEffectGate,
    ConditionEffectKind,
)

from dnd5e_engine import PlayerIntent
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.activities.effects import (
    applicable_condition_statuses,
    applicable_effect_statuses,
)
from dnd5e_engine.rules import conditions as rules
from dnd5e_engine.rules.effects import project_condition_effects
from dnd5e_engine.specs import EncounterMemberSpec, GridScene, PartyMemberSpec
from dnd5e_engine.types.effects import ActiveEffect, ActiveEffectChange
from tests.e2e.harness import run_async

K = ConditionEffectKind
HERO, ALLY, FOE, FOE2 = "char:z", "char:a", "mon:z", "mon:a"
INCAPACITATING = ("incapacitated", "paralyzed", "petrified", "stunned", "unconscious")
_BASE_RANDOM = random.Random
ADV_KEY = "flags.advantage.initiative"
DIS_KEY = "flags.disadvantage.initiative"


@pytest.mark.parametrize("remove_from", ["allowlist", "canonical"])
def test_invisible_initiative_clause_removal_preserves_attack_and_unseen(remove_from, monkeypatch):
    before = rules._project_condition_changes(["invisible"])
    if remove_from == "allowlist":
        monkeypatch.setitem(
            rules._DECLARATIVE_CONDITION_MIGRATIONS,
            "invisible",
            rules._DECLARATIVE_CONDITION_MIGRATIONS["invisible"] - {K.ADVANTAGE_INITIATIVE},
        )
    else:
        monkeypatch.setitem(
            rules._DECLARATIVE_CONDITION_EFFECTS,
            "invisible",
            tuple(
                c
                for c in rules._DECLARATIVE_CONDITION_EFFECTS["invisible"]
                if c.kind != K.ADVANTAGE_INITIATIVE
            ),
        )
    assert rules._project_condition_changes(["invisible"]) == [
        c for c in before if c.key != ADV_KEY
    ]
    assert not rules.conditions_advantage_initiative(["invisible"])
    assert rules.conditions_grant_advantage_on_attack(["invisible"], ["invisible"]) == (True, True)
    assert rules.conditions_unseen(["invisible"], observer_can_see_bearer=False)
    assert not rules.conditions_unseen(["invisible"], observer_can_see_bearer=True)


def test_generic_initiative_support_requires_explicit_condition_opt_in(monkeypatch):
    clause = ConditionEffect(kind=K.ADVANTAGE_INITIATIVE)
    assert project_condition_effects([clause]) == [
        ActiveEffectChange(key=ADV_KEY, mode="override", value=True)
    ]
    monkeypatch.setitem(rules._DECLARATIVE_CONDITION_EFFECTS, "charmed", (clause,))
    assert rules._project_condition_changes(["charmed"]) == []
    assert not rules.conditions_advantage_initiative(["charmed"])


@pytest.mark.parametrize(
    "kind,key,consumer",
    [
        (K.ADVANTAGE_INITIATIVE, ADV_KEY, rules.conditions_advantage_initiative),
        (K.DISADVANTAGE_INITIATIVE, DIS_KEY, rules.conditions_disadvantage_initiative),
    ],
)
@pytest.mark.parametrize(
    "update",
    [
        {"value": 1},
        {"value": "true"},
        {"value": False},
        {"value": None},
        {"key": "flags.advantage.initiative.other"},
        {"key": "flags.disadvantage.initiative.other"},
        {"mode": "custom"},
        {"mode": "add"},
        {"mode": "multiply"},
        {"mode": "upgrade"},
        {"mode": "downgrade"},
    ],
)
def test_initiative_consumer_accepts_only_exact_boolean_override(
    kind, key, consumer, update, monkeypatch
):
    change = ActiveEffectChange(key=key, mode="override", value=True).model_copy(update=update)
    monkeypatch.setattr(rules, "_project_condition_changes", lambda _: [change])
    assert not consumer(["invisible", "incapacitated"])


@pytest.mark.parametrize("kind", [K.ADVANTAGE_INITIATIVE, K.DISADVANTAGE_INITIATIVE])
@pytest.mark.parametrize("gate", list(ConditionEffectGate))
def test_initiative_does_not_support_visibility_or_other_gates(kind, gate):
    assert project_condition_effects([ConditionEffect(kind=kind, gate=gate)]) == []


def test_initiative_ignores_prose_and_never_falls_back_to_names(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Migrated Initiative cannot fall back to condition predicates")

    monkeypatch.setattr(rules, "is_condition_active", forbidden)
    for slug in ("invisible", "incapacitated"):
        monkeypatch.setitem(
            rules._DECLARATIVE_CONDITION_EFFECTS,
            slug,
            tuple(
                c.model_copy(update={"qualifier": "never roll initiative; observer can see you"})
                for c in rules._DECLARATIVE_CONDITION_EFFECTS[slug]
            ),
        )
    assert rules.conditions_advantage_initiative(["INVISIBLE", "invisible"])
    assert rules.conditions_disadvantage_initiative(["paralyzed"])
    monkeypatch.setattr(rules, "_project_condition_changes", lambda _: [])
    assert not rules.conditions_advantage_initiative(["invisible"])
    assert not rules.conditions_disadvantage_initiative(["incapacitated", "paralyzed"])


def _specs(*, target=HERO, fixed=False, surprised=False, immunities=()):
    party = [
        PartyMemberSpec(
            entity_id=entity,
            name=entity,
            initiative=None,
            dexterity=dex,
            hp_current=30,
            hp_max=30,
            zone_id=f"{index},0",
        )
        for index, (entity, dex) in enumerate(((HERO, 16), (ALLY, 12)))
    ]
    encounter = [
        EncounterMemberSpec(
            entity_id=entity,
            entity_type="Monster",
            name=entity,
            initiative=None,
            dexterity=dex,
            hp_current=30,
            hp_max=30,
            zone_id=f"{index + 2},0",
        )
        for index, (entity, dex) in enumerate(((FOE, 14), (FOE2, 10)))
    ]
    for spec in [*party, *encounter]:
        if spec.entity_id == target:
            spec.initiative = 17 if fixed else None
            spec.is_surprised = surprised
            spec.condition_immunities = list(immunities)
    return party, encounter


def _effect(statuses, *, target=HERO, index=0):
    return ActiveEffect(
        id=f"effect:seed-{index}",
        name="Seed",
        origin=f"test:seed-{index}",
        target_id=target,
        statuses=set(statuses),
    )


def _open(party, encounter, effects, *, seed=11):
    result = run_async(
        orch.start_combat(
            session_id="initiative-condition-contract",
            rng_seed=seed,
            grid_scene=GridScene(width=6, height=6),
            party=party,
            encounter=encounter,
            active_effects=effects,
        )
    )
    return result, orch._get_live(result.handle)


@pytest.fixture
def recorded_rng(monkeypatch):
    instances = []

    class RecordingRandom(_BASE_RANDOM):
        def __init__(self, seed):
            super().__init__(seed)
            self.draws = []
            instances.append(self)

        def randint(self, a, b):
            value = super().randint(a, b)
            self.draws.append((a, b, value))
            return value

    monkeypatch.setattr(orch.random, "Random", RecordingRandom)
    return instances


# Each case declares the two condition sides separately, before Surprise.
CASES = [
    ((), False, (), False, False),
    ((), True, (), False, False),
    (("invisible",), False, (), True, False),
    (("invisible",), True, (), True, False),
    (("invisible",), False, ("invisible",), False, False),
    (("invisible",), True, ("invisible",), False, False),
    (("invisible", "paralyzed"), False, ("paralyzed",), True, False),
    (("invisible", "paralyzed"), True, ("paralyzed",), True, False),
    (("invisible", "paralyzed"), False, ("invisible",), False, True),
    (("invisible", "paralyzed"), False, ("invisible", "paralyzed"), False, False),
    (("invisible", "incapacitated", "stunned", "paralyzed"), True, (), True, True),
] + [
    (statuses, surprised, immunities, advantage, disadvantage)
    for condition in INCAPACITATING
    for surprised in (False, True)
    for statuses, immunities, advantage, disadvantage in (
        ((condition,), (), False, True),
        (("invisible", condition), (), True, True),
        ((condition,), (condition,), False, False),
    )
]


@pytest.mark.parametrize("statuses,surprised,immunities,advantage,disadvantage", CASES)
@pytest.mark.parametrize("target", [HERO, FOE])
@pytest.mark.parametrize("fixed", [False, True])
def test_initiative_values_history_state_and_attachment_agree(
    statuses,
    surprised,
    immunities,
    advantage,
    disadvantage,
    target,
    fixed,
    recorded_rng,
):
    party, encounter = _specs(
        target=target, fixed=fixed, surprised=surprised, immunities=immunities
    )
    # Repeated statuses and effects still supply only presence, never extra dice.
    effects = [_effect(statuses * 2, target=target, index=i) for i in range(2)]
    before = [e.model_dump() for e in effects]
    spec_before = [s.model_dump() for s in [*party, *encounter]]
    sources = orch._seeded_initiative_sources(party, encounter, effects)[target]
    assert bool(sources.advantage) is advantage
    assert bool(sources.disadvantage) is disadvantage
    _result, live = _open(party, encounter, effects)
    mirror = _BASE_RANDOM(11)
    history, expected = [], {}
    for spec in [*party, *encounter]:
        if spec.initiative is not None:
            expected[spec.entity_id] = spec.initiative
            continue
        is_target = spec.entity_id == target
        has_adv = is_target and advantage
        has_dis = is_target and (disadvantage or surprised)
        dice = [mirror.randint(1, 20) for _ in range(2 if has_adv != has_dis else 1)]
        history.extend((1, 20, value) for value in dice)
        kept = (max(dice) if has_adv else min(dice)) if len(dice) == 2 else dice[0]
        expected[spec.entity_id] = kept + (spec.dexterity - 10) // 2
    assert {c.entity_id: c.initiative for c in live.initiative} == expected
    assert recorded_rng == [live.rng]
    assert live.rng.draws == history
    assert live.rng.getstate() == mirror.getstate()
    attached = [c.condition for c in orch._find_combatant(live, target).conditions]
    assert rules.conditions_advantage_initiative(attached) is advantage
    assert rules.conditions_disadvantage_initiative(attached) is disadvantage
    assert set(attached) == set(statuses) - set(immunities)
    for effect in effects:
        lineage = live.conditions_by_effect[(target, effect.id, effect.origin)]
        assert set(lineage) == set(attached)
    assert [e.model_dump() for e in effects] == before
    assert [s.model_dump() for s in [*party, *encounter]] == spec_before


@pytest.mark.parametrize("fixed", [False, True])
def test_foe_template_immunity_is_not_hydrated(fixed, recorded_rng):
    template = BundledAssetLoader().get_monster("ghost")
    assert template is not None
    assert "paralyzed" in template.condition_immunities
    party, encounter = _specs(target=FOE, fixed=fixed)
    encounter[0].monster_template_slug = "ghost"
    # Host DEX 14 is already authoritative; only host immunity participates.
    _result, live = _open(party, encounter, [_effect(["paralyzed"], target=FOE)])
    foe = orch._find_combatant(live, FOE)
    assert foe.condition_immunities == []
    assert [c.condition for c in foe.conditions] == ["paralyzed"]
    assert rules.conditions_disadvantage_initiative([c.condition for c in foe.conditions])
    mirror = _BASE_RANDOM(11)
    dice = [mirror.randint(1, 20) for _ in range(3 if fixed else 5)]
    assert foe.initiative == (17 if fixed else min(dice[2:4]) + 2)
    assert live.rng.draws == [(1, 20, value) for value in dice]
    assert live.rng.getstate() == mirror.getstate()


def test_pc_preview_reuses_existing_combat_build_immunity_seam(monkeypatch):
    existing = orch._pc_condition_immunities
    monkeypatch.setattr(orch, "_pc_condition_immunities", lambda pc: [*existing(pc), "invisible"])
    party, encounter = _specs()
    assert party[0].condition_immunities == []
    assert not orch._seeded_initiative_sources(party, encounter, [_effect(["invisible"])])[
        HERO
    ].advantage
    _result, live = _open(party, encounter, [_effect(["invisible"])])
    hero = orch._find_combatant(live, HERO)
    assert hero.condition_immunities == ["invisible"]
    assert hero.conditions == []
    mirror = _BASE_RANDOM(11)
    assert hero.initiative == mirror.randint(1, 20) + 3
    for _ in range(3):
        mirror.randint(1, 20)
    assert live.rng.getstate() == mirror.getstate()


def _grant_invisible_immunity(monkeypatch):
    clauses = rules._DECLARATIVE_CONDITION_EFFECTS["petrified"]
    monkeypatch.setitem(
        rules._DECLARATIVE_CONDITION_EFFECTS,
        "petrified",
        tuple(
            c.model_copy(update={"condition_slugs": ["invisible"]})
            if c.kind == K.IMMUNE_TO_CONDITION
            else c
            for c in clauses
        ),
    )


@pytest.mark.parametrize("immune_to_grant", [False, True])
@pytest.mark.parametrize("layout", ["same-set", "grant-first", "grant-last"])
def test_condition_derived_immunity_matches_attachment_and_preserves_effect_order(
    immune_to_grant,
    layout,
    monkeypatch,
    recorded_rng,
):
    _grant_invisible_immunity(monkeypatch)
    party, encounter = _specs(immunities=["petrified"] if immune_to_grant else [])
    grant, invisible = _effect(["petrified"]), _effect(["invisible"], index=1)
    effects = {
        "same-set": [_effect(["invisible", "petrified"])],
        "grant-first": [grant, invisible],
        "grant-last": [invisible, grant],
    }[layout]
    sources = orch._seeded_initiative_sources(party, encounter, effects)[HERO]
    expected_adv = immune_to_grant or layout == "grant-last"
    expected_dis = not immune_to_grant
    assert bool(sources.advantage) is expected_adv
    assert bool(sources.disadvantage) is expected_dis
    _result, live = _open(party, encounter, effects)
    attached = [c.condition for c in orch._find_combatant(live, HERO).conditions]
    assert rules.conditions_advantage_initiative(attached) is expected_adv
    assert rules.conditions_disadvantage_initiative(attached) is expected_dis
    mirror = _BASE_RANDOM(11)
    dice = [mirror.randint(1, 20) for _ in range(1 if expected_adv and expected_dis else 2)]
    kept = max(dice) if expected_adv else min(dice)
    assert orch._find_combatant(live, HERO).initiative == kept + 3
    for _ in range(3):
        mirror.randint(1, 20)
    assert live.rng.getstate() == mirror.getstate()
    rejected = {"petrified"} if immune_to_grant else ({"invisible"} if not expected_adv else set())
    assert not (rejected & set(attached))
    assert all(not (rejected & set(names)) for names in live.conditions_by_effect.values())


def test_applicability_is_pure_and_preserves_missing_target_policy():
    statuses = {"poisoned", "petrified", "invisible"}
    names, immunities = ["poisoned"], ["stunned"]
    assert applicable_condition_statuses(
        statuses, condition_immunities=immunities, condition_names=names
    ) == ["petrified", "invisible"]
    assert statuses == {"poisoned", "petrified", "invisible"}
    assert names == ["poisoned"]
    assert immunities == ["stunned"]
    # Unknown targets historically expose all sorted raw statuses without filtering.
    assert applicable_effect_statuses(None, statuses) == ["petrified", "invisible", "poisoned"]


def test_all_fixed_initiatives_keep_ties_and_consume_zero_rng(recorded_rng):
    party, encounter = _specs(surprised=True)
    for spec in [*party, *encounter]:
        spec.initiative, spec.dexterity, spec.is_surprised = 17, 16, True
    effects = [
        _effect(["invisible", *INCAPACITATING], target=spec.entity_id, index=index)
        for index, spec in enumerate([*party, *encounter])
    ]
    _result, live = _open(party, encounter, effects)
    assert [c.entity_id for c in live.initiative] == [ALLY, HERO, FOE2, FOE]
    assert recorded_rng == [live.rng]
    assert live.rng.draws == []
    assert live.rng.getstate() == _BASE_RANDOM(11).getstate()


def _replay_snapshot(reverse=False):
    party, encounter = _specs(target=FOE, surprised=True)
    party[0].initiative = 50
    effects = [
        _effect(["invisible", "stunned", "paralyzed"], target=FOE),
        _effect(["invisible"], target=FOE2, index=1),
        _effect(["invisible", "paralyzed"], target=FOE, index=2),
        _effect(["invisible"], target="unknown", index=3),
    ]
    if reverse:
        effects.reverse()
    result, live = _open(party, encounter, effects)
    run_async(
        orch.submit_player_intent(
            result.handle,
            actor_id=HERO,
            intent=PlayerIntent(
                intent_type="attack",
                target_id=FOE,
                weapon_id="longsword",
            ),
        )
    )
    return {
        "initiative": [
            c.model_copy(
                update={
                    "conditions": sorted(
                        c.conditions,
                        key=lambda ac: (ac.condition, ac.source_effect_id or ""),
                    )
                }
            ).model_dump(mode="json")
            for c in live.initiative
        ],
        "conditions": {k: sorted(v) for k, v in live.active_conditions.items()},
        "lineage": sorted(
            (identity, sorted(names)) for identity, names in live.conditions_by_effect.items()
        ),
        "hp": live.tracked_hp,
        "events": [e.model_dump(mode="json") for e in live.event_log],
        "rng": live.rng.getstate(),
    }


def test_same_seed_intents_and_unordered_effects_replay_identically():
    assert _replay_snapshot() == _replay_snapshot() == _replay_snapshot(reverse=True)


def test_initiative_and_subsequent_events_are_stable_across_python_hash_seeds():
    script = (
        "import json; from tests.test_initiative_condition_effects import _replay_snapshot; "
        "print(json.dumps(_replay_snapshot(reverse=True), sort_keys=True))"
    )
    runs = [
        json.loads(
            subprocess.run(
                [sys.executable, "-c", script],
                env={**os.environ, "PYTHONHASHSEED": str(seed)},
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )
        for seed in range(6)
    ]
    assert all(run == runs[0] for run in runs)
