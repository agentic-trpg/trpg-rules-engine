"""Executable local stateless API. State Machine alone commits the returned proposal."""

import copy
from collections.abc import Callable
from typing import TypedDict

from dnd5e_srd_data.loader import AssetLoader

from dnd5e_engine.activities.context import AttackHitContext
from dnd5e_engine.evaluation_attack import HasteAttackResult, execute_attack
from dnd5e_engine.evaluation_checks import evaluate_check
from dnd5e_engine.evaluation_closure import evaluate_closure
from dnd5e_engine.evaluation_contracts import (
    CombatIntentPayload,
    EvaluationVersion,
    ExplorationEnvironmentalSpellCastPayload,
    ReadVersion,
    RuleError,
    RuleEvaluationRequest,
    RuleEvaluationResult,
)
from dnd5e_engine.evaluation_equipment_dependencies import snapshot_equipment_kind_failure
from dnd5e_engine.evaluation_items import evaluate_exploration_item, evaluate_item
from dnd5e_engine.evaluation_movement import evaluate_movement
from dnd5e_engine.evaluation_preflight import prepare_attack
from dnd5e_engine.evaluation_projection import EvaluationInvariantError, attack_delta
from dnd5e_engine.evaluation_rest import evaluate_rest
from dnd5e_engine.evaluation_rng import RNGTransition
from dnd5e_engine.evaluation_ruleset import RulesetBinding, get_default_ruleset, verify_ruleset
from dnd5e_engine.evaluation_saves import _held_inspiration, evaluate_save
from dnd5e_engine.evaluation_spell_termination import (
    SPELL_TERMINATION_SCHEMA_VERSION,
    WorldSpellEnd,
    spell_cast_ends,
    verify_cast_applications,
)
from dnd5e_engine.evaluation_spells import evaluate_invocation
from dnd5e_engine.evaluation_stages import EventStageFacts
from dnd5e_engine.evaluation_state import CombatSnapshot
from dnd5e_engine.evaluation_turn import TurnEvaluation, _refuse, evaluate_turn
from dnd5e_engine.lib_loader import scoped_lib_loader


class _ResultIdentity(TypedDict):
    """Describe request-associated identity fields copied into an evaluation result."""

    schema_version: EvaluationVersion
    session_id: str
    command_id: str
    input_world_version: int
    input_ruleset_binding: RulesetBinding
    read_set: tuple[ReadVersion, ...]


def _turn_result(common: _ResultIdentity, outcome: TurnEvaluation) -> RuleEvaluationResult:
    """One computation supplies both mechanical results and independent coverage."""
    return RuleEvaluationResult(
        **common,
        status=outcome["status"],
        state_delta=outcome["state_delta"],
        proposed_events=outcome["proposed_events"],
        stage_facts=_stage_coverage(outcome["status"], outcome.get("stage_facts")),
        rng_transition=outcome["rng_transition"],
        choice=outcome["choice"],
        error=outcome["error"],
    )


def _result_identity(request: RuleEvaluationRequest) -> _ResultIdentity:
    """Build the shared identity and world-version read fence for one result."""
    return dict(
        schema_version=request.schema_version,
        session_id=request.session_id,
        command_id=request.command_id,
        input_world_version=request.state_snapshot.world_version,
        input_ruleset_binding=request.ruleset_binding,
        read_set=(
            ReadVersion(
                kind="world", ref=request.session_id, version=request.state_snapshot.world_version
            ),
        ),
    )


def _equipment_rejection(
    request: RuleEvaluationRequest, loader: AssetLoader, common: _ResultIdentity
) -> RuleEvaluationResult | None:
    """Return the existing equipment capability refusal, if the snapshot needs one."""
    failure = snapshot_equipment_kind_failure(request.state_snapshot, loader)
    if failure is None:
        return None
    return RuleEvaluationResult(
        **common,
        status="unsupported",
        state_delta=None,
        proposed_events=(),
        stage_facts=None,
        rng_transition=None,
        choice=None,
        error=RuleError(code="snapshot.capability", reason=failure),
    )


def _stage_coverage(status: str, facts: EventStageFacts | None = None) -> EventStageFacts | None:
    """Select complete or explicitly uncovered event-stage metadata for this result."""
    if status != "accepted":
        return None
    return (
        facts if facts is not None else EventStageFacts.uncovered("operation_not_in_stage_profile")
    )


