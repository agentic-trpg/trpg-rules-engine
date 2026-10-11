"""Bounded explicit Pass / End Turn over supplied facts; no Legacy hooks."""

from typing import Literal, TypedDict

from dnd5e_srd_data.loader import AssetLoader

from dnd5e_engine.evaluation_class_dependencies import operation_dependencies
from dnd5e_engine.evaluation_computation import CombatComputation
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, RuleError, RuleEvaluationRequest
from dnd5e_engine.evaluation_delta import StateDelta
from dnd5e_engine.evaluation_preflight import template_support_failure
from dnd5e_engine.evaluation_projection import EvaluationInvariantError, attack_delta
from dnd5e_engine.evaluation_rng import RNGTransition
from dnd5e_engine.evaluation_state import CombatSnapshot
from dnd5e_engine.events import CombatEvent, IntentSubmitted


class TurnEvaluation(TypedDict):
    status: Literal["accepted", "rejected", "unsupported"]
    state_delta: StateDelta | None
    proposed_events: tuple[CombatEvent, ...]
    rng_transition: RNGTransition | None
    choice: None
    error: RuleError | None


def _refuse(status: Literal["rejected", "unsupported"], code: str, reason: str) -> TurnEvaluation:
    return dict(
        status=status,
        state_delta=None,
        proposed_events=(),
        rng_transition=None,
        choice=None,
        error=RuleError(code=code, reason=reason),
    )


def evaluate_turn(request: RuleEvaluationRequest, loader: AssetLoader) -> TurnEvaluation:
    snapshot, intent = request.state_snapshot, request.payload
    if not isinstance(snapshot, CombatSnapshot) or not isinstance(intent, CombatIntentPayload):
        raise EvaluationInvariantError("turn evaluation requires a combat intent")
    basic = CombatIntentPayload(intent_type="pass", source_id=intent.source_id)
    if intent != basic:
        return _refuse("unsupported", "turn.intent", "End Turn accepts only explicit plain Pass")
    state = snapshot.combat_state
    if state.ended:
        return _refuse("rejected", "combat_ended", "combat has ended")
    if request.actor_id not in state.initiative_ids:
        return _refuse("rejected", "actor_invalid", "actor is absent from initiative")
    if request.actor_id != state.initiative_ids[state.current_turn_index]:
        return _refuse("rejected", "not_actor_turn", "actor does not own this turn")
    if request.actor_id in state.dead_ids:
        return _refuse("rejected", "actor_incapacitated", "recorded dead actors have no turn")
    # Lazy import keeps the shared R15 Save core independent of turn dispatch.
    from dnd5e_engine.evaluation_concentration import concentration_delta
    from dnd5e_engine.evaluation_effect_turn import (
        EffectTurnComputation,
        effect_turn_delta,
        effect_turn_support_failure,
    )
    from dnd5e_engine.evaluation_hold_turn import HoldTurnComputation, hold_turn_support_failure
    from dnd5e_engine.evaluation_opportunity import opportunity_dependencies
    from dnd5e_engine.evaluation_shield import (
        ShieldComputation,
        has_shield,
        shield_delta,
        shield_support_failure,
    )

    if has_shield(snapshot):
        failure = shield_support_failure(snapshot, loader, turn=True) or template_support_failure(
            snapshot, loader
        )
        if failure:
            return _refuse("unsupported", "turn.shield", failure)
        rng = request.rng_context.state.restore()
        shield = ShieldComputation(snapshot, rng, loader)
        shield.emit(IntentSubmitted(actor_id=request.actor_id, intent_type="pass"))
        shield.finish_turn(request.actor_id, force=True)
        return dict(
            status="accepted",
            state_delta=shield_delta(snapshot, shield.result()),
            proposed_events=tuple(shield.events),
            rng_transition=RNGTransition.between(request.rng_context, rng),
            choice=None,
            error=None,
        )

    _, reaction_failure = opportunity_dependencies(snapshot, loader)
    if reaction_failure:
        return _refuse("unsupported", "turn.reaction", reaction_failure)
    has_effect = bool(snapshot.effect_states)
    has_concentration = bool(state.concentration_chain)
    failure = (
        hold_turn_support_failure(snapshot, loader)
        if has_concentration
        else effect_turn_support_failure(snapshot, loader)
        if has_effect
        else operation_dependencies(
            snapshot,
            loader,
            turn_lifecycle=True,
            movement=True,
            reviewed_reactions=bool(state.pending_reactions),
            attack=bool(state.pending_reactions),
            equipped=True,
            reviewed_sap=not bool(state.pending_reactions),
        )
        or template_support_failure(snapshot, loader)
    )
    if failure:
        return _refuse("unsupported", "turn.capability", failure)
    rng = request.rng_context.state.restore()
    computation_type = (
        HoldTurnComputation
        if has_concentration
        else EffectTurnComputation
        if has_effect
        else CombatComputation
    )
    computation = computation_type(snapshot, rng)
    computation.emit(IntentSubmitted(actor_id=request.actor_id, intent_type="pass"))
    # Pass pays no Action; it closes the turn even with unused budgets.
    computation.finish_turn(request.actor_id, force=True)
    after = computation.result()
    return dict(
        status="accepted",
        state_delta=(
            concentration_delta
            if has_concentration
            else effect_turn_delta
            if has_effect
            else attack_delta
        )(snapshot, after),
        proposed_events=tuple(computation.events),
        rng_transition=RNGTransition.between(request.rng_context, rng),
        choice=None,
        error=None,
    )
