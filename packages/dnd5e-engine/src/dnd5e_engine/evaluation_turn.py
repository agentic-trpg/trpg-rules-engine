"""Bounded explicit Pass over supplied TurnEnd and TurnStart facts."""

from functools import partial
from typing import Literal, NotRequired, TypedDict

from dnd5e_srd_data.loader import AssetLoader

from dnd5e_engine.evaluation_burning import apply_burning_turn_start, burning_turn_input
from dnd5e_engine.evaluation_class_dependencies import operation_dependencies
from dnd5e_engine.evaluation_computation import CombatComputation, TurnStartConflictError
from dnd5e_engine.evaluation_contracts import (
    CombatIntentPayload,
    MultiSpellWillingnessChoice,
    ProposedEvents,
    RuleError,
    RuleEvaluationRequest,
    SpellWillingnessChoice,
)
from dnd5e_engine.evaluation_delta import StateDelta
from dnd5e_engine.evaluation_preflight import template_support_failure
from dnd5e_engine.evaluation_projection import EvaluationInvariantError, attack_delta
from dnd5e_engine.evaluation_rng import RNGTransition
from dnd5e_engine.evaluation_speed_effects import (
    SpeedEffectComputation,
    has_speed_effects,
    speed_effect_delta,
    speed_effect_support_failure,
    without_speed_effect,
)
from dnd5e_engine.evaluation_stages import EventStageFacts
from dnd5e_engine.evaluation_state import CombatSnapshot
from dnd5e_engine.events import IntentSubmitted


class TurnEvaluation(TypedDict):
    """Describe uncommitted fields shared by turn and other operation evaluators."""

    status: Literal["accepted", "rejected", "unsupported", "needs_choice"]
    state_delta: StateDelta | None
    proposed_events: ProposedEvents
    rng_transition: RNGTransition | None
    choice: SpellWillingnessChoice | MultiSpellWillingnessChoice | None
    error: RuleError | None
    stage_facts: NotRequired[EventStageFacts]


def _refuse(status: Literal["rejected", "unsupported"], code: str, reason: str) -> TurnEvaluation:
    """Build a refusal with no Delta, proposed events or RNG transition."""
    return dict(
        status=status,
        state_delta=None,
        proposed_events=(),
        rng_transition=None,
        choice=None,
        error=RuleError(code=code, reason=reason),
    )


def evaluate_turn(request: RuleEvaluationRequest, loader: AssetLoader) -> TurnEvaluation:
    """Evaluate an explicit Pass and return the ordered, uncommitted handoff.

    A simultaneous start boundary whose ordering remains undecided provides no
    candidate, events or RNG transition.
    """
    try:
        return _evaluate_turn(request, loader)
    except TurnStartConflictError as exc:
        return _refuse("unsupported", "turn.start_order", str(exc))


