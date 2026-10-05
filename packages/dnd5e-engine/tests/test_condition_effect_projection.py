"""Typed condition clauses project without names, mutation, or dice draws."""

from __future__ import annotations

import builtins
import io
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


@pytest.mark.parametrize("slug", ["poisoned", "restrained", "blinded", "prone"])
def test_projection_is_repeatable_pure_and_never_draws_dice(
    slug: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = BundledAssetLoader().get_condition(slug)
    assert definition is not None
    before = definition.model_dump()

    def forbid_rng(*args: object) -> int:
        pytest.fail("Condition projection must not consume RNG")

    def forbid_io(*args: object, **kwargs: object) -> None:
        pytest.fail("Condition projection must not perform I/O")

    with monkeypatch.context() as projection_patch:
        projection_patch.setattr(random, "randint", forbid_rng)
        projection_patch.setattr(random.Random, "randint", forbid_rng)
        projection_patch.setattr(random.Random, "random", forbid_rng)
        projection_patch.setattr(random.Random, "getrandbits", forbid_rng)
        projection_patch.setattr(builtins, "open", forbid_io)
        projection_patch.setattr(io, "open", forbid_io)
        projection_patch.setattr(BundledAssetLoader, "get_condition", forbid_io)
        first = project_condition_effects(iter(definition.effects))
        second = project_condition_effects(iter(definition.effects))
        collected = _project_condition_changes([slug.upper(), slug])

    assert first == second
    assert definition.model_dump() == before
    # Runtime changes belong to the caller; changing one result must not poison
    # the canonical definition or the next resolution's projection.
    first[0].value = False
    assert second[0].value is True
    assert collected == second
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


@pytest.mark.parametrize("slug", ["poisoned", "restrained", "blinded", "prone"])
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


@pytest.mark.parametrize(
    ("slug", "expected_kinds"),
    [
        (
            "restrained",
            {
                ConditionEffectKind.SPEED_ZERO,
                ConditionEffectKind.ADVANTAGE_ATTACKS_AGAINST,
                ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS,
                ConditionEffectKind.DISADVANTAGE_SAVE,
            },
        ),
        (
            "blinded",
            {
                ConditionEffectKind.AUTO_FAIL_SIGHT_CHECKS,
                ConditionEffectKind.ADVANTAGE_ATTACKS_AGAINST,
                ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS,
            },
        ),
        (
            "prone",
            {
                ConditionEffectKind.RESTRICTED_MOVEMENT_CRAWL,
                ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS,
                ConditionEffectKind.ADVANTAGE_ATTACKS_AGAINST,
                ConditionEffectKind.DISADVANTAGE_ATTACKS_AGAINST,
            },
        ),
    ],
)
def test_partial_migrations_project_only_own_attack_disadvantage(
    slug: str, expected_kinds: set[ConditionEffectKind]
) -> None:
    definition = BundledAssetLoader().get_condition(slug)
    assert definition is not None
    # Feed the entire canonical definition to the same translator: its other
    # clauses must remain unsupported and stay on the legacy paths.
    assert {effect.kind for effect in definition.effects} == expected_kinds
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


@pytest.mark.parametrize(
    ("slug", "distance_ft", "expected_target"),
    [
        ("blinded", None, (True, False)),
        ("prone", 0, (True, False)),
        ("prone", 5, (True, False)),
        ("prone", 6, (False, True)),
        ("prone", 30, (False, True)),
        ("prone", None, (False, False)),
    ],
)
def test_removing_attack_clause_preserves_blinded_and_prone_legacy_neighbours(
    slug: str,
    distance_ft: int | None,
    expected_target: tuple[bool, bool],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = BundledAssetLoader().get_condition(slug)
    assert definition is not None
    monkeypatch.setitem(
        condition_rules._DECLARATIVE_CONDITION_EFFECTS,
        slug,
        tuple(
            effect
            for effect in definition.effects
            if effect.kind != ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS
        ),
    )

    assert _project_condition_changes([slug]) == []
    assert condition_rules.conditions_grant_advantage_on_attack([slug], []) == (False, False)
    assert (
        condition_rules.conditions_grant_advantage_on_attack([], [slug], distance_ft=distance_ft)
        == expected_target
    )
    assert condition_rules.project_speed(30, [slug]) == 30
    assert condition_rules.project_passive_check_modifiers([slug]) == {
        "passive_check_adv": [],
        "passive_check_dis": [],
    }
    assert condition_rules.project_passive_save_modifiers([slug]) == {
        "passive_save_adv": [],
        "passive_save_dis": [],
        "passive_save_auto_fail": [],
    }


@pytest.mark.parametrize("conditions", [["unconscious"], ["UNCONSCIOUS", "prone", "PRONE"]])
def test_implied_prone_uses_the_same_projector_and_canonical_attack_clause(
    conditions: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    definition = BundledAssetLoader().get_condition("prone")
    assert definition is not None
    calls: list[tuple[ConditionEffect, ...]] = []

    def record_projection(effects: Iterable[ConditionEffect]) -> list[ActiveEffectChange]:
        clauses = tuple(effects)
        calls.append(clauses)
        return project_condition_effects(clauses)

    monkeypatch.setattr(condition_rules, "project_condition_effects", record_projection)
    assert condition_rules.conditions_grant_advantage_on_attack(conditions, []) == (False, True)
    assert calls == [tuple(definition.effects)]

    monkeypatch.setitem(
        condition_rules._DECLARATIVE_CONDITION_EFFECTS,
        "prone",
        tuple(
            effect
            for effect in definition.effects
            if effect.kind != ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS
        ),
    )
    assert condition_rules.conditions_grant_advantage_on_attack(conditions, []) == (False, False)
