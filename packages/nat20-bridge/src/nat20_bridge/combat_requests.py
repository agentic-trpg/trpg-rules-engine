"""JSON transport models reuse the authoritative Engine input contracts."""

import json
from typing import Any

from dnd5e_engine import CombatObject, ObjectMutation, PlayerIntent, StrongWind
from pydantic import BaseModel, ConfigDict, Field, model_validator


class IntentRequest(PlayerIntent):
    """Validate the Bridge wire representation of the shared PlayerIntent contract."""

    actor_id: str = Field(strict=True, min_length=1)
    request_id: str | None = Field(default=None, strict=True, min_length=1, max_length=128)

    @model_validator(mode="before")
    @classmethod
    def strict_wire_intent(cls, value: Any) -> Any:
        # FastAPI has decoded JSON arrays to lists. Strict JSON mode preserves
        # tuple/enum wire representations while rejecting coercion of scalars.
        """Require the wire intent to preserve strict shared PlayerIntent types."""
        if isinstance(value, dict):
            payload = {k: v for k, v in value.items() if k not in {"actor_id", "request_id"}}
            PlayerIntent.model_validate_json(json.dumps(payload), strict=True)
        return value

    def intent(self) -> PlayerIntent:
        """Restore the shared PlayerIntent from validated transport input."""
        return PlayerIntent.model_validate_json(
            self.model_dump_json(exclude={"actor_id", "request_id"}), strict=True
        )


class MutationRequest(BaseModel):
    """Carry guarded request identity for trusted Bridge host mutations."""

    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)


class HostRequest(MutationRequest):
    """Associate a trusted Host mutation with its owner authorization."""

    request_id: str = Field(min_length=1, max_length=128)


class RegisterObjectsRequest(HostRequest):
    """Carry the trusted combat object registration payload."""

    objects: list[CombatObject] = Field(min_length=1)


class MutateObjectRequest(HostRequest):
    """Carry the trusted object mutation payload and request identity."""

    mutation: ObjectMutation


class WindRequest(HostRequest):
    """Carry a trusted wind mutation with guarded request identity."""

    wind: StrongWind

    @model_validator(mode="before")
    @classmethod
    def strict_wind(cls, value: Any) -> Any:
        """Validate the explicit wind input before shared environment handling."""
        if isinstance(value, dict) and "wind" in value:
            StrongWind.model_validate_json(json.dumps(value["wind"]), strict=True)
        return value
