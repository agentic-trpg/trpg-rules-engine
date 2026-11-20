"""Terminal closure, independent commit consumption and Legacy outcome parity."""

import asyncio
from copy import deepcopy

import pytest
from pydantic import ValidationError
from pydantic_core import to_json

from dnd5e_engine import evaluation_delta as delta
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.evaluation_contracts import (
    CombatClosePayload,
    RuleEvaluationRequest,
    RuleEvaluationResult,
)
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import RulesetBindingError
from dnd5e_engine.evaluation_snapshot import capture_combat_snapshot
from dnd5e_engine.evaluation_state import CombatSnapshot, ConditionRecord, InventoryState
from dnd5e_engine.events import CombatEnded
from dnd5e_engine.outcome import DeathRecord, LootDrop
from dnd5e_engine.specs import GridScene
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import execute, request_and_live


def terminal_request(*, defeat=False):
    request, handle, live = request_and_live(ac=1, hp=1)
    live.xp_value_by_entity[FOE] = 50
    if defeat:
        live.initiative[0] = live.initiative[0].model_copy(
            update={"hp_current": 0, "is_alive": False}
        )
        live.tracked_hp[HERO] = 0
        live.dead_ids.add(HERO)
        live.deaths_recorded.append(
            DeathRecord(
                target_id=HERO,
                target_kind="character",
                location_id=live.scene_location_id,
                reason="instant_kill",
                killer_id=FOE,
            )
        )
    else:
        asyncio.run(orch.submit_player_intent(handle, HERO, request.payload))
    snapshot = capture_combat_snapshot(
        live,
        resource_state=None,
        casting_state=None,
        inventory_state=InventoryState(entries=()),
        grid=GridScene(width=3, height=3),
        world_version=7,
        combat_id="combat:synthetic",
    )
    return (
        request.model_copy(
            update={
                "schema_version": "engine-evaluation/24",
                "operation_kind": "combat.close",
                "payload": CombatClosePayload(kind="combat.close"),
                "state_snapshot": snapshot,
            }
        ),
        handle,
        live,
    )


def consume(snapshot, result, *, xp=10, fail=False):
    """Host-style atomic candidate; all owner/expected checks precede publication."""
    assert result.status == "accepted"
    assert result.state_delta.expected_world_version == snapshot.world_version
    candidate = snapshot.model_dump(mode="python")
    actors = {a["entity_id"]: a for a in candidate["character_states"]}
    state = candidate["combat_state"]
    for op in result.state_delta.operations:
        if isinstance(op, delta.DeathStateUpdate):
            actor = actors[op.target_id]
            assert {k: actor[k] for k in type(op.expected).model_fields} == op.expected.model_dump()
            actor.update(op.value.model_dump())
        elif isinstance(op, delta.CombatClose):
            assert state["combat_id"] == op.combat_id
            assert state["ended"] is op.expected_ended
            assert op.historical.residual_hp == {
                i: actors[i]["hp_current"] for i in state["party_ids"]
            }
            assert {r.target_id for r in op.historical.deaths} == state["dead_ids"]
            assert set(op.xp_increments) <= state["party_ids"] - state["dead_ids"]
            assert op.historical.expended_resources == state["expended_resources"]
            state["ended"] = op.ended
            xp += op.xp_increments.get(HERO, 0)
        else:
            raise TypeError(f"unexpected closure operation {op.kind}")
    result.rng_transition.verify_input(result_to_rng_context(result))
    if fail:
        raise RuntimeError("host commit fault")
    return CombatSnapshot.model_validate_json(to_json(candidate)), xp


def result_to_rng_context(result):
    from dnd5e_engine.evaluation_rng import RNGContext

    transition = result.rng_transition
    return RNGContext(
        stream_id=transition.stream_id,
        version=transition.input_version,
        state=transition.input_state,
    )


@pytest.mark.parametrize("defeat", [False, True])
def test_terminal_closure_legacy_outcome_and_atomic_consumer(monkeypatch, defeat):
    request, handle, _live = terminal_request(defeat=defeat)
    before = deepcopy(request)
    legacy = asyncio.run(orch.end_combat(handle))
    monkeypatch.setattr(orch, "_get_live", lambda *a: pytest.fail("registry access"))
    monkeypatch.setattr(orch, "CombatHandle", lambda *a: pytest.fail("legacy handle"))
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("execution RNG restored"))
    result = execute(request)
    assert result == execute(request)
    assert request == before
    assert result.status == "accepted"
    closure = result.state_delta.operations[-1]
    assert isinstance(closure, delta.CombatClose)
    assert closure.reason == legacy.outcome.ended_reason
    assert closure.xp_increments == legacy.outcome.xp_awarded
    for field in ("residual_hp", "residual_temp_hp", "expended_resources"):
        assert getattr(closure.historical, field) == getattr(legacy.outcome, field)
    assert [r.model_dump() for r in closure.historical.deaths] == [
        r.model_dump() for r in legacy.outcome.deaths
    ]
    assert result.proposed_events == tuple(legacy.events) == (CombatEnded(reason=closure.reason),)
    assert not result.rng_transition.state_changed
    assert result.rng_transition.next_state == request.rng_context.state
    committed, xp = consume(request.state_snapshot, result)
    assert committed.combat_state.ended
    assert all(
        not a.is_alive
        for a in committed.character_states
        if a.entity_id in committed.combat_state.dead_ids
    )
    assert xp == (10 if defeat else 60)
    assert (
        execute(request.model_copy(update={"state_snapshot": committed})).error.code
        == "combat_ended"
    )
    with pytest.raises(AssertionError):
        consume(committed, result, xp=xp)
    with pytest.raises(RuntimeError, match="host commit fault"):
        consume(request.state_snapshot, result, fail=True)
    assert request == before
    assert RuleEvaluationResult.model_validate_json(result.model_dump_json()) == result


