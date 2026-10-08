"""Hermetic reviewed action-policy ingestion and closed schema validation."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from dnd5e_srd_data import BundledAssetLoader
from dnd5e_srd_data.schema.action_policy import ActionPolicy, RestrictedActionGrant
from dnd5e_srd_data.schema.spell import Spell
from tools.translators.action_policy import apply_action_policy
from tools.translators.foundry import translate_spell_yaml


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
