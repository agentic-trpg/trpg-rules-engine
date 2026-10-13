"""Private request-local sampling; never execute rules or consume RNG here."""

from collections.abc import Iterable, Mapping

from dnd5e_engine.activities.passive_stats import CombatantMovementModes
from dnd5e_engine.evaluation_stages import (
    _SAMPLES,
    EventStageFacts,
    EventStageReference,
    MechanicalStage,
    StageKind,
    StageReason,
    actor_facts,
    combat_change,
    combat_facts,
)
from dnd5e_engine.evaluation_state import CombatSnapshot, CombatState
from dnd5e_engine.movement import project_speeds, speed_for_mode
from dnd5e_engine.types.combat import Combatant


def stage_profile_reason(snapshot: CombatSnapshot) -> StageReason | None:
    """Coverage admission only; never change rule admission or execution."""
    state = snapshot.combat_state
    if any(
        (
            snapshot.effect_states,
            state.concentration_chain,
            state.pending_reactions,
            state.active_reaction_responses,
            state.reaction_effects_pending_expiry,
            state.timed_activities.pending,
            state.persistent_areas.areas,
            state.persistent_areas.next_turn_start_effects,
            state.combat_objects.objects,
        )
    ):
        return "complex_effects_or_reactions"
    if state.sap_marks or any(
        a.weapon_mastery_slugs
        or any(
            c.condition not in {"unconscious", "prone"}
            or c.source_entity_id not in {"implied:event", "implied:revive"}
            for c in a.conditions
        )
        for a in snapshot.character_states
    ):
        return "complex_mechanics"
    for actor in snapshot.character_states:
        ledger = state.movement_ledgers[actor.entity_id]
        speeds = project_speeds(
            actor.base_speed,
            CombatantMovementModes.model_validate(actor.movement_modes.model_dump()),
            condition_names=tuple(c.condition for c in actor.conditions),
        )
        if actor.movement_remaining != ledger.remaining(speed_for_mode(speeds, ledger.active_mode)):
            return "input_budget_ledger_not_aligned"
    return None


class StageRecorder:
    """Cache bounded immutable values once, then retain only changed projections."""

    def __init__(self, snapshot: CombatSnapshot) -> None:
        self.snapshot = snapshot
        self.participants = tuple(sorted(a.entity_id for a in snapshot.character_states))
        state = snapshot.combat_state
        self.actors = {
            a.entity_id: actor_facts(
                a, state.actor_zone[a.entity_id], state.movement_ledgers[a.entity_id]
            )
            for a in snapshot.character_states
        }
        self.combat = combat_facts(state)
        self.stages = [
            MechanicalStage(
                sequence=0,
                kind="input",
                sample_point="input_reference",
                event_cursor=0,
                actor_updates=(),
                combat_update=None,
            )
        ]
        self.references: list[EventStageReference] = []
        self.execution: int | None = None
        self.movement_points: list[int] = []

    def sample(
        self,
        kind: StageKind,
        cursor: int,
        actors: Mapping[str, Combatant],
        state: CombatState,
        actor_ids: Iterable[str] | None = None,
    ) -> int:
        changed = []
        for key in sorted(set(self.participants if actor_ids is None else actor_ids)):
            value = actor_facts(actors[key], state.actor_zone[key], state.movement_ledgers[key])
            if value != self.actors[key]:
                changed.append(value)
                self.actors[key] = value
        combat = combat_facts(state)
        combat_update = combat_change(self.combat, combat)
        self.combat = combat
        sequence = len(self.stages)
        self.stages.append(
            MechanicalStage(
                sequence=sequence,
                kind=kind,
                sample_point=_SAMPLES[kind],
                event_cursor=cursor,
                actor_updates=tuple(changed),
                combat_update=combat_update,
            )
        )
        if kind == "attack_execution":
            self.execution = sequence
        elif kind == "movement_step":
            self.movement_points.append(sequence)
        return sequence

    def event(
        self,
        index: int,
        actors: Mapping[str, Combatant],
        state: CombatState,
        actor_ids: Iterable[str],
        *,
        attack_roll: bool = False,
        movement: bool = False,
    ) -> None:
        sequence = self.sample("event", index + 1, actors, state, actor_ids)
        points: tuple[int, ...] = (
            (self.execution, sequence)
            if attack_roll and self.execution is not None
            else (sequence,)
        )
        if movement:
            points = (*self.movement_points, sequence)
            self.movement_points.clear()
        self.references.append(
            EventStageReference(
                event_index=index,
                stage_sequences=points,
                event_role="mechanical_audit",
            )
        )

    def finish(
        self, cursor: int, actors: Mapping[str, Combatant], state: CombatState
    ) -> EventStageFacts:
        self.sample("operation_completed", cursor, actors, state)
        value = EventStageFacts(
            schema_version="engine-stage-facts/2",
            input_reference="request.state_snapshot",
            projection_scope="bounded_participant_mechanics",
            world_coverage="outside_snapshot_unknown",
            coverage="complete",
            reason=None,
            participant_ids=self.participants,
            stages=tuple(self.stages),
            event_references=tuple(self.references),
            final_stage=len(self.stages) - 1,
        )
        return value
