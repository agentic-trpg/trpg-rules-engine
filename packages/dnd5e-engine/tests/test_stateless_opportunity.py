"""Authorized OA: independent rule expectations, Legacy parity and atomicity."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from random import Random

import pytest
from pydantic import ValidationError

from dnd5e_engine import evaluation_opportunity
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, RuleEvaluationResult
from dnd5e_engine.evaluation_delta import PendingReactionUpdate
from dnd5e_engine.evaluation_reaction_state import OpportunityAttackState
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_snapshot import capture_evaluation_snapshot
from dnd5e_engine.evaluation_state import CombatSnapshot, InventoryEntry, InventoryState
from dnd5e_engine.events import ActorMoved, AttackRolled, DamageApplied, IntentSubmitted
from dnd5e_engine.specs import GridScene
from tests.evaluation_support import FOE, HERO, WEAPON
from tests.test_stateless_attack import apply_delta, execute
from tests.test_stateless_item_closure_core import isolate
from tests.test_stateless_movement import move_case


def opportunity_case(*, npc=True, seed=0, hp=40, ac=1, temp=0, destination="3,0"):
    request, handle, live = move_case(npc=npc, destination=destination)
    mover, owner = (FOE, HERO) if npc else (HERO, FOE)
    live.actor_zone = {owner: "0,0", mover: "1,0"}
    live.initiative = [
        a.model_copy(
            update={
                "entity_type": "Character" if a.entity_id == owner else a.entity_type,
                "granted_features": (),
                **(
                    {
                        "carried_item_slugs": (WEAPON,),
                        "weapon_in_hands": WEAPON,
                        "weapon_grip": "one_handed",
                        "strength": 16,
                        "attack_bonus": 5,
                    }
                    if a.entity_id == owner
                    else {"hp_current": hp, "hp_max": 40, "ac": ac, "temp_hp": temp}
                ),
            }
        )
        for a in live.initiative
    ]
    live.opportunity_attack_weapons[owner] = WEAPON
    live.tracked_hp = {a.entity_id: a.hp_current for a in live.initiative}
    live.tracked_temp_hp = {a.entity_id: a.temp_hp for a in live.initiative}
    live.rng.seed(seed)
    snapshot = capture_evaluation_snapshot(
        live, request.state_snapshot, GridScene(width=12, height=4)
    )
    declaration = OpportunityAttackState(
        source_kind="opportunity_attack",
        owner_id=owner,
        mover_id=mover,
        weapon_id=WEAPON,
        two_handed=False,
        trigger="voluntary_leave_reach",
        expiry="owner_next_turn_start",
    )
    snapshot = snapshot.model_copy(
        update={
            "inventory_state": InventoryState(
                entries=(
                    InventoryEntry(
                        instance_id="oa:weapon",
                        owner_id=owner,
                        item_slug=WEAPON,
                        quantity=1,
                        charges_remaining_per_unit=0,
                        accessible=True,
                    ),
                )
            ),
            "combat_state": snapshot.combat_state.model_copy(
                update={"pending_reactions": [declaration]}
            ),
        }
    )
    return (
        request.model_copy(
            update={
                "state_snapshot": snapshot,
                "rng_context": request.rng_context.model_copy(
                    update={"state": RNGState.capture(live.rng)}
                ),
            }
        ),
        handle,
        live,
    )


def apply_opportunity(snapshot, result):
    operations = [o for o in result.state_delta.operations if isinstance(o, PendingReactionUpdate)]
    for op in operations:
        assert op.combat_id == snapshot.combat_state.combat_id
        assert op.expected == tuple(snapshot.combat_state.pending_reactions)
    mechanical = result.model_copy(
        update={
            "state_delta": result.state_delta.model_copy(
                update={
                    "operations": tuple(
                        o
                        for o in result.state_delta.operations
                        if not isinstance(o, PendingReactionUpdate)
                    )
                }
            )
        }
    )
    after = apply_delta(snapshot, mechanical)
    if operations:
        after = after.model_copy(
            update={
                "combat_state": after.combat_state.model_copy(
                    update={"pending_reactions": list(operations[0].value)}
                )
            }
        )
    return CombatSnapshot.model_validate_json(after.model_dump_json())


def legacy_parity(request, handle, live, result):
    offset = len(live.event_log)
    asyncio.run(orch.submit_player_intent(handle, request.actor_id, request.payload))
    expected = capture_evaluation_snapshot(
        live, request.state_snapshot, GridScene(width=12, height=4)
    )
    # Legacy has no consent DTO. Preserve this explicit SM-validated fact only when it
    # neither fired nor expired; compare every actual mechanical state otherwise.
    after = apply_opportunity(request.state_snapshot, result)
    expected = expected.model_copy(
        update={
            "combat_state": expected.combat_state.model_copy(
                update={"pending_reactions": after.combat_state.pending_reactions}
            )
        }
    )
    assert result.proposed_events == tuple(live.event_log[offset:])
    assert result.rng_transition.next_state == RNGState.capture(live.rng)
    assert after == expected
    return after


@pytest.mark.parametrize("npc", [False, True])
@pytest.mark.parametrize(
    "seed,ac,hp,temp", [(0, 1, 40, 0), (1, 99, 40, 0), (31, 1, 40, 0), (0, 1, 1, 0), (0, 1, 1, 20)]
)
def test_shared_oa_complete_state_events_rng_and_stop(npc, seed, ac, hp, temp, monkeypatch):
    request, handle, live = opportunity_case(npc=npc, seed=seed, ac=ac, hp=hp, temp=temp)
    before = deepcopy(request)
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request)
    assert result.status == "accepted", result.error
    after = legacy_parity(request, handle, live, result)
    owner = HERO if npc else FOE
    reactor = next(a for a in after.character_states if a.entity_id == owner)
    mover = next(a for a in after.character_states if a.entity_id == request.actor_id)
    assert not reactor.reaction_available
    assert reactor.action_available
    assert reactor.bonus_action_available
    assert not after.combat_state.pending_reactions
    assert (
        after.combat_state.current_turn_index
        == before.state_snapshot.combat_state.current_turn_index
    )
    assert request == before
    if mover.hp_current == 0:
        assert after.combat_state.actor_zone[mover.entity_id] == "1,0"
        assert after.combat_state.movement_ledgers[mover.entity_id].spent_ft == 0
        assert not any(isinstance(e, ActorMoved) for e in result.proposed_events)
    else:
        assert after.combat_state.actor_zone[mover.entity_id] == "3,0"
        assert after.combat_state.movement_ledgers[mover.entity_id].spent_ft == 10
    assert isinstance(result.proposed_events[0], IntentSubmitted)
    assert result.proposed_events[0].actor_id == owner
    assert sum(isinstance(e, AttackRolled) for e in result.proposed_events) == 1


@pytest.mark.parametrize("reason", ["inside", "spent", "disengage", "outside"])
def test_legal_nontrigger_keeps_declaration_and_pays_nothing(reason, monkeypatch):
    request, handle, live = opportunity_case(destination="1,1" if reason == "inside" else "3,0")
    if reason == "outside":
        live.actor_zone[HERO] = "10,3"
    live.initiative = [
        a.model_copy(
            update={
                **(
                    {"reaction_available": False}
                    if a.entity_id == HERO and reason == "spent"
                    else {}
                ),
                **(
                    {"disengaging_this_turn": True}
                    if a.entity_id == FOE and reason == "disengage"
                    else {}
                ),
            }
        )
        for a in live.initiative
    ]
    captured = capture_evaluation_snapshot(
        live, request.state_snapshot, GridScene(width=12, height=4)
    )
    request = request.model_copy(
        update={
            "state_snapshot": captured.model_copy(
                update={
                    "combat_state": captured.combat_state.model_copy(
                        update={
                            "pending_reactions": request.state_snapshot.combat_state.pending_reactions
                        }
                    )
                }
            )
        }
    )
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request)
    assert result.status == "accepted", result.error
    after = legacy_parity(request, handle, live, result)
    assert (
        after.combat_state.pending_reactions
        == request.state_snapshot.combat_state.pending_reactions
    )
    assert not result.rng_transition.state_changed
    assert not any(isinstance(e, (AttackRolled, DamageApplied)) for e in result.proposed_events)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "wrong_mover",
        "weapon",
        "grip",
        "features",
        "mastery",
        "not_held",
        "inventory",
        "counter",
    ],
)
def test_incomplete_dependency_refuses_before_rng(mutation, monkeypatch):
    request, _, _ = opportunity_case()
    value = request.state_snapshot.model_dump(mode="python")
    declaration = value["combat_state"]["pending_reactions"][0]
    if mutation == "missing":
        value["combat_state"]["pending_reactions"] = []
    elif mutation == "wrong_mover":
        declaration["mover_id"] = HERO
    elif mutation == "weapon":
        declaration["weapon_id"] = "missing"
    elif mutation == "grip":
        value["character_states"][0]["weapon_grip"] = "two_handed"
    elif mutation == "features":
        value["character_states"][0]["granted_features"] = None
    elif mutation == "mastery":
        value["character_states"][0]["weapon_mastery_slugs"] = (WEAPON,)
    elif mutation == "inventory":
        value["inventory_state"]["entries"][0]["quantity"] = 0
    elif mutation == "counter":
        value["character_states"][0]["custom_counters"] = {"unreviewed": {"spent": 1}}
    else:
        value["character_states"][0]["weapon_in_hands"] = None
        value["character_states"][0]["weapon_grip"] = "none"
    snapshot = CombatSnapshot.model_validate(value)
    request = request.model_copy(update={"state_snapshot": snapshot})
    isolate(monkeypatch)
    monkeypatch.setattr(RNGState, "restore", lambda _: pytest.fail("refusal restored RNG"))
    result = execute(request)
    assert result.status == "unsupported", result.error
    assert result.state_delta is None
    assert not result.proposed_events
    assert result.rng_transition is None


def test_replay_concurrency_stale_duplicate_and_closed_json(monkeypatch):
    request, _, _ = opportunity_case()
    isolate(monkeypatch)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: execute(request), range(8)))
    assert all(r == results[0] for r in results)
    result = results[0]
    assert result.status == "accepted", result.error
    assert RuleEvaluationResult.model_validate_json(result.model_dump_json()) == result
    assert type(request).model_validate_json(request.model_dump_json()) == request
    after = apply_opportunity(request.state_snapshot, result)
    with pytest.raises(AssertionError):
        apply_opportunity(after, result)
    with pytest.raises(AssertionError):
        apply_opportunity(request.state_snapshot.model_copy(update={"world_version": 8}), result)
    value = request.state_snapshot.model_dump(mode="python")
    del value["combat_state"]["pending_reactions"][0]["two_handed"]
    with pytest.raises(ValidationError):
        CombatSnapshot.model_validate(value)


@pytest.mark.parametrize("fault", [RuntimeError, asyncio.CancelledError])
def test_fault_after_real_damage_discards_all_local_state(fault, monkeypatch):
    request, _, live = opportunity_case()
    before = deepcopy(request)
    old = evaluation_opportunity.resolve_activity

    def fail(*args, **kwargs):
        old(*args, **kwargs)
        raise fault("after real OA")

    isolate(monkeypatch)
    monkeypatch.setattr(evaluation_opportunity, "resolve_activity", fail)
    with pytest.raises(fault):
        execute(request)
    assert request == before
    assert live.rng.getstate() == request.rng_context.state.restore().getstate()


def test_nontrigger_declaration_expires_on_owner_turn_reset(monkeypatch):
    request, _, _ = opportunity_case(destination="1,1")
    isolate(monkeypatch)
    move = execute(request)
    snapshot = apply_opportunity(request.state_snapshot, move)
    end = request.model_copy(
        update={"state_snapshot": snapshot, "payload": CombatIntentPayload(intent_type="pass")}
    )
    result = execute(end)
    assert result.status == "accepted", result.error
    after = apply_opportunity(snapshot, result)
    assert not after.combat_state.pending_reactions
    assert next(a for a in after.character_states if a.entity_id == HERO).reaction_available


@pytest.mark.parametrize(
    "defense", [None, "damage_resistances", "damage_immunities", "damage_vulnerabilities"]
)
@pytest.mark.parametrize("seed", [0, 5, 31])
def test_independent_attack_damage_and_rng_with_actual_defenses(seed, defense, monkeypatch):
    request, handle, live = opportunity_case(seed=seed)
    if defense:
        live.initiative = [
            a.model_copy(update={defense: ["slashing"]}) if a.entity_id == FOE else a
            for a in live.initiative
        ]
        snapshot = capture_evaluation_snapshot(
            live, request.state_snapshot, GridScene(width=12, height=4)
        )
        request = request.model_copy(
            update={
                "state_snapshot": snapshot.model_copy(
                    update={
                        "combat_state": snapshot.combat_state.model_copy(
                            update={
                                "pending_reactions": request.state_snapshot.combat_state.pending_reactions
                            }
                        )
                    }
                )
            }
        )
    expected = Random(seed)
    natural = expected.randint(1, 20)
    amount = (
        0
        if natural == 1
        else sum(expected.randint(1, 6) for _ in range(2 if natural == 20 else 1)) + 3
    )
    if defense == "damage_immunities":
        amount = 0
    elif defense == "damage_resistances":
        amount //= 2
    elif defense == "damage_vulnerabilities":
        amount *= 2
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request)
    assert result.status == "accepted", result.error
    after = legacy_parity(request, handle, live, result)
    assert next(a for a in after.character_states if a.entity_id == FOE).hp_current == 40 - amount
    assert result.rng_transition.next_state == RNGState.capture(expected)


def test_trigger_later_in_path_flushes_prefix_and_stops_at_that_cell(monkeypatch):
    request, handle, live = opportunity_case(hp=1, destination="3,0")
    live.actor_zone = {HERO: "1,1", FOE: "0,0"}
    snapshot = capture_evaluation_snapshot(
        live, request.state_snapshot, GridScene(width=12, height=4)
    )
    request = request.model_copy(
        update={
            "state_snapshot": snapshot.model_copy(
                update={
                    "combat_state": snapshot.combat_state.model_copy(
                        update={
                            "pending_reactions": request.state_snapshot.combat_state.pending_reactions
                        }
                    )
                }
            )
        }
    )
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request)
    assert result.status == "accepted", result.error
    after = legacy_parity(request, handle, live, result)
    assert after.combat_state.actor_zone[FOE] == "2,0"
    assert after.combat_state.movement_ledgers[FOE].spent_ft == 10
    assert isinstance(result.proposed_events[0], ActorMoved)
    assert result.proposed_events[0].path == ("0,0", "1,0", "2,0")


def test_full_cleric_move_oa_cantrip_pass_reset_cantrip(monkeypatch):
    from tests.test_stateless_damage_spell import damage_spell_case
    from tests.test_stateless_spell_feature import apply_healing

    request, handle, live, loader = damage_spell_case(seed=0, empty_slots=True)
    live.initiative = [
        a.model_copy(
            update={
                "entity_type": "Character",
                "strength": 16,
                "attack_bonus": 5,
                "carried_item_slugs": (WEAPON,),
                "weapon_in_hands": WEAPON,
                "weapon_grip": "one_handed",
            }
        )
        if a.entity_id == FOE
        else a
        for a in live.initiative
    ]
    live.actor_zone = {HERO: "1,0", FOE: "0,0"}
    live.opportunity_attack_weapons[FOE] = WEAPON
    snapshot = capture_evaluation_snapshot(
        live, request.state_snapshot, GridScene(width=3, height=3)
    )
    declaration = OpportunityAttackState(
        source_kind="opportunity_attack",
        owner_id=FOE,
        mover_id=HERO,
        weapon_id=WEAPON,
        two_handed=False,
        trigger="voluntary_leave_reach",
        expiry="owner_next_turn_start",
    )
    snapshot = snapshot.model_copy(
        update={
            "inventory_state": InventoryState(
                entries=(
                    InventoryEntry(
                        instance_id="foe:weapon",
                        owner_id=FOE,
                        item_slug=WEAPON,
                        quantity=1,
                        charges_remaining_per_unit=0,
                        accessible=True,
                    ),
                )
            ),
            "combat_state": snapshot.combat_state.model_copy(
                update={"pending_reactions": [declaration]}
            ),
        }
    )
    rng = request.rng_context
    steps = [
        (HERO, CombatIntentPayload(intent_type="move", target_zone_id="2,0")),
        (HERO, request.payload),
        (FOE, CombatIntentPayload(intent_type="pass")),
        (HERO, request.payload),
    ]
    for index, (actor, payload) in enumerate(steps):
        offset = len(live.event_log)
        asyncio.run(orch.submit_player_intent(handle, actor, payload))
        invocation = request.model_copy(
            update={
                "state_snapshot": snapshot,
                "rng_context": rng,
                "actor_id": actor,
                "command_id": f"oa-workflow:{index}",
                "payload": payload,
            }
        )
        with monkeypatch.context() as scoped:
            isolate(scoped)
            result = execute(invocation, loader)
        assert result.status == "accepted", result.error
        snapshot = (apply_opportunity if index == 0 else apply_healing)(snapshot, result)
        rng = rng.model_copy(update={"state": result.rng_transition.next_state})
        assert result.proposed_events == tuple(live.event_log[offset:])
        assert rng.state == RNGState.capture(live.rng)
        assert snapshot == capture_evaluation_snapshot(live, snapshot, GridScene(width=3, height=3))
    hero = next(a for a in snapshot.character_states if a.entity_id == HERO)
    assert hero.classes == {"cleric": 1}
    assert (
        hero.granted_features
        == next(
            a for a in request.state_snapshot.character_states if a.entity_id == HERO
        ).granted_features
    )
    assert hero.spell_slots == {1: 0}


@pytest.mark.parametrize("dead_owner", [False, True])
def test_complete_dead_facts_never_pay_or_roll(dead_owner, monkeypatch):
    from dnd5e_engine.damage_rules import death_record
    from dnd5e_engine.evaluation_actor import combatant
    from dnd5e_engine.events import Death

    request, _, _ = opportunity_case()
    snapshot = request.state_snapshot
    dead = HERO if dead_owner else FOE
    actors = tuple(
        a.model_copy(update={"hp_current": 0, "is_alive": False}) if a.entity_id == dead else a
        for a in snapshot.character_states
    )
    actor = next(a for a in actors if a.entity_id == dead)
    record = death_record(
        combatant(actor),
        Death(target_id=dead, reason="damage"),
        location_id=snapshot.scene_state.scene_id,
        killer_id=FOE if dead_owner else HERO,
    )
    snapshot = snapshot.model_copy(
        update={
            "character_states": actors,
            "combat_state": snapshot.combat_state.model_copy(
                update={"dead_ids": {dead}, "deaths_recorded": [record]}
            ),
        }
    )
    isolate(monkeypatch)
    result = execute(request.model_copy(update={"state_snapshot": snapshot}))
    assert result.status == ("accepted" if dead_owner else "rejected"), result.error
    assert not any(isinstance(e, (AttackRolled, DamageApplied)) for e in result.proposed_events)
    if dead_owner:
        after = apply_opportunity(snapshot, result)
        assert after.combat_state.pending_reactions == snapshot.combat_state.pending_reactions
        assert not result.rng_transition.state_changed
    else:
        assert result.state_delta is None
        assert result.rng_transition is None


def test_weapon_reach_not_longer_body_reach_controls_trigger(monkeypatch):
    request, handle, live = opportunity_case(destination="2,0")
    live.initiative[0] = live.initiative[0].model_copy(update={"melee_reach_ft": 10})
    snapshot = capture_evaluation_snapshot(
        live, request.state_snapshot, GridScene(width=12, height=4)
    )
    request = request.model_copy(
        update={
            "state_snapshot": snapshot.model_copy(
                update={
                    "combat_state": snapshot.combat_state.model_copy(
                        update={
                            "pending_reactions": request.state_snapshot.combat_state.pending_reactions
                        }
                    )
                }
            )
        }
    )
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request)
    assert result.status == "accepted", result.error
    legacy_parity(request, handle, live, result)
    assert sum(isinstance(e, AttackRolled) for e in result.proposed_events) == 1


def test_los_blocked_reactor_does_not_fire_or_pay(monkeypatch):
    from dnd5e_engine.spatial import GridTopology
    from dnd5e_engine.specs import WallSegment

    request, handle, live = opportunity_case()
    scene = GridScene(
        width=12, height=4, wall_segments=[WallSegment(x1=1.0, y1=0.0, x2=1.0, y2=1.0)]
    )
    live.topology = GridTopology(scene)
    snapshot = capture_evaluation_snapshot(live, request.state_snapshot, scene)
    request = request.model_copy(
        update={
            "state_snapshot": snapshot.model_copy(
                update={
                    "combat_state": snapshot.combat_state.model_copy(
                        update={
                            "pending_reactions": request.state_snapshot.combat_state.pending_reactions
                        }
                    )
                }
            )
        }
    )
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request)
    assert result.status == "accepted", result.error
    # Capture with the actual scene; no geometry normalization in the comparison.
    offset = len(live.event_log)
    asyncio.run(orch.submit_player_intent(handle, request.actor_id, request.payload))
    after = apply_opportunity(request.state_snapshot, result)
    expected = capture_evaluation_snapshot(live, request.state_snapshot, scene)
    expected = expected.model_copy(
        update={
            "combat_state": expected.combat_state.model_copy(
                update={"pending_reactions": after.combat_state.pending_reactions}
            )
        }
    )
    assert after == expected
    assert result.proposed_events == tuple(live.event_log[offset:])
    assert not result.rng_transition.state_changed


def test_canonical_reach_weapon_fires_from_ten_feet(monkeypatch):
    from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader

    from dnd5e_engine.evaluation_ruleset import ruleset_binding

    request, handle, live = opportunity_case()
    loader = MemoryAssetLoader(items=[BundledAssetLoader().get_weapon("glaive")])
    live.ruleset_loader = loader
    live.initiative[0] = live.initiative[0].model_copy(
        update={
            "carried_item_slugs": ("glaive",),
            "weapon_in_hands": "glaive",
            "weapon_grip": "two_handed",
        }
    )
    live.opportunity_attack_weapons[HERO] = "glaive"
    live.actor_zone = {HERO: "0,0", FOE: "2,0"}
    snapshot = capture_evaluation_snapshot(
        live, request.state_snapshot, GridScene(width=12, height=4)
    )
    declaration = request.state_snapshot.combat_state.pending_reactions[0].model_copy(
        update={"weapon_id": "glaive", "two_handed": True}
    )
    snapshot = snapshot.model_copy(
        update={
            "inventory_state": InventoryState(
                entries=(
                    InventoryEntry(
                        instance_id="oa:glaive",
                        owner_id=HERO,
                        item_slug="glaive",
                        quantity=1,
                        charges_remaining_per_unit=0,
                        accessible=True,
                    ),
                )
            ),
            "combat_state": snapshot.combat_state.model_copy(
                update={"pending_reactions": [declaration]}
            ),
        }
    )
    request = request.model_copy(
        update={"state_snapshot": snapshot, "ruleset_binding": ruleset_binding(loader)}
    )
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request, loader)
    assert result.status == "accepted", result.error
    legacy_parity(request, handle, live, result)
    assert sum(isinstance(e, AttackRolled) for e in result.proposed_events) == 1
