"""configure_lib_loader — public host seam for AssetLoader injection."""

from dnd5e_srd_data.loader import BundledAssetLoader, MemoryAssetLoader

import dnd5e_engine
from dnd5e_engine.events import CastFailed, SpellCast
from dnd5e_engine.lib_loader import get_lib_loader
from tests.c20_support import act, combatant, events, start, wizard


def test_configure_lib_loader_is_public() -> None:
    assert "configure_lib_loader" in dnd5e_engine.__all__


def test_configure_installs_and_none_reverts() -> None:
    custom = MemoryAssetLoader()
    try:
        dnd5e_engine.configure_lib_loader(custom)
        assert get_lib_loader() is custom
    finally:
        dnd5e_engine.configure_lib_loader(None)
    assert isinstance(get_lib_loader(), BundledAssetLoader)


def test_configure_replacement_only_applies_to_future_combats() -> None:
    original = BundledAssetLoader()
    replacement = MemoryAssetLoader()
    try:
        dnd5e_engine.configure_lib_loader(original)
        handle, live = start([wizard(spells_known=["message"])], seed=301)
        dnd5e_engine.configure_lib_loader(replacement)
        assert get_lib_loader() is replacement

        act(handle, "char:wiz", intent_type="cast_spell", spell_id="message", target_id="mon:foe")
        assert len(events(live, SpellCast)) == 1
        assert not events(live, CastFailed)
        assert not combatant(live, "char:wiz").action_available
    finally:
        dnd5e_engine.configure_lib_loader(None)
