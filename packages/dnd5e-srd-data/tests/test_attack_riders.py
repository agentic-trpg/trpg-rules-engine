"""Exact, closed and reproducible attack-rider ingestion contracts."""

import re
from datetime import date

import pytest
import yaml
from pydantic import ValidationError

from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.common import UtilityActivity
from dnd5e_srd_data.schema.feature import (
    AttackRiderSemantics,
    RiderEffectSpec,
    RiderForcedMovement,
)
from tools.translators.attack_riders import (
    attack_rider_activities,
    attack_rider_choice_limits,
    attack_rider_context,
    attack_rider_effects,
    attack_riders,
)
from tools.translators.foundry import translate_feature_yaml


def test_every_shipped_attack_binding_matches_exact_ingestion():
    loader = BundledAssetLoader()
    for slug in loader.list_slugs("features"):
        feature = loader.get_feature(slug)
        ids = [activity.id for activity in feature.activities]
        assert feature.attack_riders == attack_riders(slug, ids)
        assert feature.attack_rider_context == attack_rider_context(slug)
        assert feature.attack_rider_choice_limits == attack_rider_choice_limits(slug)
        assert feature.activities == attack_rider_activities(slug, list(feature.activities))
        assert feature.passive_effects == attack_rider_effects(slug, feature.passive_effects)
        for semantics in feature.attack_riders.values():
            if semantics.deferred_reason is None:
                assert semantics.inventory_role == "rider"
            for effect in semantics.effects:
                assert effect.effect_id in {value.id for value in feature.passive_effects}
    assert attack_riders("stunning-strike", ["unknown"]) == {}
    assert attack_riders("unknown", ["Xto99a8Zt46VLwaR"]) == {}
    assert attack_rider_context("unknown") is None


def test_all_corpus_hit_sneak_flurry_reckless_phrases_were_reviewed():
    # Audit discovery belongs to ingestion tests, never runtime. Examine the
    # full corpus so a future unreviewed producer cannot silently evade audit.
    pattern = re.compile(
        r"\b(?:when(?:ever)?|once)[^.\n]{0,220}\bhits?\b(?!\s+Points?\b)"
        r"|\bSneak Attack\b|\bFlurry of Blows\b|\bReckless Attack\b"
        r"|\beach time you hit\b|\bafter you score a Critical Hit\b",
        re.IGNORECASE,
    )
    loader = BundledAssetLoader()
    unreviewed = []
    for slug in loader.list_slugs("features"):
        feature = loader.get_feature(slug)
        text = (
            feature.description
            + "\n"
            + "\n".join(activity.activation.condition for activity in feature.activities)
        )
        if pattern.search(text) and not (feature.attack_riders or feature.attack_rider_context):
            unreviewed.append(slug)
    assert unreviewed == []


def test_stunning_outcomes_and_shared_changes_are_exact():
    feature = BundledAssetLoader().get_feature("stunning-strike")
    activity = feature.activities[0]
    assert {ref.id: ref.on_save for ref in activity.effects} == {
        "vofnieSTB0l8rpRg": False,
        "cj9HhBNKtF6iOsH4": True,
    }
    effects = {effect.id: effect for effect in feature.passive_effects}
    assert effects["vofnieSTB0l8rpRg"].statuses == ["stunned"]
    assert [(c.key, c.mode, c.value) for c in effects["cj9HhBNKtF6iOsH4"].changes] == [
        ("speed.multiplier", 1, "0.5"),
        ("flags.attack.next_advantage", 5, "true"),
    ]
    semantics = feature.attack_riders[activity.id]
    assert semantics.once_per_turn and semantics.qualification == "monk_weapon_or_unarmed"
    assert all(effect.expiry == "source_next_turn_start" for effect in semantics.effects)
    assert activity.consumption.targets[0].target.endswith(".phbmnkMonksFocus")
    assert activity.consumption.targets[0].value == "1"


def test_open_hand_and_obscure_preserve_all_rule_clauses():
    loader = BundledAssetLoader()
    hand = loader.get_feature("open-hand-technique")
    addle = hand.attack_riders["1jdSaWanuRrdkVs3"]
    assert addle.effects[0].expiry == "target_next_turn_start"
    effect = next(e for e in hand.passive_effects if e.id == addle.effects[0].effect_id)
    assert [(c.key, c.mode, c.value) for c in effect.changes] == [
        ("flags.cannot_make_opportunity_attacks", 5, "true")
    ]
    push = hand.attack_riders["XoaS0RtDCGAqrQsf"].forced_movement
    assert push == RiderForcedMovement(max_distance_ft=15)
    obscure = loader.get_feature("devious-strikes")
    activity = next(a for a in obscure.activities if a.id == "ki4lIPVGNA0HjEzH")
    semantics = obscure.attack_riders[activity.id]
    assert activity.save.ability == ["dex"]
    assert semantics.sneak_dice_cost == 3
    assert semantics.effects[0].expiry == "target_next_turn_end"
    assert loader.get_feature("improved-cunning-strike").attack_rider_choice_limits == {
        "cunning-strike": 2
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("trigger", "when_you_hit"),
        ("qualification", "host_override"),
        ("phase", "after_payment_error"),
        ("sneak_dice_cost", -1),
        ("target_role", "last_damaged_by"),
    ],
)
def test_rider_metadata_values_are_closed(field, value):
    raw = {"trigger": "final_hit", "qualification": "any_weapon", "phase": "after_damage"}
    with pytest.raises(ValidationError):
        AttackRiderSemantics.model_validate({**raw, field: value})


def test_effect_and_movement_values_are_closed_and_empty_feature_fields_omitted():
    with pytest.raises(ValidationError):
        RiderEffectSpec(effect_id="x", expiry="guessed_duration")
    with pytest.raises(ValidationError):
        RiderEffectSpec(effect_id="x", outcome="guessed_save")
    with pytest.raises(ValidationError):
        RiderForcedMovement(max_distance_ft=-1)
    feature = BundledAssetLoader().get_feature("second-wind")
    assert not any(key.startswith("attack_rider") for key in feature.model_dump(mode="json"))


def test_translator_identity_mapping_ignores_feature_name_and_prose(tmp_path):
    path = tmp_path / "classes24/monk/class-features/stunning-strike.yml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "name": "Unrelated name",
                "system": {
                    "identifier": "stunning-strike",
                    "description": {"value": "Unrelated prose"},
                    "activities": {
                        "Xto99a8Zt46VLwaR": {"type": "save"},
                        "other": {"type": "utility"},
                    },
                },
            }
        ),
        encoding="utf8",
    )
    feature = translate_feature_yaml(path, ingest_date=date(2026, 10, 8), ingest_version="test")
    assert set(feature.attack_riders) == {"Xto99a8Zt46VLwaR"}
    assert feature.attack_riders["Xto99a8Zt46VLwaR"].qualification == "monk_weapon_or_unarmed"


def test_metadata_templates_and_input_activities_are_not_mutated():
    first = attack_riders("stunning-strike", ["Xto99a8Zt46VLwaR"])
    first["Xto99a8Zt46VLwaR"].deferred_options["host"] = "mutation"
    second = attack_riders("stunning-strike", ["Xto99a8Zt46VLwaR"])
    assert second["Xto99a8Zt46VLwaR"].deferred_options == {}
    activity = UtilityActivity(id="unrelated")
    assert attack_rider_activities("stunning-strike", [activity]) == [activity]
    assert activity.model_dump(mode="json") == UtilityActivity(id="unrelated").model_dump(
        mode="json"
    )
