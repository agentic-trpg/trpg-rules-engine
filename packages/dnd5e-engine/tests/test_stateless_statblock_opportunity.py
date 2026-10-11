"""Canonical Ogre OA: independent SRD expectations, actual Legacy parity and isolation."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from random import Random

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from pydantic import ValidationError
from pydantic_core import to_json

from dnd5e_engine import evaluation_opportunity, live_reactions
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.evaluation_contracts import (
    SCHEMA_VERSION,
    CombatIntentPayload,
    RuleEvaluationRequest,
    RuleEvaluationResult,
)
from dnd5e_engine.evaluation_reaction_state import StatBlockOpportunityAttackState
from dnd5e_engine.evaluation_rng import RNGContext, RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_engine.evaluation_snapshot import capture_combat_snapshot, capture_evaluation_snapshot
from dnd5e_engine.evaluation_state import CombatSnapshot, InventoryState
from dnd5e_engine.events import ActorMoved, AttackRolled, DamageApplied
from dnd5e_engine.lib_loader import scoped_lib_loader
from dnd5e_engine.specs import EncounterMemberSpec, GridScene, PartyMemberSpec, WallSegment
from tests.test_stateless_attack import execute
from tests.test_stateless_item_closure_core import forbidden, isolate
from tests.test_stateless_opportunity import apply_opportunity

MOVER, OGRE = "actor:r29-mover", "npc:r29-ogre"
GRID = GridScene(width=12, height=4)


def ogre_case(*, seed=0, npc=False, hp=40, temp=0, ac=1, defense=None, destination="3,0"):
    monster = BundledAssetLoader().get_monster("ogre")
    loader = MemoryAssetLoader(monsters=[monster])
    with scoped_lib_loader(loader):
        started = asyncio.run(
            orch.start_combat(
                session_id="session:r29",
                rng_seed=seed,
                grid_scene=GRID,
                party=[
                    PartyMemberSpec(
                        entity_id=MOVER,
                        name="Mover",
                        initiative=20,
                        hp_current=hp,
                        hp_max=40,
                        ac=ac,
                        zone_id="1,0",
                    )
                ],
                encounter=[
                    EncounterMemberSpec(
                        entity_id=OGRE,
                        entity_type="NPC",
                        name="Canonical Ogre",
                        initiative=1,
                        hp_current=monster.hp,
                        hp_max=monster.hp,
                        ac=monster.ac,
                        base_speed=monster.movement.walk,
                        creature_type=monster.creature_type,
                        physical_resistances_nonmagical_only=False,
                        monster_template_slug=monster.slug,
                        zone_id="0,0",
                    )
                ],
            )
        )
    live = orch._get_live(started.handle)
    live.initiative = [
        a.model_copy(
            update={
                "weapon_in_hands": None,
                "weapon_grip": "none",
                "other_hand_occupied": False,
                "weapon_mastery_slugs": (),
                "granted_features": (),
                **(
                    {
                        "entity_type": "NPC" if npc else "Character",
                        "temp_hp": temp,
                        **({defense: ["bludgeoning"]} if defense else {}),
                    }
                    if a.entity_id == MOVER
                    else {}
                ),
            }
        )
        for a in live.initiative
    ]
    live.tracked_temp_hp[MOVER] = temp
    live.rng.seed(seed)
    live.event_log.clear()
    while not live.event_queue.empty():
        live.event_queue.get_nowait()
    snapshot = capture_combat_snapshot(
        live,
        grid=GRID,
        world_version=7,
        combat_id="combat:r29",
        inventory_state=InventoryState(entries=()),
        resource_state=None,
        casting_state=None,
    )
    declaration = StatBlockOpportunityAttackState(
        source_kind="stat_block_opportunity_attack",
        owner_id=OGRE,
        mover_id=MOVER,
        monster_slug="ogre",
        stat_block_action_id="greatclub",
        trigger="voluntary_leave_reach",
        expiry="owner_next_turn_start",
    )
    snapshot = snapshot.model_copy(
        update={
            "combat_state": snapshot.combat_state.model_copy(
                update={"pending_reactions": [declaration]}
            )
        }
    )
    request = RuleEvaluationRequest(
        schema_version=SCHEMA_VERSION,
        session_id=live.session_id,
        command_id="command:r29",
        operation_kind="combat.intent",
        actor_id=MOVER,
        payload=CombatIntentPayload(intent_type="move", target_zone_id=destination),
        state_snapshot=snapshot,
        ruleset_binding=ruleset_binding(loader),
        rng_context=RNGContext(stream_id="main", version=3, state=RNGState.capture(live.rng)),
    )
    return request, started.handle, live, loader


def isolated(monkeypatch):
    isolate(monkeypatch)
    for name in (
        "_resolve_monster_attack_activities",
        "advance_monster_turn",
        "_stat_block_opportunity_attack",
        "_resolve_opportunity_attack",
        "attach_reaction_hooks",
    ):
        monkeypatch.setattr(orch, name, forbidden)
    monkeypatch.setattr(live_reactions, "attach_reaction_hooks", forbidden)


def recapture(request, live, **updates):
    captured = capture_evaluation_snapshot(
        live,
        request.state_snapshot,
        GridScene.model_validate(request.state_snapshot.scene_state.grid.model_dump()),
    )
    captured = captured.model_copy(
        update={
            "combat_state": captured.combat_state.model_copy(
                update={"pending_reactions": request.state_snapshot.combat_state.pending_reactions}
            )
        }
    )
    return request.model_copy(update={"state_snapshot": captured, **updates})


def parity(request, handle, live, loader, result):
    offset = len(live.event_log)
    with scoped_lib_loader(loader):
        asyncio.run(orch.submit_player_intent(handle, request.actor_id, request.payload))
    after = apply_opportunity(request.state_snapshot, result)
    expected = capture_evaluation_snapshot(
        live,
        request.state_snapshot,
        GridScene.model_validate(request.state_snapshot.scene_state.grid.model_dump()),
    )
    # Legacy has no trusted consent DTO. Only this authorization fact is supplied
    # from the new result; every mechanical field, event and RNG is compared.
    expected = expected.model_copy(
        update={
            "combat_state": expected.combat_state.model_copy(
                update={"pending_reactions": after.combat_state.pending_reactions}
            )
        }
    )
    assert after == expected
    assert result.proposed_events == tuple(live.event_log[offset:])
    assert result.rng_transition.next_state == RNGState.capture(live.rng)
    assert RuleEvaluationResult.model_validate_json(result.model_dump_json()) == result
    return after


@pytest.mark.parametrize("npc", [False, True])
@pytest.mark.parametrize(
    "seed,ac,hp,temp",
    [(0, 1, 40, 0), (1, 99, 40, 0), (5, 1, 40, 0), (31, 1, 40, 0), (0, 1, 1, 0), (0, 1, 1, 20)],
)
@pytest.mark.parametrize(
    "defense", [None, "damage_resistances", "damage_immunities", "damage_vulnerabilities"]
)
def test_real_ogre_oa_full_state_events_rng(npc, seed, ac, hp, temp, defense, monkeypatch):
    request, handle, live, loader = ogre_case(
        npc=npc, seed=seed, ac=ac, hp=hp, temp=temp, defense=defense
    )
    before = deepcopy(request)
    with monkeypatch.context() as scoped:
        isolated(scoped)
        result = execute(request, loader)
    assert result.status == "accepted", result.error
    after = parity(request, handle, live, loader, result)
    roll = next(e for e in result.proposed_events if isinstance(e, AttackRolled))
    independent = Random(seed)
    natural = independent.randint(1, 20)
    assert (roll.attacker_id, roll.target_id, roll.natural, roll.modifier, roll.roll_total) == (
        OGRE,
        MOVER,
        natural,
        6,
        natural + 6,
    )
    hit = natural != 1 and (natural == 20 or natural + 6 >= ac)
    assert roll.is_hit is hit
    assert roll.is_crit is (natural == 20)
    assert roll.is_opportunity_attack
    damage = (
        sum(independent.randint(1, 8) for _ in range(4 if natural == 20 else 2)) + 4 if hit else 0
    )
    if defense == "damage_resistances":
        damage //= 2
    elif defense == "damage_immunities":
        damage = 0
    elif defense == "damage_vulnerabilities":
        damage *= 2
    packets = [e for e in result.proposed_events if isinstance(e, DamageApplied)]
    assert sum(e.amount for e in packets) == damage
    assert all(e.source_actor_id == OGRE and OGRE in e.damage_instance_id for e in packets)
    mover = next(a for a in after.character_states if a.entity_id == MOVER)
    owner = next(a for a in after.character_states if a.entity_id == OGRE)
    assert mover.temp_hp == max(0, temp - damage)
    assert mover.hp_current == max(0, hp - max(0, damage - temp))
    assert not owner.reaction_available
    assert owner.action_available
    assert owner.bonus_action_available
    assert owner.attack_rolls_made_this_turn == 0
    assert not after.combat_state.pending_reactions
    assert after.combat_state.actor_zone[MOVER] == ("1,0" if mover.hp_current == 0 else "3,0")
    assert after.combat_state.movement_ledgers[MOVER].spent_ft == (
        0 if mover.hp_current == 0 else 10
    )
    assert request == before


@pytest.mark.parametrize("reason", ["inside", "spent", "disengage", "outside", "wall"])
def test_nontrigger_retains_declaration_then_owner_turn_expires(reason, monkeypatch):
    request, handle, live, loader = ogre_case(destination="1,1" if reason == "inside" else "3,0")
    if reason == "outside":
        live.actor_zone[OGRE] = "10,3"
    if reason == "wall":
        grid = GRID.model_copy(update={"wall_segments": [WallSegment(x1=1, y1=0, x2=1, y2=1)]})
        from dnd5e_engine.spatial import GridTopology

        live.topology = GridTopology(grid)
        request = request.model_copy(
            update={
                "state_snapshot": request.state_snapshot.model_copy(
                    update={
                        "scene_state": request.state_snapshot.scene_state.model_copy(
                            update={
                                "grid": type(
                                    request.state_snapshot.scene_state.grid
                                ).model_validate(grid.model_dump())
                            }
                        )
                    }
                )
            }
        )
    live.initiative = [
        a.model_copy(
            update={
                **(
                    {"reaction_available": False}
                    if a.entity_id == OGRE and reason == "spent"
                    else {}
                ),
                **(
                    {"disengaging_this_turn": True}
                    if a.entity_id == MOVER and reason == "disengage"
                    else {}
                ),
            }
        )
        for a in live.initiative
    ]
    request = recapture(request, live)
    with monkeypatch.context() as scoped:
        isolated(scoped)
        result = execute(request, loader)
    assert result.status == "accepted", result.error
    after = parity(request, handle, live, loader, result)
    assert not result.rng_transition.state_changed
    assert (
        after.combat_state.pending_reactions
        == request.state_snapshot.combat_state.pending_reactions
    )
    request = request.model_copy(
        update={
            "state_snapshot": after,
            "payload": CombatIntentPayload(intent_type="pass"),
            "rng_context": request.rng_context.model_copy(
                update={"state": result.rng_transition.next_state}
            ),
        }
    )
    with monkeypatch.context() as scoped:
        isolated(scoped)
        result = execute(request, loader)
    assert result.status == "accepted", result.error
    after = parity(request, handle, live, loader, result)
    assert not after.combat_state.pending_reactions
    assert after.combat_state.initiative_ids[after.combat_state.current_turn_index] == OGRE


def test_completed_prefix_stops_on_actual_npc_death(monkeypatch):
    request, handle, live, loader = ogre_case(npc=True, hp=1)
    # A party NPC uses the original party death policy. Make this an actual
    # massive-damage death, independently: 12 damage exceeds 1 HP + 1 max HP.
    live.initiative = [
        a.model_copy(update={"hp_max": 1}) if a.entity_id == MOVER else a for a in live.initiative
    ]
    live.actor_zone = {MOVER: "0,0", OGRE: "1,1"}
    request = recapture(request, live)
    with monkeypatch.context() as scoped:
        isolated(scoped)
        result = execute(request, loader)
    assert result.status == "accepted", result.error
    after = parity(request, handle, live, loader, result)
    assert MOVER in after.combat_state.dead_ids
    assert after.combat_state.actor_zone[MOVER] == "2,0"
    assert after.combat_state.movement_ledgers[MOVER].spent_ft == 10
    moved = [e for e in result.proposed_events if isinstance(e, ActorMoved)]
    assert len(moved) == 1
    assert moved[0].path == ("0,0", "1,0", "2,0")
    kinds = [e.type for e in result.proposed_events]
    assert kinds.index("actor_moved") < kinds.index("attack_rolled") < kinds.index("death")


@pytest.mark.parametrize(
    "change",
    [
        "strength",
        "ac",
        "reach",
        "grant",
        "counter",
        "action",
        "monster",
        "mover",
        "missing_consent",
        "binding_missing",
        "competing",
    ],
)
def test_unknown_dependencies_refuse_before_rng(change, monkeypatch):
    request, _, _, loader = ogre_case()
    snapshot = request.state_snapshot
    actors = list(snapshot.character_states)
    owner = next(i for i, a in enumerate(actors) if a.entity_id == OGRE)
    state = snapshot.combat_state
    declaration = state.pending_reactions[0]
    if change in {"strength", "ac", "reach", "grant", "counter"}:
        field, value = {
            "strength": ("strength", 20),
            "ac": ("ac", 12),
            "reach": ("melee_reach_ft", 10),
            "grant": ("granted_features", ("unknown",)),
            "counter": ("custom_counters", {"unknown": {"value": 1, "max": 1}}),
        }[change]
        actors[owner] = actors[owner].model_copy(update={field: value})
    elif change in {"action", "monster", "mover"}:
        field, value = {
            "action": ("stat_block_action_id", "javelin"),
            "monster": ("monster_slug", "bandit"),
            "mover": ("mover_id", OGRE),
        }[change]
        state = state.model_copy(
            update={"pending_reactions": [declaration.model_copy(update={field: value})]}
        )
    elif change == "missing_consent":
        state = state.model_copy(update={"pending_reactions": []})
    elif change == "binding_missing":
        state = state.model_copy(update={"monster_slug_by_entity": {}})
    else:
        state = state.model_copy(
            update={
                "pending_reactions": [
                    declaration,
                    declaration.model_copy(update={"owner_id": MOVER, "mover_id": OGRE}),
                ]
            }
        )
    snapshot = CombatSnapshot.model_validate_json(
        snapshot.model_copy(
            update={"character_states": tuple(actors), "combat_state": state}
        ).model_dump_json()
    )
    request = request.model_copy(update={"state_snapshot": snapshot})
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("unsupported RNG"))
    result = execute(request, loader)
    assert result.status == "unsupported"
    assert result.state_delta is None
    assert result.rng_transition is None
    assert not result.proposed_events


@pytest.mark.parametrize("consent", [False, True])
@pytest.mark.parametrize("kind", ["recharge", "limited", "multiattack", "legendary", "trait"])
def test_unreviewed_template_refused_even_without_consent(kind, consent, monkeypatch):
    request, _, _, loader = ogre_case()
    monster = loader.get_monster("ogre")
    action = monster.actions[0]
    if kind in {"recharge", "limited"}:
        action = action.model_copy(
            update={"recharge": "5-6"} if kind == "recharge" else {"uses_per_day": 1}
        )
        monster = monster.model_copy(update={"actions": [action, *monster.actions[1:]]})
    elif kind == "multiattack":
        monster = monster.model_copy(
            update={
                "actions": [action.model_copy(update={"slug": "multiattack"}), *monster.actions[1:]]
            }
        )
    elif kind == "legendary":
        monster = monster.model_copy(update={"legendary_action_uses": 3})
    else:
        monster = monster.model_copy(
            update={
                "special_abilities": BundledAssetLoader().get_monster("commoner").special_abilities
            }
        )
    loader = MemoryAssetLoader(monsters=[monster])
    if not consent:
        request = request.model_copy(
            update={
                "state_snapshot": request.state_snapshot.model_copy(
                    update={
                        "combat_state": request.state_snapshot.combat_state.model_copy(
                            update={"pending_reactions": []}
                        )
                    }
                )
            }
        )
    request = request.model_copy(update={"ruleset_binding": ruleset_binding(loader)})
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("template RNG"))
    assert execute(request, loader).status == "unsupported"


def test_replay_guards_fault_and_cancellation(monkeypatch):
    request, _, live, loader = ogre_case()
    before = deepcopy(request)
    isolated(monkeypatch)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(lambda _: execute(request, loader), range(8))) == [result] * 8
    after = apply_opportunity(request.state_snapshot, result)
    with pytest.raises(AssertionError):
        apply_opportunity(after, result)
    with pytest.raises(AssertionError):
        apply_opportunity(request.state_snapshot.model_copy(update={"world_version": 8}), result)
    real = evaluation_opportunity.resolve_activity
    for fault in (RuntimeError("after resolver"), asyncio.CancelledError()):

        def fail(*args, fault=fault, **kwargs):
            real(*args, **kwargs)
            raise fault

        monkeypatch.setattr(evaluation_opportunity, "resolve_activity", fail)
        with pytest.raises(type(fault)):
            execute(request, loader)
        assert request == before
        assert RNGState.capture(live.rng) == request.rng_context.state


def test_consumed_oa_composes_move_pass_reset_monster_move_attack(monkeypatch):
    request, handle, live, loader = ogre_case()
    commands = [
        (MOVER, request.payload),
        (MOVER, CombatIntentPayload(intent_type="move", target_zone_id="4,0")),
        (MOVER, CombatIntentPayload(intent_type="pass")),
        (OGRE, CombatIntentPayload(intent_type="move", target_zone_id="3,0")),
        (
            OGRE,
            CombatIntentPayload(
                intent_type="attack", stat_block_action_id="greatclub", target_id=MOVER
            ),
        ),
    ]
    for index, (actor, intent) in enumerate(commands):
        request = request.model_copy(update={"actor_id": actor, "payload": intent})
        with monkeypatch.context() as scoped:
            isolated(scoped)
            result = execute(request, loader)
        assert result.status == "accepted", result.error
        after = parity(request, handle, live, loader, result)
        assert not after.combat_state.pending_reactions
        if index == 1:
            assert not result.rng_transition.state_changed
        if index == 2:
            assert next(a for a in after.character_states if a.entity_id == OGRE).reaction_available
        request = request.model_copy(
            update={
                "state_snapshot": after,
                "rng_context": request.rng_context.model_copy(
                    update={"state": result.rng_transition.next_state}
                ),
            }
        )


@pytest.mark.parametrize("npc", [False, True])
def test_complete_equipped_cleric_mover_then_move_after_consent_consumption(npc, monkeypatch):
    from tests.evaluation_support import HERO
    from tests.test_stateless_equipped_class_attack import equipped_case

    request, _, _, loader = ogre_case()
    equipped, gear_loader, _, _ = equipped_case(npc=npc)
    source = equipped.state_snapshot
    hero = next(a for a in source.character_states if a.entity_id == HERO)
    snapshot = request.state_snapshot
    hero = hero.model_copy(update={"entity_id": MOVER, "initiative": 20})
    loader = MemoryAssetLoader(
        monsters=[loader.get_monster("ogre")],
        items=[gear_loader.get_item(s) for s in gear_loader.list_slugs("items")],
        classes=[gear_loader.get_class(s) for s in gear_loader.list_slugs("classes")],
        features=[gear_loader.get_feature(s) for s in gear_loader.list_slugs("features")],
        spells=[gear_loader.get_spell(s) for s in gear_loader.list_slugs("spells")],
    )
    snapshot = snapshot.model_copy(
        update={
            "character_states": tuple(
                hero if a.entity_id == MOVER else a for a in snapshot.character_states
            ),
            "inventory_state": source.inventory_state.model_copy(
                update={
                    "entries": tuple(
                        e.model_copy(update={"owner_id": MOVER})
                        for e in source.inventory_state.entries
                    )
                }
            ),
            "resource_state": source.resource_state.model_copy(
                update={
                    "pools": tuple(
                        p.model_copy(update={"owner_id": MOVER})
                        for p in source.resource_state.pools
                    )
                }
            ),
            "casting_state": source.casting_state.model_copy(
                update={
                    "actors": tuple(
                        a.model_copy(update={"actor_id": MOVER})
                        for a in source.casting_state.actors
                    )
                }
            ),
        }
    )
    snapshot = CombatSnapshot.model_validate_json(snapshot.model_dump_json())
    request = request.model_copy(
        update={"state_snapshot": snapshot, "ruleset_binding": ruleset_binding(loader)}
    )
    original = deepcopy(request)
    isolated(monkeypatch)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    after = apply_opportunity(snapshot, result)
    assert after.inventory_state == snapshot.inventory_state
    assert after.resource_state == snapshot.resource_state
    assert after.casting_state == snapshot.casting_state
    assert (
        next(a for a in after.character_states if a.entity_id == MOVER).granted_features
        == hero.granted_features
    )
    request = request.model_copy(
        update={
            "state_snapshot": after,
            "payload": CombatIntentPayload(intent_type="move", target_zone_id="4,0"),
            "rng_context": request.rng_context.model_copy(
                update={"state": result.rng_transition.next_state}
            ),
        }
    )
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert not result.rng_transition.state_changed
    assert apply_opportunity(after, result).combat_state.actor_zone[MOVER] == "4,0"
    assert original.state_snapshot == snapshot


@pytest.mark.parametrize("field", list(StatBlockOpportunityAttackState.model_fields))
@pytest.mark.parametrize("mode", ["python", "json"])
def test_declaration_fields_required_and_strict(field, mode):
    request, _, _, _ = ogre_case()
    declaration = request.state_snapshot.combat_state.pending_reactions[0]
    value = declaration.model_dump()
    del value[field]
    validate = (
        StatBlockOpportunityAttackState.model_validate_json
        if mode == "json"
        else StatBlockOpportunityAttackState.model_validate
    )
    with pytest.raises(ValidationError):
        validate(to_json(value) if mode == "json" else value)
    value = declaration.model_dump()
    value[field] = False
    with pytest.raises(ValidationError):
        validate(to_json(value) if mode == "json" else value)


def test_versions_extra_fields_and_ordinary_ogre_attack(monkeypatch):
    request, handle, live, loader = ogre_case()
    value = request.model_dump(mode="python")
    value["schema_version"] = "engine-evaluation/18"
    with pytest.raises(ValidationError):
        RuleEvaluationRequest.model_validate_json(to_json(value))
    value = request.state_snapshot.model_dump(mode="python")
    value["snapshot_schema_version"] = "engine-snapshot/13"
    with pytest.raises(ValidationError):
        CombatSnapshot.model_validate_json(to_json(value))
    from dnd5e_engine.evaluation_availability import ActionAvailabilityRequest

    with pytest.raises(ValidationError):
        ActionAvailabilityRequest.model_validate_json(
            to_json(
                {
                    "schema_version": "engine-availability/11",
                    "session_id": request.session_id,
                    "operation_kind": request.operation_kind,
                    "actor_id": request.actor_id,
                    "payload": request.payload,
                    "state_snapshot": request.state_snapshot,
                    "ruleset_binding": request.ruleset_binding,
                }
            )
        )
    declaration = request.state_snapshot.combat_state.pending_reactions[0]
    with pytest.raises(ValidationError):
        StatBlockOpportunityAttackState.model_validate(
            {**declaration.model_dump(), "trusted": True}
        )
    live.current_turn_index = 1
    live.current_actor_id = OGRE
    request = recapture(
        request,
        live,
        actor_id=OGRE,
        payload=CombatIntentPayload(
            intent_type="attack", stat_block_action_id="greatclub", target_id=MOVER
        ),
    )
    request = request.model_copy(
        update={
            "state_snapshot": request.state_snapshot.model_copy(
                update={
                    "combat_state": request.state_snapshot.combat_state.model_copy(
                        update={"pending_reactions": []}
                    )
                }
            )
        }
    )
    with monkeypatch.context() as scoped:
        isolated(scoped)
        result = execute(request, loader)
    assert result.status == "accepted", result.error
    parity(request, handle, live, loader, result)
    assert next(e for e in result.proposed_events if isinstance(e, AttackRolled)).modifier == 6


@pytest.mark.parametrize("mode", ["validation", "serialization"])
def test_nested_declaration_schema_exposes_exact_required_facts(mode):
    root = CombatSnapshot.model_json_schema(mode=mode)
    node = root["$defs"]["StatBlockOpportunityAttackState"]
    assert set(node["required"]) == set(StatBlockOpportunityAttackState.model_fields)
    assert node["additionalProperties"] is False
