"""Reviewed equipped Cleric 1 closure for ordinary attacks and turn reset."""

from dnd5e_srd_data import BundledAssetLoader
from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.item import Armor

from dnd5e_engine.evaluation_state import CharacterStateV2, CombatSnapshot, NonCombatSnapshot
from dnd5e_engine.rules.character import (
    armor_class,
    armor_training_from_changes,
    proficiency_grants,
    weapon_proficiencies_from_changes,
)
from dnd5e_engine.rules.dice import ability_modifier


def equipped_cleric_dependencies(
    actor: CharacterStateV2,
    snapshot: CombatSnapshot | NonCombatSnapshot,
    loader: AssetLoader,
    canonical: BundledAssetLoader,
) -> str | None:
    # Reviewed identities, never a rule inferred from item prose.
    levels = actor.classes or (
        {actor.class_slug: actor.character_level} if actor.class_slug else {}
    )
    if levels != {"cleric": 1}:
        return "only equipped Cleric 1 dependencies are reviewed"
    weapon_slug = actor.weapon_in_hands
    if weapon_slug not in {"mace", "quarterstaff"} or actor.worn_armor != "medium":
        return "equipped profile requires a reviewed simple weapon and chain shirt"
    slugs = {weapon_slug, "chain-shirt"} | ({"shield"} if actor.shield_equipped else set())
    entries = [e for e in snapshot.inventory_state.entries if e.owner_id == actor.entity_id]
    if (
        len(entries) != len(slugs)
        or {e.item_slug for e in entries} != slugs
        or len(actor.carried_item_slugs) != len(slugs)
        or set(actor.carried_item_slugs) != slugs
        or any(e.quantity != 1 or not e.accessible or e.charges_remaining_per_unit for e in entries)
    ):
        return "equipped profile requires unique accessible owned equipment instances"
    if any(loader.get_item(s) != canonical.get_item(s) for s in slugs):
        return "equipment differs from reviewed canonical definitions"
    if (
        actor.weapon_mastery_slugs
        or actor.fighting_styles
        or actor.attack_bonus is not None
        or actor.proficiency_bonus_override not in (None, 2)
        or actor.other_hand_occupied != actor.shield_equipped
        or actor.weapon_grip not in ("one_handed", "two_handed")
        or (actor.weapon_grip == "two_handed" and (actor.shield_equipped or weapon_slug == "mace"))
    ):
        return "unreviewed equipped attack modifier, mastery or conflicting grip"
    grants = proficiency_grants([(canonical.get_class("cleric"), 1, "primary")])
    protector = loader.get_feature("divine-order-protector")
    if protector is None or protector != canonical.get_feature("divine-order-protector"):
        return "selected Divine Order differs from reviewed canonical Protector"
    changes = [c for e in protector.passive_effects if not e.disabled for c in e.changes]
    weapons = grants.weapons | weapon_proficiencies_from_changes(changes)
    armor_training = grants.armor | armor_training_from_changes(changes)
    if (
        actor.weapon_proficiencies is None
        or set(actor.weapon_proficiencies) != weapons
        or set(actor.save_proficiencies) != grants.saves
        or actor.worn_armor not in armor_training
        or (actor.shield_equipped and "shield" not in armor_training)
    ):
        return "explicit equipment proficiencies must match canonical class grants"
    armor = loader.get_item("chain-shirt")
    shield = loader.get_item("shield") if actor.shield_equipped else None
    assert isinstance(armor, Armor)
    assert shield is None or isinstance(shield, Armor)
    expected = armor_class(
        "default",
        {"dexterity": ability_modifier(actor.dexterity)},
        body_armor=armor,
        body_armor_bonus=armor.magical_bonus,
        shield_bonus=shield.base_ac if shield else 0,
    )
    if actor.ac != expected or actor.stealth_disadvantage != armor.stealth_disadvantage:
        return "armor/shield AC or stealth facts contradict shared armor rules"
    return None
