"""Executable local stateless API. State Machine alone commits the returned proposal."""

import copy
import random
from typing import TypedDict

from dnd5e_srd_data import BundledAssetLoader
from dnd5e_srd_data.loader import AssetLoader

from dnd5e_engine import action_policy
from dnd5e_engine import orchestrator as orch
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
from dnd5e_engine.evaluation_preflight import prepare_attack
from dnd5e_engine.evaluation_projection import EvaluationInvariantError, attack_delta
from dnd5e_engine.evaluation_rng import RNGState, RNGTransition
from dnd5e_engine.evaluation_ruleset import RulesetBinding, verify_ruleset
from dnd5e_engine.evaluation_snapshot import capture_combat_snapshot
from dnd5e_engine.evaluation_state import CombatSnapshot
from dnd5e_engine.lib_loader import scoped_lib_loader
from dnd5e_engine.specs import GridScene


class _ResultIdentity(TypedDict):
    schema_version: EvaluationVersion
    session_id: str
    command_id: str
    input_world_version: int
    input_ruleset_binding: RulesetBinding
    read_set: tuple[ReadVersion, ...]


def _capture(live: orch._LiveCombat, snapshot: CombatSnapshot, grid: GridScene) -> CombatSnapshot:
    captured = capture_combat_snapshot(
        live,
        grid=grid,
        world_version=snapshot.world_version,
        combat_id=snapshot.combat_state.combat_id,
        snapshot_schema_version=snapshot.snapshot_schema_version,
    )
    by_id = {actor.entity_id: actor for actor in captured.character_states}
    return captured.model_copy(
        update={
            "character_states": tuple(by_id[actor.entity_id] for actor in snapshot.character_states)
        }
    )


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
    if request.operation_kind == "rules.check" and request.schema_version == "engine-evaluation/4":
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
    with scoped_lib_loader(assets):
        admission = prepare_attack(
            snapshot,
            request.actor_id,
            request.payload,
            assets,
            common_weapons=request.schema_version in ("engine-evaluation/3", "engine-evaluation/4")
            and snapshot.snapshot_schema_version == "engine-snapshot/2",
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
        live = admission.context
        if live is None:
            raise EvaluationInvariantError("accepted preflight lacks execution context")
        grid = GridScene.model_validate(snapshot.scene_state.grid.model_dump())
        captured = _capture(live, snapshot, grid)
        if captured != snapshot or RNGState.capture(live.rng) != RNGState.capture(random.Random(0)):
            raise EvaluationInvariantError("preflight changed mechanical state or RNG")
        live.rng = request.rng_context.state.restore()
        before_actor = orch._find_combatant(live, request.actor_id)
        await orch._submit_live_intent(
            live, orch.CombatHandle(live.handle_id), request.actor_id, request.payload
        )
        action_policy.record_budget_changes(live, before_actor)
        after = _capture(live, snapshot, grid)
        result = RuleEvaluationResult(
            **common,
            status="accepted",
            state_delta=attack_delta(snapshot, after),
            proposed_events=after.combat_state.event_log[len(snapshot.combat_state.event_log) :],
            rng_transition=RNGTransition.between(request.rng_context, live.rng),
            choice=None,
            error=None,
        )
        result.verify_request(request)
        return result
