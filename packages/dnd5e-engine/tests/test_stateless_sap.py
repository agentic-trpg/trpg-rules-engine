"""Real equipped Fighter Sap: independent rules, Legacy and atomic composition."""

import asyncio
import random
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader

from dnd5e_engine import evaluation_attack
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, RuleEvaluationResult
from dnd5e_engine.evaluation_resources import FeatureResource, ResourceState
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_engine.evaluation_snapshot import capture_combat_snapshot
from dnd5e_engine.evaluation_state import InventoryEntry, InventoryState
from dnd5e_engine.events import AttackRolled, DamageApplied
from dnd5e_engine.specs import GridScene
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import execute
from tests.test_stateless_equipped_class_attack import equipped_case
from tests.test_stateless_item_closure_core import isolate
from tests.test_stateless_spell_feature import apply_healing


def sap_case(*, seed=0, npc=False, ac=10, immune=False, shield=True):
    request, old, handle, live = equipped_case(seed=seed, npc=npc, ac=ac, shield=shield)
    canonical = BundledAssetLoader()
    loader = MemoryAssetLoader(
        feats=[canonical.get_feat("defense")],
        classes=[old.get_class(s) for s in old.list_slugs("classes")],
        features=[old.get_feature(s) for s in old.list_slugs("features")],
        items=[
            canonical.get_item(s) for s in ("mace", "flail", "morningstar", "chain-shirt", "shield")
        ],
    )
    grants = tuple(
        g.slug
        for g in canonical.get_class("fighter").granted_features
        if g.level <= 1 and g.ref_type == "feature"
    )
    hero, foe = live.initiative
    hero = hero.model_copy(
        update={
            "class_slug": "fighter",
            "classes": {"fighter": 1},
            "granted_features": grants,
            "spellcasting_ability": None,
            "spell_slots": {},
            "spells_known": [],
            "save_proficiencies": ["str", "con"],
            "fighting_styles": ("defense",),
            "weapon_mastery_slugs": ("mace", "flail", "morningstar"),
            "custom_counters": {"feature_use:second-wind": {"spent": 0}},
            "ac": 18 if shield else 16,
        }
    )
    foe = foe.model_copy(
        update={
            "carried_item_slugs": ("mace",),
            "weapon_in_hands": "mace",
            "weapon_grip": "one_handed",
            "attack_bonus": 5,
            "damage_immunities": ["bludgeoning"] if immune else [],
        }
    )
    live.initiative = [hero, foe]
    live.ruleset_loader = loader
    live.spell_slots_by_entity[HERO] = {}
    live.spells_known_by_entity[HERO] = []
    live.custom_counters_by_entity[HERO] = deepcopy(hero.custom_counters)
    resources = ResourceState(
        pools=(
            FeatureResource(
                kind="feature_uses",
                owner_id=HERO,
                pool_id="second-wind",
                current=2,
                maximum=2,
                feature_slug="second-wind",
            ),
        )
    )
    inventory = InventoryState(
        entries=(
            *request.state_snapshot.inventory_state.entries,
            InventoryEntry(
                instance_id="foe:mace",
                owner_id=FOE,
                item_slug="mace",
                quantity=1,
                charges_remaining_per_unit=0,
                accessible=True,
            ),
        )
    )
    snapshot = capture_combat_snapshot(
        live,
        grid=GridScene(width=3, height=3),
        world_version=7,
        combat_id=request.state_snapshot.combat_state.combat_id,
        inventory_state=inventory,
        resource_state=resources,
        casting_state=None,
    )
    request = request.model_copy(
        update={"state_snapshot": snapshot, "ruleset_binding": ruleset_binding(loader)}
    )
    return request, loader, handle, live


