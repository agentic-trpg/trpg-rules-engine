"""Canonical timing mappings are explicit, source-verified and reproducible."""

from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.common import ActivityTiming, DamageActivity
from tools.translators.foundry import translate_spell_yaml
from tools.translators.spell_timing import _MANUAL, _PATTERNS, apply_spell_timing

FIXTURES = Path(__file__).parent / "fixtures" / "spell_timing"


@pytest.mark.parametrize("slug", ["weird", "vitriolic-sphere", "stinking-cloud"])
def test_translation_matches_canonical_timing(slug):
    canonical = BundledAssetLoader().get_spell(slug)
    translated = translate_spell_yaml(
        FIXTURES / f"{slug}.yml",
        ingest_date=date(2026, 5, 30),
        ingest_version="foundry-translator-v1",
    )
    assert translated.activities == canonical.activities
    assert [a.timing.trigger for a in translated.activities].count("immediate") == 1


@pytest.mark.parametrize("key", sorted(_PATTERNS))
def test_verified_mappings_reproduce_shipped_timing_and_resolve_effect_refs(key):
    slug, activity_id = key
    spell = BundledAssetLoader().get_spell(slug)
    raw = [a.model_copy(update={"timing": ActivityTiming()}) for a in spell.activities]
    assert apply_spell_timing(slug, spell.description, raw) == list(spell.activities)
    timing = next(a.timing for a in spell.activities if a.id == activity_id)
    assert timing.effect_id is None or timing.effect_id in {p.id for p in spell.passive_effects}


@pytest.mark.parametrize("key", sorted(_MANUAL))
def test_unsupported_event_or_sequence_is_explicitly_manual(key):
    slug, activity_id = key
    spell = BundledAssetLoader().get_spell(slug)
    activity = next(a for a in spell.activities if a.id == activity_id)
    assert activity.timing.trigger == "manual"


@pytest.mark.parametrize("change", ["id", "name", "kind", "condition", "clause"])
def test_source_drift_cannot_silently_apply_a_timing(change):
    spell = BundledAssetLoader().get_spell("weird")
    activities = list(spell.activities)
    description = spell.description
    timed = activities[1]
    if change == "id":
        activities[1] = timed.model_copy(update={"id": "renamed"})
    elif change == "name":
        activities[1] = timed.model_copy(update={"name": "Not the audited activity"})
    elif change == "kind":
        activities[1] = DamageActivity(id=timed.id, name=timed.name, activation=timed.activation)
    elif change == "condition":
        activities[1] = timed.model_copy(
            update={"activation": timed.activation.model_copy(update={"condition": "unknown"})}
        )
    else:
        description = "Timing is decided by arbitrary prose."
    with pytest.raises(ValueError, match="source drift"):
        apply_spell_timing(spell.slug, description, activities)


def test_unrecognized_prose_is_never_inferred_and_timing_is_typed():
    activity = DamageActivity(name="End of Turn Damage")
    assert apply_spell_timing("unknown", "at the end of its next turn", [activity]) == [activity]
    assert activity.timing.trigger == "immediate"
    assert "timing" not in activity.model_dump()  # byte-stable immediate defaults
    with pytest.raises(ValidationError):
        ActivityTiming(trigger="whenever the prose suggests")
