"""Lossless conversion of explicit actor facts; no runtime hydration/default facts."""

from dnd5e_engine.evaluation_state import CharacterStateV2
from dnd5e_engine.types.combat import Combatant


def combatant(state: CharacterStateV2) -> Combatant:
    """Construct an isolated combatant working value from snapshot character state."""
    value = state.model_dump(
        mode="python",
        exclude={
            "spell_slots",
            "pact_slots",
            "spells_known",
            "custom_counters",
            "held_weapon_instance_id",
            "worn_armor_instance_id",
            "equipped_shield_instance_id",
            "offhand_item_instance_id",
        },
    )
    value["death_saves"] = value["death_saves"] or {}
    return Combatant.model_validate(value)


def actor_state(actor: Combatant, original: CharacterStateV2) -> CharacterStateV2:
    """Project the working combatant back into the supplied snapshot actor shape."""
    value = actor.model_dump(mode="python")
    value["death_saves"] = value["death_saves"] or None
    for field in (
        "spell_slots",
        "pact_slots",
        "spells_known",
        "custom_counters",
        "held_weapon_instance_id",
        "worn_armor_instance_id",
        "equipped_shield_instance_id",
        "offhand_item_instance_id",
    ):
        value[field] = getattr(original, field)
    return CharacterStateV2.model_validate(value)
