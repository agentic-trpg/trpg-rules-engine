"""Read-only, versioned availability over the exact executable attack preflight."""

from __future__ import annotations

import copy
from collections.abc import Callable
from fractions import Fraction
from typing import Annotated, Any, Literal, Self

from dnd5e_srd_data.loader import AssetLoader
from pydantic import Field, model_serializer, model_validator

from dnd5e_engine.evaluation_attack import PreviewDiceDrawError, PreviewReason, preview_attack_hit
from dnd5e_engine.evaluation_base import EvaluationModel
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, RuleError
from dnd5e_engine.evaluation_equipment_dependencies import snapshot_equipment_kind_failure
from dnd5e_engine.evaluation_opportunity import ordinary_attack_opportunity_failure
from dnd5e_engine.evaluation_preflight import prepare_attack, refused
from dnd5e_engine.evaluation_projection import EvaluationInvariantError
from dnd5e_engine.evaluation_ruleset import RulesetBinding, get_default_ruleset, verify_ruleset
from dnd5e_engine.evaluation_state import CombatSnapshot
from dnd5e_engine.lib_loader import scoped_lib_loader

AvailabilityVersion = Literal[
    "engine-availability/13",
    "engine-availability/14",
    "engine-availability/15",
    "engine-availability/16",
    "engine-availability/17",
]


class ActionAvailabilityRequest(EvaluationModel):
    """Bind a read-only availability query to explicit snapshot and rules identity."""

    schema_version: AvailabilityVersion
    session_id: Annotated[str, Field(min_length=1)]
    operation_kind: Literal["combat.intent"]
    actor_id: Annotated[str, Field(min_length=1)]
    payload: CombatIntentPayload
    state_snapshot: CombatSnapshot
    ruleset_binding: RulesetBinding
    include_hit_preview: bool = False

    @model_serializer(mode="wrap")
    def serialize_version(
        self, handler: Callable[[ActionAvailabilityRequest], dict[str, Any]]
    ) -> dict[str, Any]:
        """Omit the /17 preview opt-in from legacy request serialization."""
        value = handler(self)
        if self.schema_version != "engine-availability/17":
            value.pop("include_hit_preview", None)
        return value

    @model_validator(mode="after")
    def identities(self) -> Self:
        """Validate unique identities and referenced ownership in the supplied component."""
        if self.session_id != self.state_snapshot.session_id or (
            self.payload.source_id is not None and self.payload.source_id != self.actor_id
        ):
            raise ValueError("query actor/session identity mismatch")
        version_pairs = {
            "engine-availability/13": "engine-snapshot/17",
            "engine-availability/14": "engine-snapshot/18",
            "engine-availability/15": "engine-snapshot/19",
            "engine-availability/16": "engine-snapshot/20",
            "engine-availability/17": "engine-snapshot/20",
        }
        if self.state_snapshot.snapshot_schema_version != version_pairs[self.schema_version]:
            raise ValueError("availability and snapshot versions differ")
        return self


class ActionAvailabilityResult(EvaluationModel):
    """Return supported action options and refusal/choice information without payment."""

    schema_version: AvailabilityVersion
    session_id: str
    actor_id: str
    operation_kind: Literal["combat.intent"]
    snapshot_world_version: Annotated[int, Field(ge=0)]
    ruleset_binding: RulesetBinding
    status: Literal["available", "unavailable", "unknown"]
    reason: RuleError
    hit_preview: AttackHitPreview | None = None

    @model_serializer(mode="wrap")
    def serialize_version(
        self, handler: Callable[[ActionAvailabilityResult], dict[str, Any]]
    ) -> dict[str, Any]:
        """Keep the /13–/16 wire shape unchanged when serializing results."""
        value = handler(self)
        if self.schema_version != "engine-availability/17":
            value.pop("hit_preview", None)
        return value


