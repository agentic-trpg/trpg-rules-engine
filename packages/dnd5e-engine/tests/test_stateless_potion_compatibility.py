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
from tests.test_stateless_items import ACTIVITY, HEALING_POTIONS, POTION
from tests.test_stateless_spell_feature import apply_healing

FIRST = "hero:first-potion"
SPARE = "hero:spare-potion"


def potion_cleric(
    *,
    spell="cure-wounds",
    spare=True,
    offhand=False,
    potion_changes=None,
    seed=0,
    potion_slug=POTION,
):
    """Build the reviewed Cleric with explicit armor, weapon, and optional spare stock."""
    original, base, _, _ = equipped_spell_case(spell=spell, weapon="mace", seed=seed)
    potion = BundledAssetLoader().get_item(potion_slug)
    if potion_changes:
        potion = potion.model_copy(update=potion_changes)
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
        hero["carried_item_slugs"].append(potion_slug)
        snapshot["inventory_state"]["entries"].extend(
            {
                "instance_id": instance_id,
                "owner_id": HERO,
                "item_slug": potion_slug,
                "quantity": 1,
                "charges_remaining_per_unit": 1,
                "accessible": True,
            }
            for instance_id in (FIRST, SPARE)
        )
    return request, hero, loader


def drink_request(wire, *, activity_id=ACTIVITY):
    """Select the exact first bottle on the current Cleric turn."""
    wire.update(
        operation_kind="combat.item",
        payload={
            "kind": "combat.item",
            "instance_id": FIRST,
            "activity_id": activity_id,
            "target_id": HERO,
        },
    )
    return RuleEvaluationRequest.model_validate_json(json.dumps(wire))


@pytest.mark.parametrize(("slug", "dice", "bonus"), HEALING_POTIONS)
def test_equipped_cleric_drinks_exact_bottle_and_pays_bonus_only(slug, dice, bonus):
    wire, _, loader = potion_cleric(potion_slug=slug)
    request = drink_request(wire, activity_id=loader.get_item(slug).activities[0].id)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    consume = [op for op in result.state_delta.operations if isinstance(op, InventoryConsume)]
    assert len(consume) == 1
    assert consume[0].expected.instance_id == FIRST
    assert consume[0].expected.item_slug == slug
    assert consume[0].value.quantity == 0
    assert any(isinstance(op, HPDelta) for op in result.state_delta.operations)
    (healing,) = (e for e in result.proposed_events if e.type == "healing_applied")
    assert dice + bonus <= healing.amount <= 4 * dice + bonus
    budget = next(op for op in result.state_delta.operations if isinstance(op, ActionBudgetUpdate))
    assert budget.expected.bonus_action_available
    assert not budget.value.bonus_action_available
    assert budget.expected.action_available == budget.value.action_available
    assert result.rng_transition.state_changed
    assert execute(request, loader) == result


@pytest.mark.parametrize("change", ["offhand", "noncanonical"])
def test_equipped_class_keeps_unproven_healing_stock_in_equipment_validation(change, monkeypatch):
    slug = "greater-potion-of-healing"
    wire, _, loader = potion_cleric(
        potion_slug=slug,
        offhand=change == "offhand",
        potion_changes={"description": "Different bound greater healing asset."}
        if change == "noncanonical"
        else None,
    )
    request = drink_request(wire, activity_id=loader.get_item(slug).activities[0].id)
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("refusal restored RNG"))

    result = execute(request, loader)
    assert result.status == "unsupported"
    assert result.error.code == "item.snapshot"
    assert result.state_delta is result.rng_transition is None
    assert not result.proposed_events


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


def test_sacred_flame_with_stowed_potions_matches_no_stock():
    """Stowed stock has no effect on save, damage, RNG or payment proposals."""
    wire, _, loader = potion_cleric(spell="sacred-flame", seed=2)
    baseline, _, _ = potion_cleric(spell="sacred-flame", seed=2, spare=False)
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    control = RuleEvaluationRequest.model_validate_json(json.dumps(baseline))

    result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert result == execute(control, loader)
    assert result.rng_transition.state_changed
    assert any(
        isinstance(op, HPDelta) and op.target_id == FOE and op.amount < 0
        for op in result.state_delta.operations
    )
    assert not any(isinstance(op, InventoryConsume) for op in result.state_delta.operations)


@pytest.mark.parametrize("spell", ["cure-wounds", "sacred-flame"])
@pytest.mark.parametrize("change", ["offhand", "attuned", "custom", "different_owner"])
def test_spare_allowance_requires_owned_stowed_canonical_stock(spell, change, monkeypatch):
    overrides = {
        "attuned": {"requires_attunement": True},
        "custom": {"description": "Different bound Potion of Healing asset."},
    }
    wire, _, loader = potion_cleric(
        spell=spell,
        offhand=change == "offhand",
        potion_changes=overrides.get(change),
    )
    if change == "different_owner":
        snapshot = wire["state_snapshot"]
        target = next(a for a in snapshot["character_states"] if a["entity_id"] == FOE)
        target["carried_item_slugs"] = [POTION]
        # The caster still falsely projects these units as its own stock.
        for entry in snapshot["inventory_state"]["entries"]:
            if entry["item_slug"] == POTION:
                entry["owner_id"] = FOE
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("refusal restored RNG"))
    result = execute(request, loader)
    assert result.error.code == "healing.dependencies", result.error
    assert result.state_delta is None
    assert result.rng_transition is None
    assert not result.proposed_events


@pytest.mark.parametrize("evaluator_version", ["dnd5e-evaluation/40", "dnd5e-evaluation/41"])
def test_previous_evaluator_binding_cannot_adopt_new_admission(monkeypatch, evaluator_version):
    wire, _, loader = potion_cleric()
    wire["ruleset_binding"]["evaluator_version"] = evaluator_version
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
