"""Canonical class closure for reviewed operations over complete actor facts."""

from dnd5e_srd_data.loader import AssetLoader

from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.scale import build_scale_values
from dnd5e_engine.build_spec import derive_multiclass_pact_slots, derive_multiclass_slots
from dnd5e_engine.evaluation_actor import combatant
from dnd5e_engine.evaluation_equipment_dependencies import (
    HEALING_POTION_SLUG,
    equipped_dependencies,
    inert_component_pouch,
    is_stowed_healing_potion,
    reviewed_healing_potion_owners,
)
from dnd5e_engine.evaluation_resources import FeatureResource, SlotResource
from dnd5e_engine.evaluation_ruleset import get_default_ruleset
from dnd5e_engine.evaluation_state import CharacterStateV2, CombatSnapshot, NonCombatSnapshot
from dnd5e_engine.feature_runtime import DrawFreeRandom, scalar_formula

# Canonical grants are reviewed together with the operation, not globally disabled.
# Divine Order/Defense/mastery affect equipment, skills or attacks, not this
# unarmored heal. Second Wind is separately priced; Spellcasting supplies slots.
_REVIEWED = {("cleric", 1), ("fighter", 1), ("wizard", 1), ("wizard", 5)}


def _class_equipment_profile(
    actor: CharacterStateV2,
    snapshot: CombatSnapshot | NonCombatSnapshot,
    loader: AssetLoader,
    canonical: AssetLoader,
    *,
    equipped: bool,
    allow_stowed_healing_potions: bool,
    stowed_consumable_ids: frozenset[str],
    allow_life_pouch: bool = False,
) -> tuple[bool, str | None]:
    """Check equipment dependencies while retaining the complete inventory identity."""
    if (allow_stowed_healing_potions or stowed_consumable_ids) and set(
        actor.carried_item_slugs
    ) != {
        entry.item_slug
        for entry in snapshot.inventory_state.entries
        if entry.owner_id == actor.entity_id
    }:
        return False, "supplied equipment and inventory identities disagree"
    has_worn_or_offhand = actor.worn_armor or actor.shield_equipped or actor.other_hand_occupied
    has_equipment = bool(
        has_worn_or_offhand
        or (
            not stowed_consumable_ids
            and any(
                not allow_stowed_healing_potions or slug != HEALING_POTION_SLUG
                for slug in actor.carried_item_slugs
            )
        )
        or any(
            entry.owner_id == actor.entity_id
            and entry.instance_id not in stowed_consumable_ids
            and (not allow_stowed_healing_potions or not is_stowed_healing_potion(entry, actor))
            for entry in snapshot.inventory_state.entries
        )
    )
    if not has_equipment:
        return False, None
    if allow_life_pouch and inert_component_pouch(actor, snapshot, loader, canonical):
        return False, None
    if not equipped:
        return True, (
            "only unarmored, unencumbered component profile is reviewed"
            if has_worn_or_offhand
            else "only the complete empty equipment profile is reviewed"
        )
    return True, equipped_dependencies(
        actor,
        snapshot,
        loader,
        canonical,
        allow_stowed_healing_potions=allow_stowed_healing_potions,
        stowed_consumable_ids=stowed_consumable_ids,
    )


