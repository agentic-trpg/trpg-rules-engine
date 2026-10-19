"""Openable against main's current protocol and trusted frozen dataset path."""

import asyncio
import random
from collections import Counter
from copy import deepcopy

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader
from pydantic import BaseModel, ValidationError

from dnd5e_engine import evaluation_ruleset as rules
from dnd5e_engine.evaluation import evaluate
from dnd5e_engine.evaluation_contracts import RuleEvaluationResult
from dnd5e_engine.evaluation_openable import OpenableCheckRequest, evaluate_openable
from dnd5e_engine.evaluation_rng import RNGState
from tests.evaluation_support import FOE, synthetic_loader
from tests.test_openable_checks import case
from tests.test_stateless_attack import request_and_live


def operation_case(operation="pick_lock", **kwargs):
    tool = {"pick_lock": "thieves-tools", "force_open": "crowbar", "clear_jam": "smiths-tools"}[
        operation
    ]
    spec = case(tool=tool, **kwargs)
    if operation != "pick_lock":
        spec = spec.model_copy(
            update={
                "operation": operation,
                "required_tool": tool,
                "leverage_applicable": operation == "force_open",
                "request": spec.request.model_copy(
                    update={
                        "payload": spec.request.payload.model_copy(
                            update={"ability": "str", "skill": "athletics"}
                        )
                    }
                ),
            }
        )
    return spec


def actor_update(spec, **updates):
    snapshot = spec.request.state_snapshot
    actors = (
        snapshot.character_states[0].model_copy(update=updates),
        *snapshot.character_states[1:],
    )
    return spec.model_copy(
        update={
            "request": spec.request.model_copy(
                update={"state_snapshot": snapshot.model_copy(update={"character_states": actors})}
            )
        }
    )


def execute(spec, loader):
    return asyncio.run(evaluate_openable(spec, loader=loader))


