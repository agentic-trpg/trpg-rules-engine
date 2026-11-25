"""R14 real SRD initialization -> explicit NPC attack, closed facts and isolation."""

import asyncio
import random
from copy import deepcopy

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd5e_srd_data.schema.common import ActivityTiming, UsesBlock
from dnd5e_srd_data.schema.monster import MonsterActionKind, MonsterTraitMechanic
from pydantic import ValidationError

from dnd5e_engine import evaluation_initialization as init
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, RuleEvaluationRequest
from dnd5e_engine.evaluation_delta import CombatCreationCandidate
from dnd5e_engine.evaluation_rng import RNGContext, RNGState
from dnd5e_engine.evaluation_ruleset import RulesetBindingError, freeze_ruleset, ruleset_binding
from dnd5e_engine.evaluation_snapshot import capture_combat_snapshot
from dnd5e_engine.evaluation_state import (
    CombatSetupState,
    InventoryEntry,
    InventoryState,
    NonCombatSnapshot,
    NPCStatBlockBinding,
    ObjectState,
    PersistentAreasState,
    TimedActivitiesState,
)
from dnd5e_engine.events import AttackRolled, DamageApplied
from dnd5e_engine.lib_loader import scoped_lib_loader
from dnd5e_engine.specs import EncounterMemberSpec, GridScene, PartyMemberSpec
from tests.test_stateless_attack import apply_delta, execute, pass_after_attack
from tests.test_stateless_initialization import apply_creation
from tests.test_stateless_item_closure_core import forbidden, isolate

HERO, NPC = "hero:approved", "npc:approved-bandit"


