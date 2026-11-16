"""Bounded Longstrider duration proposal for the host-owned world clock."""

import json

import pytest

from dnd5e_engine.evaluation_ruleset import get_default_ruleset
from dnd5e_engine.evaluation_world_effect import (
    WorldEffectApplication,
    WorldEffectApplicationRequest,
    plan_longstrider_application,
)


def _request(**changes):
    fields = dict(
        schema_version="engine-world-effect/1",
        session_id="session-1",
        command_id="command-1",
        source_id="caster-1",
        target_id="target-1",
        ruleset_binding=get_default_ruleset().binding,
    )
    fields.update(changes)
    return WorldEffectApplicationRequest(**fields)


def test_longstrider_is_canonical_timed_effect_without_world_clock() -> None:
    result = plan_longstrider_application(_request())
    assert result.duration_seconds == 3600
    assert result.identity == (
        "target-1",
        "effect:longstrider",
        "cast:longstrider:caster-1",
    )
    assert result.expiry.action == "remove"
    assert result.effect.duration.seconds == 3600
    assert [(change.key, change.mode, change.value) for change in result.effect.changes] == [
        ("system.attributes.movement.walk", "add", "10")
    ]
    wire = result.model_dump(mode="json")
    assert "identity" not in wire
    assert "duration_seconds" not in wire
    assert wire["expiry"] == {"action": "remove"}
    assert WorldEffectApplication.model_validate_json(json.dumps(wire)) == result
    result.verify_request(_request())


def test_world_effect_request_rejects_clock_and_binding_errors() -> None:
    with pytest.raises(ValueError):
        _request(applied_tick=1)
    with pytest.raises(ValueError):
        plan_longstrider_application(
            _request(
                ruleset_binding=get_default_ruleset().binding.model_copy(
                    update={"data_revision": "sha256:" + "0" * 64}
                )
            )
        )


def test_world_effect_result_rejects_malformed_expiry_and_duration() -> None:
    result = plan_longstrider_application(_request())
    wire = result.model_dump(mode="json")
    wire["expiry"]["action"] = "retain"
    with pytest.raises(ValueError):
        WorldEffectApplication.model_validate_json(json.dumps(wire))
    wire = result.model_dump(mode="json")
    wire["effect"]["duration"]["seconds"] = None
    with pytest.raises(ValueError):
        WorldEffectApplication.model_validate_json(json.dumps(wire))
    wire = result.model_dump(mode="json")
    wire["identity"] = ["forged", "effect", "origin"]
    with pytest.raises(ValueError):
        WorldEffectApplication.model_validate_json(json.dumps(wire))
    with pytest.raises(ValueError):
        result.verify_request(_request(command_id="another"))
