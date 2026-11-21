"""Resolve and verify an authored spell's one uncharged material component."""

from typing import Literal, Self

from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.common import ConsumptionBlock, ConsumptionTargetEntry
from dnd5e_srd_data.schema.spell import Spell
from pydantic import model_validator

from dnd5e_engine.evaluation_base import EvaluationModel
from dnd5e_engine.evaluation_state import InventoryEntry, InventoryState


class SpellMaterialConsume(EvaluationModel):
    """Propose removal of one exact, accessible, uncharged ingredient."""

    kind: Literal["spell.material_consume"]
    expected: InventoryEntry

    @model_validator(mode="after")
    def validate_component(self) -> Self:
        """Require a complete physical unit in the fixed inventory."""
        if (
            self.expected.quantity != 1
            or self.expected.charges_remaining_per_unit != 0
            or not self.expected.accessible
        ):
            raise ValueError("material consumption requires an accessible uncharged unit")
        return self


def consumed_material_slug(spell: Spell, consumption: ConsumptionBlock) -> str:
    """Resolve the single fixed material declaration from the bound activity."""
    if "M" not in spell.components or not spell.materials.consumed or spell.materials.supply:
        raise ValueError("spell has no reviewed consumed material")
    if len(consumption.targets) != 1:
        raise ValueError("spell requires one authored material declaration")
    target = consumption.targets[0]
    expected = ConsumptionBlock(
        targets=[ConsumptionTargetEntry(type="material", target=target.target)]
    )
    if not target.target or consumption != expected:
        raise ValueError("spell requires one fixed material unit and ordinary cast payment")
    return target.target


def spell_material_payment(
    spell: Spell,
    consumption: ConsumptionBlock,
    inventory: InventoryState,
    *,
    actor_id: str,
    instance_id: str,
    loader: AssetLoader,
) -> SpellMaterialConsume:
    """Bind the caller's selected instance to the authored asset and price."""
    slug = consumed_material_slug(spell, consumption)
    asset = loader.get_item(slug)
    if (
        asset is None
        or asset.slug != slug
        or asset.cost_gp is None
        or asset.cost_gp < spell.materials.cost
    ):
        raise ValueError("material asset is missing or does not meet the declared price")
    entry = next((entry for entry in inventory.entries if entry.instance_id == instance_id), None)
    if entry is None or entry.owner_id != actor_id or entry.item_slug != slug:
        raise ValueError("selected material component is not available to the caster")
    return SpellMaterialConsume(kind="spell.material_consume", expected=entry)
