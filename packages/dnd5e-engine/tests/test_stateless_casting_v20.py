"""Canonical source-owned spell facts and legacy casting compatibility."""

import json

import pytest
from pydantic import ValidationError

from dnd5e_engine.evaluation_casting import (
    ActorCastingStateV20,
    ClassSpellSourceV20,
    SpellMaterial,
    SpellSelection,
)
from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest
from dnd5e_engine.evaluation_snapshot import capture_evaluation_snapshot
from dnd5e_engine.events import HealingApplied
from dnd5e_engine.specs import GridScene
from tests.test_stateless_attack import execute
from tests.test_stateless_spell_feature import healing_case


def canonical_healing_wire(request=None) -> tuple[dict, object]:
    """Upgrade a real Cleric 1 fixture to the complete /20 casting wire."""
    if request is None:
        request, loader, _, _ = healing_case()
    else:
        loader = None
    wire = json.loads(request.model_dump_json())
    wire["schema_version"] = "engine-evaluation/27"
    snapshot = wire["state_snapshot"]
    snapshot["snapshot_schema_version"] = "engine-snapshot/20"
    for actor in snapshot["character_states"]:
        del actor["spells_known"]
    source = snapshot["casting_state"]["actors"][0]["sources"][0]
    known = source.pop("known_spells")
    prepared = source.pop("prepared_spells")
    source["spells"] = [
        {
            "spell_id": spell_id,
            "prepared_order": prepared.index(spell_id) if spell_id in prepared else None,
            "material": None,
        }
        for spell_id in known
    ]
    snapshot["casting_state"]["actors"][0].pop("materials")
    return wire, loader


def test_empty_casting_state_selects_versioned_shape() -> None:
    """Empty actors are unambiguous at each version boundary and survive transport."""
    old, _, _, _ = healing_case()
    legacy = json.loads(old.model_dump_json())
    legacy["state_snapshot"]["casting_state"] = {"actors": []}
    for actor in legacy["state_snapshot"]["character_states"]:
        actor["spells_known"] = []
    old_request = RuleEvaluationRequest.model_validate_json(json.dumps(legacy))
    assert old_request.state_snapshot.casting_state.__class__.__name__ == "CastingState"
    assert RuleEvaluationRequest.model_validate_json(old_request.model_dump_json()) == old_request

    canonical, _ = canonical_healing_wire()
    canonical["state_snapshot"]["casting_state"] = {"actors": []}
    new_request = RuleEvaluationRequest.model_validate_json(json.dumps(canonical))
    assert new_request.state_snapshot.casting_state.__class__.__name__ == "CastingStateV20"
    assert RuleEvaluationRequest.model_validate_json(new_request.model_dump_json()) == new_request


def test_snapshot_recapture_preserves_canonical_casting_and_equipment() -> None:
    """The Legacy parity adapter retains /20 sources without restoring the actor mirror."""
    legacy, _, _, live = healing_case()
    wire, _ = canonical_healing_wire(legacy)
    actor = wire["state_snapshot"]["character_states"][0]
    actor.update(
        weapon_in_hands="mace",
        weapon_grip="one_handed",
        carried_item_slugs=["mace", "chain-shirt", "shield"],
        worn_armor="medium",
        shield_equipped=True,
        other_hand_occupied=True,
        held_weapon_instance_id="item:mace",
        worn_armor_instance_id="item:armor",
        equipped_shield_instance_id="item:shield",
    )
    wire["state_snapshot"]["inventory_state"]["entries"] = [
        {
            "instance_id": instance_id,
            "owner_id": actor["entity_id"],
            "item_slug": slug,
            "quantity": 1,
            "charges_remaining_per_unit": 0,
            "accessible": True,
        }
        for instance_id, slug in (
            ("item:mace", "mace"),
            ("item:armor", "chain-shirt"),
            ("item:shield", "shield"),
        )
    ]
    live.initiative[0] = live.initiative[0].model_copy(
        update={
            "weapon_in_hands": "mace",
            "weapon_grip": "one_handed",
            "carried_item_slugs": ("mace", "chain-shirt", "shield"),
            "worn_armor": "medium",
            "shield_equipped": True,
            "other_hand_occupied": True,
        }
    )
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    recaptured = capture_evaluation_snapshot(
        live, request.state_snapshot, GridScene(width=3, height=3)
    )
    assert recaptured.snapshot_schema_version == "engine-snapshot/20"
    assert recaptured.casting_state == request.state_snapshot.casting_state
    assert (
        recaptured.character_states[0].held_weapon_instance_id
        == request.state_snapshot.character_states[0].held_weapon_instance_id
    )
    assert (
        recaptured.character_states[0].worn_armor_instance_id
        == request.state_snapshot.character_states[0].worn_armor_instance_id
    )
    assert all(
        "spells_known" not in actor.model_fields_set for actor in recaptured.character_states
    )
    assert (
        recaptured.model_dump(mode="json")["casting_state"]
        == wire["state_snapshot"]["casting_state"]
    )


