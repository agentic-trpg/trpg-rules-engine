"""Contract and lifecycle regressions spanning successive committed proposals."""

from copy import deepcopy

import pytest
from pydantic import TypeAdapter, ValidationError

from dnd5e_engine import orchestrator as orch
from dnd5e_engine.evaluation_contracts import (
    CheckAdjudicationChoice,
    CombatIntentPayload,
    RuleEvaluationRequest,
)
from dnd5e_engine.evaluation_projection import EvaluationInvariantError, attack_delta
from dnd5e_engine.evaluation_rng import RNGContext, RNGState
from dnd5e_engine.evaluation_state import InventoryState, StateSnapshot
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import apply_delta, execute
from tests.test_stateless_weapons import weapon_case

OTHER = "npc:other"


def multi_enemy_case():
    request, _, _, loader = weapon_case("mace", seed=0)
    snapshot = request.state_snapshot
    hero, foe = snapshot.character_states
    hero = hero.model_copy(update={"bonus_action_available": False, "ac": 1})
    other = hero.model_copy(
        update={
            "entity_id": OTHER,
            "entity_type": "Monster",
            "initiative": 0,
            "bonus_action_available": False,
            "hp_current": 40,
            "hp_max": 40,
        }
    )
    foe = foe.model_copy(update={"hp_current": 1})
    state = snapshot.combat_state
    state = state.model_copy(
        update={
            "initiative_ids": (*state.initiative_ids, OTHER),
            "encounter_ids": {*state.encounter_ids, OTHER},
            "actor_zone": {**state.actor_zone, OTHER: "0,1"},
            "movement_ledgers": {**state.movement_ledgers, OTHER: state.movement_ledgers[FOE]},
            "xp_value_by_entity": {**state.xp_value_by_entity, OTHER: 50},
        }
    )
    return request.model_copy(
        update={
            "state_snapshot": snapshot.model_copy(
                update={
                    "character_states": (hero, foe, other),
                    "combat_state": state,
                }
            )
        }
    ), loader


def test_killed_enemy_does_not_block_remaining_enemy_attack():
    request, loader = multi_enemy_case()
    first = execute(request, loader)
    assert first.status == "accepted", first.error
    committed = apply_delta(request.state_snapshot, first)
    assert committed.combat_state.dead_ids == {FOE}
    assert not committed.character_states[1].is_alive
    assert committed.combat_state.initiative_ids[committed.combat_state.current_turn_index] == OTHER
    assert not committed.combat_state.ended
    second_request = request.model_copy(
        update={
            "command_id": "command:second",
            "actor_id": OTHER,
            "state_snapshot": committed,
            "payload": CombatIntentPayload(intent_type="attack", weapon_id="mace", target_id=HERO),
            "rng_context": RNGContext(
                stream_id="main", version=4, state=first.rng_transition.next_state
            ),
        }
    )
    second = execute(second_request, loader)
    assert second.status == "accepted", second.error
    after = apply_delta(committed, second)
    assert after.combat_state.dead_ids == {FOE}
    assert after.combat_state.deaths_recorded == committed.combat_state.deaths_recorded
    assert after.character_states[0].hp_current < committed.character_states[0].hp_current
    assert execute(second_request, loader) == second
    assert after.combat_state.initiative_ids[after.combat_state.current_turn_index] == HERO
    third_request = request.model_copy(
        update={
            "command_id": "command:third",
            "state_snapshot": after,
            "payload": CombatIntentPayload(intent_type="attack", weapon_id="mace", target_id=OTHER),
            "rng_context": RNGContext(
                stream_id="main", version=5, state=second.rng_transition.next_state
            ),
        }
    )
    third = execute(third_request, loader)
    assert third.status == "accepted", third.error
    final = apply_delta(after, third)
    assert final.combat_state.dead_ids == {FOE}
    assert final.character_states[2].hp_current < after.character_states[2].hp_current
    assert final.combat_state.event_log == (*after.combat_state.event_log, *third.proposed_events)


@pytest.mark.parametrize("mutation", ["record", "life", "hp", "kind", "location", "killer"])
def test_inconsistent_death_fails_before_rng(mutation, monkeypatch):
    request, loader = multi_enemy_case()
    committed = apply_delta(request.state_snapshot, execute(request, loader))
    state = committed.combat_state
    if mutation == "record":
        state.deaths_recorded.clear()
    elif mutation in ("life", "hp"):
        committed.character_states[1].__dict__.update(
            {"is_alive": True} if mutation == "life" else {"hp_current": 1}
        )
    else:
        field, value = {
            "kind": ("target_kind", "character"),
            "location": ("location_id", "other"),
            "killer": ("killer_id", "absent"),
        }[mutation]
        state.deaths_recorded[0] = state.deaths_recorded[0].model_copy(update={field: value})
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("RNG before validation"))
    result = execute(request.model_copy(update={"state_snapshot": committed}), loader)
    assert result.status == "unsupported"
    assert result.rng_transition is result.state_delta is None
    assert result.proposed_events == ()


def test_dead_target_rejected_before_payment_or_rng(monkeypatch):
    request, loader = multi_enemy_case()
    committed = apply_delta(request.state_snapshot, execute(request, loader))
    candidate = request.model_copy(
        update={
            "actor_id": OTHER,
            "state_snapshot": committed,
            "payload": CombatIntentPayload(intent_type="attack", weapon_id="mace", target_id=FOE),
        }
    )
    before = deepcopy(candidate)
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("RNG before rejection"))
    result = execute(candidate, loader)
    assert result.status == "rejected"
    assert result.error.code == "target_invalid"
    assert result.state_delta is result.rng_transition is None
    assert candidate == before


