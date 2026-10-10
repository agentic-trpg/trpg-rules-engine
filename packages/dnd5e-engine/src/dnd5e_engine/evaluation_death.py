"""Shared admission of complete terminal death facts; no death rule inference."""

from dnd5e_engine.evaluation_state import CombatSnapshot


def death_consistency(snapshot: CombatSnapshot) -> str | None:
    state = snapshot.combat_state
    actors = {actor.entity_id: actor for actor in snapshot.character_states}
    records = state.deaths_recorded
    if (
        len({r.target_id for r in records}) != len(records)
        or {r.target_id for r in records} != state.dead_ids
    ):
        return "death records must identify every dead actor exactly once"
    kinds = {"Character": "character", "Monster": "monster", "NPC": "npc"}
    for record in records:
        actor = actors.get(record.target_id)
        if (
            actor is None
            or actor.hp_current != 0
            or actor.is_alive
            or record.target_kind != kinds[actor.entity_type]
            or record.location_id != snapshot.scene_state.scene_id
            or (record.killer_id is not None and record.killer_id not in actors)
        ):
            return "death record contradicts actor, HP, life state, kind, location or killer"
    for actor in actors.values():
        if actor.entity_id not in state.dead_ids and (actor.hp_current == 0 or not actor.is_alive):
            return "dying or dead actors without a terminal death record are not migrated"
    return None
