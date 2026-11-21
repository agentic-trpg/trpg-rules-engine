"""Project reviewed elapsed spell effects for ordinary combat mechanics."""

from collections.abc import Iterable

from dnd5e_engine.evaluation_effects import EffectState
from dnd5e_engine.evaluation_timed_spells import (
    attack_world_spell_modifiers,
    is_canonical_elapsed_longstrider,
)


def world_walk_bonus(effect: EffectState) -> int:
    """Read an admitted elapsed passive effect's flat walking-speed change."""
    bonus, armor_floor = attack_world_spell_modifiers(effect)
    if armor_floor is not None:
        raise ValueError("timed walking cannot compose an Armor Class floor")
    if not any(change.key == "system.attributes.movement.walk" for change in effect.changes):
        raise ValueError("timed walking requires a flat walking-speed modifier")
    return bonus


def world_spell_combat_modifiers(
    effects: Iterable[EffectState],
    *,
    walking_only: bool = False,
) -> dict[str, tuple[int, int | None]]:
    """Project one effective contribution from each reviewed elapsed spell.

    The caller supplies active effects and owns their deadlines. Equal
    Longstrider casts keep separate identities and contribute once during
    overlap. Other conflicts raise ValueError. A walking-only caller requires
    a flat walk modifier and no Armor Class floor on each supplied effect.
    """
    modifiers: dict[str, tuple[int, int | None]] = {}
    walk_effects: dict[str, EffectState] = {}
    walk_origins: set[tuple[str, str]] = set()
    for effect in effects:
        walk, floor = (
            (world_walk_bonus(effect), None)
            if walking_only
            else attack_world_spell_modifiers(effect)
        )
        target = effect.target_id
        prior_walk, prior_floor = modifiers.get(target, (0, None))
        if floor is not None and prior_floor is not None:
            raise ValueError("elapsed spells have overlapping combat modifiers on one target")
        if walk:
            identity = (target, effect.origin)
            if prior_walk:
                if identity in walk_origins or not (
                    is_canonical_elapsed_longstrider(walk_effects[target])
                    and is_canonical_elapsed_longstrider(effect)
                ):
                    raise ValueError(
                        "elapsed spells have overlapping combat modifiers on one target"
                    )
                walk = 0
            else:
                walk_effects[target] = effect
            walk_origins.add(identity)
        modifiers[target] = (prior_walk + walk, prior_floor if floor is None else floor)
    return modifiers
