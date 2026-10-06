"""Source-relative clauses: pure vocabulary, strict flags and isolated opt-ins."""

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
CLAUSES = [
    ("charmed", K.CANT_ATTACK_CHARMER, None, "targeting.cannot_attack_charmer"),
    (
        "frightened",
        K.DISADVANTAGE_ABILITY_CHECKS,
        G.FEAR_SOURCE_IN_SIGHT,
        "flags.disadvantage.check.gate.fear_source_in_sight",
    ),
    (
        "frightened",
        K.CANT_MOVE_TOWARD_FEAR_SOURCE,
        None,
        "movement.cannot_move_toward_fear_source",
    ),
    (
        "invisible",
        K.UNSEEN,
        G.OBSERVER_CANNOT_SEE_BEARER,
        "visibility.unseen.gate.observer_cannot_see_bearer",
    ),
]


def _consume(conditions):
    return (
        rules.conditions_cannot_attack_charmer(conditions),
        rules.conditions_grant_disadvantage_on_ability_checks(conditions),
        rules.conditions_cannot_move_toward_fear_source(conditions),
        rules.conditions_unseen(conditions, observer_can_see_bearer=False),
    )


@pytest.mark.parametrize("slug,kind,gate,key", CLAUSES)
@pytest.mark.parametrize("prose", ["", "source unseen", "can see; 999 feet; different charmer"])
def test_projection_and_consumers_ignore_prose_and_do_not_stack(
    slug, kind, gate, key, prose, monkeypatch
):
    clause = ConditionEffect(kind=kind, gate=gate, qualifier=prose)
    before = clause.model_dump()
    expected = ActiveEffectChange(key=key, mode="override", value=True)
    assert project_condition_effects(iter([clause, clause])) == [expected, expected]
    monkeypatch.setitem(rules._DECLARATIVE_CONDITION_EFFECTS, slug, (clause, clause))
    assert rules._project_condition_changes([slug, slug.upper()]) == [expected, expected]
    assert _consume([slug, slug.upper()]) == tuple(k == kind for _, k, _, _ in CLAUSES)
    assert clause.model_dump() == before


@pytest.mark.parametrize("slug,kind,gate,key", CLAUSES)
@pytest.mark.parametrize(
    "update", [{"value": 5}, {"value": True}, {"value": "5"}, {"gate": "unknown"}]
)
def test_malformed_projection_stays_inert(slug, kind, gate, key, update):
    clause = ConditionEffect(kind=kind, gate=gate).model_copy(update=update)
    assert project_condition_effects([clause]) == []


@pytest.mark.parametrize("kind", [K.CANT_ATTACK_CHARMER, K.CANT_MOVE_TOWARD_FEAR_SOURCE])
@pytest.mark.parametrize("gate", list(G))
def test_source_restrictions_do_not_accept_visibility_gates(kind, gate):
    assert project_condition_effects([ConditionEffect(kind=kind, gate=gate)]) == []


@pytest.mark.parametrize("gate", [None, G.FEAR_SOURCE_IN_SIGHT, "unknown"])
def test_unseen_requires_its_exact_gate(gate):
    clause = ConditionEffect(kind=K.UNSEEN).model_copy(update={"gate": gate})
    assert project_condition_effects([clause]) == []


def test_ability_check_gate_remains_distinct_from_ungated_poisoned():
    ungated = ConditionEffect(kind=K.DISADVANTAGE_ABILITY_CHECKS)
    gated = ungated.model_copy(update={"gate": G.FEAR_SOURCE_IN_SIGHT})
    assert [c.key for c in project_condition_effects([ungated, gated])] == [
        "flags.disadvantage.check",
        "flags.disadvantage.check.gate.fear_source_in_sight",
    ]
    assert (
        project_condition_effects([gated.model_copy(update={"gate": G.OBSERVER_CANNOT_SEE_BEARER})])
        == []
    )


