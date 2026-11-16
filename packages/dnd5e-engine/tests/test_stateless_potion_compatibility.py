"""Reviewed character drinking and spare potion isolation through public evaluation."""

import json

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader

from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest
from dnd5e_engine.evaluation_delta import ActionBudgetUpdate, HPDelta, InventoryConsume
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import RulesetBindingError, ruleset_binding
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import execute
from tests.test_stateless_casting_v20 import canonical_healing_wire
from tests.test_stateless_equipped_spells import equipped_spell_case
from tests.test_stateless_items import ACTIVITY, POTION
from tests.test_stateless_spell_feature import apply_healing

FIRST = "hero:first-potion"
SPARE = "hero:spare-potion"


def potion_cleric(*, spell="cure-wounds", spare=True, offhand=False, custom=False):
    """Build the reviewed Cleric with explicit armor, weapon, and optional spare stock."""
    original, base, _, _ = equipped_spell_case(spell=spell, weapon="mace")
    potion = BundledAssetLoader().get_item(POTION)
    if custom:
        potion = potion.model_copy(update={"requires_attunement": True})
    loader = MemoryAssetLoader(
        classes=[base.get_class(s) for s in base.list_slugs("classes")],
        features=[base.get_feature(s) for s in base.list_slugs("features")],
        items=[*(base.get_item(s) for s in base.list_slugs("items")), potion],
        spells=[base.get_spell(s) for s in base.list_slugs("spells")],
    )
    request, _ = canonical_healing_wire(original)
    request["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    snapshot = request["state_snapshot"]
    hero = next(actor for actor in snapshot["character_states"] if actor["entity_id"] == HERO)
    hero.update(
        held_weapon_instance_id="hero:mace",
        worn_armor_instance_id="hero:chain-shirt",
        offhand_item_instance_id=FIRST if offhand else None,
        other_hand_occupied=offhand,
    )
    if spare:
        hero["carried_item_slugs"].append(POTION)
        snapshot["inventory_state"]["entries"].extend(
            {
                "instance_id": instance_id,
                "owner_id": HERO,
                "item_slug": POTION,
                "quantity": 1,
                "charges_remaining_per_unit": 1,
                "accessible": True,
            }
            for instance_id in (FIRST, SPARE)
        )
    return request, hero, loader


def drink_request(wire):
    """Select the exact first bottle on the current Cleric turn."""
    wire.update(
        operation_kind="combat.item",
        payload={
            "kind": "combat.item",
            "instance_id": FIRST,
            "activity_id": ACTIVITY,
            "target_id": HERO,
        },
    )
    return RuleEvaluationRequest.model_validate_json(json.dumps(wire))


def test_equipped_cleric_drinks_exact_bottle_and_pays_bonus_only():
    wire, _, loader = potion_cleric()
    request = drink_request(wire)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    consume = [op for op in result.state_delta.operations if isinstance(op, InventoryConsume)]
    assert len(consume) == 1
    assert consume[0].expected.instance_id == FIRST
    assert consume[0].value.quantity == 0
    assert any(isinstance(op, HPDelta) for op in result.state_delta.operations)
    budget = next(op for op in result.state_delta.operations if isinstance(op, ActionBudgetUpdate))
    assert budget.expected.bonus_action_available
    assert not budget.value.bonus_action_available
    assert budget.expected.action_available == budget.value.action_available
    assert result.rng_transition.state_changed
    assert execute(request, loader) == result


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("spent_bonus", "no_action_economy"),
        ("untrained_grant", "item.snapshot"),
        ("inaccessible", "item_unauthorized"),
    ],
)
def test_combat_drinking_still_validates_actual_dependencies(change, code, monkeypatch):
    wire, hero, loader = potion_cleric()
    if change == "spent_bonus":
        hero["bonus_action_available"] = False
    elif change == "untrained_grant":
        hero["granted_features"].append("second-wind")
    else:
        entry = next(
            e
            for e in wire["state_snapshot"]["inventory_state"]["entries"]
            if e["instance_id"] == FIRST
        )
        entry["accessible"] = False
    request = drink_request(wire)
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("refusal restored RNG"))
    result = execute(request, loader)
    assert result.error.code == code, result.error
    assert result.state_delta is None
    assert result.rng_transition is None
    assert not result.proposed_events


def test_cure_wounds_leaves_spare_potions_and_uses_one_spell_slot():
    wire, _, loader = potion_cleric()
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert not any(isinstance(op, InventoryConsume) for op in result.state_delta.operations)
    after = apply_healing(request.state_snapshot, result)
    assert after.inventory_state == request.state_snapshot.inventory_state
    hero = next(actor for actor in after.character_states if actor.entity_id == HERO)
    assert hero.spell_slots == {1: 1}
    assert any(isinstance(op, HPDelta) for op in result.state_delta.operations)


@pytest.mark.parametrize(
    "options", [{"offhand": True}, {"custom": True}, {"spell": "sacred-flame"}]
)
def test_spare_allowance_is_limited_to_inert_stock_for_healing(options, monkeypatch):
    wire, _, loader = potion_cleric(**options)
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("refusal restored RNG"))
    result = execute(request, loader)
    assert result.error.code == "healing.dependencies", result.error
    assert result.state_delta is None
    assert result.rng_transition is None
    assert not result.proposed_events


def test_previous_evaluator_binding_cannot_adopt_new_admission(monkeypatch):
    wire, _, loader = potion_cleric()
    wire["ruleset_binding"]["evaluator_version"] = "dnd5e-evaluation/40"
    request = drink_request(wire)
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("refusal restored RNG"))
    with pytest.raises(RulesetBindingError, match="effective rules/evaluator"):
        execute(request, loader)


def test_unclassed_targets_equipped_potion_is_not_spare_stock(monkeypatch):
    wire, _, loader = potion_cleric()
    snapshot = wire["state_snapshot"]
    target = next(actor for actor in snapshot["character_states"] if actor["entity_id"] == FOE)
    assert not target["classes"]
    target.update(
        weapon_in_hands=POTION,
        weapon_grip="one_handed",
        held_weapon_instance_id=SPARE,
        carried_item_slugs=[POTION],
    )
    entry = next(e for e in snapshot["inventory_state"]["entries"] if e["instance_id"] == SPARE)
    entry["owner_id"] = FOE
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("refusal restored RNG"))
    result = execute(request, loader)
    assert result.error.code == "healing.dependencies", result.error
    assert result.state_delta is None
    assert result.rng_transition is None


def test_unarmored_cleric_does_not_excuse_a_lying_potion_projection(monkeypatch):
    wire, hero, loader = potion_cleric()
    inventory = wire["state_snapshot"]["inventory_state"]
    inventory["entries"] = [e for e in inventory["entries"] if e["item_slug"] == POTION]
    hero.update(
        weapon_in_hands=None,
        weapon_grip="none",
        held_weapon_instance_id=None,
        worn_armor=None,
        worn_armor_instance_id=None,
        carried_item_slugs=[],
    )
    request = drink_request(wire)
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("refusal restored RNG"))
    result = execute(request, loader)
    assert result.error.code == "item.snapshot", result.error
    assert result.error.reason == "supplied equipment and inventory identities disagree"
    assert result.rng_transition is None
