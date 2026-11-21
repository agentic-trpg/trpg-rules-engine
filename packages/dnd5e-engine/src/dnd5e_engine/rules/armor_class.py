"""Interpret the reviewed scalar Armor Class floor and apply it after modifiers."""

from dnd5e_engine.types.effects import ActiveEffectChange

ARMOR_CLASS_FLOOR_KEYS = frozenset(("ac.min", "system.attributes.ac.min"))


def armor_class_floor_value(change: ActiveEffectChange) -> int:
    """Read a positive integer minimum AC from the reviewed upgrade change."""
    if change.mode != "upgrade" or type(change.value) not in (str, int):
        raise ValueError("an Armor Class floor requires an integer upgrade")
    floor = int(change.value)
    if floor <= 0:
        raise ValueError("an Armor Class floor must be positive")
    return floor


def apply_armor_class_floor(ordinary_ac: int, floor: int | None) -> int:
    """Apply the floor after all applicable ordinary Armor Class modifiers."""
    return ordinary_ac if floor is None else max(ordinary_ac, floor)
