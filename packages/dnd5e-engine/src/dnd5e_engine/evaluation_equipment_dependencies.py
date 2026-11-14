"""Reviewed complete Cleric/Fighter equipment closure for bounded operations."""

from collections.abc import Mapping

from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.item import Armor, ArmorCategory, Item

from dnd5e_engine.evaluation_state import (
    CharacterStateV2,
    CombatSnapshot,
    InventoryEntry,
    NonCombatSnapshot,
)
from dnd5e_engine.rules.character import (
    armor_class,
    armor_training_from_changes,
    defense_ac_bonus,
    proficiency_grants,
    weapon_proficiencies_from_changes,
)
from dnd5e_engine.rules.dice import ability_modifier


def equipped_dependencies(
    actor: CharacterStateV2,
    snapshot: CombatSnapshot | NonCombatSnapshot,
    loader: AssetLoader,
    canonical: AssetLoader,
) -> str | None:
    # Reviewed identities, never a rule inferred from item prose.
    """Check effective equipped item dependencies against the bound rules assets."""
    levels = actor.classes or (
        {actor.class_slug: actor.character_level} if actor.class_slug else {}
    )
    if levels == {"wizard": 5}:
        return wizard_pouch_dependencies(actor, snapshot, loader, canonical)
    fighter = levels == {"fighter": 1}
    if not fighter and levels != {"cleric": 1}:
        return "only equipped Cleric 1 or Fighter 1 dependencies are reviewed"
    weapon_slug = actor.weapon_in_hands
    if (
        weapon_slug not in ({"mace"} if fighter else {"mace", "quarterstaff"})
        or actor.worn_armor != "medium"
    ):
        return "equipped profile requires a reviewed simple weapon and chain shirt"
    by_id: dict[str, InventoryEntry] = {}
    if snapshot.snapshot_schema_version == "engine-snapshot/19":
        owned = []
        for entry in snapshot.inventory_state.entries:
            by_id[entry.instance_id] = entry
            if entry.owner_id == actor.entity_id:
                owned.append(entry)
        failure = instance_equipment_dependencies(actor, owned, by_id, loader, canonical)
        if failure:
            return failure
        slugs = {weapon_slug, "chain-shirt"} | ({"shield"} if actor.shield_equipped else set())
    else:
        slugs = {weapon_slug, "chain-shirt"} | ({"shield"} if actor.shield_equipped else set())
        entries = [e for e in snapshot.inventory_state.entries if e.owner_id == actor.entity_id]
        if snapshot.snapshot_schema_version == "engine-snapshot/18":
            # Only the explicitly held weapon contributes to the reviewed equipment profile.
            entries = [
                e
                for e in entries
                if e.item_slug != weapon_slug or e.instance_id == actor.held_weapon_instance_id
            ]
        if (
            len(entries) != len(slugs)
            or {e.item_slug for e in entries} != slugs
            or len(actor.carried_item_slugs) != len(slugs)
            or set(actor.carried_item_slugs) != slugs
            or any(
                e.quantity != 1 or not e.accessible or e.charges_remaining_per_unit for e in entries
            )
        ):
            return "equipped profile requires unique accessible owned equipment instances"
    if any(loader.get_item(s) != canonical.get_item(s) for s in slugs):
        return "equipment differs from reviewed canonical definitions"
    if (
        (actor.weapon_mastery_slugs and not fighter)
        or (actor.fighting_styles != ("defense",) if fighter else bool(actor.fighting_styles))
        or actor.attack_bonus is not None
        or actor.proficiency_bonus_override not in (None, 2)
        or (
            snapshot.snapshot_schema_version != "engine-snapshot/19"
            and actor.other_hand_occupied != actor.shield_equipped
        )
        or actor.weapon_grip not in ("one_handed", "two_handed")
        or (actor.weapon_grip == "two_handed" and (actor.shield_equipped or weapon_slug == "mace"))
    ):
        return "unreviewed equipped attack modifier, mastery or conflicting grip"
    if fighter:
        if loader.get_feat("defense") != canonical.get_feat("defense"):
            return "selected Defense feat differs from reviewed canonical definition"
        if (
            set(actor.weapon_mastery_slugs) != {"mace", "flail", "morningstar"}
            or len(actor.weapon_mastery_slugs) != 3
        ):
            return "complete reviewed three-weapon Sap mastery training is required"
        for slug in actor.weapon_mastery_slugs:
            weapon = loader.get_weapon(slug)
            if weapon is None or weapon != canonical.get_weapon(slug) or weapon.mastery != "sap":
                return "mastery training differs from reviewed Sap weapon definitions"
    grants = proficiency_grants(
        [(canonical.get_class("fighter" if fighter else "cleric"), 1, "primary")]
    )
    changes = []
    if not fighter:
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
    if snapshot.snapshot_schema_version == "engine-snapshot/19":
        assert actor.worn_armor_instance_id is not None
        armor_slug = by_id[actor.worn_armor_instance_id].item_slug
        shield_slug = (
            by_id[actor.equipped_shield_instance_id].item_slug
            if actor.equipped_shield_instance_id is not None
            else None
        )
    else:
        armor_slug = "chain-shirt"
        shield_slug = "shield" if actor.shield_equipped else None
    armor = loader.get_item(armor_slug)
    shield = loader.get_item(shield_slug) if shield_slug is not None else None
    assert isinstance(armor, Armor)
    assert shield is None or isinstance(shield, Armor)
    expected = armor_class(
        "default",
        {"dexterity": ability_modifier(actor.dexterity)},
        body_armor=armor,
        body_armor_bonus=armor.magical_bonus,
        shield_bonus=shield.base_ac if shield else 0,
    )
    expected += defense_ac_bonus(actor.fighting_styles, wearing_armor=True)
    if actor.ac != expected or actor.stealth_disadvantage != armor.stealth_disadvantage:
        return "armor/shield AC or stealth facts contradict shared armor rules"
    return None


