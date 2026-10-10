"""Differential execution and limited SM-style application of complete typed deltas."""

import asyncio
import random
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from pydantic_core import to_json

from dnd5e_engine import evaluation_delta as delta
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.evaluation import evaluate
from dnd5e_engine.evaluation_contracts import (
    CombatIntentPayload,
    RuleEvaluationRequest,
    RuleEvaluationResult,
)
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_projection import EvaluationInvariantError, attack_delta
from dnd5e_engine.evaluation_rng import RNGContext, RNGState
from dnd5e_engine.evaluation_ruleset import RulesetBindingError, ruleset_binding
from dnd5e_engine.evaluation_snapshot import capture_combat_snapshot
from dnd5e_engine.evaluation_state import CombatSnapshot
from dnd5e_engine.events import AttackRolled
from dnd5e_engine.specs import GridScene
from dnd5e_engine.types.effects import ActiveEffect
from tests.evaluation_support import FOE, HERO, WEAPON, synthetic_combat, synthetic_loader


def request_and_live(**kwargs):
    handle, live = synthetic_combat(**kwargs)
    snapshot = capture_combat_snapshot(
        live, grid=GridScene(width=3, height=3), world_version=7, combat_id="combat:synthetic"
    )
    request = RuleEvaluationRequest(
        schema_version="engine-evaluation/1",
        session_id=live.session_id,
        command_id="command:synthetic",
        operation_kind="combat.intent",
        actor_id=HERO,
        payload=CombatIntentPayload(intent_type="attack", weapon_id=WEAPON, target_id=FOE),
        state_snapshot=snapshot,
        ruleset_binding=ruleset_binding(live.ruleset_loader),
        rng_context=RNGContext(stream_id="main", version=3, state=RNGState.capture(live.rng)),
    )
    return request, handle, live


def execute(request, loader=None):
    return asyncio.run(evaluate(request, loader=loader or synthetic_loader()))


def apply_delta(snapshot, result):
    """Test consumer checks expected values; never derives mutations from event text."""
    value = snapshot.model_dump(mode="python")
    actors = {a["entity_id"]: a for a in value["character_states"]}
    combat = value["combat_state"]
    assert result.state_delta.expected_world_version == snapshot.world_version
    for op in result.state_delta.operations:
        if isinstance(op, delta.HPDelta):
            actor = actors[op.target_id]
            assert actor["hp_current"] == op.expected_hp
            actor["hp_current"] += op.amount
            assert actor["hp_current"] == op.resulting_hp
        elif isinstance(op, delta.TempHPSet):
            actor = actors[op.target_id]
            assert actor["temp_hp"] == op.expected_temp_hp
            actor["temp_hp"] = op.temp_hp
        elif isinstance(op, (delta.DeathStateUpdate, delta.ActionBudgetUpdate, delta.TurnUpdate)):
            target = (
                actors[op.target_id]
                if isinstance(op, delta.DeathStateUpdate)
                else actors[op.actor_id]
                if isinstance(op, delta.ActionBudgetUpdate)
                else combat
            )
            expected = op.expected.model_dump(mode="python")
            assert {k: target[k] for k in expected} == expected
            target.update(op.value.model_dump(mode="python"))
        elif isinstance(op, delta.ConditionsUpdate):
            assert actors[op.target_id]["conditions"] == [c.model_dump() for c in op.expected]
            actors[op.target_id]["conditions"] = [c.model_dump() for c in op.value]
        elif isinstance(op, delta.DamageAttributionUpdate):
            assert actors[op.target_id]["last_damaged_by"] == op.expected
            actors[op.target_id]["last_damaged_by"] = op.value
        elif isinstance(op, delta.MovementLedgerUpdate):
            assert combat["movement_ledgers"][op.actor_id] == op.expected.model_dump()
            combat["movement_ledgers"][op.actor_id] = op.value.model_dump()
        elif isinstance(op, delta.DamageSequenceUpdate):
            assert combat["damage_instance_sequence"] == op.expected
            combat["damage_instance_sequence"] = op.value
        elif isinstance(op, delta.ProcessedDamageUpdate):
            assert combat["processed_zero_hp_damage_instances"] == op.expected
            combat["processed_zero_hp_damage_instances"] = set(op.value)
        elif isinstance(op, delta.DeathLedgerUpdate):
            assert combat["dead_ids"] == op.expected_dead_ids
            assert combat["deaths_recorded"] == [r.model_dump() for r in op.expected_records]
            combat["dead_ids"] = set(op.dead_ids)
            combat["deaths_recorded"] = [r.model_dump() for r in op.records]
        else:
            raise TypeError(f"unhandled delta: {op.kind}")
    combat["event_log"] += tuple(e.model_dump() for e in result.proposed_events)
    return CombatSnapshot.model_validate_json(to_json(value))


