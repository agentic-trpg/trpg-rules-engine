"""Versioned local request/result contracts with explicit failure boundaries."""

from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from dnd5e_engine.evaluation_base import EvaluationModel
from dnd5e_engine.evaluation_delta import CombatClose, StateDelta
from dnd5e_engine.evaluation_rng import RNGContext, RNGTransition
from dnd5e_engine.evaluation_ruleset import RulesetBinding
from dnd5e_engine.evaluation_state import CombatSnapshot, StateSnapshot
from dnd5e_engine.events import ALL_COMBAT_EVENT_TYPES, CombatEvent
from dnd5e_engine.orchestrator import PlayerIntent
from dnd5e_engine.types.checks import CheckRequest

SCHEMA_VERSION = "engine-evaluation/1"
EvaluationVersion = Literal["engine-evaluation/1", "engine-evaluation/2", "engine-evaluation/3"]


class CombatClosePayload(EvaluationModel):
    """No caller-supplied end reason, reward or claimed death."""

    kind: Literal["combat.close"]


class CombatIntentPayload(PlayerIntent):
    """Reuse the legacy typed intent vocabulary; admission is evaluator-specific."""

    model_config = ConfigDict(extra="forbid", strict=True, revalidate_instances="always")

    stat_block_action_id: Annotated[str, Field(strict=True, min_length=1)] | None = None


class RuleEvaluationRequest(EvaluationModel):
    schema_version: EvaluationVersion
    session_id: Annotated[str, Field(min_length=1)]
    command_id: Annotated[str, Field(min_length=1)]
    operation_kind: Literal["combat.intent", "rules.check", "combat.close"]
    actor_id: Annotated[str, Field(min_length=1)]
    payload: CombatIntentPayload | CheckRequest | CombatClosePayload
    state_snapshot: StateSnapshot
    ruleset_binding: RulesetBinding
    rng_context: RNGContext

    @model_validator(mode="after")
    def operation_payload(self) -> Self:
        if self.session_id != self.state_snapshot.session_id:
            raise ValueError("request and snapshot session identities differ")
        if (
            self.state_snapshot.snapshot_schema_version == "engine-snapshot/2"
            and self.schema_version != "engine-evaluation/3"
        ):
            raise ValueError("snapshot /2 requires engine-evaluation/3")
        if self.operation_kind == "combat.intent":
            if not isinstance(self.payload, CombatIntentPayload):
                raise ValueError("combat.intent requires a typed combat payload")
            if not isinstance(self.state_snapshot, CombatSnapshot):
                raise ValueError("combat.intent requires a combat snapshot")
            if self.payload.source_id is not None and self.payload.source_id != self.actor_id:
                raise ValueError("payload source_id differs from actor_id")
        elif self.operation_kind == "combat.close":
            if self.schema_version not in ("engine-evaluation/2", "engine-evaluation/3"):
                raise ValueError("combat.close requires engine-evaluation/2")
            if not isinstance(self.payload, CombatClosePayload) or not isinstance(
                self.state_snapshot, CombatSnapshot
            ):
                raise ValueError("combat.close requires a closure payload and combat snapshot")
        elif not isinstance(self.payload, CheckRequest) or self.payload.actor_id != self.actor_id:
            raise ValueError("rules.check requires a matching actor and typed check payload")
        return self


class ReadVersion(EvaluationModel):
    kind: Literal["world", "actor", "combat", "scene", "effects", "history"]
    ref: Annotated[str, Field(min_length=1)]
    version: Annotated[int, Field(ge=0)]


class RuleError(EvaluationModel):
    code: Annotated[str, Field(min_length=1)]
    reason: Annotated[str, Field(min_length=1)]


class PreflightChoice(EvaluationModel):
    kind: Literal["attack.weapon"]
    actor_id: Annotated[str, Field(min_length=1)]
    allowed_weapon_ids: Annotated[tuple[str, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def unique_options(self) -> Self:
        if any(not option for option in self.allowed_weapon_ids) or len(
            set(self.allowed_weapon_ids)
        ) != len(self.allowed_weapon_ids):
            raise ValueError("choice options must be nonempty and unique")
        return self


ProposedEvents = tuple[CombatEvent, ...]


class RuleEvaluationResult(EvaluationModel):
    schema_version: EvaluationVersion
    session_id: Annotated[str, Field(min_length=1)]
    command_id: Annotated[str, Field(min_length=1)]
    input_world_version: Annotated[int, Field(ge=0)]
    input_ruleset_binding: RulesetBinding
    status: Literal["accepted", "rejected", "needs_choice", "unsupported"]
    read_set: tuple[ReadVersion, ...]
    state_delta: StateDelta | None
    proposed_events: ProposedEvents
    rng_transition: RNGTransition | None
    choice: PreflightChoice | None
    error: RuleError | None

    @field_validator("proposed_events", mode="before")
    @classmethod
    def closed_event_fields(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            kinds = {
                model.model_fields["type"].default: set(model.model_fields)
                for model in ALL_COMBAT_EVENT_TYPES
            }
            for event in value:
                if isinstance(event, dict) and set(event) - kinds.get(event.get("type"), set()):
                    raise ValueError("unknown proposed event field or authoritative event identity")
            return tuple(value)
        return value

    @model_validator(mode="after")
    def status_fields(self) -> Self:
        if self.status == "accepted":
            if self.state_delta is None or self.rng_transition is None:
                raise ValueError("accepted requires delta and RNG transition")
            if self.choice is not None or self.error is not None:
                raise ValueError("accepted forbids choice/error")
            if self.state_delta.expected_world_version != self.input_world_version:
                raise ValueError("delta and result world versions differ")
            if self.schema_version == "engine-evaluation/1" and any(
                isinstance(op, CombatClose) for op in self.state_delta.operations
            ):
                raise ValueError("closure delta requires engine-evaluation/2")
        else:
            if (
                self.state_delta is not None
                or self.proposed_events
                or self.rng_transition is not None
            ):
                raise ValueError("non-accepted result cannot carry mechanical results")
            if self.status == "needs_choice":
                if self.choice is None or self.error is not None:
                    raise ValueError("needs_choice requires choice and forbids error")
            elif self.error is None or self.choice is not None:
                raise ValueError("rejected/unsupported require error and forbid choice")
        if len({(read.kind, read.ref) for read in self.read_set}) != len(self.read_set):
            raise ValueError("duplicate read-set entry")
        return self

    def verify_request(self, request: RuleEvaluationRequest) -> None:
        if (
            self.schema_version,
            self.session_id,
            self.command_id,
            self.input_world_version,
            self.input_ruleset_binding,
        ) != (
            request.schema_version,
            request.session_id,
            request.command_id,
            request.state_snapshot.world_version,
            request.ruleset_binding,
        ):
            raise ValueError("result does not match request identity/version/binding")
        if self.rng_transition is not None:
            self.rng_transition.verify_input(request.rng_context)