def class_dependencies(
    actor: CharacterStateV2,
    snapshot: CombatSnapshot | NonCombatSnapshot,
    loader: AssetLoader,
    canonical: AssetLoader,
    *,
    equipped: bool = False,
    allow_stowed_healing_potions: bool = False,
    stowed_consumable_ids: frozenset[str] = frozenset(),
    reviewed_lore_bard: bool = False,
    reviewed_wizard_pouch: bool = False,
    reviewed_life_healing: bool = False,
) -> str | None:
    """Check class dependencies within the caller's reviewed operation profile.

    The reviewed Lore Bard profile requires empty equipment and the complete
    Bard/Lore grants. This profile does not admit Bard feature invocations.
    A caller can admit only the reviewed Wizard pouch without enabling other equipment.
    Exploration healing can use Life Cleric 3 or 5 with the complete Life grants.
    """
    if actor.species_slug:
        return "subclass/species operation dependencies are not migrated"
    levels = actor.classes or (
        {actor.class_slug: actor.character_level} if actor.class_slug else {}
    )
    if not levels:
        if (
            actor.granted_features
            or actor.spell_slots
            or actor.pact_slots
            or actor.subclass_slug
            or actor.fighting_styles
            or actor.weapon_mastery_slugs
        ):
            return "class/source dependency is absent"
        return None
    has_equipment, failure = _class_equipment_profile(
        actor,
        snapshot,
        loader,
        canonical,
        equipped=equipped or (reviewed_wizard_pouch and levels == {"wizard": 5}),
        allow_stowed_healing_potions=allow_stowed_healing_potions,
        stowed_consumable_ids=stowed_consumable_ids,
        allow_life_pouch=(
            reviewed_life_healing
            and levels in ({"cleric": 3}, {"cleric": 5})
            and actor.subclass_slug == "life"
        ),
    )
    if failure:
        return failure
    if actor.fighting_styles not in ((), ("defense",)):
        return "unreviewed fighting style"
    if any(not k.startswith("feature_use:") for k in actor.custom_counters):
        return "unreviewed mechanical counter"
    lore_bard = reviewed_lore_bard and levels == {"bard": 5}
    if lore_bard and (
        actor.subclass_slug != "lore"
        or has_equipment
        or actor.proficiency_bonus_override is not None
    ):
        return "Lore Bard requires the ordinary unencumbered level-5 profile"
    life_cleric = (
        reviewed_life_healing
        and levels in ({"cleric": 3}, {"cleric": 5})
        and actor.subclass_slug == "life"
    )
    if actor.subclass_slug and not (
        lore_bard or life_cleric or (levels == {"wizard": 5} and actor.subclass_slug == "evoker")
    ):
        return "unreviewed subclass dependencies"
    if levels == {"wizard": 5} and actor.subclass_slug != "evoker":
        return "complete reviewed Evoker 5 subclass is required"
    if len(levels) != 1 or sum(levels.values()) != actor.character_level:
        return "only an explicit single-class profile is reviewed"
    slug, level = next(iter(levels.items()))
    if not (lore_bard or life_cleric or (slug, level) in _REVIEWED) or (
        actor.class_slug not in (None, slug)
    ):
        return "class/level has unreviewed operation dependencies"
    cls = loader.get_class(slug)
    if cls is None or cls != canonical.get_class(slug):
        return "class definition differs from the reviewed binding"
    grants = {g.slug for g in cls.granted_features if g.ref_type == "feature" and g.level <= level}
    if equipped and has_equipment and slug == "cleric":
        grants.add("divine-order-protector")
    if levels == {"wizard": 5} or lore_bard or life_cleric:
        # Cutting Words reacts to attack rolls, ability checks, or damage rolls.
        # Font of Inspiration changes rest, recovery, and Inspiration payments.
        # Neither applies to healing. Life's active Preserve Life and other
        # class actions are separate invocations from its Disciple of Life rider.
        assert actor.subclass_slug is not None
        subclass_slug = actor.subclass_slug
        additional = _reviewed_subclass_grants(loader, canonical, subclass_slug, slug, level)
        if additional is None:
            return "subclass definition differs from reviewed binding"
        grants |= additional
    if actor.granted_features is None or set(actor.granted_features) != grants:
        return "complete explicit class grants are required"
    if any(loader.get_feature(g) != canonical.get_feature(g) for g in grants):
        return "class feature differs from the reviewed definition"
    if actor.fighting_styles and slug != "fighter":
        return "fighting style source is absent"
    if actor.weapon_mastery_slugs and slug != "fighter":
        return "mastery source is absent"
    if any(loader.get_weapon(w) is None for w in actor.weapon_mastery_slugs):
        return "mastery weapon definition is absent"
    if snapshot.resource_state is None:
        return "complete resource state is required for class dependencies"
    return pool_dependencies(actor, snapshot, loader, grants, levels, cls.spellcasting.ability)


def pool_dependencies(
    actor: CharacterStateV2,
    snapshot: CombatSnapshot | NonCombatSnapshot,
    loader: AssetLoader,
    grants: set[str],
    levels: dict[str, int],
    ability: str,
) -> str | None:
    """Check owned resource pools against their class/feature definitions."""
    assert snapshot.resource_state is not None
    pools = [p for p in snapshot.resource_state.pools if p.owner_id == actor.entity_id]
    classes = levels
    for kind, maxima in (
        ("spell_slot", derive_multiclass_slots(classes, loader=loader)),
        ("pact_slot", derive_multiclass_pact_slots(classes, loader=loader)),
    ):
        if {
            p.level: p.maximum for p in pools if isinstance(p, SlotResource) and p.kind == kind
        } != maxima:
            return "slot capacities do not match class rules"
    features = {p.feature_slug: p for p in pools if isinstance(p, FeatureResource)}
    finite = {g for g in grants if (f := loader.get_feature(g)) and f.uses and f.uses.max}
    if set(features) != finite:
        return "finite feature pool closure is incomplete"
    context = build_activity_context(
        combatant(actor),
        [],
        rng=DrawFreeRandom(0),
        event_emitter=lambda event: None,
        slot_level=None,
        base_spell_level=None,
        spellcasting_ability=ability or None,
        concentration=False,
        source_passive_effects=[],
        spell_book={},
        passive_damage_modifiers={},
        save_modifiers={},
        class_levels=levels,
        scale_values=build_scale_values(
            class_slug=actor.class_slug,
            subclass_slug=None,
            species_slug=None,
            level=actor.character_level,
            loader=loader,
            classes=levels,
        ),
    )
    for grant in finite:
        feature = loader.get_feature(grant)
        assert feature is not None
        assert feature.uses is not None
        if scalar_formula(feature.uses.max, context) != features[grant].maximum:
            return "feature capacity contradicts shared class scale"
    return None


