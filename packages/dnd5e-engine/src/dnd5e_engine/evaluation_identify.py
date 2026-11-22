"""Resolve the selected object's Identify result from its bound rules and instance facts."""

from typing import Annotated

from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.item import MagicItem
from dnd5e_srd_data.schema.spell import Spell
from pydantic import Field

from dnd5e_engine.evaluation_base import EvaluationModel
from dnd5e_engine.evaluation_state import InventoryEntry
from dnd5e_engine.events import ObjectIdentification

IDENTIFY_ACTIVITY_SCHEMA_VERSION = "engine-evaluation/40"
IDENTIFY_SPELL_ID = "identify"
PEARL_ITEM_ID = "pearl"
PEARL_MIN_GP = 100
IDENTIFIABLE_ITEMS = frozenset({"wand-of-magic-missiles"})

Identifier = Annotated[str, Field(min_length=1, max_length=256)]


class IdentifyObjectFacts(EvaluationModel):
    """Project just the contacted object and the exact retained material unit."""

    target_id: Identifier
    item_slug: Identifier
    charges_remaining: Annotated[int, Field(ge=0)]
    ongoing_spell_ids: tuple[Identifier, ...]
    material: InventoryEntry


def identify_object(
    spell: Spell, facts: IdentifyObjectFacts, actor_id: str, loader: AssetLoader
) -> ObjectIdentification:
    """Validate the priced pearl and derive the caster's result from one reviewed profile."""
    material = facts.material
    pearl = loader.get_item(PEARL_ITEM_ID)
    if (
        spell.slug != IDENTIFY_SPELL_ID
        or "M" not in spell.components
        or spell.materials.consumed
        or material.item_slug != PEARL_ITEM_ID
        or material.owner_id != actor_id
        or material.quantity != 1
        or not material.accessible
        or material.charges_remaining_per_unit != 0
        or pearl is None
        or pearl.slug != PEARL_ITEM_ID
        or pearl.cost_gp is None
        or pearl.cost_gp < max(PEARL_MIN_GP, spell.materials.cost)
    ):
        raise ValueError("Identify needs the selected accessible pearl worth at least 100 GP")
    item = loader.get_item(facts.item_slug)
    if (
        facts.item_slug not in IDENTIFIABLE_ITEMS
        or not isinstance(item, MagicItem)
        or item.uses is None
        or not item.uses.max.isdecimal()
        or facts.charges_remaining > int(item.uses.max)
        or len(spell.activities) != 1
        or spell.duration.units != "inst"
        or spell.range.units != "touch"
    ):
        raise ValueError("Identify requires a supported instant spell and magic item profile")
    ongoing = []
    for slug in facts.ongoing_spell_ids:
        current = loader.get_spell(slug)
        if current is None or current.slug != slug:
            raise ValueError("Identify target has an unbound ongoing spell")
        ongoing.append(current.name)
    return ObjectIdentification(
        target_id=facts.target_id,
        item_slug=item.slug,
        item_name=item.name,
        properties_and_use=item.description,
        requires_attunement=item.requires_attunement,
        charges_remaining=facts.charges_remaining,
        ongoing_spells=tuple(ongoing),
    )
