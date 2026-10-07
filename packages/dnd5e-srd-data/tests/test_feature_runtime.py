"""Hermetic tests of exact ingestion bindings and their shipped typed carriers."""

import json
from datetime import date
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.feature import Feature
from tools.translators.feature_runtime import (
    feature_runtime_activities,
    feature_runtime_effects,
    feature_runtime_operations,
    feature_target_rules,
)
from tools.translators.foundry import translate_feature_yaml


def test_shipped_bindings_and_numeric_corrections_match_ingestion():
    loader = BundledAssetLoader()
    for slug in loader.list_slugs("features"):
        f = loader.get_feature(slug)
        ids = [a.id for a in f.activities]
        assert f.runtime_operations == feature_runtime_operations(slug, ids)
        assert f.target_rules == feature_target_rules(slug, ids)
        assert f.activities == feature_runtime_activities(slug, list(f.activities))
        assert f.passive_effects == feature_runtime_effects(slug, f.passive_effects)
    assert feature_runtime_operations("unknown", ["K6UeXQwTyDHWvis8"]) == {}
    assert feature_runtime_operations("lay-on-hands", ["other"]) == {}


def test_translator_attaches_exact_operation_without_reading_prose(tmp_path):
    path = tmp_path / "classes24/paladin/class-features/lay-on-hands.yml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "_id": "phbpdnLayOnHands",
                "name": "Ignored prose",
                "system": {
                    "identifier": "lay-on-hands",
                    "description": {"value": "Nothing about poison"},
                    "activities": {
                        "K6UeXQwTyDHWvis8": {"type": "utility", "_id": "K6UeXQwTyDHWvis8"}
                    },
                },
            }
        ),
        encoding="utf8",
    )
    f = translate_feature_yaml(path, ingest_date=date(2026, 10, 7), ingest_version="test")
    assert f.runtime_operations == {"K6UeXQwTyDHWvis8": "remove_poison"}


def test_operation_schema_is_closed_and_empty_fields_stay_omitted():
    f = BundledAssetLoader().get_feature("second-wind")
    raw = f.model_dump(mode="json")
    assert "runtime_operations" not in raw
    assert "target_rules" not in raw
    with pytest.raises(ValidationError):
        Feature.model_validate({**raw, "runtime_operations": {"x": "eval_description"}})


def test_canonical_effects_have_no_heal_bonus_producers():
    def keys(value):
        if isinstance(value, dict):
            if isinstance(value.get("key"), str):
                yield value["key"]
            for child in value.values():
                yield from keys(child)
        elif isinstance(value, list):
            for child in value:
                yield from keys(child)

    root = Path(__file__).parents[1] / "src/dnd5e_srd_data/canonical"
    producers = [
        (path.relative_to(root).as_posix(), key)
        for path in sorted(root.rglob("*.json"))
        for key in keys(json.loads(path.read_text(encoding="utf8")))
        if key.startswith("system.bonuses.heal.")
    ]
    assert producers == []
