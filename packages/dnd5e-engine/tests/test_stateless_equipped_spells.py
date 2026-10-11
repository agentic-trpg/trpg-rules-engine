"""Real equipped Cleric spells, preserved facts and subsequent weapon attacks."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from random import Random

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd5e_srd_data.schema.spell import SpellComponent

from dnd5e_engine import evaluation_spells
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_engine.evaluation_snapshot import capture_combat_snapshot
from dnd5e_engine.events import DamageApplied, HealingApplied, SaveRolled, SpellCast
from dnd5e_engine.specs import GridScene
from dnd5e_engine.spellcasting import spell_component_failure
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import execute
from tests.test_stateless_damage_spell import CLERIC_KNOWN
from tests.test_stateless_equipped_class_attack import equipped_case
from tests.test_stateless_item_closure_core import isolate
from tests.test_stateless_spell_feature import apply_healing


def equipped_spell_case(
    *, spell="cure-wounds", npc=False, seed=0, shield=False, two_handed=False, weapon="quarterstaff"
):
    request, old, handle, live = equipped_case(
        weapon=weapon, npc=npc, seed=seed, shield=shield, two_handed=two_handed
    )
    canonical = BundledAssetLoader()
    loader = MemoryAssetLoader(
        classes=[old.get_class(s) for s in old.list_slugs("classes")],
        features=[old.get_feature(s) for s in old.list_slugs("features")],
        items=[old.get_item(s) for s in old.list_slugs("items")],
        spells=[canonical.get_spell(s) for s in CLERIC_KNOWN],
    )
    live.ruleset_loader = loader
    live.initiative = [
        a.model_copy(
            update={
                "hp_current": 20,
                **(
                    {
                        "granted_features": (),
                        "dexterity": 14,
                        "save_proficiencies": ["dex"],
                        "proficiency_bonus_override": None,
                    }
                    if a.entity_id == FOE
                    else {}
                ),
            }
        )
        for a in live.initiative
    ]
    live.tracked_hp = {a.entity_id: a.hp_current for a in live.initiative}
    live.spells_known_by_entity[HERO] = list(CLERIC_KNOWN)
    casting = request.state_snapshot.casting_state
    entry = casting.actors[0]
    source = entry.sources[0].model_copy(
        update={
            "known_spells": CLERIC_KNOWN,
            "prepared_spells": CLERIC_KNOWN[3:],
        }
    )
    casting = casting.model_copy(
        update={"actors": (entry.model_copy(update={"sources": (source,)}),)}
    )
    snapshot = capture_combat_snapshot(
        live,
        grid=GridScene(width=3, height=3),
        world_version=7,
        combat_id=request.state_snapshot.combat_state.combat_id,
        inventory_state=request.state_snapshot.inventory_state,
        resource_state=request.state_snapshot.resource_state,
        casting_state=casting,
    )
    request = request.model_copy(
        update={
            "state_snapshot": snapshot,
            "ruleset_binding": ruleset_binding(loader),
            "payload": CombatIntentPayload(
                intent_type="cast_spell",
                spell_id=spell,
                target_id=FOE,
                spell_source_id="class:cleric",
                slot_level=1 if spell == "cure-wounds" else None,
            ),
        }
    )
    return request, loader, handle, live


@pytest.mark.parametrize("npc", [False, True])
@pytest.mark.parametrize("spell", ["cure-wounds", "sacred-flame"])
@pytest.mark.parametrize("seed", [0, 1, 5, 31])
@pytest.mark.parametrize("weapon", ["mace", "quarterstaff"])
def test_equipped_spell_complete_legacy_and_independent(spell, npc, seed, weapon, monkeypatch):
    request, loader, handle, live = equipped_spell_case(
        spell=spell, npc=npc, seed=seed, weapon=weapon
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
    assert after == capture_combat_snapshot(
        live,
        grid=GridScene(width=3, height=3),
        world_version=7,
        combat_id=after.combat_state.combat_id,
        inventory_state=after.inventory_state,
        resource_state=after.resource_state,
        casting_state=after.casting_state,
    )
    independent = Random(seed)
    cast = next(e for e in result.proposed_events if isinstance(e, SpellCast))
    assert cast.components == ["V", "S"]
    assert cast.material is None
    assert not cast.material_consumed
    assert cast.material_cost_gp == 0
    if spell == "cure-wounds":
        amount = independent.randint(1, 8) + independent.randint(1, 8) + 3
        assert next(
            e.amount for e in result.proposed_events if isinstance(e, HealingApplied)
        ) == min(20, amount)
        assert after.character_states[0].spell_slots == {1: 1}
    else:
        damage = independent.randint(1, 8)  # Original resolver rolls damage before the Save.
        natural = independent.randint(1, 20)
        save = next(e for e in result.proposed_events if isinstance(e, SaveRolled))
        assert save.dc == 13
        assert save.natural == natural
        assert save.modifier == 4
        assert save.succeeded == (natural + 4 >= 13)
        actual = sum(e.amount for e in result.proposed_events if isinstance(e, DamageApplied))
        assert actual == (0 if save.succeeded else damage)
        assert after.resource_state == request.state_snapshot.resource_state
    assert after.inventory_state == request.state_snapshot.inventory_state
    assert after.casting_state == request.state_snapshot.casting_state
    assert request == before


@pytest.mark.parametrize("npc", [False, True])
@pytest.mark.parametrize("spell", ["cure-wounds", "sacred-flame"])
def test_equipped_spell_pass_reset_same_weapon_attack(spell, npc, monkeypatch):
    request, loader, handle, live = equipped_spell_case(spell=spell, npc=npc)
    snapshot, rng = request.state_snapshot, request.rng_context
    steps = [
        (HERO, request.payload),
        (FOE, CombatIntentPayload(intent_type="pass")),
        (HERO, CombatIntentPayload(intent_type="attack", weapon_id="quarterstaff", target_id=FOE)),
    ]
    for actor_id, intent in steps:
        asyncio.run(orch.submit_player_intent(handle, actor_id, intent))
    events = []
    with monkeypatch.context() as scoped:
        isolate(scoped)
        scoped.setattr(orch, "_submit_live_intent", lambda *a, **kw: pytest.fail("Legacy executor"))
        for actor_id, intent in steps:
            current = request.model_copy(
                update={
                    "actor_id": actor_id,
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
            assert snapshot.inventory_state == request.state_snapshot.inventory_state
            assert snapshot.casting_state == request.state_snapshot.casting_state
            hero = snapshot.character_states[0]
            assert hero.weapon_in_hands == "quarterstaff"
            assert hero.weapon_grip == "one_handed"
            assert hero.ac == 15
            assert hero.worn_armor == "medium"
            assert hero.classes == {"cleric": 1}
            assert set(hero.granted_features) == {
                "spellcasting",
                "divine-order",
                "divine-order-protector",
            }
    assert snapshot == capture_combat_snapshot(
        live,
        grid=GridScene(width=3, height=3),
        world_version=7,
        combat_id=snapshot.combat_state.combat_id,
        inventory_state=snapshot.inventory_state,
        resource_state=snapshot.resource_state,
        casting_state=snapshot.casting_state,
    )
    assert tuple(events) == tuple(live.event_log)
    assert rng.state == RNGState.capture(live.rng)


@pytest.mark.parametrize("spell", ["cure-wounds", "sacred-flame"])
@pytest.mark.parametrize("shield,two_handed", [(True, False), (False, True)])
def test_no_free_somatic_hand_refuses_before_rng(spell, shield, two_handed, monkeypatch):
    request, loader, _, live = equipped_spell_case(
        spell=spell, shield=shield, two_handed=two_handed
    )
    before = deepcopy(request)
    rng = live.rng.getstate()
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("RNG restored"))
    result = execute(request, loader)
    assert result.status == "rejected"
    assert result.error.code == "spell.source"
    assert "Somatic" in result.error.reason
    assert result.state_delta is None
    assert result.rng_transition is None
    assert not result.proposed_events
    assert request == before
    assert live.rng.getstate() == rng
    assert not live.event_log


@pytest.mark.parametrize(
    "problem",
    [
        "verbal",
        "source",
        "unprepared",
        "unknown",
        "empty-slot",
        "training",
        "armor",
        "unknown-feature",
        "instance",
        "attunement",
        "material-drift",
    ],
)
def test_equipped_cast_refusals_keep_complete_facts(problem, monkeypatch):
    request, loader, _, _ = equipped_spell_case()
    snapshot = request.state_snapshot
    actor = snapshot.character_states[0]
    casting = snapshot.casting_state
    entry = casting.actors[0]
    if problem == "verbal":
        casting = casting.model_copy(
            update={"actors": (entry.model_copy(update={"verbal_available": False}),)}
        )
    elif problem == "source":
        request = request.model_copy(
            update={"payload": request.payload.model_copy(update={"spell_source_id": "absent"})}
        )
    elif problem in {"unprepared", "unknown"}:
        source = entry.sources[0].model_copy(
            update={
                "prepared_spells": (),
                **({"known_spells": ("sacred-flame",)} if problem == "unknown" else {}),
            }
        )
        casting = casting.model_copy(
            update={"actors": (entry.model_copy(update={"sources": (source,)}),)}
        )
        if problem == "unknown":
            actor = actor.model_copy(update={"spells_known": ["sacred-flame"]})
    elif problem == "empty-slot":
        actor = actor.model_copy(update={"spell_slots": {1: 0}})
        snapshot = snapshot.model_copy(
            update={
                "resource_state": snapshot.resource_state.model_copy(
                    update={
                        "pools": (
                            snapshot.resource_state.pools[0].model_copy(update={"current": 0}),
                        )
                    }
                )
            }
        )
    elif problem in {"training", "armor", "unknown-feature"}:
        actor = actor.model_copy(
            update={
                "training": {"weapon_proficiencies": []},
                "armor": {"ac": 19},
                "unknown-feature": {"granted_features": (*actor.granted_features, "war-caster")},
            }[problem]
        )
    elif problem == "instance":
        entries = list(snapshot.inventory_state.entries)
        entries[0] = entries[0].model_copy(update={"accessible": False})
        snapshot = snapshot.model_copy(
            update={
                "inventory_state": snapshot.inventory_state.model_copy(
                    update={"entries": tuple(entries)}
                )
            }
        )
    else:
        loader = MemoryAssetLoader(
            classes=[loader.get_class(s) for s in loader.list_slugs("classes")],
            features=[loader.get_feature(s) for s in loader.list_slugs("features")],
            items=[
                loader.get_item(s).model_copy(update={"requires_attunement": True})
                if problem == "attunement" and s == "quarterstaff"
                else loader.get_item(s)
                for s in loader.list_slugs("items")
            ],
            spells=[
                loader.get_spell(s).model_copy(update={"components": [SpellComponent.MATERIAL]})
                if problem == "material-drift" and s == "cure-wounds"
                else loader.get_spell(s)
                for s in loader.list_slugs("spells")
            ],
        )
        request = request.model_copy(update={"ruleset_binding": ruleset_binding(loader)})
    snapshot = snapshot.model_copy(
        update={"casting_state": casting, "character_states": (actor, snapshot.character_states[1])}
    )
    request = request.model_copy(update={"state_snapshot": snapshot})
    before = deepcopy(request)
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("RNG restored"))
    result = execute(request, loader)
    assert result.status in {"rejected", "unsupported"}, result.error
    assert result.state_delta is None
    assert result.rng_transition is None
    assert not result.proposed_events
    assert request == before


@pytest.mark.parametrize(
    "components,verbal,hand,material,expected",
    [
        (["V"], False, True, False, "Verbal"),
        (["S"], False, True, False, None),
        (["V"], True, False, False, None),
        (["S"], True, False, False, "Somatic"),
        (["V", "S", "M"], True, True, False, "Material"),
    ],
)
def test_shared_component_gate_uses_actual_components(components, verbal, hand, material, expected):
    spell = (
        BundledAssetLoader()
        .get_spell("cure-wounds")
        .model_copy(update={"components": [SpellComponent(c) for c in components]})
    )
    failure = spell_component_failure(
        spell, verbal_available=verbal, somatic_hand_available=hand, material_available=material
    )
    assert failure is None if expected is None else expected in failure


@pytest.mark.parametrize("spell", ["cure-wounds", "sacred-flame"])
@pytest.mark.parametrize("cancel", [False, True])
def test_equipped_cast_replay_and_actual_resolver_fault(spell, cancel, monkeypatch):
    request, loader, _, live = equipped_spell_case(spell=spell)
    before = deepcopy(request)
    live_rng = live.rng.getstate()
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request, loader)
        with ThreadPoolExecutor(4) as pool:
            assert list(pool.map(lambda _: execute(request, loader), range(8))) == [result] * 8
    after = apply_healing(request.state_snapshot, result)
    with pytest.raises(AssertionError):
        apply_healing(after, result)
    with pytest.raises(AssertionError):
        apply_healing(request.state_snapshot, result, world_version=8)
    real = evaluation_spells.resolve_activity

    def fault(activity, context):
        real(activity, context)
        raise asyncio.CancelledError() if cancel else RuntimeError("after actual spell")

    monkeypatch.setattr(evaluation_spells, "resolve_activity", fault)
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        execute(request, loader)
    assert request == before
    assert live.rng.getstate() == live_rng
    assert not live.event_log


def test_old_evaluator_binding_refuses_before_rng(monkeypatch):
    from dnd5e_engine.evaluation_ruleset import RulesetBindingError

    request, loader, _, _ = equipped_spell_case()
    request = request.model_copy(
        update={
            "ruleset_binding": request.ruleset_binding.model_copy(
                update={"evaluator_version": "dnd5e-evaluation/31"}
            )
        }
    )
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("RNG restored"))
    with pytest.raises(RulesetBindingError):
        execute(request, loader)
