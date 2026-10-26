"""Explicit effect snapshot DTOs; Legacy authoring defaults stay internal."""

from typing import Any, Literal, cast

from dnd5e_srd_data.schema.action_policy import ActionPolicy, RestrictedActionGrant
from dnd5e_srd_data.schema.lifecycle import (
    EffectEndFollowUp,
    EffectExpiryBoundary,
    EffectLifecycleSpec,
    OneUseModifier,
    RepeatSaveSpec,
)
from pydantic import (
    Field,
    GetJsonSchemaHandler,
    SerializerFunctionWrapHandler,
    field_serializer,
    model_serializer,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

from dnd5e_engine.effect_lifecycle import EffectLifecycleApplication, OngoingEffectLifecycle
from dnd5e_engine.evaluation_base import EvaluationModel
from dnd5e_engine.types.effects import ActiveEffect, ActiveEffectChange, ChangeMode


def _model_field_schema(schema: CoreSchema) -> CoreSchema:
    """Build the schema for an explicitly represented model field."""
    value = {key: item for key, item in schema.items() if key != "serialization"}
    if value.get("type") in {"function-after", "function-before", "function-wrap"}:
        value["schema"] = _model_field_schema(value["schema"])
    return cast(CoreSchema, value)


class EffectDurationState(EvaluationModel):
    """Represent an effect's explicit duration in the evaluation contract."""

    rounds: int | None
    turns: int | None
    seconds: int | None
    start_round: int | None
    start_turn: int | None


class EffectChangeState(ActiveEffectChange):
    """Represent a typed active effect change within fixed evaluation inputs."""

    model_config = EvaluationModel.model_config
    key: str = Field(...)
    mode: ChangeMode = Field(...)
    value: bool | int | str = Field(...)
    priority: int = Field(...)


class RestrictedActionGrantState(RestrictedActionGrant):
    """Represent an explicit restricted action grant in fixed effect inputs."""

    model_config = EvaluationModel.model_config
    attacks: Literal[1] = Field(...)


class ActionPolicyState(ActionPolicy):
    """Represent effect-derived action policy in the evaluation contract."""

    model_config = EvaluationModel.model_config
    deny_actions: bool = Field(...)
    deny_bonus_actions: bool = Field(...)
    deny_reactions: bool = Field(...)
    action_or_bonus: bool = Field(...)
    attack_count_cap: Literal[1] | None = Field(...)
    somatic_failure_percent: int = Field(..., ge=0, le=100)
    extra_action: RestrictedActionGrantState | None = Field(...)


class RepeatSaveState(RepeatSaveSpec):
    """Represent captured repeat-save requirements with explicit wire fields."""

    model_config = EvaluationModel.model_config
    phase: Literal["target_turn_end"] = Field(...)
    save_source: Literal["triggering_activity"] = Field(...)
    on_success: Literal["expire_effect"] = Field(...)


class EffectEndFollowUpState(EffectEndFollowUp):
    """Represent the configured consequence of an effect ending."""

    model_config = EvaluationModel.model_config


class EffectLifecycleSpecState(EffectLifecycleSpec):
    """Represent all lifecycle rules needed to reconstruct a captured effect clock."""

    model_config = EvaluationModel.model_config
    repeat_save: RepeatSaveState | None = Field(...)
    maximum_rounds: int | None = Field(..., gt=0)
    expiry_boundary: EffectExpiryBoundary | None = Field(...)
    expire_on_positive_damage: bool = Field(...)
    one_use_modifiers: tuple[OneUseModifier, ...] = Field(...)
    stacking: Literal["latest_only", "latest_applies"] | None = Field(...)
    stacking_group: str | None = Field(...)
    next_attack_scope: Literal["other_creature"] | None = Field(...)
    next_attack_bonus_group: str | None = Field(...)
    on_end: tuple[EffectEndFollowUpState, ...] = Field(...)

    @model_serializer(mode="wrap")
    def _serialize_lifecycle(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # Snapshot nulls/empty values attest state and cannot be omitted.
        """Serialize lifecycle using the explicit nested wire representation."""
        data: dict[str, Any] = handler(self)
        return data

    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        # The wrap serializer preserves exactly these model fields. Its dict
        # return annotation must not erase their serialization schema. Copy the
        # schema metadata only; runtime validation/serialization remain intact.
        """Expose the explicit lifecycle representation in the generated JSON Schema."""
        return handler(_model_field_schema(schema))


class EffectLifecycleApplicationState(EffectLifecycleApplication):
    """Represent the captured application inputs of a lifecycle clock."""

    model_config = EvaluationModel.model_config
    spec: EffectLifecycleSpecState = Field(...)
    save_ability: Literal["str", "dex", "con", "int", "wis", "cha"] | None = Field(...)
    save_dc: int | None = Field(...)
    is_magical: bool = Field(...)


class OngoingEffectLifecycleState(EvaluationModel):
    """Complete wire clock; authoring defaults never become runtime input defaults.

    The original immutable clock remains the shared rule algorithm's value object.
    This DTO supplies both validation and serialization schemas at that boundary.
    """

    identity: tuple[str, str, str]
    application: EffectLifecycleApplicationState
    applied_turn_serial: int
    applied_round: int
    expires_round: int | None
    expiry_actor_id: str | None
    last_repeat_turn_serial: int | None
    remaining_one_use_modifiers: tuple[OneUseModifier, ...]

    def clock(self) -> OngoingEffectLifecycle:
        """Restore the runtime lifecycle clock from its explicit wire representation."""
        return OngoingEffectLifecycle(
            **{name: getattr(self, name) for name in type(self).model_fields}
        )


class EffectState(EvaluationModel):
    """Describe a supplied active effect independently of authoritative host persistence."""

    id: str
    name: str
    origin: str
    target_id: str
    disabled: bool
    transfer: bool
    duration: EffectDurationState
    changes: list[EffectChangeState]
    action_policy: ActionPolicyState | None
    statuses: set[str]
    flags: dict[str, Any]
    lifecycle: EffectLifecycleApplicationState | None
    end_effects: tuple["EffectState", ...]

    @field_serializer("statuses")
    def sorted_statuses(self, statuses: set[str]) -> list[str]:
        """Require deterministic ordering and uniqueness of condition statuses."""
        return sorted(statuses)


def explicit_effect_fields(value: Any) -> Any:
    """Preserve every lifecycle field, including defaults normally omitted by authoring."""
    import copy

    from pydantic import BaseModel

    if isinstance(value, BaseModel):
        return {
            name: explicit_effect_fields(getattr(value, name)) for name in type(value).model_fields
        }
    if isinstance(value, dict):
        return {key: explicit_effect_fields(item) for key, item in value.items()}
    if isinstance(value, list):
        return [explicit_effect_fields(item) for item in value]
    if isinstance(value, tuple):
        return tuple(explicit_effect_fields(item) for item in value)
    return copy.deepcopy(value)


def effect_state(effect: ActiveEffect) -> EffectState:
    """Capture explicit values from an existing Legacy document, never from SM gaps."""
    return EffectState.model_validate(explicit_effect_fields(effect))
