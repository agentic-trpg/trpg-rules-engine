"""Actual attacks against an authorized Hold Person caster, including mixed packets."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from random import Random

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd5e_srd_data.schema.common import DamagePart

from dnd5e_engine import evaluation_concentration as concentration
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.evaluation_casting import ActorCastingState, CastingState, ClassSpellSource
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, RuleEvaluationRequest
from dnd5e_engine.evaluation_resources import FeatureResource, ResourceState, SlotResource
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_engine.evaluation_snapshot import capture_combat_snapshot
from dnd5e_engine.events import ConcentrationCheck, DamageApplied
from dnd5e_engine.specs import GridScene
from tests.evaluation_support import FOE, HERO, WEAPON, synthetic_loader
from tests.test_stateless_attack import execute
from tests.test_stateless_concentration import apply_candidate, concentration_case
from tests.test_stateless_item_closure_core import isolate
from tests.test_stateless_reactions import KNOWN

ATTACKER = "char:attacker"


def attack_concentration_case(
    *, seed=0, mixed=False, hp=40, temp=0, immune=False, resist=False, pc=False
):
    candidate, handle, live, _ = concentration_case(npc=True, seed=seed, hp=hp, temp=temp)
    canonical = BundledAssetLoader()
    grants = {
        g.slug
        for definition in (canonical.get_class("wizard"), canonical.get_subclass("evoker"))
        for g in definition.granted_features
        if g.ref_type == "feature" and g.level <= 5
    }
    weapon = (
        synthetic_loader()
        .get_weapon(WEAPON)
        .model_copy(
            update={
                "damage_parts": [DamagePart(dice="12", damage_type="slashing")]
                + ([DamagePart(dice="12", damage_type="fire")] if mixed else []),
            }
        )
    )
    known = tuple("hold-person" if slug == "mirror-image" else slug for slug in KNOWN)
    loader = MemoryAssetLoader(
        items=[weapon],
        spells=[canonical.get_spell(s) for s in known],
        classes=[canonical.get_class("wizard")],
        subclasses=[canonical.get_subclass("evoker")],
        features=[canonical.get_feature(s) for s in sorted(grants)],
        conditions=[canonical.get_condition("paralyzed")],
    )
    held, caster = live.initiative
    attacker = held.model_copy(
        update={
            "entity_id": ATTACKER,
            "name": "Attacker",
            "conditions": [],
            "movement_remaining": 30,
            "initiative": 25,
            "strength": 10,
        }
    )
    caster = caster.model_copy(
        update={
            "class_slug": "wizard",
            "classes": {"wizard": 5},
            "character_level": 5,
            "subclass_slug": "evoker",
            "granted_features": tuple(sorted(grants)),
            "intelligence": 18,
            "spellcasting_ability": "int",
            "proficiency_bonus_override": None,
            "save_proficiencies": ["int", "wis"],
            "ac": 1,
            "damage_resistances": ["slashing"] if resist else [],
            "damage_immunities": ["slashing", "fire"] if immune else [],
        }
    )
    live.initiative = [attacker, held, caster]
    live.current_actor_id = ATTACKER
    live.party_ids.add(ATTACKER)
    if pc:
        live.initiative = [
            actor.model_copy(
                update={"entity_type": "Character" if actor.entity_id == FOE else "NPC"}
            )
            for actor in live.initiative
        ]
        live.party_ids = {FOE}
        live.encounter_ids = {ATTACKER, HERO}
        live.xp_value_by_entity = {ATTACKER: 0, HERO: 0}
    live.actor_zone.update({ATTACKER: "0,0", HERO: "0,1"})
    live.movement_ledgers[ATTACKER] = deepcopy(live.movement_ledgers[HERO])
    live.tracked_hp[ATTACKER] = attacker.hp_current
    live.ruleset_loader = loader
    live.spell_slots_by_entity = {ATTACKER: {}, HERO: {}, FOE: {1: 4, 2: 2, 3: 2}}
    live.pact_slots_by_entity = {a.entity_id: {} for a in live.initiative}
    live.custom_counters_by_entity = {
        ATTACKER: {},
        HERO: {},
        FOE: {"feature_use:arcane-recovery": {"spent": 0}},
    }
    live.spells_known_by_entity = {ATTACKER: [], HERO: [], FOE: list(known)}
    resources = ResourceState(
        pools=(
            *(
                SlotResource(
                    kind="spell_slot",
                    owner_id=FOE,
                    pool_id=f"spell:{level}",
                    level=level,
                    current=current,
                    maximum=maximum,
                )
                for level, current, maximum in ((1, 4, 4), (2, 2, 3), (3, 2, 2))
            ),
            FeatureResource(
                kind="feature_uses",
                owner_id=FOE,
                pool_id="arcane-recovery",
                feature_slug="arcane-recovery",
                current=1,
                maximum=1,
            ),
        )
    )
    casting = CastingState(
        actors=(
            ActorCastingState(
                actor_id=FOE,
                verbal_available=True,
                sources=(
                    ClassSpellSource(
                        source_id="class:wizard",
                        class_slug="wizard",
                        ability="int",
                        known_spells=known,
                        prepared_spells=known[-9:],
                    ),
                ),
            ),
        )
    )
    snapshot = capture_combat_snapshot(
        live,
        grid=GridScene(width=3, height=3),
        world_version=7,
        combat_id=candidate.state_snapshot.combat_state.combat_id,
        inventory_state=candidate.state_snapshot.inventory_state,
        resource_state=resources,
        casting_state=casting,
    )
    request = RuleEvaluationRequest(
        schema_version=candidate.schema_version,
        session_id=candidate.session_id,
        command_id="attack:concentration",
        operation_kind="combat.intent",
        actor_id=ATTACKER,
        payload=CombatIntentPayload(intent_type="attack", weapon_id=WEAPON, target_id=FOE),
        state_snapshot=snapshot,
        ruleset_binding=ruleset_binding(loader),
        rng_context=candidate.rng_context,
    )
    return request, handle, live, loader


def test_legacy_one_concentration_check_per_completed_mixed_damage_instance():
    request, handle, live, _ = attack_concentration_case(mixed=True, temp=200)
    asyncio.run(orch.submit_player_intent(handle, ATTACKER, request.payload))
    checks = [e for e in live.event_log if isinstance(e, ConcentrationCheck)]
    assert len(checks) == 1
    assert checks[0].dc == 12
    assert checks[0].natural == 14


@pytest.mark.parametrize("seed,success", [(0, True), (5, False), (31, None)])
@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize("pc", [False, True])
def test_actual_attack_full_state_event_rng_parity(seed, success, mixed, pc, monkeypatch):
    request, handle, live, loader = attack_concentration_case(seed=seed, mixed=mixed, pc=pc)
    original = deepcopy(request)
    asyncio.run(orch.submit_player_intent(handle, ATTACKER, request.payload))
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request, loader)
        assert result.status == "accepted", result.error
        assert execute(request, loader) == result
    after = apply_candidate(request.state_snapshot, result)
    expected = capture_combat_snapshot(
        live,
        grid=GridScene(width=3, height=3),
        world_version=7,
        combat_id=after.combat_state.combat_id,
        inventory_state=after.inventory_state,
        resource_state=after.resource_state,
        casting_state=after.casting_state,
    )
    assert after == expected
    assert result.proposed_events == tuple(live.event_log)
    assert result.rng_transition.next_state == RNGState.capture(live.rng)
    checks = [e for e in result.proposed_events if isinstance(e, ConcentrationCheck)]
    assert len(checks) == (0 if success is None else 1)
    if success is not None:
        # With one 12-damage packet DC=10: seed 5's 9+2 save succeeds.
        assert checks[0].succeeded is (success if mixed else True)
        # The NPC weapon path carries its explicit +3 damage bonus.
        damage = (24 if mixed else 12) + (3 if pc else 0)
        assert checks[0].dc == (damage // 2 if mixed else 10)
        assert after.character_states[-1].hp_current == 40 - damage
    assert after.resource_state == original.state_snapshot.resource_state
    assert request == original


@pytest.mark.parametrize(
    "immune,resist,total,dc",
    [(False, False, 24, 12), (False, True, 18, 10), (True, False, 0, None)],
)
def test_post_defense_aggregate_includes_temporary_hp(immune, resist, total, dc, monkeypatch):
    request, _, _, loader = attack_concentration_case(
        mixed=True, temp=200, immune=immune, resist=resist
    )
    isolate(monkeypatch)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    after = apply_candidate(request.state_snapshot, result)
    assert (after.character_states[-1].hp_current, after.character_states[-1].temp_hp) == (
        40,
        200 - total,
    )
    checks = [e for e in result.proposed_events if isinstance(e, ConcentrationCheck)]
    assert [e.dc for e in checks] == ([] if dc is None else [dc])
    assert sum(e.amount for e in result.proposed_events if isinstance(e, DamageApplied)) == total
    rng = Random(0)
    rng.randint(1, 20)
    if dc is not None:
        rng.randint(1, 20)
    assert result.rng_transition.next_state == RNGState.capture(rng)


@pytest.mark.parametrize("pc", [False, True])
def test_atomic_cleanup_on_lethal_attack_and_version_fence(pc, monkeypatch):
    request, handle, live, loader = attack_concentration_case(mixed=True, hp=1, pc=pc)
    asyncio.run(orch.submit_player_intent(handle, ATTACKER, request.payload))
    isolate(monkeypatch)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    after = apply_candidate(request.state_snapshot, result)
    assert (FOE in after.combat_state.dead_ids) is (not pc)
    assert after.character_states[-1].hp_current == 0
    assert after.character_states[-1].is_alive is pc
    assert not after.effect_states
    assert not after.combat_state.concentration_chain
    assert not after.combat_state.conditions_by_effect
    assert not after.combat_state.effect_lifecycles
    assert not next(a for a in after.character_states if a.entity_id == HERO).conditions
    assert result.proposed_events == tuple(live.event_log)
    assert result.rng_transition.next_state == RNGState.capture(live.rng)
    with pytest.raises(AssertionError):
        apply_candidate(request.state_snapshot, result, world_version=8)
    with pytest.raises(AssertionError):
        apply_candidate(after, result)


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_fault_after_real_concentration_roll_returns_no_partial_candidate(failure, monkeypatch):
    request, _, _, loader = attack_concentration_case(mixed=True)
    before = deepcopy(request)
    original = concentration.resolve_snapshot_save
    seen = []

    def broken(*args, **kwargs):
        seen.append(original(*args, **kwargs))
        raise failure("after concentration roll")

    isolate(monkeypatch)
    monkeypatch.setattr(concentration, "resolve_snapshot_save", broken)
    with pytest.raises(failure, match="after concentration roll"):
        execute(request, loader)
    assert len(seen) == 1
    assert request == before


@pytest.mark.parametrize(
    "corruption", ["source", "lineage", "clock", "feature", "dead_caster", "mastery", "style"]
)
def test_incomplete_mechanical_closure_fails_before_rng(corruption, monkeypatch):
    request, _, _, loader = attack_concentration_case()
    snapshot = request.state_snapshot
    if corruption == "source":
        effect = snapshot.effect_states[0]
        snapshot = snapshot.model_copy(
            update={
                "effect_states": (
                    effect.model_copy(
                        update={
                            "lifecycle": effect.lifecycle.model_copy(
                                update={"source_id": "missing"}
                            )
                        }
                    ),
                )
            }
        )
    elif corruption in {"lineage", "clock"}:
        field = "conditions_by_effect" if corruption == "lineage" else "effect_lifecycles"
        snapshot = snapshot.model_copy(
            update={"combat_state": snapshot.combat_state.model_copy(update={field: ()})}
        )
    elif corruption in {"mastery", "style"}:
        actors = snapshot.character_states
        field = "weapon_mastery_slugs" if corruption == "mastery" else "fighting_styles"
        snapshot = snapshot.model_copy(
            update={
                "character_states": (
                    actors[0].model_copy(
                        update={field: (WEAPON if field == "weapon_mastery_slugs" else "archery",)}
                    ),
                    *actors[1:],
                )
            }
        )
    else:
        actors = snapshot.character_states
        value = {"granted_features": (*actors[-1].granted_features, "unknown-save-hook")}
        if corruption == "dead_caster":
            value = {"is_alive": False, "hp_current": 0}
        snapshot = snapshot.model_copy(
            update={"character_states": (*actors[:-1], actors[-1].model_copy(update=value))}
        )
    request = request.model_copy(update={"state_snapshot": snapshot})
    isolate(monkeypatch)
    monkeypatch.setattr(
        RNGState, "restore", lambda *a: pytest.fail("unsupported path restored RNG")
    )
    result = execute(request, loader)
    assert result.status == "unsupported"
    assert result.state_delta is result.rng_transition is None
    assert result.proposed_events == ()


def test_multitarget_lineage_cleanup_and_concurrent_replay(monkeypatch):
    from dnd5e_engine.events import EffectApplied
    from dnd5e_engine.lib_loader import scoped_lib_loader

    request, handle, live, loader = attack_concentration_case(seed=5, mixed=True)
    held = live.initiative[1].model_copy(update={"entity_id": "char:held-two", "conditions": []})
    live.initiative.append(held)
    live.party_ids.add(held.entity_id)
    live.actor_zone[held.entity_id] = "2,2"
    live.movement_ledgers[held.entity_id] = deepcopy(live.movement_ledgers[HERO])
    live.tracked_hp[held.entity_id] = held.hp_current
    for values, empty in (
        (live.spell_slots_by_entity, {}),
        (live.pact_slots_by_entity, {}),
        (live.spells_known_by_entity, []),
        (live.custom_counters_by_entity, {}),
    ):
        values[held.entity_id] = empty
    effect = live.active_effects[HERO][0].model_copy(update={"target_id": held.entity_id})
    with scoped_lib_loader(loader):
        orch._emit(live, EffectApplied(effect=effect))
    live.concentration_chain[FOE].append((held.entity_id, effect.id, effect.origin))
    live.event_log.clear()
    snapshot = capture_combat_snapshot(
        live,
        grid=GridScene(width=3, height=3),
        world_version=7,
        combat_id=request.state_snapshot.combat_state.combat_id,
        inventory_state=request.state_snapshot.inventory_state,
        resource_state=request.state_snapshot.resource_state,
        casting_state=request.state_snapshot.casting_state,
    )
    request = request.model_copy(update={"state_snapshot": snapshot})
    before = deepcopy(request)
    asyncio.run(orch.submit_player_intent(handle, ATTACKER, request.payload))
    with monkeypatch.context() as scoped:
        isolate(scoped)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: execute(request, loader), range(4)))
    assert all(result == results[0] for result in results)
    result = results[0]
    assert result.status == "accepted", result.error
    after = apply_candidate(snapshot, result)
    assert not after.effect_states
    assert all(not a.conditions for a in after.character_states)
    assert result.proposed_events == tuple(live.event_log)
    assert result.rng_transition.next_state == RNGState.capture(live.rng)
    assert request == before


def test_attack_round_wrap_preserves_concentration_clock_and_legacy_order(monkeypatch):
    request, handle, live, loader = attack_concentration_case(mixed=True)
    attacker, held, caster = live.initiative
    live.initiative = [held, caster, attacker]
    live.current_turn_index = 2
    snapshot = capture_combat_snapshot(
        live,
        grid=GridScene(width=3, height=3),
        world_version=7,
        combat_id=request.state_snapshot.combat_state.combat_id,
        inventory_state=request.state_snapshot.inventory_state,
        resource_state=request.state_snapshot.resource_state,
        casting_state=request.state_snapshot.casting_state,
    )
    request = request.model_copy(update={"state_snapshot": snapshot})
    asyncio.run(orch.submit_player_intent(handle, ATTACKER, request.payload))
    isolate(monkeypatch)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    after = apply_candidate(snapshot, result)
    assert after.combat_state.round_number == snapshot.combat_state.round_number + 1
    assert (
        after.combat_state.concentration_rounds_remaining
        == snapshot.combat_state.concentration_rounds_remaining
    )
    assert after == capture_combat_snapshot(
        live,
        grid=GridScene(width=3, height=3),
        world_version=7,
        combat_id=snapshot.combat_state.combat_id,
        inventory_state=after.inventory_state,
        resource_state=after.resource_state,
        casting_state=after.casting_state,
    )
    assert result.proposed_events == tuple(live.event_log)
    assert result.rng_transition.next_state == RNGState.capture(live.rng)


def test_self_damage_source_turn_hooks_refuse_before_rng(monkeypatch):
    request, _, _, loader = attack_concentration_case()
    snapshot = request.state_snapshot.model_copy(
        update={
            "combat_state": request.state_snapshot.combat_state.model_copy(
                update={"current_turn_index": 2}
            )
        }
    )
    request = request.model_copy(update={"actor_id": FOE, "state_snapshot": snapshot})
    isolate(monkeypatch)
    monkeypatch.setattr(RNGState, "restore", lambda *a: pytest.fail("refusal restored RNG"))
    result = execute(request, loader)
    assert result.status == "unsupported"
    assert "source turn-end" in result.error.reason
    assert result.state_delta is result.rng_transition is None


def test_immune_condition_does_not_run_incapacitation_cleanup(monkeypatch):
    request, handle, live, loader = attack_concentration_case(mixed=True, hp=1, pc=True)
    live.initiative[-1] = live.initiative[-1].model_copy(
        update={"condition_immunities": ["unconscious"]}
    )
    snapshot = capture_combat_snapshot(
        live,
        grid=GridScene(width=3, height=3),
        world_version=7,
        combat_id=request.state_snapshot.combat_state.combat_id,
        inventory_state=request.state_snapshot.inventory_state,
        resource_state=request.state_snapshot.resource_state,
        casting_state=request.state_snapshot.casting_state,
    )
    request = request.model_copy(update={"state_snapshot": snapshot})
    asyncio.run(orch.submit_player_intent(handle, ATTACKER, request.payload))
    isolate(monkeypatch)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    after = apply_candidate(snapshot, result)
    assert after.effect_states == snapshot.effect_states
    assert after.combat_state.concentration_chain == snapshot.combat_state.concentration_chain
    assert not after.character_states[-1].conditions
    assert result.proposed_events == tuple(live.event_log)
    assert result.rng_transition.next_state == RNGState.capture(live.rng)
