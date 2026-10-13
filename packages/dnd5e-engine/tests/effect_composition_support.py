"""Trusted test Host boundary and independent guarded typed-Delta consumer.

No production graph projector is used to derive expected/apply behavior.
"""

from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd5e_srd_data.schema.common import DamagePart

from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd5e_engine.evaluation_delta import (
    ConcentrationUpdate,
    EffectLifecycleUpdate,
    PendingReactionUpdate,
    ResourceUpdate,
    SlotSpendLedgerUpdate,
)
from dnd5e_engine.evaluation_reaction_state import (
    PendingReactionState,
    ReactionConditionState,
    ReactionSemanticsState,
)
from dnd5e_engine.evaluation_resources import ResourceState, SlotResource
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_engine.evaluation_shield_delta import ShieldStateTransition
from dnd5e_engine.evaluation_state import CombatSnapshot
from tests.evaluation_support import FOE, HERO, WEAPON
from tests.test_stateless_attack import apply_delta, execute
from tests.test_stateless_attack_concentration import ATTACKER
from tests.test_stateless_hold_cast import apply_cast, cast_case


def declare_shield(snapshot, loader):
    """Test Host supplies one authorized declaration at an explicit boundary."""
    activity = loader.get_spell("shield").activities[0]
    declaration = PendingReactionState(
        owner_id=FOE,
        spell_id="shield",
        activity_id=activity.id,
        slot_level=1,
        conditions=tuple(
            ReactionConditionState.model_validate(c.model_dump())
            for c in activity.activation.reaction_conditions
        ),
        semantics=ReactionSemanticsState.model_validate_json(activity.reaction.model_dump_json()),
        source_kind="spell",
        spell_source_id="class:wizard",
    )
    assert not snapshot.combat_state.pending_reactions
    assert not snapshot.combat_state.active_reaction_responses
    return CombatSnapshot.model_validate_json(
        snapshot.model_copy(
            update={
                "combat_state": snapshot.combat_state.model_copy(
                    update={"pending_reactions": [declaration]}
                )
            }
        ).model_dump_json()
    )


SECOND_TARGET = "char:second-target"
SECOND_ATTACKER = "char:second-attacker"


def composed_case(
    *,
    seed=0,
    pc=False,
    hp=40,
    temp=0,
    multi=False,
    extra_attacker=False,
    mixed=False,
    target_wisdom=None,
):
    request, _, _, loader = cast_case(seed=seed, pc=pc, multi=multi)
    actors = tuple(
        a.model_copy(update={"attack_bonus": 2})
        if a.entity_id == ATTACKER
        else a.model_copy(update={"hp_current": hp, "temp_hp": temp})
        if a.entity_id == FOE
        else a
        for a in request.state_snapshot.character_states
    )
    request = request.model_copy(
        update={
            "state_snapshot": request.state_snapshot.model_copy(update={"character_states": actors})
        }
    )
    if target_wisdom is not None:
        snapshot = request.state_snapshot
        request = request.model_copy(
            update={
                "state_snapshot": snapshot.model_copy(
                    update={
                        "character_states": tuple(
                            a.model_copy(update={"wisdom": target_wisdom})
                            if a.entity_id == HERO
                            else a
                            for a in snapshot.character_states
                        )
                    }
                )
            }
        )
    if multi or extra_attacker:
        snapshot, state = request.state_snapshot, request.state_snapshot.combat_state
        ref = HERO if multi else ATTACKER
        identity = SECOND_TARGET if multi else SECOND_ATTACKER
        original_target = next(a for a in snapshot.character_states if a.entity_id == ref)
        second = original_target.model_copy(
            update={"entity_id": identity, "name": "Extra initial participant"}
        )
        ids = (ATTACKER, HERO, identity, FOE) if multi else (ATTACKER, identity, HERO, FOE)
        actors = {a.entity_id: a for a in snapshot.character_states}
        actors[identity] = second
        side = "party_ids" if ref in state.party_ids else "encounter_ids"
        snapshot = snapshot.model_copy(
            update={
                "character_states": tuple(actors[i] for i in ids),
                "combat_state": state.model_copy(
                    update={
                        "initiative_ids": ids,
                        "current_turn_index": 3,
                        "actor_zone": {**state.actor_zone, identity: "1,1"},
                        "movement_ledgers": {
                            **state.movement_ledgers,
                            identity: state.movement_ledgers[ref],
                        },
                        side: getattr(state, side) | {identity},
                        "xp_value_by_entity": {**state.xp_value_by_entity, identity: 0},
                    }
                ),
            }
        )
        request = request.model_copy(
            update={
                "state_snapshot": CombatSnapshot.model_validate_json(snapshot.model_dump_json()),
                "payload": request.payload.model_copy(update={"target_ids": (HERO, SECOND_TARGET)})
                if multi
                else request.payload,
            }
        )
    if mixed:
        canonical = BundledAssetLoader()
        caster = next(a for a in request.state_snapshot.character_states if a.entity_id == FOE)
        source = request.state_snapshot.casting_state.actors[0].sources[0]
        loader = MemoryAssetLoader(
            items=[
                canonical.get_item("component-pouch"),
                loader.get_weapon(WEAPON).model_copy(
                    update={
                        "damage_parts": [
                            DamagePart(dice="12", damage_type="slashing"),
                            DamagePart(dice="12", damage_type="fire"),
                        ]
                    }
                ),
            ],
            spells=[loader.get_spell(s) for s in source.known_spells],
            classes=[canonical.get_class("wizard")],
            subclasses=[canonical.get_subclass("evoker")],
            features=[canonical.get_feature(s) for s in caster.granted_features],
            conditions=[canonical.get_condition("paralyzed")],
        )
        request = request.model_copy(update={"ruleset_binding": ruleset_binding(loader)})
    first = execute(request, loader)
    assert first.status == "accepted", first.error
    after = apply_cast(request.state_snapshot, first)
    after = declare_shield(after, loader)
    return (
        request.model_copy(
            update={
                "command_id": "command:composed-attack",
                "actor_id": ATTACKER,
                "state_snapshot": after,
                "payload": CombatIntentPayload(
                    intent_type="attack", weapon_id=WEAPON, target_id=FOE
                ),
                "rng_context": request.rng_context.model_copy(
                    update={"state": first.rng_transition.next_state}
                ),
            }
        ),
        loader,
        first,
    )


