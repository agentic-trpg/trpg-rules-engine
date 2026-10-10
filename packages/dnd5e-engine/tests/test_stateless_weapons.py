"""Canonical weapons, explicit grip/mastery state, and shared PC/NPC execution."""

import asyncio
from copy import deepcopy

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from pydantic import ValidationError

from dnd5e_engine import orchestrator as orch
from dnd5e_engine.evaluation_availability import (
    ActionAvailabilityRequest,
    query_action_availability,
)
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, RuleEvaluationRequest
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_engine.evaluation_snapshot import capture_combat_snapshot
from dnd5e_engine.evaluation_state import InventoryState
from dnd5e_engine.events import AttackRolled, DamageApplied
from dnd5e_engine.specs import GridScene
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import apply_delta, execute, request_and_live


def weapon_case(
    slug="rapier",
    *,
    two_handed=False,
    npc=False,
    seed=0,
    strength=16,
    dexterity=18,
    distance=1,
    ac=1,
):
    request, handle, live = request_and_live(seed=seed, ac=ac)
    weapon = BundledAssetLoader().get_weapon(slug)
    loader = MemoryAssetLoader(items=[weapon])
    live.ruleset_loader = loader
    index = int(npc)
    actor_id = FOE if npc else HERO
    target_id = HERO if npc else FOE
    for i, actor in enumerate(live.initiative):
        live.initiative[i] = actor.model_copy(
            update={
                "weapon_in_hands": None,
                "weapon_grip": "none",
                "other_hand_occupied": False,
                "weapon_mastery_slugs": (),
            }
        )
    live.initiative[index] = live.initiative[index].model_copy(
        update={
            "carried_item_slugs": (slug,),
            "weapon_in_hands": slug,
            "weapon_grip": "two_handed" if two_handed else "one_handed",
            "strength": strength,
            "dexterity": dexterity,
            "attack_bonus": None,
            "proficiency_bonus_override": 2,
            "weapon_proficiencies": [weapon.weapon_category],
        }
    )
    if npc:
        live.current_turn_index = 1
        live.current_actor_id = FOE
        live.initiative[0] = live.initiative[0].model_copy(update={"ac": ac})
    live.actor_zone[target_id] = f"{distance},0"
    live.actor_zone[actor_id] = "0,0"
    snapshot = capture_combat_snapshot(
        live,
        inventory_state=InventoryState(entries=()),
        grid=GridScene(width=4, height=3),
        world_version=7,
        combat_id="combat:synthetic",
        snapshot_schema_version="engine-snapshot/6",
    )
    request = request.model_copy(
        update={
            "schema_version": "engine-evaluation/9",
            "actor_id": actor_id,
            "payload": CombatIntentPayload(
                intent_type="attack", weapon_id=slug, target_id=target_id, two_handed=two_handed
            ),
            "state_snapshot": snapshot,
            "ruleset_binding": ruleset_binding(loader),
        }
    )
    return request, handle, live, loader


def availability(request, loader):
    query = ActionAvailabilityRequest(
        schema_version="engine-availability/5",
        session_id=request.session_id,
        operation_kind="combat.intent",
        actor_id=request.actor_id,
        payload=request.payload,
        state_snapshot=request.state_snapshot,
        ruleset_binding=request.ruleset_binding,
    )
    return query_action_availability(query, loader=loader)


