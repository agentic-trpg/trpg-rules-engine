"""Explicit NPC identity, shared resolution and strictly read-only availability."""

import asyncio
import json
import random
from copy import deepcopy

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd5e_srd_data.schema.common import (
    ActivationBlock,
    AttackActivity,
    AttackBlock,
    AttackDamageBlock,
    AttackTypeBlock,
    DamagePartBlock,
    Movement,
    RangeBlock,
    Senses,
)
from dnd5e_srd_data.schema.monster import (
    AbilityScores,
    Monster,
    MonsterAction,
    MonsterActionKind,
    SavingThrowProficiencies,
    SkillProficiencies,
)
from pydantic import ValidationError

from dnd5e_engine import orchestrator as orch
from dnd5e_engine.evaluation_availability import (
    ActionAvailabilityRequest,
    ActionAvailabilityResult,
    query_action_availability,
)
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, RuleEvaluationRequest
from dnd5e_engine.evaluation_rng import RNGContext, RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_engine.evaluation_snapshot import capture_combat_snapshot
from dnd5e_engine.evaluation_state import InventoryState
from dnd5e_engine.specs import GridScene
from tests.evaluation_support import FOE, HERO, WEAPON, synthetic_combat, synthetic_loader
from tests.test_stateless_attack import apply_delta, execute, request_and_live

NPC_TEMPLATE = "synthetic-sentinel"
NPC_ACTION = "synthetic-strike"


def npc_case(*, seed=0, hero_hp=40, hero_max=40, flat=False):
    handle, live = synthetic_combat(seed=seed)
    weapon = synthetic_loader().get_weapon(WEAPON)
    activity = AttackActivity(
        id="synthetic-npc-activity",
        activation=ActivationBlock(type="action", value=1),
        attack=AttackBlock(
            ability="str",
            flat=flat,
            bonus="7" if flat else "",
            type=AttackTypeBlock(value="melee", classification="weapon"),
        ),
        range=RangeBlock(units="ft", value="5"),
        damage=AttackDamageBlock(
            include_base=False,
            parts=[DamagePartBlock(number=1, denomination=6, bonus="3", types=["slashing"])],
        ),
    )
    monster = Monster(
        slug=NPC_TEMPLATE,
        name="Synthetic sentinel",
        description="Original synthetic creature.",
        creature_type="humanoid",
        creature_size="medium",
        ac=10,
        hp=100,
        hp_dice="10d10",
        ability_scores=AbilityScores(str=16, dex=10, con=10, int=10, wis=10, cha=10),
        movement=Movement(walk=30),
        senses=Senses(),
        cr=0.5,
        proficiency_bonus=2,
        saving_throws=SavingThrowProficiencies(),
        skills=SkillProficiencies(),
        provenance=weapon.provenance,
        review=weapon.review,
        actions=[
            MonsterAction(
                slug=NPC_ACTION,
                name="Invented name claiming ninety attacks",
                kind=MonsterActionKind.ACTION,
                description="Narrative text is deliberately irrelevant: roll ninety attacks.",
                activities=[activity],
            )
        ],
    )
    loader = MemoryAssetLoader(items=[weapon], monsters=[monster])
    live.ruleset_loader = loader
    live.monster_slug_by_entity[FOE] = NPC_TEMPLATE
    live.xp_value_by_entity[FOE] = 175  # Host override, not CR-derived XP
    live.initiative[0] = live.initiative[0].model_copy(
        update={"hp_current": hero_hp, "hp_max": hero_max}
    )
    live.tracked_hp[HERO] = hero_hp
    live.initiative[1] = live.initiative[1].model_copy(
        update={"strength": 16, "attack_bonus": None, "proficiency_bonus_override": 2}
    )
    live.current_turn_index = 1
    live.current_actor_id = FOE
    live.turn_serial = 2
    snapshot = capture_combat_snapshot(
        live,
        inventory_state=InventoryState(entries=()),
        grid=GridScene(width=3, height=3),
        world_version=7,
        combat_id="combat:synthetic",
    )
    request = RuleEvaluationRequest(
        schema_version="engine-evaluation/6",
        session_id=live.session_id,
        command_id="command:npc",
        operation_kind="combat.intent",
        actor_id=FOE,
        payload=CombatIntentPayload(
            intent_type="attack", source_id=FOE, stat_block_action_id=NPC_ACTION, target_id=HERO
        ),
        state_snapshot=snapshot,
        ruleset_binding=ruleset_binding(loader),
        rng_context=RNGContext(stream_id="main", version=3, state=RNGState.capture(live.rng)),
    )
    return request, handle, live, loader