def _evaluate_turn(request: RuleEvaluationRequest, loader: AssetLoader) -> TurnEvaluation:
    """Check the dependency profile, then advance the turn in isolation."""
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
    from dnd5e_engine.evaluation_haste import (
        HasteComputation,
        has_haste_effects,
        haste_delta,
        haste_support_failure,
    )
    from dnd5e_engine.evaluation_hold_turn import HoldTurnComputation, hold_turn_support_failure
    from dnd5e_engine.evaluation_opportunity import opportunity_dependencies
    from dnd5e_engine.evaluation_region_effect import supports_fixed_combat_regions
    from dnd5e_engine.evaluation_shield import (
        ShieldComputation,
        has_shield,
        shield_delta,
        shield_support_failure,
    )

    try:
        snapshot, burning = burning_turn_input(snapshot, loader)
    except ValueError as exc:
        return _refuse("unsupported", "turn.burning", str(exc))
    start_effect = partial(apply_burning_turn_start, burning, loader) if burning else None
    haste_effect = has_haste_effects(snapshot)
    shield_effect = has_shield(snapshot)
    if burning and (
        (snapshot.effect_states and not haste_effect) or state.concentration_chain or shield_effect
    ):
        return _refuse(
            "unsupported", "turn.burning", "Burning composes with a plain turn or Lethargy"
        )
    if haste_effect:
        if request.schema_version != "engine-evaluation/30":
            return _refuse("unsupported", "turn.haste", "Haste turns require evaluation/30")
        if failure := haste_support_failure(snapshot, loader):
            return _refuse("unsupported", "turn.haste", failure)
        rng = request.rng_context.state.restore()
        haste = HasteComputation(snapshot, rng)
        haste.enable_stages()
        haste.emit(IntentSubmitted(actor_id=request.actor_id, intent_type="pass"))
        haste.finish_turn(request.actor_id, start_effect=start_effect)
        computed = haste.result()
        # RUO-45-F2: closure cannot yet adopt a surviving Lethargy graph.
        if (
            burning
            and computed.candidate_state.effect_states
            and haste.state.dead_ids != state.dead_ids
            and haste.has_terminal_outcome()
        ):
            return _refuse(
                "unsupported",
                "turn.burning_closure",
                "RUO-45-F2: terminal Burning requires supported closure of outstanding Lethargy",
            )
        return dict(
            status="accepted",
            state_delta=haste_delta(snapshot, computed.candidate_state, haste.follow_ups),
            proposed_events=computed.proposed_events,
            rng_transition=RNGTransition.between(request.rng_context, rng),
            choice=None,
            error=None,
            stage_facts=computed.stage_facts,
        )

    if shield_effect:
        failure = shield_support_failure(snapshot, loader, turn=True) or template_support_failure(
            snapshot, loader
        )
        if failure:
            return _refuse("unsupported", "turn.shield", failure)
        rng = request.rng_context.state.restore()
        shield = ShieldComputation(snapshot, rng, loader)
        shield.enable_stages()
        shield.emit(IntentSubmitted(actor_id=request.actor_id, intent_type="pass"))
        shield.finish_turn(request.actor_id)
        computed = shield.result()
        return dict(
            status="accepted",
            state_delta=shield_delta(snapshot, computed.candidate_state),
            proposed_events=computed.proposed_events,
            rng_transition=RNGTransition.between(request.rng_context, rng),
            choice=None,
            error=None,
            stage_facts=computed.stage_facts,
        )

    _, reaction_failure = opportunity_dependencies(snapshot, loader)
    if reaction_failure:
        return _refuse("unsupported", "turn.reaction", reaction_failure)
    speed_effect = has_speed_effects(snapshot)
    if speed_effect and (failure := speed_effect_support_failure(snapshot, loader)):
        return _refuse("unsupported", "turn.capability", failure)
    dependencies = without_speed_effect(snapshot) if speed_effect else snapshot
    reviewed_regions = supports_fixed_combat_regions(snapshot, loader)
    has_effect = bool(dependencies.effect_states) and not reviewed_regions
    has_concentration = bool(state.concentration_chain)
    failure = (
        hold_turn_support_failure(snapshot, loader)
        if has_concentration
        else effect_turn_support_failure(snapshot, loader)
        if has_effect
        else operation_dependencies(
            dependencies,
            loader,
            turn_lifecycle=True,
            movement=True,
            reviewed_reactions=bool(state.pending_reactions),
            attack=bool(state.pending_reactions),
            equipped=True,
            reviewed_sap=not bool(state.pending_reactions),
            allow_stowed_healing_potions=not (state.pending_reactions or state.sap_marks),
            reviewed_class_lifecycle=not (state.pending_reactions or state.sap_marks),
            reviewed_region_conditions=reviewed_regions,
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
        else SpeedEffectComputation
        if speed_effect
        else CombatComputation
    )
    computation = computation_type(snapshot, rng)
    if burning:
        computation.stage_reason = "complex_effects_or_reactions"
    else:
        computation.enable_stages()
    computation.emit(IntentSubmitted(actor_id=request.actor_id, intent_type="pass"))
    # Pass pays no Action; it closes the turn even with unused budgets.
    computation.finish_turn(request.actor_id, start_effect=start_effect)
    computed = computation.result()
    after = computed.candidate_state
    return dict(
        status="accepted",
        state_delta=(
            concentration_delta
            if has_concentration
            else effect_turn_delta
            if has_effect
            else speed_effect_delta
            if speed_effect
            else attack_delta
        )(snapshot, after),
        proposed_events=computed.proposed_events,
        rng_transition=RNGTransition.between(request.rng_context, rng),
        choice=None,
        error=None,
        stage_facts=computed.stage_facts,
    )
