"""Canonical consumable execution, ownership and atomic proposal application."""

import asyncio
from copy import deepcopy

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from pydantic import ValidationError

from dnd5e_engine import orchestrator as orch
from dnd5e_engine.evaluation_contracts import (
    CombatIntentPayload,
    ItemUsePayload,
    RuleEvaluationRequest,
    RuleEvaluationResult,
)
from dnd5e_engine.evaluation_delta import ActionBudgetUpdate, HPDelta, InventoryConsume
from dnd5e_engine.evaluation_projection import EvaluationInvariantError
from dnd5e_engine.evaluation_rng import RNGContext, RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_engine.evaluation_snapshot import capture_evaluation_snapshot
from dnd5e_engine.evaluation_state import CombatSnapshot, InventoryEntry, InventoryState
from dnd5e_engine.events import HealingApplied
from dnd5e_engine.lib_loader import scoped_lib_loader
from tests.evaluation_support import FOE, HERO, WEAPON, synthetic_loader
from tests.test_stateless_attack import execute
from tests.test_stateless_weapons import weapon_case

POTION = "potion-of-healing"
ACTIVITY = "ZyWmXem7zzQwFFcF"


def item_case(*, hp=10, quantity=1, charges=1, **entry_updates):
    request, handle, live, _ = weapon_case("mace", seed=1)
    potion = BundledAssetLoader().get_item(POTION)
    loader = MemoryAssetLoader(
        items=[
            potion,
            BundledAssetLoader().get_weapon("mace"),
            synthetic_loader().get_weapon(WEAPON),
        ]
    )
    live.ruleset_loader = loader
    live.initiative[0] = live.initiative[0].model_copy(update={"hp_current": hp})
    live.tracked_hp[HERO] = hp
    actors = list(request.state_snapshot.character_states)
    actors[0] = actors[0].model_copy(update={"hp_current": hp})
    entry = dict(
        instance_id="inventory:potion-stack",
        owner_id=HERO,
        item_slug=POTION,
        quantity=quantity,
        charges_remaining_per_unit=charges,
        accessible=True,
    )
    entry.update(entry_updates)
    snapshot = CombatSnapshot(
        **request.state_snapshot.model_dump(
            exclude={
                "snapshot_schema_version",
                "snapshot_kind",
                "character_states",
                "inventory_state",
            }
        ),
        snapshot_schema_version="engine-snapshot/5",
        snapshot_kind="combat",
        character_states=tuple(actors),
        inventory_state=InventoryState(entries=(InventoryEntry(**entry),)),
    )
    request = request.model_copy(
        update={
            "schema_version": "engine-evaluation/7",
            "operation_kind": "combat.item",
            "state_snapshot": snapshot,
            "payload": ItemUsePayload(
                kind="combat.item",
                instance_id="inventory:potion-stack",
                activity_id=ACTIVITY,
                target_id=HERO,
            ),
            "ruleset_binding": ruleset_binding(loader),
        }
    )
    return request, handle, live, loader


def legacy_intent():
    return CombatIntentPayload(
        intent_type="use_item", item_id=POTION, activity_id=ACTIVITY, target_id=HERO
    )


def test_confirmed_legacy_item_activation_cost_uses_typed_bonus_not_item_name():
    _, handle, live, loader = item_case()
    with scoped_lib_loader(loader):
        asyncio.run(orch.submit_player_intent(handle, HERO, legacy_intent()))
    actor = orch._find_combatant(live, HERO)
    assert actor.action_available is True
    assert actor.bonus_action_available is False
    assert actor.bonus_action_taken_this_turn is True
    assert any(isinstance(event, HealingApplied) for event in live.event_log)


