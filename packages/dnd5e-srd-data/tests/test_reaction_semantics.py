"""Reviewed reaction carriers are canonical, closed, and identity-specific."""

from datetime import date

import pytest
import yaml
from pydantic import ValidationError

from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.common import (
    ReactionResponse,
    ReactionSemantics,
    ReactionTriggerKind,
    UtilityActivity,
)
from tools.translators.foundry import translate_spell_yaml
from tools.translators.reactions import apply_reaction_semantics


def test_shipped_reaction_semantics_have_exact_targets_and_responses():
    loader = BundledAssetLoader()
    expected = {
        "shield": "self",
        "counterspell": "triggering_actor",
        "hellish-rebuke": "damage_source",
        "feather-fall": "affected_creature",
    }
    for slug, target_role in expected.items():
        activity = loader.get_spell(slug).activities[0]
        assert activity.reaction is not None
        assert activity.reaction.target_role == target_role
        assert activity.activation.reaction_conditions
        # Foundry inherited reaction conditions are authoritative even though
        # the activity's own serialized activation type remains "action".
        assert activity.activation.type == "action"
        assert apply_reaction_semantics(slug, [activity]) == [activity]
    shield = loader.get_spell("shield").activities[0].reaction
    assert shield is not None
    assert shield.effect_expiry == "owner_next_turn_start"
    assert shield.responses == [
        ReactionResponse(
            kind="negate_triggering_spell_damage",
            trigger_kind=ReactionTriggerKind.TARGETED_BY_SPELL,
        )
    ]
    counterspell = loader.get_spell("counterspell").activities[0].reaction
    assert counterspell is not None
    assert counterspell.effect_expiry is None
    assert "effect_expiry" not in counterspell.model_dump(mode="json")
    assert counterspell.responses == [
        ReactionResponse(kind="cancel_triggering_spell_on_failed_save")
    ]


def test_reaction_carriers_are_closed_and_unrelated_activities_stay_byte_stable():
    activity = UtilityActivity(id="other")
    assert "reaction" not in activity.model_dump(mode="json")
    with pytest.raises(ValidationError):
        ReactionSemantics.model_validate({"target_role": "last_damaged_by"})
    with pytest.raises(ValidationError):
        ReactionResponse.model_validate({"kind": "parse_description"})
    with pytest.raises(ValidationError):
        ReactionResponse.model_validate(
            {"kind": "negate_triggering_spell_damage", "trigger_kind": "targeted_by_magic_missile"}
        )
    assert apply_reaction_semantics("shield", [activity]) == [activity]
    assert (
        apply_reaction_semantics("unknown", [UtilityActivity(id="dnd5eactivity000")])[0].reaction
        is None
    )


def test_translator_carriers_use_exact_identity_without_description_inference(tmp_path):
    path = tmp_path / "spells24/1st-level/shield.yml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "name": "Shield",
                "system": {
                    "identifier": "shield",
                    "description": {"value": "No shielding prose here."},
                    "activation": {
                        "type": "reaction",
                        "condition": "when you are hit by an attack roll",
                    },
                    "activities": {
                        "dnd5eactivity000": {"type": "utility"},
                        "other": {"type": "utility"},
                    },
                },
            }
        ),
        encoding="utf8",
    )
    spell = translate_spell_yaml(path, ingest_date=date(2026, 10, 7), ingest_version="test")
    assert spell.activities[0].reaction is not None
    assert spell.activities[0].reaction.target_role == "self"
    assert spell.activities[1].reaction is None
    assert all(activity.activation.reaction_conditions for activity in spell.activities)


def test_all_canonical_reaction_spells_have_reviewed_carriers():
    loader = BundledAssetLoader()
    reaction_spells = []
    for slug in loader.list_slugs("spells"):
        spell = loader.get_spell(slug)
        if spell.casting_time.unit == "reaction":
            reaction_spells.append(slug)
            assert all(activity.activation.reaction_conditions for activity in spell.activities)
            assert all(activity.reaction is not None for activity in spell.activities)
    assert reaction_spells == ["counterspell", "feather-fall", "hellish-rebuke", "shield"]
    # Absorb Elements is absent from this shipped SRD corpus. Do not invent a
    # carrier or claim damage-type trigger support for nonexistent content.
    assert "absorb-elements" not in loader.list_slugs("spells")
