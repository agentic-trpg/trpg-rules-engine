"""Finite-resource arithmetic and existing Spellcasting-before-Pact selection.

No authority, mutation, RNG, command or source authorization is provided here.
"""

from collections.abc import Mapping
from typing import Literal


def consume_resource(current: int, cost: int) -> int:
    if current < 0 or cost <= 0 or cost > current:
        raise ValueError("invalid or insufficient resource payment")
    return current - cost


def choose_spell_slot(
    spell: Mapping[int, int], pact: Mapping[int, int], level: int
) -> tuple[Literal["spell_slot", "pact_slot"], int] | None:
    choices: tuple[tuple[Literal["spell_slot", "pact_slot"], Mapping[int, int]], ...] = (
        ("spell_slot", spell),
        ("pact_slot", pact),
    )
    for kind, pool in choices:
        current = int(pool.get(level, 0))
        if current > 0:
            return kind, consume_resource(current, 1)
    return None
