"""Reviewed exact-instance wearable and attunement rules for bound evaluation."""

from functools import lru_cache

from dnd5e_srd_data.loader import AssetLoader, BundledAssetLoader
from dnd5e_srd_data.schema.item import Item

from dnd5e_engine.evaluation_state import CharacterStateV2
from dnd5e_engine.rules.character import attunement_limit


def attunement_needs(actor_id: str, instance_id: str) -> tuple[str, str]:
    """Declare the exact Character and World item whose bond is being evaluated."""
    return actor_id, instance_id


def reviewed_protective_cloak(item: Item | None) -> bool:
    """Admit the fixed SRD cloak profile whose +1 applies only while worn and attuned."""
    return item is not None and item == _canonical_cloak()


@lru_cache(maxsize=1)
def _canonical_cloak() -> Item | None:
    """Reuse the bundled SRD profile for this narrow mechanical admission."""
    return BundledAssetLoader().get_item("cloak-of-protection")


def protective_cloak_ac_bonus(item: Item | None, *, worn: bool, attuned_to_wearer: bool) -> int:
    """Return the bound cloak's scalar AC bonus only while both conditions hold."""
    if not reviewed_protective_cloak(item):
        raise ValueError("unreviewed wearable AC modifier")
    return int(worn and attuned_to_wearer)


def attune_cloak_reason(
    actor: CharacterStateV2,
    item: Item,
    existing_slugs: tuple[str, ...],
    loader: AssetLoader,
) -> str | None:
    """Apply the bound item prerequisite, same-type rule and existing RE limit."""
    if not reviewed_protective_cloak(item) or not item.requires_attunement:
        return "unsupported_item_binding"
    if item.attunement_constraint is not None:
        return "item_attunement_prerequisite"
    if item.slug in existing_slugs:
        return "item_attunement_same_type"
    changes = [
        change
        for slug in actor.granted_features or ()
        if (feature := loader.get_feature(slug)) is not None
        for effect in feature.passive_effects
        if not effect.disabled
        for change in effect.changes
    ]
    if len(existing_slugs) >= attunement_limit(changes):
        return "item_attunement_limit"
    return None
