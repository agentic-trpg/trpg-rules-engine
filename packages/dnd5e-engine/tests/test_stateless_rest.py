"""R16 real Short Rest resolver, explicit resources and atomic candidate consumers."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from pydantic import ValidationError

from dnd5e_engine import evaluation_rest
from dnd5e_engine.evaluation_contracts import (
    RestPayload,
    RuleEvaluationRequest,
    RuleEvaluationResult,
)
from dnd5e_engine.evaluation_delta import HPDelta, ResourceUpdate
from dnd5e_engine.evaluation_resources import (
    FeatureResource,
    HitDiceResource,
    HitDieSpend,
    ResourceState,
    SlotResource,
)
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_engine.evaluation_state import NonCombatSnapshot
from dnd5e_engine.events import HealingApplied
from dnd5e_engine.rest import HitDicePool, resolve_short_rest
from tests.evaluation_support import HERO, synthetic_loader
from tests.test_stateless_attack import execute, request_and_live
from tests.test_stateless_item_closure_core import isolate


def rest_case(*, hp=10, con=14, seed=0, spends=None):
    request, _, _ = request_and_live(seed=seed)
    canonical, source = BundledAssetLoader(), synthetic_loader()
    loader = MemoryAssetLoader(
        items=[source.get_item(s) for s in source.list_slugs("items")],
        classes=[canonical.get_class(s) for s in ("fighter", "warlock", "wizard")],
    )
    pools = [
        HitDiceResource(
            kind="hit_dice",
            owner_id=HERO,
            pool_id="hd:" + slug,
            current=1,
            maximum=1,
            class_slug=slug,
            die_size=size,
        )
        for slug, size in (("fighter", 10), ("warlock", 8), ("wizard", 6))
    ]
    pools += [
        SlotResource(
            kind="spell_slot", owner_id=HERO, pool_id="spell:1", current=1, maximum=2, level=1
        ),
        SlotResource(
            kind="pact_slot", owner_id=HERO, pool_id="pact:1", current=0, maximum=1, level=1
        ),
    ]
    resources = ResourceState(pools=tuple(pools))
    actors = list(request.state_snapshot.character_states)
    actors[0] = actors[0].model_copy(
        update={
            "hp_current": hp,
            "constitution": con,
            "character_level": 3,
            "classes": {"fighter": 1, "warlock": 1, "wizard": 1},
            "class_slug": None,
            "granted_features": (),
            "spell_slots": {1: 1},
            "pact_slots": {1: 0},
        }
    )
    snapshot = NonCombatSnapshot(
        snapshot_kind="non_combat",
        combat_setup=None,
        resource_state=resources,
        character_states=tuple(actors),
        **request.state_snapshot.model_dump(
            exclude={"snapshot_kind", "combat_state", "character_states", "resource_state"}
        ),
    )
    return request.model_copy(
        update={
            "schema_version": "engine-evaluation/12",
            "operation_kind": "rules.rest",
            "state_snapshot": snapshot,
            "ruleset_binding": ruleset_binding(loader),
            "payload": RestPayload(
                kind="rules.rest",
                target_id=HERO,
                rest_type="short",
                hit_dice=tuple(
                    HitDieSpend(pool_id=pid, amount=amount)
                    for pid, amount in (
                        spends if spends is not None else [("hd:fighter", 1), ("hd:warlock", 1)]
                    )
                ),
            ),
        }
    ), loader


def apply_rest(snapshot, result, *, world_version=None):
    """Independent expected-value consumer; validate all writes before publishing."""
    assert result.state_delta.expected_world_version == (
        snapshot.world_version if world_version is None else world_version
    )
    value = snapshot.model_dump(mode="python")
    actors = {a["entity_id"]: a for a in value["character_states"]}
    pools = {(p.owner_id, p.pool_id): p for p in snapshot.resource_state.pools}
    for op in result.state_delta.operations:
        if isinstance(op, ResourceUpdate):
            assert pools[(op.owner_id, op.pool_id)] == op.expected
        elif isinstance(op, HPDelta):
            assert actors[op.target_id]["hp_current"] == op.expected_hp
        else:
            raise TypeError(type(op))
    for op in result.state_delta.operations:
        if isinstance(op, ResourceUpdate):
            pools[(op.owner_id, op.pool_id)] = op.value
            if isinstance(op.value, FeatureResource):
                actors[op.owner_id]["custom_counters"]["feature_use:" + op.value.feature_slug] = {
                    "spent": op.value.maximum - op.value.current
                }
            if isinstance(op.value, SlotResource):
                field = "pact_slots" if op.value.kind == "pact_slot" else "spell_slots"
                actors[op.owner_id][field][op.value.level] = op.value.current
        else:
            actors[op.target_id]["hp_current"] = op.resulting_hp
    value["resource_state"] = ResourceState(pools=tuple(pools.values())).model_dump(mode="python")
    return NonCombatSnapshot.model_validate(value)


def test_real_short_rest_mixed_pools_matches_independent_values_and_legacy(monkeypatch):
    request, loader = rest_case()
    before = deepcopy(request)
    rng = request.rng_context.state.restore()
    old1 = resolve_short_rest(
        HitDicePool(10, 1, 1), 1, 2, rng=rng, pact_slots={1: 0}, pact_slot_max={1: 1}
    )
    old2 = resolve_short_rest(HitDicePool(8, 1, 1), 1, 2, rng=rng)
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request, loader)
        assert result == execute(request, loader)
    assert result.status == "accepted", result.error
    after = apply_rest(request.state_snapshot, result)
    assert old1.rolls == (9,)
    assert old2.rolls == (9,)
    assert after.character_states[0].hp_current == 28
    assert after.character_states[0].spell_slots == {1: 1}
    assert after.character_states[0].pact_slots == old1.pact_slots == {1: 1}
    assert [p.current for p in after.resource_state.pools] == [0, 0, 1, 1, 1]
    assert after.inventory_state == request.state_snapshot.inventory_state
    assert after.scene_state == request.state_snapshot.scene_state
    assert result.rng_transition.next_state == RNGState.capture(rng)
    assert result.proposed_events[0].hit_dice[0].healing_rolls == old1.rolls
    assert result.proposed_events[0].hit_dice[1].healing_rolls == old2.rolls
    assert result.proposed_events[0].hp_regained == 18
    assert result.proposed_events[1] == HealingApplied(target_id=HERO, amount=18)
    assert request == before
    assert RuleEvaluationRequest.model_validate_json(request.model_dump_json()) == request
    assert RuleEvaluationResult.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(AssertionError):
        apply_rest(after, result)
    with pytest.raises(AssertionError):
        apply_rest(request.state_snapshot, result, world_version=8)


@pytest.mark.parametrize(("hp", "con", "seed", "expected"), [(39, 14, 0, 40), (10, 1, 2, 12)])
def test_caps_and_per_die_minimum_still_spend_real_hit_dice(hp, con, seed, expected):
    request, loader = rest_case(hp=hp, con=con, seed=seed)
    result = execute(request, loader)
    after = apply_rest(request.state_snapshot, result)
    assert after.character_states[0].hp_current == expected
    assert [p.current for p in after.resource_state.pools][:2] == [0, 0]
    assert result.proposed_events[0].hp_regained == expected - hp


def test_pact_recovery_without_dice_has_explicit_unchanged_rng():
    request, loader = rest_case(spends=[])
    result = execute(request, loader)
    after = apply_rest(request.state_snapshot, result)
    assert after.character_states[0].hp_current == 10
    assert after.character_states[0].pact_slots == {1: 1}
    assert result.proposed_events[0].hit_dice == ()
    assert result.rng_transition.next_state == request.rng_context.state
    assert not result.rng_transition.state_changed


@pytest.mark.parametrize(
    "bad", ["long", "missing", "overspend", "foreign", "die", "class", "effect", "feature"]
)
def test_incomplete_dependencies_refuse_before_rng(monkeypatch, bad):
    request, loader = rest_case()
    snapshot = request.state_snapshot
    if bad == "long":
        request = request.model_copy(
            update={"payload": request.payload.model_copy(update={"rest_type": "long"})}
        )
    elif bad == "missing":
        snapshot = snapshot.model_copy(update={"resource_state": None})
    elif bad in {"overspend", "foreign"}:
        request = request.model_copy(
            update={
                "payload": request.payload.model_copy(
                    update={
                        "hit_dice": (
                            HitDieSpend(
                                pool_id="absent" if bad == "foreign" else "hd:fighter", amount=2
                            ),
                        )
                    }
                )
            }
        )
    elif bad == "die":
        pools = list(snapshot.resource_state.pools)
        pools[0] = pools[0].model_copy(update={"die_size": 6})
        snapshot = snapshot.model_copy(update={"resource_state": ResourceState(pools=tuple(pools))})
    else:
        actor = snapshot.character_states[0]
        if bad == "class":
            actor = actor.model_copy(update={"classes": {"fighter": 3}})
        elif bad == "effect":
            from dnd5e_engine.evaluation_effects import effect_state
            from dnd5e_engine.types.effects import ActiveEffect

            snapshot = snapshot.model_copy(
                update={
                    "effect_states": (
                        effect_state(
                            ActiveEffect(
                                id="active", name="Active", origin="approved:source", target_id=HERO
                            )
                        ),
                    )
                }
            )
        else:
            actor = actor.model_copy(
                update={"custom_counters": {"feature_use:second-wind": {"spent": 1}}}
            )
            snapshot = snapshot.model_copy(
                update={
                    "resource_state": ResourceState(
                        pools=(
                            *snapshot.resource_state.pools,
                            FeatureResource(
                                kind="feature_uses",
                                owner_id=HERO,
                                pool_id="feature:wind",
                                current=1,
                                maximum=2,
                                feature_slug="second-wind",
                            ),
                        )
                    )
                }
            )
        snapshot = snapshot.model_copy(
            update={"character_states": (actor, *snapshot.character_states[1:])}
        )
    request = request.model_copy(update={"state_snapshot": snapshot})
    monkeypatch.setattr(RNGState, "restore", lambda *a: pytest.fail("refusal restored RNG"))
    result = execute(request, loader)
    assert result.status == ("rejected" if bad in {"overspend", "foreign"} else "unsupported")
    assert result.state_delta is None
    assert result.proposed_events == ()
    assert result.rng_transition is None


@pytest.mark.parametrize("fault", [RuntimeError, asyncio.CancelledError])
def test_real_rest_post_roll_fault_cannot_publish_partial_hp_resource_rng(monkeypatch, fault):
    request, loader = rest_case()
    before = deepcopy(request)
    original = evaluation_rest.resolve_short_rest

    def fail(*args, **kwargs):
        outcome = original(*args, **kwargs)
        assert outcome.rolls == (9,)
        raise fault("post-rest-roll")

    monkeypatch.setattr(evaluation_rest, "resolve_short_rest", fail)
    isolate(monkeypatch)
    with pytest.raises(fault):
        execute(request, loader)
    assert request == before


def test_last_unit_new_request_rejected_and_same_snapshot_concurrent_replay(monkeypatch):
    request, loader = rest_case()
    isolate(monkeypatch)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: execute(request, loader), range(8)))
    assert all(r == results[0] for r in results)
    after = apply_rest(request.state_snapshot, results[0])
    following = request.model_copy(
        update={
            "state_snapshot": after,
            "command_id": "rest:next",
            "rng_context": request.rng_context.model_copy(
                update={"state": results[0].rng_transition.next_state}
            ),
        }
    )
    result = execute(following, loader)
    assert result.status == "rejected"
    assert result.error.code == "rest.insufficient"
    assert result.rng_transition is None


@pytest.mark.parametrize("bad", ["owner", "maximum", "current", "slot", "duplicate", "missing_max"])
def test_resource_schema_closure_and_expected_values(bad):
    request, _ = rest_case()
    value = request.model_dump(mode="python")
    pools = value["state_snapshot"]["resource_state"]["pools"]
    if bad == "owner":
        pools[0]["owner_id"] = "absent"
    elif bad == "maximum":
        pools[0]["maximum"] = 0
    elif bad == "current":
        pools[0]["current"] = -1
    elif bad == "slot":
        pools[-1]["current"] = 1
    elif bad == "duplicate":
        value["state_snapshot"]["resource_state"]["pools"] = pools + pools[:1]
    else:
        del pools[0]["maximum"]
    with pytest.raises(ValidationError):
        RuleEvaluationRequest.model_validate(value)


@pytest.mark.parametrize("bad", ["owner", "capacity", "arithmetic", "identity"])
def test_closed_resource_delta_cannot_change_identity_or_capacity(bad):
    request, loader = rest_case()
    result = execute(request, loader)
    value = result.state_delta.operations[0].model_dump(mode="python")
    if bad == "owner":
        value["owner_id"] = "absent"
    elif bad == "capacity":
        value["value"]["maximum"] = 2
    elif bad == "arithmetic":
        value["amount"] = -2
    else:
        value["value"]["pool_id"] = "other"
    with pytest.raises(ValidationError):
        ResourceUpdate.model_validate(value)


@pytest.mark.parametrize("current", [0, 1, 2])
def test_real_srd_fighter_second_wind_short_rest_recovery(monkeypatch, current):
    from dnd5e_engine.rest import recover_feature_uses

    request, loader = rest_case(spends=[("hd:fighter", 1)])
    canonical = BundledAssetLoader()
    feature = canonical.get_feature("second-wind")
    loader = MemoryAssetLoader(
        items=[loader.get_item(s) for s in loader.list_slugs("items")],
        classes=[loader.get_class("fighter")],
        features=[feature],
    )
    snapshot = request.state_snapshot
    actor = snapshot.character_states[0].model_copy(
        update={
            "classes": {"fighter": 1},
            "character_level": 1,
            "spell_slots": {},
            "pact_slots": {},
            "granted_features": ("second-wind",),
            "custom_counters": {"feature_use:second-wind": {"spent": 2 - current}},
        }
    )
    resources = ResourceState(
        pools=(
            snapshot.resource_state.pools[0],
            FeatureResource(
                kind="feature_uses",
                owner_id=HERO,
                pool_id="feature:second-wind",
                feature_slug="second-wind",
                current=current,
                maximum=2,
            ),
        )
    )
    snapshot = snapshot.model_copy(
        update={
            "resource_state": resources,
            "character_states": (actor, *snapshot.character_states[1:]),
        }
    )
    request = request.model_copy(
        update={"state_snapshot": snapshot, "ruleset_binding": ruleset_binding(loader)}
    )
    isolate(monkeypatch)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    after = apply_rest(snapshot, result)
    old = recover_feature_uses(actor.custom_counters, "sr", {"second-wind": feature.uses.recovery})
    remaining = min(2, current + 1)
    assert after.resource_state.pools[1].current == remaining
    assert after.character_states[0].custom_counters == {
        "feature_use:second-wind": {"spent": old["second-wind"]}
    }
    assert old["second-wind"] == 2 - remaining
    assert after.character_states[0].hp_current == 19
    assert result.proposed_events[0].feature_uses_restored == {"second-wind": remaining - current}


@pytest.mark.parametrize("fault", [RuntimeError, asyncio.CancelledError])
def test_post_resource_hp_projection_fault_is_atomic(monkeypatch, fault):
    request, loader = rest_case()
    before = deepcopy(request)

    def fail(*args, **kwargs):
        raise fault("post resource and HP computation")

    monkeypatch.setattr(evaluation_rest.RNGTransition, "between", fail)
    with pytest.raises(fault):
        execute(request, loader)
    assert request == before


def test_resource_component_changes_cannot_be_silently_lost_by_attack_projection():
    from dnd5e_engine.evaluation_projection import EvaluationInvariantError, attack_delta

    request, _, _ = request_and_live()
    snapshot = request.state_snapshot
    after = snapshot.model_copy(update={"resource_state": ResourceState(pools=())})
    with pytest.raises(EvaluationInvariantError, match="unrepresented snapshot"):
        attack_delta(snapshot, after)