def instance_equipment_dependencies(
    actor: CharacterStateV2,
    owned: list[InventoryEntry],
    entries: Mapping[str, InventoryEntry],
    loader: AssetLoader,
    canonical: AssetLoader,
) -> str | None:
    """Check only bound equipped instances; identical inventory spares are inert."""
    if actor.worn_armor_instance_id is None or actor.held_weapon_instance_id is None:
        return "equipped profile requires bound armor and held weapon instances"
    armor_entry = entries[actor.worn_armor_instance_id]  # Snapshot validates presence and owner.
    armor = loader.get_item(armor_entry.item_slug)
    if (
        armor_entry.item_slug != "chain-shirt"
        or not isinstance(armor, Armor)
        or armor.armor_category != ArmorCategory.MEDIUM
        or armor != canonical.get_item("chain-shirt")
    ):
        return "worn armor instance is outside the reviewed medium armor profile"
    if actor.equipped_shield_instance_id is not None:
        shield_entry = entries[actor.equipped_shield_instance_id]
        shield = loader.get_item(shield_entry.item_slug)
        if (
            shield_entry.item_slug != "shield"
            or not isinstance(shield, Armor)
            or shield.armor_category != ArmorCategory.SHIELD
            or shield != canonical.get_item("shield")
        ):
            return "shield instance is outside the reviewed shield profile"
    if actor.offhand_item_instance_id is not None:
        offhand_entry = entries[actor.offhand_item_instance_id]
        offhand = loader.get_item(offhand_entry.item_slug)
        if (
            offhand is None
            or not isinstance(offhand, Item)
            or isinstance(offhand, Armor)
            or offhand.passive_effects
            or offhand.requires_attunement
            or offhand.uses is not None
        ):
            return "offhand item has unreviewed active equipment mechanics"
    held_entry = entries[actor.held_weapon_instance_id]
    if (
        held_entry.item_slug != actor.weapon_in_hands
        or set(actor.carried_item_slugs) != {entry.item_slug for entry in owned}
        or any(entry.item_slug not in actor.carried_item_slugs for entry in owned)
        or any(entry.charges_remaining_per_unit for entry in owned)
    ):
        return "supplied equipment and inventory identities disagree"
    reviewed = {actor.weapon_in_hands, "chain-shirt", "shield"}
    if actor.offhand_item_instance_id is not None:
        reviewed.add(entries[actor.offhand_item_instance_id].item_slug)
    if any(
        entry.item_slug not in reviewed and entry.instance_id != actor.offhand_item_instance_id
        for entry in owned
    ):
        return "inventory contains an unreviewed equipment type"
    return None


