"""R26: generated lifecycle wire schemas and actual strict input agree recursively."""

from copy import deepcopy
from dataclasses import fields

import pytest
from pydantic import ValidationError
from pydantic_core import to_json

from dnd5e_engine.effect_lifecycle import OngoingEffectLifecycle
from dnd5e_engine.evaluation_effects import (
    EffectLifecycleApplicationState,
    EffectLifecycleSpecState,
    RepeatSaveState,
)
from dnd5e_engine.evaluation_state import CombatSnapshot, LifecycleRecord
from tests.test_stateless_concentration import concentration_case

PATH_FIELDS = (
    ((), tuple(field.name for field in fields(OngoingEffectLifecycle))),
    (("application",), tuple(EffectLifecycleApplicationState.model_fields)),
    (("application", "spec"), tuple(EffectLifecycleSpecState.model_fields)),
    (("application", "spec", "repeat_save"), tuple(RepeatSaveState.model_fields)),
)
OMISSIONS = [(path, name) for path, names in PATH_FIELDS for name in names]


@pytest.fixture(scope="module")
def clock():
    request, _, _, _ = concentration_case()
    return request.state_snapshot.combat_state.effect_lifecycles[0]


def at(value, path):
    for name in path:
        value = value[name]
    return value


def schema_node(root, node):
    while "$ref" in node or "anyOf" in node:
        if "$ref" in node:
            node = at(root, node["$ref"].removeprefix("#/").split("/"))
        else:
            node = next(branch for branch in node["anyOf"] if branch.get("type") != "null")
    return node


def validate_wire(wire, json_input):
    if json_input:
        return LifecycleRecord.model_validate_json(to_json(wire))
    return LifecycleRecord.model_validate(wire)


@pytest.mark.parametrize("mode", ["validation", "serialization"])
def test_generated_schema_requires_every_clock_and_nested_field(mode):
    root = LifecycleRecord.model_json_schema(mode=mode)
    for path, names in PATH_FIELDS:
        node = schema_node(root, root["properties"]["state"])
        for name in path:
            node = schema_node(root, node["properties"][name])
        assert set(node["properties"]) == set(names)
        assert set(node["required"]) == set(names)
        assert node["additionalProperties"] is False


@pytest.mark.parametrize("path,name", OMISSIONS)
@pytest.mark.parametrize("json_input", [False, True])
def test_every_omitted_lifecycle_field_is_rejected(clock, path, name, json_input):
    wire = deepcopy(clock.model_dump(mode="python"))
    del at(wire["state"], path)[name]
    with pytest.raises(ValidationError):
        validate_wire(wire, json_input)


@pytest.mark.parametrize("path,_names", PATH_FIELDS)
@pytest.mark.parametrize("json_input", [False, True])
def test_extra_fields_are_rejected_recursively(clock, path, _names, json_input):
    wire = deepcopy(clock.model_dump(mode="python"))
    at(wire["state"], path)["unknown_mechanic"] = None
    with pytest.raises(ValidationError):
        validate_wire(wire, json_input)


@pytest.mark.parametrize(
    "path,value",
    [
        (("applied_round",), True),
        (("expires_round",), "11"),
        (("application", "is_magical"), "true"),
        (("application", "save_dc"), "15"),
        (("application", "spec", "maximum_rounds"), True),
        (("application", "spec", "expire_on_positive_damage"), 0),
        (("application", "spec", "repeat_save", "phase"), 1),
    ],
)
@pytest.mark.parametrize("json_input", [False, True])
def test_invalid_types_never_coerce_clock_facts(clock, path, value, json_input):
    wire = deepcopy(clock.model_dump(mode="python"))
    at(wire["state"], path[:-1])[path[-1]] = value
    with pytest.raises(ValidationError):
        validate_wire(wire, json_input)


def test_python_tuple_types_are_strict_but_json_arrays_round_trip(clock):
    wire = clock.model_dump(mode="python")
    wire["state"]["identity"] = list(wire["state"]["identity"])
    with pytest.raises(ValidationError):
        LifecycleRecord.model_validate(wire)
    assert LifecycleRecord.model_validate_json(to_json(wire)) == clock


@pytest.mark.parametrize("mode", ["validation", "serialization"])
def test_snapshot_schema_reaches_the_same_complete_clock(mode):
    root = CombatSnapshot.model_json_schema(mode=mode)
    clock_schema = root["$defs"]["OngoingEffectLifecycleState"]
    assert set(clock_schema["required"]) == set(PATH_FIELDS[0][1])
    spec = root["$defs"]["EffectLifecycleSpecState"]
    assert set(spec["required"]) == set(EffectLifecycleSpecState.model_fields)


def test_explicit_nulls_empty_collections_and_round_trips(clock):
    wire = clock.model_dump(mode="python")
    wire["state"].update(expires_round=None, expiry_actor_id=None, last_repeat_turn_serial=None)
    wire["state"]["application"].update(save_ability=None, save_dc=None, is_magical=False)
    wire["state"]["application"]["spec"].update(repeat_save=None)
    python_clock = LifecycleRecord.model_validate(wire)
    json_clock = LifecycleRecord.model_validate_json(to_json(wire))
    assert python_clock == json_clock
    assert python_clock.model_dump(mode="python") == wire
    assert python_clock.state.remaining_one_use_modifiers == ()
    assert python_clock.state.application.spec.on_end == ()
    assert LifecycleRecord.model_validate(python_clock) == python_clock
    assert LifecycleRecord.model_validate_json(python_clock.model_dump_json()) == python_clock
    assert LifecycleRecord.model_validate_json(clock.model_dump_json()) == clock


@pytest.mark.parametrize("old_version", ["engine-snapshot/9", "engine-snapshot/12"])
def test_old_snapshots_cannot_bypass_current_clock_boundary(old_version):
    request, _, _, _ = concentration_case()
    wire = request.state_snapshot.model_dump(mode="json")
    wire["snapshot_schema_version"] = old_version
    with pytest.raises(ValidationError):
        CombatSnapshot.model_validate_json(to_json(wire))
