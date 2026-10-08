"""Exact environmental ingestion contracts and canonical regeneration."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from dnd5e_srd_data import BundledAssetLoader
from dnd5e_srd_data.schema.common import UtilityActivity
from dnd5e_srd_data.schema.environment import EnvironmentalSpec
from dnd5e_srd_data.schema.spell import Spell
from tools.translators.environment import _REVIEWED, apply_environment
from tools.translators.foundry import translate_spell_yaml

PACKAGE = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "slug,folder",
    [("fog-cloud", "1st-level"), ("darkness", "2nd-level"), ("daylight", "3rd-level")],
)
def test_pinned_source_regenerates_exact_canonical_environment(slug, folder):
    canonical = BundledAssetLoader().get_spell(slug)
    translated = translate_spell_yaml(
        PACKAGE / f"tests/fixtures/environment/_source/spells24/{folder}/{slug}.yml",
        ingest_date=canonical.provenance.ingest_date,
        ingest_version=canonical.provenance.ingest_version,
    )
    assert translated == canonical
    assert canonical.activities[0].persistent_area.environment is not None
    assert canonical.activities[0].target.template.stationary


@pytest.mark.parametrize("slug", list(_REVIEWED))
@pytest.mark.parametrize(
    "mutation",
    [
        "radius",
        "shape",
        "range",
        "level",
        "duration",
        "concentration",
        "casting_time",
        "activity_id",
        "target",
        "effect",
        "description",
        "formula",
    ],
)
def test_environment_source_drift_requires_review(slug, mutation):
    spell = BundledAssetLoader().get_spell(slug).model_dump(mode="json")
    activity = spell["activities"][0]
    activity.pop("persistent_area")
    activity["target"]["template"]["size"] = _REVIEWED[slug][5]
    activity["target"]["template"]["stationary"] = False
    if mutation == "radius":
        activity["target"]["template"]["size"] = "999"
    elif mutation == "shape":
        activity["target"]["template"]["type"] = "cube"
    elif mutation == "range":
        spell["range"]["value"] = 300
    elif mutation == "level":
        spell["level"] = 9
    elif mutation == "duration":
        spell["duration"]["value"] = 2
    elif mutation == "concentration":
        spell["concentration"] = not spell["concentration"]
    elif mutation == "casting_time":
        spell["casting_time"]["value"] = 2
    elif mutation == "activity_id":
        activity["id"] = "unreviewed"
    elif mutation == "target":
        activity["target"]["affects"]["type"] = "object"
    elif mutation == "effect":
        activity["effects"] = [{"id": "unknown"}]
    elif mutation == "description":
        spell["description"] = "New mechanics require review."
    else:
        activity["roll"]["formula"] = "1d20"
    with pytest.raises(ValueError, match="environment source drift"):
        apply_environment(Spell.model_validate(spell))


@pytest.mark.parametrize(
    "data",
    [
        {"kind": "unknown"},
        {"kind": "fog", "light": "bright"},
        {"kind": "fog", "obscurement": "heavy", "radius": 20},
        {"kind": "magical_darkness", "light": "bright"},
        {"kind": "light", "light": "dim", "sunlight": True},
        {"kind": "light", "light": "bright", "dim_extension_ft": -1},
        {"kind": "light", "light": "bright", "strong_wind_dispersal": True},
        {"kind": "magical_darkness", "light": "dark", "dispels_light_through_level": 10},
    ],
)
def test_environment_vocabulary_and_combinations_fail_closed(data):
    with pytest.raises(ValidationError):
        EnvironmentalSpec.model_validate(data)


def test_unrelated_canonical_activity_serialization_is_unchanged():
    assert "persistent_area" not in UtilityActivity().model_dump(mode="json")
    spell = BundledAssetLoader().get_spell("fireball")
    assert apply_environment(spell) is spell
