"""R13 local calculation candidate, independent values and Legacy differential."""

import asyncio
import random
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from pydantic import ValidationError
from pydantic_core import to_json

from dnd5e_engine import evaluation_initialization as init
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest, RuleEvaluationResult
from dnd5e_engine.evaluation_delta import CombatCreationCandidate
from dnd5e_engine.evaluation_rng import RNGContext, RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_engine.evaluation_snapshot import capture_combat_snapshot
from dnd5e_engine.evaluation_state import (
    CombatSetupState,
    CombatSnapshot,
    InventoryEntry,
    InventoryState,
    NonCombatSnapshot,
    ObjectState,
    PersistentAreasState,
    TimedActivitiesState,
)
from dnd5e_engine.lib_loader import scoped_lib_loader
from dnd5e_engine.specs import EncounterMemberSpec, GridScene, PartyMemberSpec
from tests.evaluation_support import FOE, HERO, WEAPON, synthetic_loader
from tests.test_stateless_item_closure_core import isolate


def init_case(*, fixed=(20, 1), dex=(16, 10), surprised=False, seed=0):
    loader = synthetic_loader()
    scene = GridScene(width=4, height=4)
    with scoped_lib_loader(loader):
        started = asyncio.run(
            orch.start_combat(
                session_id="synthetic-init",
                rng_seed=seed,
                scene_location_id="scene:init",
                grid_scene=scene,
                party=[
                    PartyMemberSpec(
                        entity_id=HERO,
                        name="Hero",
                        initiative=fixed[0],
                        dexterity=dex[0],
                        hp_current=40,
                        hp_max=40,
                        equipment=(WEAPON,),
                        zone_id="0,0",
                        is_surprised=surprised,
                        spell_slots={1: 2},
                        pact_slots={1: 1},
                        spells_known=["known-fact"],
                        custom_counters={"pool": {"value": 2, "max": 3}},
                    )
                ],
                encounter=[
                    EncounterMemberSpec(
                        entity_id=FOE,
                        entity_type="Monster",
                        name="Foe",
                        initiative=fixed[1],
                        dexterity=dex[1],
                        hp_current=20,
                        hp_max=20,
                        xp_value=50,
                        zone_id="3,3",
                    )
                ],
            )
        )
    live = orch._get_live(started.handle)
    live.initiative = [
        a.model_copy(
            update={
                "weapon_in_hands": WEAPON if a.entity_id == HERO else None,
                "weapon_grip": "one_handed" if a.entity_id == HERO else "none",
                "other_hand_occupied": False,
                "weapon_mastery_slugs": (),
            }
        )
        for a in live.initiative
    ]
    expected = capture_combat_snapshot(
        live,
        resource_state=None,
        inventory_state=InventoryState(
            entries=(
                InventoryEntry(
                    instance_id="weapon:owned",
                    owner_id=HERO,
                    item_slug=WEAPON,
                    quantity=1,
                    charges_remaining_per_unit=0,
                    accessible=True,
                ),
            )
        ),
        grid=scene,
        world_version=7,
        combat_id="combat:init",
    )
    by_id = {a.entity_id: a for a in expected.character_states}
    actors = tuple(
        by_id[a].model_copy(
            update={
                "initiative": 999,
                "action_available": False,
                "bonus_action_available": False,
                "reaction_available": False,
                "movement_remaining": 1,
            }
        )
        for a in (HERO, FOE)
    )
    setup = CombatSetupState(
        combat_id="combat:init",
        party_ids=(HERO,),
        encounter_ids=(FOE,),
        roll_order=(HERO, FOE),
        actor_zone={HERO: "0,0", FOE: "3,3"},
        fixed_initiative={HERO: fixed[0], FOE: fixed[1]},
        initiative_modifiers={HERO: None, FOE: None},
        surprised_ids={HERO} if surprised else set(),
        xp_value_by_entity={FOE: 50},
        opportunity_attack_weapons={HERO: WEAPON},
        npc_stat_blocks=(),
        timed_activities=TimedActivitiesState(pending=[], next_sequence=0),
        persistent_areas=PersistentAreasState(
            areas=[], next_sequence=0, next_turn_start_effects=()
        ),
        combat_objects=ObjectState(objects=[], used_ids=set()),
    )
    snapshot = NonCombatSnapshot(
        resource_state=None,
        snapshot_schema_version="engine-snapshot/9",
        snapshot_kind="non_combat",
        session_id="synthetic-init",
        world_version=7,
        character_states=actors,
        effect_states=(),
        scene_state=expected.scene_state,
        inventory_state=expected.inventory_state,
        combat_setup=setup,
    )
    request = init.CombatInitializationCandidateRequest(
        schema_version="engine-evaluation/13",
        payload=init.CombatInitializationCandidatePayload(kind="combat.init.local-candidate"),
        session_id="synthetic-init",
        command_id="cmd:init",
        state_snapshot=snapshot,
        ruleset_binding=ruleset_binding(loader),
        rng_context=RNGContext(
            stream_id="main", version=2, state=RNGState.capture(random.Random(seed))
        ),
    )
    return request, expected, live, loader, started


