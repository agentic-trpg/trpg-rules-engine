"""R39 real evaluate: one provisional roll, Shield payment, completed concentration."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from dnd5e_srd_data import MemoryAssetLoader

from dnd5e_engine import evaluation_shield
from dnd5e_engine.evaluation_availability import (
    ActionAvailabilityRequest,
    query_action_availability,
)
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, RuleEvaluationResult
from dnd5e_engine.evaluation_delta import ResourceUpdate, SlotSpendLedgerUpdate
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from tests.effect_composition_support import (
    SECOND_ATTACKER,
    apply_composed,
    composed_case,
    continued,
)
from tests.evaluation_support import FOE, HERO, WEAPON
from tests.test_stateless_attack import execute
from tests.test_stateless_hold_turn import isolated


@pytest.mark.parametrize("pc", [False, True])
@pytest.mark.parametrize("temp", [0, 20])
@pytest.mark.parametrize("seed", [0, 1, 5, 7, 9, 31])
def test_real_composed_attack_explicit_roll_resource_graph_and_rng(seed, pc, temp, monkeypatch):
    request, loader, _ = composed_case(seed=seed, pc=pc, temp=temp)
    before = deepcopy(request)
    rng = request.rng_context.state.restore()
    natural = rng.randint(1, 20)
    owner = next(a for a in request.state_snapshot.character_states if a.entity_id == FOE)
    fires = natural != 1 and (natural == 20 or natural + 2 >= owner.ac)
    hit = natural != 1 and (natural == 20 or natural + 2 >= owner.ac + (5 if fires else 0))
    isolated(monkeypatch)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    result.verify_request(request)
    after = apply_composed(request.state_snapshot, result)
    roll = next(e for e in result.proposed_events if e.type == "attack_rolled")
    assert (roll.natural, roll.roll_total, roll.is_hit) == (natural, natural + 2, hit)
    assert len([e for e in result.proposed_events if e.type == "attack_rolled"]) == 1
    assert len([e for e in result.proposed_events if e.type == "reaction_triggered"]) == int(fires)
    checks = [e for e in result.proposed_events if e.type == "concentration_check"]
    assert len(checks) == int(hit)
    if hit:
        save = rng.randint(1, 20)
        total = save + (owner.constitution - 10) // 2
        assert (checks[0].natural, checks[0].roll_total, checks[0].dc, checks[0].succeeded) == (
            save,
            total,
            10,
            total >= 10,
        )
        assert bool(after.combat_state.concentration_chain) is (total >= 10)
    else:
        assert (
            after.combat_state.concentration_chain
            == request.state_snapshot.combat_state.concentration_chain
        )
    assert after.combat_state.effect_lifecycles == (
        request.state_snapshot.combat_state.effect_lifecycles
        if after.combat_state.concentration_chain
        else ()
    )
    assert after.combat_state.concentration_rounds_remaining == (
        request.state_snapshot.combat_state.concentration_rounds_remaining
        if after.combat_state.concentration_chain
        else {}
    )
    paid = [o for o in result.state_delta.operations if isinstance(o, ResourceUpdate)]
    ledgers = [o for o in result.state_delta.operations if isinstance(o, SlotSpendLedgerUpdate)]
    assert len(paid) == len(ledgers) == int(fires)
    if fires:
        assert paid[0].owner_id == ledgers[0].owner_id == FOE
        assert paid[0].amount == -1
    actual_owner = next(a for a in after.character_states if a.entity_id == FOE)
    assert actual_owner.spell_slots[1] == owner.spell_slots[1] - int(fires)
    assert actual_owner.reaction_available is (not fires)
    assert actual_owner.hp_current == owner.hp_current - (max(0, 12 - temp) if hit else 0)
    assert actual_owner.temp_hp == temp - (min(temp, 12) if hit else 0)
    assert any(e.id == "effect:imperceptible_barrier" for e in after.effect_states) is fires
    assert result.rng_transition.next_state == RNGState.capture(rng)
    assert result.stage_facts.coverage == "uncovered"
    assert not result.stage_facts.stages
    assert RuleEvaluationResult.model_validate_json(result.model_dump_json()) == result
    assert request == before


def test_availability_uses_the_same_draw_free_composition_preflight(monkeypatch):
    request, loader, _ = composed_case()
    original = deepcopy(request)
    monkeypatch.setattr(RNGState, "restore", lambda *a: pytest.fail("availability drew RNG"))
    query = ActionAvailabilityRequest(
        schema_version="engine-availability/13",
        session_id=request.session_id,
        operation_kind=request.operation_kind,
        actor_id=request.actor_id,
        payload=request.payload,
        state_snapshot=request.state_snapshot,
        ruleset_binding=request.ruleset_binding,
    )
    assert query_action_availability(query, loader=loader).status == "available"
    assert request == original


@pytest.mark.parametrize(
    "fault",
    [
        "unknown",
        "missing_clock",
        "wrong_owner",
        "wrong_source",
        "missing_material",
        "target",
        "turn_extra_fields",
    ],
)
def test_composition_gaps_fail_before_rng_or_payment(fault, monkeypatch):
    request, loader, _ = composed_case()
    snapshot = request.state_snapshot
    if fault == "unknown":
        effect = snapshot.effect_states[0].model_copy(update={"id": "effect:unknown"})
        snapshot = snapshot.model_copy(update={"effect_states": (effect,)})
    elif fault == "missing_clock":
        snapshot = snapshot.model_copy(
            update={
                "combat_state": snapshot.combat_state.model_copy(update={"effect_lifecycles": ()})
            }
        )
    elif fault in {"wrong_owner", "wrong_source"}:
        pending = snapshot.combat_state.pending_reactions[0]
        pending = pending.model_copy(
            update={"owner_id": HERO}
            if fault == "wrong_owner"
            else {"spell_source_id": "class:absent"}
        )
        snapshot = snapshot.model_copy(
            update={
                "combat_state": snapshot.combat_state.model_copy(
                    update={"pending_reactions": [pending]}
                )
            }
        )
    elif fault == "missing_material":
        casting = snapshot.casting_state
        snapshot = snapshot.model_copy(
            update={
                "casting_state": casting.model_copy(
                    update={
                        "actors": tuple(
                            a.model_copy(update={"materials": ()}) for a in casting.actors
                        )
                    }
                )
            }
        )
    elif fault == "target":
        request = request.model_copy(
            update={"payload": request.payload.model_copy(update={"target_id": HERO})}
        )
    else:
        request = request.model_copy(
            update={
                "actor_id": HERO,
                "payload": CombatIntentPayload(intent_type="pass", target_id=FOE),
            }
        )
        snapshot = snapshot.model_copy(
            update={
                "combat_state": snapshot.combat_state.model_copy(update={"current_turn_index": 1})
            }
        )
    request = request.model_copy(update={"state_snapshot": snapshot})
    original = deepcopy(request)
    monkeypatch.setattr(
        RNGState, "restore", lambda *a: pytest.fail("unsupported composition restored RNG")
    )
    result = execute(request, loader)
    assert result.status == "unsupported"
    assert result.state_delta is result.rng_transition is result.stage_facts is None
    assert not result.proposed_events
    assert request == original


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
def test_failure_after_real_shield_payment_never_returns_partial_result(error, monkeypatch):
    request, loader, _ = composed_case(seed=0)
    original = deepcopy(request)
    resolve = evaluation_shield.resolve_activity
    seen = []

    def fail(*args, **kwargs):
        seen.append(resolve(*args, **kwargs))
        raise error("after Shield")

    monkeypatch.setattr(evaluation_shield, "resolve_activity", fail)
    with pytest.raises(error, match="after Shield"):
        execute(request, loader)
    assert seen
    assert request == original


def test_interleaved_sessions_are_deterministic_and_guarded():
    request, loader, _ = composed_case(seed=0)
    other = request.model_copy(
        update={
            "session_id": "session:other",
            "state_snapshot": request.state_snapshot.model_copy(
                update={"session_id": "session:other"}
            ),
        }
    )
    expected = (execute(request, loader), execute(other, loader))
    with ThreadPoolExecutor(max_workers=4) as pool:
        actual = list(pool.map(lambda i: execute((request, other)[i % 2], loader), range(8)))
    assert actual == [expected[i % 2] for i in range(8)]
    after = apply_composed(request.state_snapshot, expected[0])
    with pytest.raises(AssertionError):
        apply_composed(after, expected[0])


@pytest.mark.parametrize("pc", [False, True])
def test_existing_zero_hp_cleanup_preserves_independent_shield(pc):
    request, loader, _ = composed_case(seed=9, pc=pc, hp=1)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    roll = next(e for e in result.proposed_events if e.type == "attack_rolled")
    assert roll.is_crit
    after = apply_composed(request.state_snapshot, result)
    assert not after.combat_state.concentration_chain
    assert not after.combat_state.effect_lifecycles
    assert len(after.effect_states) == 1
    assert after.effect_states[0].id == "effect:imperceptible_barrier"
    assert not next(a for a in after.character_states if a.entity_id == HERO).conditions
    assert (FOE in after.combat_state.dead_ids) is (not pc)
    assert bool(after.combat_state.active_reaction_responses) is pc


@pytest.mark.parametrize("seed", [0, 1, 5, 7, 9, 31])
def test_one_complete_two_target_chain_composes_without_clock_reset(seed):
    request, loader, _ = composed_case(seed=seed, multi=True)
    old_clocks = request.state_snapshot.combat_state.effect_lifecycles
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    after = apply_composed(request.state_snapshot, result)
    assert after.combat_state.effect_lifecycles == (
        old_clocks if after.combat_state.concentration_chain else ()
    )
    assert not any(e.type == "save_rolled" for e in result.proposed_events)


def test_paid_actor_mirror_is_current_before_completed_concentration(monkeypatch):
    from dnd5e_engine import evaluation_concentration

    request, loader, _ = composed_case(seed=1)
    resolve = evaluation_concentration.resolve_snapshot_save
    slots = []

    def observe(actor, *args, **kwargs):
        slots.append(actor.spell_slots[1])
        return resolve(actor, *args, **kwargs)

    monkeypatch.setattr(evaluation_concentration, "resolve_snapshot_save", observe)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert slots == [3]


def test_existing_active_shield_can_defend_next_ordinary_attacker_once():
    request, loader, _ = composed_case(seed=0, extra_attacker=True)
    first = execute(request, loader)
    assert first.status == "accepted", first.error
    next_request = continued(
        request,
        first,
        intent=CombatIntentPayload(intent_type="attack", weapon_id=WEAPON, target_id=FOE),
    )
    assert next_request.actor_id == SECOND_ATTACKER
    clocks = next_request.state_snapshot.combat_state.effect_lifecycles
    second = execute(next_request, loader)
    assert second.status == "accepted", second.error
    after = apply_composed(next_request.state_snapshot, second)
    assert not any(e.type == "reaction_triggered" for e in second.proposed_events)
    assert not any(isinstance(o, ResourceUpdate) for o in second.state_delta.operations)
    assert after.combat_state.effect_lifecycles == clocks
    assert any(e.id == "effect:imperceptible_barrier" for e in after.effect_states)


@pytest.mark.parametrize("temp", [0, 20])
def test_one_completed_mixed_damage_instance_has_one_concentration_check(temp):
    request, loader, _ = composed_case(seed=1, mixed=True, temp=temp)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    after = apply_composed(request.state_snapshot, result)
    packets = [e for e in result.proposed_events if e.type == "damage_applied"]
    assert len(packets) == 2
    assert len({e.damage_instance_id for e in packets}) == 1
    checks = [e for e in result.proposed_events if e.type == "concentration_check"]
    assert len(checks) == 1
    assert checks[0].dc == 12
    assert next(a for a in after.character_states if a.entity_id == FOE).hp_current == 40 - max(
        0, 24 - temp
    )


@pytest.mark.parametrize("mode", ["empty", "spent"])
def test_unavailable_legal_shield_preserves_declaration_without_payment(mode):
    request, loader, _ = composed_case(seed=0)
    snapshot = request.state_snapshot
    actors = tuple(
        a.model_copy(
            update={"spell_slots": {**a.spell_slots, 1: 0}}
            if mode == "empty"
            else {"reaction_available": False}
        )
        if a.entity_id == FOE
        else a
        for a in snapshot.character_states
    )
    resources = snapshot.resource_state.model_copy(
        update={
            "pools": tuple(
                p.model_copy(update={"current": 0})
                if mode == "empty" and getattr(p, "level", None) == 1
                else p
                for p in snapshot.resource_state.pools
            )
        }
    )
    snapshot = snapshot.model_copy(update={"character_states": actors, "resource_state": resources})
    request = request.model_copy(update={"state_snapshot": snapshot})
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    after = apply_composed(snapshot, result)
    assert not any(e.type == "reaction_triggered" for e in result.proposed_events)
    assert not any(isinstance(o, ResourceUpdate) for o in result.state_delta.operations)
    assert after.resource_state == snapshot.resource_state
    assert after.combat_state.pending_reactions == snapshot.combat_state.pending_reactions


def test_composed_projector_retains_the_full_base_omission_guard():
    from dnd5e_engine.evaluation_composition import composition_delta
    from dnd5e_engine.evaluation_projection import EvaluationInvariantError

    request, loader, _ = composed_case(seed=0)
    after = apply_composed(request.state_snapshot, execute(request, loader))
    actors = tuple(
        a.model_copy(update={"ac": a.ac + 1}) if a.entity_id == FOE else a
        for a in after.character_states
    )
    after = after.model_copy(update={"character_states": actors})
    with pytest.raises(EvaluationInvariantError, match="unrepresented"):
        composition_delta(request.state_snapshot, after)


def test_missing_canonical_hold_asset_is_unsupported_before_rng(monkeypatch):
    request, loader, _ = composed_case(seed=0)
    loader = MemoryAssetLoader(
        items=[loader.get_item(s) for s in loader.list_slugs("items")],
        spells=[loader.get_spell(s) for s in loader.list_slugs("spells") if s != "hold-person"],
        classes=[loader.get_class(s) for s in loader.list_slugs("classes")],
        subclasses=[loader.get_subclass(s) for s in loader.list_slugs("subclasses")],
        features=[loader.get_feature(s) for s in loader.list_slugs("features")],
        conditions=[loader.get_condition(s) for s in loader.list_slugs("conditions")],
    )
    request = request.model_copy(update={"ruleset_binding": ruleset_binding(loader)})
    monkeypatch.setattr(RNGState, "restore", lambda *args: pytest.fail("missing asset drew RNG"))
    result = execute(request, loader)
    assert result.status == "unsupported"
    assert result.state_delta is result.rng_transition is result.stage_facts is None
