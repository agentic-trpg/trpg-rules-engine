"""Consume target-side one-use save clauses through authoritative events."""

from __future__ import annotations

from typing import TYPE_CHECKING

from dnd5e_engine.events import EffectModifiersConsumed

if TYPE_CHECKING:
    from dnd5e_engine.orchestrator import _LiveCombat


def consume_next_save_modifier(live: _LiveCombat, target_id: str) -> bool:
    """Consume each active next-save disadvantage grant, retaining other clauses.

    All matching sources are spent on the same actual saving throw, including
    automatic failure or cancellation by advantage. Disabled effects and false
    or incorrectly-modeled changes are inert. Full identities retain lineage.
    """
    from dnd5e_engine import orchestrator as orch

    disadvantaged = False
    for effect in list(live.active_effects.get(target_id, [])):
        if effect.disabled:
            continue
        if any(
            change.key == "flags.save.next_disadvantage"
            and change.mode == "override"
            and (change.value is True or change.value == "true")
            for change in effect.changes
        ):
            disadvantaged = True
            orch._emit(
                live,
                EffectModifiersConsumed(
                    target_id=target_id,
                    effect_id=effect.id,
                    origin=effect.origin,
                    keys=("flags.save.next_disadvantage",),
                ),
            )
    return disadvantaged