@pytest.mark.parametrize("npc", [False, True])
@pytest.mark.parametrize("seed,ac", [(0, 1), (1, 99), (31, 1), (5, 1)])
@pytest.mark.parametrize(
    "slug,two_handed,distance",
    [
        ("rapier", False, 1),
        ("longsword", False, 1),
        ("longsword", True, 1),
        ("quarterstaff", True, 1),
        ("greatclub", True, 1),
        ("greatsword", True, 1),
        ("glaive", True, 2),
        ("whip", False, 2),
        ("battleaxe", False, 1),
        ("flail", False, 1),
        ("greataxe", True, 1),
        ("halberd", True, 2),
        ("lance", True, 2),
        ("mace", False, 1),
        ("maul", True, 1),
        ("morningstar", False, 1),
        ("pike", True, 2),
        ("staff", False, 1),
        ("war-pick", False, 1),
        ("warhammer", False, 1),
        ("wooden-staff", False, 1),
    ],
)
def test_real_weapons_match_legacy_full_state_rng_events_and_budgets(
    monkeypatch, npc, seed, ac, slug, two_handed, distance
):
    request, handle, live, loader = weapon_case(
        slug, npc=npc, seed=seed, ac=ac, two_handed=two_handed, distance=distance
    )
    before = deepcopy(request)
    if slug == "war-pick":
        # A confirmed bundled-data gap: Versatile without alternate damage.
        assert loader.get_weapon(slug).versatile_damage is None
        monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("RNG restored"))
        assert availability(request, loader).status == "unknown"
        refused = execute(request, loader)
        assert refused.status == "unsupported"
        assert refused.error.code == "damage.capability"
        assert refused.state_delta is None
        assert refused.rng_transition is None
        assert not refused.proposed_events
        assert request == before
        return
    assert availability(request, loader).status == "available"
    asyncio.run(orch.submit_player_intent(handle, request.actor_id, request.payload))
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert result.proposed_events == tuple(live.event_log)
    assert result.rng_transition.next_state == RNGState.capture(live.rng)
    expected = capture_combat_snapshot(
        live,
        inventory_state=InventoryState(entries=()),
        grid=GridScene(width=4, height=3),
        world_version=7,
        combat_id="combat:synthetic",
        snapshot_schema_version="engine-snapshot/6",
    )
    assert apply_delta(request.state_snapshot, result) == expected
    assert not expected.combat_state.vex_grants
    assert not expected.combat_state.sap_marks
    assert not expected.combat_state.slow_marks
    assert result == execute(request, loader)
    assert request == before


@pytest.mark.parametrize("strength,dexterity,modifier", [(10, 18, 6), (18, 10, 6), (10, 10, 2)])
def test_finesse_uses_shared_best_ability(strength, dexterity, modifier):
    request, _, _, loader = weapon_case(strength=strength, dexterity=dexterity)
    result = execute(request, loader)
    attack = next(e for e in result.proposed_events if isinstance(e, AttackRolled))
    assert attack.modifier == modifier


def test_versatile_alternate_damage_and_heavy_threshold():
    one, _, _, loader = weapon_case("longsword", seed=1)
    two, _, _, _ = weapon_case("longsword", two_handed=True, seed=1)
    one_result, two_result = execute(one, loader), execute(two, loader)
    one_damage = next(e for e in one_result.proposed_events if isinstance(e, DamageApplied))
    two_damage = next(e for e in two_result.proposed_events if isinstance(e, DamageApplied))
    # Seed 1: natural 5, then d8=2 / d10=10; STR 16 contributes +3.
    assert one_damage.amount == 5
    assert two_damage.amount == 13
    for strength, mode in [(12, "disadvantage"), (13, "normal")]:
        request, _, _, loader = weapon_case("greatsword", two_handed=True, strength=strength)
        result = execute(request, loader)
        attack = next(e for e in result.proposed_events if isinstance(e, AttackRolled))
        assert attack.advantage == mode


@pytest.mark.parametrize("slug", ["dagger", "shortsword", "spear", "shortbow", "light-crossbow"])
def test_unmigrated_properties_refused_before_rng_and_match_availability(monkeypatch, slug):
    request, _, _, loader = weapon_case(slug)
    before = deepcopy(request)
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("execution RNG restored"))
    assert execute(request, loader).status == "unsupported"
    assert availability(request, loader).status == "unknown"
    assert request == before


