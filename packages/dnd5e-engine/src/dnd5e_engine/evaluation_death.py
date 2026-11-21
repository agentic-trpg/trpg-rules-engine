"""Shared admission of complete terminal death facts; no death rule inference."""

from dnd5e_engine.evaluation_region_effect import is_fixed_combat_poisoned_condition
from dnd5e_engine.evaluation_state import CharacterStateV2, CombatSnapshot, ConditionRecord


def death_consistency(snapshot: CombatSnapshot, *, allow_dying: bool = False) -> str | None:
    """Check actor, death-save and combat death records for supported consistency."""
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


def _is_permanent_implied_combat_condition(condition: ConditionRecord) -> bool:
    """Identify an implied condition without expiry, source-effect or save hooks."""
    return (
        condition.source_entity_id in {"implied:event", "implied:revive"}
        and condition.scope == "combat"
        and condition.duration_rounds is None
        and condition.save_dc is None
        and condition.source_effect_id is None
        and condition.exhaustion_level == 1
    )


def reviewed_terminal_unconscious(actor: CharacterStateV2, dead_ids: set[str]) -> bool:
    """Recognize the retained knockout condition after validated Character death."""
    return (
        actor.entity_type == "Character"
        and actor.entity_id in dead_ids
        and len(actor.conditions) == 1
        and actor.conditions[0].condition == "unconscious"
        and actor.conditions[0].source_entity_id == "implied:event"
        and _is_permanent_implied_combat_condition(actor.conditions[0])
    )


def turn_lifecycle_support_failure(
    snapshot: CombatSnapshot, *, reviewed_region_conditions: bool = False
) -> str | None:
    """Admit implied life conditions and optional already-verified fixed region Poisoned."""
    for actor in snapshot.character_states:
        for condition in actor.conditions:
            if reviewed_region_conditions and is_fixed_combat_poisoned_condition(condition):
                continue
            if condition.condition not in {
                "unconscious",
                "prone",
            } or not _is_permanent_implied_combat_condition(condition):
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
