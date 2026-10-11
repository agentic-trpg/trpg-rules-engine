"""Complete resource mirror and spend projection for rule-priced invocations."""

from dnd5e_engine.evaluation_delta import ResourceUpdate, SlotSpendLedgerUpdate, StateDelta
from dnd5e_engine.evaluation_projection import EvaluationInvariantError, attack_delta
from dnd5e_engine.evaluation_resources import FeatureResource, ResourceState, SlotResource
from dnd5e_engine.evaluation_state import CombatSnapshot


def payment_post_state(
    snapshot: CombatSnapshot, payments: tuple[ResourceUpdate, ...]
) -> CombatSnapshot:
    """Private immutable computation, not an authoritative apply/commit API."""
    if snapshot.resource_state is None:
        raise EvaluationInvariantError("missing payment pools")
    pools = {(p.owner_id, p.pool_id): p for p in snapshot.resource_state.pools}
    actors = {a.entity_id: a for a in snapshot.character_states}
    seen = set()
    for payment in payments:
        key = (payment.owner_id, payment.pool_id)
        if key in seen or pools.get(key) != payment.expected:
            raise EvaluationInvariantError("duplicate/stale priced payment")
        seen.add(key)
        pools[key] = payment.value
        actor = actors[payment.owner_id]
        pool = payment.value
        if isinstance(pool, SlotResource):
            field = "spell_slots" if pool.kind == "spell_slot" else "pact_slots"
            actor = actor.model_copy(
                update={field: {**getattr(actor, field), pool.level: pool.current}}
            )
        elif isinstance(pool, FeatureResource):
            actor = actor.model_copy(
                update={
                    "custom_counters": {
                        **actor.custom_counters,
                        "feature_use:" + pool.feature_slug: {"spent": pool.maximum - pool.current},
                    }
                }
            )
        else:
            raise EvaluationInvariantError("invocation cannot spend Hit Dice")
        actors[actor.entity_id] = actor
    return snapshot.model_copy(
        update={
            "resource_state": ResourceState(pools=tuple(pools.values())),
            "character_states": tuple(actors[a.entity_id] for a in snapshot.character_states),
        }
    )


def paid_delta(
    before: CombatSnapshot,
    after: CombatSnapshot,
    payments: tuple[ResourceUpdate, ...],
    ledgers: tuple[SlotSpendLedgerUpdate, ...],
) -> StateDelta:
    expected = payment_post_state(before, payments)
    if after.resource_state != expected.resource_state:
        raise EvaluationInvariantError("unrepresented payment pool change")
    actors = []
    for old, paid, new in zip(
        before.character_states, expected.character_states, after.character_states, strict=True
    ):
        fields = ("spell_slots", "pact_slots", "custom_counters")
        if any(getattr(paid, f) != getattr(new, f) for f in fields):
            raise EvaluationInvariantError("unrepresented payment mirror change")
        actors.append(new.model_copy(update={f: getattr(old, f) for f in fields}))
    spend = dict(before.combat_state.expended_resources)
    for ledger in ledgers:
        if (
            ledger.combat_id != before.combat_state.combat_id
            or spend.get(ledger.owner_id) != ledger.expected
        ):
            raise EvaluationInvariantError("stale slot spend ledger")
        spend[ledger.owner_id] = ledger.value
    if spend != after.combat_state.expended_resources:
        raise EvaluationInvariantError("unrepresented slot spend attribution")
    slots = [p for p in payments if isinstance(p.value, SlotResource)]
    if len(slots) != len(ledgers) or any(
        not any(
            p.owner_id == ledger.owner_id
            and p.value.kind == ledger.slot_kind
            and p.value.level == ledger.level
            and p.amount == -1
            for p in slots
        )
        for ledger in ledgers
    ):
        raise EvaluationInvariantError("slot payments and spend ledger differ")
    normalized = after.model_copy(
        update={
            "resource_state": before.resource_state,
            "character_states": tuple(actors),
            "combat_state": after.combat_state.model_copy(
                update={"expended_resources": before.combat_state.expended_resources}
            ),
        }
    )
    mechanical = attack_delta(before, normalized)
    return StateDelta(
        expected_world_version=before.world_version,
        operations=(*payments, *ledgers, *mechanical.operations),
    )


def slot_ledger(snapshot: CombatSnapshot, payment: ResourceUpdate) -> SlotSpendLedgerUpdate:
    pool = payment.value
    if not isinstance(pool, SlotResource) or payment.amount != -1:
        raise EvaluationInvariantError("not a single slot payment")
    prior = snapshot.combat_state.expended_resources.get(payment.owner_id)
    value = dict(prior or {})
    key = f"{pool.kind}:{pool.level}"
    value[key] = value.get(key, 0) + 1
    return SlotSpendLedgerUpdate(
        kind="combat.slot_spend_update",
        combat_id=snapshot.combat_state.combat_id,
        owner_id=payment.owner_id,
        slot_kind=pool.kind,
        level=pool.level,
        expected=prior,
        value=value,
    )
