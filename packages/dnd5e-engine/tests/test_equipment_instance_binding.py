"""New equipment instance wire binds mechanical facts without activating spares."""

import pytest
from pydantic import ValidationError

from dnd5e_engine.evaluation_availability import (
    ActionAvailabilityRequest,
    query_action_availability,
)
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd5e_engine.evaluation_state import CombatSnapshot, InventoryEntry, InventoryState
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import execute
from tests.test_stateless_equipped_class_attack import equipped_case


def instance_case(*, shield=True, offhand=False, offhand_slug="component-pouch", spare=True):
    """Bind the selected profile to inventory IDs, optionally retaining inert spares."""
    request, loader, _, _ = equipped_case(shield=shield)
    prior = request.state_snapshot
    entries = list(prior.inventory_state.entries)
    if spare:
        entries.extend(
            entry.model_copy(update={"instance_id": entry.instance_id + ":spare"})
            for entry in prior.inventory_state.entries
            if entry.item_slug in {"chain-shirt", "shield"}
        )
    if offhand:
        entries.append(
            InventoryEntry(
                instance_id="hero:pouch",
                owner_id=HERO,
                item_slug=offhand_slug,
                quantity=1,
                charges_remaining_per_unit=0,
                accessible=True,
            )
        )
        if spare:
            entries.append(entries[-1].model_copy(update={"instance_id": "hero:pouch:spare"}))
    actors = tuple(
        actor.model_copy(
            update={
                "held_weapon_instance_id": "hero:mace",
                "worn_armor_instance_id": "hero:chain-shirt",
                "equipped_shield_instance_id": "hero:shield" if shield else None,
                "offhand_item_instance_id": "hero:pouch" if offhand else None,
                "other_hand_occupied": shield or offhand,
                "carried_item_slugs": (
                    *actor.carried_item_slugs,
                    *((offhand_slug,) if offhand else ()),
                ),
            }
        )
        if actor.entity_id == HERO
        else actor
        for actor in prior.character_states
    )
    snapshot = CombatSnapshot.model_validate(
        prior.model_copy(
            update={
                "snapshot_schema_version": "engine-snapshot/19",
                "character_states": actors,
                "inventory_state": InventoryState(entries=tuple(entries)),
            }
        )
    )
    request = request.model_copy(
        update={
            "schema_version": "engine-evaluation/26",
            "state_snapshot": snapshot,
            "payload": CombatIntentPayload(
                intent_type="attack", weapon_instance_id="hero:mace", target_id=FOE
            ),
        }
    )
    return request, loader


@pytest.mark.parametrize(
    "shield,offhand,offhand_slug",
    [(True, False, "component-pouch"), (False, True, "component-pouch"), (False, True, "mace")],
)
def test_bound_equipment_ignores_same_type_spares(shield, offhand, offhand_slug):
    request, loader = instance_case(shield=shield, offhand=offhand, offhand_slug=offhand_slug)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    query = ActionAvailabilityRequest(
        schema_version="engine-availability/15",
        session_id=request.session_id,
        operation_kind="combat.intent",
        actor_id=HERO,
        payload=request.payload,
        state_snapshot=request.state_snapshot,
        ruleset_binding=request.ruleset_binding,
    )
    assert query_action_availability(query, loader=loader).status == "available"
    actor = next(a for a in request.state_snapshot.character_states if a.entity_id == HERO)
    wire = actor.model_dump(mode="json")
    assert wire["worn_armor_instance_id"] == "hero:chain-shirt"
    assert ("offhand_item_instance_id" in wire) == offhand


@pytest.mark.parametrize(
    "field,value",
    [
        ("worn_armor_instance_id", None),
        ("worn_armor_instance_id", "hero:shield"),
        ("worn_armor_instance_id", "hero:mace"),
        ("equipped_shield_instance_id", "hero:chain-shirt"),
        ("offhand_item_instance_id", "hero:shield"),
    ],
)
def test_equipment_binding_rejects_missing_or_conflicting_ids(field, value):
    request, _ = instance_case()
    snapshot = request.state_snapshot
    actors = tuple(
        actor.model_copy(update={field: value}) if actor.entity_id == HERO else actor
        for actor in snapshot.character_states
    )
    with pytest.raises(ValidationError):
        CombatSnapshot.model_validate(snapshot.model_copy(update={"character_states": actors}))


def test_bound_loader_rejects_wrong_armor_kind_on_target():
    request, loader = instance_case()
    snapshot = request.state_snapshot
    target = next(actor for actor in snapshot.character_states if actor.entity_id == FOE)
    actors = tuple(
        actor.model_copy(
            update={
                "worn_armor": "medium",
                "worn_armor_instance_id": "foe:bad-armor",
                "carried_item_slugs": (*actor.carried_item_slugs, "mace"),
            }
        )
        if actor.entity_id == FOE
        else actor
        for actor in snapshot.character_states
    )
    assert not target.classes
    assert target.class_slug is None
    entries = (
        *snapshot.inventory_state.entries,
        InventoryEntry(
            instance_id="foe:bad-armor",
            owner_id=FOE,
            item_slug="mace",
            quantity=1,
            charges_remaining_per_unit=0,
            accessible=True,
        ),
    )
    changed = CombatSnapshot.model_validate(
        snapshot.model_copy(
            update={
                "character_states": actors,
                "inventory_state": InventoryState(entries=entries),
            }
        )
    )
    result = execute(request.model_copy(update={"state_snapshot": changed}), loader)
    assert result.status == "unsupported"
    assert result.error.code == "snapshot.capability"
