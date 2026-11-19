"""Exercise the fixed exploration region planner and its condition projection."""

import pytest
from dnd5e_srd_data import MemoryAssetLoader
from dnd5e_srd_data.schema.common import PassiveEffect

from dnd5e_engine.effect_lifecycle import (
    conditions_after_effect_application,
    conditions_after_effect_expiry,
)
from dnd5e_engine.evaluation_effects import EffectState
from dnd5e_engine.evaluation_region_effect import (
    claims_region_condition_identity,
    is_fixed_region_condition_effect,
    is_fixed_region_effect,
    is_region_condition,
    plan_region_condition_transition,
    plan_region_effect_transition,
    region_condition_identity,
    resolve_region_condition_rule,
    validate_region_effect_rule,
)
from dnd5e_engine.evaluation_ruleset import RulesetBindingError, freeze_ruleset, get_default_ruleset


def _plan(
    *,
    entering: bool,
    present: bool,
    condition_immunities: tuple[str, ...] = (),
    source_id: str = "fog-a",
    rule_ref: str = "condition:poisoned",
):
    return plan_region_condition_transition(
        ruleset_binding=get_default_ruleset().binding,
        scene_id="scene-a",
        source_id=source_id,
        target_id="target-a",
        entering=entering,
        present=present,
        condition_immunities=condition_immunities,
        rule_ref=rule_ref,
    )


def test_identity_separates_scenes_and_sources_and_remains_stable() -> None:
    first = region_condition_identity("隧道:a", "fog:west", "target-a")
    second = region_condition_identity("隧道:a", "fog:east", "target-a")
    other_scene = region_condition_identity("隧道", "a:fog:west", "target-a")
    other_target = region_condition_identity("隧道:a", "fog:west", "target-b")

    assert first == region_condition_identity("隧道:a", "fog:west", "target-a")
    assert first[2] == r'region:poisoned:["\u96a7\u9053:a","fog:west"]'
    assert len({first[1], second[1], other_scene[1]}) == 3
    assert first[1:] == other_target[1:]
    assert "fog:west" not in first[1]
    assert "fog:west" in first[2]
    assert all(is_region_condition(*identity[1:]) for identity in (first, second, other_scene))
    assert not is_region_condition(first[1], second[2])
    assert not is_region_condition("effect:longstrider", first[2])
    assert not is_region_condition(first[1], "region:poisoned:invalid")
    assert claims_region_condition_identity("effect:longstrider", first[2])
    assert claims_region_condition_identity(first[1], "spell:longstrider")
    assert not claims_region_condition_identity("effect:longstrider", "spell:longstrider")


@pytest.mark.parametrize(
    ("entering", "present", "immunities", "expected"),
    [
        (True, False, (), "add"),
        (True, False, ("charmed",), "add"),
        (True, False, ("poisoned",), "none"),
        (True, True, (), "none"),
        (False, False, (), "none"),
        (False, True, (), "remove"),
        (False, True, ("poisoned",), "remove"),
    ],
)
def test_plans_guarded_edge(entering, present, immunities, expected) -> None:
    result = _plan(entering=entering, present=present, condition_immunities=immunities)
    assert result.operation == expected
    assert result.identity == region_condition_identity("scene-a", "fog-a", "target-a")
    assert (result.effect is not None) == (expected == "add")


def test_add_projects_a_durable_untimed_effect() -> None:
    result = _plan(entering=True, present=False)
    assert result.effect is not None
    restored = EffectState.model_validate_json(result.effect.model_dump_json())
    assert (restored.target_id, restored.id, restored.origin) == result.identity
    assert restored.name == "Poisoned"
    assert restored.statuses == {"poisoned"}
    assert all(value is None for value in restored.duration.model_dump().values())
    assert not restored.disabled
    assert not restored.transfer
    assert not restored.changes
    assert not restored.flags
    assert not restored.end_effects
    assert restored.action_policy is None
    assert restored.lifecycle is None


def test_explicit_deafened_rule_uses_bound_content_and_its_own_identity() -> None:
    result = _plan(entering=True, present=False, rule_ref="condition:deafened")
    assert result.effect is not None
    assert result.effect.name == "Deafened"
    assert result.effect.statuses == {"deafened"}
    assert is_fixed_region_condition_effect(result.effect)
    assert not is_fixed_region_condition_effect(
        result.effect.model_copy(update={"statuses": {"poisoned"}})
    )
    assert result.identity != region_condition_identity("scene-a", "fog-a", "target-a")
    assert (
        _plan(
            entering=True,
            present=False,
            rule_ref="condition:deafened",
            condition_immunities=("deafened",),
        ).operation
        == "none"
    )
    removal = _plan(entering=False, present=True, rule_ref="condition:deafened")
    assert removal.operation == "remove"
    assert removal.identity == result.identity


def test_fixed_area_rule_requires_its_bound_asset_except_for_legacy_poisoned() -> None:
    empty = freeze_ruleset(MemoryAssetLoader())
    canonical = get_default_ruleset()
    assert resolve_region_condition_rule(
        "condition:poisoned", ruleset_binding=empty.binding, loader=empty
    ) == ("poisoned", "Poisoned")
    with pytest.raises(ValueError, match="standalone condition asset"):
        resolve_region_condition_rule(
            "condition:deafened", ruleset_binding=empty.binding, loader=empty
        )
    for invalid in ("deafened", "condition:unknown"):
        with pytest.raises(ValueError, match="Unknown fixed-area condition rule"):
            resolve_region_condition_rule(
                invalid, ruleset_binding=canonical.binding, loader=canonical
            )