def apply_composed(snapshot, result):
    """Check all old graph components before applying any unpublished proposal."""
    assert result.state_delta.expected_world_version == snapshot.world_version
    assert result.read_set[0].version == snapshot.world_version
    graph_types = (ConcentrationUpdate, EffectLifecycleUpdate, ShieldStateTransition)
    graph_ops = [o for o in result.state_delta.operations if isinstance(o, graph_types)]
    seen = set()
    for op in result.state_delta.operations:
        identity = getattr(
            op,
            "identity",
            getattr(
                op,
                "target_id",
                getattr(op, "actor_id", getattr(op, "source_id", getattr(op, "owner_id", None))),
            ),
        )
        key = (op.kind, identity)
        assert key not in seen
        seen.add(key)
    for op in graph_ops:
        assert op.combat_id == snapshot.combat_state.combat_id
        if isinstance(op, ConcentrationUpdate):
            source = next(a for a in snapshot.character_states if a.entity_id == op.source_id)
            assert source.concentration_effect_id == op.expected_effect_id
            assert (
                tuple(snapshot.combat_state.concentration_chain[op.source_id]) == op.expected_chain
            )
            assert (
                snapshot.combat_state.concentration_rounds_remaining[op.source_id]
                == op.expected_rounds
            )
        elif isinstance(op, EffectLifecycleUpdate):
            identity = op.identity
            assert (
                next(
                    (
                        e
                        for e in snapshot.effect_states
                        if (e.target_id, e.id, e.origin) == identity
                    ),
                    None,
                )
                == op.expected_effect
            )
            assert (
                next(
                    (c for c in snapshot.combat_state.effect_lifecycles if c.identity == identity),
                    None,
                )
                == op.expected_lifecycle
            )
            assert (
                next(
                    (
                        c
                        for c in snapshot.combat_state.conditions_by_effect
                        if c.identity == identity
                    ),
                    None,
                )
                == op.expected_lineage
            )
        else:
            assert op.actor_ids == snapshot.combat_state.initiative_ids
            old = op.expected
            shield = [e for e in snapshot.effect_states if e.id == "effect:imperceptible_barrier"]
            assert shield == ([old.effect] if old else [])
            assert snapshot.combat_state.active_reaction_responses == (
                [old.response.response()] if old and old.response else []
            )
            assert snapshot.combat_state.reaction_effects_pending_expiry == (
                {op.owner_id: [old.expiry_identity]} if old else {}
            )
            if old:
                assert old.lineage in snapshot.combat_state.conditions_by_effect
                assert old.owner_dead == (op.owner_id in snapshot.combat_state.dead_ids)
    filtered = result.model_copy(
        update={
            "state_delta": result.state_delta.model_copy(
                update={
                    "operations": tuple(
                        o for o in result.state_delta.operations if not isinstance(o, graph_types)
                    )
                }
            )
        }
    )
    after = apply_paid(snapshot, filtered)
    effects = {(e.target_id, e.id, e.origin): e for e in after.effect_states}
    clocks = {c.identity: c for c in after.combat_state.effect_lifecycles}
    links = {c.identity: c for c in after.combat_state.conditions_by_effect}
    chains = dict(after.combat_state.concentration_chain)
    rounds = dict(after.combat_state.concentration_rounds_remaining)
    responses = list(after.combat_state.active_reaction_responses)
    expiry = dict(after.combat_state.reaction_effects_pending_expiry)
    actors = {a.entity_id: a for a in after.character_states}
    for op in graph_ops:
        if isinstance(op, ConcentrationUpdate):
            actors[op.source_id] = actors[op.source_id].model_copy(
                update={"concentration_effect_id": op.effect_id}
            )
            if op.chain:
                chains[op.source_id] = list(op.chain)
                rounds[op.source_id] = op.rounds
            else:
                chains.pop(op.source_id)
                rounds.pop(op.source_id)
        elif isinstance(op, EffectLifecycleUpdate):
            for mapping, value in (
                (effects, op.effect),
                (clocks, op.lifecycle),
                (links, op.lineage),
            ):
                if value is None:
                    mapping.pop(op.identity, None)
                else:
                    mapping[op.identity] = value
        else:
            if op.expected:
                effects.pop(op.expected.expiry_identity)
                links.pop(op.expected.expiry_identity)
            new = op.value
            responses = [new.response.response()] if new and new.response else []
            expiry = {op.owner_id: [new.expiry_identity]} if new else {}
            if new:
                effects[new.expiry_identity] = new.effect
                links[new.expiry_identity] = new.lineage
                assert new.owner_dead == (op.owner_id in after.combat_state.dead_ids)
    value = after.model_copy(
        update={
            "character_states": tuple(actors[a.entity_id] for a in after.character_states),
            "effect_states": tuple(effects.values()),
            "combat_state": after.combat_state.model_copy(
                update={
                    "effect_lifecycles": tuple(clocks.values()),
                    "conditions_by_effect": tuple(links.values()),
                    "concentration_chain": chains,
                    "concentration_rounds_remaining": rounds,
                    "active_reaction_responses": responses,
                    "reaction_effects_pending_expiry": expiry,
                }
            ),
        }
    )
    return CombatSnapshot.model_validate_json(value.model_dump_json())


