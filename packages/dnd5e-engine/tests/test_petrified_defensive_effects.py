"""Petrified defenses derive from independent canonical clauses and typed scopes."""

from __future__ import annotations

import pytest
from dnd5e_srd_data.schema.condition import (
    ConditionEffect,
    ConditionEffectGate,
    ConditionEffectKind,
)

from dnd5e_engine.activities.effects import is_condition_immune
from dnd5e_engine.rules import conditions as rules
from dnd5e_engine.rules.effects import condition_immunity_slugs, project_condition_effects
from dnd5e_engine.types.combat import Combatant
from dnd5e_engine.types.conditions import ActiveCondition
from dnd5e_engine.types.effects import ActiveEffectChange

K = ConditionEffectKind


def _target(names=("petrified",), immunities=()):
    return Combatant(
        entity_id="mon:target",
        entity_type="Monster",
        name="Target",
        initiative=1,
        hp_current=50,
        hp_max=50,
        condition_immunities=list(immunities),
        conditions=[
            ActiveCondition(condition=n, source_entity_id="implied:test", scope="combat")
            for n in names
        ],
    )


@pytest.mark.parametrize("qualifier", ["anything", "", "immune to fire damage", "poisoned"])
@pytest.mark.parametrize("kind", [K.RESIST_ALL_DAMAGE, K.IMMUNE_TO_CONDITION])
def test_defense_projection_uses_machine_scope_only(kind, qualifier):
    effect = ConditionEffect(kind=kind, condition_slugs=["poisoned"], qualifier=qualifier)
    before = effect.model_dump()
    expected = ActiveEffectChange(
        key="damage.resistance.all" if kind == K.RESIST_ALL_DAMAGE else "condition.immunity",
        mode="override",
        value=True if kind == K.RESIST_ALL_DAMAGE else "poisoned",
    )
    assert project_condition_effects(iter([effect, effect])) == [expected, expected]
    assert effect.model_dump() == before


def test_condition_scopes_preserve_declaration_order_and_consume_stable_unique():
    effects = [
        ConditionEffect(
            kind=K.IMMUNE_TO_CONDITION,
            condition_slugs=["poisoned", "Prone", " poisoned ", "", "prone"],
        ),
        ConditionEffect(kind=K.IMMUNE_TO_CONDITION, condition_slugs=["prone", "charmed"]),
    ]
    changes = project_condition_effects(effects)
    assert [c.value for c in changes] == ["poisoned", "prone", "prone", "charmed"]
    assert condition_immunity_slugs(changes) == ["poisoned", "prone", "charmed"]


def test_unscoped_immunity_never_parses_qualifier_or_other_fields():
    effect = ConditionEffect(
        kind=K.IMMUNE_TO_CONDITION, abilities=["poisoned"], value=5, qualifier="poisoned"
    )
    assert project_condition_effects([effect]) == []


@pytest.mark.parametrize("kind", [K.RESIST_ALL_DAMAGE, K.IMMUNE_TO_CONDITION])
@pytest.mark.parametrize("gate", list(ConditionEffectGate))
def test_unsupported_gated_defenses_are_inert(kind, gate):
    assert (
        project_condition_effects(
            [ConditionEffect(kind=kind, gate=gate, condition_slugs=["poisoned"])]
        )
        == []
    )


@pytest.mark.parametrize(
    "update",
    [
        {"key": "condition.immunity.poisoned"},
        {"mode": "add"},
        {"value": True},
        {"value": 1},
        {"value": ""},
    ],
)
def test_condition_immunity_consumer_requires_exact_string_override(update):
    change = ActiveEffectChange(key="condition.immunity", mode="override", value="poisoned")
    assert condition_immunity_slugs([change.model_copy(update=update)]) == []


@pytest.mark.parametrize(
    "update",
    [
        {"key": "damage.resistance"},
        {"mode": "add"},
        {"value": False},
        {"value": 1},
        {"value": "True"},
    ],
)
def test_damage_consumer_requires_exact_true_override(update, monkeypatch):
    change = ActiveEffectChange(key="damage.resistance.all", mode="override", value=True)
    monkeypatch.setattr(
        rules, "_project_condition_changes", lambda conditions: [change.model_copy(update=update)]
    )
    assert rules.project_passive_damage_modifiers(["petrified"]) == {
        "resistances": [],
        "vulnerabilities": [],
        "immunities": [],
    }