def apply_atomic(store, result, *, fail_at=None):
    """Independent consumer commits only the fully checked scratch unit."""
    scratch = deepcopy(store)
    snapshot = scratch["snapshot"]
    assert result.state_delta.expected_world_version == snapshot.world_version
    result.rng_transition.verify_input(scratch["rng"])
    value = snapshot.model_dump(mode="python")
    actors = {a["entity_id"]: a for a in value["character_states"]}
    inventory = {e["instance_id"]: e for e in value["inventory_state"]["entries"]}
    touched = set()
    for index, op in enumerate(result.state_delta.operations):
        if isinstance(op, InventoryConsume):
            key = ("inventory", op.expected.instance_id)
            assert inventory[op.expected.instance_id] == op.expected.model_dump()
            inventory[op.expected.instance_id].update(op.value.model_dump())
        elif isinstance(op, HPDelta):
            key = ("hp", op.target_id)
            assert actors[op.target_id]["hp_current"] == op.expected_hp
            assert 0 <= op.resulting_hp <= actors[op.target_id]["hp_max"]
            actors[op.target_id]["hp_current"] += op.amount
            assert actors[op.target_id]["hp_current"] == op.resulting_hp
        elif isinstance(op, ActionBudgetUpdate):
            key = ("budget", op.actor_id)
            expected = op.expected.model_dump(mode="python")
            assert {k: actors[op.actor_id][k] for k in expected} == expected
            actors[op.actor_id].update(op.value.model_dump(mode="python"))
        else:
            raise TypeError(f"unexpected healing operation: {op.kind}")
        assert key not in touched
        touched.add(key)
        if fail_at == index:
            raise RuntimeError("injected consumer failure")
    value["combat_state"]["event_log"] += tuple(e.model_dump() for e in result.proposed_events)
    value["world_version"] += 1  # Test host policy; not a frozen Engine ABI rule.
    from pydantic_core import to_json

    scratch["snapshot"] = CombatSnapshot.model_validate_json(to_json(value))
    scratch["rng"] = RNGContext(
        stream_id=result.rng_transition.stream_id,
        version=scratch["rng"].version + 1,
        state=result.rng_transition.next_state,
    )
    store.update(scratch)


@pytest.mark.parametrize("hp", [1, 10, 39, 40])
@pytest.mark.parametrize("quantity", [1, 2])
def test_canonical_healing_inventory_budget_rng_events_and_legacy_parity(hp, quantity):
    request, handle, live, loader = item_case(hp=hp, quantity=quantity)
    before = deepcopy(request)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert execute(request, loader) == result
    assert request == before
    assert RuleEvaluationRequest.model_validate_json(request.model_dump_json()) == request
    assert RuleEvaluationResult.model_validate_json(result.model_dump_json()) == result
    asyncio.run(orch.submit_player_intent(handle, HERO, legacy_intent()))
    assert result.proposed_events == tuple(live.event_log)
    assert result.rng_transition.next_state == RNGState.capture(live.rng)
    assert result.rng_transition.state_changed
    assert live.custom_counters_by_entity[HERO][orch._item_use_counter_key(POTION)] == {"spent": 1}
    del live.custom_counters_by_entity[HERO][orch._item_use_counter_key(POTION)]
    from dnd5e_engine.specs import GridScene

    expected = capture_evaluation_snapshot(
        live,
        request.state_snapshot,
        GridScene.model_validate(request.state_snapshot.scene_state.grid.model_dump()),
    )
    store = {"snapshot": request.state_snapshot, "rng": request.rng_context}
    original = deepcopy(store)
    for index in range(len(result.state_delta.operations)):
        with pytest.raises(RuntimeError, match="consumer failure"):
            apply_atomic(store, result, fail_at=index)
        assert store == original
    apply_atomic(store, result)
    actual = store["snapshot"].model_copy(update={"world_version": 7})
    assert actual.model_copy(update={"inventory_state": expected.inventory_state}) == expected
    assert store["snapshot"].inventory_state.entries[0].quantity == quantity - 1
    actor = next(a for a in actual.character_states if a.entity_id == HERO)
    assert actor.hp_current == min(40, hp + 5)  # seed 1: 2d4+2 = 2+1+2.
    assert actor.action_available is True
    assert actor.bonus_action_available is False
    assert actor.custom_counters == before.state_snapshot.character_states[0].custom_counters
    assert any(isinstance(op, HPDelta) for op in result.state_delta.operations) == (hp < 40)
    with pytest.raises(AssertionError):
        apply_atomic(store, result)


@pytest.mark.parametrize(
    "updates,expected",
    [
        ({"quantity": 0}, "item_exhausted"),
        ({"charges": 0}, "item_exhausted"),
        ({"owner_id": FOE}, "item_unauthorized"),
        ({"accessible": False}, "item_unauthorized"),
        ({"charges": 2}, "item.charges"),
        ({"item_slug": WEAPON}, "item.capability"),
    ],
)
def test_inventory_preflight_before_rng(updates, expected, monkeypatch):
    request, _, _, loader = item_case(**updates)
    before = deepcopy(request)
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("RNG before admission"))
    result = execute(request, loader)
    assert result.status in ("rejected", "unsupported")
    assert result.error.code == expected
    assert result.state_delta is result.rng_transition is None
    assert result.proposed_events == ()
    assert request == before


