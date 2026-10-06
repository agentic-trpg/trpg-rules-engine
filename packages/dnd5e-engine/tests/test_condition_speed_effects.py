"""Zero Speed comes from opted-in canonical clauses, with neighbouring rules intact."""

from __future__ import annotations

import pytest
from dnd5e_srd_data.schema.condition import ConditionEffect, ConditionEffectKind

from dnd5e_engine.rules import conditions as condition_rules
from dnd5e_engine.rules.effects import project_condition_effects
from dnd5e_engine.types.effects import ActiveEffectChange

_ZERO_SPEED_CONDITIONS = ("grappled", "restrained", "paralyzed", "petrified", "unconscious")


@pytest.mark.parametrize("condition", _ZERO_SPEED_CONDITIONS)
@pytest.mark.parametrize("base_speed", [30, 45])
@pytest.mark.parametrize("exhaustion_level", [0, 2])
def test_zero_speed_conditions_override_base_speed_and_exhaustion(
    condition: str, base_speed: int, exhaustion_level: int
) -> None:
    assert condition_rules.project_speed(base_speed, [condition], exhaustion_level) == 0


@pytest.mark.parametrize("conditions", [[], ["stunned"], ["prone"]])
@pytest.mark.parametrize(
    ("base_speed", "exhaustion_level", "expected"),
    [(30, 0, 30), (30, 2, 20), (45, 2, 35), (5, 2, 0)],
)
def test_unmigrated_speed_neighbours_keep_exhaustion_and_floor(
    conditions: list[str], base_speed: int, exhaustion_level: int, expected: int
) -> None:
    assert condition_rules.project_speed(base_speed, conditions, exhaustion_level) == expected
    if conditions == ["stunned"]:
        assert all(
            effect.kind != ConditionEffectKind.SPEED_ZERO
            for effect in condition_rules._DECLARATIVE_CONDITION_EFFECTS["stunned"]
        )


@pytest.mark.parametrize(
    "conditions",
    [
        *[[name.upper(), name.title(), name] for name in _ZERO_SPEED_CONDITIONS],
        ["grappled", "restrained"],
        ["paralyzed", "petrified", "unconscious"],
        list(_ZERO_SPEED_CONDITIONS),
    ],
)
def test_speed_projection_normalizes_duplicates_and_multiple_conditions(
    conditions: list[str],
) -> None:
    before = conditions.copy()
    assert condition_rules.project_speed(45, conditions, exhaustion_level=2) == 0
    assert conditions == before
    assert condition_rules._project_condition_changes(conditions) == (
        condition_rules._project_condition_changes(
            list(dict.fromkeys(c.lower() for c in conditions))
        )
    )


@pytest.mark.parametrize("condition", _ZERO_SPEED_CONDITIONS)
@pytest.mark.parametrize("remove_from", ["allowlist", "canonical"])
def test_speed_zero_requires_canonical_clause_and_opt_in_without_losing_neighbours(
    condition: str, remove_from: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    kind = ConditionEffectKind.SPEED_ZERO
    canonical = condition_rules._DECLARATIVE_CONDITION_EFFECTS[condition]
    clauses = [effect for effect in canonical if effect.kind == kind]
    assert len(clauses) == 1
    assert clauses[0].qualifier == ""
    assert clauses[0].value is None
    before_changes = condition_rules._project_condition_changes([condition])
    own_attack = condition_rules.conditions_grant_advantage_on_attack([condition], [])
    target_attack = condition_rules.conditions_grant_advantage_on_attack([], [condition])
    saves = condition_rules.project_passive_save_modifiers([condition])
    assert condition_rules.project_speed(45, [condition], exhaustion_level=2) == 0

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

    assert condition_rules.project_speed(45, [condition], exhaustion_level=2) == 35
    assert condition_rules._project_condition_changes([condition]) == [
        change for change in before_changes if change.key != "speed.override"
    ]
    assert condition_rules.conditions_grant_advantage_on_attack([condition], []) == own_attack
    assert condition_rules.conditions_grant_advantage_on_attack([], [condition]) == target_attack
    assert condition_rules.project_passive_save_modifiers([condition]) == saves
    if condition == "grappled":
        assert condition_rules._project_condition_changes([condition]) == [
            ActiveEffectChange(
                key="flags.disadvantage.attack.except_grappler", mode="override", value=True
            )
        ]
        assert condition_rules.conditions_grant_advantage_on_attack(
            [condition], [], grappler_id="char:grappler", target_id="char:grappler"
        ) == (False, False)
        assert condition_rules.conditions_grant_advantage_on_attack(
            [condition], [], grappler_id="char:grappler", target_id="mon:other"
        ) == (False, True)
        assert condition_rules.conditions_grant_advantage_on_attack([condition], []) == (
            False,
            False,
        )
    if condition == "unconscious":
        assert own_attack == (False, True)  # Implied Prone remains declarative.
        assert condition_rules.conditions_block_actions([condition]) is True


@pytest.mark.parametrize("condition", ["stunned", "prone", "poisoned", "deafened"])
def test_generic_speed_support_does_not_opt_in_an_unmigrated_clause(
    condition: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clause = ConditionEffect(kind=ConditionEffectKind.SPEED_ZERO)
    assert project_condition_effects([clause]) == [
        ActiveEffectChange(key="speed.override", mode="override", value=0)
    ]
    before = condition_rules._project_condition_changes([condition])
    monkeypatch.setitem(
        condition_rules._DECLARATIVE_CONDITION_EFFECTS,
        condition,
        (*condition_rules._DECLARATIVE_CONDITION_EFFECTS.get(condition, ()), clause),
    )
    assert condition_rules._project_condition_changes([condition]) == before
    assert condition_rules.project_speed(45, [condition], exhaustion_level=2) == 35


def test_scalar_projection_retains_clause_order_and_integer_zero() -> None:
    clauses = [
        ConditionEffect(kind=ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS),
        ConditionEffect(kind=ConditionEffectKind.SPEED_ZERO),
        ConditionEffect(kind=ConditionEffectKind.AUTO_FAIL_SAVE, abilities=["str", "dex"]),
    ]
    changes = project_condition_effects(iter(clauses))
    assert changes == [
        ActiveEffectChange(key="flags.disadvantage.attack", mode="override", value=True),
        ActiveEffectChange(key="speed.override", mode="override", value=0),
        ActiveEffectChange(key="flags.auto_fail.save.strength", mode="override", value=True),
        ActiveEffectChange(key="flags.auto_fail.save.dexterity", mode="override", value=True),
    ]
    assert type(changes[1].value) is int


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (ActiveEffectChange(key="speed.override", mode="override", value=0), 0),
        (ActiveEffectChange(key="speed.override", mode="add", value=0), 20),
        (ActiveEffectChange(key="speed.override", mode="override", value=False), 20),
        (ActiveEffectChange(key="speed.override", mode="override", value="0"), 20),
        (ActiveEffectChange(key="flags.auto_fail.save.strength", mode="override", value=True), 20),
    ],
)
def test_speed_consumer_accepts_the_zero_scalar_override(
    change: ActiveEffectChange, expected: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    exhaustion_changes = condition_rules._project_condition_changes(["exhaustion"])
    monkeypatch.setattr(
        condition_rules,
        "_project_condition_changes",
        lambda conditions: exhaustion_changes if conditions == ["exhaustion"] else [change],
    )
    assert condition_rules.project_speed(30, [], exhaustion_level=2) == expected