def real_case(*, rolled=False):
    loader = BundledAssetLoader()
    monster = loader.get_monster("bandit")
    grid = GridScene(width=4, height=4)
    with scoped_lib_loader(loader):
        started = asyncio.run(
            orch.start_combat(
                session_id="session:r14",
                rng_seed=0,
                scene_location_id="scene:r14",
                grid_scene=grid,
                party=[
                    PartyMemberSpec(
                        entity_id=HERO,
                        name="Hero",
                        initiative=None if rolled else 1,
                        hp_current=40,
                        hp_max=40,
                        ac=10,
                        zone_id="0,0",
                        equipment=("potion-of-healing",),
                        spell_slots={1: 2},
                        custom_counters={"original-pool": {"value": 1, "max": 3}},
                    )
                ],
                encounter=[
                    EncounterMemberSpec(
                        entity_id=NPC,
                        entity_type="NPC",
                        name="Approved NPC identity",
                        initiative=None if rolled else 20,
                        hp_current=monster.hp,
                        hp_max=monster.hp,
                        ac=monster.ac,
                        base_speed=monster.movement.walk,
                        creature_type=monster.creature_type,
                        condition_immunities=monster.condition_immunities,
                        monster_template_slug=monster.slug,
                        zone_id="2,0",
                        xp_value=25,
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
            }
        )
        for a in live.initiative
    ]
    inventory = InventoryState(
        entries=(
            InventoryEntry(
                instance_id="potion:owned",
                owner_id=HERO,
                item_slug="potion-of-healing",
                quantity=2,
                charges_remaining_per_unit=1,
                accessible=True,
            ),
        )
    )
    expected = capture_combat_snapshot(
        live,
        resource_state=None,
        casting_state=None,
        grid=grid,
        inventory_state=inventory,
        world_version=7,
        combat_id="combat:r14",
    )
    binding = ruleset_binding(loader)
    setup = CombatSetupState(
        combat_id="combat:r14",
        party_ids=(HERO,),
        encounter_ids=(NPC,),
        roll_order=(HERO, NPC),
        actor_zone={HERO: "0,0", NPC: "2,0"},
        fixed_initiative={HERO: None if rolled else 1, NPC: None if rolled else 20},
        initiative_modifiers={HERO: None, NPC: monster.initiative_modifier},
        surprised_ids=set(),
        xp_value_by_entity={NPC: 25},
        opportunity_attack_weapons={},
        npc_stat_blocks=(
            NPCStatBlockBinding(
                actor_id=NPC, monster_slug="bandit", data_revision=binding.data_revision
            ),
        ),
        timed_activities=TimedActivitiesState(pending=[], next_sequence=0),
        persistent_areas=PersistentAreasState(
            areas=[], next_sequence=0, next_turn_start_effects=()
        ),
        combat_objects=ObjectState(objects=[], used_ids=set()),
    )
    actors = tuple(
        a.model_copy(
            update={
                "initiative": 777,
                "action_available": False,
                "bonus_action_available": False,
                "reaction_available": False,
                "movement_remaining": 1,
            }
        )
        for a in expected.character_states
    )
    snapshot = NonCombatSnapshot(
        resource_state=None,
        casting_state=None,
        snapshot_schema_version="engine-snapshot/17",
        snapshot_kind="non_combat",
        session_id=expected.session_id,
        world_version=7,
        character_states=actors,
        effect_states=(),
        inventory_state=inventory,
        scene_state=expected.scene_state,
        combat_setup=setup,
    )
    request = init.CombatInitializationCandidateRequest(
        schema_version="engine-evaluation/24",
        payload=init.CombatInitializationCandidatePayload(kind="combat.init.local-candidate"),
        session_id=expected.session_id,
        command_id="cmd:r14:init",
        state_snapshot=snapshot,
        ruleset_binding=binding,
        rng_context=RNGContext(
            stream_id="main", version=2, state=RNGState.capture(random.Random(0))
        ),
    )
    return request, expected, live, loader, started, grid


def isolated(monkeypatch):
    isolate(monkeypatch)
    for name in ("_build_foe_combatants", "_hydrate_monster_action_uses", "_resolve_initiative"):
        monkeypatch.setattr(orch, name, forbidden)


@pytest.mark.parametrize("rolled", [False, True])
def test_real_srd_init_then_npc_explicit_attack_without_legacy(monkeypatch, rolled):
    request, expected, live, loader, started, grid = real_case(rolled=rolled)
    before = deepcopy(request)
    registry = dict(orch._REGISTRY)
    with monkeypatch.context() as scoped:
        isolated(scoped)
        result = init.evaluate_combat_initialization_candidate(request, loader=loader)
        assert result.status == "accepted", result.error
        assert result == init.evaluate_combat_initialization_candidate(request, loader=loader)
        after = apply_creation(request, result)
        assert after == expected
        assert after.combat_state.monster_slug_by_entity == {NPC: "bandit"}
        assert after.combat_state.monster_action_uses_by_entity == {NPC: {}}
        assert result.proposed_events == tuple(started.events)
        assert {
            u.actor_id: u.value.initiative for u in result.state_delta.operations[0].actor_updates
        } == ({HERO: 13, NPC: 15} if rolled else {HERO: 1, NPC: 20})
        assert after.inventory_state == request.state_snapshot.inventory_state
        assert next(a for a in after.character_states if a.entity_id == HERO).spell_slots == {1: 2}
        attack = RuleEvaluationRequest(
            schema_version="engine-evaluation/24",
            session_id=request.session_id,
            command_id="cmd:r14:attack",
            actor_id=NPC,
            operation_kind="combat.intent",
            payload=CombatIntentPayload(
                intent_type="attack",
                source_id=NPC,
                stat_block_action_id="light-crossbow",
                target_id=HERO,
            ),
            state_snapshot=after,
            ruleset_binding=request.ruleset_binding,
            rng_context=request.rng_context.model_copy(
                update={"state": result.rng_transition.next_state}
            ),
        )
        attack_result = execute(attack, loader)
        assert attack_result.status == "accepted", attack_result.error
        assert attack_result == execute(attack, loader)
        attacked = apply_delta(after, attack_result)
        combat = attacked.combat_state
        assert combat.initiative_ids[combat.current_turn_index] == NPC
        if not rolled:
            roll = next(e for e in attack_result.proposed_events if isinstance(e, AttackRolled))
            damage = next(e for e in attack_result.proposed_events if isinstance(e, DamageApplied))
            assert roll.roll_total == 16  # d20=13 + DEX=1 + PB=2
            assert damage.amount == 8  # d8=7 + DEX=1
            assert (
                next(a for a in attacked.character_states if a.entity_id == HERO).hp_current == 32
            )
        movement = attack.model_copy(
            update={
                "payload": CombatIntentPayload(
                    intent_type="move", source_id=NPC, target_zone_id="3,0"
                )
            }
        )
        # Template reaction reach remains deliberately unported, before any payment.
        move_result = execute(movement, loader)
        assert move_result.status == "unsupported"
        assert move_result.error.code == "movement.template"
        assert move_result.state_delta is None
        assert move_result.rng_transition is None
        passed, events = pass_after_attack(attack, attack_result, attacked, loader)
    asyncio.run(orch.submit_player_intent(started.handle, NPC, attack.payload))
    assert events == tuple(live.event_log[len(started.events) :])
    assert passed.rng_transition.next_state == RNGState.capture(live.rng)
    assert apply_delta(attacked, passed) == capture_combat_snapshot(
        live,
        resource_state=None,
        casting_state=None,
        grid=grid,
        inventory_state=after.inventory_state,
        world_version=7,
        combat_id="combat:r14",
    )
    assert request == before
    assert registry == orch._REGISTRY
    with pytest.raises(AssertionError):
        apply_creation(request, result, world_version=8)
    with pytest.raises(AssertionError):
        apply_creation(request, result, existing=result.state_delta.operations[0].value)


@pytest.fixture(scope="module")
def multiattack_template_case():
    """Reuse an initialized Bandit and a real unported action from the same source."""
    request, _, _, loader, _, _ = real_case()
    bandit = loader.get_monster("bandit")
    captain = loader.get_monster("bandit-captain")
    multiattack = next(action for action in captain.actions if action.slug == "multiattack")
    return request, bandit, multiattack, loader.get_item("potion-of-healing")


def bind_monster(request, monster, potion):
    """Supply an explicitly frozen rules revision and its corresponding NPC binding."""
    loader = freeze_ruleset(MemoryAssetLoader(monsters=[monster], items=[potion]))
    binding = ruleset_binding(loader)
    snapshot = request.state_snapshot
    setup = snapshot.combat_setup
    npc_binding = setup.npc_stat_blocks[0].model_copy(
        update={"data_revision": binding.data_revision}
    )
    snapshot = snapshot.model_copy(
        update={"combat_setup": setup.model_copy(update={"npc_stat_blocks": (npc_binding,)})}
    )
    return request.model_copy(
        update={"state_snapshot": snapshot, "ruleset_binding": binding}
    ), loader


def test_optional_inert_multiattack_does_not_block_initialization(
    monkeypatch, multiattack_template_case
):
    request, bandit, multiattack, potion = multiattack_template_case
    baseline_request, baseline_loader = bind_monster(request, bandit, potion)
    monster = bandit.model_copy(update={"actions": [*bandit.actions, multiattack]})
    request, loader = bind_monster(request, monster, potion)
    assert request.ruleset_binding.data_revision != baseline_request.ruleset_binding.data_revision

    isolated(monkeypatch)
    baseline = init.evaluate_combat_initialization_candidate(
        baseline_request, loader=baseline_loader
    )
    assert baseline.status == "accepted", baseline.error
    result = init.evaluate_combat_initialization_candidate(request, loader=loader)
    assert result.status == "accepted", result.error
    assert result.proposed_events == baseline.proposed_events
    assert result.rng_transition == baseline.rng_transition
    after = apply_creation(request, result)
    assert after == apply_creation(baseline_request, baseline)
    assert after.combat_state.monster_action_uses_by_entity == {NPC: {}}

    attack = RuleEvaluationRequest(
        schema_version=request.schema_version,
        session_id=request.session_id,
        command_id="cmd:r14:ordinary-with-multiattack",
        actor_id=NPC,
        operation_kind="combat.intent",
        payload=CombatIntentPayload(
            intent_type="attack", stat_block_action_id="light-crossbow", target_id=HERO
        ),
        state_snapshot=after,
        ruleset_binding=request.ruleset_binding,
        rng_context=request.rng_context.model_copy(
            update={"state": result.rng_transition.next_state}
        ),
    )
    ordinary = execute(attack, loader)
    assert ordinary.status == "accepted", ordinary.error
    assert any(isinstance(event, AttackRolled) for event in ordinary.proposed_events)
    reserved = execute(
        attack.model_copy(
            update={
                "payload": attack.payload.model_copy(update={"stat_block_action_id": "multiattack"})
            }
        ),
        loader,
    )
    assert reserved.status == "unsupported"
    assert reserved.error.code == "action.capability"
    assert reserved.state_delta is None
    assert reserved.rng_transition is None
    assert not reserved.proposed_events


@pytest.mark.parametrize(
    "change",
    [
        {"recharge": "5-6"},
        {"uses_per_day": 1},
        {"legendary_cost": 1},
        {"mechanic": MonsterTraitMechanic.REGENERATION},
        {"kind": MonsterActionKind.REACTION},
        "turn_start",
        "activity_uses",
        "duplicate",
        "no_ordinary_attack",
    ],
)
def test_multiattack_with_mechanics_or_without_ordinary_attack_refuses(
    monkeypatch, multiattack_template_case, change
):
    request, bandit, multiattack, potion = multiattack_template_case
    actions = list(bandit.actions)
    if isinstance(change, dict):
        multiattack = multiattack.model_copy(update=change)
    elif change in {"turn_start", "activity_uses"}:
        update = (
            {"timing": ActivityTiming(trigger="turn_start")}
            if change == "turn_start"
            else {"uses": UsesBlock(max="1")}
        )
        multiattack = multiattack.model_copy(
            update={"activities": [multiattack.activities[0].model_copy(update=update)]}
        )
    elif change == "duplicate":
        actions.append(multiattack)
    else:
        actions.clear()

    monster = bandit.model_copy(update={"actions": [*actions, multiattack]})
    request, loader = bind_monster(request, monster, potion)
    monkeypatch.setattr(RNGState, "restore", lambda _: pytest.fail("invalid template restored RNG"))
    result = init.evaluate_combat_initialization_candidate(request, loader=loader)
    assert result.status == "unsupported"
    assert result.error.code == "init.template"
    assert result.state_delta is None
    assert result.rng_transition is None
    assert not result.proposed_events


@pytest.mark.parametrize(
    "field,value",
    [
        ("strength", 12),
        ("dexterity", 10),
        ("ac", 13),
        ("hp_max", 12),
        ("base_speed", 35),
        ("proficiency_bonus_override", 3),
        ("creature_type", "beast"),
        ("attack_bonus", 0),
        ("condition_immunities", ["poisoned"]),
        ("save_proficiencies", ["str"]),
        ("skill_proficiencies", ["stealth"]),
    ],
)
def test_template_mechanical_mismatch_refuses_before_rng(monkeypatch, field, value):
    request, _, _, loader, _, _ = real_case()
    npc = next(a for a in request.state_snapshot.character_states if a.entity_id == NPC)
    npc.__dict__[field] = value
    monkeypatch.setattr(RNGState, "restore", lambda _: pytest.fail("mismatch restored RNG"))
    result = init.evaluate_combat_initialization_candidate(request, loader=loader)
    assert result.status == "unsupported"
    assert result.error.code == "init.template"
    assert result.state_delta is None
    assert result.rng_transition is None
    assert not result.proposed_events


@pytest.mark.parametrize("mutation", ["absent", "revision", "complex", "initiative"])
def test_invalid_template_fact_refuses_before_rng(monkeypatch, mutation):
    request, _, _, loader, _, _ = real_case()
    setup = request.state_snapshot.combat_setup
    binding = setup.npc_stat_blocks[0]
    if mutation == "absent":
        binding.__dict__["monster_slug"] = "unapproved-missing-template"
    elif mutation == "revision":
        binding.__dict__["data_revision"] = "sha256:" + "0" * 64
    elif mutation == "complex":
        binding.__dict__["monster_slug"] = "troll"
    else:
        setup.initiative_modifiers[NPC] = None
    monkeypatch.setattr(RNGState, "restore", lambda _: pytest.fail("invalid template restored RNG"))
    result = init.evaluate_combat_initialization_candidate(request, loader=loader)
    assert result.status == "unsupported"
    assert result.error.code == "init.template"
    assert result.state_delta is None
    assert result.rng_transition is None


def test_binding_schema_identity_and_creation_delta_closure():
    request, _, _, loader, _, _ = real_case()
    value = request.model_dump(mode="python")
    value["state_snapshot"]["combat_setup"]["npc_stat_blocks"][0]["actor_id"] = HERO
    with pytest.raises(ValidationError):
        init.CombatInitializationCandidateRequest.model_validate(value)
    value = request.model_dump(mode="python")
    bindings = value["state_snapshot"]["combat_setup"]["npc_stat_blocks"]
    value["state_snapshot"]["combat_setup"]["npc_stat_blocks"] = bindings + bindings
    with pytest.raises(ValidationError):
        init.CombatInitializationCandidateRequest.model_validate(value)
    result = init.evaluate_combat_initialization_candidate(request, loader=loader)
    value = result.state_delta.operations[0].model_dump(mode="python")
    value["value"]["monster_slug_by_entity"] = {}
    with pytest.raises(ValidationError):
        CombatCreationCandidate.model_validate(value)


def test_pinned_binding_mismatch_is_external_error_before_rng(monkeypatch):
    request, _, _, loader, _, _ = real_case()
    request.ruleset_binding.__dict__["data_revision"] = "sha256:" + "0" * 64
    monkeypatch.setattr(RNGState, "restore", lambda _: pytest.fail("binding error restored RNG"))
    with pytest.raises(RulesetBindingError):
        init.evaluate_combat_initialization_candidate(request, loader=loader)


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_real_npc_post_roll_fault_has_no_registry_or_partial_state(monkeypatch, failure):
    request, _, _, loader, _, _ = real_case(rolled=True)
    before = deepcopy(request)
    registry = orch._REGISTRY
    saved = dict(registry)
    original = init.resolve_initiative_value
    values = []

    def broken(*args, **kwargs):
        values.append(original(*args, **kwargs))
        raise failure("R14 after Initiative")

    isolated(monkeypatch)
    monkeypatch.setattr(init, "resolve_initiative_value", broken)
    with pytest.raises(failure, match="R14 after Initiative"):
        init.evaluate_combat_initialization_candidate(request, loader=loader)
    assert values == [13]
    assert request == before
    assert saved == dict(registry)
