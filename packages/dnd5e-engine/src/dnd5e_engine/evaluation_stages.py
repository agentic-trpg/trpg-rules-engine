"""Request-local mechanical stage candidate, not world Evidence or commit edits.

Unchanged facts refer to the enclosing request Snapshot. Sparse immutable views
cover only the declared projection, never all world entities or sensory facts.
"""

from typing import Annotated, Literal, Self

from pydantic import Field, field_serializer, model_validator

from dnd5e_engine.evaluation_base import EvaluationModel
from dnd5e_engine.evaluation_delta import AttackBudgetState, DeathState, TurnState
from dnd5e_engine.evaluation_state import (
    CharacterStateV2,
    CombatSnapshot,
    CombatState,
    ConditionRecord,
    DeathSaveRecord,
    MovementLedgerState,
)
from dnd5e_engine.events import CombatEvent
from dnd5e_engine.spatial import GridTopology, canonical_cell_id
from dnd5e_engine.specs import GridScene
from dnd5e_engine.types.combat import Combatant

StageReason = Literal[
    "operation_not_in_stage_profile", "complex_effects_or_reactions", "complex_mechanics"
]
StageKind = Literal[
    "input",
    "event",
    "attack_execution",
    "damage_instance_completed",
    "death_save_completed",
    "turn_advanced",
    "budget_reset",
    "movement_step",
    "operation_completed",
]
SamplePoint = Literal[
    "input_reference",
    "after_event_fold",
    "before_resolution",
    "after_damage_instance",
    "after_death_save",
    "after_turn_advance",
    "after_budget_reset",
    "after_step",
    "operation_end",
]
_SAMPLES: dict[str, str] = dict(
    input="input_reference",
    event="after_event_fold",
    attack_execution="before_resolution",
    damage_instance_completed="after_damage_instance",
    death_save_completed="after_death_save",
    turn_advanced="after_turn_advance",
    budget_reset="after_budget_reset",
    movement_step="after_step",
    operation_completed="operation_end",
)


class StageActorFacts(EvaluationModel):
    """Only mutable fields in this stage profile; all other facts reference input."""

    actor_id: Annotated[str, Field(min_length=1)]
    cell: Annotated[str, Field(min_length=1)]
    hp_current: Annotated[int, Field(ge=0)]
    temp_hp: Annotated[int, Field(ge=0)]
    death: DeathState
    conditions: tuple[ConditionRecord, ...]
    last_damaged_by: str | None
    budget: AttackBudgetState
    movement: MovementLedgerState

    @model_validator(mode="after")
    def canonical_position(self) -> Self:
        if canonical_cell_id(self.cell) != self.cell:
            raise ValueError("stage position must be canonical")
        return self