@pytest.mark.parametrize("mutation", ["grip", "reach", "mastery", "held", "budget"])
def test_equipment_and_resource_preflight(monkeypatch, mutation):
    request, _, _, loader = weapon_case("greatsword", two_handed=True)
    actor = request.state_snapshot.character_states[0]
    expected = "rejected"
    if mutation == "grip":
        request = request.model_copy(
            update={"payload": request.payload.model_copy(update={"two_handed": False})}
        )
    elif mutation == "reach":
        request.state_snapshot.combat_state.actor_zone[FOE] = "3,0"
    elif mutation == "mastery":
        actor.__dict__["weapon_mastery_slugs"] = ("greatsword",)
        expected = "unsupported"
    elif mutation == "held":
        actor.__dict__.update(weapon_in_hands=None, weapon_grip="none")
    else:
        actor.__dict__.update(action_available=False, attacks_remaining=0)
    before = deepcopy(request)
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("execution RNG restored"))
    result = execute(request, loader)
    assert result.status == expected
    assert result.state_delta is None
    assert result.rng_transition is None
    assert not result.proposed_events
    assert availability(request, loader).status == (
        "unknown" if expected == "unsupported" else "unavailable"
    )
    assert request == before


@pytest.mark.parametrize(
    "field", ["weapon_in_hands", "weapon_grip", "other_hand_occupied", "weapon_mastery_slugs"]
)
def test_new_equipment_fields_required_and_versioned(field):
    request, _, _, loader = weapon_case()
    wire = request.model_dump(mode="python")
    del wire["state_snapshot"]["character_states"][0][field]
    with pytest.raises(ValidationError):
        RuleEvaluationRequest.model_validate(wire)
    with pytest.raises(ValidationError, match="engine-evaluation/9"):
        execute(request.model_copy(update={"schema_version": "engine-evaluation/2"}), loader)


def test_inconsistent_two_hand_equipment_is_schema_error():
    request, _, _, loader = weapon_case("greatsword", two_handed=True)
    request.state_snapshot.character_states[0].__dict__["other_hand_occupied"] = True
    with pytest.raises(ValidationError, match="occupied other hand"):
        execute(request, loader)


def test_capture_cannot_discard_explicit_equipment_dependencies():
    _, _, live, _ = weapon_case()
    live.initiative[0] = live.initiative[0].model_copy(update={"weapon_grip": None})
    with pytest.raises(ValidationError, match="weapon_grip"):
        capture_combat_snapshot(
            live,
            inventory_state=InventoryState(entries=()),
            grid=GridScene(width=4, height=3),
            world_version=7,
            combat_id="combat:synthetic",
        )


@pytest.mark.parametrize("training,damage", [(None, True), ((), False), (("greatsword",), True)])
def test_shared_mastery_training_gate_preserves_legacy_default(training, damage):
    request, handle, live, _loader = weapon_case("greatsword", two_handed=True, ac=99, seed=1)
    live.initiative[0] = live.initiative[0].model_copy(update={"weapon_mastery_slugs": training})
    asyncio.run(orch.submit_player_intent(handle, HERO, request.payload))
    hits = [e for e in live.event_log if isinstance(e, DamageApplied)]
    assert bool(hits) is damage
    if damage:
        assert hits[0].amount == 3  # Graze: STR modifier, independent of damage dice.


def test_equipped_shield_cannot_be_hidden_by_the_new_hand_flag(monkeypatch):
    request, _, _, loader = weapon_case("greatsword", two_handed=True)
    actors = list(request.state_snapshot.character_states)
    actors[0] = actors[0].model_copy(update={"shield_equipped": True})
    request = request.model_copy(
        update={
            "state_snapshot": request.state_snapshot.model_copy(
                update={"character_states": tuple(actors)}
            )
        }
    )
    monkeypatch.setattr(
        RNGState, "restore", lambda self: pytest.fail("RNG before schema rejection")
    )
    with pytest.raises(ValidationError, match="two-handed grip"):
        execute(request, loader)
