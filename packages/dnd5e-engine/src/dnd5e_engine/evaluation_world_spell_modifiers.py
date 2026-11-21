"""Project reviewed elapsed spell effects for ordinary combat mechanics."""

from collections.abc import Iterable

from dnd5e_engine.evaluation_effects import EffectState
from dnd5e_engine.evaluation_timed_spells import attack_world_spell_modifiers


def world_spell_combat_modifiers(
    effects: Iterable[EffectState],
) -> dict[str, tuple[int, int | None]]:
    """Return walking bonus and Armor Class floor by target from live elapsed spells.

    Reject unsupported mechanics or conflicting contributions to the same stat.
    The caller provides the active effects and retains responsibility for the
    rest of the combat profile, including reactions and turn-bound effects.
    """
    modifiers: dict[str, tuple[int, int | None]] = {}
    for effect in effects:
        walk, floor = attack_world_spell_modifiers(effect)
        prior_walk, prior_floor = modifiers.get(effect.target_id, (0, None))
        if (walk and prior_walk) or (floor is not None and prior_floor is not None):
            raise ValueError("elapsed spells have overlapping combat modifiers on one target")
        modifiers[effect.target_id] = (prior_walk + walk, prior_floor if floor is None else floor)
    return modifiers
