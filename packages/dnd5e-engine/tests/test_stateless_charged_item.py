"""Bound charged item casts through the public stateless evaluator."""

import json

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd5e_srd_data.schema.common import (
    ActivationBlock,
    CastActivity,
    CastSpellBlock,
    ConsumptionBlock,
    ConsumptionTargetEntry,
)
from dnd5e_srd_data.schema.feature import RecoveryRule
from dnd5e_srd_data.schema.item import ItemUses, MagicItem

from dnd5e_engine.evaluation_contracts import (
    CHARGED_ITEM_SCHEMA_VERSION,
    RuleEvaluationRequest,
    RuleEvaluationResult,
)
from dnd5e_engine.evaluation_delta import HPDelta, InventorySpendCharge
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import freeze_ruleset, ruleset_binding
from dnd5e_engine.events import HealingApplied, SpellCast
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import execute
from tests.test_stateless_items import exploration_item_wire

ITEM = "test-healing-charm"
ACTIVITY = "test-healing-charm-cast"


def charged_wire(*, charges=2, cost=1, spell_slot=False, recovery=()):
    """Bind a typed authored item to the actual SRD Cure Wounds definition."""
    wire, _ = exploration_item_wire(charges=charges)
    spell = BundledAssetLoader().get_spell("cure-wounds")
    activity = CastActivity(
        id=ACTIVITY,
        activation=ActivationBlock(type="action", value=1),
        consumption=ConsumptionBlock(
            spell_slot=spell_slot,
            targets=[ConsumptionTargetEntry(type="itemUses", value=str(cost))],
        ),
        spell=CastSpellBlock(
            ability="wis", level=1, properties=[], spellbook=False, uuid=spell.foundry_uuid
        ),
    )
    item = MagicItem(
        slug=ITEM,
        name="Healing Charm",
        description="Authored exploration healing fixture.",
        weight=0,
        cost_gp=0,
        rarity="common",
        provenance=spell.provenance,
        review=spell.review,
        uses=ItemUses(max="3", recovery=list(recovery)),
        activities=[activity],
    )
    loader = freeze_ruleset(MemoryAssetLoader(items=[item], spells=[spell]))
    wire.update(
        schema_version=CHARGED_ITEM_SCHEMA_VERSION,
        ruleset_binding=ruleset_binding(loader).model_dump(mode="json"),
        spell_positions={"source_cell": "0,0", "target_cell": "1,0"},
    )
    wire["payload"].update(activity_id=ACTIVITY, target_id=FOE)
    snapshot = wire["state_snapshot"]
    for entry in snapshot["inventory_state"]["entries"]:
        entry["item_slug"] = ITEM
    actors = {actor["entity_id"]: actor for actor in snapshot["character_states"]}
    actors[HERO].update(carried_item_slugs=[ITEM], wisdom=16)
    actors[FOE]["hp_current"] = 1
    return wire, loader


@pytest.mark.parametrize(("charges", "cost"), [(3, 1), (1, 1), (2, 2)])
def test_charged_cast_uses_exact_instance_and_shared_healing(charges, cost) -> None:
    wire, loader = charged_wire(charges=charges, cost=cost)
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert execute(request, loader) == result
    assert RuleEvaluationResult.model_validate_json(result.model_dump_json()) == result
    spend, hp = result.state_delta.operations
    assert isinstance(spend, InventorySpendCharge)
    assert spend.expected == request.state_snapshot.inventory_state.entries[0]
    assert spend.amount == cost
    assert spend.value == spend.expected.model_copy(
        update={"charges_remaining_per_unit": charges - cost}
    )
    assert isinstance(hp, HPDelta)
    rng = request.rng_context.state.restore()
    amount = rng.randint(1, 8) + rng.randint(1, 8) + 3
    assert result.proposed_events[-1] == HealingApplied(target_id=FOE, amount=amount)
    assert hp.target_id == FOE
    assert hp.resulting_hp == min(
        1 + amount,
        next(a.hp_max for a in request.state_snapshot.character_states if a.entity_id == FOE),
    )
    cast = result.proposed_events[1]
    assert isinstance(cast, SpellCast)
    assert (cast.spell_id, cast.slot_level, cast.components) == ("cure-wounds", None, [])
    assert result.rng_transition.next_state == RNGState.capture(rng)


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        ("inaccessible", "item_unauthorized"),
        ("other_owner", "item_unauthorized"),
        ("activity", "activity_invalid"),
        ("blocked", "target_invalid"),
        ("exhausted", "item_exhausted"),
        ("wrong_payment", "item.capability"),
        ("over_capacity", "item.charges"),
    ],
)
def test_charged_item_refusal_has_no_mechanical_proposal(fault, code) -> None:
    wire, loader = charged_wire(spell_slot=fault == "wrong_payment")
    snapshot = wire["state_snapshot"]
    entry = snapshot["inventory_state"]["entries"][0]
    if fault == "inaccessible":
        entry["accessible"] = False
    elif fault == "other_owner":
        entry["owner_id"] = FOE
        next(actor for actor in snapshot["character_states"] if actor["entity_id"] == FOE)[
            "carried_item_slugs"
        ] = [ITEM]
    elif fault == "activity":
        wire["payload"]["activity_id"] = "different-activity"
    elif fault == "blocked":
        snapshot["scene_state"]["grid"]["wall_segments"] = [{"x1": 1, "y1": 0, "x2": 1, "y2": 2}]
    elif fault == "exhausted":
        entry["charges_remaining_per_unit"] = 0
    elif fault == "over_capacity":
        entry["charges_remaining_per_unit"] = 4
    result = execute(RuleEvaluationRequest.model_validate_json(json.dumps(wire)), loader)
    assert result.error.code == code
    assert result.state_delta is None
    assert result.proposed_events == ()
    assert result.rng_transition is None