@pytest.mark.parametrize("status", ["blinded", "prone", "restrained"])
def test_region_refuses_bound_conditions_without_a_supported_exit(status: str) -> None:
    with pytest.raises(ValueError, match="prevent a supported exploration exit"):
        _plan(entering=True, present=False, rule_ref=f"condition:{status}")


@pytest.mark.parametrize("status", ["charmed", "exhaustion", "frightened"])
def test_region_refuses_condition_with_missing_context_or_state(status: str) -> None:
    with pytest.raises(ValueError, match="additional rule context or state changes"):
        _plan(entering=True, present=False, rule_ref=f"condition:{status}")


def test_region_converts_configured_numeric_effect_with_no_condition_link() -> None:
    canonical = get_default_ruleset()
    effect = PassiveEffect.model_validate(
        {
            "name": "Sluggish air",
            "transfer": False,
            "changes": [{"key": "check.bonus", "mode": 2, "value": "-3"}],
        }
    )
    arguments = dict(
        ruleset_binding=canonical.binding,
        scene_id="scene-a",
        source_id="local-a",
        target_id="target-a",
        condition_immunities=("poisoned",),
        rule=effect,
    )
    enter = plan_region_effect_transition(**arguments, entering=True, present=False)
    leave = plan_region_effect_transition(**arguments, entering=False, present=True)
    assert enter.operation == "add"
    assert leave.operation == "remove"
    assert enter.identity == leave.identity
    assert enter.effect is not None
    assert is_fixed_region_effect(enter.effect)
    assert not enter.effect.statuses
    (change,) = enter.effect.changes
    assert (change.key, change.mode, change.value) == ("check.bonus", "add", "-3")


def test_region_refuses_malformed_known_modifier_at_configuration() -> None:
    canonical = get_default_ruleset()
    effect = PassiveEffect.model_validate(
        {
            "name": "Opaque effect",
            "transfer": False,
            "changes": [{"key": "check.bonus", "mode": 2, "value": "oops"}],
        }
    )
    with pytest.raises(ValueError, match="invalid numeric check bonus"):
        validate_region_effect_rule(effect, ruleset_binding=canonical.binding)


def test_native_region_projects_multiple_statuses_and_noncheck_modifiers() -> None:
    canonical = get_default_ruleset()
    rule = PassiveEffect.model_validate(
        {
            "name": "Fog",
            "transfer": False,
            "statuses": ["poisoned", "deafened"],
            "changes": [{"key": "attack.roll.bonus", "mode": 2, "value": "-3"}],
        }
    )
    validate_region_effect_rule(rule, ruleset_binding=canonical.binding)
    result = plan_region_effect_transition(
        ruleset_binding=canonical.binding,
        scene_id="scene-a",
        source_id="local-a",
        target_id="target-a",
        entering=True,
        present=False,
        condition_immunities=("poisoned",),
        rule=rule,
    )
    assert result.effect is not None
    assert result.effect.statuses == {"deafened"}
    assert is_fixed_region_effect(result.effect)
    assert not is_fixed_region_effect(result.effect.model_copy(update={"statuses": {"prone"}}))
    assert [change.key for change in result.effect.changes] == ["attack.roll.bonus"]


@pytest.mark.parametrize("status", ["poisoned", "deafened"])
def test_fixed_area_rule_rejects_modified_bound_mechanics(status: str) -> None:
    definition = get_default_ruleset().get_condition(status)
    assert definition is not None
    modified = freeze_ruleset(
        MemoryAssetLoader(conditions=[definition.model_copy(update={"effects": []})])
    )
    with pytest.raises(ValueError, match="modified condition mechanics"):
        resolve_region_condition_rule(
            f"condition:{status}", ruleset_binding=modified.binding, loader=modified
        )


def test_projected_session_conditions_retain_overlapping_sources() -> None:
    first = _plan(entering=True, present=False)
    second = _plan(entering=True, present=False, source_id="fog-b")
    assert first.effect is not None
    assert second.effect is not None

    conditions, names = conditions_after_effect_application(
        [], first.effect.id, first.effect.statuses, scope="session"
    )
    assert names == ["poisoned"]
    conditions, names = conditions_after_effect_application(
        conditions, second.effect.id, second.effect.statuses, scope="session"
    )
    assert not names
    assert [(c.condition, c.scope, c.source_effect_id) for c in conditions] == [
        ("poisoned", "session", first.effect.id),
        ("poisoned", "session", second.effect.id),
    ]

    removal = _plan(entering=False, present=True)
    remaining = conditions_after_effect_expiry(conditions, removal.identity[1], ("poisoned",), ())
    assert [(c.condition, c.source_effect_id) for c in remaining] == [
        ("poisoned", second.effect.id)
    ]
    combat, _ = conditions_after_effect_application([], first.effect.id, first.effect.statuses)
    assert combat[0].scope == "combat"


def test_no_op_rejects_stale_ruleset_binding() -> None:
    loader = get_default_ruleset()
    stale = loader.binding.model_copy(update={"data_revision": "sha256:" + "0" * 64})
    with pytest.raises(RulesetBindingError):
        plan_region_condition_transition(
            ruleset_binding=stale,
            scene_id="scene-a",
            source_id="fog-a",
            target_id="target-a",
            entering=False,
            present=False,
            condition_immunities=(),
            loader=loader,
        )