class AttackHitPreview(EvaluationModel):
    """A read-only total hit chance, including critical hits, on this snapshot."""

    status: Literal["known", "unknown"]
    probability_numerator: Annotated[int, Field(ge=0)] | None = None
    probability_denominator: Annotated[int, Field(gt=0)] | None = None
    mode: Literal["normal", "advantage", "disadvantage"] | None = None
    advantage_reasons: tuple[PreviewReason, ...] = ()
    disadvantage_reasons: tuple[PreviewReason, ...] = ()

    @model_validator(mode="after")
    def complete_known(self) -> Self:
        """Keep incomplete or unsupported preview inputs out of numeric results."""
        if self.status == "known" and (
            self.probability_numerator is None
            or self.probability_denominator is None
            or self.mode is None
            or self.probability_numerator > self.probability_denominator
        ):
            raise ValueError("known preview requires a bounded fraction and mode")
        if self.status == "unknown" and (
            self.probability_numerator is not None
            or self.probability_denominator is not None
            or self.mode is not None
            or self.advantage_reasons
            or self.disadvantage_reasons
        ):
            raise ValueError("unknown preview must not disclose mechanics")
        return self


def query_action_availability(
    request: ActionAvailabilityRequest, *, loader: AssetLoader | None = None
) -> ActionAvailabilityResult:
    """No command ID, request RNG, receipt, delta, events or authoritative mutation.

    Availability is conditional on this snapshot and immutable rules pin. SM
    independently authorizes actors and rechecks versions before real execution.
    """
    request = ActionAvailabilityRequest.model_validate(copy.deepcopy(request))
    assets = loader if loader is not None else get_default_ruleset()
    verify_ruleset(request.ruleset_binding, assets)
    if failure := snapshot_equipment_kind_failure(request.state_snapshot, assets):
        return ActionAvailabilityResult(
            schema_version=request.schema_version,
            session_id=request.session_id,
            actor_id=request.actor_id,
            operation_kind=request.operation_kind,
            snapshot_world_version=request.state_snapshot.world_version,
            ruleset_binding=request.ruleset_binding,
            status="unknown",
            reason=RuleError(code="snapshot.capability", reason=failure),
            hit_preview=AttackHitPreview(status="unknown")
            if request.schema_version == "engine-availability/17"
            else None,
        )
    with scoped_lib_loader(assets):
        failure = ordinary_attack_opportunity_failure(request.state_snapshot, assets)
        admission = (
            refused("unsupported", "attack.reaction", failure)
            if failure
            else prepare_attack(
                request.state_snapshot,
                request.actor_id,
                request.payload,
                assets,
                common_weapons=True,
            )
        )
        status: Literal["available", "unavailable", "unknown"]
        if admission.status == "accepted":
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
    preview = None
    if request.schema_version == "engine-availability/17":
        if request.include_hit_preview and status == "available" and admission.plan is not None:
            try:
                with scoped_lib_loader(assets):
                    computed = preview_attack_hit(
                        request.state_snapshot, request.payload, admission.plan
                    )
            except PreviewDiceDrawError:
                computed = None
            if computed is not None:
                chance, mode, advantage_reasons, disadvantage_reasons = computed
                fraction = Fraction(chance, 400 if mode != "normal" else 20)
                preview = AttackHitPreview(
                    status="known",
                    probability_numerator=fraction.numerator,
                    probability_denominator=fraction.denominator,
                    mode=mode,
                    advantage_reasons=advantage_reasons,
                    disadvantage_reasons=disadvantage_reasons,
                )
        if preview is None:
            preview = AttackHitPreview(status="unknown")
    return ActionAvailabilityResult(
        schema_version=request.schema_version,
        session_id=request.session_id,
        actor_id=request.actor_id,
        operation_kind=request.operation_kind,
        snapshot_world_version=request.state_snapshot.world_version,
        ruleset_binding=request.ruleset_binding,
        status=status,
        reason=reason,
        hit_preview=preview,
    )
