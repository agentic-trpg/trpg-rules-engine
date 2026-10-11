"""Canonical Sacred Flame: real class sources, shared saves/damage and lineage."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from random import Random

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader

from dnd5e_engine import evaluation_spells
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.evaluation_casting import ActorCastingState, CastingState, ClassSpellSource
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd5e_engine.evaluation_resources import ResourceState, SlotResource
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_engine.evaluation_snapshot import capture_combat_snapshot
from dnd5e_engine.events import ConcentrationCheck, DamageApplied, SaveRolled, SpellCast
from dnd5e_engine.specs import GridScene
from tests.evaluation_support import FOE, HERO, WEAPON, synthetic_loader
from tests.test_stateless_attack import execute
from tests.test_stateless_attack_concentration import ATTACKER, attack_concentration_case
from tests.test_stateless_concentration import apply_candidate
from tests.test_stateless_item_closure_core import isolate
from tests.test_stateless_reactions import reaction_case
from tests.test_stateless_spell_feature import healing_case

CLERIC_KNOWN = (
    "sacred-flame",
    "guidance",
    "thaumaturgy",
    "bless",
    "cure-wounds",
    "guiding-bolt",
    "healing-word",
)


def damage_spell_case(
    *,
    seed=0,
    pc=True,
    concentrating=False,
    hp=40,
    temp=0,
    defense=None,
    dodge=False,
    empty_slots=False,
):
    if concentrating:
        request, handle, live, _ = attack_concentration_case(seed=seed, pc=not pc)
        caster_id = ATTACKER
        resources = request.state_snapshot.resource_state
        casting_actors = request.state_snapshot.casting_state.actors
    else:
        request, _, handle, live = healing_case(seed=seed, hp=hp)
        caster_id = HERO
        resources = ResourceState(pools=())
        casting_actors = ()
        if not pc:
            live.initiative = [
                a.model_copy(update={"entity_type": "NPC" if a.entity_id == HERO else "Character"})
                for a in live.initiative
            ]
            live.party_ids, live.encounter_ids = {FOE}, {HERO}
            live.xp_value_by_entity = {HERO: 0}
    canonical = BundledAssetLoader()
    grants = tuple(
        g.slug
        for g in canonical.get_class("cleric").granted_features
        if g.level <= 1 and g.ref_type == "feature"
    )
    actors = []
    for actor in live.initiative:
        if actor.entity_id == caster_id:
            actor = actor.model_copy(
                update={
                    "class_slug": "cleric",
                    "classes": {"cleric": 1},
                    "character_level": 1,
                    "granted_features": grants,
                    "wisdom": 16,
                    "spellcasting_ability": "wis",
                    "save_proficiencies": ["wis", "cha"],
                    "proficiency_bonus_override": None,
                    "carried_item_slugs": (),
                    "weapon_in_hands": None,
                    "weapon_grip": "none",
                }
            )
        if actor.entity_id == FOE:
            actor = actor.model_copy(
                update={
                    "hp_current": hp,
                    "hp_max": 40,
                    "temp_hp": temp,
                    "dexterity": 14,
                    "save_proficiencies": ["int", "wis"] if concentrating else ["dex"],
                    "character_level": 5 if concentrating else 1,
                    "granted_features": actor.granted_features if concentrating else (),
                    "proficiency_bonus_override": None,
                    "damage_resistances": ["radiant"] if defense == "resist" else [],
                    "damage_immunities": ["radiant"] if defense == "immune" else [],
                    "damage_vulnerabilities": ["radiant"] if defense == "vulnerable" else [],
                    "dodging": dodge,
                }
            )
        actors.append(actor)
    live.initiative = actors
    live.tracked_hp = {a.entity_id: a.hp_current for a in actors}
    live.tracked_temp_hp = {a.entity_id: a.temp_hp for a in actors}
    live.spell_slots_by_entity[caster_id] = {1: 0 if empty_slots else 2}
    live.spells_known_by_entity[caster_id] = list(CLERIC_KNOWN)
    live.custom_counters_by_entity[caster_id] = {}
    resources = ResourceState(
        pools=(
            *resources.pools,
            SlotResource(
                kind="spell_slot",
                owner_id=caster_id,
                pool_id="spell:1",
                current=0 if empty_slots else 2,
                maximum=2,
                level=1,
            ),
        )
    )
    casting = CastingState(
        actors=(
            *casting_actors,
            ActorCastingState(
                actor_id=caster_id,
                verbal_available=True,
                materials=(),
                sources=(
                    ClassSpellSource(
                        source_id="class:cleric",
                        class_slug="cleric",
                        ability="wis",
                        known_spells=CLERIC_KNOWN,
                        prepared_spells=CLERIC_KNOWN[3:],
                    ),
                ),
            ),
        )
    )
    spell_slugs = {slug for a in casting.actors for s in a.sources for slug in s.known_spells}
    feature_slugs = {g for a in actors for g in a.granted_features or ()}
    loader = MemoryAssetLoader(
        items=[synthetic_loader().get_item(WEAPON)],
        classes=[canonical.get_class(s) for s in ("cleric", "wizard")],
        subclasses=[canonical.get_subclass("evoker")],
        features=[canonical.get_feature(s) for s in sorted(feature_slugs)],
        spells=[canonical.get_spell(s) for s in sorted(spell_slugs)],
        conditions=[canonical.get_condition(s) for s in ("paralyzed", "unconscious", "prone")],
    )
    live.ruleset_loader = loader
    snapshot = capture_combat_snapshot(
        live,
        grid=GridScene(width=3, height=3),
        world_version=7,
        combat_id=request.state_snapshot.combat_state.combat_id,
        inventory_state=request.state_snapshot.inventory_state,
        resource_state=resources,
        casting_state=casting,
    )
    request = request.model_copy(
        update={
            "actor_id": caster_id,
            "state_snapshot": snapshot,
            "ruleset_binding": ruleset_binding(loader),
            "payload": CombatIntentPayload(
                intent_type="cast_spell",
                spell_id="sacred-flame",
                target_id=FOE,
                spell_source_id="class:cleric",
            ),
        }
    )
    return request, handle, live, loader


def parity(request, handle, live, loader, monkeypatch):
    original = deepcopy(request)
    asyncio.run(orch.submit_player_intent(handle, request.actor_id, request.payload))
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request, loader)
        assert result.status == "accepted", result.error
        assert execute(request, loader) == result
    after = apply_candidate(request.state_snapshot, result)
    assert after == capture_combat_snapshot(
        live,
        grid=GridScene(width=3, height=3),
        world_version=7,
        combat_id=after.combat_state.combat_id,
        inventory_state=after.inventory_state,
        resource_state=after.resource_state,
        casting_state=after.casting_state,
    )
    assert result.proposed_events == tuple(live.event_log)
    assert result.rng_transition.next_state == RNGState.capture(live.rng)
    assert request == original
    return result, after


@pytest.mark.parametrize("pc", [True, False])
@pytest.mark.parametrize(
    "seed,raw,natural,success", [(0, 7, 14, True), (2, 1, 3, False), (15, 4, 1, False)]
)
def test_complete_class_save_damage_cost_event_rng_parity(
    pc, seed, raw, natural, success, monkeypatch
):
    request, handle, live, loader = damage_spell_case(pc=pc, seed=seed)
    result, after = parity(request, handle, live, loader, monkeypatch)
    saves = [e for e in result.proposed_events if isinstance(e, SaveRolled)]
    assert len(saves) == 1
    assert (saves[0].dc, saves[0].modifier, saves[0].natural, saves[0].succeeded) == (
        13,
        4,
        natural,
        success,
    )
    assert after.character_states[-1].hp_current == 40 - (0 if success else raw)
    assert after.resource_state == request.state_snapshot.resource_state
    assert (
        after.combat_state.expended_resources
        == request.state_snapshot.combat_state.expended_resources
    )
    caster = next(a for a in after.character_states if a.entity_id == request.actor_id)
    assert not caster.action_available
    assert caster.granted_features == request.state_snapshot.character_states[0].granted_features
    assert [e.slot_level for e in result.proposed_events if isinstance(e, SpellCast)] == [None]
    expected = Random(seed)
    assert expected.randint(1, 8) == raw  # original Resolver rolls damage before the save
    assert expected.randint(1, 20) == natural
    assert result.rng_transition.next_state == RNGState.capture(expected)


@pytest.mark.parametrize(
    "defense,damage", [(None, 4), ("resist", 2), ("immune", 0), ("vulnerable", 8)]
)
def test_damage_defenses_and_temp_hp(defense, damage, monkeypatch):
    request, handle, live, loader = damage_spell_case(seed=15, defense=defense, temp=20)
    result, after = parity(request, handle, live, loader, monkeypatch)
    assert after.character_states[-1].hp_current == 40
    assert after.character_states[-1].temp_hp == 20 - damage
    assert sum(e.amount for e in result.proposed_events if isinstance(e, DamageApplied)) == damage


@pytest.mark.parametrize("pc", [True, False])
def test_lethal_damage_life_state(pc, monkeypatch):
    request, handle, live, loader = damage_spell_case(seed=15, pc=pc, hp=1)
    _, after = parity(request, handle, live, loader, monkeypatch)
    assert after.character_states[-1].hp_current == 0
    assert after.character_states[-1].is_alive is (not pc)
    assert (FOE in after.combat_state.dead_ids) is pc


@pytest.mark.parametrize("seed,concentration_success", [(0, None), (2, False), (15, True)])
@pytest.mark.parametrize("pc", [True, False])
def test_real_damage_composes_existing_concentration(pc, seed, concentration_success, monkeypatch):
    request, handle, live, loader = damage_spell_case(seed=seed, pc=pc, concentrating=True, temp=20)
    result, after = parity(request, handle, live, loader, monkeypatch)
    checks = [e for e in result.proposed_events if isinstance(e, ConcentrationCheck)]
    assert len(checks) == (0 if concentration_success is None else 1)
    if checks:
        assert checks[0].dc == 10
        assert checks[0].succeeded is concentration_success
    assert bool(after.effect_states) is (concentration_success is not False)
    if concentration_success is False:
        assert not after.combat_state.conditions_by_effect
        assert not after.combat_state.effect_lifecycles
        assert not after.combat_state.concentration_chain
        assert all(not a.conditions for a in after.character_states)


def test_cantrip_costs_no_slot_and_dodge_uses_shared_save(monkeypatch):
    request, handle, live, loader = damage_spell_case(seed=2, empty_slots=True, dodge=True)
    result, after = parity(request, handle, live, loader, monkeypatch)
    save = next(e for e in result.proposed_events if isinstance(e, SaveRolled))
    assert save.advantage == "advantage"
    assert after.resource_state == request.state_snapshot.resource_state


@pytest.mark.parametrize(
    "corruption", ["range", "target", "source", "verbal", "slot", "budget", "feature", "dead"]
)
def test_incomplete_invocations_refuse_before_rng(corruption, monkeypatch):
    request, _, _, loader = damage_spell_case()
    snapshot = request.state_snapshot
    if corruption in {"target", "source", "slot"}:
        field, value = {
            "target": ("target_id", "absent"),
            "source": ("spell_source_id", "absent"),
            "slot": ("slot_level", 1),
        }[corruption]
        request = request.model_copy(
            update={"payload": request.payload.model_copy(update={field: value})}
        )
    elif corruption == "range":
        snapshot = snapshot.model_copy(
            update={
                "scene_state": snapshot.scene_state.model_copy(
                    update={"grid": snapshot.scene_state.grid.model_copy(update={"width": 20})}
                ),
                "combat_state": snapshot.combat_state.model_copy(
                    update={"actor_zone": {HERO: "0,0", FOE: "13,0"}}
                ),
            }
        )
    elif corruption == "verbal":
        snapshot = snapshot.model_copy(
            update={
                "casting_state": CastingState(
                    actors=(
                        snapshot.casting_state.actors[0].model_copy(
                            update={"verbal_available": False}
                        ),
                    )
                )
            }
        )
    else:
        hero, foe = snapshot.character_states
        update = {
            "budget": {"action_available": False},
            "feature": {"granted_features": (*hero.granted_features, "unknown-spell-hook")},
            "dead": {"is_alive": False, "hp_current": 0},
        }[corruption]
        snapshot = snapshot.model_copy(
            update={"character_states": (hero.model_copy(update=update), foe)}
        )
    request = request.model_copy(update={"state_snapshot": snapshot})
    before = deepcopy(request)
    isolate(monkeypatch)
    monkeypatch.setattr(RNGState, "restore", lambda *a: pytest.fail("refusal restored RNG"))
    result = execute(request, loader)
    assert result.status in {"unsupported", "rejected"}
    assert result.state_delta is result.rng_transition is None
    assert result.proposed_events == ()
    assert request == before


def test_new_spell_reactions_refuse_before_rng(monkeypatch):
    request, loader, _, _ = reaction_case()
    request = request.model_copy(
        update={
            "payload": request.payload.model_copy(
                update={"spell_id": "sacred-flame", "slot_level": None}
            )
        }
    )
    isolate(monkeypatch)
    monkeypatch.setattr(
        RNGState, "restore", lambda *a: pytest.fail("reaction refusal restored RNG")
    )
    result = execute(request, loader)
    assert result.status == "unsupported"
    assert result.state_delta is result.rng_transition is None


@pytest.mark.parametrize("fault", [RuntimeError, asyncio.CancelledError])
def test_fault_after_resolver_has_no_partial_result(fault, monkeypatch):
    request, _, _, loader = damage_spell_case(seed=2, concentrating=True)
    before = deepcopy(request)
    original = evaluation_spells.resolve_activity
    seen = []

    def broken(*args, **kwargs):
        original(*args, **kwargs)
        seen.append(True)
        raise fault("after actual save/damage")

    isolate(monkeypatch)
    monkeypatch.setattr(evaluation_spells, "resolve_activity", broken)
    with pytest.raises(fault, match="after actual save/damage"):
        execute(request, loader)
    assert seen == [True]
    assert request == before


def test_concurrent_replay_and_atomic_version_fences(monkeypatch):
    request, _, _, loader = damage_spell_case(seed=2, concentrating=True)
    isolate(monkeypatch)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: execute(request, loader), range(4)))
    assert all(r == results[0] for r in results)
    result = results[0]
    assert result.status == "accepted", result.error
    after = apply_candidate(request.state_snapshot, result)
    with pytest.raises(AssertionError):
        apply_candidate(request.state_snapshot, result, world_version=8)
    with pytest.raises(AssertionError):
        apply_candidate(after, result)


def test_unlearned_cantrip_refuses_even_with_available_slots(monkeypatch):
    request, _, _, loader = damage_spell_case()
    snapshot = request.state_snapshot
    entry = snapshot.casting_state.actors[0]
    source = entry.sources[0].model_copy(update={"known_spells": CLERIC_KNOWN[1:]})
    hero, foe = snapshot.character_states
    snapshot = snapshot.model_copy(
        update={
            "casting_state": CastingState(
                actors=(entry.model_copy(update={"sources": (source,)}),)
            ),
            "character_states": (
                hero.model_copy(update={"spells_known": list(CLERIC_KNOWN[1:])}),
                foe,
            ),
        }
    )
    request = request.model_copy(update={"state_snapshot": snapshot})
    isolate(monkeypatch)
    monkeypatch.setattr(
        RNGState, "restore", lambda *a: pytest.fail("unlearned Cantrip restored RNG")
    )
    result = execute(request, loader)
    assert result.status == "rejected"
    assert result.error.code == "spell.source"
    assert result.state_delta is result.rng_transition is None


def test_held_caster_uses_shared_action_denial_before_rng(monkeypatch):
    request, _, _, loader = damage_spell_case(concentrating=True)
    snapshot = request.state_snapshot.model_copy(
        update={
            "combat_state": request.state_snapshot.combat_state.model_copy(
                update={"current_turn_index": 1}
            )
        }
    )
    request = request.model_copy(update={"actor_id": HERO, "state_snapshot": snapshot})
    isolate(monkeypatch)
    monkeypatch.setattr(
        RNGState, "restore", lambda *a: pytest.fail("paralyzed caster restored RNG")
    )
    result = execute(request, loader)
    assert result.status == "rejected"
    assert result.error.code == "actor_incapacitated"
    assert result.state_delta is result.rng_transition is None


def test_busy_somatic_hand_refuses_before_rng(monkeypatch):
    request, _, _, loader = damage_spell_case()
    snapshot = request.state_snapshot
    hero, foe = snapshot.character_states
    snapshot = snapshot.model_copy(
        update={
            "character_states": (
                hero.model_copy(update={"other_hand_occupied": True}),
                foe,
            )
        }
    )
    request = request.model_copy(update={"state_snapshot": snapshot})
    isolate(monkeypatch)
    monkeypatch.setattr(RNGState, "restore", lambda *a: pytest.fail("busy hand restored RNG"))
    result = execute(request, loader)
    assert result.status == "unsupported"
    assert "component" in result.error.reason


def test_unknown_target_feature_repertoire_refuses_before_rng(monkeypatch):
    request, _, _, loader = damage_spell_case()
    snapshot = request.state_snapshot
    hero, foe = snapshot.character_states
    snapshot = snapshot.model_copy(
        update={"character_states": (hero, foe.model_copy(update={"granted_features": None}))}
    )
    request = request.model_copy(update={"state_snapshot": snapshot})
    isolate(monkeypatch)
    monkeypatch.setattr(
        RNGState, "restore", lambda *a: pytest.fail("unknown repertoire restored RNG")
    )
    result = execute(request, loader)
    assert result.status == "unsupported"
    assert result.state_delta is result.rng_transition is None
