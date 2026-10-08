"""Combat-local Host attestations, separate from equipment and spell ownership."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ObjectChangeOperation = Literal[
    "created",
    "move",
    "pickup",
    "drop",
    "wear",
    "cover",
    "uncover",
    "removed",
    "holder_died",
    "holder_left",
    "combat_end",
]


class CombatObject(BaseModel):
    """Ground position OR live holder position, never two authoritative origins.

    owner_id names the Host mutation authority, not a spell targeting restriction.
    source_id identifies the Host record that introduced the object.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    id: str = Field(min_length=1, pattern=r"^object:[A-Za-z0-9][A-Za-z0-9:._-]*$")
    owner_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    position: str | None = None
    disposition: Literal["unattended", "carried", "worn"] = "unattended"
    holder_id: str | None = None
    opaque_cover: bool = False

    @model_validator(mode="after")
    def one_position_authority(self) -> "CombatObject":
        if self.disposition == "unattended":
            valid = self.position is not None and self.holder_id is None
        else:
            valid = self.position is None and bool(self.holder_id)
        if not valid:
            raise ValueError("object requires either a ground position or a holder")
        return self


class ObjectMutation(BaseModel):
    """A completed Host interaction; does not execute an SRD object action."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    object_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    operation: Literal["move", "pickup", "drop", "wear", "cover", "uncover", "remove"]
    position: str | None = None
    holder_id: str | None = None

    @model_validator(mode="after")
    def operation_inputs(self) -> "ObjectMutation":
        if (self.position is not None) != (self.operation == "move"):
            raise ValueError("only move requires a position")
        if (self.holder_id is not None) != (self.operation in ("pickup", "wear")):
            raise ValueError("only pickup/wear require a holder")
        return self


class CombatObjectView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    object: CombatObject
    position: str
    area_ids: tuple[str, ...] = ()


__all__ = ["CombatObject", "CombatObjectView", "ObjectMutation"]
