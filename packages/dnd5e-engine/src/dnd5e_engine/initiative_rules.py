"""Shared Initiative calculation; no registration or session runtime."""

import random

from dnd5e_engine.activities.d20 import AdvantageSources, roll_d20_test


def resolve_initiative_value(
    fixed: int | None, modifier: int, sources: AdvantageSources, surprised: bool, rng: random.Random
) -> int:
    if fixed is not None:
        return fixed
    effective = AdvantageSources(
        advantage=sources.advantage,
        disadvantage=sources.disadvantage + (("condition:attacker",) if surprised else ()),
    )
    return roll_d20_test(rng, modifier, effective).total


def initiative_order_key(initiative: int, dexterity: int, entity_id: str) -> tuple[int, int, str]:
    return -initiative, -dexterity, entity_id
