"""Executable local stateless API. State Machine alone commits the returned proposal."""

import copy
from typing import TypedDict

from dnd5e_srd_data import BundledAssetLoader
from dnd5e_srd_data.loader import AssetLoader

from dnd5e_engine.evaluation_attack import execute_attack
from dnd5e_engine.evaluation_checks import evaluate_check
from dnd5e_engine.evaluation_closure import evaluate_closure
from dnd5e_engine.evaluation_contracts import (
    CombatIntentPayload,
    EvaluationVersion,
    ReadVersion,
    RuleError,
    RuleEvaluationRequest,
    RuleEvaluationResult,
)
from dnd5e_engine.evaluation_items import evaluate_item
from dnd5e_engine.evaluation_movement import evaluate_movement
from dnd5e_engine.evaluation_preflight import prepare_attack
from dnd5e_engine.evaluation_projection import EvaluationInvariantError, attack_delta
from dnd5e_engine.evaluation_rng import RNGTransition
from dnd5e_engine.evaluation_ruleset import RulesetBinding, verify_ruleset
from dnd5e_engine.evaluation_state import CombatSnapshot
from dnd5e_engine.evaluation_turn import evaluate_turn
from dnd5e_engine.lib_loader import scoped_lib_loader


class _ResultIdentity(TypedDict):
    schema_version: EvaluationVersion
    session_id: str
    command_id: str
    input_world_version: int
    input_ruleset_binding: RulesetBinding
    read_set: tuple[ReadVersion, ...]


async def evaluate(
    request: RuleEvaluationRequest, *, loader: AssetLoader | None = None
) -> RuleEvaluationResult:
    """Evaluate independently: no registry, persistent handle, receipt or publication.

    Schema/binding/internal exceptions propagate independently of rule status.
    Pass an immutable explicit loader for overlays; omitted loader is bundled data.
    """
    request = RuleEvaluationRequest.model_validate(copy.deepcopy(request))
    assets = loader if loader is not None else BundledAssetLoader()
    verify_ruleset(request.ruleset_binding, assets)
    common: _ResultIdentity = dict(
        schema_version=request.schema_version,
        session_id=request.session_id,
        command_id=request.command_id,
        input_world_version=request.state_snapshot.world_version,
        input_ruleset_binding=request.ruleset_binding,
        # A conservative whole-session OCC read fence, not invented per-entity versions.
        read_set=(
            ReadVersion(
                kind="world", ref=request.session_id, version=request.state_snapshot.world_version
            ),
        ),
    )
    if request.operation_kind == "combat.item":
        result = RuleEvaluationResult(**common, **await evaluate_item(request, assets))
        result.verify_request(request)
        return result
    if request.operation_kind == "rules.check":
        result = RuleEvaluationResult(**common, **evaluate_check(request, assets))
        result.verify_request(request)
        return result
    if request.operation_kind == "combat.close":
        closure = evaluate_closure(request, assets)
        result = RuleEvaluationResult(**common, **closure)
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
            rng_transition=None,
            choice=None,
            error=RuleError(
                code="operation.capability", reason="operation execution is not migrated"
            ),
        )
    snapshot = request.state_snapshot
    if request.payload.intent_type == "pass":
        result = RuleEvaluationResult(**common, **evaluate_turn(request, assets))
        result.verify_request(request)
        return result
    if request.payload.intent_type in {"move", "dash", "disengage"}:
        result = RuleEvaluationResult(**common, **evaluate_movement(request, assets))
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
                rng_transition=None,
                choice=admission.choice,
                error=admission.error,
            )
        if admission.plan is None:
            raise EvaluationInvariantError("accepted preflight lacks an attack plan")
        rng = request.rng_context.state.restore()
        after = execute_attack(snapshot, request.payload, admission.plan, rng)
        result = RuleEvaluationResult(
            **common,
            status="accepted",
            state_delta=attack_delta(snapshot, after),
            proposed_events=after.combat_state.event_log[len(snapshot.combat_state.event_log) :],
            rng_transition=RNGTransition.between(request.rng_context, rng),
            choice=None,
            error=None,
        )
        result.verify_request(request)
        return result
