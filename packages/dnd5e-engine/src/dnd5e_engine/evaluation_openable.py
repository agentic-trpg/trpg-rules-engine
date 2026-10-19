"""Bounded World tool checks, using the shared SRD 5.2.1 check resolver.

The host admits object identities, reach, lock policy and consequences. This API
owns mechanical tool eligibility, proficiency, advantage and check resolution.
It cannot write world facts. Its additive ABI is engine-openable/1; the existing
generic evaluation ABI remains unchanged.
"""

import copy
from typing import Literal, Self

from dnd5e_srd_data.loader import AssetLoader
from pydantic import model_validator

from dnd5e_engine.evaluation import _stage_coverage
from dnd5e_engine.evaluation_base import EvaluationModel
from dnd5e_engine.evaluation_checks import (
    CheckEvaluation,
    _refuse,
    check_support_failure,
    evaluate_check,
)
from dnd5e_engine.evaluation_contracts import (
    CheckPayload,
    ReadVersion,
    RuleEvaluationRequest,
    RuleEvaluationResult,
)
from dnd5e_engine.evaluation_ruleset import get_default_ruleset, verify_ruleset


class OpenableCheckRequest(EvaluationModel):
    schema_version: Literal["engine-openable/1"]
    request: RuleEvaluationRequest
    operation: Literal["pick_lock", "force_open", "clear_jam"]
    tool_instance_id: str | None
    required_tool: Literal["thieves-tools", "smiths-tools", "crowbar"] | None
    leverage_applicable: bool

    @model_validator(mode="after")
    def bounded_check(self) -> Self:
        p = self.request.payload
        if self.request.operation_kind != "rules.check" or not isinstance(p, CheckPayload):
            raise ValueError("openable resolution requires an explicit check")
        if p.dc is None or p.tool is not None or p.target_id is not None:
            raise ValueError("object policy requires a DC and host-owned target")
        if p.advantage or p.disadvantage or p.redeem_granted_die:
            raise ValueError("mechanical sources must derive from explicit facts")
        if self.operation == "pick_lock" and (
            p.ability != "dex"
            or p.skill != "sleight_of_hand"
            or self.required_tool != "thieves-tools"
            or self.leverage_applicable
        ):
            raise ValueError("lock picking uses Dexterity/Sleight of Hand and Thieves' Tools")
        if self.leverage_applicable and (p.ability != "str" or self.required_tool != "crowbar"):
            raise ValueError("crowbar leverage requires a Strength check and a crowbar")
        if (self.tool_instance_id is None) != (self.required_tool is None):
            raise ValueError("explicit tool instance and requirement must agree")
        return self


async def evaluate_openable(
    spec: OpenableCheckRequest, *, loader: AssetLoader | None = None
) -> RuleEvaluationResult:
    """Use the same fixed dataset, binding and result contract as ordinary checks.

    Hosts retain one freeze_ruleset(source) per effective custom corpus; omission
    shares main's bundled dataset. Raw mutable loaders retain main's full binding
    verification compatibility path. Object consequences remain host-owned.
    """
    spec = OpenableCheckRequest.model_validate(copy.deepcopy(spec))
    assets = loader if loader is not None else get_default_ruleset()
    req = spec.request
    verify_ruleset(req.ruleset_binding, assets)
    outcome = openable_support_failure(spec, loader=assets)
    if outcome is None:
        outcome = evaluate_check(
            req,
            assets,
            tool_slug=None if spec.required_tool == "crowbar" else spec.required_tool,
            tool_advantage=spec.leverage_applicable,
        )
    result = RuleEvaluationResult(
        schema_version=req.schema_version,
        session_id=req.session_id,
        command_id=req.command_id,
        input_world_version=req.state_snapshot.world_version,
        input_ruleset_binding=req.ruleset_binding,
        read_set=(
            ReadVersion(kind="world", ref=req.session_id, version=req.state_snapshot.world_version),
        ),
        stage_facts=_stage_coverage(outcome["status"]),
        **outcome,
    )
    result.verify_request(req)
    return result


def openable_support_failure(
    spec: OpenableCheckRequest, *, loader: AssetLoader
) -> CheckEvaluation | None:
    req = spec.request
    outcome = None
    if spec.required_tool is not None:
        entry = next(
            (
                i
                for i in req.state_snapshot.inventory_state.entries
                if i.instance_id == spec.tool_instance_id
            ),
            None,
        )
        if (
            entry is None
            or entry.owner_id != req.actor_id
            or not entry.accessible
            or entry.quantity < 1
            or entry.item_slug != spec.required_tool
        ):
            outcome = _refuse(
                "rejected", "tool_unavailable", "An accessible owned tool is required"
            )
    return outcome or check_support_failure(req, loader)
