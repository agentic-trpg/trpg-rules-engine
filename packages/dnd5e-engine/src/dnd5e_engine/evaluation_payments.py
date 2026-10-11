"""Internal rule-priced payments over the single authoritative ResourceState.

ResourceUpdate is one logical fact: its atomic consumer validates the resource
expected value and the snapshot's mirrors, then updates the pool and derived
actor slot/spent mirrors together. Mirrors are never independently payable.
No generic resource-spend command is exposed.
"""

from dnd5e_engine.evaluation_delta import ResourceUpdate
from dnd5e_engine.evaluation_resources import FeatureResource, ResourceState, SlotResource
from dnd5e_engine.feature_runtime import ResourcePayment
from dnd5e_engine.resource_rules import choose_spell_slot, consume_resource


def spell_payment(resources: ResourceState, owner_id: str, level: int) -> ResourceUpdate:
    if not 1 <= level <= 9:
        raise ValueError("invalid spell slot level")
    slots = [p for p in resources.pools if isinstance(p, SlotResource) and p.owner_id == owner_id]
    selected = choose_spell_slot(
        {p.level: p.current for p in slots if p.kind == "spell_slot"},
        {p.level: p.current for p in slots if p.kind == "pact_slot"},
        level,
    )
    if selected is None:
        raise ValueError("no owned slot remains")
    kind, remaining = selected
    pool = next(p for p in slots if p.kind == kind and p.level == level)
    return ResourceUpdate(
        kind="resource.update",
        owner_id=owner_id,
        pool_id=pool.pool_id,
        expected=pool,
        value=pool.model_copy(update={"current": remaining}),
        amount=-1,
    )


def feature_payments(
    resources: ResourceState, owner_id: str, payments: tuple[ResourcePayment, ...]
) -> tuple[ResourceUpdate, ...]:
    if len({p.feature_slug for p in payments}) != len(payments):
        raise ValueError("duplicate priced feature payment")
    updates = []
    for payment in payments:
        matches = [
            p
            for p in resources.pools
            if isinstance(p, FeatureResource)
            and p.owner_id == owner_id
            and p.feature_slug == payment.feature_slug
        ]
        if len(matches) != 1 or matches[0].maximum != payment.maximum:
            raise ValueError("feature owner, identity or priced maximum mismatch")
        pool = matches[0]
        remaining = consume_resource(pool.current, payment.cost)
        updates.append(
            ResourceUpdate(
                kind="resource.update",
                owner_id=owner_id,
                pool_id=pool.pool_id,
                expected=pool,
                value=pool.model_copy(update={"current": remaining}),
                amount=-payment.cost,
            )
        )
    return tuple(updates)
