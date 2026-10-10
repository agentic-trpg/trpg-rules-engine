"""Strict, serializable local contracts and explicit RNG/binding boundaries."""

import json
import random
from copy import deepcopy

import pytest
from pydantic import TypeAdapter, ValidationError

from dnd5e_engine import evaluation_delta as delta_types
from dnd5e_engine.evaluation_contracts import (
    CombatIntentPayload,
    PreflightChoice,
    RuleError,
    RuleEvaluationRequest,
    RuleEvaluationResult,
)
from dnd5e_engine.evaluation_delta import HPDelta, StateDelta, StateDeltaOperation
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_rng import RNGContext, RNGState, RNGTransition
from dnd5e_engine.evaluation_ruleset import RulesetBindingError, ruleset_binding, verify_ruleset
from dnd5e_engine.evaluation_snapshot import capture_combat_snapshot
from dnd5e_engine.evaluation_state import (
    CombatSnapshot,
    InventoryState,
    NonCombatSnapshot,
    StateSnapshot,
)
from dnd5e_engine.specs import GridScene
from dnd5e_engine.types.effects import ActiveEffect
from tests.evaluation_support import FOE, HERO, WEAPON, synthetic_combat, synthetic_loader


def input_request():
    _, live = synthetic_combat()
    snapshot = capture_combat_snapshot(
        live,
        resource_state=None,
        inventory_state=InventoryState(entries=()),
        grid=GridScene(width=3, height=3),
        world_version=7,
        combat_id="combat:synthetic",
    )
    return RuleEvaluationRequest(
        schema_version="engine-evaluation/13",
        session_id=live.session_id,
        command_id="command:synthetic",
        operation_kind="combat.intent",
        actor_id=HERO,
        payload=CombatIntentPayload(intent_type="attack", weapon_id=WEAPON, target_id=FOE),
        state_snapshot=snapshot,
        ruleset_binding=ruleset_binding(live.ruleset_loader),
        rng_context=RNGContext(stream_id="main", version=3, state=RNGState.capture(live.rng)),
    )


def result_for(request, status="accepted"):
    return RuleEvaluationResult(
        schema_version="engine-evaluation/13",
        session_id=request.session_id,
        command_id=request.command_id,
        input_world_version=request.state_snapshot.world_version,
        input_ruleset_binding=request.ruleset_binding,
        status=status,
        read_set=(),
        state_delta=StateDelta(operations=(), expected_world_version=7)
        if status == "accepted"
        else None,
        proposed_events=(),
        rng_transition=RNGTransition.between(
            request.rng_context, request.rng_context.state.restore()
        )
        if status == "accepted"
        else None,
        choice=PreflightChoice(kind="attack.weapon", actor_id=HERO, allowed_weapon_ids=(WEAPON,))
        if status == "needs_choice"
        else None,
        error=RuleError(code="test.reason", reason="synthetic reason")
        if status in ("rejected", "unsupported")
        else None,
    )


def test_complete_combat_and_request_json_roundtrip():
    request = input_request()
    assert RuleEvaluationRequest.model_validate_json(request.model_dump_json()) == request
    assert (
        TypeAdapter(StateSnapshot).validate_json(request.state_snapshot.model_dump_json())
        == request.state_snapshot
    )
    assert "rng" not in request.state_snapshot.model_dump_json()


def test_noncombat_has_shared_complete_actors_but_no_combat_component():
    value = input_request().state_snapshot.model_dump(mode="json")
    value["snapshot_kind"] = "non_combat"
    combat = value.pop("combat_state")
    value["combat_setup"] = None
    snapshot = NonCombatSnapshot.model_validate_json(json.dumps(value))
    assert TypeAdapter(StateSnapshot).validate_json(snapshot.model_dump_json()) == snapshot
    value["combat_state"] = combat
    with pytest.raises(ValidationError):
        NonCombatSnapshot.model_validate_json(json.dumps(value))


@pytest.mark.parametrize("status", ["accepted", "rejected", "needs_choice", "unsupported"])
def test_four_result_states_roundtrip_and_match_request(status):
    request = input_request()
    result = result_for(request, status)
    assert RuleEvaluationResult.model_validate_json(result.model_dump_json()) == result
    result.verify_request(request)


@pytest.mark.parametrize("status", ["rejected", "needs_choice", "unsupported"])
@pytest.mark.parametrize("field", ["state_delta", "rng_transition", "proposed_events"])
def test_nonaccepted_cannot_carry_partial_mechanics(status, field):
    request = input_request()
    value = result_for(request, status).model_dump(mode="json")
    value[field] = result_for(request).model_dump(mode="json")[field]
    if field == "proposed_events":
        value[field] = [{"type": "intent_submitted", "actor_id": HERO, "intent_type": "attack"}]
    with pytest.raises(ValidationError):
        RuleEvaluationResult.model_validate_json(json.dumps(value))


@pytest.mark.parametrize("status", ["committed", "error", "internal_error", "conflict"])
def test_no_fifth_rule_status(status):
    value = result_for(input_request()).model_dump(mode="json")
    value["status"] = status
    with pytest.raises(ValidationError):
        RuleEvaluationResult.model_validate_json(json.dumps(value))


