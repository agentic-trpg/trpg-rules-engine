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
from dnd5e_engine.events import HealingApplied
from tests.test_stateless_attack import execute
from tests.test_stateless_spell_feature import healing_case


def canonical_healing_wire() -> tuple[dict, object]:
    """Upgrade a real Cleric 1 fixture to the complete /20 casting wire."""
    request, loader, _, _ = healing_case()
    wire = json.loads(request.model_dump_json())
    wire["schema_version"] = "engine-evaluation/27"
    snapshot = wire["state_snapshot"]
    snapshot["snapshot_schema_version"] = "engine-snapshot/20"
    for actor in snapshot["character_states"]:
        del actor["spells_known"]
    source = snapshot["casting_state"]["actors"][0]["sources"][0]
    source.pop("known_spells")
    source.pop("prepared_spells")
    source["spells"] = [{"spell_id": "cure-wounds", "prepared_order": 0, "material": None}]
    snapshot["casting_state"]["actors"][0].pop("materials")
    return wire, loader


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