@pytest.mark.parametrize("npc", [False, True])
@pytest.mark.parametrize("seed,ac", [(0, 10), (1, 99), (5, 10), (31, 10)])
@pytest.mark.parametrize("immune", [False, True])
def test_sap_hit_miss_crit_zero_damage_independent_and_legacy(npc, seed, ac, immune, monkeypatch):
    request, loader, handle, live = sap_case(npc=npc, seed=seed, ac=ac, immune=immune)
    before = deepcopy(request)
    asyncio.run(orch.submit_player_intent(handle, HERO, request.payload))
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request, loader)
    assert result.status == "accepted", result.error
    after = apply_sap(request.state_snapshot, result)
    assert after == legacy_snapshot(live, after)
    assert result.proposed_events == tuple(live.event_log)
    assert result.rng_transition.next_state == RNGState.capture(live.rng)
    roll = next(e for e in result.proposed_events if isinstance(e, AttackRolled))
    rng = random.Random(seed)
    natural = rng.randint(1, 20)
    assert roll.modifier == 5
    assert roll.is_hit == (natural == 20 or (natural != 1 and natural + 5 >= ac))
    assert after.combat_state.sap_marks == ({FOE: HERO} if roll.is_hit else {})
    if roll.is_hit:
        damage = sum(rng.randint(1, 6) for _ in range(2 if natural == 20 else 1)) + 3
        assert next(e.amount for e in result.proposed_events if isinstance(e, DamageApplied)) == (
            0 if immune else damage
        )
    assert result.rng_transition.next_state == RNGState.capture(rng)
    assert after.inventory_state == before.state_snapshot.inventory_state
    assert after.resource_state == before.state_snapshot.resource_state
    assert request == before
    assert RuleEvaluationResult.model_validate_json(result.model_dump_json()) == result


def legacy_snapshot(live, snapshot):
    return capture_combat_snapshot(
        live,
        grid=GridScene(width=3, height=3),
        world_version=snapshot.world_version,
        combat_id=snapshot.combat_state.combat_id,
        inventory_state=snapshot.inventory_state,
        resource_state=snapshot.resource_state,
        casting_state=snapshot.casting_state,
    )


def apply_sap(snapshot, result):
    from dnd5e_engine.evaluation_delta import SapMarksUpdate

    updates = [o for o in result.state_delta.operations if isinstance(o, SapMarksUpdate)]
    for op in updates:
        assert op.combat_id == snapshot.combat_state.combat_id
        assert set(op.actor_ids) == set(snapshot.combat_state.initiative_ids)
        assert {m.target_id: m.source_id for m in op.expected} == snapshot.combat_state.sap_marks
    mechanical = result.model_copy(
        update={
            "state_delta": result.state_delta.model_copy(
                update={
                    "operations": tuple(
                        o
                        for o in result.state_delta.operations
                        if not isinstance(o, SapMarksUpdate)
                    )
                }
            )
        }
    )
    after = apply_healing(snapshot, mechanical)
    for op in updates:
        after = after.model_copy(
            update={
                "combat_state": after.combat_state.model_copy(
                    update={"sap_marks": {m.target_id: m.source_id for m in op.value}}
                )
            }
        )
    return type(snapshot).model_validate_json(after.model_dump_json())