def test_every_contract_envelope_field_is_required_and_unknown_fields_fail():
    request = input_request()
    models = [
        request,
        request.state_snapshot,
        request.ruleset_binding,
        request.rng_context,
        request.rng_context.state,
        result_for(request),
        result_for(request).state_delta,
        result_for(request).rng_transition,
        request.state_snapshot.character_states[0],
        request.state_snapshot.scene_state,
        request.state_snapshot.scene_state.grid,
        request.state_snapshot.combat_state,
    ]
    for model in models:
        assert model is not None
        value = model.model_dump(mode="json")
        for field in type(model).model_fields:
            missing = deepcopy(value)
            del missing[field]
            with pytest.raises(ValidationError):
                type(model).model_validate_json(json.dumps(missing))
        value["unknown"] = 1
        with pytest.raises(ValidationError):
            type(model).model_validate_json(json.dumps(value))


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", "module-contracts/0.2"),
        ("operation_kind", "attack"),
        ("actor_id", None),
        ("actor_id", ""),
        ("session_id", "another-session"),
    ],
)
def test_request_version_operation_actor_and_session_errors(field, value):
    payload = input_request().model_dump(mode="json")
    payload[field] = value
    with pytest.raises(ValidationError):
        RuleEvaluationRequest.model_validate_json(json.dumps(payload))


def test_operation_kind_selects_payload_schema_and_snapshot_kind():
    value = input_request().model_dump(mode="json")
    value["operation_kind"] = "rules.check"
    with pytest.raises(ValidationError):
        RuleEvaluationRequest.model_validate_json(json.dumps(value))
    value["operation_kind"] = "combat.intent"
    value["state_snapshot"]["snapshot_kind"] = "non_combat"
    del value["state_snapshot"]["combat_state"]
    with pytest.raises(ValidationError):
        RuleEvaluationRequest.model_validate_json(json.dumps(value))


@pytest.mark.parametrize("mutation", ["actor", "position", "ledger", "side", "effect", "turn"])
def test_snapshot_dependency_closure_is_not_optional(mutation):
    value = input_request().state_snapshot.model_dump(mode="json")
    state = value["combat_state"]
    if mutation == "actor":
        value["character_states"].pop()
    elif mutation == "position":
        del state["actor_zone"][FOE]
    elif mutation == "ledger":
        del state["movement_ledgers"][FOE]
    elif mutation == "side":
        state["party_ids"].append("missing:actor")
    elif mutation == "turn":
        state["current_turn_index"] = 99
    else:
        value["effect_states"] = [
            effect_state(
                ActiveEffect(
                    id="effect:synthetic",
                    name="Synthetic",
                    target_id="missing:actor",
                    origin="synthetic",
                )
            ).model_dump(mode="json")
        ]
    with pytest.raises(ValidationError):
        CombatSnapshot.model_validate_json(json.dumps(value))


def test_closed_delta_union_and_hp_arithmetic():
    operation = HPDelta(
        kind="actor.hp_delta", target_id=FOE, expected_hp=10, amount=-4, resulting_hp=6
    )
    adapter = TypeAdapter(StateDeltaOperation)
    assert adapter.validate_json(operation.model_dump_json()) == operation
    for mutation in [
        {"kind": "json.patch", "path": "/hp"},
        operation.model_dump() | {"path": "actor.hp"},
        operation.model_dump() | {"resulting_hp": 7},
    ]:
        with pytest.raises(ValidationError):
            adapter.validate_json(json.dumps(mutation))


@pytest.mark.parametrize("gaussian", [False, True])
def test_rng_complete_state_roundtrip_and_deterministic_successor(gaussian):
    rng = random.Random(100)
    if gaussian:
        rng.gauss(0, 1)
    state = RNGState.capture(rng)
    restored = RNGState.model_validate_json(state.model_dump_json()).restore()
    assert restored.getstate() == rng.getstate()
    assert [restored.randint(1, 20) for _ in range(20)] == [rng.randint(1, 20) for _ in range(20)]


def test_rng_no_consumption_and_transition_validation():
    context = input_request().rng_context
    transition = RNGTransition.between(context, context.state.restore())
    assert not transition.state_changed
    transition.verify_input(context)
    bad = transition.model_dump(mode="json") | {"state_changed": True}
    with pytest.raises(ValidationError):
        RNGTransition.model_validate_json(json.dumps(bad))
    with pytest.raises(ValueError):
        transition.verify_input(context.model_copy(update={"version": 4}))
    for key, value in [
        ("encoding", "pickle/1"),
        ("index", 625),
        ("words", [1]),
        ("gaussian", float("inf")),
    ]:
        bad = context.state.model_dump(mode="json") | {key: value}
        with pytest.raises(ValidationError):
            RNGState.model_validate_json(json.dumps(bad))