@pytest.mark.parametrize(
    "payload_updates",
    [
        {"target_id": FOE},
        {"target_id": "missing"},
        {"activity_id": "unknown"},
        {"instance_id": "missing"},
    ],
)
def test_invalid_payload_rejected_without_rng(payload_updates, monkeypatch):
    request, _, _, loader = item_case()
    request = request.model_copy(
        update={"payload": request.payload.model_copy(update=payload_updates)}
    )
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("RNG before rejection"))
    assert execute(request, loader).status == "rejected"


@pytest.mark.parametrize(
    "updates",
    [
        {"bonus_action_available": False},
        {"other_hand_occupied": True},
        {"shield_equipped": True},
        {"weapon_grip": "two_handed"},
    ],
)
def test_no_budget_or_hand_before_rng(updates, monkeypatch):
    request, _, _, loader = item_case()
    actors = list(request.state_snapshot.character_states)
    actors[0] = actors[0].model_copy(update=updates)
    request = request.model_copy(
        update={
            "state_snapshot": request.state_snapshot.model_copy(
                update={"character_states": tuple(actors)}
            )
        }
    )
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("RNG before rejection"))
    assert execute(request, loader).status == "rejected"


def test_actor_turn_charge_authority_schema_and_projection_faults(monkeypatch):
    request, _, _, loader = item_case(quantity=2)
    actors = list(request.state_snapshot.character_states)
    actors[0] = actors[0].model_copy(
        update={"custom_counters": {orch._item_use_counter_key(POTION): {"spent": 0}}}
    )
    conflicting = request.model_copy(
        update={
            "state_snapshot": request.state_snapshot.model_copy(
                update={"character_states": tuple(actors)}
            )
        }
    )
    assert execute(conflicting, loader).error.code == "item.charge_authority"
    wrong_turn = request.model_copy(
        update={
            "state_snapshot": request.state_snapshot.model_copy(
                update={
                    "combat_state": request.state_snapshot.combat_state.model_copy(
                        update={"current_turn_index": 1}
                    )
                }
            )
        }
    )
    assert execute(wrong_turn, loader).status == "rejected"
    for field in InventoryEntry.model_fields:
        value = request.model_dump(mode="python")
        del value["state_snapshot"]["inventory_state"]["entries"][0][field]
        with pytest.raises(ValidationError):
            RuleEvaluationRequest.model_validate(value)
    for update in [{"schema_version": "engine-evaluation/5"}, {"operation_kind": "combat.intent"}]:
        with pytest.raises(ValidationError):
            execute(request.model_copy(update=update), loader)
    from dnd5e_engine import evaluation_items

    original = evaluation_items.attack_delta
    before = deepcopy(request)

    def fail_after_resolution(old, new):
        original(old, new)
        raise EvaluationInvariantError("injected after item resolution")

    monkeypatch.setattr(evaluation_items, "attack_delta", fail_after_resolution)
    with pytest.raises(EvaluationInvariantError, match="after item resolution"):
        execute(request, loader)
    assert request == before


def test_successive_fresh_units_do_not_reuse_a_slug_charge_pool():
    request, _, _, loader = item_case(quantity=2)
    store = {"snapshot": request.state_snapshot, "rng": request.rng_context}
    first = execute(request, loader)
    apply_atomic(store, first)
    snapshot = store["snapshot"]
    actors = list(snapshot.character_states)
    actors[0] = actors[0].model_copy(
        update={"bonus_action_available": True, "bonus_action_taken_this_turn": False}
    )
    snapshot = snapshot.model_copy(update={"character_states": tuple(actors)})
    second = request.model_copy(
        update={"command_id": "next-turn", "state_snapshot": snapshot, "rng_context": store["rng"]}
    )
    result = execute(second, loader)
    assert result.status == "accepted"
    assert result.state_delta.operations[0].value.quantity == 0


