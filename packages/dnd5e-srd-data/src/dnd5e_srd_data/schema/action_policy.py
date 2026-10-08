"""Closed effect-owned action permissions; no runtime prose interpretation."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ActionType = Literal["attack", "dash", "disengage", "hide", "utilize", "magic", "other"]


class RestrictedActionGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    actions: tuple[Literal["attack", "dash", "disengage", "hide", "utilize"], ...] = Field(
        min_length=1
    )
    attacks: Literal[1] = 1

    @model_validator(mode="after")
    def unique_actions(self) -> "RestrictedActionGrant":
        if len(set(self.actions)) != len(self.actions):
            raise ValueError("action grant candidates must be unique")
        return self


class ActionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    deny_actions: bool = False
    deny_bonus_actions: bool = False
    deny_reactions: bool = False
    action_or_bonus: bool = False
    attack_count_cap: Literal[1] | None = None
    somatic_failure_percent: int = Field(default=0, ge=0, le=100)
    extra_action: RestrictedActionGrant | None = None
