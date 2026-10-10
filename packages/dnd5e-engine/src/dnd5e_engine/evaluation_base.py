"""Local evaluation schema primitives; not a frozen cross-repository ABI."""

from pydantic import BaseModel, ConfigDict


class EvaluationModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, revalidate_instances="always"
    )
