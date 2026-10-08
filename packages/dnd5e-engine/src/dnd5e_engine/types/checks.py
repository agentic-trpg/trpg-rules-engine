"""Typed check semantics supplied by rules or by a DM, never inferred from prose."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dnd5e_engine.events import Ability, AdvantageSource, CheckContext, RequiredSense
from dnd5e_engine.rules.skills import Skill
from dnd5e_engine.types.effects import ActiveEffect

GrantedDie = Literal["feature_grant:bardic-inspiration"]


class CheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_id: str
    ability: Ability
    skill: Skill | None = None
    # Canonical tool labels can be reported without implementing tool inventory.
    tool: str | None = None
    dc: int | None = Field(default=None, strict=True, ge=0)
    target_id: str | None = None
    context: CheckContext = "generic"
    required_sense: RequiredSense = "none"
    social_interaction: bool = False
    advantage: tuple[AdvantageSource, ...] = ()
    disadvantage: tuple[AdvantageSource, ...] = ()
    redeem_granted_die: GrantedDie | None = None

    @model_validator(mode="after")
    def one_proficiency(self) -> CheckRequest:
        if self.skill is not None and self.tool is not None:
            raise ValueError("a check uses one skill or tool, not both")
        return self


class HelpCheckSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    beneficiary_id: str
    skill: Skill
    assistance: Literal["physical", "verbal"]
    # SRD leaves 'near enough' and whether assistance is possible to the GM.
    # The engine enforces this declared range against actual grid distance.
    range_ft: int = Field(strict=True, ge=0)
    assistance_possible: bool


@dataclass(frozen=True)
class HelpCheckGrant:
    helper_id: str
    beneficiary_id: str
    skill: Skill


@dataclass(frozen=True)
class CheckActorState:
    """Live, draw-free projection; independent of orchestrator internals."""

    effects: tuple[ActiveEffect, ...] = ()
    fear_source_in_sight: bool = True
    in_sunlight: bool = False
    help_grants: tuple[HelpCheckGrant, ...] = ()
    charmer_ids: tuple[str, ...] = ()
    sight_blocked_targets: frozenset[str] = frozenset()
    sight_dim_targets: frozenset[str] = frozenset()
    surroundings_blocked: bool = False
    surroundings_dim: bool = False


__all__ = ["CheckActorState", "CheckRequest", "GrantedDie", "HelpCheckGrant", "HelpCheckSpec"]
