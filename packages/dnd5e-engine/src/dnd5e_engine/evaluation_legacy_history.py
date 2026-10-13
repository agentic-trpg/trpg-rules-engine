"""Explicit Legacy conversion boundary; never used by evaluate/availability."""

from collections.abc import Sequence, Set

from dnd5e_engine.events import ALL_COMBAT_EVENT_TYPES, CombatEnded, CombatEvent


def validate_legacy_history(
    events: Sequence[CombatEvent],
    *,
    ended: bool,
    applied_indices: Set[int],
) -> None:
    """Refuse contradictory closure and unconvertible log-relative bookkeeping.

    This checks a supplied, completed Legacy boundary; it does not reconstruct
    missing mechanical facts or make a partial log complete. Only the caller
    owning that boundary may supply it. No history is returned as current state.
    """
    if type(ended) is not bool or any(type(e) not in ALL_COMBAT_EVENT_TYPES for e in events):
        raise ValueError("invalid typed Legacy history boundary")
    closes = sum(isinstance(e, CombatEnded) for e in events)
    if closes > 1 or bool(closes) != ended:
        raise ValueError("Legacy ended fact contradicts CombatEnded history")
    if applied_indices:
        raise ValueError("Legacy legendary event indices have no admitted current-state conversion")
