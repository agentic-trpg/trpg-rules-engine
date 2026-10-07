"""Typed context metadata, explicit opt-in, and independent attack clauses."""

from __future__ import annotations

import pytest
from dnd5e_srd_data.schema.condition import (
    ConditionEffect,
    ConditionEffectGate,
    ConditionEffectKind,
)

from dnd5e_engine.rules import conditions as rules
from dnd5e_engine.rules.effects import project_condition_effects
from dnd5e_engine.types.effects import ActiveEffectChange

K = ConditionEffectKind
G = ConditionEffectGate
_CLAUSES = [
    (
        "invisible",
        K.ADVANTAGE_OWN_ATTACKS,
        G.OBSERVER_CANNOT_SEE_BEARER,
        "flags.advantage.attack.gate.observer_cannot_see_bearer",
    ),
    (
        "invisible",
        K.DISADVANTAGE_ATTACKS_AGAINST,
        G.OBSERVER_CANNOT_SEE_BEARER,
        "flags.disadvantage.attack.gate.observer_cannot_see_bearer",
    ),
    (
        "frightened",
        K.DISADVANTAGE_OWN_ATTACKS,
        G.FEAR_SOURCE_IN_SIGHT,
        "flags.disadvantage.attack.gate.fear_source_in_sight",
    ),
    (
        "grappled",
        K.DISADVANTAGE_ATTACKS_EXCEPT_GRAPPLER,
        None,
        "flags.disadvantage.attack.except_grappler",
    ),
]


@pytest.mark.parametrize(("slug", "kind", "gate", "key"), _CLAUSES)
@pytest.mark.parametrize("qualifier", ["", "arbitrary prose", "not in line of sight; can see you"])
def test_projection_uses_typed_metadata_and_ignores_prose(slug, kind, gate, key, qualifier):
    clause = ConditionEffect(kind=kind, gate=gate, qualifier=qualifier)
    before = clause.model_dump()
    assert project_condition_effects([clause, clause]) == [
        ActiveEffectChange(key=key, mode="override", value=True),
        ActiveEffectChange(key=key, mode="override", value=True),
    ]
    assert clause.model_dump() == before


@pytest.mark.parametrize(
    "kind",
    [
        K.DISADVANTAGE_OWN_ATTACKS,
        K.DISADVANTAGE_ATTACKS_AGAINST,
        K.ADVANTAGE_OWN_ATTACKS,
        K.DISADVANTAGE_ATTACKS_EXCEPT_GRAPPLER,
        K.ADVANTAGE_ATTACKS_AGAINST,
        K.SPEED_ZERO,
    ],
)
def test_unknown_gate_never_falls_back_to_unconditional_or_distance(kind):
    clause = ConditionEffect(kind=kind, value=5).model_copy(update={"gate": "unknown"})
    assert project_condition_effects([clause]) == []


@pytest.mark.parametrize(
    ("kind", "gate"),
    [
        (K.DISADVANTAGE_OWN_ATTACKS, G.OBSERVER_CANNOT_SEE_BEARER),
        (K.ADVANTAGE_OWN_ATTACKS, G.FEAR_SOURCE_IN_SIGHT),
        (K.DISADVANTAGE_ATTACKS_AGAINST, G.FEAR_SOURCE_IN_SIGHT),
        (K.DISADVANTAGE_ATTACKS_EXCEPT_GRAPPLER, G.FEAR_SOURCE_IN_SIGHT),
        (K.DISADVANTAGE_ABILITY_CHECKS, G.OBSERVER_CANNOT_SEE_BEARER),
        (K.UNSEEN, G.FEAR_SOURCE_IN_SIGHT),
    ],
)
def test_unsupported_kind_gate_pairs_stay_inert(kind, gate):
    assert project_condition_effects([ConditionEffect(kind=kind, gate=gate)]) == []


@pytest.mark.parametrize(("slug", "kind", "gate", "key"), _CLAUSES[:3])
@pytest.mark.parametrize("value", [5, True, "5"])
def test_context_gate_cannot_be_combined_with_a_distance_value(slug, kind, gate, key, value):
    clause = ConditionEffect(kind=kind, gate=gate).model_copy(update={"value": value})
    assert project_condition_effects([clause]) == []


def test_gate_metadata_changes_projection_and_stable_order():
    effects = [ConditionEffect(kind=kind, gate=gate) for _, kind, gate, _ in _CLAUSES]
    assert [change.key for change in project_condition_effects(effects)] == [
        key for _, _, _, key in _CLAUSES
    ]
    assert project_condition_effects([effects[2].model_copy(update={"gate": None})]) == [
        ActiveEffectChange(key="flags.disadvantage.attack", mode="override", value=True)
    ]
    for clause in effects[:2]:
        assert project_condition_effects([clause.model_copy(update={"gate": None})]) == []


@pytest.mark.parametrize("slug", ["poisoned", "restrained", "blinded", "prone"])
def test_unconditional_disadvantage_is_preserved(slug):
    changes = rules._project_condition_changes([slug])
    assert (
        ActiveEffectChange(key="flags.disadvantage.attack", mode="override", value=True) in changes
    )