@pytest.mark.parametrize(
    "seed,ac,hp,temp_hp",
    [
        (0, 1, 100, 0),
        (1, 99, 100, 0),
        (5, 1, 100, 2),
        (31, 1, 100, 0),
        (0, 1, 1, 0),
        (0, 1, 100, 100),
    ],
)
@pytest.mark.parametrize(
    "defense", [None, "damage_resistances", "damage_immunities", "damage_vulnerabilities"]
)
def test_legacy_stateless_complete_delta_events_and_rng(seed, ac, hp, temp_hp, defense):
    request, handle, live = request_and_live(seed=seed, ac=ac, hp=hp, temp_hp=temp_hp)
    if defense:
        live.initiative[1] = live.initiative[1].model_copy(update={defense: ["slashing"]})
        request = request.model_copy(
            update={
                "state_snapshot": capture_combat_snapshot(
                    live,
                    grid=GridScene(width=3, height=3),
                    world_version=7,
                    combat_id="combat:synthetic",
                )
            }
        )
    before = deepcopy(request)
    asyncio.run(orch.submit_player_intent(handle, HERO, request.payload))
    legacy = capture_combat_snapshot(
        live, grid=GridScene(width=3, height=3), world_version=7, combat_id="combat:synthetic"
    )
    registered = dict(orch._REGISTRY)
    result = execute(request)
    assert result.status == "accepted", result.error
    assert result.proposed_events == tuple(live.event_log)
    assert result.rng_transition.next_state == RNGState.capture(live.rng)
    assert apply_delta(request.state_snapshot, result) == legacy
    assert result == execute(request)
    assert request == before
    assert registered == orch._REGISTRY
    assert RuleEvaluationResult.model_validate_json(result.model_dump_json()) == result
    assert result.read_set[0].version == 7
    assert any(isinstance(e, AttackRolled) for e in result.proposed_events)


@pytest.mark.parametrize(
    "change,status",
    [
        ({"actor_id": "missing"}, "rejected"),
        ({"actor_id": FOE}, "rejected"),
        ({"target_id": "missing"}, "rejected"),
        ({"target_id": HERO}, "rejected"),
        ({"weapon_id": "missing"}, "rejected"),
        ({"weapon_id": None}, "needs_choice"),
        ({"intent_type": "cast_spell", "spell_id": "unknown"}, "unsupported"),
        ({"reckless_attack": True}, "unsupported"),
        ({"target_ids": (FOE,)}, "unsupported"),
    ],
)
def test_nonaccepted_has_no_commit_material_and_never_executes(monkeypatch, change, status):
    request, _, _ = request_and_live()
    if "actor_id" in change:
        request = request.model_copy(update=change)
    else:
        request = request.model_copy(update={"payload": request.payload.model_copy(update=change)})
    before = deepcopy(request)

    async def forbidden(*args, **kwargs):
        raise AssertionError("execution after refusal")

    monkeypatch.setattr(orch, "_submit_live_intent", forbidden)
    result = execute(request)
    assert result.status == status
    assert result.state_delta is None
    assert result.rng_transition is None
    assert result.proposed_events == ()
    assert request == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("help_grants", {FOE: [HERO]}),
        ("hidden_entities", {HERO}),
        ("vex_grants", {HERO: {FOE: 1}}),
        ("concentration_chain", {HERO: [(HERO, "effect", "origin")]}),
    ],
)
def test_complex_sidecars_are_unsupported_before_rng_restore(monkeypatch, field, value):
    request, _, _ = request_and_live()
    request.state_snapshot.combat_state.__dict__[field] = value
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("restored execution RNG"))
    assert execute(request).status == "unsupported"


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_fault_after_damage_cannot_publish_or_mutate_external_state(monkeypatch, failure):
    request, _, live = request_and_live()
    original = orch.resolve_activity
    registry = dict(orch._REGISTRY)
    before = deepcopy(request)
    observed = []
    live.event_listeners.append(observed.append)

    def broken(activity, ctx, **kwargs):
        original(activity, ctx, **kwargs)
        raise failure("fault after damage")

    monkeypatch.setattr(orch, "resolve_activity", broken)
    with pytest.raises(failure, match="fault after damage"):
        execute(request)
    assert request == before
    assert observed == []
    assert registry == orch._REGISTRY
    assert live.event_log == []
    assert live.initiative[1].hp_current == 100