def evoker_grants(loader: AssetLoader, canonical: AssetLoader, level: int) -> set[str] | None:
    """Resolve the reviewed Evoker feature grants from the bound and canonical datasets."""
    return _reviewed_subclass_grants(loader, canonical, "evoker", "wizard", level)


def _reviewed_subclass_grants(
    loader: AssetLoader, canonical: AssetLoader, subclass_slug: str, class_slug: str, level: int
) -> set[str] | None:
    """Resolve feature grants only when the subclass matches its canonical identity."""
    subclass = loader.get_subclass(subclass_slug)
    if (
        subclass is None
        or subclass != canonical.get_subclass(subclass_slug)
        or subclass.class_identifier != class_slug
    ):
        return None
    return {
        g.slug for g in subclass.granted_features if g.ref_type == "feature" and g.level <= level
    }


def operation_dependencies(
    snapshot: CombatSnapshot,
    loader: AssetLoader,
    *,
    movement: bool = False,
    turn_lifecycle: bool = False,
    allow_difficult_terrain: bool = False,
    attack: bool = False,
    reviewed_reactions: bool = False,
    equipped: bool = False,
    reviewed_sap: bool = False,
    reviewed_world_effects: bool = False,
    reviewed_region_conditions: bool = False,
    allow_stowed_healing_potions: bool = False,
    reviewed_class_lifecycle: bool = False,
    reviewed_closure: bool = False,
) -> str | None:
    """Pass/reset, walking, initialization and closure do not activate these grants.

    Attack/AC/damage/save operations must supply their own reviewed profile.
    Any world effect must already have been verified by the requesting operation.
    Plain Pass can keep reviewed stowed healing potions in the equipment closure.
    Foundation admission and neutral Pass can opt in to the reviewed Lore Bard.
    Modern closure can retain the reviewed Wizard pouch and terminal unconscious state.
    Other effects, reactions, traits and non-class mechanics retain ordinary guards.
    """
    from dnd5e_engine.evaluation_preflight import snapshot_support_failure

    canonical = get_default_ruleset()
    potion_owners = (
        reviewed_healing_potion_owners(snapshot, loader, canonical)
        if allow_stowed_healing_potions
        else set()
    )

    def dependencies(actor: CharacterStateV2) -> str | None:
        """Check the complete rules dependency set for the enclosing operation."""
        if actor.trait_mechanics:
            return "unreviewed operation trait"
        if attack and actor.weapon_mastery_slugs:
            if not reviewed_sap:
                return "trained weapon mastery attack execution is not migrated"
            if failure := equipped_dependencies(actor, snapshot, loader, canonical):
                return failure
        if not (
            actor.class_slug
            or actor.classes
            or actor.granted_features
            or actor.subclass_slug
            or actor.species_slug
            or actor.fighting_styles
            or actor.weapon_mastery_slugs
        ):
            return None
        return class_dependencies(
            actor,
            snapshot,
            loader,
            canonical,
            equipped=equipped or attack,
            allow_stowed_healing_potions=actor.entity_id in potion_owners,
            reviewed_lore_bard=reviewed_class_lifecycle,
            reviewed_wizard_pouch=reviewed_closure,
        )

    if reviewed_sap:
        if not turn_lifecycle:
            from dnd5e_engine.evaluation_death import turn_lifecycle_support_failure

            if not reviewed_region_conditions and any(
                c.condition != "prone" for a in snapshot.character_states for c in a.conditions
            ):
                return "only reviewed permanent implied Prone can compose with Sap attacks"
            if any(
                c.condition == "prone" for a in snapshot.character_states for c in a.conditions
            ) and loader.get_condition("prone") != canonical.get_condition("prone"):
                return "Prone condition differs from reviewed canonical clauses"
            if failure := turn_lifecycle_support_failure(
                snapshot, reviewed_region_conditions=reviewed_region_conditions
            ):
                return failure
        actors = {a.entity_id: a for a in snapshot.character_states}
        for target, source in snapshot.combat_state.sap_marks.items():
            if target == source or target not in actors or source not in actors:
                return "Sap mark requires distinct present source and target"
            origin = actors[source]
            if origin.classes != {"fighter": 1} or not origin.weapon_mastery_slugs:
                return "Sap source requires complete reviewed Fighter training"
            if failure := equipped_dependencies(origin, snapshot, loader, canonical):
                return failure
    return snapshot_support_failure(
        snapshot,
        movement=movement,
        turn_lifecycle=turn_lifecycle,
        allow_difficult_terrain=allow_difficult_terrain,
        actor_dependencies=dependencies,
        reviewed_reactions=reviewed_reactions,
        reviewed_sap=reviewed_sap,
        reviewed_conditions=reviewed_sap,
        reviewed_world_effects=reviewed_world_effects,
        reviewed_region_conditions=reviewed_region_conditions,
        reviewed_closure=reviewed_closure,
    )