@pytest.mark.parametrize(("slug", "kind", "gate", "key"), _CLAUSES)
@pytest.mark.parametrize("remove_from", ["allowlist", "canonical"])
def test_removal_disables_only_one_clause(slug, kind, gate, key, remove_from, monkeypatch):
    before = rules._project_condition_changes([slug])
    checks = rules.project_passive_check_modifiers([slug])
    speed = rules.project_speed(30, [slug])
    if remove_from == "allowlist":
        monkeypatch.setitem(
            rules._DECLARATIVE_CONDITION_MIGRATIONS,
            slug,
            rules._DECLARATIVE_CONDITION_MIGRATIONS[slug] - {kind},
        )
    else:
        monkeypatch.setitem(
            rules._DECLARATIVE_CONDITION_EFFECTS,
            slug,
            tuple(c for c in rules._DECLARATIVE_CONDITION_EFFECTS[slug] if c.kind != kind),
        )
    assert rules._project_condition_changes([slug]) == [c for c in before if c.key != key]
    assert rules.project_passive_check_modifiers([slug]) == checks
    assert rules.project_speed(30, [slug]) == speed
    assert rules.conditions_grant_advantage_on_attack(["invisible"], []) == (
        not (slug == "invisible" and kind == K.ADVANTAGE_OWN_ATTACKS),
        False,
    )
    assert rules.conditions_grant_advantage_on_attack([], ["invisible"]) == (
        False,
        not (slug == "invisible" and kind == K.DISADVANTAGE_ATTACKS_AGAINST),
    )
    assert rules.conditions_grant_advantage_on_attack(["frightened"], []) == (
        False,
        slug != "frightened",
    )
    assert rules.conditions_grant_advantage_on_attack(
        ["grappled"], [], grappler_id="source", target_id="other"
    ) == (False, slug != "grappled")


@pytest.mark.parametrize(("slug", "kind", "gate", "key"), _CLAUSES[:2])
def test_canonical_visibility_gate_removal_has_no_name_or_prose_fallback(
    slug, kind, gate, key, monkeypatch
):
    canonical = rules._DECLARATIVE_CONDITION_EFFECTS[slug]
    monkeypatch.setitem(
        rules._DECLARATIVE_CONDITION_EFFECTS,
        slug,
        tuple(c.model_copy(update={"gate": None}) if c.kind == kind else c for c in canonical),
    )
    assert any(c.qualifier for c in rules._DECLARATIVE_CONDITION_EFFECTS[slug] if c.kind == kind)
    assert all(c.key != key for c in rules._project_condition_changes([slug]))
    assert rules.conditions_grant_advantage_on_attack([slug], [slug]) == (
        kind != K.ADVANTAGE_OWN_ATTACKS,
        kind != K.DISADVANTAGE_ATTACKS_AGAINST,
    )


@pytest.mark.parametrize(("slug", "kind", "gate", "key"), _CLAUSES)
def test_generic_support_does_not_opt_in_unrelated_condition(slug, kind, gate, key, monkeypatch):
    clause = ConditionEffect(kind=kind, gate=gate)
    assert project_condition_effects([clause])
    monkeypatch.setitem(rules._DECLARATIVE_CONDITION_EFFECTS, "deafened", (clause,))
    assert rules._project_condition_changes(["deafened"]) == []
    assert rules.conditions_grant_advantage_on_attack(
        ["deafened"], ["deafened"], grappler_id="source", target_id="other"
    ) == (False, False)
    monkeypatch.setitem(rules._DECLARATIVE_CONDITION_MIGRATIONS, "deafened", frozenset({kind}))
    assert rules._project_condition_changes(["deafened"]) == project_condition_effects([clause])


@pytest.mark.parametrize(("slug", "kind", "gate", "key"), _CLAUSES)
@pytest.mark.parametrize(
    "update",
    [
        {"mode": "add"},
        {"value": False},
        {"value": 1},
        {"value": "True"},
        {"key": "flags.disadvantage.attack.other"},
    ],
)
def test_context_consumer_rejects_non_boolean_overrides(slug, kind, gate, key, update, monkeypatch):
    change = ActiveEffectChange(key=key, mode="override", value=True).model_copy(update=update)
    monkeypatch.setattr(rules, "_project_condition_changes", lambda conditions: [change])
    assert rules.conditions_grant_advantage_on_attack(
        [slug], [slug], grappler_id="source", target_id="other"
    ) == (False, False)


def test_context_mechanics_do_not_call_condition_name_predicates(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("contextual attack mechanics must consume projected flags")

    monkeypatch.setattr(rules, "is_condition_active", forbidden)
    assert rules.conditions_grant_advantage_on_attack(
        ["INVISIBLE", "invisible", "FRIGHTENED", "Grappled", "grappled"],
        ["Invisible"],
        grappler_id="source",
        target_id="other",
    ) == (True, True)


def test_metadata_only_clauses_remain_outside_runtime_opt_in():
    # Initiative and social checks are explicitly opted in; movement remains metadata.
    assert K.ADVANTAGE_INITIATIVE in rules._DECLARATIVE_CONDITION_MIGRATIONS["invisible"]
    assert K.CHARMER_SOCIAL_ADVANTAGE in rules._DECLARATIVE_CONDITION_MIGRATIONS["charmed"]
    assert K.MOVABLE_BY_GRAPPLER not in rules._DECLARATIVE_CONDITION_MIGRATIONS["grappled"]
    assert rules.project_passive_check_modifiers(["frightened"]) == {
        "passive_check_adv": [],
        "passive_check_dis": ["all"],
    }
