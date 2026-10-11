"""Real predeclared Counterspell: atomic trigger, save, payment and rollback."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd5e_srd_data.schema.common import ReactionTriggerKind
from pydantic import ValidationError

from dnd5e_engine import evaluation_reactions, evaluation_spells
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.evaluation_casting import ActorCastingState, ClassSpellSource
from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest, RuleEvaluationResult
from dnd5e_engine.evaluation_delta import PendingReactionUpdate, ResourceUpdate
from dnd5e_engine.evaluation_resources import FeatureResource, ResourceState, SlotResource
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_engine.evaluation_snapshot import capture_combat_snapshot
from dnd5e_engine.events import CastFailed, HealingApplied, ReactionTriggered, SaveRolled
from dnd5e_engine.reactions import PendingReaction, ReactionOpportunity, matching_conditions
from dnd5e_engine.specs import GridScene
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import execute
from tests.test_stateless_item_closure_core import isolate
from tests.test_stateless_spell_feature import apply_healing, healing_case

OBSERVER = "char:observer"
KNOWN = (
    "shield",
    "magic-missile",
    "burning-hands",
    "detect-magic",
    "identify",
    "sleep",
    "misty-step",
    "mirror-image",
    "scorching-ray",
    "invisibility",
    "counterspell",
    "fireball",
)


def reaction_case(*, seed=0, npc=False):
    request, _, handle, live = healing_case(seed=seed)
    canonical = BundledAssetLoader()
    grants = {
        g.slug
        for g in canonical.get_class("wizard").granted_features
        if g.ref_type == "feature" and g.level <= 5
    }
    grants |= {
        g.slug
        for g in canonical.get_subclass("evoker").granted_features
        if g.ref_type == "feature" and g.level <= 5
    }
    feature_slugs = grants | {"spellcasting", "divine-order"}
    loader = MemoryAssetLoader(
        classes=[canonical.get_class(s) for s in ("cleric", "wizard")],
        subclasses=[canonical.get_subclass("evoker")],
        features=[canonical.get_feature(s) for s in sorted(feature_slugs)],
        spells=[canonical.get_spell(s) for s in (*KNOWN, "cure-wounds")],
    )
    hero, old_foe = live.initiative
    observer = old_foe.model_copy(
        update={"entity_id": OBSERVER, "entity_type": "Character", "name": "Observer"}
    )
    foe = old_foe.model_copy(
        update={
            "entity_type": "Monster" if npc else "Character",
            "class_slug": "wizard",
            "classes": {"wizard": 5},
            "subclass_slug": "evoker",
            "character_level": 5,
            "granted_features": tuple(sorted(grants)),
            "intelligence": 18,
            "constitution": 14,
            "spellcasting_ability": "int",
            "save_proficiencies": ["int", "wis"],
            "proficiency_bonus_override": None,
        }
    )
    live.initiative = [hero, observer, foe]
    live.party_ids.add(OBSERVER)
    live.actor_zone[OBSERVER] = "2,0"
    live.movement_ledgers[OBSERVER] = deepcopy(live.movement_ledgers[FOE])
    live.tracked_hp[OBSERVER] = observer.hp_current
    live.ruleset_loader = loader
    live.spell_slots_by_entity.update({FOE: {1: 4, 2: 3, 3: 2}, OBSERVER: {}})
    live.pact_slots_by_entity[OBSERVER] = {}
    live.spells_known_by_entity.update({FOE: list(KNOWN), OBSERVER: []})
    live.custom_counters_by_entity.update(
        {FOE: {"feature_use:arcane-recovery": {"spent": 0}}, OBSERVER: {}}
    )
    spell = canonical.get_spell("counterspell")
    activity = spell.activities[0]
    pending = PendingReaction(
        FOE,
        spell.slug,
        activity.id,
        3,
        tuple(activity.activation.reaction_conditions),
        activity.reaction,
    )
    live.pending_reactions = [pending]
    resources = ResourceState(
        pools=(
            *request.state_snapshot.resource_state.pools,
            *(
                SlotResource(
                    kind="spell_slot",
                    owner_id=FOE,
                    pool_id=f"spell:{level}",
                    current=count,
                    maximum=count,
                    level=level,
                )
                for level, count in ((1, 4), (2, 3), (3, 2))
            ),
            FeatureResource(
                kind="feature_uses",
                owner_id=FOE,
                pool_id="arcane-recovery",
                current=1,
                maximum=1,
                feature_slug="arcane-recovery",
            ),
        )
    )
    casting = request.state_snapshot.casting_state.model_copy(
        update={
            "actors": (
                *request.state_snapshot.casting_state.actors,
                ActorCastingState(
                    actor_id=FOE,
                    verbal_available=False,
                    materials=(),
                    sources=(
                        ClassSpellSource(
                            source_id="class:wizard",
                            class_slug="wizard",
                            ability="int",
                            known_spells=KNOWN,
                            prepared_spells=KNOWN[-9:],
                        ),
                    ),
                ),
            )
        }
    )
    snapshot = capture_combat_snapshot(
        live,
        grid=GridScene(width=20, height=3),
        world_version=7,
        combat_id=request.state_snapshot.combat_state.combat_id,
        inventory_state=request.state_snapshot.inventory_state,
        resource_state=resources,
        casting_state=casting,
        reaction_sources={FOE: "class:wizard"},
    )
    request = request.model_copy(
        update={
            "state_snapshot": snapshot,
            "payload": request.payload.model_copy(update={"target_id": HERO}),
            "ruleset_binding": ruleset_binding(loader),
        }
    )
    return request, loader, handle, live


@pytest.mark.parametrize("seed", [0, 1, 5, 31])
@pytest.mark.parametrize("npc", [False, True])
def test_full_legacy_state_event_rng_and_independent_srd_costs(seed, npc, monkeypatch):
    request, loader, handle, live = reaction_case(seed=seed, npc=npc)
    original = deepcopy(request)
    asyncio.run(orch.submit_player_intent(handle, HERO, request.payload))
    with monkeypatch.context() as scoped:
        isolate(scoped)
        for name in ("fire_reaction", "_eligible", "pay_reaction"):
            scoped.setattr(
                "dnd5e_engine.live_reactions." + name,
                lambda *a, **k: pytest.fail("Legacy reaction called"),
            )
        result = execute(request, loader)
    assert result.status == "accepted", result.error
    after = apply_healing(request.state_snapshot, result)
    legacy = capture_combat_snapshot(
        live,
        grid=GridScene(width=20, height=3),
        world_version=7,
        combat_id=after.combat_state.combat_id,
        inventory_state=after.inventory_state,
        resource_state=after.resource_state,
        casting_state=after.casting_state,
        reaction_sources={FOE: "class:wizard"},
    )
    assert after == legacy
    assert result.proposed_events == tuple(live.event_log)
    assert result.rng_transition.next_state == RNGState.capture(live.rng)
    save = next(e for e in result.proposed_events if isinstance(e, SaveRolled))
    assert save.dc == 15
    assert save.modifier == 2
    assert save.ability == "con"
    assert save.target_id == HERO
    assert save.succeeded is (seed in (0, 5))
    pools = {(p.owner_id, p.pool_id): p.current for p in after.resource_state.pools}
    assert pools[FOE, "spell:3"] == 1
    assert pools[HERO, "spell:1"] == (1 if save.succeeded else 2)
    assert pools[FOE, "arcane-recovery"] == 1
    assert not after.character_states[0].action_available
    assert not after.character_states[2].reaction_available
    assert after.combat_state.current_turn_index == 1
    assert not after.combat_state.pending_reactions
    assert len([e for e in result.proposed_events if isinstance(e, ReactionTriggered)]) == 1
    assert any(isinstance(e, HealingApplied) for e in result.proposed_events) is save.succeeded
    assert any(
        isinstance(e, CastFailed) and e.reason == "countered" for e in result.proposed_events
    ) is (not save.succeeded)
    assert result == RuleEvaluationResult.model_validate_json(result.model_dump_json())
    assert request == original
    with pytest.raises(AssertionError):
        apply_healing(after, result)
    with pytest.raises(AssertionError):
        apply_healing(request.state_snapshot, result, world_version=8)


@pytest.mark.parametrize("reason", ["no_reaction", "empty_slot", "out_of_range"])
def test_ineligible_legal_declaration_retained_without_cost_or_rng(reason):
    request, loader, _, _ = reaction_case()
    snapshot = request.state_snapshot
    if reason == "out_of_range":
        state = snapshot.combat_state.model_copy(
            update={"actor_zone": {**snapshot.combat_state.actor_zone, FOE: "13,0"}}
        )
        snapshot = snapshot.model_copy(update={"combat_state": state})
    else:
        actors = list(snapshot.character_states)
        foe = actors[2]
        if reason == "no_reaction":
            actors[2] = foe.model_copy(update={"reaction_available": False})
        else:
            actors[2] = foe.model_copy(update={"spell_slots": {1: 4, 2: 3, 3: 0}})
            pools = tuple(
                p.model_copy(update={"current": 0})
                if p.owner_id == FOE and p.pool_id == "spell:3"
                else p
                for p in snapshot.resource_state.pools
            )
            snapshot = snapshot.model_copy(update={"resource_state": ResourceState(pools=pools)})
        snapshot = snapshot.model_copy(update={"character_states": tuple(actors)})
    request = request.model_copy(update={"state_snapshot": snapshot})
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    after = apply_healing(snapshot, result)
    assert after.combat_state.pending_reactions == snapshot.combat_state.pending_reactions
    assert not any(isinstance(e, (ReactionTriggered, SaveRolled)) for e in result.proposed_events)
    assert next(e.amount for e in result.proposed_events if isinstance(e, HealingApplied)) == 17
    assert not any(
        isinstance(o, ResourceUpdate) and o.owner_id == FOE for o in result.state_delta.operations
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("spell_id", "shield"),
        ("activity_id", "wrong"),
        ("slot_level", 4),
        ("spell_source_id", "made-up"),
    ],
)
def test_unreviewed_declaration_fails_before_rng(field, value, monkeypatch):
    request, loader, _, _ = reaction_case()
    snapshot = request.state_snapshot
    pending = snapshot.combat_state.pending_reactions[0].model_copy(update={field: value})
    snapshot = snapshot.model_copy(
        update={
            "combat_state": snapshot.combat_state.model_copy(
                update={"pending_reactions": [pending]}
            )
        }
    )
    request = request.model_copy(update={"state_snapshot": snapshot})
    monkeypatch.setattr(
        "dnd5e_engine.evaluation_rng.RNGState.restore", lambda *a: pytest.fail("RNG restored")
    )
    result = execute(request, loader)
    assert result.status == "unsupported"
    assert result.state_delta is None
    assert result.rng_transition is None


@pytest.mark.parametrize(
    "mutation",
    [
        "prepared",
        "ability",
        "hand",
        "subclass",
        "grant",
        "species",
        "condition",
        "response",
        "condition_kind",
        "duplicate",
    ],
)
def test_missing_or_unreviewed_dependency_fails_closed(mutation, monkeypatch):
    request, loader, _, _ = reaction_case()
    snapshot = request.state_snapshot
    data = snapshot.model_dump(mode="python")
    if mutation in ("prepared", "ability"):
        source = data["casting_state"]["actors"][1]["sources"][0]
        source["prepared_spells"] = () if mutation == "prepared" else source["prepared_spells"]
        if mutation == "ability":
            source["ability"] = "wis"
    elif mutation == "duplicate":
        data["combat_state"]["pending_reactions"] *= 2
    elif mutation in ("response", "condition_kind"):
        p = data["combat_state"]["pending_reactions"][0]
        if mutation == "response":
            p["semantics"]["responses"][0]["kind"] = "negate_triggering_spell_damage"
        else:
            p["conditions"][0]["kind"] = ReactionTriggerKind.TAKES_DAMAGE
    else:
        actor = data["character_states"][2]
        if mutation == "hand":
            actor["weapon_in_hands"] = "longsword"
        if mutation == "subclass":
            actor["subclass_slug"] = "other"
        if mutation == "grant":
            actor["granted_features"] = tuple(
                g for g in actor["granted_features"] if g != "potent-cantrip"
            )
        if mutation == "species":
            actor["species_slug"] = "elf"
        if mutation == "condition":
            actor["conditions"] = [
                {"condition": "incapacitated", "duration_rounds": None, "applied_round": 0}
            ]
    monkeypatch.setattr(
        "dnd5e_engine.evaluation_rng.RNGState.restore", lambda *a: pytest.fail("RNG restored")
    )
    # evaluate revalidates even model_copy supplied malformed state.
    snapshot = snapshot.model_copy(update={})
    try:
        snapshot = type(snapshot).model_validate(data)
    except ValidationError:
        assert mutation in ("duplicate", "condition", "hand")
        return
    result = execute(request.model_copy(update={"state_snapshot": snapshot}), loader)
    assert result.status == "unsupported"
    assert result.state_delta is None
    assert not result.proposed_events


@pytest.mark.parametrize("fault", ["save", "after_reaction", "cancel"])
def test_fault_or_cancel_discards_entire_candidate(fault, monkeypatch):
    request, loader, _, _ = reaction_case(seed=0)
    original = deepcopy(request)
    if fault == "save":

        def fail(*a, **k):
            raise ValueError("injected save failure")

        monkeypatch.setattr(evaluation_reactions, "resolve_activity", fail)
    else:

        def fail(*a, **k):
            if fault == "cancel":
                raise asyncio.CancelledError()
            raise ValueError("after reaction failure")

        monkeypatch.setattr(evaluation_spells, "resolve_activity", fail)
    with pytest.raises(asyncio.CancelledError if fault == "cancel" else ValueError):
        execute(request, loader)
    assert request == original


def test_replay_concurrency_and_json_with_no_live_state(monkeypatch):
    request, loader, _, _ = reaction_case(seed=1)
    isolate(monkeypatch)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: execute(request, loader), range(8)))
    assert all(r == results[0] and r.status == "accepted" for r in results)
    assert RuleEvaluationRequest.model_validate_json(request.model_dump_json()) == request
    after = apply_healing(request.state_snapshot, results[0])
    assert (
        len([o for o in results[0].state_delta.operations if isinstance(o, PendingReactionUpdate)])
        == 1
    )
    assert not after.combat_state.pending_reactions


@pytest.mark.parametrize(
    "path",
    [
        ("spell_source_id",),
        ("conditions", 0, "max_range_ft"),
        ("conditions", 0, "target_spell_slug"),
        ("conditions", 0, "condition_text"),
        ("semantics", "effect_expiry"),
        ("semantics", "responses", 0, "trigger_kind"),
    ],
)
def test_every_nested_reaction_fact_required(path):
    request, _, _, _ = reaction_case()
    data = request.model_dump(mode="python")
    current = data["state_snapshot"]["combat_state"]["pending_reactions"][0]
    for part in path[:-1]:
        current = current[part]
    del current[path[-1]]
    with pytest.raises(ValidationError):
        RuleEvaluationRequest.model_validate(data)


def test_wrong_opportunity_does_not_match_and_capture_needs_source():
    request, _, _, live = reaction_case()
    pending = request.state_snapshot.combat_state.pending_reactions[0]
    assert not matching_conditions(
        pending.declaration(), ReactionOpportunity(ReactionTriggerKind.TAKES_DAMAGE, HERO)
    )
    with pytest.raises(ValueError, match="explicit approved"):
        capture_combat_snapshot(
            live,
            grid=GridScene(width=20, height=3),
            world_version=7,
            combat_id="combat:test",
            inventory_state=request.state_snapshot.inventory_state,
            resource_state=request.state_snapshot.resource_state,
            casting_state=request.state_snapshot.casting_state,
        )


def test_reaction_snapshot_closed_schema_and_no_legacy_imports():
    import ast
    from pathlib import Path

    from dnd5e_engine.evaluation_reaction_state import PendingReactionState

    schema = PendingReactionState.model_json_schema()
    assert set(schema["required"]) == set(schema["properties"])
    for definition in schema["$defs"].values():
        if "properties" in definition:
            assert set(definition["required"]) == set(definition["properties"])
    source = Path(evaluation_reactions.__file__).read_text(encoding="utf8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not any(
                s in (node.module or "") for s in ("orchestrator", "live_", "evaluation_snapshot")
            )
    for forbidden in ("_LiveCombat", "CombatHandle", "_REGISTRY", "fire_reaction"):
        assert forbidden not in source


@pytest.mark.parametrize("seed,expected_heal", [(0, 11), (1, None), (5, 14)])
def test_independent_rng_draw_order_and_last_slot_once(seed, expected_heal):
    import random

    request, loader, _, _ = reaction_case(seed=seed)
    snapshot = request.state_snapshot
    actors = tuple(
        a.model_copy(update={"spell_slots": {**a.spell_slots, 1 if a.entity_id == HERO else 3: 1}})
        if a.entity_id in (HERO, FOE)
        else a
        for a in snapshot.character_states
    )
    pools = tuple(
        p.model_copy(update={"current": 1})
        if p.kind == "spell_slot"
        and ((p.owner_id == HERO and p.level == 1) or (p.owner_id == FOE and p.level == 3))
        else p
        for p in snapshot.resource_state.pools
    )
    snapshot = snapshot.model_copy(
        update={"character_states": actors, "resource_state": ResourceState(pools=pools)}
    )
    result = execute(request.model_copy(update={"state_snapshot": snapshot}), loader)
    assert result.status == "accepted"
    rng = random.Random(seed)
    natural = rng.randint(1, 20)
    save = next(e for e in result.proposed_events if isinstance(e, SaveRolled))
    assert save.natural == natural
    assert save.roll_total == natural + 2
    if expected_heal is not None:
        assert rng.randint(1, 8) + rng.randint(1, 8) + 3 == expected_heal
        assert (
            next(e.amount for e in result.proposed_events if isinstance(e, HealingApplied))
            == expected_heal
        )
    assert result.rng_transition.next_state == RNGState.capture(rng)
    after = apply_healing(snapshot, result)
    assert (
        next(
            p.current
            for p in after.resource_state.pools
            if p.owner_id == FOE and p.pool_id == "spell:3"
        )
        == 0
    )
    assert next(p.current for p in after.resource_state.pools if p.owner_id == HERO) == (
        0 if expected_heal is not None else 1
    )


@pytest.mark.parametrize("dead,incapacitated", [(True, False), (False, True)])
def test_dead_or_incapacitated_reactor_never_pays_or_rolls(dead, incapacitated, monkeypatch):
    request, loader, _, _ = reaction_case()
    snapshot = request.state_snapshot
    actors = list(snapshot.character_states)
    update = {"hp_current": 0, "is_alive": False} if dead else {"conditions": []}
    if incapacitated:
        from dnd5e_engine.evaluation_state import ConditionRecord

        update["conditions"] = [
            ConditionRecord(
                condition="incapacitated",
                source_entity_id="implied:event",
                scope="session",
                duration_rounds=None,
                save_dc=None,
                applied_round=1,
                exhaustion_level=1,
                source_effect_id=None,
            )
        ]
    actors[2] = actors[2].model_copy(update=update)
    snapshot = snapshot.model_copy(update={"character_states": tuple(actors)})
    monkeypatch.setattr(
        "dnd5e_engine.evaluation_rng.RNGState.restore", lambda *a: pytest.fail("RNG restored")
    )
    result = execute(request.model_copy(update={"state_snapshot": snapshot}), loader)
    # Invalid death closure or an unmigrated condition invalidates the whole
    # operation; never silently ignore it to make Counterspell eligible.
    assert result.status == "unsupported"
    assert result.state_delta is None
    assert result.rng_transition is None
    assert not result.proposed_events


def test_npc_triggering_caster_uses_same_con_and_healing():
    request, loader, _, _ = reaction_case(seed=0, npc=True)
    snapshot = request.state_snapshot
    actors = list(snapshot.character_states)
    actors[0] = actors[0].model_copy(update={"entity_type": "Monster"})
    result = execute(
        request.model_copy(
            update={
                "state_snapshot": snapshot.model_copy(update={"character_states": tuple(actors)})
            }
        ),
        loader,
    )
    assert result.status == "accepted", result.error
    save = next(e for e in result.proposed_events if isinstance(e, SaveRolled))
    assert save.modifier == 2
    assert save.dc == 15
    assert next(e.amount for e in result.proposed_events if isinstance(e, HealingApplied)) == 11


def test_complete_recorded_dead_reactor_is_ineligible_and_unchanged():
    from dnd5e_engine.damage_rules import death_record
    from dnd5e_engine.evaluation_actor import combatant
    from dnd5e_engine.events import Death

    request, loader, _, _ = reaction_case()
    snapshot = request.state_snapshot
    actors = list(snapshot.character_states)
    actors[2] = actors[2].model_copy(update={"hp_current": 0, "is_alive": False})
    record = death_record(
        combatant(actors[2]),
        Death(target_id=FOE, reason="damage"),
        location_id=snapshot.scene_state.scene_id,
        killer_id=HERO,
    )
    state = snapshot.combat_state.model_copy(
        update={"dead_ids": {FOE}, "deaths_recorded": [record]}
    )
    snapshot = snapshot.model_copy(
        update={"character_states": tuple(actors), "combat_state": state}
    )
    result = execute(request.model_copy(update={"state_snapshot": snapshot}), loader)
    assert result.status == "accepted", result.error
    after = apply_healing(snapshot, result)
    assert after.character_states[2] == snapshot.character_states[2]
    assert after.combat_state.pending_reactions == state.pending_reactions
    assert not any(isinstance(e, (ReactionTriggered, SaveRolled)) for e in result.proposed_events)
    assert not any(
        isinstance(o, ResourceUpdate) and o.owner_id == FOE for o in result.state_delta.operations
    )