@pytest.mark.parametrize("kind", ["combat", "non_combat"])
def test_exactly_two_contexts_require_complete_inventory(kind):
    request, _ = multi_enemy_case()
    value = request.state_snapshot.model_dump(mode="json")
    value["snapshot_kind"] = kind
    if kind == "non_combat":
        del value["combat_state"]
    import json

    adapter = TypeAdapter(StateSnapshot)
    snapshot = adapter.validate_json(json.dumps(value))
    assert snapshot.inventory_state == InventoryState(entries=())
    for bad in [None, {}, [], {"entries": [], "unknown": 1}]:
        with pytest.raises(ValidationError):
            adapter.validate_json(json.dumps(value | {"inventory_state": bad}))
    del value["inventory_state"]
    with pytest.raises(ValidationError, match="inventory_state"):
        adapter.validate_json(json.dumps(value))


@pytest.mark.parametrize(
    "old_version", ["engine-snapshot/1", "engine-snapshot/2", "engine-snapshot/3"]
)
def test_old_snapshot_versions_are_not_silently_upgraded(old_version):
    request, _ = multi_enemy_case()
    value = request.model_dump(mode="json")
    value["state_snapshot"]["snapshot_schema_version"] = old_version
    import json

    with pytest.raises(ValidationError, match="engine-snapshot/4"):
        RuleEvaluationRequest.model_validate_json(json.dumps(value))
    value["state_snapshot"]["snapshot_kind"] = "combat_inventory"
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        RuleEvaluationRequest.model_validate_json(json.dumps(value))


def test_capture_projection_cannot_drop_or_rewrite_inventory():
    from tests.test_stateless_items import item_case

    request, _, _, _ = item_case()
    before = request.state_snapshot
    after = before.model_copy(update={"inventory_state": InventoryState(entries=())})
    with pytest.raises(EvaluationInvariantError, match="snapshot dependency"):
        attack_delta(before, after)


@pytest.mark.parametrize("fields", [(), ("dc", "dc")])
def test_adjudication_choice_requires_one_unambiguous_dc(fields):
    with pytest.raises(ValidationError):
        CheckAdjudicationChoice(kind="check.adjudication", actor_id=HERO, required_fields=fields)


@pytest.mark.parametrize("operation", ["attack", "check", "item", "closure"])
def test_every_evaluation_path_isolated_from_runtime_registry_and_external_publication(
    operation, monkeypatch
):
    from tests.test_stateless_attack import request_and_live
    from tests.test_stateless_checks import check_case
    from tests.test_stateless_closure import terminal_request
    from tests.test_stateless_items import item_case

    if operation == "item":
        request, _, live, loader = item_case()
    elif operation == "closure":
        request, _, live = terminal_request()
        loader = live.ruleset_loader
    else:
        request, _, live = request_and_live()
        loader = live.ruleset_loader
        if operation == "check":
            request = check_case()
    before = deepcopy(request)
    log = deepcopy(live.event_log)
    state = deepcopy(live.initiative)
    rng = RNGState.capture(live.rng)
    published = []
    live.event_listeners.append(published.append)

    class ForbiddenRegistry(dict):
        def __getitem__(self, key):
            pytest.fail("persistent registry read")

        def __setitem__(self, key, value):
            pytest.fail("persistent registry write")

        def get(self, *args):
            pytest.fail("persistent registry lookup")

    def forbidden(*args, **kwargs):
        pytest.fail("external state access or publication")

    monkeypatch.setattr(orch, "_REGISTRY", ForbiddenRegistry())
    for name in (
        "_get_live",
        "start_combat",
        "submit_player_intent",
        "end_combat",
        "advance_monster_turn",
    ):
        monkeypatch.setattr(orch, name, forbidden)
    import random
    import sqlite3

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(random, "randint", forbidden)
    monkeypatch.setattr(random, "random", forbidden)
    first = execute(request, loader)
    assert first.status == "accepted", first.error
    assert execute(request, loader) == first
    assert request == before
    assert live.event_log == log
    assert live.initiative == state
    assert RNGState.capture(live.rng) == rng
    assert not published
    assert not orch._REGISTRY


def test_closure_cannot_smuggle_historical_balances_into_new_write_fields():
    from dnd5e_engine.evaluation_contracts import RuleEvaluationResult
    from tests.test_stateless_closure import consume, terminal_request

    request, _, _ = terminal_request()
    result = execute(request)
    closure = result.state_delta.operations[-1]
    assert closure.xp_increments == {HERO: 50}
    assert closure.historical.residual_hp == {HERO: 40}
    assert set(type(closure).model_fields) == {
        "kind",
        "combat_id",
        "expected_ended",
        "ended",
        "reason",
        "xp_increments",
        "historical",
    }
    import json

    for old in ("residual_hp", "expended_resources", "xp_awarded", "deaths"):
        value = result.model_dump(mode="json")
        value["state_delta"]["operations"][-1][old] = {}
        with pytest.raises(ValidationError, match="extra_forbidden"):
            RuleEvaluationResult.model_validate_json(json.dumps(value))
    committed, xp = consume(request.state_snapshot, result)
    assert committed.character_states == request.state_snapshot.character_states
    assert committed.inventory_state == request.state_snapshot.inventory_state
    assert xp == 60
    with pytest.raises(AssertionError):
        consume(committed, result, xp=xp)
