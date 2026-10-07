"""Persistent producers are typed, allowlisted and regeneration-stable."""

import pytest
from pydantic import ValidationError

from dnd5e_srd_data import PersistentAreaSpec
from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.common import SaveActivity
from tools.translators.persistent_areas import apply_persistent_areas


@pytest.mark.parametrize(
    "slug,kind",
    [
        ("spirit-guardians", "spell"),
        ("stinking-cloud", "spell"),
        ("ball-bearings", "item"),
        ("caltrops", "item"),
    ],
)
def test_canonical_producers_are_reproducible_and_have_valid_effect_references(slug, kind):
    loader = BundledAssetLoader()
    source = loader.get_spell(slug) if kind == "spell" else loader.get_item(slug)
    activities = list(source.activities)
    raw = [a.model_copy(update={"persistent_area": None}) for a in activities]
    assert apply_persistent_areas(slug, source.description, raw) == activities
    (payload,) = [a for a in activities if a.persistent_area is not None]
    assert {e.id for e in payload.effects} <= {e.id for e in source.passive_effects}
    assert payload.target.template.stationary == (payload.persistent_area.placement == "stationary")


@pytest.mark.parametrize("change", ["id", "kind", "template", "dc", "description"])
def test_source_drift_fails_closed(change):
    source = BundledAssetLoader().get_item("caltrops")
    payload = source.activities[0]
    description = source.description
    if change == "id":
        payload = payload.model_copy(update={"id": "unknown"})
    elif change == "kind":
        payload = payload.model_copy(update={"kind": "damage"})
    elif change == "template":
        payload = payload.model_copy(
            update={
                "target": payload.target.model_copy(
                    update={"template": payload.target.template.model_copy(update={"size": "10"})}
                )
            }
        )
    elif change == "dc":
        payload = payload.model_copy(
            update={
                "save": payload.save.model_copy(
                    update={"dc": payload.save.dc.model_copy(update={"formula": "20"})}
                )
            }
        )
    else:
        description = "An unverified rule."
    with pytest.raises(ValueError, match="source drift"):
        apply_persistent_areas(source.slug, description, [payload])


def test_unknown_sources_stay_immediate_and_trigger_vocabulary_is_closed():
    activity = SaveActivity()
    assert apply_persistent_areas("unknown", "enter an area", [activity]) == [activity]
    assert "persistent_area" not in activity.model_dump()
    with pytest.raises(ValidationError):
        PersistentAreaSpec(triggers=("guess-from-prose",))
