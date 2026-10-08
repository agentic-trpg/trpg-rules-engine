"""Reviewed choice mappings reproduce canonical contracts and reject source drift."""

import pytest
from pydantic import ValidationError

from dnd5e_srd_data import BundledAssetLoader
from dnd5e_srd_data.schema.common import UtilityActivity
from dnd5e_srd_data.schema.lifecycle import EffectLifecycleSpec
from dnd5e_srd_data.schema.spell import Spell
from tools.translators.effect_selection import _REVIEWED, apply_effect_selection


@pytest.mark.parametrize("slug", sorted(_REVIEWED))
def test_reviewed_choice_mapping_reproduces_canonical_without_mutation(slug):
    spell = BundledAssetLoader().get_spell(slug)
    activities = [
        a.model_copy(
            update={
                "effect_selection": None,
                "effects": [ref.model_copy(update={"lifecycle": None}) for ref in a.effects],
            }
        )
        for a in spell.activities
    ]
    raw = spell.model_copy(update={"activities": activities})
    before = raw.model_dump()
    assert apply_effect_selection(raw) == spell
    assert raw.model_dump() == before
    assert {r.id for r in spell.activities[0].effects} == {e.id for e in spell.passive_effects}


@pytest.mark.parametrize("slug", sorted(_REVIEWED))
@pytest.mark.parametrize(
    "mutation",
    [
        "key",
        "formula",
        "mode",
        "effect_id",
        "duration",
        "count",
        "target",
        "activity_id",
        "missing_ref",
        "disabled",
        "concentration",
        "spell_duration",
        "casting_time",
    ],
)
def test_choice_source_mechanical_drift_fails_closed(slug, mutation):
    raw = BundledAssetLoader().get_spell(slug).model_dump(mode="json")
    activity = raw["activities"][0]
    effect = raw["passive_effects"][0]
    if mutation == "key":
        effect["changes"][0]["key"] = "unknown.consumer"
    elif mutation == "formula":
        effect["changes"][0]["value"] = "999"
    elif mutation == "mode":
        effect["changes"][0]["mode"] = 5
    elif mutation == "effect_id":
        effect["id"] = "different-candidate"
    elif mutation == "duration":
        effect["duration"]["seconds"] = 6
    elif mutation == "count":
        activity["target"]["affects"]["count"] = "99"
    elif mutation == "target":
        activity["target"]["affects"]["type"] = "object"
    elif mutation == "activity_id":
        activity["id"] = "other-activity"
    elif mutation == "missing_ref":
        activity["effects"].pop()
    elif mutation == "disabled":
        effect["disabled"] = True
    elif mutation == "concentration":
        raw["concentration"] = False
    elif mutation == "spell_duration":
        raw["duration"]["value"] = 2
    else:
        raw["casting_time"]["unit"] = "bonus"
    with pytest.raises(ValueError, match="effect selection source drift"):
        apply_effect_selection(Spell.model_validate(raw))


def test_choice_contract_is_closed_and_unrelated_assets_keep_their_digest():
    ordinary = UtilityActivity(id="ordinary")
    assert "effect_selection" not in ordinary.model_dump()
    with pytest.raises(ValidationError):
        UtilityActivity(effect_selection="guess-from-prose")
    with pytest.raises(ValidationError, match="stacking group"):
        EffectLifecycleSpec(stacking="latest_applies")
    spell = BundledAssetLoader().get_spell("bless")
    assert apply_effect_selection(spell) is spell