def apply_creation(request, result, *, existing=None, world_version=7):
    assert existing is None
    snapshot = request.state_snapshot
    assert world_version == snapshot.world_version == result.state_delta.expected_world_version
    op = result.state_delta.operations[0]
    assert isinstance(op, CombatCreationCandidate)
    assert op.expected_absent
    assert op.expected_setup == snapshot.combat_setup
    value = snapshot.model_dump(mode="python")
    actors = {a["entity_id"]: a for a in value["character_states"]}
    for update in op.actor_updates:
        actor = actors[update.actor_id]
        assert actor["initiative"] == update.expected.initiative
        expected = update.expected.budget.model_dump()
        assert {key: actor[key] for key in expected} == expected
        actor.update(update.value.budget.model_dump())
        actor["initiative"] = update.value.initiative
    value.pop("combat_setup")
    value["snapshot_kind"] = "combat"
    value["combat_state"] = op.value.model_dump(mode="python")
    value["combat_state"]["event_log"] = tuple(e.model_dump() for e in result.proposed_events)
    # Combat roster order is explicit; Character collection order remains the input order.
    return CombatSnapshot.model_validate_json(to_json(value))


@pytest.mark.parametrize(
    "fixed,dex,surprised,expected_values,order",
    [
        ((20, 1), (16, 10), False, {HERO: 20, FOE: 1}, (HERO, FOE)),
        ((0, 0), (10, 10), False, {HERO: 0, FOE: 0}, (HERO, FOE)),
        ((10, 10), (10, 16), False, {HERO: 10, FOE: 10}, (FOE, HERO)),
        ((None, None), (16, 10), False, {HERO: 16, FOE: 14}, (HERO, FOE)),
        ((None, None), (16, 10), True, {HERO: 16, FOE: 2}, (HERO, FOE)),
    ],
)
def test_init_independent_values_full_state_events_rng_legacy_and_isolation(
    monkeypatch, fixed, dex, surprised, expected_values, order
):
    request, expected, live, loader, started = init_case(fixed=fixed, dex=dex, surprised=surprised)
    before = deepcopy(request)
    registered = dict(orch._REGISTRY)
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = init.evaluate_combat_initialization_candidate(request, loader=loader)
        assert result.status == "accepted", result.error
        with ThreadPoolExecutor(max_workers=3) as pool:
            assert (
                list(
                    pool.map(
                        lambda _: init.evaluate_combat_initialization_candidate(
                            request, loader=loader
                        ),
                        range(6),
                    )
                )
                == [result] * 6
            )
    op = result.state_delta.operations[0]
    assert op.value.initiative_ids == order
    assert {u.actor_id: u.value.initiative for u in op.actor_updates} == expected_values
    assert op.value.round_number == 1
    assert op.value.turn_serial == 1
    assert op.value.current_turn_index == 0
    assert result.proposed_events == tuple(started.events)
    assert result.rng_transition.next_state == RNGState.capture(live.rng)
    after = apply_creation(request, result)
    expected = expected.model_copy(
        update={
            "character_states": tuple(
                next(a for a in expected.character_states if a.entity_id == actor.entity_id)
                for actor in request.state_snapshot.character_states
            )
        }
    )
    assert after == expected
    assert all(
        a.action_available and a.bonus_action_available and a.reaction_available
        for a in after.character_states
    )
    assert all(
        a.movement_remaining == 30 and a.attacks_remaining == 1 for a in after.character_states
    )
    assert all(
        ledger.spent_ft == 0 and ledger.distance_ft == 0 and ledger.dash_count == 0
        for ledger in after.combat_state.movement_ledgers.values()
    )
    assert result.proposed_events[2].actor_id == order[0]
    assert after.inventory_state == request.state_snapshot.inventory_state
    assert after.inventory_state.entries[0].quantity == 1
    hero = after.character_states[0]
    assert hero.weapon_in_hands == WEAPON
    assert hero.spell_slots == {1: 2}
    assert hero.pact_slots == {1: 1}
    assert hero.custom_counters["pool"]["value"] == 2
    assert (
        not result.rng_transition.state_changed
        if all(v is not None for v in fixed)
        else result.rng_transition.state_changed
    )
    assert RuleEvaluationResult.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(AssertionError):
        apply_creation(request, result, existing=op.value)
    with pytest.raises(AssertionError):
        apply_creation(request, result, world_version=8)
    assert request == before
    assert registered == orch._REGISTRY