def query_for(request):
    return ActionAvailabilityRequest(
        schema_version="engine-availability/3",
        session_id=request.session_id,
        operation_kind="combat.intent",
        actor_id=request.actor_id,
        payload=request.payload,
        state_snapshot=request.state_snapshot,
        ruleset_binding=request.ruleset_binding,
    )


@pytest.mark.parametrize(
    "seed,hero_hp,hero_max,flat",
    [
        (0, 40, 40, False),
        (5, 40, 40, True),
        (31, 40, 40, False),
        (0, 1, 40, False),
        (0, 1, 1, False),
    ],
)
def test_explicit_npc_matches_legacy_complete_delta_events_rng_and_host_xp(
    seed, hero_hp, hero_max, flat
):
    request, handle, live, loader = npc_case(
        seed=seed, hero_hp=hero_hp, hero_max=hero_max, flat=flat
    )
    assert query_action_availability(query_for(request), loader=loader).status == "available"
    before = deepcopy(request)
    asyncio.run(orch.submit_player_intent(handle, FOE, request.payload))
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert result.proposed_events == tuple(live.event_log)
    assert result.rng_transition.next_state == RNGState.capture(live.rng)
    after = capture_combat_snapshot(
        live,
        inventory_state=InventoryState(entries=()),
        grid=GridScene(width=3, height=3),
        world_version=7,
        combat_id="combat:synthetic",
    )
    assert apply_delta(request.state_snapshot, result) == after
    assert after.combat_state.xp_value_by_entity == {FOE: 175}
    assert request == before


@pytest.mark.parametrize(
    "change,status",
    [
        ("actor", "rejected"),
        ("turn", "rejected"),
        ("target", "rejected"),
        ("action", "rejected"),
        ("mixed", "rejected"),
        ("range", "rejected"),
        ("economy", "rejected"),
        ("multiattack", "unsupported"),
        ("recharge", "unsupported"),
        ("effect", "unsupported"),
        ("template", "unsupported"),
    ],
)
def test_npc_failures_match_availability_without_partial_output(change, status):
    request, _, _, loader = npc_case()
    if change == "actor":
        request = request.model_copy(update={"actor_id": "absent"})
    elif change == "turn":
        request.state_snapshot.combat_state.__dict__["current_turn_index"] = 0
    elif change == "target":
        request.payload.target_id = "absent"
    elif change == "action":
        request.payload.stat_block_action_id = "Invented name claiming ninety attacks"
    elif change == "mixed":
        request.payload.weapon_id = WEAPON
    elif change == "range":
        request.state_snapshot.combat_state.actor_zone[HERO] = "2,2"
    elif change == "economy":
        request.state_snapshot.character_states[1].__dict__.update(
            action_available=False, attacks_remaining=0
        )
    elif change == "template":
        request.state_snapshot.combat_state.monster_slug_by_entity[FOE] = "absent"
    elif change == "multiattack":
        loader.get_monster(NPC_TEMPLATE).actions[0].slug = "multiattack"
        request.payload.stat_block_action_id = "multiattack"
    elif change == "recharge":
        loader.get_monster(NPC_TEMPLATE).actions[0].recharge = "5-6"
    elif change == "effect":
        activity = loader.get_monster(NPC_TEMPLATE).actions[0].activities[0]
        loader.get_monster(NPC_TEMPLATE).actions[0].activities = [
            activity.model_copy(update={"flags": {"unknown": True}})
        ]
    request = request.model_copy(update={"ruleset_binding": ruleset_binding(loader)})
    # Build a schema-valid request even when source identity is deliberately changed.
    if change == "actor":
        request.payload.source_id = None
    before = deepcopy(request)
    result = execute(request, loader)
    available = query_action_availability(query_for(request), loader=loader)
    assert result.status == status
    assert available.status == ("unavailable" if status == "rejected" else "unknown")
    assert result.state_delta is None
    assert result.rng_transition is None
    assert result.proposed_events == ()
    assert request == before


