"""Per-evaluation actor updates, immutable record updates and typed event folds.

Only actor updates, typed combat-field updates and a short event buffer live here.
The immutable input supplies all other facts. Admission excludes lifecycle systems
that this vertical slice cannot yet execute; this is not a general combat runtime.
"""

import random
from dataclasses import dataclass

from dnd5e_engine import attack_rules
from dnd5e_engine.activities.context import DamageInstanceContext
from dnd5e_engine.activities.effects import is_condition_immune
from dnd5e_engine.damage_rules import (
    damage_balances,
    death_record,
    healing_balance,
    with_event_condition,
    zero_hp_damage,
)
from dnd5e_engine.death_saves import roll_death_save
from dnd5e_engine.evaluation_actor import actor_state, combatant
from dnd5e_engine.evaluation_reaction_state import (
    OpportunityAttackState,
    StatBlockOpportunityAttackState,
)
from dnd5e_engine.evaluation_stage_capture import StageRecorder, stage_profile_reason
from dnd5e_engine.evaluation_stages import EventStageFacts, StageKind, StageReason
from dnd5e_engine.evaluation_state import CombatSnapshot, MovementLedgerState
from dnd5e_engine.events import (
    ActorMoved,
    AttackRolled,
    CombatEvent,
    ConditionApplied,
    ConditionRemoved,
    DamageApplied,
    Death,
    HealingApplied,
    RoundStarted,
    TurnEnded,
    TurnPhase,
    TurnStarted,
)
from dnd5e_engine.movement import MovementMode, project_speeds, speed_for_mode
from dnd5e_engine.rules.conditions import active_condition_names, exhaustion_level_of
from dnd5e_engine.turn_rules import (
    next_turn_index,
    reset_turn_budget,
)
from dnd5e_engine.types.combat import Combatant


@dataclass(frozen=True)
class CombatComputationResult:
    """Private candidate mechanics and only this call's ordered proposals.

    The candidate is input to exhaustive Delta projection, never an instruction
    to persist an entire Snapshot. Committed audit history is not appended here.
    """

    candidate_state: CombatSnapshot
    proposed_events: tuple[CombatEvent, ...]
    stage_facts: EventStageFacts