def forbid_rng(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("refused request restored RNG before admission")

    monkeypatch.setattr(RNGState, "restore", forbidden)


@pytest.mark.parametrize("operation", ["pick_lock", "force_open", "clear_jam"])
@pytest.mark.parametrize("succeeded,dc", [(True, 0), (False, 100)])
def test_complete_result_transport_replay_and_ordinary_pipeline_parity(operation, succeeded, dc):
    spec = operation_case(operation)
    req = spec.request.model_copy(
        update={"payload": spec.request.payload.model_copy(update={"dc": dc})}
    )
    spec = spec.model_copy(update={"request": req})
    before = deepcopy(spec)
    fixed = rules.freeze_ruleset(synthetic_loader())
    result = execute(spec, fixed)
    assert result.status == "accepted"
    assert result.schema_version == "engine-evaluation/24"
    assert result.input_ruleset_binding.evaluator_version == "dnd5e-evaluation/37"
    assert result.state_delta.expected_world_version == req.state_snapshot.world_version
    assert result.state_delta.operations == ()
    assert [(r.kind, r.ref, r.version) for r in result.read_set] == [
        ("world", req.session_id, req.state_snapshot.world_version)
    ]
    assert result.proposed_events[0].succeeded is succeeded
    assert result.rng_transition.state_changed
    result.rng_transition.verify_input(req.rng_context)
    assert result.stage_facts.schema_version == "engine-stage-facts/2"
    assert result.stage_facts.coverage == "uncovered"
    assert result.stage_facts == asyncio.run(evaluate(req, loader=fixed)).stage_facts
    for restored in (
        RuleEvaluationResult.model_validate(result.model_dump(mode="python")),
        RuleEvaluationResult.model_validate_json(result.model_dump_json()),
    ):
        restored.verify_request(req)
        assert restored == result
    assert (
        execute(OpenableCheckRequest.model_validate_json(spec.model_dump_json()), fixed) == result
    )
    assert spec == before
    # With no proficiency sources, the only difference is host-qualified leverage.
    ordinary = req.model_copy(
        update={
            "payload": req.payload.model_copy(
                update={"advantage": ("flag",) if spec.leverage_applicable else ()}
            )
        }
    )
    assert asyncio.run(evaluate(ordinary, loader=fixed)) == result


@pytest.mark.parametrize("problem", ["missing", "inaccessible", "empty", "owner", "slug"])
def test_invalid_tool_refuses_before_rng_and_has_null_stages(monkeypatch, problem):
    spec = operation_case()
    inventory = spec.request.state_snapshot.inventory_state
    entry = inventory.entries[0]
    changes = {
        "missing": {"instance_id": "other-instance"},
        "inaccessible": {"accessible": False},
        "empty": {"quantity": 0},
        "owner": {"owner_id": FOE},
        "slug": {"item_slug": "crowbar"},
    }
    snapshot = spec.request.state_snapshot.model_copy(
        update={
            "inventory_state": inventory.model_copy(
                update={"entries": (entry.model_copy(update=changes[problem]),)}
            )
        }
    )
    spec = spec.model_copy(
        update={"request": spec.request.model_copy(update={"state_snapshot": snapshot})}
    )
    fixed = rules.freeze_ruleset(synthetic_loader())
    forbid_rng(monkeypatch)
    result = execute(spec, fixed)
    assert result.status == "rejected"
    assert result.error.code == "tool_unavailable"
    assert result.stage_facts is result.state_delta is result.rng_transition is None
    assert result.proposed_events == ()
    RuleEvaluationResult.model_validate_json(result.model_dump_json()).verify_request(spec.request)


@pytest.mark.parametrize("problem", ["context", "combat", "actor"])
def test_unsupported_or_invalid_checks_do_not_restore_rng(monkeypatch, problem):
    spec = operation_case()
    if problem == "context":
        spec = spec.model_copy(
            update={
                "request": spec.request.model_copy(
                    update={
                        "payload": spec.request.payload.model_copy(update={"context": "search"})
                    }
                )
            }
        )
    elif problem == "combat":
        combat, _, _ = request_and_live()
        snapshot = combat.state_snapshot.model_copy(
            update={"inventory_state": spec.request.state_snapshot.inventory_state}
        )
        spec = spec.model_copy(
            update={"request": spec.request.model_copy(update={"state_snapshot": snapshot})}
        )
    else:
        spec = actor_update(spec, hp_current=0)
    fixed = rules.freeze_ruleset(synthetic_loader())
    forbid_rng(monkeypatch)
    result = execute(spec, fixed)
    assert result.status == ("rejected" if problem == "actor" else "unsupported")
    assert result.stage_facts is result.rng_transition is result.state_delta is None
    assert result.proposed_events == ()


@pytest.mark.parametrize(
    "field,value",
    [
        ("ruleset_id", "wrong"),
        ("data_revision", "sha256:" + "0" * 64),
        ("evaluator_version", "dnd5e-evaluation/36"),
    ],
)
def test_all_binding_fields_checked_before_rng(monkeypatch, field, value):
    spec = operation_case()
    spec = spec.model_copy(
        update={
            "request": spec.request.model_copy(
                update={
                    "ruleset_binding": spec.request.ruleset_binding.model_copy(
                        update={field: value}
                    )
                }
            )
        }
    )
    fixed = rules.freeze_ruleset(synthetic_loader())
    forbid_rng(monkeypatch)
    with pytest.raises(rules.RulesetBindingError):
        execute(spec, fixed)


@pytest.mark.parametrize("problem", ["version", "leverage", "tool_pair", "payload_tool"])
def test_invalid_envelopes_revalidate_before_rng(monkeypatch, problem):
    spec = operation_case()
    if problem == "version":
        spec = spec.model_copy(
            update={
                "request": spec.request.model_copy(
                    update={"schema_version": "engine-evaluation/23"}
                )
            }
        )
    elif problem == "leverage":
        spec = spec.model_copy(update={"leverage_applicable": True})
    elif problem == "tool_pair":
        spec = spec.model_copy(update={"tool_instance_id": None})
    else:
        spec = spec.model_copy(
            update={
                "request": spec.request.model_copy(
                    update={
                        "payload": spec.request.payload.model_copy(update={"tool": "thieves-tools"})
                    }
                )
            }
        )
    fixed = rules.freeze_ruleset(synthetic_loader())
    forbid_rng(monkeypatch)
    with pytest.raises(ValidationError):
        execute(spec, fixed)


@pytest.mark.parametrize(
    "tool_prof,skill_prof,expected", [(True, False, 2), (True, True, 2), (False, True, 2)]
)
def test_reliable_talent_uses_shared_tool_or_skill_eligibility(tool_prof, skill_prof, expected):
    spec = actor_update(
        operation_case(proficient=tool_prof, skill=skill_prof), reliable_talent=True
    )
    spec = spec.model_copy(
        update={
            "request": spec.request.model_copy(
                update={
                    "rng_context": spec.request.rng_context.model_copy(
                        update={"state": RNGState.capture(random.Random(2))}
                    )
                }
            )
        }
    )
    result = execute(spec, rules.freeze_ruleset(synthetic_loader()))
    event = result.proposed_events[0]
    assert event.rolled_natural < 10
    assert event.natural == 10
    assert event.reliable_talent_applied
    actor = spec.request.state_snapshot.character_states[0]
    assert event.modifier == (actor.dexterity - 10) // 2 + expected


def test_crowbar_does_not_enable_reliable_talent_or_proficiency():
    spec = actor_update(operation_case("force_open", proficient=True), reliable_talent=True)
    spec = spec.model_copy(
        update={
            "request": spec.request.model_copy(
                update={
                    "rng_context": spec.request.rng_context.model_copy(
                        update={"state": RNGState.capture(random.Random(2))}
                    )
                }
            )
        }
    )
    event = execute(spec, rules.freeze_ruleset(synthetic_loader())).proposed_events[0]
    assert event.advantage == "advantage"
    assert event.rolled_natural < 10
    assert event.natural == event.rolled_natural
    assert not event.reliable_talent_applied
    actor = spec.request.state_snapshot.character_states[0]
    assert event.modifier == (actor.strength - 10) // 2


def test_tool_proficiency_replaces_jack_half_bonus_without_adding_twice():
    spec = actor_update(operation_case(proficient=True), jack_of_all_trades=True)
    result = execute(spec, rules.freeze_ruleset(synthetic_loader()))
    actor = spec.request.state_snapshot.character_states[0]
    assert result.proposed_events[0].modifier == (actor.dexterity - 10) // 2 + 2


def test_force_open_without_tool_matches_generic_check():
    spec = operation_case("force_open").model_copy(
        update={"required_tool": None, "tool_instance_id": None, "leverage_applicable": False}
    )
    fixed = rules.freeze_ruleset(synthetic_loader())
    assert execute(spec, fixed) == asyncio.run(evaluate(spec.request, loader=fixed))


def test_frozen_openable_counts_construction_separately_from_sequential_runtime(monkeypatch):
    specs = []
    for operation in ("pick_lock", "force_open", "clear_jam"):
        spec = operation_case(operation)
        # Use real bundled equipment; the helper's synthetic blade is deliberately
        # absent from the canonical corpus and ordinary checks reject that gear.
        specs.append(
            actor_update(
                spec,
                carried_item_slugs=(spec.required_tool,),
                weapon_in_hands=None,
                weapon_grip="none",
            )
        )
    counts = Counter()
    read = BundledAssetLoader._load_json
    enumerate_source = BundledAssetLoader.list_slugs
    enumerate_fixed = rules.FrozenRuleset.list_slugs
    compute = rules._compute_binding
    validate = BaseModel.model_validate.__func__

    def counted_read(loader, category, slug):
        counts["file_reads"] += 1
        return read(loader, category, slug)

    def counted_source(loader, category):
        counts["source_enumerations"] += 1
        return enumerate_source(loader, category)

    def counted_fixed(loader, category):
        counts["fixed_enumerations"] += 1
        return enumerate_fixed(loader, category)

    def counted_compute(loader):
        counts["corpus_digests"] += 1
        return compute(loader)

    def counted_validate(cls, obj, *args, **kwargs):
        if cls.__module__.startswith("dnd5e_srd_data.schema."):
            counts["asset_constructions"] += 1
        return validate(cls, obj, *args, **kwargs)

    monkeypatch.setattr(BundledAssetLoader, "_load_json", counted_read)
    monkeypatch.setattr(BundledAssetLoader, "list_slugs", counted_source)
    monkeypatch.setattr(rules.FrozenRuleset, "list_slugs", counted_fixed)
    monkeypatch.setattr(rules, "_compute_binding", counted_compute)
    monkeypatch.setattr(BaseModel, "model_validate", classmethod(counted_validate))
    fixed = rules.freeze_ruleset(BundledAssetLoader())
    construction = counts.copy()
    assert construction["file_reads"] > 0
    assert construction["asset_constructions"] == construction["file_reads"]
    assert construction["source_enumerations"] == construction["fixed_enumerations"] == 11
    assert construction["corpus_digests"] == 1
    # The default and explicit routes must reuse exactly the same captured dataset.
    monkeypatch.setattr(rules, "_DEFAULT_RULESET", fixed)
    counts.clear()
    rng = specs[0].request.rng_context
    for spec in specs * 2:
        req = spec.request.model_copy(update={"ruleset_binding": fixed.binding, "rng_context": rng})
        spec = spec.model_copy(update={"request": req})
        result = execute(spec, fixed)
        assert result.status == "accepted", result
        assert asyncio.run(evaluate_openable(spec)) == result
        result.verify_request(req)
        rng = rng.model_copy(
            update={"version": rng.version + 1, "state": result.rng_transition.next_state}
        )
    assert counts == Counter(), (construction, counts)