def test_nonterminal_and_alien_actor_rejected_without_rng(monkeypatch):
    request, _, _ = request_and_live()
    request = request.model_copy(
        update={
            "schema_version": "engine-evaluation/24",
            "operation_kind": "combat.close",
            "payload": CombatClosePayload(kind="combat.close"),
        }
    )
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("RNG restored"))
    for candidate, code in [
        (request, "closure.nonterminal"),
        (request.model_copy(update={"actor_id": "missing"}), "actor_invalid"),
    ]:
        result = execute(candidate)
        assert result.status == "rejected"
        assert result.error.code == code
        assert result.state_delta is None
        assert result.rng_transition is None
        assert not result.proposed_events


@pytest.mark.parametrize(
    "mutation",
    [None, "pouch", "source", "duration", "other_condition", "dying", "missing_record"],
)
def test_equipped_wizard_closure_with_recorded_death_save(monkeypatch, mutation):
    """The closure consumes complete equipment, condition and terminal death facts."""
    from tests.test_stateless_casting_v20 import canonical_healing_wire
    from tests.test_stateless_hold_cast import cast_case

    opening, _, _, loader = cast_case(pc=True)
    wire, _ = canonical_healing_wire(opening)
    wire.update(schema_version="engine-evaluation/30", operation_kind="combat.close")
    wire["payload"] = {"kind": "combat.close"}
    snapshot = wire["state_snapshot"]
    inventory = snapshot["inventory_state"]["entries"]
    for enemy in snapshot["character_states"]:
        if weapon := enemy["weapon_in_hands"]:
            weapon_id = f"weapon:{enemy['entity_id']}"
            enemy.update(held_weapon_instance_id=weapon_id, carried_item_slugs=[weapon])
            inventory.append(
                {
                    "instance_id": weapon_id,
                    "owner_id": enemy["entity_id"],
                    "item_slug": weapon,
                    "quantity": 1,
                    "charges_remaining_per_unit": 0,
                    "accessible": True,
                }
            )
    spells = snapshot["casting_state"]["actors"][0]["sources"][0]["spells"]
    next(spell for spell in spells if spell["spell_id"] == "hold-person")["material"] = {
        "inventory_instance_id": "pouch:wizard",
        "available": True,
    }
    wizard = next(a for a in snapshot["character_states"] if a["entity_id"] == FOE)
    wizard.update(hp_current=0, is_alive=False)
    wizard["death_saves"] = {"successes": 0, "failures": 3, "is_stable": False}
    condition = ConditionRecord(
        condition="unconscious",
        source_entity_id="implied:event",
        scope="combat",
        duration_rounds=None,
        save_dc=None,
        applied_round=1,
        exhaustion_level=1,
        source_effect_id=None,
    ).model_dump()
    wizard["conditions"] = [condition]
    state = snapshot["combat_state"]
    state["dead_ids"] = [FOE]
    state["deaths_recorded"] = [
        {
            "target_id": FOE,
            "target_kind": "character",
            "location_id": snapshot["scene_state"]["scene_id"],
            "reason": "death_saves",
            "killer_id": None,
        }
    ]
    if mutation == "pouch":
        inventory[0]["accessible"] = False
    elif mutation == "source":
        condition["source_entity_id"] = "implied:revive"
    elif mutation == "duration":
        condition["duration_rounds"] = 1
    elif mutation == "other_condition":
        condition["condition"] = "prone"
    elif mutation == "dying":
        state["dead_ids"] = []
        state["deaths_recorded"] = []
        wizard["is_alive"] = True
        wizard["death_saves"]["failures"] = 2
    elif mutation == "missing_record":
        state["deaths_recorded"] = []

    request = RuleEvaluationRequest.model_validate_json(to_json(wire))
    before = deepcopy(request)
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("RNG restored"))
    result = execute(request, loader)
    assert request == before
    if mutation:
        assert result.status == "unsupported"
        assert result.error.code == "closure.capability"
        assert result.state_delta is None
        assert result.rng_transition is None
        assert not result.proposed_events
        return

    assert result.status == "accepted", result.error
    (closure,) = result.state_delta.operations
    assert isinstance(closure, delta.CombatClose)
    assert closure.reason == "defeat_tpk"
    assert [record.model_dump() for record in closure.historical.deaths] == [
        record.model_dump() for record in request.state_snapshot.combat_state.deaths_recorded
    ]
    assert result.proposed_events == (CombatEnded(reason="defeat_tpk"),)
    assert not result.rng_transition.state_changed


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_record",
        "hp",
        "kind",
        "location",
        "killer",
        "alive_pc",
        "resources",
        "effects",
        "dying",
    ],
)
def test_inconsistent_or_unmigrated_closure_refused(monkeypatch, mutation):
    request, _, _ = terminal_request(defeat=mutation == "alive_pc")
    snapshot = request.state_snapshot
    state = snapshot.combat_state
    if mutation == "missing_record":
        state.deaths_recorded.clear()
    elif mutation in ("kind", "location", "killer"):
        field, value = {
            "kind": ("target_kind", "npc"),
            "location": ("location_id", "elsewhere"),
            "killer": ("killer_id", "missing"),
        }[mutation]
        state.deaths_recorded[0] = state.deaths_recorded[0].model_copy(update={field: value})
    elif mutation in ("hp", "alive_pc", "dying"):
        index, changes = {
            "hp": (1, {"hp_current": 1}),
            "alive_pc": (0, {"is_alive": True}),
            "dying": (0, {"hp_current": 0}),
        }[mutation]
        snapshot.character_states[index].__dict__.update(changes)
    elif mutation == "resources":
        state.expended_resources["missing"] = {"slot": 1}
    else:
        state.hidden_entities.add(HERO)
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("RNG restored"))
    result = execute(request)
    assert result.status == "unsupported"
    assert result.state_delta is None
    assert result.rng_transition is None
    assert not result.proposed_events


