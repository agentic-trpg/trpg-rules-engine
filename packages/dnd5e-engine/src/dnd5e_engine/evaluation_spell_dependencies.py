"""Reviewed immediate-healing admission without removing any input facts."""

from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.monster import MonsterTraitMechanic

from dnd5e_engine.evaluation_class_dependencies import class_dependencies
from dnd5e_engine.evaluation_preflight import snapshot_support_failure
from dnd5e_engine.evaluation_reactions import reaction_dependencies
from dnd5e_engine.evaluation_ruleset import get_default_ruleset
from dnd5e_engine.evaluation_state import CharacterStateV2, CombatSnapshot


def invocation_dependencies(
    snapshot: CombatSnapshot, loader: AssetLoader, target_id: str, *, damaging: bool
) -> str | None:
    """Check rules dependencies needed by the selected invocation."""
    if not damaging:
        return healing_dependencies(snapshot, loader, target_id)
    if snapshot.combat_state.pending_reactions:
        return "instant damage spell reactions are not migrated"
    if snapshot.effect_states or snapshot.combat_state.concentration_chain:
        from dnd5e_engine.evaluation_concentration import concentration_support_failure

        return concentration_support_failure(snapshot, target_id, loader, composed=True)
    if any(a.trait_mechanics for a in snapshot.character_states):
        return "unreviewed spell damage/save trait"
    from dnd5e_engine.evaluation_saves import save_actor_failure

    if failure := healing_dependencies(snapshot, loader, target_id):
        return failure
    for actor in snapshot.character_states:
        if actor.granted_features is None:
            return "complete explicit feature repertoire is required for spell damage/saves"
        if failure := save_actor_failure(
            actor, loader, reviewed_identity=True, reviewed_equipment=True
        ):
            return failure
    return None


def healing_dependencies(
    snapshot: CombatSnapshot, loader: AssetLoader, target_id: str
) -> str | None:
    """Check dependencies required by the admitted healing invocation."""
    if failure := reaction_dependencies(snapshot, loader):
        return failure
    canonical = get_default_ruleset()

    def dependencies(actor: CharacterStateV2) -> str | None:
        """Check the complete rules dependency set for the enclosing operation."""
        if failure := class_dependencies(actor, snapshot, loader, canonical, equipped=True):
            return f"unreviewed spell component/equipment profile: {failure}"
        if not (actor.class_slug or actor.classes) and (
            actor.worn_armor or actor.shield_equipped or actor.other_hand_occupied
        ):
            return "unreviewed healing equipment profile"
        if any(not key.startswith("feature_use:") for key in actor.custom_counters):
            return "unreviewed healing counter"
        for slug in actor.carried_item_slugs:
            item = loader.get_item(slug)
            if item is None or item.passive_effects or item.requires_attunement or item.uses:
                return "unreviewed healing equipment dependency"
        if actor.trait_mechanics and not (
            actor.entity_id == target_id
            and actor.entity_type != "Character"
            and set(actor.trait_mechanics) == {MonsterTraitMechanic.SWARM}
        ):
            return "unreviewed healing or turn trait"
        return None

    return snapshot_support_failure(
        snapshot, actor_dependencies=dependencies, reviewed_reactions=True
    )
