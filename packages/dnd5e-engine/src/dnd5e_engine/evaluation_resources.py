"""Explicit resource ownership; inventory instances and action budgets stay distinct."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from dnd5e_engine.evaluation_base import EvaluationModel


class ResourcePoolBase(EvaluationModel):
    owner_id: Annotated[str, Field(min_length=1)]
    pool_id: Annotated[str, Field(min_length=1)]
    current: Annotated[int, Field(ge=0)]
    maximum: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def bounds(self) -> Self:
        if self.current > self.maximum:
            raise ValueError("resource current exceeds its explicit maximum")
        return self


class HitDiceResource(ResourcePoolBase):
    kind: Literal["hit_dice"]
    class_slug: Annotated[str, Field(min_length=1)]
    die_size: Literal[6, 8, 10, 12]


class SlotResource(ResourcePoolBase):
    kind: Literal["spell_slot", "pact_slot"]
    level: Annotated[int, Field(ge=1, le=9)]


class FeatureResource(ResourcePoolBase):
    kind: Literal["feature_uses"]
    feature_slug: Annotated[str, Field(min_length=1)]


ResourcePool = Annotated[
    HitDiceResource | SlotResource | FeatureResource, Field(discriminator="kind")
]


class ResourceState(EvaluationModel):
    pools: tuple[ResourcePool, ...]

    @model_validator(mode="after")
    def unique_ownership(self) -> Self:
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
    pool_id: Annotated[str, Field(min_length=1)]
    amount: Annotated[int, Field(ge=1)]
