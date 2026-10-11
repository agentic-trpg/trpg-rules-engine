"""Predeclared real Wizard Shield: original roll, persistent AC, expiry and parity."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from random import Random

import pytest
from pydantic import ValidationError

from dnd5e_engine import evaluation_shield, live_reactions
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, RuleEvaluationResult
from dnd5e_engine.evaluation_resources import ResourceState
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import RulesetBindingError
from dnd5e_engine.evaluation_shield_delta import ShieldStateTransition
from dnd5e_engine.evaluation_snapshot import capture_combat_snapshot, capture_evaluation_snapshot
from dnd5e_engine.evaluation_state import CombatSnapshot
from dnd5e_engine.intents import PlayerIntent
from dnd5e_engine.lib_loader import scoped_lib_loader
from dnd5e_engine.specs import GridScene
from tests.evaluation_support import FOE, HERO, WEAPON
from tests.test_stateless_attack import execute
from tests.test_stateless_attack_concentration import ATTACKER
from tests.test_stateless_hold_cast import cast_case
from tests.test_stateless_hold_turn import isolated
from tests.test_stateless_spell_feature import apply_healing


def shield_case(*, seed=7, pc=False, empty=False, spent=False, hp=40, start=0):
    request, handle, live, loader = cast_case(seed=seed, pc=pc)
    live.initiative = [
        a.model_copy(update={"attack_bonus": 2}) if a.entity_id == ATTACKER else a
        for a in live.initiative
    ]
    live.initiative = [
        a.model_copy(update={"reaction_available": not spent, "hp_current": hp})
        if a.entity_id == FOE
        else a
        for a in live.initiative
    ]
    live.tracked_hp[FOE] = hp
    live.current_turn_index = start
    live.current_actor_id = live.initiative[start].entity_id
    live.turn_serial = start + 1
    if empty:
        live.spell_slots_by_entity[FOE][1] = 0
    resources = ResourceState(
        pools=tuple(
            p.model_copy(update={"current": 0}) if empty and getattr(p, "level", None) == 1 else p
            for p in request.state_snapshot.resource_state.pools
        )
    )
    with scoped_lib_loader(loader):
        live_reactions.register_pending_reaction(
            live, FOE, PlayerIntent(intent_type="ready", spell_id="shield", slot_level=1)
        )
    snapshot = capture_combat_snapshot(
        live,
        grid=GridScene(width=3, height=3),
        world_version=7,
        combat_id=request.state_snapshot.combat_state.combat_id,
        inventory_state=request.state_snapshot.inventory_state,
        casting_state=request.state_snapshot.casting_state,
        resource_state=resources,
        reaction_sources={FOE: "class:wizard"},
    )
    return (
        request.model_copy(
            update={
                "actor_id": live.current_actor_id,
                "state_snapshot": snapshot,
                "payload": CombatIntentPayload(
                    intent_type="attack", weapon_id=WEAPON, target_id=FOE
                ),
            }
        ),
        handle,
        live,
        loader,
    )


def apply_shield(snapshot, result, *, world_version=None):
    """Guard all expected components before constructing an unpublished replacement."""
    assert result.state_delta.expected_world_version == (
        snapshot.world_version if world_version is None else world_version
    )
    assert result.read_set[0].version == snapshot.world_version
    changes = [o for o in result.state_delta.operations if isinstance(o, ShieldStateTransition)]
    for op in changes:
        assert op.combat_id == snapshot.combat_state.combat_id
        assert op.actor_ids == snapshot.combat_state.initiative_ids
        old = op.expected
        assert snapshot.effect_states == ((old.effect,) if old else ())
        assert snapshot.combat_state.conditions_by_effect == ((old.lineage,) if old else ())
        assert snapshot.combat_state.active_reaction_responses == (
            [old.response.response()] if old and old.response else []
        )
        assert snapshot.combat_state.reaction_effects_pending_expiry == (
            {op.owner_id: [old.expiry_identity]} if old else {}
        )
        if old:
            assert old.owner_dead == (op.owner_id in snapshot.combat_state.dead_ids)
    filtered = result.model_copy(
        update={
            "state_delta": result.state_delta.model_copy(
                update={
                    "operations": tuple(
                        o
                        for o in result.state_delta.operations
                        if not isinstance(o, ShieldStateTransition)
                    )
                }
            )
        }
    )
    plain = snapshot.model_copy(
        update={
            "effect_states": (),
            "combat_state": snapshot.combat_state.model_copy(
                update={
                    "conditions_by_effect": (),
                    "active_reaction_responses": [],
                    "reaction_effects_pending_expiry": {},
                }
            ),
        }
    )
    after = apply_healing(plain, filtered)
    after = after.model_copy(
        update={
            "effect_states": snapshot.effect_states,
            "combat_state": after.combat_state.model_copy(
                update={
                    "conditions_by_effect": snapshot.combat_state.conditions_by_effect,
                    "active_reaction_responses": snapshot.combat_state.active_reaction_responses,
                    "reaction_effects_pending_expiry": snapshot.combat_state.reaction_effects_pending_expiry,
                }
            ),
        }
    )
    for op in changes:
        new = op.value
        if new:
            assert new.owner_dead == (op.owner_id in after.combat_state.dead_ids)
        after = after.model_copy(
            update={
                "effect_states": (new.effect,) if new else (),
                "combat_state": after.combat_state.model_copy(
                    update={
                        "conditions_by_effect": (new.lineage,) if new else (),
                        "active_reaction_responses": [new.response.response()]
                        if new and new.response
                        else [],
                        "reaction_effects_pending_expiry": {op.owner_id: [new.expiry_identity]}
                        if new
                        else {},
                    }
                ),
            }
        )
    return CombatSnapshot.model_validate_json(after.model_dump_json())


def step(request, handle, live, loader, monkeypatch, intent=None):
    if intent is not None:
        state = request.state_snapshot.combat_state
        request = request.model_copy(
            update={"payload": intent, "actor_id": state.initiative_ids[state.current_turn_index]}
        )
    original = deepcopy(request)
    offset = len(live.event_log)
    asyncio.run(orch.submit_player_intent(handle, request.actor_id, request.payload))
    with monkeypatch.context() as scoped:
        isolated(scoped)
        for name in (
            "fire_reaction",
            "attach_reaction_hooks",
            "record_reaction_effects",
            "observe_reaction_lifecycle",
        ):
            scoped.setattr(
                live_reactions, name, lambda *a, **kw: pytest.fail("Legacy reaction runtime")
            )
        scoped.setattr(
            orch,
            "_hook_expire_reaction_effects",
            lambda *a, **kw: pytest.fail("Legacy expiry hook"),
        )
        result = execute(request, loader)
        assert execute(request, loader) == result
    assert result.status == "accepted", result.error
    after = apply_shield(request.state_snapshot, result)
    assert after == capture_evaluation_snapshot(live, after, GridScene(width=3, height=3))
    assert result.proposed_events == tuple(live.event_log[offset:])
    assert result.rng_transition.next_state == RNGState.capture(live.rng)
    assert RuleEvaluationResult.model_validate_json(result.model_dump_json()) == result
    assert request == original
    return request.model_copy(
        update={
            "state_snapshot": after,
            "rng_context": request.rng_context.model_copy(
                update={"state": result.rng_transition.next_state}
            ),
        }
    ), result


@pytest.mark.parametrize("pc", [False, True])
@pytest.mark.parametrize("seed", [0, 1, 5, 7, 9, 31])
def test_real_shield_trigger_original_roll_cost_and_full_legacy_parity(pc, seed, monkeypatch):
    request, handle, live, loader = shield_case(seed=seed, pc=pc)
    after, result = step(request, handle, live, loader, monkeypatch)
    rng = Random(seed)
    natural = rng.randint(1, 20)
    roll = next(e for e in result.proposed_events if e.type == "attack_rolled")
    assert roll.natural == natural
    assert roll.roll_total == natural + 2
    caster = next(a for a in request.state_snapshot.character_states if a.entity_id == FOE)
    fires = natural != 1 and (natural == 20 or natural + 2 >= caster.ac)
    hit = natural != 1 and (natural == 20 or natural + 2 >= caster.ac + (5 if fires else 0))
    assert roll.is_hit == hit
    assert roll.is_crit == (natural == 20)
    reactions = [e for e in result.proposed_events if e.type == "reaction_triggered"]
    assert bool(reactions) == fires
    assert result.rng_transition.next_state == RNGState.capture(rng)
    owner = next(a for a in after.state_snapshot.character_states if a.entity_id == FOE)
    assert owner.spell_slots[1] == 4 - int(fires)
    assert owner.reaction_available is (not fires)
    assert owner.hp_current == 40 - (12 if hit else 0)
    if fires:
        evidence = reactions[0].provisional_attack
        assert (evidence.natural, evidence.roll_total, evidence.effective_ac) == (
            natural,
            natural + 2,
            caster.ac,
        )
        assert evidence.provisional_hit is True
        kinds = [e.type for e in result.proposed_events]
        assert (
            kinds.index("reaction_triggered")
            < kinds.index("spell_cast")
            < kinds.index("effect_applied")
            < kinds.index("attack_rolled")
        )
        assert after.state_snapshot.effect_states[0].changes[0].value == "5"
    else:
        assert (
            after.state_snapshot.combat_state.pending_reactions
            == request.state_snapshot.combat_state.pending_reactions
        )
        assert not after.state_snapshot.effect_states


@pytest.mark.parametrize("pc", [False, True])
def test_persisted_plus_five_next_attack_expiry_and_later_attack(pc, monkeypatch):
    request, handle, live, loader = shield_case(seed=7, pc=pc)
    request, first = step(request, handle, live, loader, monkeypatch)
    assert next(e for e in first.proposed_events if e.type == "attack_rolled").is_hit is False
    assert request.state_snapshot.effect_states
    request, second = step(
        request,
        handle,
        live,
        loader,
        monkeypatch,
        CombatIntentPayload(intent_type="attack", weapon_id=WEAPON, target_id=FOE),
    )
    attack = next(e for e in second.proposed_events if e.type == "attack_rolled")
    assert (attack.natural, attack.roll_total, attack.is_hit) == (5, 10, False)
    assert not any(e.type == "reaction_triggered" for e in second.proposed_events)
    assert not request.state_snapshot.effect_states
    assert not request.state_snapshot.combat_state.active_reaction_responses
    assert not request.state_snapshot.combat_state.reaction_effects_pending_expiry
    assert not request.state_snapshot.combat_state.conditions_by_effect
    owner = next(a for a in request.state_snapshot.character_states if a.entity_id == FOE)
    assert owner.spell_slots[1] == 3
    assert owner.reaction_available is True
    events = list(second.proposed_events)
    start = next(
        i for i, e in enumerate(events) if e.type == "turn_phase" and e.phase == "turn_start"
    )
    expiry = next(i for i, e in enumerate(events) if e.type == "effect_expired")
    assert start < expiry
    request, _ = step(
        request, handle, live, loader, monkeypatch, CombatIntentPayload(intent_type="pass")
    )
    request, last = step(
        request,
        handle,
        live,
        loader,
        monkeypatch,
        CombatIntentPayload(intent_type="attack", weapon_id=WEAPON, target_id=FOE),
    )
    assert next(e for e in last.proposed_events if e.type == "attack_rolled").is_hit is True
    assert not any(e.type == "reaction_triggered" for e in last.proposed_events)


@pytest.mark.parametrize("mode", ["spent", "empty"])
def test_legal_unavailable_reaction_retained_without_payment(mode, monkeypatch):
    request, handle, live, loader = shield_case(seed=7, **{mode: True})
    after, result = step(request, handle, live, loader, monkeypatch)
    assert not any(e.type == "reaction_triggered" for e in result.proposed_events)
    assert after.state_snapshot.resource_state == request.state_snapshot.resource_state
    assert (
        after.state_snapshot.combat_state.pending_reactions
        == request.state_snapshot.combat_state.pending_reactions
    )
    assert next(e for e in result.proposed_events if e.type == "attack_rolled").is_hit is True


@pytest.mark.parametrize("pc", [False, True])
def test_pass_restores_active_graph_and_expires_at_owner_start(pc, monkeypatch):
    request, handle, live, loader = shield_case(pc=pc)
    request, _ = step(request, handle, live, loader, monkeypatch)
    before = request.rng_context.state
    request, result = step(
        request, handle, live, loader, monkeypatch, CombatIntentPayload(intent_type="pass")
    )
    assert result.rng_transition.next_state == before
    assert not request.state_snapshot.effect_states
    assert not request.state_snapshot.combat_state.active_reaction_responses
    assert not request.state_snapshot.combat_state.reaction_effects_pending_expiry
    assert not request.state_snapshot.combat_state.conditions_by_effect
    assert sum(e.type == "effect_expired" for e in result.proposed_events) == 1
    owner = next(a for a in request.state_snapshot.character_states if a.entity_id == FOE)
    assert owner.reaction_available is True
    assert owner.spell_slots[1] == 3


@pytest.mark.parametrize("intent", ["attack", "pass"])
@pytest.mark.parametrize("case", ["expiry", "response", "lineage", "ac", "duration"])
def test_incomplete_persisted_graph_refuses_before_rng(case, intent, monkeypatch):
    request, handle, live, loader = shield_case()
    request, _ = step(request, handle, live, loader, monkeypatch)
    snap = request.state_snapshot
    state = snap.combat_state
    effect = snap.effect_states[0]
    if case == "expiry":
        state = state.model_copy(update={"reaction_effects_pending_expiry": {}})
    if case == "response":
        state = state.model_copy(update={"active_reaction_responses": []})
    if case == "lineage":
        state = state.model_copy(update={"conditions_by_effect": ()})
    if case == "ac":
        effect = effect.model_copy(
            update={"changes": [effect.changes[0].model_copy(update={"value": "6"})]}
        )
    if case == "duration":
        effect = effect.model_copy(
            update={"duration": effect.duration.model_copy(update={"rounds": 2})}
        )
    snap = snap.model_copy(update={"combat_state": state, "effect_states": (effect,)})
    request = request.model_copy(
        update={
            "actor_id": HERO,
            "state_snapshot": snap,
            "payload": CombatIntentPayload(intent_type="pass")
            if intent == "pass"
            else CombatIntentPayload(intent_type="attack", weapon_id=WEAPON, target_id=FOE),
        }
    )
    original = deepcopy(request)
    monkeypatch.setattr(RNGState, "restore", lambda *_: pytest.fail("RNG before graph refusal"))
    result = execute(request, loader)
    assert result.status == "unsupported"
    assert result.state_delta is None
    assert result.rng_transition is None
    assert result.proposed_events == ()
    assert request == original


def test_creation_and_expiry_in_same_attack_is_one_atomic_result(monkeypatch):
    request, handle, live, loader = shield_case(seed=7, start=1)
    after, result = step(request, handle, live, loader, monkeypatch)
    assert any(e.type == "effect_applied" for e in result.proposed_events)
    assert any(e.type == "effect_expired" for e in result.proposed_events)
    assert not after.state_snapshot.effect_states
    assert (
        next(a for a in after.state_snapshot.character_states if a.entity_id == FOE).spell_slots[1]
        == 3
    )


@pytest.mark.parametrize("pc", [False, True])
def test_shield_cannot_stop_crit_damage_and_death_or_dying(pc, monkeypatch):
    request, handle, live, loader = shield_case(seed=5, pc=pc, hp=1)
    after, result = step(request, handle, live, loader, monkeypatch)
    assert next(e for e in result.proposed_events if e.type == "attack_rolled").is_crit is True
    assert (
        next(a for a in after.state_snapshot.character_states if a.entity_id == FOE).hp_current == 0
    )


@pytest.mark.parametrize(
    "case",
    [
        "hand",
        "verbal",
        "source",
        "prepared",
        "ability",
        "grant",
        "activity",
        "slot",
        "conditions",
        "competing",
    ],
)
def test_unknown_or_incomplete_policy_refuses_before_rng(case, monkeypatch):
    request, _, _, loader = shield_case()
    snap = request.state_snapshot
    pending = snap.combat_state.pending_reactions[0]
    actor = next(a for a in snap.character_states if a.entity_id == FOE)
    entry = snap.casting_state.actors[0]
    if case == "hand":
        actor = actor.model_copy(update={"other_hand_occupied": True})
    if case == "verbal":
        entry = entry.model_copy(update={"verbal_available": False})
    if case == "source":
        pending = pending.model_copy(update={"spell_source_id": "missing"})
    if case == "prepared":
        entry = entry.model_copy(
            update={"sources": (entry.sources[0].model_copy(update={"prepared_spells": ()}),)}
        )
    if case == "ability":
        actor = actor.model_copy(update={"spellcasting_ability": "wis"})
    if case == "grant":
        actor = actor.model_copy(update={"granted_features": ()})
    if case == "activity":
        pending = pending.model_copy(update={"activity_id": "wrong"})
    if case == "slot":
        pending = pending.model_copy(update={"slot_level": 2})
    if case == "conditions":
        pending = pending.model_copy(update={"conditions": (pending.conditions[1],)})
    snap = snap.model_copy(
        update={
            "character_states": tuple(
                actor if a.entity_id == FOE else a for a in snap.character_states
            ),
            "casting_state": snap.casting_state.model_copy(update={"actors": (entry,)}),
            "combat_state": snap.combat_state.model_copy(
                update={
                    "pending_reactions": [pending, pending.model_copy(update={"owner_id": HERO})]
                    if case == "competing"
                    else [pending]
                }
            ),
        }
    )
    request = request.model_copy(update={"state_snapshot": snap})
    original = deepcopy(request)
    monkeypatch.setattr(RNGState, "restore", lambda *_: pytest.fail("RNG before policy refusal"))
    result = execute(request, loader)
    assert result.status == "unsupported"
    assert result.state_delta is None
    assert result.rng_transition is None
    assert result.proposed_events == ()
    assert request == original


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
def test_fault_after_real_shield_effect_has_no_partial_publication(error, monkeypatch):
    request, _, live, loader = shield_case()
    original = deepcopy(request)
    events = tuple(live.event_log)
    resolver = evaluation_shield.resolve_activity

    def broken(*args, **kwargs):
        resolver(*args, **kwargs)
        assert args[1].event_emitter.__self__.effects
        raise error("after actual Shield")

    monkeypatch.setattr(evaluation_shield, "resolve_activity", broken)
    with pytest.raises(error):
        execute(request, loader)
    assert request == original
    assert tuple(live.event_log) == events


def test_concurrent_replay_stale_guards_graph_closure_and_previous_versions(monkeypatch):
    request, _, _, loader = shield_case()
    isolated(monkeypatch)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: execute(request, loader), range(8)))
    assert all(r == results[0] for r in results)
    result = results[0]
    after = apply_shield(request.state_snapshot, result)
    with pytest.raises(AssertionError):
        apply_shield(after, result)
    with pytest.raises(AssertionError):
        apply_shield(request.state_snapshot, result, world_version=8)
    op = next(o for o in result.state_delta.operations if isinstance(o, ShieldStateTransition))
    for patch in ({"owner_id": HERO}, {"actor_ids": (HERO,)}, {"expected": op.value}):
        with pytest.raises(ValidationError):
            ShieldStateTransition.model_validate(op.model_copy(update=patch))
    value = op.value.model_dump(mode="json")
    value["effect"]["changes"][0]["value"] = "6"
    with pytest.raises(ValidationError):
        type(op.value).model_validate_json(json.dumps(value))
    wire = request.model_dump(mode="json")
    wire["schema_version"] = "engine-evaluation/21"
    with pytest.raises(ValidationError):
        type(request).model_validate_json(json.dumps(wire))
    wire = request.state_snapshot.model_dump(mode="json")
    wire["snapshot_schema_version"] = "engine-snapshot/15"
    with pytest.raises(ValidationError):
        CombatSnapshot.model_validate_json(json.dumps(wire))
    prior = request.model_copy(
        update={
            "ruleset_binding": request.ruleset_binding.model_copy(
                update={"evaluator_version": "dnd5e-evaluation/34"}
            )
        }
    )
    with pytest.raises(RulesetBindingError):
        execute(prior, loader)
