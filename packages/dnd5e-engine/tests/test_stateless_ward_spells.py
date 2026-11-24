"""Guard the reviewed ward fact declaration against changed bound assets."""

from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader

from dnd5e_engine.evaluation_contracts import ExplorationSpellCastPayload
from dnd5e_engine.evaluation_ward_spells import prepare_ward_spell


def _payload(spell_id):
    """Select the same bounded ordered target list for both ward profiles."""
    return ExplorationSpellCastPayload(
        kind="world.cast_spell",
        spell_id=spell_id,
        spell_source_id="class:cleric",
        slot_level=1,
        target_id="npc.one",
        target_ids=("npc.one", "npc.two"),
    )


def test_ward_needs_are_declared_from_the_bound_profile():
    loader = BundledAssetLoader()
    bless = prepare_ward_spell(_payload("bless"), "pc.caster", loader)
    bane = prepare_ward_spell(_payload("bane"), "pc.caster", loader)
    assert bless.actor_ids == bane.actor_ids == ("pc.caster", "npc.one", "npc.two")
    assert bless.range_ft == bane.range_ft == 30
    assert bless.sight_target_ids == ()
    assert bane.sight_target_ids == ("npc.one", "npc.two")


def test_changed_bound_ward_does_not_inherit_reviewed_fact_needs():
    canonical = BundledAssetLoader().get_spell("bane")
    changed = canonical.model_copy(update={"concentration": False})
    loader = MemoryAssetLoader(spells=[changed])
    assert prepare_ward_spell(_payload("bane"), "pc.caster", loader) is None
