"""Public stateless evaluation of separately verified exploration targets."""

import json

import pytest
from dnd5e_srd_data import BundledAssetLoader
from dnd5e_srd_data.schema.spell import Spell
from pydantic import ValidationError

from dnd5e_engine.evaluation_contracts import (
    MULTI_SPELL_SCHEMA_VERSION,
    MultiSpellWillingnessChoice,
    RuleEvaluationRequest,
    RuleEvaluationResult,
)
from dnd5e_engine.evaluation_delta import ResourceUpdate, WorldEffectCreate
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import execute
from tests.test_stateless_timed_spells import spell_wire


def _multi_spell_wire(*, willing=False, target_cell="1,0", pending_target=FOE, count="2"):
    """Bind an authored shared walking-speed effect using the ordinary touch utility."""
    base = BundledAssetLoader().get_spell("longstrider").model_dump(mode="json")
    base.update(slug="fixture-shared-stride", foundry_uuid="", name="Fixture Shared Stride")
    base["activities"][0]["target"]["affects"].update(count=count, type="willing")
    spell = Spell.model_validate_json(json.dumps(base))
    wire, loader = spell_wire(spell)
    wire.pop("spell_positions")
    wire.update(
        schema_version=MULTI_SPELL_SCHEMA_VERSION,
        operation_kind="world.cast_spell_many",
        multi_spell_positions={
            "source_cell": "0,0",
            "targets": [
                {"target_id": HERO, "cell_id": "0,0", "willing": True},
                {"target_id": pending_target, "cell_id": target_cell, "willing": willing},
            ],
        },
    )
    wire["payload"].pop("target_id")
    wire["payload"].update(kind="world.cast_spell_many", target_ids=[HERO, pending_target])
    return wire, loader


def test_multi_spell_waits_for_each_unverified_target_and_has_no_side_effects():
    wire, loader = _multi_spell_wire()
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    result = execute(request, loader)
    assert result.status == "needs_choice", result.error
    assert result.choice == MultiSpellWillingnessChoice(
        kind="spell.targets_willingness", actor_id=HERO, target_ids=(FOE,)
    )
    assert result.rng_transition is result.state_delta is None
    assert result.proposed_events == ()
    assert RuleEvaluationResult.model_validate_json(result.model_dump_json()) == result
    assert RuleEvaluationRequest.model_validate_json(request.model_dump_json()) == request


@pytest.mark.parametrize("one_target", [True, False])
def test_multi_spell_pays_one_slot_for_only_the_selected_willing_creatures(one_target):
    wire, loader = _multi_spell_wire(willing=True)
    if one_target:
        wire["payload"]["target_ids"].pop(0)
        wire["multi_spell_positions"]["targets"].pop(0)
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert result == execute(request, loader)
    payment, *creations = result.state_delta.operations
    assert isinstance(payment, ResourceUpdate)
    assert payment.amount == -1
    assert all(isinstance(op, WorldEffectCreate) for op in creations)
    assert [op.value.effect.target_id for op in creations] == wire["payload"]["target_ids"]
    assert result.rng_transition.next_state == request.rng_context.state
    assert sum(event.type == "spell_cast" for event in result.proposed_events) == 1
    assert RuleEvaluationResult.model_validate_json(result.model_dump_json()) == result


def test_multi_spell_invalid_target_refuses_before_payment_or_prompt():
    wire, loader = _multi_spell_wire(target_cell="2,0")
    result = execute(RuleEvaluationRequest.model_validate_json(json.dumps(wire)), loader)
    assert (result.status, result.error.code) == ("rejected", "target_invalid")
    assert result.choice is result.rng_transition is result.state_delta is None


def test_multi_spell_refuses_target_count_above_its_ruleset_capacity():
    wire, loader = _multi_spell_wire(count="1")
    result = execute(RuleEvaluationRequest.model_validate_json(json.dumps(wire)), loader)
    assert (result.status, result.error.code) == ("rejected", "spell.target_count")
    assert result.choice is result.rng_transition is result.state_delta is None


def test_multi_spell_wire_rejects_duplicate_or_mismatched_targets():
    wire, _ = _multi_spell_wire()
    wire["payload"]["target_ids"] = [FOE, FOE]
    with pytest.raises(ValidationError):
        RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    wire["payload"]["target_ids"] = [FOE, HERO]
    with pytest.raises(ValidationError, match="matching exploration spell target facts"):
        RuleEvaluationRequest.model_validate_json(json.dumps(wire))


def test_multi_spell_result_rejects_a_choice_for_the_caster():
    wire, loader = _multi_spell_wire()
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    result = execute(request, loader)
    forged_choice = result.choice.model_copy(update={"target_ids": (HERO,)})
    forged = result.model_copy(update={"choice": forged_choice})
    with pytest.raises(ValueError, match="unverified targets"):
        forged.verify_request(request)
