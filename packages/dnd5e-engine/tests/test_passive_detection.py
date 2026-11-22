"""D&D 2024 Detect Magic reach, qualifying source kinds and blocking thicknesses."""

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader

from dnd5e_engine.passive_detection import DETECT_MAGIC, DetectionBarrier, is_magic_item


@pytest.mark.parametrize("material", ["stone", "dirt", "wood", "metal"])
def test_detect_magic_thickness_boundary(material):
    threshold = 1000 if material == "metal" else 12000
    assert DETECT_MAGIC.detects("spell_effect", 30, [DetectionBarrier(material, threshold - 1)])
    assert not DETECT_MAGIC.detects("spell_effect", 30, [DetectionBarrier(material, threshold)])


def test_detect_magic_qualifies_presence_only_through_a_known_path():
    assert DETECT_MAGIC.detects("magic_item", 0, [])
    assert DETECT_MAGIC.detects("magic_item", 30, [DetectionBarrier("wood", 100)])
    assert not DETECT_MAGIC.detects("magic_item", 31, [])
    assert not DETECT_MAGIC.detects("magic_item", 30, [None])
    assert not DETECT_MAGIC.detects("magic_item", 30, [DetectionBarrier("lead", 1)])
    assert not DETECT_MAGIC.detects("mundane", 30, [])


@pytest.mark.parametrize("thickness", [0, -1, True, 0.1])
def test_detect_magic_barrier_requires_positive_integer_mils(thickness):
    with pytest.raises(ValueError):
        DetectionBarrier("wood", thickness)


def test_magic_item_uses_bound_catalog_discriminators():
    loader = BundledAssetLoader()
    assert is_magic_item(loader.get_item("wand-of-magic-missiles"))
    assert is_magic_item(loader.get_item("flame-tongue"))
    assert not is_magic_item(loader.get_item("dagger"))
    assert not is_magic_item(None)