def evaluate_bound_save(
    request: RuleEvaluationRequest, *, loader: AssetLoader
) -> RuleEvaluationResult:
    """Evaluate a bound save synchronously inside one already admitted world stage.

    This pure local entry retains the ordinary save's binding, equipment, typed
    result and RNG checks. Nested world actions have no player choice window.
    """
    request = RuleEvaluationRequest.model_validate(copy.deepcopy(request))
    if request.operation_kind != "rules.save":
        raise ValueError("Expected a typed saving throw request")
    verify_ruleset(request.ruleset_binding, loader)
    common = _result_identity(request)
    if equipment_rejection := _equipment_rejection(request, loader, common):
        result = equipment_rejection
    elif any(
        effect.target_id == request.actor_id
        and not effect.disabled
        and _held_inspiration(effect, loader)
        for effect in request.state_snapshot.effect_states
    ):
        result = _turn_result(
            common,
            _refuse("unsupported", "save.choice", "Nested saves cannot open an Inspiration choice"),
        )
    else:
        result = _turn_result(common, evaluate_save(request, loader))
    result.verify_request(request, loader=loader)
    return result


async def evaluate(
    request: RuleEvaluationRequest, *, loader: AssetLoader | None = None
) -> RuleEvaluationResult:
    """Evaluate independently: no registry, persistent handle, receipt or publication.

    Schema/binding/internal exceptions propagate independently of rule status.
    Pass freeze_ruleset(source) for overlays; omitted loader shares fixed bundled
    data. Raw mutable loaders retain full binding verification on every request
    and must remain stable during each call.
    """
    return await _evaluate(request, loader=loader)


