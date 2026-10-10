"""Shared admission of complete terminal death facts; no death rule inference."""

from dnd5e_engine.evaluation_state import CombatSnapshot


def death_consistency(snapshot: CombatSnapshot, *, allow_dying: bool = False) -> str | None:
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
            if (
                allow_dying
                and actor.entity_type == "Character"
                and actor.hp_current == 0
                and actor.is_alive
            ):
                continue
            return "dying or dead actors without a terminal death record are not migrated"
    return None


def turn_lifecycle_support_failure(snapshot: CombatSnapshot) -> str | None:
    """Only permanent implied unconscious/prone and consistent bounded PC dying state."""
    for actor in snapshot.character_states:
        for condition in actor.conditions:
            if (
                condition.condition not in {"unconscious", "prone"}
                or condition.source_entity_id not in {"implied:event", "implied:revive"}
                or condition.scope != "combat"
                or condition.duration_rounds is not None
                or condition.save_dc is not None
                or condition.source_effect_id is not None
                or condition.exhaustion_level != 1
            ):
                return "condition expiry, provenance or save hooks are not migrated"
            if condition.condition == "unconscious" and actor.hp_current > 0:
                return "unconscious source is outside the bounded dying state"
        if actor.entity_id in snapshot.combat_state.dead_ids:
            continue
        saves = actor.death_saves
        if actor.hp_current > 0:
            if saves is not None and (saves.successes or saves.failures or saves.is_stable):
                return "living actor has stale death-save counters"
            continue
        if (
            not any(c.condition == "unconscious" for c in actor.conditions)
            and "unconscious" not in actor.condition_immunities
        ):
            return "dying Character requires its implied unconscious condition"
        if saves is not None and (saves.failures >= 3 or saves.is_stable != (saves.successes == 3)):
            return "nonterminal death saves contradict stable/dead status"
    return None
