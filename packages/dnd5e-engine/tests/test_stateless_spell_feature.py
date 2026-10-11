"""Actual class PCs, canonical healing, full Legacy differential and rollback."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd5e_srd_data.schema.monster import MonsterTraitMechanic
from pydantic import ValidationError

from dnd5e_engine import evaluation_spells
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.evaluation_casting import ActorCastingState, CastingState, ClassSpellSource
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, RuleEvaluationResult
from dnd5e_engine.evaluation_delta import ResourceUpdate, SlotSpendLedgerUpdate
from dnd5e_engine.evaluation_resources import FeatureResource, ResourceState, SlotResource
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_engine.evaluation_snapshot import capture_combat_snapshot
from dnd5e_engine.evaluation_state import CombatSnapshot
from dnd5e_engine.events import HealingApplied
from dnd5e_engine.specs import GridScene
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import apply_delta, execute, request_and_live
from tests.test_stateless_item_closure_core import isolate


def healing_case(*, feature=False, seed=0, hp=10, swarm=False):
    request, handle, live = request_and_live(seed=seed)
    canonical = BundledAssetLoader()
    loader = MemoryAssetLoader(
        classes=[canonical.get_class(s) for s in ("cleric", "fighter")],
        features=[
            canonical.get_feature(s)
            for s in (
                "spellcasting",
                "divine-order",
                "second-wind",
                "fighting-style",
                "weapon-mastery",
            )
        ],
        spells=[canonical.get_spell("cure-wounds")],
    )
    slug = "fighter" if feature else "cleric"
    grants = tuple(
        g.slug
        for g in canonical.get_class(slug).granted_features
        if g.level <= 1 and g.ref_type == "feature"
    )
    actors = []
    for actor in live.initiative:
        update = {"carried_item_slugs": (), "weapon_in_hands": None, "weapon_grip": "none"}
        if actor.entity_id == HERO:
            update.update(
                class_slug=slug,
                classes={slug: 1},
                granted_features=grants,
                character_level=1,
                wisdom=16,
                constitution=14,
                spellcasting_ability=None if feature else "wis",
                hp_current=hp,
                fighting_styles=("defense",) if feature else (),
            )
        elif swarm:
            update["trait_mechanics"] = [MonsterTraitMechanic.SWARM]
        if actor.entity_id == FOE:
            update["hp_current"] = hp
        actors.append(actor.model_copy(update=update))
    live.initiative = actors
    live.tracked_hp = {a.entity_id: a.hp_current for a in actors}
    live.ruleset_loader = loader
    live.spell_slots_by_entity = {HERO: {} if feature else {1: 2}, FOE: {}}
    live.pact_slots_by_entity = {HERO: {}, FOE: {}}
    live.spells_known_by_entity = {HERO: [] if feature else ["cure-wounds"], FOE: []}
    live.custom_counters_by_entity = {
        HERO: {"feature_use:second-wind": {"spent": 0}} if feature else {},
        FOE: {},
    }
    resources = ResourceState(
        pools=(
            FeatureResource(
                kind="feature_uses",
                owner_id=HERO,
                pool_id="second-wind",
                current=2,
                maximum=2,
                feature_slug="second-wind",
            )
            if feature
            else SlotResource(
                kind="spell_slot", owner_id=HERO, pool_id="spell:1", current=2, maximum=2, level=1
            ),
        )
    )
    casting = (
        None
        if feature
        else CastingState(
            actors=(
                ActorCastingState(
                    actor_id=HERO,
                    verbal_available=True,
                    sources=(
                        ClassSpellSource(
                            source_id="class:cleric",
                            class_slug="cleric",
                            ability="wis",
                            known_spells=("cure-wounds",),
                            prepared_spells=("cure-wounds",),
                        ),
                    ),
                ),
            )
        )
    )
    snapshot = capture_combat_snapshot(
        live,
        grid=GridScene(width=3, height=3),
        world_version=7,
        combat_id=request.state_snapshot.combat_state.combat_id,
        inventory_state=request.state_snapshot.inventory_state,
        resource_state=resources,
        casting_state=casting,
    )
    intent = (
        CombatIntentPayload(
            intent_type="use_feature",
            feature_id="second-wind",
            activity_id="zumaUC11cpntqme1",
            target_id=HERO,
        )
        if feature
        else CombatIntentPayload(
            intent_type="cast_spell",
            spell_id="cure-wounds",
            target_id=FOE,
            slot_level=1,
            spell_source_id="class:cleric",
        )
    )
    request = request.model_copy(
        update={
            "state_snapshot": snapshot,
            "payload": intent,
            "ruleset_binding": ruleset_binding(loader),
        }
    )
    return request, loader, handle, live


def apply_healing(snapshot, result, *, world_version=None):
    """Independent atomic consumer: guard ALL facts before publishing anything."""
    assert result.state_delta.expected_world_version == (
        snapshot.world_version if world_version is None else world_version
    )
    payments = [o for o in result.state_delta.operations if isinstance(o, ResourceUpdate)]
    ledgers = [o for o in result.state_delta.operations if isinstance(o, SlotSpendLedgerUpdate)]
    pools = {(p.owner_id, p.pool_id): p for p in snapshot.resource_state.pools}
    for op in payments:
        assert pools[(op.owner_id, op.pool_id)] == op.expected
    for op in ledgers:
        assert snapshot.combat_state.combat_id == op.combat_id
        assert snapshot.combat_state.expended_resources.get(op.owner_id) == op.expected
    mechanical = result.model_copy(
        update={
            "state_delta": result.state_delta.model_copy(
                update={
                    "operations": tuple(
                        o
                        for o in result.state_delta.operations
                        if not isinstance(o, (ResourceUpdate, SlotSpendLedgerUpdate))
                    ),
                }
            )
        }
    )
    value = apply_delta(snapshot, mechanical).model_dump(mode="python")
    actors = {a["entity_id"]: a for a in value["character_states"]}
    for op in payments:
        pools[(op.owner_id, op.pool_id)] = op.value
        if isinstance(op.value, SlotResource):
            field = "spell_slots" if op.value.kind == "spell_slot" else "pact_slots"
            actors[op.owner_id][field][op.value.level] = op.value.current
        else:
            assert isinstance(op.value, FeatureResource)
            actors[op.owner_id]["custom_counters"]["feature_use:" + op.value.feature_slug] = {
                "spent": op.value.maximum - op.value.current
            }
    for op in ledgers:
        value["combat_state"]["expended_resources"][op.owner_id] = op.value
    value["resource_state"] = ResourceState(pools=tuple(pools.values())).model_dump(mode="python")
    return CombatSnapshot.model_validate(value)


@pytest.mark.parametrize("feature", [False, True])
@pytest.mark.parametrize("seed", [0, 1, 5, 31])
def test_actual_class_full_legacy_state_event_rng_parity(feature, seed, monkeypatch):
    request, loader, handle, live = healing_case(feature=feature, seed=seed)
    original = deepcopy(request)
    asyncio.run(orch.submit_player_intent(handle, HERO, request.payload))
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request, loader)
    assert result.status == "accepted", result.error
    after = apply_healing(request.state_snapshot, result)
    legacy = capture_combat_snapshot(
        live,
        grid=GridScene(width=3, height=3),
        world_version=7,
        combat_id=after.combat_state.combat_id,
        inventory_state=after.inventory_state,
        resource_state=after.resource_state,
        casting_state=after.casting_state,
    )
    assert after == legacy
    assert result.proposed_events == tuple(live.event_log)
    assert result.rng_transition.next_state == RNGState.capture(live.rng)
    assert RuleEvaluationResult.model_validate_json(result.model_dump_json()) == result
    assert request == original
    assert (
        after.character_states[0].granted_features
        == request.state_snapshot.character_states[0].granted_features
    )
    assert after.character_states[0].classes == request.state_snapshot.character_states[0].classes


@pytest.mark.parametrize("feature,expected", [(False, 17), (True, 8)])
def test_independent_rules_magnitudes_costs_budgets_and_turn(feature, expected):
    request, loader, _, _ = healing_case(feature=feature)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    after = apply_healing(request.state_snapshot, result)
    target = after.character_states[0 if feature else 1]
    assert target.hp_current == 10 + expected
    assert (
        next(e.amount for e in result.proposed_events if isinstance(e, HealingApplied)) == expected
    )
    assert after.resource_state.pools[0].current == 1
    hero = after.character_states[0]
    assert hero.bonus_action_available is (not feature)
    assert hero.action_available is feature
    assert after.combat_state.current_turn_index == (0 if feature else 1)
    assert after.combat_state.expended_resources == ({} if feature else {HERO: {"spell_slot:1": 1}})
    with pytest.raises(AssertionError):
        apply_healing(after, result)
    with pytest.raises(AssertionError):
        apply_healing(request.state_snapshot, result, world_version=8)


def test_swarm_cannot_be_healed_but_legal_spell_pays_cost_without_draw(monkeypatch):
    request, loader, handle, live = healing_case(swarm=True)
    asyncio.run(orch.submit_player_intent(handle, HERO, request.payload))
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request, loader)
    assert result.status == "accepted", result.error
    after = apply_healing(request.state_snapshot, result)
    assert after.character_states[1].hp_current == 10
    assert after.resource_state.pools[0].current == 1
    assert not after.character_states[0].action_available
    assert not any(isinstance(e, HealingApplied) for e in result.proposed_events)
    assert result.rng_transition.next_state == request.rng_context.state
    assert result.proposed_events == tuple(live.event_log)


@pytest.mark.parametrize("feature", [False, True])
def test_replay_concurrent_and_last_owned_unit(feature):
    request, loader, _, _ = healing_case(feature=feature)
    pool = request.state_snapshot.resource_state.pools[0].model_copy(update={"current": 1})
    actor = request.state_snapshot.character_states[0]
    actor = actor.model_copy(
        update={"custom_counters": {"feature_use:second-wind": {"spent": 1}}}
        if feature
        else {"spell_slots": {1: 1}}
    )
    request = request.model_copy(
        update={
            "state_snapshot": request.state_snapshot.model_copy(
                update={
                    "resource_state": ResourceState(pools=(pool,)),
                    "character_states": (actor, request.state_snapshot.character_states[1]),
                }
            )
        }
    )
    original = deepcopy(request)
    with ThreadPoolExecutor(4) as executor:
        results = list(executor.map(lambda _: execute(request, loader), range(8)))
    assert all(r == results[0] for r in results)
    assert results[0].status == "accepted"
    assert apply_healing(request.state_snapshot, results[0]).resource_state.pools[0].current == 0
    assert request == original


@pytest.mark.parametrize("feature", [False, True])
@pytest.mark.parametrize(
    "failure",
    [
        "budget",
        "empty",
        "dead",
        "target",
        "species",
        "feature",
        "condition",
        "effect",
        "extended_budget",
    ],
)
def test_fail_closed_before_rng_or_payment(feature, failure, monkeypatch):
    request, loader, _, _ = healing_case(feature=feature)
    snapshot = request.state_snapshot
    actor = snapshot.character_states[0]
    if failure == "budget":
        actor = actor.model_copy(
            update={"bonus_action_available" if feature else "action_available": False}
        )
    elif failure == "empty":
        pool = snapshot.resource_state.pools[0].model_copy(update={"current": 0})
        snapshot = snapshot.model_copy(update={"resource_state": ResourceState(pools=(pool,))})
        actor = actor.model_copy(
            update={"custom_counters": {"feature_use:second-wind": {"spent": 2}}}
            if feature
            else {"spell_slots": {1: 0}}
        )
    elif failure == "dead":
        actor = actor.model_copy(update={"hp_current": 0})
    elif failure == "target":
        request = request.model_copy(
            update={"payload": request.payload.model_copy(update={"target_id": "absent"})}
        )
    elif failure == "species":
        actor = actor.model_copy(update={"species_slug": "human"})
    elif failure == "feature":
        actor = actor.model_copy(
            update={"granted_features": (*actor.granted_features, "disciple-of-life")}
        )
    elif failure == "condition":
        from dnd5e_engine.evaluation_state import ConditionRecord

        actor = actor.model_copy(
            update={
                "conditions": [
                    ConditionRecord(
                        condition="poisoned",
                        source_entity_id="implied:poison",
                        scope="combat",
                        duration_rounds=None,
                        save_dc=None,
                        applied_round=0,
                        exhaustion_level=1,
                        source_effect_id=None,
                    )
                ]
            }
        )
    elif failure == "effect":
        actor = actor.model_copy(update={"concentration_effect_id": "unmigrated"})
    else:
        actor = actor.model_copy(update={"extra_actions_remaining": 1})
    snapshot = snapshot.model_copy(
        update={"character_states": (actor, snapshot.character_states[1])}
    )
    request = request.model_copy(update={"state_snapshot": snapshot})
    original = deepcopy(request)
    monkeypatch.setattr(RNGState, "restore", lambda _: pytest.fail("refusal restored RNG"))
    result = execute(request, loader)
    assert result.status in {"rejected", "unsupported"}, result
    assert result.state_delta is None
    assert not result.proposed_events
    assert result.rng_transition is None
    assert request == original


@pytest.mark.parametrize(
    "problem",
    [
        "unprepared",
        "unknown",
        "source",
        "ability",
        "verbal",
        "hands",
        "far",
        "slot",
        "ritual",
        "reaction",
    ],
)
def test_complete_spell_eligibility(problem, monkeypatch):
    request, loader, _, _ = healing_case()
    snapshot = request.state_snapshot
    entry = snapshot.casting_state.actors[0]
    source = entry.sources[0]
    if problem in {"unprepared", "unknown", "source", "ability"}:
        changes = {
            "unprepared": {"prepared_spells": ()},
            "unknown": {"known_spells": (), "prepared_spells": ()},
            "source": {"class_slug": "wizard"},
            "ability": {"ability": "int"},
        }[problem]
        source = source.model_copy(update=changes)
        entry = entry.model_copy(update={"sources": (source,)})
        if problem == "unknown":
            actor = snapshot.character_states[0].model_copy(update={"spells_known": []})
            snapshot = snapshot.model_copy(
                update={"character_states": (actor, snapshot.character_states[1])}
            )
    elif problem == "verbal":
        entry = entry.model_copy(update={"verbal_available": False})
    elif problem == "hands":
        actor = snapshot.character_states[0].model_copy(update={"other_hand_occupied": True})
        snapshot = snapshot.model_copy(
            update={"character_states": (actor, snapshot.character_states[1])}
        )
    elif problem == "far":
        snapshot = snapshot.model_copy(
            update={
                "combat_state": snapshot.combat_state.model_copy(
                    update={"actor_zone": {HERO: "0,0", FOE: "2,2"}}
                )
            }
        )
    else:
        changes = {
            "slot": {"slot_level": 2},
            "ritual": {"as_ritual": True},
            "reaction": {"spell_id": "counterspell"},
        }[problem]
        request = request.model_copy(update={"payload": request.payload.model_copy(update=changes)})
    snapshot = snapshot.model_copy(update={"casting_state": CastingState(actors=(entry,))})
    request = request.model_copy(update={"state_snapshot": snapshot})
    monkeypatch.setattr(RNGState, "restore", lambda _: pytest.fail("illegal cast restored RNG"))
    assert execute(request, loader).status in {"rejected", "unsupported"}


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("feature", [False, True])
def test_resolver_failure_rolls_back_all_local_changes(cancel, feature, monkeypatch):
    request, loader, _, _ = healing_case(feature=feature)
    original = deepcopy(request)
    real = evaluation_spells.resolve_activity

    def explode(activity, ctx):
        real(activity, ctx)
        raise asyncio.CancelledError() if cancel else RuntimeError("after heal")

    monkeypatch.setattr(evaluation_spells, "resolve_activity", explode)
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        execute(request, loader)
    assert request == original


def test_casting_schema_requires_every_fact_and_complete_mirror():
    request, _, _, _ = healing_case()
    value = request.state_snapshot.model_dump(mode="python")
    del value["casting_state"]
    with pytest.raises(ValidationError):
        CombatSnapshot.model_validate(value)
    entry = request.state_snapshot.casting_state.actors[0].model_dump(mode="python")
    del entry["verbal_available"]
    with pytest.raises(ValidationError):
        ActorCastingState.model_validate(entry)
    source = request.state_snapshot.casting_state.actors[0].sources[0].model_dump(mode="python")
    del source["prepared_spells"]
    with pytest.raises(ValidationError):
        ClassSpellSource.model_validate(source)


@pytest.mark.parametrize("feature", [False, True])
def test_npc_uses_same_real_class_mechanics(feature, monkeypatch):
    request, loader, handle, live = healing_case(feature=feature)
    # NPC controls the exact same approved class-based mechanical profile.
    live.initiative[0] = live.initiative[0].model_copy(update={"entity_type": "Monster"})
    snapshot = request.state_snapshot
    actor = snapshot.character_states[0].model_copy(update={"entity_type": "Monster"})
    request = request.model_copy(
        update={
            "state_snapshot": snapshot.model_copy(
                update={"character_states": (actor, snapshot.character_states[1])}
            )
        }
    )
    asyncio.run(orch.submit_player_intent(handle, HERO, request.payload))
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request, loader)
    assert result.status == "accepted"
    after = apply_healing(request.state_snapshot, result)
    assert result.proposed_events == tuple(live.event_log)
    assert result.rng_transition.next_state == RNGState.capture(live.rng)
    assert after.character_states[0].entity_type == "Monster"
    heal = next(e for e in result.proposed_events if isinstance(e, HealingApplied))
    assert heal.amount == (8 if feature else 17)


@pytest.mark.parametrize("feature", [False, True])
def test_full_hp_still_draws_and_pays_and_source_data_drift_refuses(feature, monkeypatch):
    request, loader, _, _ = healing_case(feature=feature, hp=40)
    result = execute(request, loader)
    assert result.status == "accepted"
    assert result.rng_transition.state_changed
    assert apply_healing(request.state_snapshot, result).resource_state.pools[0].current == 1
    canonical = BundledAssetLoader()
    changed = canonical.get_feature("second-wind").model_copy(update={"activities": []})
    drift = MemoryAssetLoader(
        classes=[loader.get_class(s) for s in loader.list_slugs("classes")],
        features=[
            changed if s == "second-wind" else loader.get_feature(s)
            for s in loader.list_slugs("features")
        ],
        spells=[loader.get_spell(s) for s in loader.list_slugs("spells")],
    )
    if feature:
        request = request.model_copy(update={"ruleset_binding": ruleset_binding(drift)})
        monkeypatch.setattr(RNGState, "restore", lambda _: pytest.fail("drift restored RNG"))
        assert execute(request, drift).status == "unsupported"


def test_pending_reaction_and_missing_casting_component_refuse_before_rng(monkeypatch):
    from dnd5e_engine.reactions import PendingReaction

    request, loader, _, _ = healing_case()
    spell = BundledAssetLoader().get_spell("counterspell")
    activity = spell.activities[0]
    pending = PendingReaction(
        owner_id=FOE,
        spell_id=spell.slug,
        activity_id=activity.id,
        slot_level=3,
        conditions=tuple(activity.activation.reaction_conditions),
        semantics=activity.reaction,
        source_kind="spell",
    )
    snapshot = request.state_snapshot
    cases = [
        snapshot.model_copy(update={"casting_state": None}),
        snapshot.model_copy(
            update={
                "combat_state": snapshot.combat_state.model_copy(
                    update={"pending_reactions": [pending]}
                ),
            }
        ),
    ]
    monkeypatch.setattr(
        RNGState, "restore", lambda _: pytest.fail("unmigrated dependency restored RNG")
    )
    for case in cases:
        result = execute(request.model_copy(update={"state_snapshot": case}), loader)
        assert result.status in {"rejected", "unsupported"}
        assert not result.proposed_events


def test_new_execution_modules_do_not_import_legacy_runtime():
    import ast
    from pathlib import Path

    from dnd5e_engine import evaluation_paid_projection, evaluation_spell_dependencies

    for module in (evaluation_spells, evaluation_paid_projection, evaluation_spell_dependencies):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not any(
                    x in (node.module or "")
                    for x in ("orchestrator", "live_", "evaluation_snapshot")
                )
            if isinstance(node, ast.Name):
                assert node.id not in {"_LiveCombat", "CombatHandle", "_REGISTRY"}