@pytest.mark.parametrize("npc", [False, True])
@pytest.mark.parametrize("consume", [False, True])
def test_mark_next_attack_or_source_start_expiry_and_reproc(npc, consume, monkeypatch):
    request, loader, handle, live = sap_case(npc=npc, immune=True)
    steps = [
        (HERO, request.payload),
        (
            FOE,
            CombatIntentPayload(intent_type="attack", target_id=HERO, weapon_id="mace")
            if consume
            else CombatIntentPayload(intent_type="pass"),
        ),
        (HERO, request.payload),
    ]
    snapshot, rng = request.state_snapshot, request.rng_context
    for index, (actor, intent) in enumerate(steps):
        prior = len(live.event_log)
        asyncio.run(orch.submit_player_intent(handle, actor, intent))
        current = request.model_copy(
            update={
                "actor_id": actor,
                "payload": intent,
                "state_snapshot": snapshot,
                "rng_context": rng,
            }
        )
        with monkeypatch.context() as scoped:
            isolate(scoped)
            scoped.setattr(
                orch, "_fold_mastery_procs", lambda *args: pytest.fail("Legacy Sap fold")
            )
            scoped.setattr(orch, "_pop_sap_mark", lambda *args: pytest.fail("Legacy Sap pop"))
            result = execute(current, loader)
            assert result.status == "accepted", result.error
            assert execute(current, loader) == result
        after = apply_sap(snapshot, result)
        assert after == legacy_snapshot(live, after)
        assert result.proposed_events == tuple(live.event_log[prior:])
        assert result.rng_transition.next_state == RNGState.capture(live.rng)
        if index == 0:
            assert after.combat_state.sap_marks == {FOE: HERO}
        if index == 1:
            assert after.combat_state.sap_marks == {}
            if consume:
                roll = next(e for e in result.proposed_events if isinstance(e, AttackRolled))
                independent = rng.state.restore()
                expected = min(independent.randint(1, 20), independent.randint(1, 20))
                assert roll.natural == expected
                assert roll.advantage == "disadvantage"
                assert roll.disadvantage_sources == ["trait"]
        snapshot = after
        rng = rng.model_copy(update={"state": result.rng_transition.next_state})
    assert snapshot.character_states[0].classes == {"fighter": 1}
    assert snapshot.character_states[0].weapon_mastery_slugs == ("mace", "flail", "morningstar")


def test_sap_cancels_prone_advantage_in_actual_attack(monkeypatch):
    from dnd5e_engine.types.conditions import ActiveCondition

    request, old, handle, live = sap_case()
    canonical = BundledAssetLoader()
    loader = MemoryAssetLoader(
        feats=[canonical.get_feat("defense")],
        classes=[old.get_class(s) for s in old.list_slugs("classes")],
        features=[old.get_feature(s) for s in old.list_slugs("features")],
        items=[old.get_item(s) for s in old.list_slugs("items")],
        conditions=[canonical.get_condition("prone")],
    )
    condition = ActiveCondition(
        condition="prone", source_entity_id="implied:revive", scope="combat"
    )
    live.initiative[0] = live.initiative[0].model_copy(update={"conditions": [condition]})
    live.active_conditions[HERO] = [condition]
    live.current_turn_index = 1
    live.current_actor_id = FOE
    live.sap_marks = {FOE: HERO}
    live.ruleset_loader = loader
    snapshot = legacy_snapshot(live, request.state_snapshot)
    intent = CombatIntentPayload(intent_type="attack", target_id=HERO, weapon_id="mace")
    request = request.model_copy(
        update={
            "actor_id": FOE,
            "payload": intent,
            "state_snapshot": snapshot,
            "ruleset_binding": ruleset_binding(loader),
        }
    )
    asyncio.run(orch.submit_player_intent(handle, FOE, intent))
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request, loader)
    assert result.status == "accepted", result.error
    roll = next(e for e in result.proposed_events if isinstance(e, AttackRolled))
    assert roll.advantage == "normal"
    assert roll.natural == 13
    assert roll.advantage_sources == ["condition:target"]
    assert roll.disadvantage_sources == ["trait"]
    after = apply_sap(snapshot, result)
    assert after.combat_state.sap_marks == {}
    assert after == legacy_snapshot(live, after)
    assert result.proposed_events == tuple(live.event_log)
    assert result.rng_transition.next_state == RNGState.capture(live.rng)


