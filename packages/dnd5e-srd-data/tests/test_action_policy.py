"""Hermetic reviewed action-policy ingestion and closed schema validation."""

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from dnd5e_srd_data import BundledAssetLoader
from dnd5e_srd_data.schema.action_policy import ActionPolicy, RestrictedActionGrant
from dnd5e_srd_data.schema.spell import Spell
from tools.translators.action_policy import apply_action_policy, apply_item_action_types
from tools.translators.foundry import translate_generic_item_yaml, translate_spell_yaml


def test_pinned_slow_regeneration_is_exact():
    canonical = BundledAssetLoader().get_spell("slow")
    source = Path(__file__).parent / "fixtures/action-policy/_source/spells24/3rd-level/slow.yml"
    translated = translate_spell_yaml(
        source,
        ingest_date=canonical.provenance.ingest_date,
        ingest_version=canonical.provenance.ingest_version,
    )
    assert translated == canonical
    assert canonical.passive_effects[0].action_policy == ActionPolicy(
        action_or_bonus=True, deny_reactions=True, attack_count_cap=1, somatic_failure_percent=25
    )
    assert canonical.activities[0].effects[0].lifecycle.repeat_save is not None


@pytest.mark.parametrize(
    "field", ["identity", "count", "save", "effect", "formula", "duration", "clause", "empty"]
)
def test_source_drift_fails_closed(field):
    data = BundledAssetLoader().get_spell("slow").model_dump(mode="json")
    if field == "identity":
        data["foundry_uuid"] = "wrong"
    elif field == "count":
        data["activities"][0]["target"]["affects"]["count"] = "7"
    elif field == "save":
        data["activities"][0]["save"]["ability"] = ["dex"]
    elif field == "effect":
        data["activities"][0]["effects"][0]["id"] = "wrong"
    elif field == "formula":
        data["passive_effects"][0]["changes"][0]["value"] = "-3"
    elif field == "duration":
        data["duration"]["value"] = 2
    elif field == "clause":
        data["description"] = data["description"].replace("25 percent", "50 percent")
    else:
        data["activities"] = []
    with pytest.raises(ValueError, match="source drift"):
        apply_action_policy(Spell.model_validate(data))


@pytest.mark.parametrize(
    "payload", [{"attack_count_cap": 0}, {"somatic_failure_percent": 101}, {"guess": "Slow"}]
)
def test_action_policy_is_closed_and_bounded(payload):
    with pytest.raises(ValidationError):
        ActionPolicy(**payload)


@pytest.mark.parametrize("actions", [(), ("attack", "attack"), ("cast_spell",)])
def test_restricted_grant_candidates_are_closed_unique_and_nonempty(actions):
    with pytest.raises(ValidationError):
        RestrictedActionGrant(actions=actions)


@pytest.mark.parametrize(
    "kind,slug", [("spell", "haste"), ("item", "ball-bearings"), ("item", "caltrops")]
)
def test_b6_pinned_sources_regenerate_exactly_with_verified_hashes(kind, slug):
    loader = BundledAssetLoader()
    canonical = loader.get_spell(slug) if kind == "spell" else loader.get_item(slug)
    fixture = Path(__file__).parent / "fixtures/action-policy"
    manifest = json.loads((fixture / "snapshot-b6.json").read_text(encoding="utf-8"))
    pins = json.loads(
        (Path(__file__).parents[1] / "raw_sources/PINS.json").read_text(encoding="utf-8")
    )
    assert manifest["commit"] == pins["foundry"]["commit"]
    relative = canonical.provenance.source_url.split("/packs/", 1)[1]
    raw = fixture / relative
    assert hashlib.sha256(raw.read_bytes()).hexdigest() == manifest["files"][relative]["sha256"]
    assert canonical.provenance.source_url == manifest["files"][relative]["source_url"]
    translator = translate_spell_yaml if kind == "spell" else translate_generic_item_yaml
    assert (
        translator(
            raw,
            ingest_date=canonical.provenance.ingest_date,
            ingest_version=canonical.provenance.ingest_version,
        )
        == canonical
    )
    if kind == "spell":
        effect = canonical.passive_effects[0]
        assert effect.action_policy.extra_action.actions == (
            "attack",
            "dash",
            "disengage",
            "hide",
            "utilize",
        )
        assert (
            canonical.activities[0].effects[0].lifecycle.on_end[0].effect_id
            == canonical.passive_effects[1].id
        )
    else:
        assert canonical.activities[0].action_type == "utilize"
        assert all(a.action_type is None for a in canonical.activities[1:])


@pytest.mark.parametrize(
    "field",
    [
        "description",
        "casting_time",
        "target",
        "range",
        "concentration",
        "effect",
        "end",
        "duration",
        "save",
    ],
)
def test_haste_source_drift_fails_closed(field):
    data = BundledAssetLoader().get_spell("haste").model_dump(mode="json")
    if field == "description":
        data["description"] += " another rule"
    elif field == "casting_time":
        data["casting_time"]["unit"] = "bonus"
    elif field == "target":
        data["activities"][0]["target"]["affects"]["type"] = "creature"
    elif field == "range":
        data["range"]["value"] = 60
    elif field == "concentration":
        data["concentration"] = False
    elif field == "effect":
        data["passive_effects"][0]["changes"][0]["value"] = "3"
    elif field == "end":
        data["passive_effects"][1]["statuses"] = []
    elif field == "duration":
        data["duration"]["value"] = 2
    else:
        data["passive_effects"][0]["changes"][-1]["value"] = "-1"
    with pytest.raises(ValueError, match="source drift"):
        apply_action_policy(Spell.model_validate(data))


def test_utilize_classification_fails_on_changed_activity_or_missing_id():
    item = BundledAssetLoader().get_item("ball-bearings")
    for activities in (
        [],
        [item.activities[0].model_copy(update={"id": "wrong"})],
        [item.activities[0].model_copy(update={"effects": []})],
    ):
        with pytest.raises(ValueError, match="source drift"):
            apply_item_action_types("KbVt56CK4ud3FUk3", activities)


def test_unreviewed_item_source_cannot_inherit_utilize_by_name_or_slug():
    item = BundledAssetLoader().get_item("ball-bearings")
    unclassified = [a.model_copy(update={"action_type": None}) for a in item.activities]
    assert apply_item_action_types("unreviewed-source-id", unclassified) == unclassified
