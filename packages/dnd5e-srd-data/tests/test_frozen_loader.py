"""Spell UUID identities must be unambiguous before a snapshot is published."""

import pytest

from dnd5e_srd_data.frozen import FrozenAssetLoader
from dnd5e_srd_data.loader import BundledAssetLoader, MemoryAssetLoader


@pytest.fixture
def spells():
    fireball = BundledAssetLoader().get_spell("fireball")
    assert fireball is not None
    assert fireball.foundry_uuid
    homebrew = fireball.model_copy(deep=True, update={"slug": "hb-fireball"})
    return fireball, homebrew


@pytest.mark.parametrize("reverse", [False, True])
def test_duplicate_spell_uuid_rejects_entire_snapshot_in_either_enumeration_order(spells, reverse):
    fireball, homebrew = spells
    source = MemoryAssetLoader(spells=[fireball, homebrew])
    enumerate_assets = source.list_slugs
    source.list_slugs = lambda category: enumerate_assets(category)[:: -1 if reverse else 1]
    candidate = FrozenAssetLoader.__new__(FrozenAssetLoader)
    with pytest.raises(ValueError) as caught:
        candidate.__init__(source)
    for identity in (fireball.foundry_uuid, fireball.slug, homebrew.slug):
        assert identity in str(caught.value)
    assert not hasattr(candidate, "_FrozenAssetLoader__corpus")
    assert not hasattr(candidate, "_FrozenAssetLoader__spell_uuids")


def test_distinct_spell_uuids_build_correct_independent_lookup_entries(spells):
    fireball, homebrew = spells
    homebrew.foundry_uuid = "custom:hb-fireball"
    source = MemoryAssetLoader(spells=[fireball, homebrew])
    fixed = FrozenAssetLoader(source)
    for spell in spells:
        assert fixed.get_spell_by_uuid(spell.foundry_uuid) == spell
        assert fixed.get_spell_by_uuid(spell.foundry_uuid) == fixed.get_spell(spell.slug)
    assert fixed.get_spell_by_uuid("missing") is None


def test_multiple_empty_spell_uuids_remain_legal_and_unindexed(spells):
    fireball, homebrew = spells
    fireball.foundry_uuid = homebrew.foundry_uuid = ""
    fixed = FrozenAssetLoader(MemoryAssetLoader(spells=[fireball, homebrew]))
    assert fixed.get_spell("fireball") == fireball
    assert fixed.get_spell("hb-fireball") == homebrew
    assert fixed.get_spell_by_uuid("") is None


def test_failed_freeze_preserves_existing_dataset_and_source_can_be_corrected(spells):
    fireball, homebrew = spells
    homebrew.foundry_uuid = "custom:hb-fireball"
    source = MemoryAssetLoader(spells=[fireball, homebrew])
    previous = FrozenAssetLoader(source)
    expected = previous.get_spell("hb-fireball")
    homebrew.foundry_uuid = fireball.foundry_uuid
    with pytest.raises(ValueError):
        FrozenAssetLoader(source)
    assert previous.get_spell_by_uuid("custom:hb-fireball") == expected
    assert previous.get_spell_by_uuid(fireball.foundry_uuid) == fireball
    homebrew.foundry_uuid = "custom:corrected-fireball"
    corrected = FrozenAssetLoader(source)
    assert corrected.get_spell_by_uuid(homebrew.foundry_uuid) == homebrew
    assert corrected.get_spell_by_uuid("custom:hb-fireball") is None
    assert previous.get_spell_by_uuid("custom:corrected-fireball") is None
