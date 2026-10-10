"""Read-only, versioned availability over the exact executable attack preflight."""

import copy
import random
from typing import Annotated, Literal, Self

from dnd5e_srd_data import BundledAssetLoader
from dnd5e_srd_data.loader import AssetLoader
from pydantic import Field, model_validator

from dnd5e_engine.evaluation import _capture
from dnd5e_engine.evaluation_base import EvaluationModel
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, RuleError
from dnd5e_engine.evaluation_preflight import prepare_attack
from dnd5e_engine.evaluation_projection import EvaluationInvariantError
from dnd5e_engine.evaluation_ruleset import RulesetBinding, verify_ruleset
from dnd5e_engine.evaluation_state import CombatSnapshot
from dnd5e_engine.lib_loader import scoped_lib_loader
from dnd5e_engine.specs import GridScene


class ActionAvailabilityRequest(EvaluationModel):
    schema_version: Literal["engine-availability/1", "engine-availability/2"]
    session_id: Annotated[str, Field(min_length=1)]
    operation_kind: Literal["combat.intent"]
    actor_id: Annotated[str, Field(min_length=1)]
    payload: CombatIntentPayload
    state_snapshot: CombatSnapshot
    ruleset_binding: RulesetBinding

    @model_validator(mode="after")
    def identities(self) -> Self:
        if (
            self.state_snapshot.snapshot_schema_version == "engine-snapshot/2"
            and self.schema_version != "engine-availability/2"
        ):
            raise ValueError("snapshot /2 requires engine-availability/2")
        if self.session_id != self.state_snapshot.session_id or (
            self.payload.source_id is not None and self.payload.source_id != self.actor_id
        ):
            raise ValueError("query actor/session identity mismatch")
        return self


class ActionAvailabilityResult(EvaluationModel):
    schema_version: Literal["engine-availability/1", "engine-availability/2"]
    session_id: str
    actor_id: str
    operation_kind: Literal["combat.intent"]
    snapshot_world_version: Annotated[int, Field(ge=0)]
    ruleset_binding: RulesetBinding
    status: Literal["available", "unavailable", "unknown"]
    reason: RuleError


def query_action_availability(
    request: ActionAvailabilityRequest, *, loader: AssetLoader | None = None
) -> ActionAvailabilityResult:
    """No command ID, request RNG, receipt, delta, events or authoritative mutation.

    Availability is conditional on this snapshot and immutable rules pin. SM
    independently authorizes actors and rechecks versions before real execution.
    """
    request = ActionAvailabilityRequest.model_validate(copy.deepcopy(request))
    assets = loader if loader is not None else BundledAssetLoader()
    verify_ruleset(request.ruleset_binding, assets)
    with scoped_lib_loader(assets):
        admission = prepare_attack(
            request.state_snapshot,
            request.actor_id,
            request.payload,
            assets,
            common_weapons=request.schema_version == "engine-availability/2"
            and request.state_snapshot.snapshot_schema_version == "engine-snapshot/2",
        )
        status: Literal["available", "unavailable", "unknown"]
        if admission.status == "accepted":
            live = admission.context
            if live is None:
                raise EvaluationInvariantError("available preflight lacks context")
            if (
                _capture(
                    live,
                    request.state_snapshot,
                    GridScene.model_validate(request.state_snapshot.scene_state.grid.model_dump()),
                )
                != request.state_snapshot
                or live.rng.getstate() != random.Random(0).getstate()
            ):
                raise EvaluationInvariantError("availability preflight mutated state or RNG")
            status = "available"
            reason = RuleError(
                code="attack.available",
                reason="shared preflight admits this attack on this snapshot",
            )
        elif admission.status == "rejected":
            status = "unavailable"
            if admission.error is None:
                raise EvaluationInvariantError("rejected preflight lacks reason")
            reason = admission.error
        else:
            status = "unknown"
            reason = admission.error or RuleError(
                code="attack.choice_required", reason="an explicit weapon choice is required"
            )
    return ActionAvailabilityResult(
        schema_version=request.schema_version,
        session_id=request.session_id,
        actor_id=request.actor_id,
        operation_kind=request.operation_kind,
        snapshot_world_version=request.state_snapshot.world_version,
        ruleset_binding=request.ruleset_binding,
        status=status,
        reason=reason,
    )