@pytest.mark.parametrize(
    "change",
    [
        "unknown-mastery",
        "partial-training",
        "style",
        "grants",
        "armor",
        "grip",
        "proficiency",
        "inaccessible",
        "source",
        "target",
    ],
)
def test_unclosed_sap_dependency_refuses_before_rng(change, monkeypatch):
    request, loader, _, _ = sap_case()
    snapshot = request.state_snapshot
    actor = snapshot.character_states[0]
    updates = {
        "unknown-mastery": {"weapon_mastery_slugs": ("mace", "quarterstaff", "morningstar")},
        "partial-training": {"weapon_mastery_slugs": ("mace",)},
        "style": {"fighting_styles": ("archery",)},
        "grants": {"granted_features": ("weapon-mastery",)},
        "armor": {"ac": 17},
        "grip": {"weapon_in_hands": None, "weapon_grip": "none"},
        "proficiency": {"weapon_proficiencies": []},
    }
    if change in updates:
        actor = actor.model_copy(update=updates[change])
        snapshot = snapshot.model_copy(
            update={"character_states": (actor, snapshot.character_states[1])}
        )
    elif change == "inaccessible":
        entries = snapshot.inventory_state.entries
        snapshot = snapshot.model_copy(
            update={
                "inventory_state": InventoryState(
                    entries=(entries[0].model_copy(update={"accessible": False}), *entries[1:])
                )
            }
        )
    else:
        snapshot = snapshot.model_copy(
            update={
                "combat_state": snapshot.combat_state.model_copy(
                    update={"sap_marks": {FOE: FOE} if change == "source" else {"absent": HERO}}
                )
            }
        )
    request = request.model_copy(update={"state_snapshot": snapshot})
    before = deepcopy(request)
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("RNG restored"))
    result = execute(request, loader)
    assert result.status == "unsupported", result.error
    assert result.state_delta is None
    assert result.rng_transition is None
    assert result.proposed_events == ()
    assert request == before


@pytest.mark.parametrize("cancel", [False, True])
def test_sap_atomic_fault_concurrency_replay_and_stale_commit(cancel, monkeypatch):
    request, loader, _, live = sap_case()
    before = deepcopy(request)
    original_rng = live.rng.getstate()
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request, loader)
        with ThreadPoolExecutor(4) as pool:
            assert list(pool.map(lambda _: execute(request, loader), range(8))) == [result] * 8
    after = apply_sap(request.state_snapshot, result)
    with pytest.raises(AssertionError):
        apply_sap(after, result)
    with pytest.raises(AssertionError):
        apply_sap(request.state_snapshot.model_copy(update={"world_version": 8}), result)
    real = evaluation_attack.resolve_activity

    def fault(activity, ctx, **kwargs):
        real(activity, ctx, **kwargs)
        assert ctx.mastery_procs == [("sap", FOE)]
        raise asyncio.CancelledError() if cancel else RuntimeError("actual Sap proc computed")

    monkeypatch.setattr(evaluation_attack, "resolve_activity", fault)
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        execute(request, loader)
    assert request == before
    assert live.rng.getstate() == original_rng
    assert not live.event_log


def test_closed_sap_delta_nonstacking_identity_and_shared_overwrite():
    from pydantic import ValidationError

    from dnd5e_engine.evaluation_delta import SapMark, SapMarksUpdate
    from dnd5e_engine.sap_rules import apply_sap as mark

    first = SapMark(
        target_id=FOE,
        source_id=HERO,
        expires="source_next_turn_start",
        consumes="target_next_attack_roll",
    )
    assert mark({FOE: HERO}, "char:second", FOE) == {FOE: "char:second"}
    assert mark({FOE: HERO}, HERO, FOE) == {FOE: HERO}
    for values in (
        (first, first),
        (first.model_copy(update={"source_id": FOE}),),
        (first.model_copy(update={"source_id": "absent"}),),
    ):
        with pytest.raises(ValidationError):
            SapMarksUpdate(
                kind="combat.sap_marks_update",
                combat_id="combat:test",
                actor_ids=(HERO, FOE),
                expected=(),
                value=values,
            )