def test_binding_hashes_effective_overrides_and_rejects_mismatch():
    loader = synthetic_loader()
    binding = ruleset_binding(loader)
    assert ruleset_binding(synthetic_loader()) == binding
    verify_ruleset(binding, loader)
    loader.get_weapon(WEAPON).magical_bonus = 2
    assert ruleset_binding(loader) != binding
    with pytest.raises(RulesetBindingError):
        verify_ruleset(binding, loader)
    for key, value in [("ruleset_id", "another"), ("evaluator_version", "another")]:
        with pytest.raises(RulesetBindingError):
            verify_ruleset(binding.model_copy(update={key: value}), synthetic_loader())


def test_proposed_event_cannot_gain_committed_identity_and_result_correlation():
    request = input_request()
    result = result_for(request)
    value = result.model_dump(mode="json")
    value["proposed_events"] = [
        {
            "type": "intent_submitted",
            "actor_id": HERO,
            "intent_type": "attack",
            "event_id": "forged",
        }
    ]
    with pytest.raises(ValidationError):
        RuleEvaluationResult.model_validate_json(json.dumps(value))
    with pytest.raises(ValueError):
        result.verify_request(request.model_copy(update={"command_id": "other"}))


@pytest.mark.parametrize(
    "status,changes",
    [
        ("accepted", {"state_delta": None}),
        ("accepted", {"rng_transition": None}),
        ("accepted", {"error": {"code": "error", "reason": "forbidden"}}),
        ("rejected", {"error": None}),
        ("unsupported", {"error": None}),
        ("needs_choice", {"choice": None}),
        ("needs_choice", {"error": {"code": "error", "reason": "forbidden"}}),
    ],
)
def test_result_branch_combinations_fail(status, changes):
    value = result_for(input_request(), status).model_dump(mode="json") | changes
    with pytest.raises(ValidationError):
        RuleEvaluationResult.model_validate_json(json.dumps(value))


def test_all_closed_delta_operation_schemas_require_fields_and_forbid_unknown_keys():
    request = input_request()
    actor = request.state_snapshot.character_states[0]
    state = request.state_snapshot.combat_state
    budget = delta_types.AttackBudgetState.model_validate(
        {name: getattr(actor, name) for name in delta_types.AttackBudgetState.model_fields}
    )
    death = delta_types.DeathState(is_alive=True, death_saves=None)
    turn = delta_types.TurnState.model_validate(
        {name: getattr(state, name) for name in delta_types.TurnState.model_fields}
    )
    ledger = state.movement_ledgers[HERO]
    operations = [
        HPDelta(kind="actor.hp_delta", target_id=HERO, expected_hp=40, amount=0, resulting_hp=40),
        delta_types.TempHPSet(
            kind="actor.temp_hp_set", target_id=HERO, expected_temp_hp=0, temp_hp=0
        ),
        delta_types.DeathStateUpdate(
            kind="actor.death_state_update", target_id=HERO, expected=death, value=death
        ),
        delta_types.ConditionsUpdate(
            kind="actor.conditions_update", target_id=HERO, expected=(), value=()
        ),
        delta_types.DamageAttributionUpdate(
            kind="actor.damage_attribution_update", target_id=HERO, expected=None, value=None
        ),
        delta_types.ActionBudgetUpdate(
            kind="combat.action_budget_update", actor_id=HERO, expected=budget, value=budget
        ),
        delta_types.TurnUpdate(
            kind="combat.turn_update", combat_id=state.combat_id, expected=turn, value=turn
        ),
        delta_types.MovementLedgerUpdate(
            kind="combat.movement_ledger_update", actor_id=HERO, expected=ledger, value=ledger
        ),
        delta_types.DamageSequenceUpdate(
            kind="combat.damage_sequence_update", combat_id=state.combat_id, expected=0, value=0
        ),
        delta_types.ProcessedDamageUpdate(
            kind="combat.processed_damage_update",
            combat_id=state.combat_id,
            expected=frozenset(),
            value=frozenset(),
        ),
        delta_types.DeathLedgerUpdate(
            kind="combat.death_ledger_update",
            combat_id=state.combat_id,
            expected_dead_ids=frozenset(),
            dead_ids=frozenset(),
            expected_records=(),
            records=(),
        ),
    ]
    adapter = TypeAdapter(StateDeltaOperation)
    for operation in operations:
        value = operation.model_dump(mode="json")
        assert adapter.validate_json(json.dumps(value)) == operation
        for field in type(operation).model_fields:
            missing = deepcopy(value)
            del missing[field]
            with pytest.raises(ValidationError):
                adapter.validate_json(json.dumps(missing))
        with pytest.raises(ValidationError):
            adapter.validate_json(json.dumps(value | {"unknown": 1}))


def test_binding_rejects_inconsistent_item_accessor_and_enumeration():
    loader = synthetic_loader()
    original = loader.get_weapon(WEAPON)
    loader.get_weapon = lambda slug: original.model_copy(update={"magical_bonus": 5})
    with pytest.raises(RulesetBindingError):
        ruleset_binding(loader)
    loader = synthetic_loader()
    loader.list_slugs = lambda category: [WEAPON, WEAPON] if category == "items" else []
    with pytest.raises(RulesetBindingError):
        ruleset_binding(loader)