@pytest.mark.parametrize("slug,kind,gate,key", CLAUSES)
@pytest.mark.parametrize(
    "update",
    [{"key": "wrong.key"}, {"mode": "add"}, {"value": False}, {"value": 1}, {"value": "true"}],
)
def test_all_consumers_require_exact_boolean_override(slug, kind, gate, key, update, monkeypatch):
    change = ActiveEffectChange(key=key, mode="override", value=True).model_copy(update=update)
    monkeypatch.setattr(rules, "_project_condition_changes", lambda conditions: [change])
    assert _consume([slug]) == (False, False, False, False)
    assert rules.project_passive_check_modifiers([slug])["passive_check_dis"] == []


@pytest.mark.parametrize(
    "update", [{"key": "wrong"}, {"mode": "add"}, {"value": 1}, {"value": "true"}]
)
def test_ungated_check_also_requires_exact_boolean_override(update, monkeypatch):
    change = ActiveEffectChange(key="flags.disadvantage.check", mode="override", value=True)
    monkeypatch.setattr(
        rules, "_project_condition_changes", lambda conditions: [change.model_copy(update=update)]
    )
    assert rules.conditions_grant_disadvantage_on_ability_checks(["poisoned"]) is False


@pytest.mark.parametrize("slug,kind,gate,key", CLAUSES)
@pytest.mark.parametrize("remove_from", ["allowlist", "canonical"])
def test_removal_disables_only_the_selected_mechanic(
    slug, kind, gate, key, remove_from, monkeypatch
):
    before = rules._project_condition_changes([slug])
    flags = _consume([slug])
    attacks = rules.conditions_grant_advantage_on_attack([slug], [slug])
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
    assert _consume([slug]) == tuple(
        False if k == kind else flag for (_, k, _, _), flag in zip(CLAUSES, flags, strict=True)
    )
    assert rules.conditions_grant_advantage_on_attack([slug], [slug]) == attacks


@pytest.mark.parametrize("slug,kind,gate,key", CLAUSES)
def test_generic_vocabulary_does_not_opt_in_a_condition(slug, kind, gate, key, monkeypatch):
    clause = ConditionEffect(kind=kind, gate=gate)
    assert project_condition_effects([clause])
    for other in ("deafened", "restrained", "blinded"):
        monkeypatch.setitem(rules._DECLARATIVE_CONDITION_EFFECTS, other, (clause,))
        assert rules._project_condition_changes([other]) == []
        assert _consume([other]) == (False, False, False, False)


@pytest.mark.parametrize(
    "conditions,visible,expected",
    [
        (["poisoned"], False, True),
        (["poisoned"], True, True),
        (["frightened"], True, True),
        (["frightened"], False, False),
        (["poisoned", "frightened"], False, True),
    ],
)
def test_check_context_gates_only_the_frightened_clause(conditions, visible, expected):
    assert (
        rules.conditions_grant_disadvantage_on_ability_checks(
            conditions, fear_source_in_sight=visible
        )
        is expected
    )
    assert rules.project_passive_check_modifiers(conditions, fear_source_in_sight=visible) == {
        "passive_check_adv": [],
        "passive_check_dis": ["all"] if expected else [],
    }


def test_new_consumers_use_projection_without_condition_name_fallback(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("condition name cannot authorize a migrated mechanic")

    monkeypatch.setattr(rules, "is_condition_active", forbidden)
    assert _consume(["charmed", "frightened", "invisible"]) == (True, True, True, True)
    assert rules.conditions_unseen(["invisible"], observer_can_see_bearer=True) is False


def test_unrelated_clauses_remain_outside_all_opt_ins():
    excluded = {
        K.CHARMER_SOCIAL_ADVANTAGE,
        K.ADVANTAGE_INITIATIVE,
        K.CANNOT_SPEAK,
        K.AUTO_FAIL_SIGHT_CHECKS,
        K.AUTO_FAIL_HEARING_CHECKS,
        K.MOVABLE_BY_GRAPPLER,
        K.RESTRICTED_MOVEMENT_CRAWL,
        K.DROPS_HELD_ITEMS,
        K.DEATH_AT_LEVEL,
    }
    assert all(not (excluded & kinds) for kinds in rules._DECLARATIVE_CONDITION_MIGRATIONS.values())