@pytest.mark.parametrize("mutation", ["dying", "feature", "condition", "equipment", "position"])
def test_unmigrated_initialization_refuses_before_roll(monkeypatch, mutation):
    request, _, _, loader, _ = init_case(fixed=(None, None))
    actor = request.state_snapshot.character_states[0]
    if mutation == "dying":
        actor.__dict__["hp_current"] = 0
    elif mutation == "feature":
        actor.__dict__["granted_features"] = ("complex",)
    elif mutation == "condition":
        actor.__dict__["concentration_effect_id"] = "effect"
    elif mutation == "equipment":
        actor.__dict__["carried_item_slugs"] = (WEAPON, "unknown")
    else:
        request.state_snapshot.combat_setup.actor_zone[HERO] = "99,0"
    monkeypatch.setattr(
        RNGState, "restore", lambda _: pytest.fail("unsupported input restored RNG")
    )
    result = init.evaluate_combat_initialization_candidate(request, loader=loader)
    assert result.status == "unsupported"
    assert result.state_delta is None
    assert result.rng_transition is None
    assert not result.proposed_events


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_post_initiative_fault_never_registers_or_returns_partial(monkeypatch, failure):
    request, _, _, loader, _ = init_case(fixed=(None, None))
    before = deepcopy(request)
    registry = orch._REGISTRY
    registered = dict(registry)
    original = init.resolve_initiative_value
    draws = []

    def broken(*args, **kwargs):
        value = original(*args, **kwargs)
        draws.append(value)
        raise failure("after initiative roll")

    isolate(monkeypatch)
    monkeypatch.setattr(init, "resolve_initiative_value", broken)
    with pytest.raises(failure, match="after initiative roll"):
        init.evaluate_combat_initialization_candidate(request, loader=loader)
    assert draws == [16]
    assert request == before
    assert registered == dict(registry)


def test_incomplete_setup_and_external_start_schema_are_rejected():
    request, _, _, _, _ = init_case()
    value = request.model_dump(mode="python")
    del value["state_snapshot"]["combat_setup"]["actor_zone"][FOE]
    with pytest.raises(ValidationError):
        init.CombatInitializationCandidateRequest.model_validate(value)
    value = request.model_dump(mode="python")
    value.update(operation_kind="combat.start", actor_id=HERO, payload={"kind": "combat.start"})
    with pytest.raises(ValidationError):
        RuleEvaluationRequest.model_validate(value)


@pytest.mark.parametrize("component", ["timed_activities", "persistent_areas", "combat_objects"])
def test_precombat_scene_components_are_explicit_and_refused_before_rng(monkeypatch, component):
    request, _, _, loader, _ = init_case(fixed=(None, None))
    state = getattr(request.state_snapshot.combat_setup, component)
    if component == "combat_objects":
        state.__dict__["used_ids"] = {"existing-object"}
    else:
        state.__dict__["next_sequence"] = 1
    monkeypatch.setattr(
        RNGState, "restore", lambda _: pytest.fail("precombat mechanics restored RNG")
    )
    result = init.evaluate_combat_initialization_candidate(request, loader=loader)
    assert result.status == "unsupported"
    assert result.error.code == "init.scene_mechanics"
    assert result.state_delta is None


def test_creation_delta_rejects_nonfresh_component_and_incomplete_actor_update():
    request, _, _, loader, _ = init_case()
    result = init.evaluate_combat_initialization_candidate(request, loader=loader)
    op = result.state_delta.operations[0]
    value = op.model_dump(mode="python")
    value["value"]["ended"] = True
    with pytest.raises(ValidationError):
        CombatCreationCandidate.model_validate(value)
    value = op.model_dump(mode="python")
    value["actor_updates"] = value["actor_updates"][:-1]
    with pytest.raises(ValidationError):
        CombatCreationCandidate.model_validate(value)
