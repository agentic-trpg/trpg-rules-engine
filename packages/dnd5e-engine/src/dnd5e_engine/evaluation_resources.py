"""Explicit resource ownership; inventory instances and action budgets stay distinct."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from dnd5e_engine.evaluation_base import EvaluationModel


class ResourcePoolBase(EvaluationModel):
    """Validate owned resource identity, capacity and remaining balance."""

    owner_id: Annotated[str, Field(min_length=1)]
    pool_id: Annotated[str, Field(min_length=1)]
    current: Annotated[int, Field(ge=0)]
    maximum: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def bounds(self) -> Self:
        """Reject negative counters or values outside the component's mechanical bounds."""
        if self.current > self.maximum:
            raise ValueError("resource current exceeds its explicit maximum")
        return self


class HitDiceResource(ResourcePoolBase):
    """Represent an owned hit-dice pool with its die size."""

    kind: Literal["hit_dice"]
    class_slug: Annotated[str, Field(min_length=1)]
    die_size: Literal[6, 8, 10, 12]


class SlotResource(ResourcePoolBase):
    """Represent an owned spell or pact slot pool at an explicit level."""

    kind: Literal["spell_slot", "pact_slot"]
    level: Annotated[int, Field(ge=1, le=9)]


class FeatureResource(ResourcePoolBase):
    """Represent a named feature's owned resource capacity and remaining uses."""

    kind: Literal["feature_uses"]
    feature_slug: Annotated[str, Field(min_length=1)]


ResourcePool = Annotated[
    HitDiceResource | SlotResource | FeatureResource, Field(discriminator="kind")
]


class ResourceState(EvaluationModel):
    """Group explicit owned resource pools and validate identity uniqueness."""

    pools: tuple[ResourcePool, ...]

    @model_validator(mode="after")
    def unique_ownership(self) -> Self:
        """Require resource identity and ownership to be unique within the component."""
        identities = [(p.owner_id, p.pool_id) for p in self.pools]
        semantic = [
            (
                p.owner_id,
                p.kind,
                p.class_slug
                if isinstance(p, HitDiceResource)
                else p.level
                if isinstance(p, SlotResource)
                else p.feature_slug,
            )
            for p in self.pools
        ]
        if len(set(identities)) != len(identities) or len(set(semantic)) != len(semantic):
            raise ValueError("duplicate owned or mechanical resource identity")
        return self


class HitDieSpend(EvaluationModel):
    """Select an explicit number of dice from one owned hit-dice pool."""

    pool_id: Annotated[str, Field(min_length=1)]
    amount: Annotated[int, Field(ge=1)]
