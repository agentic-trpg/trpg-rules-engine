"""Hermetic ingestion and closed source/activity contracts for ongoing actions."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from dnd5e_srd_data import BundledAssetLoader
from dnd5e_srd_data.schema.common import OngoingActivationSpec, PersistentAreaSpec
from dnd5e_srd_data.schema.environment import EnvironmentalSpec
from dnd5e_srd_data.schema.spell import Spell
from tools.translators.foundry import translate_spell_yaml
from tools.translators.ongoing_activation import apply_ongoing_activation


def test_pinned_sunbeam_regeneration_is_exact():
    canonical = BundledAssetLoader().get_spell("sunbeam")
    translated = translate_spell_yaml(
        Path(__file__).parent / "fixtures/ongoing/_source/spells24/6th-level/sunbeam.yml",
        ingest_date=canonical.provenance.ingest_date,
        ingest_version=canonical.provenance.ingest_version,
    )
    assert translated == canonical
    assert canonical.activities[0].persistent_area.ongoing_activation.activity_ids == (
        canonical.activities[1].id,
    )
    assert all(
        a.effects[0].lifecycle.expiry_boundary == "source_next_turn_start"
        and not a.timing.effects_concentration
        for a in canonical.activities
    )
    assert all(a.damage.parts[0].scaling.number == 0 for a in canonical.activities)


@pytest.mark.parametrize(
    "mutation",
    [
        "level",
        "duration",
        "concentration",
        "description",
        "target",
        "activity",
        "effect",
        "formula",
        "shape",
        "save",
        "payment",
    ],
)
def test_ongoing_source_drift_requires_new_review(mutation):
    raw = BundledAssetLoader().get_spell("sunbeam").model_dump(mode="json")
    for source_activity in raw["activities"]:
        source_activity["damage"]["parts"][0]["scaling"]["number"] = 1
    activity = raw["activities"][1]
    if mutation == "level":
        raw["level"] = 7
    elif mutation == "duration":
        raw["duration"]["value"] = 2
    elif mutation == "concentration":
        raw["concentration"] = False
    elif mutation == "description":
        raw["description"] = "new mechanics"
    elif mutation == "target":
        activity["target"]["affects"]["choice"] = True
    elif mutation == "activity":
        activity["id"] = "unreviewed"
    elif mutation == "effect":
        activity["effects"][0]["id"] = "unreviewed"
    elif mutation == "formula":
        activity["damage"]["parts"][0]["number"] = 100
    elif mutation == "shape":
        activity["target"]["template"]["size"] = "120"
    elif mutation == "save":
        activity["save"]["ability"] = ["dex"]
    else:
        activity["consumption"]["spell_slot"] = True
    with pytest.raises(ValueError, match="ongoing source drift"):
        apply_ongoing_activation(Spell.model_validate(raw))


@pytest.mark.parametrize("ids", [(), ("",), ("repeat", "repeat")])
def test_ongoing_allowlist_is_explicit_nonempty_unique(ids):
    with pytest.raises(ValidationError):
        OngoingActivationSpec(activity_ids=ids)


def test_unknown_activation_cost_and_incoherent_source_geometry_fail_closed():
    with pytest.raises(ValidationError):
        OngoingActivationSpec(activity_ids=("repeat",), cost="bonus_action")
    with pytest.raises(ValidationError):
        PersistentAreaSpec(triggers=(), source_radius_ft=30)
    with pytest.raises(ValidationError):
        EnvironmentalSpec(kind="light", light="bright", sunlight_in_dim=True)
