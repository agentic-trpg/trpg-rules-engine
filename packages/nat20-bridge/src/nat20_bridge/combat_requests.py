"""JSON transport models reuse the authoritative Engine input contracts."""

import json
from typing import Any

from dnd5e_engine import CombatObject, ObjectMutation, PlayerIntent, StrongWind
from pydantic import BaseModel, ConfigDict, Field, model_validator


class IntentRequest(PlayerIntent):
    actor_id: str = Field(strict=True, min_length=1)
    request_id: str | None = Field(default=None, strict=True, min_length=1, max_length=128)

    @model_validator(mode="before")
    @classmethod
    def strict_wire_intent(cls, value: Any) -> Any:
        # FastAPI has decoded JSON arrays to lists. Strict JSON mode preserves
        # tuple/enum wire representations while rejecting coercion of scalars.
        if isinstance(value, dict):
            payload = {k: v for k, v in value.items() if k not in {"actor_id", "request_id"}}
            PlayerIntent.model_validate_json(json.dumps(payload), strict=True)
        return value

    def intent(self) -> PlayerIntent:
        return PlayerIntent.model_validate_json(
            self.model_dump_json(exclude={"actor_id", "request_id"}), strict=True
        )


class MutationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)


class HostRequest(MutationRequest):
    request_id: str = Field(min_length=1, max_length=128)


class RegisterObjectsRequest(HostRequest):
    objects: list[CombatObject] = Field(min_length=1)


class MutateObjectRequest(HostRequest):
    mutation: ObjectMutation


class WindRequest(HostRequest):
    wind: StrongWind

    @model_validator(mode="before")
    @classmethod
    def strict_wind(cls, value: Any) -> Any:
        if isinstance(value, dict) and "wind" in value:
            StrongWind.model_validate_json(json.dumps(value["wind"]), strict=True)
        return value
