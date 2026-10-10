"""Lossless conversion of explicit actor facts; no runtime hydration/default facts."""

from dnd5e_engine.evaluation_state import CharacterStateV2
from dnd5e_engine.types.combat import Combatant


def combatant(state: CharacterStateV2) -> Combatant:
    value = state.model_dump(mode="python")
    for field in ("spell_slots", "pact_slots", "spells_known", "custom_counters"):
        del value[field]
    value["death_saves"] = value["death_saves"] or {}
    return Combatant.model_validate(value)


def actor_state(actor: Combatant, original: CharacterStateV2) -> CharacterStateV2:
    value = actor.model_dump(mode="python")
    value["death_saves"] = value["death_saves"] or None
    for field in ("spell_slots", "pact_slots", "spells_known", "custom_counters"):
        value[field] = getattr(original, field)
    return CharacterStateV2.model_validate(value)
