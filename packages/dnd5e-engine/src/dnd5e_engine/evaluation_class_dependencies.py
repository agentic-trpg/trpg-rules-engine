"""Canonical class closure for reviewed operations over complete actor facts."""

from dnd5e_srd_data import BundledAssetLoader
from dnd5e_srd_data.loader import AssetLoader

from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.scale import build_scale_values
from dnd5e_engine.build_spec import derive_multiclass_pact_slots, derive_multiclass_slots
from dnd5e_engine.evaluation_actor import combatant
from dnd5e_engine.evaluation_resources import FeatureResource, SlotResource
from dnd5e_engine.evaluation_state import CharacterStateV2, CombatSnapshot, NonCombatSnapshot
from dnd5e_engine.feature_runtime import DrawFreeRandom, scalar_formula

# Canonical grants are reviewed together with the operation, not globally disabled.
# Divine Order/Defense/mastery affect equipment, skills or attacks, not this
# unarmored heal. Second Wind is separately priced; Spellcasting supplies slots.
_REVIEWED = {"cleric": 1, "fighter": 1, "wizard": 5}


def class_dependencies(
    actor: CharacterStateV2,
    snapshot: CombatSnapshot | NonCombatSnapshot,
    loader: AssetLoader,
    canonical: BundledAssetLoader,
) -> str | None:
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
    if actor.worn_armor or actor.shield_equipped or actor.other_hand_occupied:
        return "only unarmored, unencumbered component profile is reviewed"
    if actor.fighting_styles not in ((), ("defense",)):
        return "unreviewed fighting style"
    if any(not k.startswith("feature_use:") for k in actor.custom_counters):
        return "unreviewed mechanical counter"
    if (
        any(e.owner_id == actor.entity_id for e in snapshot.inventory_state.entries)
        or actor.carried_item_slugs
    ):
        return "only the complete empty equipment profile is reviewed"
    if actor.subclass_slug and not (levels == {"wizard": 5} and actor.subclass_slug == "evoker"):
        return "unreviewed subclass dependencies"
    if levels == {"wizard": 5} and actor.subclass_slug != "evoker":
        return "complete reviewed Evoker 5 subclass is required"
    if len(levels) != 1 or sum(levels.values()) != actor.character_level:
        return "only an explicit single-class profile is reviewed"
    slug, level = next(iter(levels.items()))
    if _REVIEWED.get(slug) != level or actor.class_slug not in (None, slug):
        return "class/level has unreviewed operation dependencies"
    cls = loader.get_class(slug)
    if cls is None or cls != canonical.get_class(slug):
        return "class definition differs from the reviewed binding"
    grants = {g.slug for g in cls.granted_features if g.ref_type == "feature" and g.level <= level}
    if slug == "wizard":
        additional = evoker_grants(loader, canonical, level)
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


def evoker_grants(
    loader: AssetLoader, canonical: BundledAssetLoader, level: int
) -> set[str] | None:
    subclass = loader.get_subclass("evoker")
    if (
        subclass is None
        or subclass != canonical.get_subclass("evoker")
        or subclass.class_identifier != "wizard"
    ):
        return None
    # The full Evoker 5 grants are neutral to instant Abjuration saves and
    # ordinary healing/turns; retain all facts in computation and output.
    return {
        g.slug for g in subclass.granted_features if g.ref_type == "feature" and g.level <= level
    }


def operation_dependencies(
    snapshot: CombatSnapshot,
    loader: AssetLoader,
    *,
    movement: bool = False,
    turn_lifecycle: bool = False,
    attack: bool = False,
) -> str | None:
    """Pass/reset, walking, initialization and closure do not activate these grants.

    Attack/AC/damage/save operations must supply their own reviewed profile.
    Effects, reactions, traits and all non-class mechanics retain ordinary guards.
    """
    from dnd5e_engine.evaluation_preflight import snapshot_support_failure

    canonical = BundledAssetLoader()

    def dependencies(actor: CharacterStateV2) -> str | None:
        if actor.trait_mechanics:
            return "unreviewed operation trait"
        if attack and actor.weapon_mastery_slugs:
            return "trained weapon mastery attack execution is not migrated"
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
        return class_dependencies(actor, snapshot, loader, canonical)

    return snapshot_support_failure(
        snapshot,
        movement=movement,
        turn_lifecycle=turn_lifecycle,
        actor_dependencies=dependencies,
    )