def test_registry_and_global_rng_are_not_dependencies(monkeypatch):
    request, _, _ = request_and_live()

    def forbidden(*args, **kwargs):
        raise AssertionError("external state access")

    monkeypatch.setattr(orch, "_get_live", forbidden)
    monkeypatch.setattr(orch, "start_combat", forbidden)
    monkeypatch.setattr(random, "randint", forbidden)
    monkeypatch.setattr(orch, "_keep_ended", forbidden)
    assert execute(request).status == "accepted"


def test_concurrent_independent_requests_and_different_rulesets():
    request, _, _ = request_and_live()
    loaders = [synthetic_loader(), synthetic_loader()]
    loaders[1].get_weapon(WEAPON).magical_bonus = 5
    requests = [
        request,
        request.model_copy(
            update={
                "ruleset_binding": ruleset_binding(loaders[1]),
                "command_id": "other",
                "rng_context": request.rng_context.model_copy(
                    update={"state": RNGState.capture(random.Random(5))}
                ),
            }
        ),
    ]
    expected = [execute(r, assets) for r, assets in zip(requests, loaders, strict=True)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda i: execute(requests[i % 2], loaders[i % 2]), range(12)))
    assert results == [expected[i % 2] for i in range(12)]
    assert expected[0] != expected[1]


def test_binding_errors_are_not_rule_rejections():
    request, _, _ = request_and_live()
    loader = synthetic_loader()
    loader.get_weapon(WEAPON).magical_bonus = 1
    with pytest.raises(RulesetBindingError):
        execute(request, loader)


@pytest.mark.parametrize("change", ["range", "economy", "ended", "effect", "position"])
def test_preflight_boundaries_do_not_draw_or_change_snapshot(change):
    request, _, _ = request_and_live()
    state = request.state_snapshot.combat_state
    if change == "range":
        state.actor_zone[FOE] = "2,2"
    elif change == "economy":
        request.state_snapshot.character_states[0].__dict__.update(
            action_available=False, attacks_remaining=0
        )
    elif change == "ended":
        state.__dict__["ended"] = True
    elif change == "effect":
        request.state_snapshot.__dict__["effect_states"] = (
            effect_state(ActiveEffect(id="x", name="x", origin="x", target_id=HERO)),
        )
    else:
        state.actor_zone[HERO] = "9,9"
    before = deepcopy(request)
    result = execute(request)
    assert result.status == ("unsupported" if change in ("effect", "position") else "rejected")
    assert result.proposed_events == ()
    assert result.rng_transition is None
    assert request == before


def test_character_storage_order_is_independent_of_initiative_order():
    request, _, _ = request_and_live()
    snapshot = request.state_snapshot.model_copy(
        update={"character_states": tuple(reversed(request.state_snapshot.character_states))}
    )
    result = execute(request.model_copy(update={"state_snapshot": snapshot}))
    assert result.status == "accepted"


def test_required_nested_ledger_and_timing_fields_cannot_reset_to_defaults():
    from pydantic import ValidationError
    from pydantic_core import to_json

    request, _, _ = request_and_live()
    for path in ("movement_ledgers", "timed_activities"):
        raw = request.model_dump(mode="json")
        target = raw["state_snapshot"]["combat_state"][path]
        if path == "movement_ledgers":
            target = target[HERO]
        del target[next(iter(target))]
        with pytest.raises(ValidationError):
            RuleEvaluationRequest.model_validate_json(to_json(raw))


def test_omission_guard_rejects_unrepresented_actor_combat_and_effect_changes():
    request, _, _ = request_and_live()
    before = request.state_snapshot
    after = deepcopy(before)
    after.character_states[0].__dict__["ac"] += 1
    with pytest.raises(EvaluationInvariantError, match="actor changes"):
        attack_delta(before, after)
    after = deepcopy(before)
    after.combat_state.__dict__["actor_zone"] = {HERO: "2,2", FOE: "1,0"}
    with pytest.raises(EvaluationInvariantError, match="combat changes"):
        attack_delta(before, after)
    after = before.model_copy(
        update={
            "effect_states": (
                effect_state(ActiveEffect(id="x", name="x", origin="x", target_id=HERO)),
            )
        }
    )
    with pytest.raises(EvaluationInvariantError, match="snapshot dependency"):
        attack_delta(before, after)