def test_prior_binding_and_envelope_refused(monkeypatch):
    from pydantic import ValidationError

    from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest
    from dnd5e_engine.evaluation_ruleset import RulesetBindingError

    request, loader, _, _ = sap_case()
    value = request.model_dump(mode="python")
    value["schema_version"] = "engine-evaluation/19"
    with pytest.raises(ValidationError):
        RuleEvaluationRequest.model_validate(value)
    request = request.model_copy(
        update={
            "ruleset_binding": request.ruleset_binding.model_copy(
                update={"evaluator_version": "dnd5e-evaluation/32"}
            )
        }
    )
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("RNG restored"))
    with pytest.raises(RulesetBindingError):
        execute(request, loader)


@pytest.mark.parametrize("npc", [False, True])
def test_both_real_fighters_repeated_proc_consume_and_followup(npc, monkeypatch):
    request, loader, handle, live = sap_case(npc=npc, immune=True)
    hero, foe = live.initiative
    hero = hero.model_copy(update={"damage_immunities": ["bludgeoning"]})
    foe = hero.model_copy(
        update={
            "entity_id": FOE,
            "entity_type": "Character" if npc else "NPC",
            "name": "Other fighter",
            "initiative": 1,
        }
    )
    live.initiative = [hero, foe]
    live.custom_counters_by_entity[FOE] = deepcopy(foe.custom_counters)
    pools = request.state_snapshot.resource_state.pools
    resources = ResourceState(pools=(*pools, pools[0].model_copy(update={"owner_id": FOE})))
    entries = [e for e in request.state_snapshot.inventory_state.entries if e.owner_id == HERO]
    inventory = InventoryState(
        entries=(
            *entries,
            *(
                e.model_copy(update={"owner_id": FOE, "instance_id": "foe:" + e.item_slug})
                for e in entries
            ),
        )
    )
    snapshot = capture_combat_snapshot(
        live,
        grid=GridScene(width=3, height=3),
        world_version=7,
        combat_id=request.state_snapshot.combat_state.combat_id,
        inventory_state=inventory,
        resource_state=resources,
        casting_state=None,
    )
    rng = request.rng_context
    for actor in (HERO, FOE, HERO, FOE, HERO, FOE):
        target = FOE if actor == HERO else HERO
        intent = CombatIntentPayload(intent_type="attack", target_id=target, weapon_id="mace")
        prior = len(live.event_log)
        asyncio.run(orch.submit_player_intent(handle, actor, intent))
        current = request.model_copy(
            update={
                "actor_id": actor,
                "payload": intent,
                "state_snapshot": snapshot,
                "rng_context": rng,
            }
        )
        with monkeypatch.context() as scoped:
            isolate(scoped)
            result = execute(current, loader)
        assert result.status == "accepted", result.error
        snapshot = apply_sap(snapshot, result)
        assert snapshot == legacy_snapshot(live, snapshot)
        assert len(snapshot.combat_state.sap_marks) <= 1
        assert result.proposed_events == tuple(live.event_log[prior:])
        assert result.rng_transition.next_state == RNGState.capture(live.rng)
        rng = rng.model_copy(update={"state": result.rng_transition.next_state})
    assert snapshot.character_states[0].hp_current == 40
    assert snapshot.character_states[1].hp_current == 40


@pytest.mark.parametrize("drift", ["training", "defense"])
def test_sap_canonical_dependency_drift_before_rng(drift, monkeypatch):
    request, old, _, _ = sap_case()
    loader = MemoryAssetLoader(
        classes=[old.get_class(s) for s in old.list_slugs("classes")],
        features=[old.get_feature(s) for s in old.list_slugs("features")],
        feats=[] if drift == "defense" else [old.get_feat("defense")],
        items=[
            old.get_item(s).model_copy(update={"mastery": "vex"})
            if drift == "training" and s == "flail"
            else old.get_item(s)
            for s in old.list_slugs("items")
        ],
    )
    request = request.model_copy(update={"ruleset_binding": ruleset_binding(loader)})
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("RNG restored"))
    result = execute(request, loader)
    assert result.status == "unsupported"
    assert result.state_delta is None
    assert result.rng_transition is None
    assert result.proposed_events == ()
