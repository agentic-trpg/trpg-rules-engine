"""Complete equipped class actors: independent magnitudes, Legacy and isolation."""

import asyncio
import random
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader

from dnd5e_engine import evaluation_attack
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, RuleEvaluationResult
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_engine.evaluation_snapshot import capture_combat_snapshot
from dnd5e_engine.evaluation_state import InventoryEntry, InventoryState
from dnd5e_engine.events import AttackRolled, DamageApplied
from dnd5e_engine.specs import GridScene
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import execute
from tests.test_stateless_item_closure_core import isolate
from tests.test_stateless_spell_feature import apply_healing, healing_case


def equipped_case(
    *,
    seed=0,
    npc=False,
    shield=True,
    two_handed=False,
    weapon="mace",
    ac=10,
    defense=None,
    temp_hp=0,
):
    request, old, handle, live = healing_case(seed=seed, hp=40)
    canonical = BundledAssetLoader()
    loader = MemoryAssetLoader(
        classes=[old.get_class(s) for s in old.list_slugs("classes")],
        features=[old.get_feature(s) for s in old.list_slugs("features")]
        + [canonical.get_feature("divine-order-protector")],
        spells=[old.get_spell("cure-wounds")],
        items=[canonical.get_item(s) for s in ("mace", "quarterstaff", "chain-shirt", "shield")],
    )
    slugs = (weapon, "chain-shirt") + (("shield",) if shield else ())
    hero, foe = live.initiative
    hero = hero.model_copy(
        update={
            "entity_type": "NPC" if npc else "Character",
            "strength": 16,
            "dexterity": 18,
            "attack_bonus": None,
            "proficiency_bonus_override": 2,
            "save_proficiencies": ["wis", "cha"],
            "weapon_proficiencies": [
                "simple_melee",
                "simple_ranged",
                "martial_melee",
                "martial_ranged",
            ],
            "granted_features": (*hero.granted_features, "divine-order-protector"),
            "carried_item_slugs": slugs,
            "weapon_in_hands": weapon,
            "weapon_grip": "two_handed" if two_handed else "one_handed",
            "other_hand_occupied": shield,
            "shield_equipped": shield,
            "worn_armor": "medium",
            "ac": 17 if shield else 15,
        }
    )
    update = {"ac": ac, "temp_hp": temp_hp, "entity_type": "Character" if npc else "Monster"}
    if defense:
        update[defense] = ["bludgeoning"]
    live.initiative = [hero, foe.model_copy(update=update)]
    live.tracked_temp_hp[FOE] = temp_hp
    if npc:
        live.party_ids, live.encounter_ids = {FOE}, {HERO}
    live.ruleset_loader = loader
    inventory = InventoryState(
        entries=tuple(
            InventoryEntry(
                instance_id="hero:" + s,
                owner_id=HERO,
                item_slug=s,
                quantity=1,
                charges_remaining_per_unit=0,
                accessible=True,
            )
            for s in slugs
        )
    )
    snapshot = capture_combat_snapshot(
        live,
        grid=GridScene(width=3, height=3),
        world_version=7,
        combat_id=request.state_snapshot.combat_state.combat_id,
        inventory_state=inventory,
        resource_state=request.state_snapshot.resource_state,
        casting_state=request.state_snapshot.casting_state,
    )
    request = request.model_copy(
        update={
            "state_snapshot": snapshot,
            "ruleset_binding": ruleset_binding(loader),
            "payload": CombatIntentPayload(
                intent_type="attack", weapon_id=weapon, target_id=FOE, two_handed=two_handed
            ),
        }
    )
    return request, loader, handle, live


@pytest.mark.parametrize("npc", [False, True])
@pytest.mark.parametrize("seed,ac", [(0, 10), (1, 99), (5, 10), (31, 10)])
@pytest.mark.parametrize(
    "defense", [None, "damage_resistances", "damage_immunities", "damage_vulnerabilities"]
)
def test_complete_equipped_attack_independent_and_legacy(seed, ac, npc, defense, monkeypatch):
    request, loader, handle, live = equipped_case(
        seed=seed, ac=ac, npc=npc, defense=defense, temp_hp=2
    )
    before = deepcopy(request)
    asyncio.run(orch.submit_player_intent(handle, HERO, request.payload))
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert result.proposed_events == tuple(live.event_log)
    assert result.rng_transition.next_state == RNGState.capture(live.rng)
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
    roll = next(e for e in result.proposed_events if isinstance(e, AttackRolled))
    independent = random.Random(seed)
    natural = independent.randint(1, 20)
    assert roll.modifier == 5  # STR 16 (+3) + level-1 PB (+2), no Sap training.
    assert roll.is_hit == (natural == 20 or (natural != 1 and natural + 5 >= ac))
    if roll.is_hit:
        raw = sum(independent.randint(1, 6) for _ in range(2 if natural == 20 else 1)) + 3
        expected = (
            raw // 2
            if defense == "damage_resistances"
            else 0
            if defense == "damage_immunities"
            else raw * 2
            if defense == "damage_vulnerabilities"
            else raw
        )
        damage = next(e for e in result.proposed_events if isinstance(e, DamageApplied))
        assert damage.amount == expected
        assert after.character_states[1].temp_hp == max(0, 2 - expected)
        assert after.character_states[1].hp_current == 40 - max(0, expected - 2)
    assert after.inventory_state == request.state_snapshot.inventory_state
    assert after.resource_state == request.state_snapshot.resource_state
    assert after.character_states[0].ac == 17  # 13 + min(DEX +4, 2) + shield 2.
    assert not after.combat_state.sap_marks
    assert RuleEvaluationResult.model_validate_json(result.model_dump_json()) == result
    assert request == before