def apply_paid(snapshot, result):
    """Independent resource mirrors and one ledger/declaration transition."""
    kinds = (ResourceUpdate, SlotSpendLedgerUpdate, PendingReactionUpdate)
    pools = {(p.owner_id, p.pool_id): p for p in snapshot.resource_state.pools}
    for op in result.state_delta.operations:
        if isinstance(op, ResourceUpdate):
            assert pools[(op.owner_id, op.pool_id)] == op.expected
        elif isinstance(op, SlotSpendLedgerUpdate):
            assert op.combat_id == snapshot.combat_state.combat_id
            assert snapshot.combat_state.expended_resources.get(op.owner_id) == op.expected
        elif isinstance(op, PendingReactionUpdate):
            assert op.combat_id == snapshot.combat_state.combat_id
            assert tuple(snapshot.combat_state.pending_reactions) == op.expected
    filtered = result.model_copy(
        update={
            "state_delta": result.state_delta.model_copy(
                update={
                    "operations": tuple(
                        o for o in result.state_delta.operations if not isinstance(o, kinds)
                    )
                }
            )
        }
    )
    after = apply_delta(snapshot, filtered)
    actors = {a.entity_id: a for a in after.character_states}
    spend = dict(after.combat_state.expended_resources)
    pending = list(after.combat_state.pending_reactions)
    for op in result.state_delta.operations:
        if isinstance(op, ResourceUpdate):
            assert isinstance(op.value, SlotResource)
            pools[(op.owner_id, op.pool_id)] = op.value
            field = "spell_slots" if op.value.kind == "spell_slot" else "pact_slots"
            actor = actors[op.owner_id]
            actors[op.owner_id] = actor.model_copy(
                update={field: {**getattr(actor, field), op.value.level: op.value.current}}
            )
        elif isinstance(op, SlotSpendLedgerUpdate):
            spend[op.owner_id] = op.value
        elif isinstance(op, PendingReactionUpdate):
            pending = list(op.value)
    return after.model_copy(
        update={
            "resource_state": ResourceState(pools=tuple(pools.values())),
            "character_states": tuple(actors[a.entity_id] for a in after.character_states),
            "combat_state": after.combat_state.model_copy(
                update={"expended_resources": spend, "pending_reactions": pending}
            ),
        }
    )


def continued(request, result, *, intent=None):
    snapshot = apply_composed(request.state_snapshot, result)
    # Test Host accepts one whole proposal and advances its existing version.
    # RE itself neither commits nor supplies an authoritative version increment.
    snapshot = snapshot.model_copy(update={"world_version": snapshot.world_version + 1})
    return request.model_copy(
        update={
            "command_id": request.command_id + ":next",
            "state_snapshot": snapshot,
            "actor_id": snapshot.combat_state.initiative_ids[
                snapshot.combat_state.current_turn_index
            ],
            "payload": intent or CombatIntentPayload(intent_type="pass"),
            "rng_context": request.rng_context.model_copy(
                update={
                    "state": result.rng_transition.next_state,
                    "version": request.rng_context.version + 1,
                }
            ),
        }
    )
