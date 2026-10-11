"""R19 actual rest/payment algorithms and independent atomic mirror checks."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from pydantic import ValidationError

from dnd5e_engine import evaluation_rest
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.activities.actor_stats import _SCORE_ATTR
from dnd5e_engine.activities.context import ActivityResolutionContext
from dnd5e_engine.activities.scale import build_scale_values
from dnd5e_engine.evaluation_actor import combatant
from dnd5e_engine.evaluation_contracts import RuleEvaluationResult
from dnd5e_engine.evaluation_payments import feature_payments, spell_payment
from dnd5e_engine.evaluation_resources import FeatureResource, ResourceState, SlotResource
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import RulesetBindingError, ruleset_binding
from dnd5e_engine.feature_runtime import (
    DrawFreeRandom,
    FeatureInvocation,
    ResourcePayment,
    preflight_feature,
)
from dnd5e_engine.resource_rules import consume_resource
from dnd5e_engine.rest import HitDicePool, resolve_long_rest
from tests.evaluation_support import HERO
from tests.test_stateless_attack import execute, request_and_live
from tests.test_stateless_item_closure_core import isolate
from tests.test_stateless_rest import apply_rest, rest_case


def long_case(*, hp=10):
    request, old_loader = rest_case(hp=hp, spends=[])
    canonical = BundledAssetLoader()
    grants = ("second-wind", "spellcasting", "ritual-adept", "arcane-recovery")
    loader = MemoryAssetLoader(
        items=[old_loader.get_item(s) for s in old_loader.list_slugs("items")],
        classes=[canonical.get_class(s) for s in ("fighter", "warlock", "wizard")],
        features=[canonical.get_feature(s) for s in grants],
    )
    pools = [
        p.model_copy(update={"current": 0}) for p in request.state_snapshot.resource_state.pools
    ]
    pools += [
        FeatureResource(
            kind="feature_uses",
            owner_id=HERO,
            pool_id="feature:" + slug,
            current=0,
            maximum=maximum,
            feature_slug=slug,
        )
        for slug, maximum in (("second-wind", 2), ("arcane-recovery", 1))
    ]
    actor = request.state_snapshot.character_states[0].model_copy(
        update={
            "granted_features": grants,
            "spell_slots": {1: 0},
            "pact_slots": {1: 0},
            "custom_counters": {
                "feature_use:second-wind": {"spent": 2},
                "feature_use:arcane-recovery": {"spent": 1},
            },
        }
    )
    snapshot = request.state_snapshot.model_copy(
        update={
            "resource_state": ResourceState(pools=tuple(pools)),
            "character_states": (actor, *request.state_snapshot.character_states[1:]),
        }
    )
    return request.model_copy(
        update={
            "payload": request.payload.model_copy(update={"rest_type": "long", "hit_dice": ()}),
            "state_snapshot": snapshot,
            "ruleset_binding": ruleset_binding(loader),
        }
    ), loader


@pytest.mark.parametrize("npc", [False, True])
@pytest.mark.parametrize("hp", [1, 10, 40])
def test_real_class_long_rest_full_recovery_no_rng_and_original_resolver_parity(
    monkeypatch, hp, npc
):
    request, loader = long_case(hp=hp)
    if npc:
        actors = request.state_snapshot.character_states
        request = request.model_copy(
            update={
                "state_snapshot": request.state_snapshot.model_copy(
                    update={
                        "character_states": (
                            actors[0].model_copy(update={"entity_type": "NPC"}),
                            *actors[1:],
                        )
                    }
                )
            }
        )
    before = deepcopy(request)
    with monkeypatch.context() as scoped:
        isolate(scoped)
        scoped.setattr(
            RNGState, "restore", lambda *a: pytest.fail("Long Rest restored execution RNG")
        )
        result = execute(request, loader)
    assert result.status == "accepted", result.error
    after = apply_rest(request.state_snapshot, result)
    actor = after.character_states[0]
    assert (actor.hp_current, actor.hp_max) == (40, 40)
    assert actor.spell_slots == {1: 2}
    assert actor.pact_slots == {1: 1}
    assert actor.custom_counters == {
        "feature_use:second-wind": {"spent": 0},
        "feature_use:arcane-recovery": {"spent": 0},
    }
    assert all(p.current == p.maximum for p in after.resource_state.pools)
    assert result.rng_transition.input_state == result.rng_transition.next_state
    assert result.rng_transition.state_changed is False
    event = result.proposed_events[0]
    assert event.type == "rest_resolved"
    assert event.rest_type == "long"
    assert event.hp_regained == 40 - hp
    assert event.hit_dice_restored == {"hd:fighter": 1, "hd:warlock": 1, "hd:wizard": 1}
    assert event.spell_slots_restored == {1: 2}
    assert event.pact_slots_restored == {1: 1}
    assert event.feature_uses_restored == {"second-wind": 2, "arcane-recovery": 1}
    assert [e.type for e in result.proposed_events] == (
        ["rest_resolved", "healing_applied"] if hp < 40 else ["rest_resolved"]
    )
    old = resolve_long_rest(
        HitDicePool(10, 0, 1),
        hp,
        40,
        spell_slots={1: 0},
        spell_slot_max={1: 2},
        pact_slots={1: 0},
        pact_slot_max={1: 1},
    )
    assert (old.hp_current, old.dice_remaining, old.spell_slots, old.pact_slots) == (
        actor.hp_current,
        1,
        actor.spell_slots,
        actor.pact_slots,
    )
    assert after.inventory_state == request.state_snapshot.inventory_state
    assert after.scene_state == request.state_snapshot.scene_state
    assert after.character_states[1:] == request.state_snapshot.character_states[1:]
    assert request == before
    assert RuleEvaluationResult.model_validate_json(result.model_dump_json()) == result


def payment_case():
    request, loader = long_case()
    recovered = apply_rest(request.state_snapshot, execute(request, loader))
    return recovered, loader


def test_actual_slot_payment_preference_last_unit_and_legacy_shared_kernel():
    snapshot, _ = payment_case()
    resources = snapshot.resource_state
    _, _, live = request_and_live()
    live.spell_slots_by_entity[HERO] = {1: 2}
    live.pact_slots_by_entity[HERO] = {1: 1}
    for expected_kind, expected_remaining in (
        ("spell_slot", 1),
        ("spell_slot", 0),
        ("pact_slot", 0),
    ):
        op = spell_payment(resources, HERO, 1)
        assert (op.value.kind, op.value.current, op.amount) == (
            expected_kind,
            expected_remaining,
            -1,
        )
        assert orch._take_spell_slot(live, HERO, 1) is True
        old_balance = (
            live.spell_slots_by_entity
            if expected_kind == "spell_slot"
            else live.pact_slots_by_entity
        )
        assert old_balance[HERO][1] == expected_remaining
        resources = ResourceState(
            pools=tuple(
                op.value if p.pool_id == op.pool_id and p.owner_id == HERO else p
                for p in resources.pools
            )
        )
    assert live.expended_resources == {HERO: {"spell_slot:1": 2, "pact_slot:1": 1}}
    before = deepcopy(resources)
    with pytest.raises(ValueError, match="no owned slot"):
        spell_payment(resources, HERO, 1)
    assert orch._take_spell_slot(live, HERO, 1) is False
    assert resources == before


def test_real_second_wind_preflight_prices_owned_feature_payment():
    snapshot, loader = payment_case()
    actor = snapshot.character_states[0]
    feature = loader.get_feature("second-wind")
    ctx = ActivityResolutionContext(
        caster=combatant(actor),
        caster_abilities={ab: getattr(actor, field) for ab, field in _SCORE_ATTR.items()},
        targets=[combatant(actor)],
        rng=DrawFreeRandom(0),
        event_emitter=lambda _: None,
        class_levels=actor.classes,
        scale_values=build_scale_values(
            class_slug=actor.class_slug,
            subclass_slug=None,
            species_slug=None,
            level=actor.character_level,
            classes=actor.classes,
            loader=loader,
        ),
    )
    invocation = preflight_feature(
        feature,
        FeatureInvocation(
            activities=[feature.activities[0]], passive_effects=[], is_bonus_action=True
        ),
        ctx,
        loader=loader,
        repertoire=actor.granted_features,
        spent={"second-wind": 0},
    )
    assert invocation.payments == (ResourcePayment("second-wind", 1, 2),)
    [op] = feature_payments(snapshot.resource_state, HERO, invocation.payments)
    assert (op.expected.current, op.value.current, op.amount) == (2, 1, -1)
    result = execute(long_case()[0], loader)
    result = result.model_copy(
        update={"state_delta": result.state_delta.model_copy(update={"operations": (op,)})}
    )
    after = apply_rest(snapshot, result)
    assert after.character_states[0].custom_counters["feature_use:second-wind"] == {"spent": 1}
    assert after.resource_state.pools[-2].current == 1
    with pytest.raises(AssertionError):
        apply_rest(after, result)


@pytest.mark.parametrize("bad", ["owner", "maximum", "cost", "duplicate", "missing", "exhausted"])
def test_feature_payment_fails_atomically_for_invalid_identity_cost_or_balance(bad):
    snapshot, _ = payment_case()
    resources = snapshot.resource_state
    payments = (ResourcePayment("second-wind", 1, 2),)
    owner = HERO
    if bad == "owner":
        owner = "npc:foreign"
    elif bad == "maximum":
        payments = (ResourcePayment("second-wind", 1, 3),)
    elif bad == "cost":
        payments = (ResourcePayment("second-wind", 0, 2),)
    elif bad == "duplicate":
        payments *= 2
    elif bad == "missing":
        payments = (ResourcePayment("unknown", 1, 2),)
    else:
        resources = ResourceState(
            pools=tuple(
                p.model_copy(update={"current": 0}) if isinstance(p, FeatureResource) else p
                for p in resources.pools
            )
        )
    before = deepcopy(resources)
    with pytest.raises(ValueError):
        feature_payments(resources, owner, payments)
    assert resources == before


@pytest.mark.parametrize(
    "bad",
    ["spell_max", "feature_max", "owner", "definition", "species", "counter", "effect", "missing"],
)
def test_long_rest_dependency_failures_before_rng(monkeypatch, bad):
    request, loader = long_case()
    snap = request.state_snapshot
    actor = snap.character_states[0]
    if bad in {"spell_max", "feature_max"}:
        pools = list(snap.resource_state.pools)
        index = 3 if bad == "spell_max" else 5
        pools[index] = pools[index].model_copy(update={"maximum": pools[index].maximum + 1})
        if bad == "feature_max":
            actor = actor.model_copy(
                update={
                    "custom_counters": {
                        **actor.custom_counters,
                        "feature_use:second-wind": {"spent": 3},
                    }
                }
            )
        snap = snap.model_copy(update={"resource_state": ResourceState(pools=tuple(pools))})
    elif bad == "owner":
        actor = actor.model_copy(
            update={"granted_features": ("ritual-adept",), "classes": {"fighter": 1, "warlock": 2}}
        )
    elif bad == "definition":
        canonical = BundledAssetLoader()
        loader = MemoryAssetLoader(
            items=[loader.get_item(s) for s in loader.list_slugs("items")],
            classes=[loader.get_class(s) for s in loader.list_slugs("classes")],
            features=[
                canonical.get_feature(s).model_copy(update={"name": "changed"})
                if s == "ritual-adept"
                else canonical.get_feature(s)
                for s in actor.granted_features
            ],
        )
        request = request.model_copy(update={"ruleset_binding": ruleset_binding(loader)})
    elif bad == "species":
        actor = actor.model_copy(update={"species_slug": "human"})
    elif bad == "counter":
        actor = actor.model_copy(
            update={"custom_counters": {**actor.custom_counters, "item_use:wand": {"spent": 1}}}
        )
    elif bad == "effect":
        actor = actor.model_copy(update={"concentration_effect_id": "existing"})
    else:
        snap = snap.model_copy(update={"resource_state": None})
    snap = snap.model_copy(update={"character_states": (actor, *snap.character_states[1:])})
    request = request.model_copy(update={"state_snapshot": snap})
    monkeypatch.setattr(RNGState, "restore", lambda *a: pytest.fail("restored RNG"))
    assert execute(request, loader).status == "unsupported"


def test_mirror_mismatch_is_schema_failure_before_any_rule_execution():
    request, loader = long_case()
    actors = request.state_snapshot.character_states
    changed = actors[0].model_copy(update={"spell_slots": {1: 1}})
    request = request.model_copy(
        update={
            "state_snapshot": request.state_snapshot.model_copy(
                update={"character_states": (changed, *actors[1:])}
            )
        }
    )
    with pytest.raises(ValidationError, match="contradicts actor balance"):
        execute(request, loader)


@pytest.mark.parametrize("cancel", [False, True])
def test_after_recovery_exception_or_cancel_discards_all_proposals(monkeypatch, cancel):
    import asyncio

    request, loader = long_case()
    before = deepcopy(request)
    original = evaluation_rest.resolve_long_rest

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        if cancel:
            raise asyncio.CancelledError()
        raise RuntimeError("after recovery")

    monkeypatch.setattr(evaluation_rest, "resolve_long_rest", fail)
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        execute(request, loader)
    assert request == before


def test_replay_concurrent_requests_conflicts_and_zero_changes():
    request, loader = long_case()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: execute(request, loader), range(8)))
    assert all(r == results[0] for r in results)
    with pytest.raises(AssertionError):
        apply_rest(request.state_snapshot, results[0], world_version=8)
    after = apply_rest(request.state_snapshot, results[0])
    with pytest.raises(AssertionError):
        apply_rest(after, results[0])
    unchanged = execute(request.model_copy(update={"state_snapshot": after}), loader)
    assert unchanged.status == "accepted"
    assert unchanged.state_delta.operations == ()
    assert unchanged.proposed_events[0].hp_regained == 0
    assert unchanged.rng_transition.state_changed is False
    with pytest.raises(RulesetBindingError):
        execute(
            request.model_copy(
                update={
                    "ruleset_binding": request.ruleset_binding.model_copy(
                        update={"data_revision": "sha256:" + "0" * 64}
                    )
                }
            ),
            loader,
        )


@pytest.mark.parametrize("current,cost", [(-1, 1), (0, 1), (2, 0), (1, 2)])
def test_shared_payment_rejects_invalid_balance_or_cost(current, cost):
    with pytest.raises(ValueError):
        consume_resource(current, cost)


def test_foreign_resource_balances_are_preserved_and_never_used_for_payment():
    from tests.evaluation_support import FOE

    request, loader = long_case()
    snapshot = request.state_snapshot
    foreign = SlotResource(
        kind="spell_slot", owner_id=FOE, pool_id="foreign:spell", current=3, maximum=3, level=1
    )
    actors = snapshot.character_states
    snapshot = snapshot.model_copy(
        update={
            "resource_state": ResourceState(pools=(*snapshot.resource_state.pools, foreign)),
            "character_states": (actors[0], actors[1].model_copy(update={"spell_slots": {1: 3}})),
        }
    )
    request = request.model_copy(update={"state_snapshot": snapshot})
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    after = apply_rest(snapshot, result)
    assert after.resource_state.pools[-1] == foreign
    assert after.character_states[1] == snapshot.character_states[1]
    # HERO has no slots before Rest; FOE's available balance cannot fund HERO.
    with pytest.raises(ValueError, match="no owned slot"):
        spell_payment(snapshot.resource_state, HERO, 1)


@pytest.mark.parametrize(
    "item_slug,accepted", [("potion-of-healing", True), ("wand-of-magic-missiles", False)]
)
def test_actual_owned_item_recovery_cannot_be_hidden_outside_carried_slug_list(
    monkeypatch, item_slug, accepted
):
    from dnd5e_engine.evaluation_state import InventoryEntry, InventoryState

    request, old = long_case()
    item = BundledAssetLoader().get_item(item_slug)
    loader = MemoryAssetLoader(
        items=[old.get_item(s) for s in old.list_slugs("items")] + [item],
        classes=[old.get_class(s) for s in old.list_slugs("classes")],
        features=[old.get_feature(s) for s in old.list_slugs("features")],
    )
    inventory = InventoryState(
        entries=(
            InventoryEntry(
                instance_id="owned:charged",
                owner_id=HERO,
                item_slug=item_slug,
                quantity=1,
                charges_remaining_per_unit=0,
                accessible=True,
            ),
        )
    )
    request = request.model_copy(
        update={
            "state_snapshot": request.state_snapshot.model_copy(
                update={"inventory_state": inventory}
            ),
            "ruleset_binding": ruleset_binding(loader),
        }
    )
    monkeypatch.setattr(RNGState, "restore", lambda *a: pytest.fail("restored RNG"))
    result = execute(request, loader)
    assert result.status == ("accepted" if accepted else "unsupported"), result.error
    if accepted:
        assert apply_rest(request.state_snapshot, result).inventory_state == inventory
    else:
        assert result.state_delta is None


def test_last_feature_unit_payment_and_exhaustion():
    snapshot, _ = payment_case()
    resources = snapshot.resource_state
    for remaining in (1, 0):
        [op] = feature_payments(resources, HERO, (ResourcePayment("second-wind", 1, 2),))
        assert op.value.current == remaining
        resources = ResourceState(
            pools=tuple(
                op.value if p.pool_id == op.pool_id and p.owner_id == HERO else p
                for p in resources.pools
            )
        )
    with pytest.raises(ValueError):
        feature_payments(resources, HERO, (ResourcePayment("second-wind", 1, 2),))


def test_partial_delta_construction_failure_keeps_input_unchanged(monkeypatch):
    request, loader = long_case()
    before = deepcopy(request)
    original = evaluation_rest.ResourceUpdate
    calls = 0

    def fail(*args, **kwargs):
        nonlocal calls
        calls += 1
        value = original(*args, **kwargs)
        if calls == 2:
            raise RuntimeError("after two resource proposals")
        return value

    monkeypatch.setattr(evaluation_rest, "ResourceUpdate", fail)
    with pytest.raises(RuntimeError, match="two resource proposals"):
        execute(request, loader)
    assert request == before


@pytest.mark.parametrize("component", ["request", "snapshot"])
def test_pre_r19_contract_versions_fail_as_schema_errors_before_rng(monkeypatch, component):
    request, loader = long_case()
    if component == "request":
        request = request.model_copy(update={"schema_version": "engine-evaluation/14"})
    else:
        request = request.model_copy(
            update={
                "state_snapshot": request.state_snapshot.model_copy(
                    update={"snapshot_schema_version": "engine-snapshot/9"}
                )
            }
        )
    monkeypatch.setattr(RNGState, "restore", lambda *a: pytest.fail("restored RNG"))
    with pytest.raises(ValidationError):
        execute(request, loader)


def test_exhaustion_recovery_requires_its_own_complete_condition_delta(monkeypatch):
    from dnd5e_engine.evaluation_state import ConditionRecord

    request, loader = long_case()
    actors = request.state_snapshot.character_states
    condition = ConditionRecord(
        condition="exhaustion",
        source_entity_id="condition:srd",
        scope="session",
        duration_rounds=None,
        save_dc=None,
        applied_round=0,
        exhaustion_level=2,
        source_effect_id=None,
    )
    actor = actors[0].model_copy(update={"conditions": [condition]})
    request = request.model_copy(
        update={
            "state_snapshot": request.state_snapshot.model_copy(
                update={"character_states": (actor, *actors[1:])}
            )
        }
    )
    monkeypatch.setattr(RNGState, "restore", lambda *a: pytest.fail("restored RNG"))
    result = execute(request, loader)
    assert result.status == "unsupported"
    assert result.state_delta is None