@pytest.mark.parametrize("mutation", ["effect", "timing", "scaling", "charges", "attunement"])
def test_unmigrated_item_mechanics_fail_closed(mutation, monkeypatch):
    from dnd5e_srd_data.schema.common import ActivityTiming, AppliedEffectRef

    request, _, _, loader = item_case()
    item = loader.get_item(POTION)
    activity = item.activities[0]
    if mutation == "effect":
        activity = activity.model_copy(update={"effects": [AppliedEffectRef(id="unmigrated")]})
    elif mutation == "timing":
        activity = activity.model_copy(update={"timing": ActivityTiming(trigger="turn_end")})
    elif mutation == "scaling":
        activity = activity.model_copy(
            update={
                "healing": activity.healing.model_copy(
                    update={
                        "custom": activity.healing.custom.model_copy(
                            update={"enabled": True, "formula": "@mod"}
                        )
                    }
                )
            }
        )
    elif mutation == "charges":
        item = item.model_copy(update={"uses": item.uses.model_copy(update={"max": "@prof"})})
    else:
        item = item.model_copy(update={"requires_attunement": True})
    item = item.model_copy(update={"activities": [activity]})
    loader = MemoryAssetLoader(items=[item, loader.get_weapon("mace"), loader.get_weapon(WEAPON)])
    request = request.model_copy(update={"ruleset_binding": ruleset_binding(loader)})
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("RNG before support"))
    assert execute(request, loader).status == "unsupported"


def test_npc_owner_and_reordered_actor_snapshot():
    request, handle, live, loader = item_case()
    state = request.state_snapshot
    entry = state.inventory_state.entries[0].model_copy(update={"owner_id": FOE})
    state = state.model_copy(
        update={
            "inventory_state": InventoryState(entries=(entry,)),
            "character_states": tuple(reversed(state.character_states)),
            "combat_state": state.combat_state.model_copy(update={"current_turn_index": 1}),
        }
    )
    request = request.model_copy(
        update={
            "actor_id": FOE,
            "state_snapshot": state,
            "payload": request.payload.model_copy(update={"target_id": FOE}),
        }
    )
    live.current_turn_index = 1
    live.current_actor_id = FOE
    result = execute(request, loader)
    assert result.status == "accepted"
    intent = legacy_intent().model_copy(update={"target_id": FOE})
    asyncio.run(orch.submit_player_intent(handle, FOE, intent))
    assert result.proposed_events == tuple(live.event_log)
    assert result.rng_transition.next_state == RNGState.capture(live.rng)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("quantity", True),
        ("quantity", "1"),
        ("accessible", 1),
        ("charges_remaining_per_unit", "1"),
        ("quantity", -1),
    ],
)
def test_inventory_boundary_is_strict(field, bad):
    request, _, _, loader = item_case()
    value = request.model_dump(mode="python")
    value["state_snapshot"]["inventory_state"]["entries"][0][field] = bad
    with pytest.raises(ValidationError):
        RuleEvaluationRequest.model_validate(value)
    assert execute(request, loader).status == "accepted"


def test_consume_cannot_transfer_owner_change_identity_or_coerce_amount():
    request, _, _, loader = item_case(quantity=2)
    consume = execute(request, loader).state_delta.operations[0]
    for updates in [
        {"owner_id": FOE},
        {"instance_id": "other"},
        {"quantity": 0},
        {"item_slug": WEAPON},
        {"charges_remaining_per_unit": 0},
        {"accessible": False},
    ]:
        with pytest.raises(ValidationError):
            InventoryConsume.model_validate(
                consume.model_copy(update={"value": consume.value.model_copy(update=updates)})
            )
    with pytest.raises(ValidationError):
        InventoryConsume.model_validate(consume.model_copy(update={"amount": True}))
    store = {"snapshot": request.state_snapshot, "rng": request.rng_context}
    bad_entry = request.state_snapshot.inventory_state.entries[0].model_copy(update={"quantity": 3})
    store["snapshot"] = store["snapshot"].model_copy(
        update={"inventory_state": InventoryState(entries=(bad_entry,))}
    )
    before = deepcopy(store)
    with pytest.raises(AssertionError):
        apply_atomic(store, execute(request, loader))
    assert store == before


@pytest.mark.parametrize("fault", ["resolver", "charge"])
def test_execution_faults_after_real_payment_and_rng_do_not_leak(fault, monkeypatch):
    request, _, _, loader = item_case()
    before = deepcopy(request)
    if fault == "resolver":
        from dnd5e_engine import evaluation_items

        original = evaluation_items.resolve_activity

        def fail(activity, ctx, **kwargs):
            original(activity, ctx, **kwargs)
            assert ctx.caster.bonus_action_available is False
            raise RuntimeError("injected item resolver fault")

        monkeypatch.setattr(evaluation_items, "resolve_activity", fail)
        expected = RuntimeError
    else:
        from dnd5e_engine import evaluation_items

        original = evaluation_items.InventoryConsume

        def fail(**kwargs):
            operation = original(**kwargs)
            assert operation.amount == 1
            assert operation.value.quantity == operation.expected.quantity - 1
            raise EvaluationInvariantError("injected inventory delta fault after healing")

        monkeypatch.setattr(evaluation_items, "InventoryConsume", fail)
        expected = EvaluationInvariantError
    with pytest.raises(expected):
        execute(request, loader)
    assert request == before