def test_nested_actor_serialization_filters_still_apply() -> None:
    """Old and new snapshots preserve caller-selected actor fields and indices."""
    old, _, _, _ = healing_case()
    canonical, _ = canonical_healing_wire(old)
    new = RuleEvaluationRequest.model_validate_json(json.dumps(canonical))
    for snapshot in (old.state_snapshot, new.state_snapshot):
        assert snapshot.model_dump(include={"character_states": {0: {"entity_id"}}}) == {
            "character_states": ({"entity_id": snapshot.character_states[0].entity_id},)
        }


def test_canonical_casting_roundtrip_and_real_heal() -> None:
    """The new wire is complete and executable without duplicate spell facts."""
    wire, loader = canonical_healing_wire()
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    out = json.loads(request.model_dump_json())
    assert out["state_snapshot"]["casting_state"] == wire["state_snapshot"]["casting_state"]
    assert all("spells_known" not in actor for actor in out["state_snapshot"]["character_states"])
    assert RuleEvaluationRequest.model_validate_json(json.dumps(out)) == request
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert any(isinstance(event, HealingApplied) for event in result.proposed_events)


def test_inline_hold_person_material_preserves_reviewed_binding() -> None:
    """Inline material projects to the existing owned-pouch check without a second wire field."""
    entry = ActorCastingStateV20(
        actor_id="char:wizard",
        verbal_available=True,
        sources=(
            ClassSpellSourceV20(
                source_id="class:wizard",
                class_slug="wizard",
                ability="int",
                spells=(
                    SpellSelection(
                        spell_id="hold-person",
                        prepared_order=0,
                        material=SpellMaterial(inventory_instance_id="pouch:1", available=True),
                    ),
                ),
            ),
        ),
    )
    assert entry.materials[0].inventory_instance_id == "pouch:1"
    assert entry.sources[0].prepared_spells == ("hold-person",)
    assert "materials" not in entry.model_dump()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda wire: wire["state_snapshot"]["character_states"][0].__setitem__(
            "spells_known", ["cure-wounds"]
        ),
        lambda wire: wire["state_snapshot"]["casting_state"]["actors"][0].__setitem__(
            "materials", []
        ),
        lambda wire: wire["state_snapshot"]["casting_state"]["actors"][0]["sources"][0].__setitem__(
            "known_spells", ["cure-wounds"]
        ),
        lambda wire: wire["state_snapshot"]["casting_state"]["actors"][0]["sources"][0][
            "spells"
        ].append({"spell_id": "cure-wounds", "prepared_order": None, "material": None}),
    ],
)
def test_canonical_wire_rejects_parallel_or_duplicate_spell_facts(mutation) -> None:
    """Mixed old/new representations and duplicate spell identity fail closed."""
    wire, _ = canonical_healing_wire()
    mutation(wire)
    with pytest.raises(ValidationError):
        RuleEvaluationRequest.model_validate_json(json.dumps(wire))


def test_legacy_casting_wire_remains_accepted() -> None:
    """Existing source lists and actor mirrors remain valid on the prior version."""
    request, _, _, _ = healing_case()
    assert RuleEvaluationRequest.model_validate_json(request.model_dump_json()) == request