class StageCombatFacts(EvaluationModel):
    turn: TurnState
    dead_ids: frozenset[str]
    damage_instance_sequence: Annotated[int, Field(ge=0)]
    processed_zero_hp_damage_instances: frozenset[tuple[str, str]]

    @field_serializer("dead_ids", when_used="json")
    def ordered_deaths(self, value: frozenset[str]) -> tuple[str, ...]:
        return tuple(sorted(value))

    @field_serializer("processed_zero_hp_damage_instances", when_used="json")
    def ordered_instances(self, value: frozenset[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(value))


class MechanicalStage(EvaluationModel):
    sequence: Annotated[int, Field(ge=0)]
    kind: StageKind
    sample_point: SamplePoint
    # Emission frontier at sampling, not an authoritative event_seq or identity.
    event_cursor: Annotated[int, Field(ge=0)]
    actor_updates: tuple[StageActorFacts, ...]
    combat_update: StageCombatFacts | None

    @model_validator(mode="after")
    def unique_changes(self) -> Self:
        if self.sample_point != _SAMPLES[self.kind]:
            raise ValueError("stage kind and sample point disagree")
        if len({a.actor_id for a in self.actor_updates}) != len(self.actor_updates):
            raise ValueError("duplicate stage actor")
        return self


class EventStageReference(EvaluationModel):
    event_index: Annotated[int, Field(ge=0)]
    stage_sequences: Annotated[tuple[int, ...], Field(min_length=1)]
    event_role: Literal["mechanical_audit"]

    @model_validator(mode="after")
    def ordered_references(self) -> Self:
        if tuple(sorted(set(self.stage_sequences))) != self.stage_sequences:
            raise ValueError("duplicate or unordered stage reference")
        return self


class EventStageFacts(EvaluationModel):
    schema_version: Literal["engine-stage-facts/1"]
    input_reference: Literal["request.state_snapshot"]
    projection_scope: Literal["bounded_participant_mechanics"]
    world_coverage: Literal["outside_snapshot_unknown"]
    coverage: Literal["complete", "uncovered"]
    reason: StageReason | None
    participant_ids: tuple[str, ...]
    stages: tuple[MechanicalStage, ...]
    event_references: tuple[EventStageReference, ...]
    final_stage: int | None

    @model_validator(mode="after")
    def structure(self) -> Self:
        if self.coverage == "uncovered":
            if self.reason is None or any(
                (
                    self.participant_ids,
                    self.stages,
                    self.event_references,
                    self.final_stage is not None,
                )
            ):
                raise ValueError("uncovered stages require a reason and no execution records")
            return self
        if self.reason is not None or not self.participant_ids or len(self.stages) < 2:
            raise ValueError("complete stage projection requires participants and boundaries")
        if tuple(sorted(set(self.participant_ids))) != self.participant_ids:
            raise ValueError("duplicate or unordered stage participants")
        if tuple(s.sequence for s in self.stages) != tuple(range(len(self.stages))):
            raise ValueError("stage sequences must be contiguous request-local ordinals")
        first, last = self.stages[0], self.stages[-1]
        if (
            first.kind != "input"
            or first.event_cursor != 0
            or first.actor_updates
            or first.combat_update is not None
            or last.kind != "operation_completed"
            or self.final_stage != last.sequence
        ):
            raise ValueError("invalid input or final stage boundary")
        if any(s.kind in {"input", "operation_completed"} for s in self.stages[1:-1]):
            raise ValueError("duplicate input or final stage boundary")
        cursors = tuple(s.event_cursor for s in self.stages)
        if tuple(sorted(cursors)) != cursors:
            raise ValueError("stage emission frontiers cannot go backwards")
        indices = tuple(r.event_index for r in self.event_references)
        if indices != tuple(range(len(indices))) or last.event_cursor != len(indices):
            raise ValueError("event references must cover the returned local sequence exactly")
        referenced_events = set()
        for reference in self.event_references:
            if any(i < 0 or i >= len(self.stages) for i in reference.stage_sequences):
                raise ValueError("stage reference outside this result")
            points = [self.stages[i] for i in reference.stage_sequences]
            emitted = [s for s in points if s.kind == "event"]
            if len(emitted) != 1 or emitted[0].event_cursor != reference.event_index + 1:
                raise ValueError("event reference requires its actual post-fold sampling point")
            referenced_events.add(emitted[0].sequence)
            if any(
                s.kind not in {"event", "movement_step", "attack_execution"}
                or (s.kind != "event" and s.sequence >= emitted[0].sequence)
                for s in points
            ):
                raise ValueError("invalid auxiliary event stage reference")
        if referenced_events != {s.sequence for s in self.stages if s.kind == "event"}:
            raise ValueError("unreferenced event sampling point")
        if any(
            a.actor_id not in self.participant_ids for s in self.stages for a in s.actor_updates
        ):
            raise ValueError("stage actor is outside the declared projection")
        return self

    @classmethod
    def uncovered(cls, reason: StageReason) -> Self:
        return cls(
            schema_version="engine-stage-facts/1",
            input_reference="request.state_snapshot",
            projection_scope="bounded_participant_mechanics",
            world_coverage="outside_snapshot_unknown",
            coverage="uncovered",
            reason=reason,
            participant_ids=(),
            stages=(),
            event_references=(),
            final_stage=None,
        )

    def verify_input(self, snapshot: CombatSnapshot, events: tuple[CombatEvent, ...]) -> None:
        """Resolve references against the exact enclosing request, without rule execution."""
        if self.coverage == "uncovered":
            return
        actors = {a.entity_id: a for a in snapshot.character_states}
        if not set(self.participant_ids) <= actors.keys():
            raise ValueError("stage participant is absent from the input closure")
        if len(events) != len(self.event_references):
            raise ValueError("stage event references differ from proposed_events")
        topology = GridTopology(GridScene(**snapshot.scene_state.grid.model_dump()))
        for stage in self.stages:
            for actor in stage.actor_updates:
                if actor.hp_current > actors[actor.actor_id].hp_max:
                    raise ValueError("stage HP exceeds the input maximum")
                if not topology.is_valid_cell(actor.cell):
                    raise ValueError("stage position is outside input geometry")
                if actor.last_damaged_by is not None and actor.last_damaged_by not in actors:
                    raise ValueError("stage damage attribution is outside input actors")
            combat = stage.combat_update
            if combat is not None and (
                not combat.dead_ids <= actors.keys()
                or combat.turn.current_turn_index >= len(snapshot.combat_state.initiative_ids)
                or (
                    combat.turn.departed_actor_id is not None
                    and combat.turn.departed_actor_id not in actors
                )
                or any(a not in actors for a, _ in combat.processed_zero_hp_damage_instances)
                or any(
                    v is not None and v[1] not in actors
                    for v in (combat.turn.last_ended_turn, combat.turn.monster_turn_start_done)
                )
            ):
                raise ValueError("stage combat reference is outside input closure")


def actor_facts(
    actor: CharacterStateV2 | Combatant, cell: str, movement: MovementLedgerState
) -> StageActorFacts:
    """Detach only this projection; nested carriers contain no mutable list/dict."""
    death_saves = actor.death_saves
    return StageActorFacts(
        actor_id=actor.entity_id,
        cell=cell,
        hp_current=actor.hp_current,
        temp_hp=actor.temp_hp,
        death=DeathState(
            is_alive=actor.is_alive,
            death_saves=DeathSaveRecord.model_validate(death_saves) if death_saves else None,
        ),
        conditions=tuple(ConditionRecord.model_validate(c.model_dump()) for c in actor.conditions),
        last_damaged_by=actor.last_damaged_by,
        budget=AttackBudgetState.model_validate(
            {name: getattr(actor, name) for name in AttackBudgetState.model_fields}
        ),
        movement=MovementLedgerState.model_validate(movement),
    )


def combat_facts(state: CombatState) -> StageCombatFacts:
    return StageCombatFacts(
        turn=TurnState.model_validate(
            {name: getattr(state, name) for name in TurnState.model_fields}
        ),
        dead_ids=frozenset(state.dead_ids),
        damage_instance_sequence=state.damage_instance_sequence,
        processed_zero_hp_damage_instances=frozenset(state.processed_zero_hp_damage_instances),
    )
