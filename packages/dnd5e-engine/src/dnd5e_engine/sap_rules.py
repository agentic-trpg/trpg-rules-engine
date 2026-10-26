"""Shared Sap mark transitions; hit decisions remain in AttackActivity."""

from collections.abc import Iterable, Mapping

from dnd5e_engine.events import AttackRolled, CombatEvent


def consume_sap(
    marks: Mapping[str, str], attacker_id: str, events: Iterable[CombatEvent]
) -> dict[str, str]:
    """Remove the Sap mark consumed by the target's attack."""
    value = dict(marks)
    if any(isinstance(e, AttackRolled) and e.attacker_id == attacker_id for e in events):
        value.pop(attacker_id, None)
    return value


def apply_sap(marks: Mapping[str, str], source_id: str, target_id: str) -> dict[str, str]:
    # One outstanding mark per creature, overwritten rather than stacked.
    """Replace or create the target's nonstacking Sap mark."""
    return {**marks, target_id: source_id}


def expire_sap(marks: Mapping[str, str], starting_actor_id: str) -> dict[str, str]:
    """Remove Sap marks expiring at the source actor's turn."""
    return {target: source for target, source in marks.items() if source != starting_actor_id}
