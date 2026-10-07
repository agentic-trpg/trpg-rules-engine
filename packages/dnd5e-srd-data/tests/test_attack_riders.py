"""Exact, closed and reproducible attack-rider ingestion contracts."""

import re
from datetime import date

import pytest
import yaml
from pydantic import ValidationError

from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.common import DamageActivity, UtilityActivity
from dnd5e_srd_data.schema.feature import (
    AttackRiderOptionSemantics,
    AttackRiderSemantics,
    RiderEffectSpec,
    RiderForcedMovement,
    RiderMovementGrant,
)
from dnd5e_srd_data.schema.monster import CreatureSize
from tools.translators.attack_riders import (
    attack_rider_activities,
    attack_rider_choice_limits,
    attack_rider_context,
    attack_rider_effects,
    attack_rider_options,
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
        assert feature.attack_rider_options == attack_rider_options(slug, ids)
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
    assert attack_rider_options("brutal-strike", ["unknown"]) == {}


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


def test_trip_is_a_size_qualified_canonical_sneak_damage_rider():
    feature = BundledAssetLoader().get_feature("cunning-strike")
    semantics = feature.attack_riders["dWcCw1vTWRMx4YzD"]
    activity = next(a for a in feature.activities if a.id == "dWcCw1vTWRMx4YzD")
    assert semantics.deferred_reason is None
    assert semantics.target_size_max is CreatureSize.LARGE
    assert semantics.trigger == "sneak_attack_damage"
    assert semantics.phase == "after_damage" and semantics.sneak_dice_cost == 1
    assert activity.save.ability == ["dex"] and activity.save.dc.calculation == "dex"
    assert semantics.effects == (RiderEffectSpec(effect_id="La47n2N3VtECtnA9", outcome="failure"),)
    effect = next(
        effect for effect in feature.passive_effects if effect.id == semantics.effects[0].effect_id
    )
    assert effect.statuses == ["prone"]


@pytest.mark.parametrize(
    "slug,activity_id,maximum,reason",
    [
        (
            "hills-tumble",
            "I2wKOUDxhIb5hHb7",
            CreatureSize.LARGE,
            "requires positive triggering damage and linked species resource",
        ),
        (
            "eldritch-smite",
            "CXJlzDUkMYU9w9i9",
            CreatureSize.HUGE,
            "requires authoritative pact-weapon binding and Pact Magic slot",
        ),
        (
            "repelling-blast",
            "OXhI1TDQxORrGAgc",
            CreatureSize.LARGE,
            "requires validated selected cantrip invocation binding",
        ),
    ],
)
def test_deferred_riders_record_completed_size_primitive_and_remaining_clauses(
    slug, activity_id, maximum, reason
):
    feature = BundledAssetLoader().get_feature(slug)
    semantics = feature.attack_riders[activity_id]
    assert semantics.target_size_max is maximum
    assert semantics.deferred_reason == reason
    assert semantics.model_dump(mode="json")["target_size_max"] == maximum.value
    assert attack_riders(slug, [activity_id])[activity_id] == semantics


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
        ("target_size_max", "colossal"),
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


def test_reckless_foundation_declares_strength_and_incoming_advantage_without_prose():
    feature = BundledAssetLoader().get_feature("reckless-attack")
    assert feature.activities == []
    foundation = feature.attack_rider_context
    assert foundation.inventory_role == "foundation" and foundation.deferred_reason is None
    assert foundation.qualification == "any_attack" and foundation.own_turn
    assert foundation.target_role == "attacker"
    assert foundation.declaration == "reckless_attack"
    [binding] = foundation.effects
    assert binding.effect_id == "XA0GhXVB54U2IuRP"
    assert binding.lifecycle.expiry_boundary == "source_next_turn_start"
    [effect] = feature.passive_effects
    assert [(c.key, c.mode, c.value) for c in effect.changes] == [
        ("flags.advantage.attack.strength", 5, "true"),
        ("flags.advantage.attack_against", 5, "true"),
    ]


def test_brutal_pool_crosses_features_while_damage_remains_one_canonical_activity():
    loader = BundledAssetLoader()
    base = loader.get_feature("brutal-strike")
    improved = loader.get_feature("improved-brutal-strike")
    pool = {**base.attack_rider_options, **improved.attack_rider_options}
    assert set(pool) == {"forceful-blow", "hamstring-blow", "staggering-blow", "sundering-blow"}
    assert base.attack_rider_choice_limits == {"brutal-strike": 1}
    assert loader.get_feature("improved-brutal-strike-2").attack_rider_choice_limits == {
        "brutal-strike": 2
    }
    [damage] = base.activities
    assert isinstance(damage, DamageActivity) and damage.activation.type == ""
    assert damage.damage.parts[0].custom.formula == "@scale.barbarian.brutal-strike"
    assert damage.damage.critical.allow is False
    for feature in (base, improved):
        for activity in feature.activities:
            rider = feature.attack_riders[activity.id]
            assert rider.shared_damage_group == "brutal-strike"
            assert rider.pre_roll_commit and rider.forgo_advantage and rider.once_per_turn
            assert rider.own_turn and rider.requires_reckless
            assert rider.deferred_reason is None and rider.deferred_options == {}
            assert set(rider.option_ids) <= pool.keys()
    forceful = pool["forceful-blow"]
    assert forceful.forced_movement == RiderForcedMovement(
        max_distance_ft=15, requires_distance_choice=False, on_save="always"
    )
    assert forceful.movement_grant == RiderMovementGrant(direction="straight_toward_target")
    assert forceful.effects == ()
    for option_id in ("hamstring-blow", "staggering-blow", "sundering-blow"):
        [binding] = pool[option_id].effects
        assert binding.lifecycle.expiry_boundary == "source_next_turn_start"
        assert binding.expiry == "none"
    hamstring = pool["hamstring-blow"].effects[0].lifecycle
    assert hamstring.stacking == "latest_only" and hamstring.stacking_group == "hamstring-blow"
    stagger = pool["staggering-blow"].effects[0].lifecycle
    assert stagger.one_use_modifiers == ("next_save_disadvantage",)
    sunder = pool["sundering-blow"].effects[0].lifecycle
    assert sunder.one_use_modifiers == ("next_attack_bonus_other_creature",)
    assert sunder.next_attack_scope == "other_creature"
    assert sunder.next_attack_bonus_group == "sundering-blow"
    assert [(c.key, c.mode, c.value) for c in base.passive_effects[0].changes] == [
        ("speed.reduction", 2, "15")
    ]
    effects = {e.id: e for e in improved.passive_effects}
    assert [(c.key, c.mode, c.value) for c in effects["L9N4evZo1jt46dap"].changes] == [
        ("flags.save.next_disadvantage", 5, "true"),
        ("flags.cannot_make_opportunity_attacks", 5, "true"),
    ]
    assert [(c.key, c.mode, c.value) for c in effects["tUyuyTQGmpUqMcFe"].changes] == [
        ("attack.next_bonus", 2, "5")
    ]


def test_frenzy_and_withdraw_have_complete_typed_trigger_and_grant_contracts():
    loader = BundledAssetLoader()
    sneak = loader.get_feature("sneak-attack").attack_riders["a1T6nHaqmvbLpyJr"]
    assert sneak.automatic and sneak.native_damage and sneak.trigger == "final_hit"
    feature = loader.get_feature("frenzy")
    [activity] = feature.activities
    rider = feature.attack_riders[activity.id]
    assert rider.automatic and rider.once_per_turn and rider.own_turn
    assert not rider.native_damage
    assert rider.requires_rage and rider.requires_reckless and rider.inherit_damage_type
    assert rider.phase == "damage_preparation" and not rider.pre_roll_commit
    assert rider.qualification == "strength" and rider.deferred_reason is None
    assert activity.damage.parts[0].custom.formula == "(@scale.barbarian.rage-damage)d6"
    assert activity.damage.critical.allow is False
    withdraw = loader.get_feature("cunning-strike").attack_riders["m2bRZ1YeD3yf9nV7"]
    assert withdraw.sneak_dice_cost == 1 and withdraw.trigger == "sneak_attack_damage"
    assert withdraw.phase == "after_damage" and withdraw.deferred_reason is None
    assert withdraw.movement_grant == RiderMovementGrant(direction="any")


@pytest.mark.parametrize(
    "factory,raw",
    [
        (RiderMovementGrant, {"direction": "fly"}),
        (RiderMovementGrant, {"direction": "any", "maximum_speed_fraction": "full"}),
        (RiderMovementGrant, {"direction": "any", "movement_mode": "fly"}),
        (RiderMovementGrant, {"direction": "any", "provokes_opportunity_attacks": True}),
        (RiderMovementGrant, {"direction": "any", "max_distance_ft": 999}),
        (AttackRiderOptionSemantics, {"activity_id": "x", "choice_group": "x", "damage": 99}),
    ],
)
def test_option_and_scoped_movement_vocabulary_rejects_host_mechanics(factory, raw):
    with pytest.raises(ValidationError):
        factory.model_validate(raw)


def test_option_templates_clone_nested_lifecycle_and_omit_new_defaults():
    first = attack_rider_options("brutal-strike", ["nN5gsB6AcSQ4uQPN"])
    second = attack_rider_options("brutal-strike", ["nN5gsB6AcSQ4uQPN"])
    assert first == second and first is not second
    assert first["hamstring-blow"].effects[0] is not second["hamstring-blow"].effects[0]
    assert (
        first["hamstring-blow"].effects[0].lifecycle
        is not second["hamstring-blow"].effects[0].lifecycle
    )
    rider = AttackRiderSemantics(
        trigger="final_hit", qualification="any_attack", phase="after_damage"
    )
    assert (
        not {"option_ids", "requires_reckless", "shared_damage_group", "movement_grant"}
        & rider.model_dump().keys()
    )
