"""Canonical multipliers and runtime Exhaustion levels have separate authority."""

from __future__ import annotations

import pytest
from dnd5e_srd_data.schema.condition import ConditionEffect, ConditionEffectKind

from dnd5e_engine.rules import conditions as rules
from dnd5e_engine.rules.effects import project_condition_effects, projected_scalar_value
from dnd5e_engine.types.conditions import ActiveCondition
from dnd5e_engine.types.effects import ActiveEffectChange

_NUMERIC = (
    (ConditionEffectKind.D20_TEST_PENALTY_PER_LEVEL, "d20_test.penalty_per_level"),
    (ConditionEffectKind.SPEED_PENALTY_PER_LEVEL, "speed.penalty_per_level"),
)


def _ac(level: int, name: str = "exhaustion") -> ActiveCondition:
    return ActiveCondition(
        condition=name, exhaustion_level=level, source_entity_id="implied:effect", scope="combat"
    )


@pytest.mark.parametrize(("kind", "key"), _NUMERIC)
@pytest.mark.parametrize("value", [0, 2, 3, 5, 7])
def test_generic_numeric_projection_copies_the_integer_without_reading_runtime_level(
    kind: ConditionEffectKind, key: str, value: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    clause = ConditionEffect(kind=kind, value=value, qualifier="arbitrary provenance")
    before = clause.model_dump()

    def forbid_level(*args: object) -> int:
        pytest.fail("Generic projection must not read a runtime level")

    monkeypatch.setattr(rules, "exhaustion_level_of", forbid_level)
    changes = project_condition_effects([clause])
    assert changes == [ActiveEffectChange(key=key, mode="override", value=value)]
    assert type(changes[0].value) is int
    assert (
        project_condition_effects([clause.model_copy(update={"qualifier": "999 feet otherwise"})])
        == changes
    )
    assert clause.model_dump() == before


def test_canonical_clause_order_and_death_boundary() -> None:
    clauses = rules._DECLARATIVE_CONDITION_EFFECTS["exhaustion"]
    assert [(clause.kind, clause.value) for clause in clauses] == [
        (ConditionEffectKind.D20_TEST_PENALTY_PER_LEVEL, 2),
        (ConditionEffectKind.SPEED_PENALTY_PER_LEVEL, 5),
        (ConditionEffectKind.DEATH_AT_LEVEL, 6),
    ]
    expected = [
        ActiveEffectChange(key="d20_test.penalty_per_level", mode="override", value=2),
        ActiveEffectChange(key="speed.penalty_per_level", mode="override", value=5),
    ]
    assert project_condition_effects(iter(clauses)) == expected
    assert rules._project_condition_changes(["EXHAUSTION", "exhaustion"]) == expected
    assert project_condition_effects([clauses[-1]]) == []
    assert (
        ConditionEffectKind.DEATH_AT_LEVEL
        not in rules._DECLARATIVE_CONDITION_MIGRATIONS["exhaustion"]
    )


@pytest.mark.parametrize(("kind", "key"), _NUMERIC)
@pytest.mark.parametrize("invalid", [None, True, False, "2"])
def test_projection_rejects_missing_or_noninteger_rule_values(
    kind: ConditionEffectKind, key: str, invalid: object
) -> None:
    clause = ConditionEffect(kind=kind, value=2).model_copy(update={"value": invalid})
    assert project_condition_effects([clause]) == []


@pytest.mark.parametrize("key", [key for _, key in _NUMERIC])
@pytest.mark.parametrize(
    "invalid",
    [{"value": True}, {"value": False}, {"value": "2"}, {"mode": "add"}, {"key": "other"}],
)
def test_scalar_and_runtime_consumers_reject_wrong_key_mode_and_value(
    key: str, invalid: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    flag = ActiveEffectChange(key=key, mode="override", value=2)
    malformed = flag.model_copy(update=invalid)
    assert projected_scalar_value([malformed], key) is None
    monkeypatch.setattr(rules, "_project_condition_changes", lambda names: [malformed])
    assert rules.d20_test_penalty([_ac(2)]) == 0
    assert rules.project_speed(30, ["exhaustion"], 2) == 30


@pytest.mark.parametrize("key", [key for _, key in _NUMERIC])
def test_scalar_consumer_uses_the_first_valid_override_without_stacking(key: str) -> None:
    first = ActiveEffectChange(key=key, mode="override", value=3)
    second = first.model_copy(update={"value": 7})
    assert projected_scalar_value([], key) is None
    assert projected_scalar_value([first, first], key) == 3
    assert (
        projected_scalar_value([first.model_copy(update={"value": True}), first, second], key) == 3
    )
    assert projected_scalar_value([second, first], key) == 7
    assert projected_scalar_value([first.model_copy(update={"value": 0})], key) == 0


@pytest.mark.parametrize(("level", "penalty"), [(0, 0), (1, -2), (3, -6), (5, -10)])
def test_runtime_d20_level_matrix(level: int, penalty: int) -> None:
    assert rules.d20_test_penalty([_ac(level)]) == penalty


def test_highest_runtime_level_and_duplicate_clauses_do_not_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert rules.exhaustion_level_of([]) == 0
    assert rules.exhaustion_level_of([_ac(5, "poisoned")]) == 0
    conditions = [_ac(1), _ac(3, "EXHAUSTION"), _ac(2), _ac(5, "poisoned")]
    assert rules.exhaustion_level_of(conditions) == 3
    clauses = rules._DECLARATIVE_CONDITION_EFFECTS["exhaustion"]
    monkeypatch.setitem(rules._DECLARATIVE_CONDITION_EFFECTS, "exhaustion", (*clauses, *clauses))
    assert rules.d20_test_penalty(conditions) == -6
    assert rules.project_speed(30, ["exhaustion", "EXHAUSTION"], 3) == 15


@pytest.mark.parametrize(
    ("base", "level", "speed"), [(30, 0, 30), (30, 1, 25), (30, 2, 20), (30, 6, 0), (5, 2, 0)]
)
@pytest.mark.parametrize("names", [[], ["exhaustion"]])
def test_speed_matrix_preserves_explicit_level_api_and_floor(
    base: int, level: int, speed: int, names: list[str]
) -> None:
    assert rules.project_speed(base, names, level) == speed


@pytest.mark.parametrize(
    "condition", ["grappled", "restrained", "paralyzed", "petrified", "unconscious"]
)
def test_zero_speed_override_precedes_a_mutated_exhaustion_multiplier(
    condition: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clauses = rules._DECLARATIVE_CONDITION_EFFECTS["exhaustion"]
    monkeypatch.setitem(
        rules._DECLARATIVE_CONDITION_EFFECTS,
        "exhaustion",
        tuple(
            clause.model_copy(update={"value": 7})
            if clause.kind == ConditionEffectKind.SPEED_PENALTY_PER_LEVEL
            else clause
            for clause in clauses
        ),
    )
    assert rules.project_speed(30, ["exhaustion"], 2) == 16
    assert rules.project_speed(30, ["exhaustion", condition], 2) == 0


@pytest.mark.parametrize(
    ("kind", "value", "penalty", "speed"),
    [
        (ConditionEffectKind.D20_TEST_PENALTY_PER_LEVEL, 3, -6, 20),
        (ConditionEffectKind.SPEED_PENALTY_PER_LEVEL, 7, -4, 16),
    ],
)
def test_canonical_multiplier_mutation_changes_only_its_mechanic(
    kind: ConditionEffectKind, value: int, penalty: int, speed: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    clauses = rules._DECLARATIVE_CONDITION_EFFECTS["exhaustion"]
    monkeypatch.setitem(
        rules._DECLARATIVE_CONDITION_EFFECTS,
        "exhaustion",
        tuple(
            clause.model_copy(update={"value": value, "qualifier": "arbitrary"})
            if clause.kind == kind
            else clause
            for clause in clauses
        ),
    )
    assert rules.d20_test_penalty([_ac(2)]) == penalty
    assert rules.project_speed(30, ["exhaustion"], 2) == speed


@pytest.mark.parametrize(("kind", "key"), _NUMERIC)
@pytest.mark.parametrize("source", ["allowlist", "canonical"])
def test_clause_removal_is_isolated_and_has_no_name_or_constant_fallback(
    kind: ConditionEffectKind, key: str, source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = rules._project_condition_changes(["exhaustion"])
    if source == "allowlist":
        monkeypatch.setitem(
            rules._DECLARATIVE_CONDITION_MIGRATIONS,
            "exhaustion",
            rules._DECLARATIVE_CONDITION_MIGRATIONS["exhaustion"] - {kind},
        )
    else:
        monkeypatch.setitem(
            rules._DECLARATIVE_CONDITION_EFFECTS,
            "exhaustion",
            tuple(
                clause
                for clause in rules._DECLARATIVE_CONDITION_EFFECTS["exhaustion"]
                if clause.kind != kind
            ),
        )
    assert rules._project_condition_changes(["exhaustion"]) == [
        change for change in before if change.key != key
    ]
    assert rules.d20_test_penalty([_ac(2)]) == (
        0 if kind == ConditionEffectKind.D20_TEST_PENALTY_PER_LEVEL else -4
    )
    assert rules.project_speed(30, ["exhaustion"], 2) == (
        30 if kind == ConditionEffectKind.SPEED_PENALTY_PER_LEVEL else 20
    )


@pytest.mark.parametrize("slug", ["poisoned", "stunned"])
@pytest.mark.parametrize(("kind", "key"), _NUMERIC)
def test_generic_numeric_support_does_not_enable_unopted_condition_clauses(
    slug: str, kind: ConditionEffectKind, key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clause = ConditionEffect(kind=kind, value=7)
    assert project_condition_effects([clause]) == [
        ActiveEffectChange(key=key, mode="override", value=7)
    ]
    before = rules._project_condition_changes([slug])
    monkeypatch.setitem(
        rules._DECLARATIVE_CONDITION_EFFECTS,
        slug,
        (*rules._DECLARATIVE_CONDITION_EFFECTS[slug], clause),
    )
    assert rules._project_condition_changes([slug]) == before
    assert rules.d20_test_penalty([_ac(2, slug)]) == 0
    assert rules.project_speed(30, [slug], 2) == 20


def test_exhaustion_never_projects_2014_check_disadvantage() -> None:
    assert rules.project_passive_check_modifiers(["exhaustion"]) == {
        "passive_check_adv": [],
        "passive_check_dis": [],
    }
