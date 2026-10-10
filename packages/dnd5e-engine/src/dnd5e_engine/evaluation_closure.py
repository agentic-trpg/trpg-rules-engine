"""Bounded, RNG-free closure using the shared value-only outcome rules."""

from typing import Literal, TypedDict

from dnd5e_srd_data.loader import AssetLoader

from dnd5e_engine.evaluation_actor import combatant
from dnd5e_engine.evaluation_contracts import RuleError, RuleEvaluationRequest
from dnd5e_engine.evaluation_death import death_consistency
from dnd5e_engine.evaluation_delta import CombatClose, HistoricalCombatOutcome, StateDelta
from dnd5e_engine.evaluation_preflight import snapshot_support_failure, template_support_failure
from dnd5e_engine.evaluation_projection import EvaluationInvariantError
from dnd5e_engine.evaluation_rng import RNGTransition
from dnd5e_engine.evaluation_state import ClosureDeath, CombatSnapshot
from dnd5e_engine.events import CombatEnded
from dnd5e_engine.outcome import DeathRecord
from dnd5e_engine.outcome_rules import derive_ended_reason, project_outcome


class ClosureEvaluation(TypedDict):
    status: Literal["accepted", "rejected", "unsupported"]
    state_delta: StateDelta | None
    proposed_events: tuple[CombatEnded, ...]
    rng_transition: RNGTransition | None
    choice: None
    error: RuleError | None


def _refuse(
    status: Literal["rejected", "unsupported"], code: str, reason: str
) -> ClosureEvaluation:
    return dict(
        status=status,
        state_delta=None,
        proposed_events=(),
        rng_transition=None,
        choice=None,
        error=RuleError(code=code, reason=reason),
    )


def evaluate_closure(request: RuleEvaluationRequest, loader: AssetLoader) -> ClosureEvaluation:
    snapshot = request.state_snapshot
    if not isinstance(snapshot, CombatSnapshot):
        raise EvaluationInvariantError("closure requires a combat snapshot")
    state = snapshot.combat_state
    if request.actor_id not in state.initiative_ids:
        return _refuse("rejected", "actor_invalid", "closure actor is absent from the roster")
    if state.ended or any(isinstance(event, CombatEnded) for event in state.event_log):
        return _refuse("rejected", "combat_ended", "combat has already ended")
    failure = snapshot_support_failure(snapshot) or template_support_failure(snapshot, loader)
    if failure:
        return _refuse("unsupported", "closure.capability", failure)
    failure = death_consistency(snapshot)
    if failure:
        return _refuse("unsupported", "closure.death_state", failure)
    if not state.party_ids or not state.encounter_ids:
        return _refuse("rejected", "closure.nonterminal", "both combat sides must be nonempty")
    if not set(state.expended_resources) <= state.party_ids or any(
        not resource or count < 0
        for resources in state.expended_resources.values()
        for resource, count in resources.items()
    ):
        return _refuse("unsupported", "closure.resources", "resource ledger is not representable")
    actors = [combatant(a) for a in snapshot.character_states]
    reason = derive_ended_reason(actors, state.party_ids, state.encounter_ids, state.dead_ids)
    if reason not in ("victory", "defeat_tpk"):
        return _refuse("rejected", "closure.nonterminal", "combat is not a victory or TPK")
    outcome = project_outcome(
        combat_id=state.combat_id,
        actors=actors,
        party_ids=state.party_ids,
        encounter_ids=state.encounter_ids,
        dead_ids=state.dead_ids,
        hp={a.entity_id: a.hp_current for a in snapshot.character_states},
        temp_hp={a.entity_id: a.temp_hp for a in snapshot.character_states},
        vanishing_temp_hp=set(),
        xp_values=state.xp_value_by_entity,
        deaths=[DeathRecord.model_validate(d.model_dump()) for d in state.deaths_recorded],
        expended_resources=state.expended_resources,
    )
    # Fail closed if the shared projector later gains another consequence.
    if set(type(outcome).model_fields) != {
        "handle_id",
        "ended_reason",
        "deaths",
        "residual_hp",
        "residual_temp_hp",
        "xp_awarded",
        "expended_resources",
        "loot_drops",
    }:
        raise EvaluationInvariantError("unrepresented combat outcome field")
    if outcome.loot_drops:
        return _refuse("unsupported", "closure.loot", "loot requires an inventory delta")
    if outcome.ended_reason != reason:
        raise EvaluationInvariantError("closure projector changed its derived reason")
    closure = CombatClose(
        kind="combat.close",
        combat_id=state.combat_id,
        expected_ended=False,
        ended=True,
        reason=reason,
        xp_increments=outcome.xp_awarded,
        historical=HistoricalCombatOutcome(
            deaths=tuple(ClosureDeath.model_validate(r.model_dump()) for r in outcome.deaths),
            residual_hp=outcome.residual_hp,
            residual_temp_hp=outcome.residual_temp_hp,
            expended_resources=outcome.expended_resources,
            loot_drops=(),
        ),
    )
    return dict(
        status="accepted",
        state_delta=StateDelta(
            operations=(closure,),
            expected_world_version=snapshot.world_version,
        ),
        proposed_events=(CombatEnded(reason=reason),),
        rng_transition=RNGTransition(
            stream_id=request.rng_context.stream_id,
            input_version=request.rng_context.version,
            input_state=request.rng_context.state,
            next_state=request.rng_context.state,
            state_changed=False,
        ),
        choice=None,
        error=None,
    )
