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
from dnd5e_engine.turn_rules import next_turn_index


def hold_turn_support_failure(snapshot: CombatSnapshot, loader: AssetLoader) -> str | None:
    """Check dependencies for the admitted Hold Person turn lifecycle."""
    combat = snapshot.combat_state
    sources = combat.concentration_chain
    if len(sources) != 1:
        return "exactly one complete existing Hold Person source is reviewed"
    source_id = next(iter(sources))
    nonpositive = tuple(a for a in snapshot.character_states if a.hp_current <= 0 or not a.is_alive)
    if nonpositive:
        # The weapon path can leave a newly brought to 0 HP source concentrating
        # when immunity prevented Unconscious. Continue a healthy-to-healthy turn
        # handoff without composing this effect graph with a dying Turn boundary.
        source = nonpositive[0]
        next_turn = next_turn_index(
            combat.initiative_ids, combat.dead_ids, combat.current_turn_index + 1
        )
        if (
            len(nonpositive) != 1
            or source.entity_id != source_id
            or source.entity_type != "Character"
            or not source.is_alive
            or "unconscious" not in source.condition_immunities
            or source.conditions
            or (
                source.death_saves is not None
                and (
                    source.death_saves.successes
                    or source.death_saves.failures
                    or source.death_saves.is_stable
                )
            )
            or next_turn is None
            or source_id
            in {
                combat.initiative_ids[combat.current_turn_index],
                combat.initiative_ids[next_turn[0]],
            }
        ):
            return "Hold Person turn graph requires living positive-HP actors at both boundaries"
    return concentration_support_failure(
        snapshot,
        source_id,
        loader,
        composed=True,
        turn_lifecycle=True,
        reviewed_dying_source=bool(nonpositive),
    ) or template_support_failure(snapshot, loader)


class HoldTurnComputation(ConcentrationComputation):
    """Resolve the admitted Hold Person turn lifecycle in isolated candidate state."""

    def emit(self, event: CombatEvent) -> None:
        """Append mechanical event evidence and fold its consequences into this candidate
        computation.
        """
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
