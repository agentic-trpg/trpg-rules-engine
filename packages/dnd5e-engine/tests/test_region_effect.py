"""Exercise the fixed exploration region planner and its condition projection."""

import pytest

from dnd5e_engine.effect_lifecycle import (
    conditions_after_effect_application,
    conditions_after_effect_expiry,
)
from dnd5e_engine.evaluation_effects import EffectState
from dnd5e_engine.evaluation_region_effect import (
    is_region_condition,
    plan_region_condition_transition,
    region_condition_identity,
)
from dnd5e_engine.evaluation_ruleset import RulesetBindingError, get_default_ruleset


def _plan(
    *,
    entering: bool,
    present: bool,
    condition_immunities: tuple[str, ...] = (),
    source_id: str = "fog-a",
):
    return plan_region_condition_transition(
        ruleset_binding=get_default_ruleset().binding,
        scene_id="scene-a",
        source_id=source_id,
        target_id="target-a",
        entering=entering,
        present=present,
        condition_immunities=condition_immunities,
    )


def test_identity_separates_scenes_and_sources_and_remains_stable() -> None:
    first = region_condition_identity("隧道:a", "fog:west", "target-a")
    second = region_condition_identity("隧道:a", "fog:east", "target-a")
    other_scene = region_condition_identity("隧道", "a:fog:west", "target-a")
    other_target = region_condition_identity("隧道:a", "fog:west", "target-b")

    assert first == region_condition_identity("隧道:a", "fog:west", "target-a")
    assert len({first[1], second[1], other_scene[1]}) == 3
    assert first[1:] == other_target[1:]
    assert "fog:west" not in first[1]
    assert "fog:west" in first[2]
    assert all(is_region_condition(*identity[1:]) for identity in (first, second, other_scene))
    assert not is_region_condition(first[1], second[2])
    assert not is_region_condition("effect:longstrider", first[2])
    assert not is_region_condition(first[1], "region:poisoned:invalid")


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
