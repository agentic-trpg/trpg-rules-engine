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
from dnd5e_engine.rules.effects import project_condition_effects, save_flag_abilities
from dnd5e_engine.types.effects import ActiveEffectChange


def test_migration_selection_is_an_explicit_clause_allowlist() -> None:
    migrations = condition_rules._DECLARATIVE_CONDITION_MIGRATIONS
    assert migrations == {
        "poisoned": frozenset(
            {
                ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS,
                ConditionEffectKind.DISADVANTAGE_ABILITY_CHECKS,
            }
        ),
        "restrained": frozenset(
            {
                ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS,
                ConditionEffectKind.DISADVANTAGE_SAVE,
                ConditionEffectKind.ADVANTAGE_ATTACKS_AGAINST,
            }
        ),
        "blinded": frozenset(
            {
                ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS,
                ConditionEffectKind.ADVANTAGE_ATTACKS_AGAINST,
            }
        ),
        "prone": frozenset({ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS}),
        "paralyzed": frozenset(
            {ConditionEffectKind.AUTO_FAIL_SAVE, ConditionEffectKind.ADVANTAGE_ATTACKS_AGAINST}
        ),
        "stunned": frozenset(
            {ConditionEffectKind.AUTO_FAIL_SAVE, ConditionEffectKind.ADVANTAGE_ATTACKS_AGAINST}
        ),
        "petrified": frozenset(
            {ConditionEffectKind.AUTO_FAIL_SAVE, ConditionEffectKind.ADVANTAGE_ATTACKS_AGAINST}
        ),
        "unconscious": frozenset(
            {ConditionEffectKind.AUTO_FAIL_SAVE, ConditionEffectKind.ADVANTAGE_ATTACKS_AGAINST}
        ),
    }


@pytest.mark.parametrize(
    ("kind", "key"),
    [
        (ConditionEffectKind.ADVANTAGE_ATTACKS_AGAINST, "flags.advantage.attack"),
        (ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS, "flags.disadvantage.attack"),
        (ConditionEffectKind.DISADVANTAGE_ABILITY_CHECKS, "flags.disadvantage.check"),
    ],
)
def test_typed_clause_translates_to_existing_change(kind: ConditionEffectKind, key: str) -> None:
    assert project_condition_effects([ConditionEffect(kind=kind)]) == [
        ActiveEffectChange(key=key, mode="override", value=True),
    ]


