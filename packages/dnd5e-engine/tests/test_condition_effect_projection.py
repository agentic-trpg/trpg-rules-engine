"""Typed condition clauses project without names, mutation, or dice draws."""

from __future__ import annotations

import random
from collections.abc import Iterable

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.condition import ConditionEffect, ConditionEffectKind

from dnd5e_engine.rules import conditions as condition_rules
from dnd5e_engine.rules.conditions import _project_condition_changes
from dnd5e_engine.rules.effects import project_condition_effects
from dnd5e_engine.types.effects import ActiveEffectChange


@pytest.mark.parametrize(
    ("kind", "key"),
    [
        (ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS, "flags.disadvantage.attack"),
        (ConditionEffectKind.DISADVANTAGE_ABILITY_CHECKS, "flags.disadvantage.check"),
    ],
)
def test_typed_clause_translates_to_existing_change(kind: ConditionEffectKind, key: str) -> None:
    assert project_condition_effects([ConditionEffect(kind=kind)]) == [
        ActiveEffectChange(key=key, mode="override", value=True),
    ]


@pytest.mark.parametrize("slug", ["poisoned", "restrained"])
def test_projection_is_repeatable_pure_and_never_draws_dice(
    slug: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = BundledAssetLoader().get_condition(slug)
    assert definition is not None
    before = definition.model_dump()

    def forbid_rng(*args: object) -> int:
        pytest.fail("Condition projection must not consume RNG")

    monkeypatch.setattr(random, "randint", forbid_rng)
    monkeypatch.setattr(random.Random, "randint", forbid_rng)
    first = project_condition_effects(iter(definition.effects))
    second = project_condition_effects(iter(definition.effects))

    assert first == second
    assert definition.model_dump() == before
    # Runtime changes belong to the caller; changing one result must not poison
    # the canonical definition or the next resolution's projection.
    first[0].value = False
    assert second[0].value is True
    assert _project_condition_changes([slug.upper(), slug]) == second
    assert definition.model_dump() == before


def test_empty_and_unsupported_effect_kinds_are_inert() -> None:
    assert project_condition_effects([]) == []
    assert project_condition_effects([ConditionEffect(kind=ConditionEffectKind.SPEED_ZERO)]) == []


def test_other_conditions_are_not_migrated() -> None:
    assert (
        _project_condition_changes(
            [
                "frightened",
                "invisible",
                "prone",
                "grappled",
                "paralyzed",
                "stunned",
            ]
        )
        == []
    )


@pytest.mark.parametrize(
    "kinds",
    [
        (),
        (ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS,),
        (ConditionEffectKind.DISADVANTAGE_ABILITY_CHECKS,),
        (
            ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS,
            ConditionEffectKind.DISADVANTAGE_ABILITY_CHECKS,
        ),
    ],
)
def test_roll_helpers_follow_data_instead_of_the_condition_name(
    kinds: tuple[ConditionEffectKind, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    definition = BundledAssetLoader().get_condition("poisoned")
    assert definition is not None
    monkeypatch.setitem(
        condition_rules._DECLARATIVE_CONDITION_EFFECTS,
        definition.slug,
        tuple(effect for effect in definition.effects if effect.kind in kinds),
    )
    attack_dis = ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS in kinds
    check_dis = ConditionEffectKind.DISADVANTAGE_ABILITY_CHECKS in kinds

    assert condition_rules.conditions_grant_advantage_on_attack([definition.slug], []) == (
        False,
        attack_dis,
    )
    assert (
        condition_rules.conditions_grant_disadvantage_on_ability_checks([definition.slug])
        is check_dis
    )
    assert condition_rules.project_passive_check_modifiers([definition.slug]) == {
        "passive_check_adv": [],
        "passive_check_dis": ["all"] if check_dis else [],
    }
    # Removing the migrated clauses must not change the legacy neighbours.
    assert condition_rules.conditions_grant_advantage_on_attack(["restrained"], []) == (False, True)
    assert condition_rules.conditions_grant_disadvantage_on_ability_checks(["frightened"]) is True


@pytest.mark.parametrize("slug", ["poisoned", "restrained"])
def test_attack_clauses_share_the_same_generic_projector(
    slug: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = BundledAssetLoader().get_condition(slug)
    assert definition is not None
    calls: list[tuple[ConditionEffect, ...]] = []

    def record_projection(effects: Iterable[ConditionEffect]) -> list[ActiveEffectChange]:
        clauses = tuple(effects)
        calls.append(clauses)
        return project_condition_effects(clauses)

    monkeypatch.setattr(condition_rules, "project_condition_effects", record_projection)

    assert condition_rules.conditions_grant_advantage_on_attack([slug], []) == (False, True)
    assert calls == [tuple(definition.effects)]
    own_attacks = [
        effect for effect in calls[0] if effect.kind == ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS
    ]
    assert len(own_attacks) == 1
    assert project_condition_effects(own_attacks) == [
        ActiveEffectChange(key="flags.disadvantage.attack", mode="override", value=True),
    ]


def test_restrained_projects_only_own_attack_disadvantage() -> None:
    definition = BundledAssetLoader().get_condition("restrained")
    assert definition is not None
    # Feed the entire canonical definition to the same translator: its other
    # three clauses must remain unsupported and stay on the legacy paths.
    assert {effect.kind for effect in definition.effects} == {
        ConditionEffectKind.SPEED_ZERO,
        ConditionEffectKind.ADVANTAGE_ATTACKS_AGAINST,
        ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS,
        ConditionEffectKind.DISADVANTAGE_SAVE,
    }
    expected = [ActiveEffectChange(key="flags.disadvantage.attack", mode="override", value=True)]
    assert project_condition_effects(definition.effects) == expected
    assert _project_condition_changes([definition.slug]) == expected
    assert (
        condition_rules.conditions_grant_disadvantage_on_ability_checks([definition.slug]) is False
    )


def test_removing_restrained_attack_clause_leaves_other_clauses_on_legacy_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = BundledAssetLoader().get_condition("restrained")
    assert definition is not None
    monkeypatch.setitem(
        condition_rules._DECLARATIVE_CONDITION_EFFECTS,
        definition.slug,
        tuple(
            effect
            for effect in definition.effects
            if effect.kind != ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS
        ),
    )
    assert _project_condition_changes([definition.slug]) == []
    assert condition_rules.conditions_grant_advantage_on_attack([definition.slug], []) == (
        False,
        False,
    )
    assert condition_rules.conditions_grant_advantage_on_attack([], [definition.slug]) == (
        True,
        False,
    )
    assert condition_rules.project_speed(30, [definition.slug]) == 0
    assert condition_rules.project_passive_save_modifiers([definition.slug]) == {
        "passive_save_adv": [],
        "passive_save_dis": ["DEX"],
        "passive_save_auto_fail": [],
    }
    assert condition_rules.project_passive_check_modifiers([definition.slug]) == {
        "passive_check_adv": [],
        "passive_check_dis": [],
    }