@pytest.mark.parametrize("remove_from", ["allowlist", "canonical"])
@pytest.mark.parametrize("kind", [K.RESIST_ALL_DAMAGE, K.IMMUNE_TO_CONDITION])
def test_defense_clause_isolation(kind, remove_from, monkeypatch):
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
    assert rules.project_passive_damage_modifiers(["petrified"]) == {
        "resistances": [] if kind == K.RESIST_ALL_DAMAGE else ["all"],
        "vulnerabilities": [],
        "immunities": [],
    }
    target = _target()
    assert is_condition_immune(target, "poisoned") is (kind != K.IMMUNE_TO_CONDITION)
    attached = rules.apply_condition_with_implies(
        rules.Condition.POISONED,
        "implied:test",
        "combat",
        target.conditions,
    )
    assert any(c.condition == "poisoned" for c in attached) is (kind == K.IMMUNE_TO_CONDITION)
    assert rules.project_speed(30, ["petrified"]) == 0
    assert rules.project_passive_save_modifiers(["petrified"])["passive_save_auto_fail"] == [
        "STR",
        "DEX",
    ]
    assert rules.conditions_grant_advantage_on_attack([], ["petrified"]) == (True, False)
    assert rules.conditions_block_actions(["petrified"])


@pytest.mark.parametrize("kind", [K.RESIST_ALL_DAMAGE, K.IMMUNE_TO_CONDITION])
def test_generic_defense_support_requires_explicit_opt_in(kind, monkeypatch):
    clause = ConditionEffect(kind=kind, condition_slugs=["poisoned"])
    assert project_condition_effects([clause])
    monkeypatch.setitem(rules._DECLARATIVE_CONDITION_EFFECTS, "deafened", (clause,))
    assert rules._project_condition_changes(["deafened"]) == []
    assert rules.project_passive_damage_modifiers(["deafened"])["resistances"] == []
    assert not is_condition_immune(_target(["deafened"]), "poisoned")
    monkeypatch.setitem(rules._DECLARATIVE_CONDITION_MIGRATIONS, "deafened", frozenset({kind}))
    assert rules._project_condition_changes(["deafened"]) == project_condition_effects([clause])
    assert bool(rules.project_passive_damage_modifiers(["deafened"])["resistances"]) is (
        kind == K.RESIST_ALL_DAMAGE
    )
    assert is_condition_immune(_target(["deafened"]), "poisoned") is (kind == K.IMMUNE_TO_CONDITION)


def test_canonical_scope_mutation_changes_authority_without_changing_prose(monkeypatch):
    original = rules._DECLARATIVE_CONDITION_EFFECTS["petrified"]
    monkeypatch.setitem(
        rules._DECLARATIVE_CONDITION_EFFECTS,
        "petrified",
        tuple(
            e.model_copy(update={"condition_slugs": ["prone"]})
            if e.kind == K.IMMUNE_TO_CONDITION
            else e
            for e in original
        ),
    )
    assert (
        next(
            e
            for e in rules._DECLARATIVE_CONDITION_EFFECTS["petrified"]
            if e.kind == K.IMMUNE_TO_CONDITION
        ).qualifier
        == "poisoned"
    )
    assert rules.project_condition_immunities(["petrified"]) == ["prone"]
    target = _target()
    assert not is_condition_immune(target, "poisoned")
    assert is_condition_immune(target, "prone")
    assert rules.apply_condition(rules.Condition.POISONED, ["petrified"]) == [
        "petrified",
        "poisoned",
    ]
    assert rules.apply_condition(rules.Condition.PRONE, ["petrified"]) == ["petrified"]


def test_static_and_projected_immunities_union_without_mutating_the_combatant():
    target = _target(["petrified", "PETRIFIED", "petrified"], ["charmed", "poisoned", "poisoned"])
    before = target.model_dump()
    assert rules.project_condition_immunities([c.condition for c in target.conditions]) == [
        "poisoned"
    ]
    assert is_condition_immune(target, "poisoned")
    assert is_condition_immune(target, "charmed")
    assert not is_condition_immune(target, "prone")
    assert rules.apply_condition(rules.Condition.POISONED, ["petrified"]) == ["petrified"]
    assert target.model_dump() == before
