"""Canonical Staff heals use recipient effects without spending a spell slot."""

import json
from random import Random

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader

from dnd5e_engine.evaluation_contracts import STAFF_CAST_SCHEMA_VERSION, RuleEvaluationRequest
from dnd5e_engine.evaluation_delta import HPDelta, InventorySpendCharge
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_engine.events import HealingApplied
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import execute
from tests.test_stateless_healing_modifiers import _cast_beacon

STAFF = "staff-of-healing"
INSTANCE = "item.staff"
CURE, MASS = "mkIIFw98V1d8uABu", "tfGfEtth3JKF7BXQ"


@pytest.mark.parametrize("mass", [False, True])
def test_staff_healing_maximizes_only_for_beacon_recipients_without_a_life_slot_bonus(mass):
    original, assets, effect, _ = _cast_beacon()
    canonical = BundledAssetLoader()
    loader = MemoryAssetLoader(
        classes=list(assets._classes.values()),
        subclasses=list(assets._subclasses.values()),
        features=list(assets._features.values()),
        spells=[*assets._spells.values(), canonical.get_spell("mass-cure-wounds")],
        items=[canonical.get_item(STAFF)],
    )
    wire = json.loads(original.model_dump_json())
    wire.update(
        schema_version=STAFF_CAST_SCHEMA_VERSION,
        operation_kind="world.use_item",
        ruleset_binding=ruleset_binding(loader).model_dump(mode="json"),
        payload={
            "kind": "world.use_item",
            "instance_id": INSTANCE,
            "activity_id": MASS if mass else CURE,
        },
    )
    payload = wire["payload"]
    if mass:
        source, center = wire.pop("spell_positions").values()
        payload["area_target"] = {"center_cell": center, "target_ids": [FOE, HERO]}
        wire["item_area_facts"] = {
            "source_cell": source,
            "visible_center_cell": center,
            "target_cells": {FOE: center, HERO: source},
        }
    else:
        payload["target_id"] = FOE
        payload["cast_level"] = 4
    snapshot = wire["state_snapshot"]
    holder = next(actor for actor in snapshot["character_states"] if actor["entity_id"] == HERO)
    holder.update(
        carried_item_slugs=[STAFF],
        weapon_in_hands=STAFF,
        held_weapon_instance_id=INSTANCE,
        weapon_grip="two_handed",
    )
    snapshot["inventory_state"]["entries"].append(
        {
            "instance_id": INSTANCE,
            "owner_id": HERO,
            "item_slug": STAFF,
            "quantity": 1,
            "charges_remaining_per_unit": 10,
            "accessible": True,
            "attuned_actor_id": HERO,
        }
    )
    snapshot["effect_states"] = [effect.model_dump(mode="json")]
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    heal = [event for event in result.proposed_events if isinstance(event, HealingApplied)]
    assert [(event.target_id, event.amount) for event in heal] == (
        [(FOE, 43), (HERO, 3 + sum((3, 2, 5, 2, 8)))] if mass else [(FOE, 67)]
    )
    assert result.rng_transition.state_changed is mass
    rng = Random(1)
    if mass:
        for _ in range(5):
            rng.randint(1, 8)
    assert result.rng_transition.next_state == RNGState.capture(rng)
    payment, *edits = result.state_delta.operations
    assert isinstance(payment, InventorySpendCharge)
    assert payment.amount == (5 if mass else 4)
    assert all(isinstance(edit, HPDelta) for edit in edits)
