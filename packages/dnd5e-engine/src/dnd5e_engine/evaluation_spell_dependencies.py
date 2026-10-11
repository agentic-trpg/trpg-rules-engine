"""Reviewed dependencies for immediate healing and its plain turn boundary."""

from dnd5e_srd_data import BundledAssetLoader
from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.monster import MonsterTraitMechanic

from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.scale import build_scale_values
from dnd5e_engine.build_spec import derive_multiclass_pact_slots, derive_multiclass_slots
from dnd5e_engine.evaluation_actor import combatant
from dnd5e_engine.evaluation_preflight import snapshot_support_failure
from dnd5e_engine.evaluation_resources import FeatureResource, SlotResource
from dnd5e_engine.evaluation_state import CharacterStateV2, CombatSnapshot
from dnd5e_engine.feature_runtime import DrawFreeRandom, scalar_formula

# Canonical grants are reviewed together with the operation, not globally disabled.
# Divine Order/Defense/mastery affect equipment, skills or attacks, not this
# unarmored heal. Second Wind is separately priced; Spellcasting supplies slots.
_REVIEWED = {"cleric": 1, "fighter": 1}


def healing_dependencies(
    snapshot: CombatSnapshot, loader: AssetLoader, target_id: str
) -> str | None:
    canonical = BundledAssetLoader()
    if snapshot.resource_state is None:
        return "complete resource state is required"
    admitted = []
    for actor in snapshot.character_states:
        if failure := class_dependencies(actor, snapshot, loader, canonical):
            return failure
        traits = actor.trait_mechanics
        if traits and not (
            actor.entity_id == target_id
            and actor.entity_type != "Character"
            and set(traits) == {MonsterTraitMechanic.SWARM}
        ):
            return "unreviewed healing or turn trait"
        admitted.append(
            actor.model_copy(
                update={
                    "class_slug": None,
                    "classes": {},
                    "granted_features": (),
                    "fighting_styles": (),
                    "weapon_mastery_slugs": (),
                    "trait_mechanics": (),
                }
            )
        )
    # This copy proves only previously overbroad flags irrelevant; the actual
    # computation and result retain every original class, feature and trait.
    view = snapshot.model_copy(update={"character_states": tuple(admitted)})
    return snapshot_support_failure(view)


def class_dependencies(
    actor: CharacterStateV2,
    snapshot: CombatSnapshot,
    loader: AssetLoader,
    canonical: BundledAssetLoader,
) -> str | None:
    if actor.subclass_slug or actor.species_slug:
        return "subclass/species healing and turn dependencies are not migrated"
    if actor.worn_armor or actor.shield_equipped or actor.other_hand_occupied:
        return "only unarmored, unencumbered component profile is reviewed"
    if actor.fighting_styles not in ((), ("defense",)):
        return "unreviewed fighting style"
    if any(not k.startswith("feature_use:") for k in actor.custom_counters):
        return "unreviewed mechanical counter"
    if snapshot.inventory_state.entries or actor.carried_item_slugs:
        return "only the complete empty equipment profile is reviewed"
    levels = actor.classes or (
        {actor.class_slug: actor.character_level} if actor.class_slug else {}
    )
    if not levels:
        if actor.granted_features or actor.spell_slots or actor.pact_slots:
            return "class/source dependency is absent"
        return None
    if len(levels) != 1 or sum(levels.values()) != actor.character_level:
        return "only an explicit single-class profile is reviewed"
    slug, level = next(iter(levels.items()))
    if _REVIEWED.get(slug) != level or actor.class_slug not in (None, slug):
        return "class/level has unreviewed healing or turn dependencies"
    cls = loader.get_class(slug)
    if cls is None or cls != canonical.get_class(slug):
        return "class definition differs from the reviewed binding"
    grants = {g.slug for g in cls.granted_features if g.ref_type == "feature" and g.level <= level}
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
        spellcasting_ability=cls.spellcasting.ability or None,
        concentration=False,
        source_passive_effects=[],
        spell_book={},
        passive_damage_modifiers={},
        save_modifiers={},
        class_levels=levels,
        scale_values=build_scale_values(
            class_slug=slug,
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
