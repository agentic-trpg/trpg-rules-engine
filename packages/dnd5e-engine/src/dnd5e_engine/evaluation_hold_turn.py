"""Existing SM-authorized Hold Person graph across ordinary turn boundaries."""

from dnd5e_srd_data.loader import AssetLoader

from dnd5e_engine.evaluation_concentration import (
    ConcentrationComputation,
    concentration_support_failure,
)
from dnd5e_engine.evaluation_preflight import template_support_failure
from dnd5e_engine.evaluation_state import CombatSnapshot
from dnd5e_engine.events import CombatEvent, TurnPhase


def hold_turn_support_failure(snapshot: CombatSnapshot, loader: AssetLoader) -> str | None:
    sources = snapshot.combat_state.concentration_chain
    if len(sources) != 1:
        return "exactly one complete existing Hold Person source is reviewed"
    if any(not a.is_alive or a.hp_current <= 0 for a in snapshot.character_states):
        return "Hold Person turn graph requires living positive-HP actors"
    return concentration_support_failure(
        snapshot, next(iter(sources)), loader, composed=True, turn_lifecycle=True
    ) or template_support_failure(snapshot, loader)


class HoldTurnComputation(ConcentrationComputation):
    def emit(self, event: CombatEvent) -> None:
        super().emit(event)
        if not isinstance(event, TurnPhase) or event.phase != "turn_end":
            return
        assert event.actor_id is not None
        self.concentration.turn_end(event.actor_id)