def test_unsupported_snapshot_effects_before_rng(monkeypatch):
    from dnd5e_engine.evaluation_effects import effect_state
    from dnd5e_engine.types.effects import ActiveEffect

    request, _, _, loader = item_case()
    effect = effect_state(
        ActiveEffect(id="guidance", name="Guidance", target_id=HERO, origin="synthetic")
    )
    request = request.model_copy(
        update={
            "state_snapshot": request.state_snapshot.model_copy(update={"effect_states": (effect,)})
        }
    )
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("RNG before support"))
    assert execute(request, loader).status == "unsupported"


def test_only_bonus_action_is_required_and_last_resource_can_advance_turn():
    from dnd5e_engine.evaluation_contracts import RuleEvaluationResult
    from dnd5e_engine.evaluation_delta import StateDelta
    from tests.test_stateless_attack import apply_delta

    request, handle, live, loader = item_case()
    updates = {"action_available": False, "attacks_remaining": 0, "movement_remaining": 0}
    actors = list(request.state_snapshot.character_states)
    actors[0] = actors[0].model_copy(update=updates)
    request = request.model_copy(
        update={
            "state_snapshot": request.state_snapshot.model_copy(
                update={"character_states": tuple(actors)}
            )
        }
    )
    live.initiative[0] = live.initiative[0].model_copy(update=updates)
    result = execute(request, loader)
    assert result.status == "accepted"
    asyncio.run(orch.submit_player_intent(handle, HERO, legacy_intent()))
    assert result.proposed_events == tuple(live.event_log)
    assert result.rng_transition.next_state == RNGState.capture(live.rng)
    del live.custom_counters_by_entity[HERO][orch._item_use_counter_key(POTION)]
    from dnd5e_engine.specs import GridScene

    view = request.state_snapshot
    expected = capture_evaluation_snapshot(
        live, view, GridScene.model_validate(view.scene_state.grid.model_dump())
    )
    combat_result = RuleEvaluationResult.model_validate(
        result.model_copy(
            update={
                "state_delta": StateDelta(
                    expected_world_version=view.world_version,
                    operations=tuple(
                        op
                        for op in result.state_delta.operations
                        if not isinstance(op, InventoryConsume)
                    ),
                )
            }
        )
    )
    assert apply_delta(view, combat_result) == expected
    assert expected.combat_state.current_turn_index == 1


@pytest.mark.parametrize("charged", [False, True])
def test_shared_bonus_activation_is_not_inferred_from_item_name(charged):
    from dnd5e_engine.lib_loader import scoped_lib_loader

    _, handle, live, loader = item_case()
    item = loader.get_item(POTION).model_copy(update={"slug": "synthetic-healing-draught"})
    if not charged:
        activity = item.activities[0].model_copy(
            update={
                "consumption": item.activities[0].consumption.model_copy(update={"targets": []})
            }
        )
        item = item.model_copy(update={"uses": None, "activities": [activity]})
    loader = MemoryAssetLoader(items=[item, loader.get_weapon("mace"), loader.get_weapon(WEAPON)])
    live.ruleset_loader = loader
    intent = legacy_intent().model_copy(update={"item_id": item.slug, "activity_id": None})
    with scoped_lib_loader(loader):
        asyncio.run(orch.submit_player_intent(handle, HERO, intent))
    actor = orch._find_combatant(live, HERO)
    assert actor.action_available is True
    assert actor.bonus_action_available is False
    assert actor.hp_current == 15


def test_result_version_and_rng_guard_are_atomic():
    request, _, _, loader = item_case()
    result = execute(request, loader)
    with pytest.raises(ValidationError):
        RuleEvaluationResult.model_validate(
            result.model_copy(update={"schema_version": "engine-evaluation/5"})
        )
    store = {
        "snapshot": request.state_snapshot,
        "rng": request.rng_context.model_copy(update={"version": request.rng_context.version + 1}),
    }
    before = deepcopy(store)
    with pytest.raises(ValueError, match="RNG transition"):
        apply_atomic(store, result)
    assert store == before
