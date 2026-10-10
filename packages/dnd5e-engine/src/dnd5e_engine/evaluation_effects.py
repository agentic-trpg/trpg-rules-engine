"""Explicit effect snapshot DTOs; Legacy authoring defaults stay internal."""

from typing import Any, Literal

from dnd5e_srd_data.schema.action_policy import ActionPolicy, RestrictedActionGrant
from dnd5e_srd_data.schema.lifecycle import (
    EffectEndFollowUp,
    EffectExpiryBoundary,
    EffectLifecycleSpec,
    OneUseModifier,
    RepeatSaveSpec,
)
from pydantic import Field, SerializerFunctionWrapHandler, field_serializer, model_serializer

from dnd5e_engine.effect_lifecycle import EffectLifecycleApplication
from dnd5e_engine.evaluation_base import EvaluationModel
from dnd5e_engine.types.effects import ActiveEffect, ActiveEffectChange, ChangeMode


class EffectDurationState(EvaluationModel):
    rounds: int | None
    turns: int | None
    seconds: int | None
    start_round: int | None
    start_turn: int | None


class EffectChangeState(ActiveEffectChange):
    model_config = EvaluationModel.model_config
    key: str = Field(...)
    mode: ChangeMode = Field(...)
    value: bool | int | str = Field(...)
    priority: int = Field(...)


class RestrictedActionGrantState(RestrictedActionGrant):
    model_config = EvaluationModel.model_config
    attacks: Literal[1] = Field(...)


class ActionPolicyState(ActionPolicy):
    model_config = EvaluationModel.model_config
    deny_actions: bool = Field(...)
    deny_bonus_actions: bool = Field(...)
    deny_reactions: bool = Field(...)
    action_or_bonus: bool = Field(...)
    attack_count_cap: Literal[1] | None = Field(...)
    somatic_failure_percent: int = Field(..., ge=0, le=100)
    extra_action: RestrictedActionGrantState | None = Field(...)


class RepeatSaveState(RepeatSaveSpec):
    model_config = EvaluationModel.model_config
    phase: Literal["target_turn_end"] = Field(...)
    save_source: Literal["triggering_activity"] = Field(...)
    on_success: Literal["expire_effect"] = Field(...)


class EffectEndFollowUpState(EffectEndFollowUp):
    model_config = EvaluationModel.model_config


class EffectLifecycleSpecState(EffectLifecycleSpec):
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
        data: dict[str, Any] = handler(self)
        return data


class EffectLifecycleApplicationState(EffectLifecycleApplication):
    model_config = EvaluationModel.model_config
    spec: EffectLifecycleSpecState = Field(...)
    save_ability: Literal["str", "dex", "con", "int", "wis", "cha"] | None = Field(...)
    save_dc: int | None = Field(...)
    is_magical: bool = Field(...)


class EffectState(EvaluationModel):
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
