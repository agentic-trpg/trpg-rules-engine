"""The scoped cast extension appends only the exact evidence-triggered ending."""

import json

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader

from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest, RuleEvaluationResult
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_ruleset import freeze_ruleset, ruleset_binding
from dnd5e_engine.evaluation_spell_termination import SpellApplicationFacts, WorldSpellEnd
from dnd5e_engine.evaluation_timed_spells import _passive_profile
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import execute
from tests.test_stateless_environmental_spells import _wire


def test_successful_light_cast_binds_ending_and_rejects_tampering():
    """A slotless cast ends the target's actual Invisibility even from another source."""
    wire, _ = _wire("light")
    assets = BundledAssetLoader()
    invisibility = assets.get_spell("invisibility")
    _, active, _ = _passive_profile(invisibility, target_id=HERO, caster_id=FOE)
    effect = effect_state(active).model_dump(mode="json")
    loader = freeze_ruleset(
        MemoryAssetLoader(
            items=[assets.get_item("component-pouch")],
            classes=[assets.get_class("wizard")],
            spells=[invisibility, assets.get_spell("light")],
        )
    )
    wire["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    wire["state_snapshot"]["effect_states"].append(effect)
    native_version = wire["schema_version"]
    wire.update(
        schema_version="engine-evaluation/37",
        spell_end_facts={
            "cast_schema_version": native_version,
            "applications": [
                {
                    "effect": effect,
                    "caster_id": FOE,
                    "spell_id": "invisibility",
                    "cast_command_id": "original-cast",
                    "expires_tick": 3600,
                    "concentration_id": "actor:original-cast",
                }
            ],
        },
    )
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    create, ending = result.state_delta.operations
    assert create.kind == "world.environmental_effect_create"
    expected = SpellApplicationFacts.model_validate_json(
        json.dumps(wire["spell_end_facts"]["applications"][0])
    )
    assert ending == WorldSpellEnd(expected=expected, reason="spell_cast")
    assert not result.rng_transition.state_changed
    RuleEvaluationResult.model_validate_json(result.model_dump_json()).verify_request(request)
    for bad_operations in ((create,), (create, ending, ending)):
        wrong = result.model_copy(
            update={
                "state_delta": result.state_delta.model_copy(update={"operations": bad_operations})
            }
        )
        with pytest.raises(ValueError):
            wrong.verify_request(request)

    wire["spell_positions"]["target_item"]["size"] = "huge"
    refused = execute(RuleEvaluationRequest.model_validate_json(json.dumps(wire)), loader)
    assert (refused.status, refused.error.code) == ("rejected", "target_invalid")
    assert refused.state_delta is None
    assert refused.proposed_events == ()