def instance_equipment_kind_failure(
    actor: CharacterStateV2,
    snapshot: CombatSnapshot | NonCombatSnapshot,
    loader: AssetLoader,
    entries: Mapping[str, InventoryEntry],
) -> str | None:
    """Validate /19 equipped kinds against the bound loader for every actor."""
    if snapshot.snapshot_schema_version != "engine-snapshot/19":
        return None
    if actor.worn_armor_instance_id is not None:
        armor = loader.get_item(entries[actor.worn_armor_instance_id].item_slug)
        if not isinstance(armor, Armor) or armor.armor_category != actor.worn_armor:
            return "worn armor instance has the wrong item category"
    if actor.equipped_shield_instance_id is not None:
        shield = loader.get_item(entries[actor.equipped_shield_instance_id].item_slug)
        if not isinstance(shield, Armor) or shield.armor_category != ArmorCategory.SHIELD:
            return "equipped shield instance has the wrong item category"
    if actor.offhand_item_instance_id is not None:
        offhand = loader.get_item(entries[actor.offhand_item_instance_id].item_slug)
        if offhand is None or (
            isinstance(offhand, Armor) and offhand.armor_category == ArmorCategory.SHIELD
        ):
            return "offhand instance has no valid ordinary item definition"
    return None


def snapshot_equipment_kind_failure(
    snapshot: CombatSnapshot | NonCombatSnapshot, loader: AssetLoader
) -> str | None:
    """Check bound equipped kinds once per new-protocol evaluation or query."""
    if snapshot.snapshot_schema_version != "engine-snapshot/19":
        return None
    entries = {entry.instance_id: entry for entry in snapshot.inventory_state.entries}
    for actor in snapshot.character_states:
        if failure := instance_equipment_kind_failure(actor, snapshot, loader, entries):
            return failure
    return None


def wizard_pouch_dependencies(
    actor: CharacterStateV2,
    snapshot: CombatSnapshot | NonCombatSnapshot,
    loader: AssetLoader,
    canonical: AssetLoader,
) -> str | None:
    """Check the reviewed wizard material-pouch dependency profile."""
    entries = [e for e in snapshot.inventory_state.entries if e.owner_id == actor.entity_id]
    item = loader.get_item("component-pouch")
    if (
        actor.worn_armor
        or actor.shield_equipped
        or actor.other_hand_occupied
        or actor.weapon_in_hands is not None
        or actor.weapon_grip != "none"
        or actor.carried_item_slugs != ("component-pouch",)
        or len(entries) != 1
        or entries[0].item_slug != "component-pouch"
        or entries[0].quantity != 1
        or not entries[0].accessible
        or entries[0].charges_remaining_per_unit
        or item is None
        or item != canonical.get_item("component-pouch")
        or item.passive_effects
        or item.requires_attunement
        or item.uses
        or actor.fighting_styles
        or actor.weapon_mastery_slugs
    ):
        return "only an owned accessible canonical component pouch and free hands are reviewed"
    grants = proficiency_grants([(canonical.get_class("wizard"), 5, "primary")])
    if (
        actor.weapon_proficiencies is None
        or set(actor.weapon_proficiencies) != grants.weapons
        or set(actor.save_proficiencies) != grants.saves
        or actor.proficiency_bonus_override not in (None, 3)
        or actor.attack_bonus is not None
        or actor.ac
        != armor_class(
            "default",
            {"dexterity": ability_modifier(actor.dexterity)},
            body_armor=None,
            body_armor_bonus=0,
            shield_bonus=0,
        )
        or actor.stealth_disadvantage
    ):
        return "Wizard proficiency or unarmored AC contradicts shared class rules"
    return None