@pytest.mark.parametrize(
    "slug",
    [
        "poisoned",
        "restrained",
        "blinded",
        "prone",
        "paralyzed",
        "stunned",
        "petrified",
        "unconscious",
    ],
)
def test_projection_is_repeatable_pure_and_never_draws_dice(
    slug: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = BundledAssetLoader().get_condition(slug)
    assert definition is not None
    before = definition.model_dump()
    expected_collected = _project_condition_changes([slug])

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
    assert collected == expected_collected
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
                "deafened",
                "incapacitated",
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
    assert calls == [
        (),
        tuple(
            effect
            for effect in definition.effects
            if effect.kind in condition_rules._DECLARATIVE_CONDITION_MIGRATIONS[slug]
        ),
    ]
    own_attacks = [
        effect for effect in calls[1] if effect.kind == ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS
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
def test_partial_migrations_project_only_opted_in_clauses(
    slug: str, expected_kinds: set[ConditionEffectKind]
) -> None:
    definition = BundledAssetLoader().get_condition(slug)
    assert definition is not None
    # Generic support is broader than migration selection: Prone's target
    # advantage kind is supported, but its distance clause is not opted in.
    assert {effect.kind for effect in definition.effects} == expected_kinds
    expected = [ActiveEffectChange(key="flags.disadvantage.attack", mode="override", value=True)]
    if slug != "prone":
        expected.insert(
            0, ActiveEffectChange(key="flags.advantage.attack", mode="override", value=True)
        )
    if slug == "restrained":
        expected.append(
            ActiveEffectChange(key="flags.disadvantage.save.dexterity", mode="override", value=True)
        )
    if slug == "prone":
        assert project_condition_effects(definition.effects) == [
            *expected,
            ActiveEffectChange(key="flags.advantage.attack", mode="override", value=True),
        ]
    else:
        assert project_condition_effects(definition.effects) == expected
    assert _project_condition_changes([definition.slug]) == expected
    assert (
        condition_rules.conditions_grant_disadvantage_on_ability_checks([definition.slug]) is False
    )


def test_removing_restrained_attack_clause_preserves_save_and_legacy_neighbours(
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
    assert _project_condition_changes([definition.slug]) == [
        ActiveEffectChange(key="flags.advantage.attack", mode="override", value=True),
        ActiveEffectChange(key="flags.disadvantage.save.dexterity", mode="override", value=True),
    ]
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
def test_removing_own_attack_clause_preserves_blinded_and_prone_target_neighbours(
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

    assert _project_condition_changes([slug]) == (
        [ActiveEffectChange(key="flags.advantage.attack", mode="override", value=True)]
        if slug == "blinded"
        else []
    )
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
    unconscious = BundledAssetLoader().get_condition("unconscious")
    assert unconscious is not None
    assert calls == [
        (),
        tuple(
            effect
            for effect in unconscious.effects
            if effect.kind in condition_rules._DECLARATIVE_CONDITION_MIGRATIONS["unconscious"]
        )
        + tuple(
            effect
            for effect in definition.effects
            if effect.kind == ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS
        ),
    ]

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


@pytest.mark.parametrize("slug", ["poisoned", "restrained", "blinded", "prone"])
def test_unopted_clauses_do_not_reach_an_expanded_projector(
    slug: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    definition = BundledAssetLoader().get_condition(slug)
    assert definition is not None
    # Also model a future canonical clause for conditions without a save clause.
    # The fixture changes the cached input, never the bundled canonical data.
    canonical = (
        *definition.effects,
        ConditionEffect(kind=ConditionEffectKind.AUTO_FAIL_SAVE, abilities=["wis"]),
    )
    monkeypatch.setitem(condition_rules._DECLARATIVE_CONDITION_EFFECTS, slug, canonical)
    expected = tuple(
        effect
        for effect in definition.effects
        if effect.kind in condition_rules._DECLARATIVE_CONDITION_MIGRATIONS[slug]
    )
    calls: list[tuple[ConditionEffect, ...]] = []

    def expanded_projector(effects: Iterable[ConditionEffect]) -> list[ActiveEffectChange]:
        clauses = tuple(effects)
        calls.append(clauses)
        projected = project_condition_effects(clauses)
        # Pretend the generic projector now supports every other canonical kind.
        projected.extend(
            ActiveEffectChange(key=f"future.{effect.kind.value}", mode="override", value=True)
            for effect in clauses
            if effect.kind
            not in {
                ConditionEffectKind.ADVANTAGE_ATTACKS_AGAINST,
                ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS,
                ConditionEffectKind.DISADVANTAGE_ABILITY_CHECKS,
                ConditionEffectKind.DISADVANTAGE_SAVE,
                ConditionEffectKind.AUTO_FAIL_SAVE,
            }
        )
        return projected

    monkeypatch.setattr(condition_rules, "project_condition_effects", expanded_projector)
    assert _project_condition_changes([slug.upper(), slug]) == project_condition_effects(expected)
    assert calls == [expected]


@pytest.mark.parametrize("slug", ["restrained", "blinded", "prone"])
def test_projector_support_alone_does_not_opt_in_a_condition_clause(
    slug: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    definition = BundledAssetLoader().get_condition(slug)
    assert definition is not None
    monkeypatch.setitem(
        condition_rules._DECLARATIVE_CONDITION_EFFECTS,
        slug,
        (
            *definition.effects,
            ConditionEffect(kind=ConditionEffectKind.DISADVANTAGE_ABILITY_CHECKS),
        ),
    )

    # This kind already has generic projector support, but these conditions
    # have never opted in to it. Support must not silently enable a mechanic.
    assert _project_condition_changes([slug]) == project_condition_effects(
        effect
        for effect in definition.effects
        if effect.kind in condition_rules._DECLARATIVE_CONDITION_MIGRATIONS[slug]
    )
    assert condition_rules.conditions_grant_disadvantage_on_ability_checks([slug]) is False


@pytest.mark.parametrize(
    ("slug", "kind", "attack_dis", "check_dis"),
    [
        ("poisoned", ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS, False, True),
        ("poisoned", ConditionEffectKind.DISADVANTAGE_ABILITY_CHECKS, True, False),
        ("restrained", ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS, False, False),
        ("blinded", ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS, False, False),
        ("prone", ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS, False, False),
    ],
)
def test_removing_opt_in_disables_only_that_clause(
    slug: str,
    kind: ConditionEffectKind,
    attack_dis: bool,
    check_dis: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = condition_rules._DECLARATIVE_CONDITION_EFFECTS[slug]
    monkeypatch.setitem(
        condition_rules._DECLARATIVE_CONDITION_MIGRATIONS,
        slug,
        condition_rules._DECLARATIVE_CONDITION_MIGRATIONS[slug] - {kind},
    )

    assert condition_rules.conditions_grant_advantage_on_attack([slug], []) == (False, attack_dis)
    assert condition_rules.conditions_grant_disadvantage_on_ability_checks([slug]) is check_dis
    assert condition_rules._DECLARATIVE_CONDITION_EFFECTS[slug] == canonical


@pytest.mark.parametrize(
    ("kind", "prefix"),
    [
        (ConditionEffectKind.DISADVANTAGE_SAVE, "flags.disadvantage.save"),
        (ConditionEffectKind.AUTO_FAIL_SAVE, "flags.auto_fail.save"),
    ],
)
@pytest.mark.parametrize(
    ("code", "name"),
    [
        ("str", "strength"),
        ("dex", "dexterity"),
        ("con", "constitution"),
        ("int", "intelligence"),
        ("wis", "wisdom"),
        ("cha", "charisma"),
    ],
)
def test_save_clauses_use_the_existing_full_name_flag_vocabulary(
    kind: ConditionEffectKind, prefix: str, code: str, name: str
) -> None:
    effect = ConditionEffect(kind=kind, abilities=[f" {code.upper()} ", code])
    changes = project_condition_effects([effect])
    assert changes == [ActiveEffectChange(key=f"{prefix}.{name}", mode="override", value=True)]
    assert save_flag_abilities(changes, kind) == [code.upper()]
    assert effect.abilities == [f" {code.upper()} ", code]


@pytest.mark.parametrize(
    "kind", [ConditionEffectKind.DISADVANTAGE_SAVE, ConditionEffectKind.AUTO_FAIL_SAVE]
)
@pytest.mark.parametrize("abilities", [[], ["unknown", ""]])
def test_unscoped_or_unknown_save_abilities_do_not_emit_broad_flags(
    kind: ConditionEffectKind, abilities: list[str]
) -> None:
    assert project_condition_effects([ConditionEffect(kind=kind, abilities=abilities)]) == []


@pytest.mark.parametrize(
    "kind", [ConditionEffectKind.DISADVANTAGE_SAVE, ConditionEffectKind.AUTO_FAIL_SAVE]
)
def test_save_flag_consumption_accepts_only_true_override_flags(kind: ConditionEffectKind) -> None:
    flag = project_condition_effects([ConditionEffect(kind=kind, abilities=["dex"])])[0]
    changes = [
        flag.model_copy(update={"mode": "add"}),
        flag.model_copy(update={"value": False}),
        flag.model_copy(update={"key": "flags.advantage.save.dexterity"}),
        flag,
        flag,
    ]
    assert save_flag_abilities(changes, kind) == ["DEX"]


@pytest.mark.parametrize(
    ("slug", "kind"),
    [
        ("restrained", ConditionEffectKind.DISADVANTAGE_SAVE),
        ("paralyzed", ConditionEffectKind.AUTO_FAIL_SAVE),
        ("stunned", ConditionEffectKind.AUTO_FAIL_SAVE),
        ("petrified", ConditionEffectKind.AUTO_FAIL_SAVE),
        ("unconscious", ConditionEffectKind.AUTO_FAIL_SAVE),
    ],
)
def test_save_scopes_come_from_canonical_clauses_instead_of_names(
    slug: str, kind: ConditionEffectKind, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical = condition_rules._DECLARATIVE_CONDITION_EFFECTS[slug]
    monkeypatch.setitem(
        condition_rules._DECLARATIVE_CONDITION_EFFECTS,
        slug,
        tuple(
            effect.model_copy(update={"abilities": ["wis", "cha"]})
            if effect.kind == kind
            else effect
            for effect in canonical
        ),
    )
    assert condition_rules.project_passive_save_modifiers([slug]) == {
        "passive_save_adv": [],
        "passive_save_dis": ["WIS", "CHA"],
        "passive_save_auto_fail": ["WIS", "CHA"]
        if kind == ConditionEffectKind.AUTO_FAIL_SAVE
        else [],
    }


@pytest.mark.parametrize("slug", ["poisoned", "blinded", "prone", "frightened"])
@pytest.mark.parametrize(
    "kind", [ConditionEffectKind.DISADVANTAGE_SAVE, ConditionEffectKind.AUTO_FAIL_SAVE]
)
def test_generic_save_support_does_not_opt_in_an_unmigrated_condition_clause(
    slug: str, kind: ConditionEffectKind, monkeypatch: pytest.MonkeyPatch
) -> None:
    clause = ConditionEffect(kind=kind, abilities=["dex"])
    assert project_condition_effects([clause])
    monkeypatch.setitem(
        condition_rules._DECLARATIVE_CONDITION_EFFECTS,
        slug,
        (*condition_rules._DECLARATIVE_CONDITION_EFFECTS.get(slug, ()), clause),
    )
    assert condition_rules.project_passive_save_modifiers([slug]) == {
        "passive_save_adv": [],
        "passive_save_dis": [],
        "passive_save_auto_fail": [],
    }


@pytest.mark.parametrize(
    ("slug", "kind", "attack_dis"),
    [
        ("restrained", ConditionEffectKind.DISADVANTAGE_SAVE, True),
        ("paralyzed", ConditionEffectKind.AUTO_FAIL_SAVE, False),
        ("stunned", ConditionEffectKind.AUTO_FAIL_SAVE, False),
        ("petrified", ConditionEffectKind.AUTO_FAIL_SAVE, False),
        ("unconscious", ConditionEffectKind.AUTO_FAIL_SAVE, True),
    ],
)
def test_removing_save_opt_in_disables_only_the_save_clause(
    slug: str, kind: ConditionEffectKind, attack_dis: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical = condition_rules._DECLARATIVE_CONDITION_EFFECTS[slug]
    monkeypatch.setitem(
        condition_rules._DECLARATIVE_CONDITION_MIGRATIONS,
        slug,
        condition_rules._DECLARATIVE_CONDITION_MIGRATIONS[slug] - {kind},
    )
    assert condition_rules.project_passive_save_modifiers([slug]) == {
        "passive_save_adv": [],
        "passive_save_dis": [],
        "passive_save_auto_fail": [],
    }
    assert condition_rules.conditions_grant_advantage_on_attack([slug], []) == (False, attack_dis)
    assert condition_rules.conditions_grant_advantage_on_attack([], [slug]) == (True, False)
    assert condition_rules._DECLARATIVE_CONDITION_EFFECTS[slug] == canonical


@pytest.mark.parametrize(
    "conditions",
    [
        ["restrained", "paralyzed"],
        ["PARALYZED", "Restrained", "paralyzed", "RESTRAINED", "stunned"],
        ["STUNNED", "petrified", "unconscious", "restrained", "PARALYZED"],
    ],
)
def test_save_projection_preserves_normalization_deduplication_and_fallback_order(
    conditions: list[str],
) -> None:
    assert condition_rules.project_passive_save_modifiers(conditions) == {
        "passive_save_adv": [],
        "passive_save_dis": ["DEX", "STR"],
        "passive_save_auto_fail": ["STR", "DEX"],
    }