def test_dawn_recovery_item_uses_the_same_charge_payment() -> None:
    wire, loader = charged_wire(
        charges=1, recovery=(RecoveryRule(period="dawn", type="recoverAll"),)
    )
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    result = execute(request, loader)

    assert result.status == "accepted", result.error
    payment = result.state_delta.operations[0]
    assert isinstance(payment, InventorySpendCharge)
    assert payment.expected == request.state_snapshot.inventory_state.entries[0]
    assert payment.value.charges_remaining_per_unit == 0
    assert isinstance(result.proposed_events[-1], HealingApplied)


@pytest.mark.parametrize(
    "recovery",
    [
        (RecoveryRule(period="dusk", type="recoverAll"),),
        (RecoveryRule(period="dawn", type="formula", formula="1d3"),),
        (RecoveryRule(period="dawn", type="recoverAll", formula="3"),),
        (RecoveryRule(period="dawn"), RecoveryRule(period="lr")),
    ],
)
def test_unreviewed_item_recovery_does_not_enable_charged_cast(recovery) -> None:
    wire, loader = charged_wire(recovery=recovery)
    result = execute(RuleEvaluationRequest.model_validate_json(json.dumps(wire)), loader)

    assert result.status == "unsupported"
    assert result.error.code == "item.capability"
    assert result.state_delta is None
    assert result.rng_transition is None


def test_charged_cast_still_spends_and_rolls_at_full_hp() -> None:
    wire, loader = charged_wire()
    target = next(a for a in wire["state_snapshot"]["character_states"] if a["entity_id"] == FOE)
    target["hp_current"] = target["hp_max"]
    result = execute(RuleEvaluationRequest.model_validate_json(json.dumps(wire)), loader)
    assert result.status == "accepted", result.error
    (payment,) = result.state_delta.operations
    assert isinstance(payment, InventorySpendCharge)
    assert isinstance(result.proposed_events[-1], HealingApplied)
    assert result.rng_transition.state_changed


def test_charged_cast_verifier_rejects_an_identical_spare_as_payment() -> None:
    wire, loader = charged_wire(charges=1)
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    result = execute(request, loader)
    payment, hp = result.state_delta.operations
    spare = request.state_snapshot.inventory_state.entries[1]
    wrong_payment = payment.model_copy(
        update={
            "expected": spare,
            "value": spare.model_copy(update={"charges_remaining_per_unit": 0}),
        }
    )
    forged = result.model_copy(
        update={
            "state_delta": result.state_delta.model_copy(update={"operations": (wrong_payment, hp)})
        }
    )
    with pytest.raises(ValueError, match="exact owned request instance"):
        forged.verify_request(request)


def test_charged_cast_rejects_a_uuid_result_outside_the_bound_corpus() -> None:
    wire, frozen = charged_wire()
    spell = frozen.get_spell("cure-wounds")

    class MismatchedUUIDLoader(MemoryAssetLoader):
        """Expose an unbound spell variant only through the UUID accessor."""

        def get_spell_by_uuid(self, uuid):
            """Return the mismatched spell for the otherwise correct UUID."""
            candidate = super().get_spell_by_uuid(uuid)
            return candidate.model_copy(update={"name": "Unbound variant"})

    loader = MismatchedUUIDLoader(items=[frozen.get_item(ITEM)], spells=[spell])
    assert ruleset_binding(loader) == ruleset_binding(frozen)
    result = execute(RuleEvaluationRequest.model_validate_json(json.dumps(wire)), loader)
    assert result.status == "unsupported"
    assert result.error.code == "item.capability"
    assert result.state_delta is None
    assert result.rng_transition is None