class CombatComputation:
    """Resolve one fixed combat input into isolated working actors, events and stage
    evidence.
    """

    def __init__(self, snapshot: CombatSnapshot, rng: random.Random) -> None:
        """Create isolated working actors, event/damage buffers and initially disabled
        stage recording.
        """
        self.snapshot = snapshot
        self.actors = {a.entity_id: combatant(a) for a in snapshot.character_states}
        self.state = snapshot.combat_state
        self.rng = rng
        self.events: list[CombatEvent] = []
        # A hit can contain several damage types. Zero-HP rules run once on the
        # completed instance, after temporary HP absorbed each ordered packet.
        self.damage: dict[tuple[str, str], tuple[int, DamageApplied, int]] = {}
        self.stage_recorder: StageRecorder | None = None
        self.stage_reason: StageReason = "operation_not_in_stage_profile"

    def enable_stages(self) -> None:
        """Enable stage capture only for the audited computation profile."""
        reason = stage_profile_reason(self.snapshot)
        if type(self) is not CombatComputation:
            reason = "complex_effects_or_reactions"
        if reason is None:
            self.stage_recorder = StageRecorder(self.snapshot)
        else:
            self.stage_reason = reason

    def sample_stage(self, kind: StageKind, actor_ids: tuple[str, ...] | None = None) -> None:
        """Capture a mechanical stage when a stage recorder is active."""
        if self.stage_recorder is not None:
            self.stage_recorder.sample(kind, len(self.events), self.actors, self.state, actor_ids)

    def sample_event(self, event: CombatEvent, index: int) -> None:
        """Capture stage references for the emitted event's affected actors."""
        if self.stage_recorder is not None:
            ids = {
                value
                for name in ("actor_id", "attacker_id", "target_id", "source_actor_id")
                if (value := getattr(event, name, None)) in self.actors
            }
            self.stage_recorder.event(
                index,
                self.actors,
                self.state,
                ids,
                attack_roll=isinstance(event, AttackRolled),
                movement=isinstance(event, ActorMoved),
            )

    def speed(self, actor: Combatant, mode: MovementMode = "walk") -> int:
        """Project the actor's effective speed for the selected movement mode."""
        speeds = project_speeds(
            actor.base_speed,
            actor.movement_modes,
            condition_names=active_condition_names(actor.conditions),
            exhaustion_level=exhaustion_level_of(actor.conditions),
        )
        return speed_for_mode(speeds, mode)

    def emit(self, event: CombatEvent) -> None:
        """Append an event and fold its mechanics into this computation's candidate state.

        Condition immunity may suppress a condition event. Damage/healing, conditions,
        death, turn/effect and resource consequences update working actors/components;
        tracked damage instances defer once-only zero-HP settlement until completion.
        Stage sampling records supported event points. No host persistence occurs here.
        """
        if isinstance(event, ConditionApplied) and is_condition_immune(
            self.actors[event.target_id], event.condition
        ):
            return
        index = len(self.events)
        self.events.append(event)
        if isinstance(event, AttackRolled):
            if event.attacker_id == self.state.initiative_ids[self.state.current_turn_index]:
                actor = self.actors[event.attacker_id]
                self.actors[actor.entity_id] = actor.model_copy(
                    update={"attack_rolls_made_this_turn": actor.attack_rolls_made_this_turn + 1}
                )
        elif isinstance(event, DamageApplied):
            target = self.actors[event.target_id]
            hp, temp, amount = damage_balances(target.hp_current, target.temp_hp, event.amount)
            update: dict[str, object] = {"hp_current": hp, "temp_hp": temp}
            if event.source_actor_id and event.source_actor_id != event.target_id:
                update["last_damaged_by"] = event.source_actor_id
            self.actors[target.entity_id] = target.model_copy(update=update)
            if event.damage_instance_id is not None and (
                target.entity_id in self.state.party_ids
                or target.entity_id in self.state.concentration_chain
            ):
                key = (target.entity_id, event.damage_instance_id)
                before, first, total = self.damage.get(key, (target.hp_current, event, 0))
                self.damage[key] = (before, first, total + amount)
            elif hp <= 0 and target.entity_id not in self.state.dead_ids:
                # Capture the packet before the nested Death append/fold.
                self.sample_event(event, index)
                self.record_death(
                    Death(target_id=target.entity_id, reason="damage"), event.source_actor_id
                )
                return
        elif isinstance(event, HealingApplied):
            actor = self.actors[event.target_id]
            self.actors[actor.entity_id] = actor.model_copy(
                update={"hp_current": healing_balance(actor.hp_current, actor.hp_max, event.amount)}
            )
        elif isinstance(event, Death):
            self.fold_death(event, None)
        elif isinstance(event, ConditionApplied):
            actor = self.actors[event.target_id]
            actor = with_event_condition(
                actor, event.condition, round_number=self.state.round_number
            )
            ledger = self.state.movement_ledgers.get(actor.entity_id)
            remaining = (
                ledger.remaining(self.speed(actor, ledger.active_mode))
                if ledger
                else self.speed(actor)
            )
            self.actors[actor.entity_id] = actor.model_copy(
                update={"movement_remaining": remaining}
            )
        elif isinstance(event, ConditionRemoved):
            actor = self.actors[event.target_id]
            actor = actor.model_copy(
                update={
                    "conditions": [c for c in actor.conditions if c.condition != event.condition]
                }
            )
            ledger = self.state.movement_ledgers[actor.entity_id]
            self.actors[actor.entity_id] = actor.model_copy(
                update={
                    "movement_remaining": ledger.remaining(self.speed(actor, ledger.active_mode))
                }
            )

        self.sample_event(event, index)

    def fold_death(self, event: Death, killer: str | None) -> None:
        """Update candidate actor/death records, ignoring an already recorded death."""
        if event.target_id in self.state.dead_ids:
            return
        actor = self.actors[event.target_id]
        self.actors[event.target_id] = actor.model_copy(update={"is_alive": False})
        record = death_record(
            actor, event, location_id=self.snapshot.scene_state.scene_id, killer_id=killer
        )
        self.state = self.state.model_copy(
            update={
                "dead_ids": self.state.dead_ids | {event.target_id},
                "deaths_recorded": [*self.state.deaths_recorded, record],
            }
        )

    def record_death(self, event: Death, killer: str | None) -> None:
        """Fold a candidate death and append its event-stage evidence."""
        self.fold_death(event, killer)
        index = len(self.events)
        self.events.append(event)
        self.sample_event(event, index)

    def next_damage_id(
        self, target_id: str, source_id: str | None, *, actor_id: str | None = None
    ) -> str:
        """Advance candidate damage sequence and return a deterministic instance identity."""
        sequence = self.state.damage_instance_sequence + 1
        self.state = self.state.model_copy(update={"damage_instance_sequence": sequence})
        actor_id = (
            actor_id
            or self.snapshot.combat_state.initiative_ids[
                self.snapshot.combat_state.current_turn_index
            ]
        )
        return f"damage:{sequence}:{actor_id}:{target_id}"

    def damage_completed(self, damage: DamageInstanceContext) -> None:
        """Finish a damage instance and capture the resulting mechanical stage."""
        self._damage_completed(damage)
        self.sample_stage("damage_instance_completed", (damage.target_id,))

    def _damage_completed(self, damage: DamageInstanceContext) -> None:
        """Resolve completed damage's zero-HP consequences once per tracked instance."""
        pending = self.damage.pop((damage.target_id, damage.damage_instance_id), None)
        if pending is None or damage.target_id in self.state.dead_ids:
            return
        actor = self.actors[damage.target_id]
        if actor.hp_current > 0:
            return
        hp_before, event, amount = pending
        if actor.entity_id not in self.state.party_ids:
            self.record_death(
                Death(target_id=actor.entity_id, reason="damage"), event.source_actor_id
            )
            return
        result = zero_hp_damage(
            actor,
            event,
            hp_before=hp_before,
            damage_after_temp=amount,
            processed=frozenset(self.state.processed_zero_hp_damage_instances),
        )
        self.actors[actor.entity_id] = result.actor
        self.state = self.state.model_copy(
            update={"processed_zero_hp_damage_instances": set(result.processed)}
        )
        for ev in result.events:
            if isinstance(ev, Death):
                self.record_death(ev, event.source_actor_id)
            else:
                self.emit(ev)

    def death_save(self, actor_id: str) -> None:
        """Resolve an eligible death save and capture its resulting stage."""
        cursor = len(self.events)
        self._death_save(actor_id)
        if len(self.events) != cursor:
            self.sample_stage("death_save_completed", (actor_id,))

    def _death_save(self, actor_id: str) -> None:
        """Resolve an eligible actor's death save with explicit RNG and fold its events."""
        actor = self.actors[actor_id]
        if (
            actor.entity_type != "Character"
            or actor.hp_current > 0
            or actor_id in self.state.dead_ids
            or actor.death_saves.get("is_stable")
        ):
            return
        result = roll_death_save(actor, self.rng)
        for event in result.events:
            self.emit(event)
        self.actors[actor_id] = result.combatant
        if result.outcome == "critical_success":
            self.emit(ConditionRemoved(target_id=actor_id, condition="unconscious"))
            self.emit(ConditionApplied(target_id=actor_id, condition="prone"))

    def finish_turn(
        self, actor_id: str, *, allow_movement: bool = False, force: bool = False
    ) -> None:
        """Finish or retain the current turn and update candidate round, budgets and
        lifecycle state.
        """
        actor = self.actors[actor_id]
        # All other payment/windows are explicitly excluded by admission.
        if not force and (
            actor.action_available
            or not attack_rules.attack_action_is_spent(actor)
            or (allow_movement and actor.movement_remaining > 0 and self.speed(actor) > 0)
        ):
            self.death_save(actor_id)
            return
        self.emit(
            TurnPhase(actor_id=actor_id, phase="turn_end", round_number=self.state.round_number)
        )
        self.emit(TurnEnded(actor_id=actor_id))
        # The empty-area boundary still projects every active movement ledger.
        # No area hooks run: admission has excluded their state and effects.
        for key, value in self.actors.items():
            ledger = self.state.movement_ledgers[key]
            self.actors[key] = value.model_copy(
                update={
                    "movement_remaining": ledger.remaining(self.speed(value, ledger.active_mode))
                }
            )
        next_turn = next_turn_index(
            self.state.initiative_ids, self.state.dead_ids, self.state.current_turn_index + 1
        )
        self.state = self.state.model_copy(
            update={"last_ended_turn": (self.state.round_number, actor_id)}
        )
        if next_turn is None:
            self.sample_stage("turn_advanced")
            return
        index, new_round = next_turn
        self.state = self.state.model_copy(
            update={
                "current_turn_index": index,
                "round_number": self.state.round_number + int(new_round),
            }
        )
        self.sample_stage("turn_advanced")
        if new_round:
            self.emit(RoundStarted(round_number=self.state.round_number))
            self.emit(
                TurnPhase(actor_id=None, phase="round_start", round_number=self.state.round_number)
            )
        next_actor = self.state.initiative_ids[index]
        self.state = self.state.model_copy(
            update={
                "pending_reactions": [
                    p
                    for p in self.state.pending_reactions
                    if not (
                        isinstance(p, (OpportunityAttackState, StatBlockOpportunityAttackState))
                        and p.owner_id == next_actor
                    )
                ]
            }
        )
        from dnd5e_engine.sap_rules import expire_sap

        self.state = self.state.model_copy(
            update={"sap_marks": expire_sap(self.state.sap_marks, next_actor)}
        )
        self.emit(TurnStarted(actor_id=next_actor))
        self.state = self.state.model_copy(
            update={
                "turn_serial": self.state.turn_serial + 1,
                "movement_ledgers": {
                    **self.state.movement_ledgers,
                    next_actor: MovementLedgerState(
                        spent_ft=0, distance_ft=0, active_mode="walk", dash_count=0
                    ),
                },
            }
        )
        self.actors[next_actor] = reset_turn_budget(
            self.actors[next_actor], effective_speed=self.speed(self.actors[next_actor]), attacks=1
        )
        for key, value in self.actors.items():
            if value.sneak_attack_spent_this_turn:
                self.actors[key] = value.model_copy(update={"sneak_attack_spent_this_turn": False})
        self.sample_stage("budget_reset")
        self.emit(
            TurnPhase(actor_id=next_actor, phase="turn_start", round_number=self.state.round_number)
        )
        self.death_save(next_actor)

    def candidate_state(self) -> CombatSnapshot:
        """Project this computation's working actors and components into a new candidate
        snapshot.
        """
        return self.snapshot.model_copy(
            update={
                "character_states": tuple(
                    actor_state(self.actors[a.entity_id], a) for a in self.snapshot.character_states
                ),
                "combat_state": self.state,
            }
        )

    def result(self) -> CombatComputationResult:
        """Return the candidate, ordered events and validated stage coverage without
        persisting them.
        """
        facts = (
            self.stage_recorder.finish(len(self.events), self.actors, self.state)
            if self.stage_recorder is not None
            else EventStageFacts.uncovered(self.stage_reason)
        )
        facts.verify_input(self.snapshot, tuple(self.events))
        return CombatComputationResult(self.candidate_state(), tuple(self.events), facts)