@pytest.mark.parametrize("npc", [False, True])
def test_equipped_attack_pass_reset_next_attack(npc, monkeypatch):
    request, loader, handle, live = equipped_case(npc=npc)
    steps = [
        (HERO, request.payload),
        (FOE, CombatIntentPayload(intent_type="pass")),
        (HERO, CombatIntentPayload(intent_type="pass")),
        (FOE, CombatIntentPayload(intent_type="pass")),
        (HERO, request.payload),
    ]
    for actor, intent in steps:
        asyncio.run(orch.submit_player_intent(handle, actor, intent))
    snapshot, rng = request.state_snapshot, request.rng_context
    events = []
    with monkeypatch.context() as scoped:
        isolate(scoped)
        for actor, intent in steps:
            current = request.model_copy(
                update={
                    "actor_id": actor,
                    "payload": intent,
                    "state_snapshot": snapshot,
                    "rng_context": rng,
                }
            )
            result = execute(current, loader)
            assert result.status == "accepted", result.error
            assert execute(current, loader) == result
            snapshot = apply_healing(snapshot, result)
            rng = rng.model_copy(update={"state": result.rng_transition.next_state})
            events.extend(result.proposed_events)
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
    assert tuple(events) == tuple(live.event_log)
    assert rng.state == RNGState.capture(live.rng)
    assert snapshot.combat_state.round_number == 3
    assert snapshot.character_states[0].classes == {"cleric": 1}
    assert set(snapshot.character_states[0].granted_features) == {
        "spellcasting",
        "divine-order",
        "divine-order-protector",
    }


@pytest.mark.parametrize(
    "two_handed,shield,expected", [(False, True, 7), (False, False, 7), (True, False, 10)]
)
def test_quarterstaff_grip_and_armor(two_handed, shield, expected):
    request, loader, _, _ = equipped_case(
        weapon="quarterstaff", two_handed=two_handed, shield=shield
    )
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert (
        next(e.amount for e in result.proposed_events if isinstance(e, DamageApplied)) == expected
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("weapon_proficiencies", None),
        ("weapon_proficiencies", []),
        ("save_proficiencies", []),
        ("attack_bonus", 99),
        ("proficiency_bonus_override", 9),
        ("ac", 19),
        ("other_hand_occupied", False),
        ("weapon_mastery_slugs", ("mace",)),
        ("fighting_styles", ("defense",)),
        ("granted_features", ("spellcasting",)),
        ("custom_counters", {"mystery": {"spent": 0}}),
    ],
)
def test_equipment_dependency_refusal_before_rng(field, value, monkeypatch):
    request, loader, _, _ = equipped_case()
    snapshot = request.state_snapshot
    actor = snapshot.character_states[0].model_copy(update={field: value})
    request = request.model_copy(
        update={
            "state_snapshot": snapshot.model_copy(
                update={"character_states": (actor, snapshot.character_states[1])}
            )
        }
    )
    before = deepcopy(request)
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("RNG restored"))
    result = execute(request, loader)
    assert result.status == "unsupported"
    assert result.state_delta is None
    assert result.rng_transition is None
    assert not result.proposed_events
    assert request == before


@pytest.mark.parametrize(
    "mutation", ["owner", "missing", "duplicate", "empty", "inaccessible", "charges"]
)
def test_equipment_instance_closure(mutation, monkeypatch):
    request, loader, _, _ = equipped_case()
    entries = list(request.state_snapshot.inventory_state.entries)
    if mutation == "missing":
        entries.pop()
    elif mutation == "duplicate":
        entries.append(entries[0].model_copy(update={"instance_id": "duplicate"}))
    else:
        changes = {
            "owner": {"owner_id": FOE},
            "empty": {"quantity": 0},
            "inaccessible": {"accessible": False},
            "charges": {"charges_remaining_per_unit": 1},
        }
        entries[0] = entries[0].model_copy(update=changes[mutation])
    request = request.model_copy(
        update={
            "state_snapshot": request.state_snapshot.model_copy(
                update={"inventory_state": InventoryState(entries=tuple(entries))}
            )
        }
    )
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("RNG restored"))
    assert execute(request, loader).status == "unsupported"


