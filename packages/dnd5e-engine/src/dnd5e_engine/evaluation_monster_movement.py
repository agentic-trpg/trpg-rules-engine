"""Closed canonical monster dependencies for ordinary reactive movement."""

from dnd5e_srd_data import BundledAssetLoader
from dnd5e_srd_data.loader import AssetLoader

from dnd5e_engine.evaluation_npc_initialization import monster_actor_failure
from dnd5e_engine.evaluation_state import CombatSnapshot


def monster_movement_failure(snapshot: CombatSnapshot, loader: AssetLoader) -> str | None:
    """Validate every binding, even without consent or an available Reaction."""
    actors = {a.entity_id: a for a in snapshot.character_states}
    for actor_id, slug in snapshot.combat_state.monster_slug_by_entity.items():
        monster = loader.get_monster(slug)
        if slug != "ogre" or monster is None or monster != BundledAssetLoader().get_monster(slug):
            return "only the pinned ordinary Ogre movement/reach profile is reviewed"
        actor = actors[actor_id]
        if (
            actor.entity_type not in {"NPC", "Monster"}
            or actor_id not in snapshot.combat_state.encounter_ids
        ):
            return "bound stat-block movement requires an encounter NPC/Monster"
        failure = monster_actor_failure(actor, monster)
        if failure:
            return failure
        if (
            actor.melee_reach_ft != 5
            or actor.character_level != 1
            or actor.granted_features != ()
            or actor.class_slug
            or actor.classes
            or actor.subclass_slug
            or actor.species_slug
            or actor.fighting_styles
            or actor.weapon_mastery_slugs
            or actor.weapon_proficiencies is not None
            or actor.carried_item_slugs
            or actor.weapon_in_hands
            or actor.other_hand_occupied
            or actor.worn_armor
            or actor.shield_equipped
            or actor.stealth_disadvantage
            or actor.spells_known
            or actor.spell_slots
            or actor.pact_slots
            or actor.custom_counters
            or actor.skill_check_bonuses
            or actor.jack_of_all_trades
            or actor.reliable_talent
            or actor.tool_proficiencies
            or actor.has_fled
            or any(e.owner_id == actor_id for e in snapshot.inventory_state.entries)
            or (
                snapshot.resource_state is not None
                and any(p.owner_id == actor_id for p in snapshot.resource_state.pools)
            )
            or (
                snapshot.casting_state is not None
                and any(a.actor_id == actor_id and a.sources for a in snapshot.casting_state.actors)
            )
            or actor_id in snapshot.combat_state.opportunity_attack_weapons
        ):
            return "bound Ogre has unreviewed class, equipment, resource or reach facts"
    return None