async def _evaluate(
    request: RuleEvaluationRequest,
    *,
    loader: AssetLoader | None = None,
    before_hit: Callable[[AttackHitContext], None] | None = None,
) -> RuleEvaluationResult:
    """Execute the bound evaluation, optionally stopping at a private attack boundary."""
    request = RuleEvaluationRequest.model_validate(copy.deepcopy(request))
    assets = loader if loader is not None else get_default_ruleset()
    verify_ruleset(request.ruleset_binding, assets)
    if request.schema_version == SPELL_TERMINATION_SCHEMA_VERSION:
        base = request.without_spell_ends()
        facts = request.spell_end_facts
        assert facts is not None  # Validated by without_spell_ends().
        verify_cast_applications(request.actor_id, facts, assets)
        result = await _evaluate(base, loader=assets)
        delta = result.state_delta
        if delta is not None:
            ends = spell_cast_ends(request.actor_id, result.proposed_events, facts.applications)
            dispelled = {
                op.expected.identity
                for op in delta.operations
                if isinstance(op, WorldSpellEnd) and op.reason == "dispelled"
            }
            ends = tuple(end for end in ends if end.expected.identity not in dispelled)
            delta = delta.model_copy(update={"operations": (*delta.operations, *ends)})
        result = result.model_copy(
            update={"schema_version": request.schema_version, "state_delta": delta}
        )
        result.verify_request(request, loader=assets)
        return result
    common = _result_identity(request)
    if equipment_rejection := _equipment_rejection(request, assets, common):
        return equipment_rejection
    if request.operation_kind in {"combat.item", "world.use_item"}:
        item = (
            await evaluate_item(request, assets)
            if request.operation_kind == "combat.item"
            else evaluate_exploration_item(request, assets)
        )
        result = RuleEvaluationResult(**common, **item, stage_facts=_stage_coverage(item["status"]))
        result.verify_request(request)
        return result
    if request.operation_kind == "world.cast_activity":
        from dnd5e_engine.evaluation_cast_activity import evaluate_cast_activity

        result = _turn_result(common, evaluate_cast_activity(request, assets))
        result.verify_request(request)
        return result
    if request.operation_kind == "world.cast_spell_many":
        from dnd5e_engine.evaluation_multi_spells import evaluate_multi_spell

        result = _turn_result(common, evaluate_multi_spell(request, assets))
        result.verify_request(request)
        return result
    if request.operation_kind == "world.cast_spell":
        if isinstance(request.payload, ExplorationEnvironmentalSpellCastPayload):
            from dnd5e_engine.evaluation_environmental_spells import evaluate_environmental_spell

            outcome = evaluate_environmental_spell(request, assets)
        else:
            from dnd5e_engine.evaluation_timed_spells import evaluate_exploration_spell

            outcome = evaluate_exploration_spell(request, assets)
        result = _turn_result(common, outcome)
        result.verify_request(request, loader=assets)
        return result
    if request.operation_kind == "rules.rest":
        result = _turn_result(common, evaluate_rest(request, assets))
        result.verify_request(request)
        return result
    if request.operation_kind == "rules.save":
        result = _turn_result(common, evaluate_save(request, assets))
        result.verify_request(request)
        return result
    if request.operation_kind == "rules.check":
        check = evaluate_check(request, assets)
        result = RuleEvaluationResult(
            **common, **check, stage_facts=_stage_coverage(check["status"])
        )
        result.verify_request(request)
        return result
    if request.operation_kind == "combat.close":
        closure = evaluate_closure(request, assets)
        result = RuleEvaluationResult(
            **common, **closure, stage_facts=_stage_coverage(closure["status"])
        )
        result.verify_request(request)
        return result
    if (
        request.operation_kind != "combat.intent"
        or not isinstance(request.state_snapshot, CombatSnapshot)
        or not isinstance(request.payload, CombatIntentPayload)
    ):
        return RuleEvaluationResult(
            **common,
            status="unsupported",
            state_delta=None,
            proposed_events=(),
            stage_facts=None,
            rng_transition=None,
            choice=None,
            error=RuleError(
                code="operation.capability", reason="operation execution is not migrated"
            ),
        )
    if request.payload.intent_type in {"cast_spell", "use_feature"}:
        result = _turn_result(common, evaluate_invocation(request, assets))
        result.verify_request(request)
        return result
    if request.payload.intent_type == "drop_concentration":
        from dnd5e_engine.evaluation_effect_release import evaluate_effect_release

        result = _turn_result(common, evaluate_effect_release(request, assets))
        result.verify_request(request)
        return result
    snapshot = request.state_snapshot
    if request.payload.intent_type == "pass":
        result = _turn_result(common, evaluate_turn(request, assets))
        result.verify_request(request)
        return result
    if request.payload.intent_type in {"move", "dash", "disengage"}:
        result = _turn_result(common, evaluate_movement(request, assets))
        result.verify_request(request)
        return result
    with scoped_lib_loader(assets):
        admission = prepare_attack(
            snapshot,
            request.actor_id,
            request.payload,
            assets,
            common_weapons=True,
        )
        if admission.status != "accepted":
            return RuleEvaluationResult(
                **common,
                status=admission.status,
                state_delta=None,
                proposed_events=(),
                stage_facts=None,
                rng_transition=None,
                choice=admission.choice,
                error=admission.error,
            )
        if admission.plan is None:
            raise EvaluationInvariantError("accepted preflight lacks an attack plan")
        profile = admission.plan.effect_profile
        if profile == "haste" and request.schema_version != "engine-evaluation/30":
            return _turn_result(
                common,
                _refuse("unsupported", "attack.haste", "Haste Attack requires evaluation/30"),
            )
        rng = request.rng_context.state.restore()
        computed = execute_attack(
            snapshot, request.payload, admission.plan, rng, loader=assets, before_hit=before_hit
        )
        after = computed.candidate_state
        if profile in {"held_target", "haste"} and any(
            actor.entity_id == admission.plan.target.entity_id and actor.hp_current == 0
            for actor in after.character_states
        ):
            # These effect profiles require living actors for later turn handling.
            return _turn_result(
                common,
                _refuse(
                    "unsupported",
                    "attack.effect_consequence",
                    "zero HP on a held target exceeds the supported Hold Person lifecycle"
                    if profile == "held_target"
                    else "zero HP exceeds the admitted Haste attack lifecycle",
                ),
            )
        from dnd5e_engine.evaluation_concentration import concentration_delta
        from dnd5e_engine.evaluation_haste import haste_delta
        from dnd5e_engine.evaluation_shield import shield_delta

        result = RuleEvaluationResult(
            **common,
            status="accepted",
            state_delta=haste_delta(snapshot, after, computed.follow_ups)
            if isinstance(computed, HasteAttackResult)
            else (
                shield_delta
                if profile == "shield"
                else concentration_delta
                if profile in {"concentration", "held_target"}
                else attack_delta
            )(snapshot, after),
            proposed_events=computed.proposed_events,
            stage_facts=computed.stage_facts,
            rng_transition=RNGTransition.between(request.rng_context, rng),
            choice=None,
            error=None,
        )
        result.verify_request(request)
        return result