def test_closure_versions_and_binding():
    request, _, _ = terminal_request()
    with pytest.raises(ValidationError):
        execute(request.model_copy(update={"schema_version": "engine-evaluation/1"}))
    with pytest.raises(ValidationError):
        execute(request.model_copy(update={"schema_version": "engine-evaluation/229"}))
    with pytest.raises(RulesetBindingError):
        execute(
            request.model_copy(
                update={
                    "ruleset_binding": request.ruleset_binding.model_copy(
                        update={"evaluator_version": "stale"}
                    )
                }
            )
        )
    result = execute(request)
    with pytest.raises(ValidationError):
        RuleEvaluationResult.model_validate(
            result.model_copy(update={"schema_version": "engine-evaluation/1"})
        )
    attack, _, _ = request_and_live()
    attack_result = execute(attack)
    with pytest.raises(ValueError, match="identity/version/binding"):
        attack_result.verify_request(
            attack.model_copy(update={"schema_version": "engine-evaluation/1"})
        )


def test_closure_fault_and_nonempty_loot_do_not_leak(monkeypatch):
    request, _, _ = terminal_request()
    before = deepcopy(request)
    from dnd5e_engine import evaluation_closure

    project = evaluation_closure.project_outcome

    def faulty(**facts):
        facts["actors"].clear()
        raise RuntimeError("projector fault")

    monkeypatch.setattr(evaluation_closure, "project_outcome", faulty)
    with pytest.raises(RuntimeError, match="projector fault"):
        execute(request)
    assert request == before

    def loot(**facts):
        outcome = project(**facts)
        outcome.loot_drops.append(LootDrop(source="transfer", item_id="item:test", to_id=HERO))
        return outcome

    monkeypatch.setattr(evaluation_closure, "project_outcome", loot)
    result = execute(request)
    assert result.status == "unsupported"
    assert result.error.code == "closure.loot"
    assert request == before


@pytest.mark.parametrize(
    "field", ["target_id", "target_kind", "location_id", "reason", "killer_id"]
)
def test_terminal_death_boundary_requires_every_field(field):
    request, _, _ = terminal_request()
    wire = request.model_dump(mode="python")
    del wire["state_snapshot"]["combat_state"]["deaths_recorded"][0][field]
    with pytest.raises(ValidationError):
        RuleEvaluationRequest.model_validate(wire)


def test_resources_and_zero_xp_are_informational_balances():
    request, _, _ = terminal_request()
    request.state_snapshot.combat_state.expended_resources[HERO] = {"1": 2}
    request.state_snapshot.combat_state.xp_value_by_entity[FOE] = 0
    result = execute(request)
    closure = result.state_delta.operations[-1]
    assert closure.xp_increments == {}
    assert closure.historical.expended_resources == {HERO: {"1": 2}}
    assert all(
        isinstance(op, (delta.CombatClose, delta.DeathStateUpdate))
        for op in result.state_delta.operations
    )
    committed, xp = consume(request.state_snapshot, result)
    assert committed.combat_state.expended_resources == {HERO: {"1": 2}}
    assert xp == 10
