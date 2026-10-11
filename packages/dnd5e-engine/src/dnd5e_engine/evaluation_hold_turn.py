"""Existing SM-authorized Hold Person graph across ordinary turn boundaries."""

from dnd5e_srd_data.loader import AssetLoader

from dnd5e_engine.effect_lifecycle import concentration_rounds_after_turn_end
from dnd5e_engine.evaluation_concentration import (
    ConcentrationComputation,
    concentration_support_failure,
)
from dnd5e_engine.evaluation_effect_turn import run_snapshot_effect_boundary
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
        run_snapshot_effect_boundary(self, event.actor_id, self.expire_linked_effect)
        remaining = self.state.concentration_rounds_remaining.get(event.actor_id)
        if remaining is None:
            return
        next_rounds = concentration_rounds_after_turn_end(remaining)
        if next_rounds is None:
            self.drop(event.actor_id, reason="duration")
        else:
            self.state = self.state.model_copy(
                update={
                    "concentration_rounds_remaining": {
                        **self.state.concentration_rounds_remaining,
                        event.actor_id: next_rounds,
                    }
                }
            )
