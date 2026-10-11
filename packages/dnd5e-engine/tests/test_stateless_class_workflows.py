"""Complete class facts survive committed invocation/turn/closure/rest/start loops."""

import asyncio
from copy import deepcopy

import pytest
from dnd5e_srd_data import MemoryAssetLoader

from dnd5e_engine import evaluation_initialization as init
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.evaluation_contracts import CombatClosePayload, CombatIntentPayload, RestPayload
from dnd5e_engine.evaluation_resources import HitDiceResource, ResourceState
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_engine.evaluation_snapshot import capture_combat_snapshot
from dnd5e_engine.evaluation_state import (
    CombatSetupState,
    InventoryEntry,
    InventoryState,
    NonCombatSnapshot,
)
from dnd5e_engine.specs import GridScene
from tests.evaluation_support import FOE, HERO, WEAPON, synthetic_loader
from tests.test_stateless_attack import execute
from tests.test_stateless_closure import consume
from tests.test_stateless_initialization import apply_creation
from tests.test_stateless_item_closure_core import isolate
from tests.test_stateless_reactions import reaction_case
from tests.test_stateless_rest import apply_rest
from tests.test_stateless_spell_feature import apply_healing, healing_case

ALLY = "char:ally"


def workflow_case(feature=False):
    request, old_loader, handle, live = healing_case(feature=feature)
    mundane = synthetic_loader()
    loader = MemoryAssetLoader(
        classes=[old_loader.get_class(s) for s in old_loader.list_slugs("classes")],
        features=[old_loader.get_feature(s) for s in old_loader.list_slugs("features")],
        spells=[old_loader.get_spell("cure-wounds")],
        items=[mundane.get_item(WEAPON)],
    )
    hero, foe = live.initiative
    ally = foe.model_copy(
        update={
            "entity_id": ALLY,
            "entity_type": "Character",
            "name": "Ally",
            "hp_current": 40,
            "hp_max": 40,
            "carried_item_slugs": (WEAPON,),
            "weapon_in_hands": WEAPON,
            "weapon_grip": "one_handed",
            "attack_bonus": 5,
        }
    )
    live.initiative = [hero, ally, foe.model_copy(update={"hp_current": 1, "ac": 1})]
    live.party_ids.add(ALLY)
    live.actor_zone[ALLY] = "2,0"
    live.movement_ledgers[ALLY] = deepcopy(live.movement_ledgers[FOE])
    live.tracked_hp.update({ALLY: 40, FOE: 1})
    live.ruleset_loader = loader
    for values, empty in (
        (live.spell_slots_by_entity, {}),
        (live.pact_slots_by_entity, {}),
        (live.spells_known_by_entity, []),
        (live.custom_counters_by_entity, {}),
    ):
        values[ALLY] = deepcopy(empty)
    cls = "fighter" if feature else "cleric"
    resources = ResourceState(
        pools=(
            *request.state_snapshot.resource_state.pools,
            HitDiceResource(
                kind="hit_dice",
                owner_id=HERO,
                pool_id="hd:" + cls,
                current=0,
                maximum=1,
                class_slug=cls,
                die_size=10 if feature else 8,
            ),
        )
    )
    inventory = InventoryState(
        entries=(
            InventoryEntry(
                instance_id="ally:weapon",
                owner_id=ALLY,
                item_slug=WEAPON,
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
        casting_state=request.state_snapshot.casting_state,
    )
    request = request.model_copy(
        update={
            "state_snapshot": snapshot,
            "ruleset_binding": ruleset_binding(loader),
            "payload": request.payload.model_copy(update={"target_id": HERO}),
        }
    )
    return request, loader, handle, live


@pytest.mark.parametrize("feature", [False, True])
def test_complete_class_encounter_rest_new_encounter_workflow(feature, monkeypatch):
    request, loader, handle, live = workflow_case(feature)
    original = deepcopy(request)
    steps = [(HERO, request.payload)]
    if feature:
        steps.append((HERO, CombatIntentPayload(intent_type="pass")))
    steps += [
        (ALLY, CombatIntentPayload(intent_type="pass")),
        (FOE, CombatIntentPayload(intent_type="pass")),
        (HERO, CombatIntentPayload(intent_type="move", target_zone_id="0,1")),
        (HERO, CombatIntentPayload(intent_type="pass")),
        (ALLY, CombatIntentPayload(intent_type="attack", target_id=FOE, weapon_id=WEAPON)),
    ]
    # Execute the same complete input on isolated Legacy state for differential evidence.
    for actor, payload in steps:
        asyncio.run(orch.submit_player_intent(handle, actor, payload))
    snapshot = request.state_snapshot
    rng = request.rng_context
    with monkeypatch.context() as scoped:
        isolate(scoped)
        for index, (actor, payload) in enumerate(steps):
            current = request.model_copy(
                update={
                    "command_id": f"workflow:{index}",
                    "actor_id": actor,
                    "payload": payload,
                    "state_snapshot": snapshot,
                    "rng_context": rng,
                }
            )
            result = execute(current, loader)
            assert result.status == "accepted", result.error
            assert execute(current, loader) == result
            snapshot = apply_healing(snapshot, result)
            rng = rng.model_copy(update={"state": result.rng_transition.next_state})
    legacy = capture_combat_snapshot(
        live,
        grid=GridScene(width=3, height=3),
        world_version=7,
        combat_id=snapshot.combat_state.combat_id,
        inventory_state=snapshot.inventory_state,
        resource_state=snapshot.resource_state,
        casting_state=snapshot.casting_state,
    )
    assert snapshot == legacy
    assert rng.state == RNGState.capture(live.rng)
    assert snapshot.combat_state.round_number == 3
    assert snapshot.combat_state.dead_ids == {FOE}
    assert snapshot.combat_state.pending_reactions == []
    assert snapshot.effect_states == ()

    close = request.model_copy(
        update={
            "state_snapshot": snapshot,
            "rng_context": rng,
            "operation_kind": "combat.close",
            "payload": CombatClosePayload(kind="combat.close"),
        }
    )
    with monkeypatch.context() as scoped:
        isolate(scoped)
        closed = execute(close, loader)
        assert closed.status == "accepted", closed.error
        ended, _ = consume(snapshot, closed)
        assert ended.combat_state.ended
        # Host chooses the relevant actor collection after the legal combat.close boundary.
        noncombat = NonCombatSnapshot(
            **ended.model_dump(
                exclude={"snapshot_kind", "combat_state", "character_states", "inventory_state"}
            ),
            snapshot_kind="non_combat",
            combat_setup=None,
            character_states=(ended.character_states[0],),
            inventory_state=InventoryState(entries=()),
        )
        rest_request = request.model_copy(
            update={
                "state_snapshot": noncombat,
                "rng_context": rng,
                "operation_kind": "rules.rest",
                "payload": RestPayload(
                    kind="rules.rest", target_id=HERO, rest_type="long", hit_dice=()
                ),
            }
        )
        rested = execute(rest_request, loader)
        assert rested.status == "accepted", rested.error
        after_rest = apply_rest(noncombat, rested)
        assert after_rest.character_states[0].hp_current == 40
        assert all(p.current == p.maximum for p in after_rest.resource_state.pools)
        assert rested.rng_transition.next_state == rng.state
        with pytest.raises(AssertionError):
            apply_rest(after_rest, rested)
        fresh_foe = original.state_snapshot.character_states[-1].model_copy(
            update={
                "entity_id": "npc:second",
                "hp_current": 10,
                "hp_max": 10,
            }
        )
        previous = ended.combat_state
        setup = CombatSetupState(
            combat_id="combat:second",
            party_ids=(HERO,),
            encounter_ids=(fresh_foe.entity_id,),
            roll_order=(HERO, fresh_foe.entity_id),
            actor_zone={HERO: "0,0", fresh_foe.entity_id: "1,0"},
            fixed_initiative={HERO: 20, fresh_foe.entity_id: 1},
            initiative_modifiers={HERO: None, fresh_foe.entity_id: None},
            surprised_ids=set(),
            xp_value_by_entity={fresh_foe.entity_id: 0},
            opportunity_attack_weapons={},
            npc_stat_blocks=(),
            timed_activities=previous.timed_activities,
            persistent_areas=previous.persistent_areas,
            combat_objects=previous.combat_objects,
        )
        start = init.CombatInitializationCandidateRequest(
            schema_version=request.schema_version,
            session_id=request.session_id,
            command_id="workflow:start",
            payload=init.CombatInitializationCandidatePayload(kind="combat.init.local-candidate"),
            ruleset_binding=request.ruleset_binding,
            rng_context=rng,
            state_snapshot=after_rest.model_copy(
                update={
                    "combat_setup": setup,
                    "character_states": (*after_rest.character_states, fresh_foe),
                }
            ),
        )
        created = init.evaluate_combat_initialization_candidate(start, loader=loader)
        assert created.status == "accepted", created.error
        next_combat = apply_creation(start, created)
        again = execute(
            request.model_copy(
                update={
                    "state_snapshot": next_combat,
                    "rng_context": rng,
                    "payload": request.payload,
                }
            ),
            loader,
        )
        assert again.status == "accepted", again.error
        assert apply_healing(next_combat, again).resource_state.pools[0].current == 1
    assert request == original


def test_complete_evoker_turn_reset_and_long_rest(monkeypatch):
    request, loader, _, _ = reaction_case()
    snapshot = request.state_snapshot
    # Authorized pending Counterspell remains unsupported for unrelated Pass.
    denied = execute(
        request.model_copy(update={"payload": CombatIntentPayload(intent_type="pass")}), loader
    )
    assert denied.status == "unsupported"
    # A real triggering cast consumes the declaration; all class facts remain.
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    snapshot = apply_healing(snapshot, result)
    rng = request.rng_context.model_copy(update={"state": result.rng_transition.next_state})
    isolate(monkeypatch)
    for _ in range(3):
        actor = snapshot.combat_state.initiative_ids[snapshot.combat_state.current_turn_index]
        result = execute(
            request.model_copy(
                update={
                    "state_snapshot": snapshot,
                    "rng_context": rng,
                    "actor_id": actor,
                    "payload": CombatIntentPayload(intent_type="pass"),
                }
            ),
            loader,
        )
        assert result.status == "accepted", result.error
        snapshot = apply_healing(snapshot, result)
    wizard = next(a for a in snapshot.character_states if a.entity_id == FOE)
    assert wizard.reaction_available
    assert wizard.subclass_slug == "evoker"
    noncombat = NonCombatSnapshot(
        **snapshot.model_dump(exclude={"snapshot_kind", "combat_state", "resource_state"}),
        snapshot_kind="non_combat",
        combat_setup=None,
        resource_state=ResourceState(
            pools=(
                *snapshot.resource_state.pools,
                HitDiceResource(
                    kind="hit_dice",
                    owner_id=FOE,
                    pool_id="hd:wizard",
                    current=0,
                    maximum=5,
                    class_slug="wizard",
                    die_size=6,
                ),
            )
        ),
    )
    rest_request = request.model_copy(
        update={
            "actor_id": FOE,
            "state_snapshot": noncombat,
            "rng_context": rng,
            "operation_kind": "rules.rest",
            "payload": RestPayload(kind="rules.rest", target_id=FOE, rest_type="long", hit_dice=()),
        }
    )
    rested = execute(rest_request, loader)
    assert rested.status == "accepted", rested.error
    after = apply_rest(noncombat, rested)
    assert all(p.current == p.maximum for p in after.resource_state.pools if p.owner_id == FOE)
    assert (
        next(a for a in after.character_states if a.entity_id == FOE).granted_features
        == wizard.granted_features
    )


@pytest.mark.parametrize(
    "change",
    [
        {"granted_features": ("spellcasting", "unknown-hook")},
        {"classes": {"cleric": 2}, "character_level": 2},
        {"species_slug": "elf"},
        {"fighting_styles": ("archery",)},
        {"custom_counters": {"unreviewed:turn": {"spent": 1}}},
    ],
)
def test_unreviewed_class_dependency_refuses_before_rng(change, monkeypatch):
    request, loader, _, _ = healing_case()
    actors = request.state_snapshot.character_states
    request = request.model_copy(
        update={
            "payload": CombatIntentPayload(intent_type="pass"),
            "state_snapshot": request.state_snapshot.model_copy(
                update={
                    "character_states": (actors[0].model_copy(update=change), *actors[1:]),
                }
            ),
        }
    )
    isolate(monkeypatch)
    monkeypatch.setattr(RNGState, "restore", lambda *a: pytest.fail("unreviewed path drew RNG"))
    result = execute(request, loader)
    assert result.status == "unsupported"
    assert result.state_delta is result.rng_transition is None
    assert result.proposed_events == ()