def test_equipped_replay_concurrency_fault_and_commit_guards(monkeypatch):
    request, loader, _, _ = equipped_case()
    before = deepcopy(request)
    result = execute(request, loader)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(lambda _: execute(request, loader), range(8))) == [result] * 8
    after = apply_healing(request.state_snapshot, result)
    with pytest.raises(AssertionError):
        apply_healing(after, result)
    with pytest.raises(AssertionError):
        apply_healing(request.state_snapshot, result, world_version=8)
    monkeypatch.setattr(
        evaluation_attack,
        "resolve_activity",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("fault")),
    )
    with pytest.raises(RuntimeError, match="fault"):
        execute(request, loader)
    assert request == before


@pytest.mark.parametrize("shield", [False, True])
def test_equipped_target_ac_controls_hit(shield, monkeypatch):
    request, loader, handle, live = equipped_case(seed=7)
    snapshot = request.state_snapshot
    slugs = ("mace", "chain-shirt") + (("shield",) if shield else ())
    target = live.initiative[0].model_copy(
        update={
            "entity_id": FOE,
            "entity_type": "NPC",
            "initiative": 1,
            "carried_item_slugs": slugs,
            "shield_equipped": shield,
            "other_hand_occupied": shield,
            "ac": 17 if shield else 15,
        }
    )
    live.initiative[1] = target
    for field in (
        "spell_slots_by_entity",
        "pact_slots_by_entity",
        "spells_known_by_entity",
        "custom_counters_by_entity",
    ):
        getattr(live, field)[FOE] = deepcopy(getattr(live, field)[HERO])
    inventory = InventoryState(
        entries=(
            *snapshot.inventory_state.entries,
            *(
                InventoryEntry(
                    instance_id="target:" + s,
                    owner_id=FOE,
                    item_slug=s,
                    quantity=1,
                    charges_remaining_per_unit=0,
                    accessible=True,
                )
                for s in slugs
            ),
        )
    )
    resources = snapshot.resource_state.model_copy(
        update={
            "pools": (
                *snapshot.resource_state.pools,
                snapshot.resource_state.pools[0].model_copy(update={"owner_id": FOE}),
            )
        }
    )
    casting = snapshot.casting_state.model_copy(
        update={
            "actors": (
                *snapshot.casting_state.actors,
                snapshot.casting_state.actors[0].model_copy(update={"actor_id": FOE}),
            )
        }
    )
    snapshot = capture_combat_snapshot(
        live,
        grid=GridScene(width=3, height=3),
        world_version=7,
        combat_id=snapshot.combat_state.combat_id,
        inventory_state=inventory,
        resource_state=resources,
        casting_state=casting,
    )
    request = request.model_copy(update={"state_snapshot": snapshot})
    asyncio.run(orch.submit_player_intent(handle, HERO, request.payload))
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request, loader)
    assert result.status == "accepted", result.error
    roll = next(e for e in result.proposed_events if isinstance(e, AttackRolled))
    assert roll.roll_total == 16
    assert roll.is_hit is (not shield)
    assert result.proposed_events == tuple(live.event_log)
    after = apply_healing(snapshot, result)
    assert after == capture_combat_snapshot(
        live,
        grid=GridScene(width=3, height=3),
        world_version=7,
        combat_id=snapshot.combat_state.combat_id,
        inventory_state=inventory,
        resource_state=resources,
        casting_state=casting,
    )


@pytest.mark.parametrize("kind", ["weapon", "armor", "shield"])
def test_equipment_asset_drift_refused(kind, monkeypatch):
    request, loader, _, _ = equipped_case()
    slug = {"weapon": "mace", "armor": "chain-shirt", "shield": "shield"}[kind]
    loader = MemoryAssetLoader(
        classes=[loader.get_class(s) for s in loader.list_slugs("classes")],
        features=[loader.get_feature(s) for s in loader.list_slugs("features")],
        spells=[loader.get_spell(s) for s in loader.list_slugs("spells")],
        items=[
            loader.get_item(s).model_copy(update={"magical_bonus": 1})
            if s == slug
            else loader.get_item(s)
            for s in loader.list_slugs("items")
        ],
    )
    request = request.model_copy(update={"ruleset_binding": ruleset_binding(loader)})
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("RNG restored"))
    assert execute(request, loader).status == "unsupported"