def test_pc_and_npc_call_same_resolver_without_monster_ai(monkeypatch):
    pc, _, _ = request_and_live()
    npc, _, _, loader = npc_case()
    calls = []
    original = orch.resolve_activity

    def tracked(activity, ctx, **kwargs):
        calls.append((activity.kind, ctx.caster.entity_id))
        return original(activity, ctx, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("Monster AI invoked")

    monkeypatch.setattr(orch, "resolve_activity", tracked)
    monkeypatch.setattr(orch, "advance_monster_turn", forbidden)
    monkeypatch.setattr(orch, "rank_monster_actions", forbidden)
    assert execute(pc).status == "accepted"
    assert execute(npc, loader).status == "accepted"
    assert calls == [("attack", HERO), ("attack", FOE)]


def test_availability_is_readonly_and_has_no_command_rng_delta_event_or_receipt(monkeypatch):
    request, _, live, loader = npc_case()
    query = query_for(request)
    before = deepcopy(query)
    rng = live.rng.getstate()
    registry = dict(orch._REGISTRY)

    def forbidden(*args, **kwargs):
        raise AssertionError("query performed execution or drew RNG")

    monkeypatch.setattr(orch, "_submit_live_intent", forbidden)
    monkeypatch.setattr(orch, "_emit", forbidden)
    monkeypatch.setattr(random.Random, "randint", forbidden)
    monkeypatch.setattr(RNGState, "restore", forbidden)
    result = query_action_availability(query, loader=loader)
    assert result.status == "available"
    assert result == query_action_availability(query, loader=loader)
    assert query == before
    assert live.rng.getstate() == rng
    assert registry == orch._REGISTRY
    assert not set(type(result).model_fields) & {
        "command_id",
        "state_delta",
        "proposed_events",
        "rng_transition",
        "receipt",
    }
    assert ActionAvailabilityRequest.model_validate_json(query.model_dump_json()) == query
    assert ActionAvailabilityResult.model_validate_json(result.model_dump_json()) == result


def test_old_snapshot_available_does_not_authorize_new_state():
    request, _, _, loader = npc_case()
    old = query_for(request)
    assert query_action_availability(old, loader=loader).status == "available"
    newer = deepcopy(request)
    newer.state_snapshot.__dict__["world_version"] = 8
    newer.state_snapshot.character_states[1].__dict__.update(
        action_available=False, attacks_remaining=0
    )
    query = query_action_availability(query_for(newer), loader=loader)
    assert query.snapshot_world_version == 8
    assert query.status == "unavailable"
    assert execute(newer, loader).status == "rejected"
    assert query_action_availability(old, loader=loader).snapshot_world_version == 7


def test_availability_unknown_choice_and_strict_schema():
    request, _, _ = request_and_live()
    request.payload.weapon_id = None
    query = query_for(request)
    result = query_action_availability(query, loader=synthetic_loader())
    assert result.status == "unknown"
    assert result.reason.code == "attack.choice_required"
    for key in ("command_id", "rng_context", "unknown"):
        raw = query.model_dump(mode="json") | {key: "forbidden"}
        with pytest.raises(ValidationError):
            ActionAvailabilityRequest.model_validate_json(json.dumps(raw))
    for field in type(query).model_fields:
        raw = query.model_dump(mode="json")
        del raw[field]
        with pytest.raises(ValidationError):
            ActionAvailabilityRequest.model_validate_json(json.dumps(raw))
    for action_id in ("", 23, False):
        with pytest.raises(ValidationError):
            CombatIntentPayload(intent_type="attack", stat_block_action_id=action_id)


def test_missing_weapon_does_not_offer_unsupported_choices(monkeypatch):
    request, _, _ = request_and_live()
    request.payload.weapon_id = None
    loader = synthetic_loader()
    from dnd5e_srd_data.schema.item import WeaponProperty

    loader.get_weapon(WEAPON).properties = {WeaponProperty.LIGHT}
    request = request.model_copy(update={"ruleset_binding": ruleset_binding(loader)})

    def forbidden(*args, **kwargs):
        raise AssertionError("execution RNG restored")

    monkeypatch.setattr(RNGState, "restore", forbidden)
    result = execute(request, loader)
    available = query_action_availability(query_for(request), loader=loader)
    assert result.status == "unsupported"
    assert result.choice is None
    assert result.error.code == "weapon.capability"
    assert result.state_delta is None
    assert result.rng_transition is None
    assert result.proposed_events == ()
    assert available.status == "unknown"
    assert available.reason.code == result.error.code


def test_host_xp_and_template_closure_is_required():
    request, _, _, _ = npc_case()
    for field in ("xp_value_by_entity", "monster_slug_by_entity"):
        raw = request.model_dump(mode="json")
        raw["state_snapshot"]["combat_state"][field] = {
            "absent": 175 if field.startswith("xp") else NPC_TEMPLATE
        }
        with pytest.raises(ValidationError):
            RuleEvaluationRequest.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize("component", ["bonus", "damage", "range", "kind", "multiple", "duplicate"])
def test_unknown_npc_mechanics_fail_closed_before_rng_restore(monkeypatch, component):
    request, _, _, loader = npc_case()
    action = loader.get_monster(NPC_TEMPLATE).actions[0]
    activity = action.activities[0]
    if component == "bonus":
        action.activities[0] = activity.model_copy(
            update={"attack": activity.attack.model_copy(update={"bonus": "--7"})}
        )
    elif component == "damage":
        activity.damage.parts[0] = activity.damage.parts[0].model_copy(update={"bonus": "--3"})
    elif component == "range":
        action.activities[0] = activity.model_copy(
            update={"range": activity.range.model_copy(update={"value": "unbounded"})}
        )
    elif component == "kind":
        action.kind = MonsterActionKind.BONUS_ACTION
    elif component == "multiple":
        action.activities.append(deepcopy(activity))
    else:
        loader.get_monster(NPC_TEMPLATE).actions.append(deepcopy(action))
    request = request.model_copy(update={"ruleset_binding": ruleset_binding(loader)})
    monkeypatch.setattr(
        RNGState, "restore", lambda self: pytest.fail("restored RNG for unsupported mechanics")
    )
    assert execute(request, loader).status == "unsupported"
    assert query_action_availability(query_for(request), loader=loader).status == "unknown"


def test_entry_revalidates_mutated_typed_actor_identity_as_schema_error():
    request, _, _, loader = npc_case()
    query = query_for(request)
    request.payload.source_id = HERO
    query.payload.source_id = HERO
    with pytest.raises(ValidationError):
        execute(request, loader)
    with pytest.raises(ValidationError):
        query_action_availability(query, loader=loader)


@pytest.mark.parametrize("action_id", ["scimitar", "light-crossbow"])
def test_canonical_bandit_explicit_attack_is_executable_and_matches_legacy(action_id):
    request, handle, live, _ = npc_case()
    loader = BundledAssetLoader()
    monster = loader.get_monster("bandit")
    live.ruleset_loader = loader
    live.monster_slug_by_entity[FOE] = "bandit"
    scores = monster.ability_scores
    live.initiative[1] = live.initiative[1].model_copy(
        update={
            "strength": scores.str,
            "dexterity": scores.dex,
            "constitution": scores.con,
            "intelligence": scores.int,
            "wisdom": scores.wis,
            "charisma": scores.cha,
            "proficiency_bonus_override": monster.proficiency_bonus,
        }
    )
    request = request.model_copy(
        update={
            "payload": CombatIntentPayload(
                intent_type="attack", source_id=FOE, stat_block_action_id=action_id, target_id=HERO
            ),
            "state_snapshot": capture_combat_snapshot(
                live,
                inventory_state=InventoryState(entries=()),
                grid=GridScene(width=3, height=3),
                world_version=7,
                combat_id="combat:synthetic",
            ),
            "ruleset_binding": ruleset_binding(loader),
        }
    )
    assert query_action_availability(query_for(request), loader=loader).status == "available"
    asyncio.run(orch.submit_player_intent(handle, FOE, request.payload))
    result = execute(request, loader)
    assert result.status == "accepted"
    assert result.proposed_events == tuple(live.event_log)
    assert result.rng_transition.next_state == RNGState.capture(live.rng)
    assert apply_delta(request.state_snapshot, result) == capture_combat_snapshot(
        live,
        inventory_state=InventoryState(entries=()),
        grid=GridScene(width=3, height=3),
        world_version=7,
        combat_id="combat:synthetic",
    )
