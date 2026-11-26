"""Exact read-only probability over an admitted current-schema attack."""

from copy import deepcopy

from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from pydantic_core import to_json

from dnd5e_engine import evaluation_availability
from dnd5e_engine.evaluation_availability import (
    ActionAvailabilityRequest,
    query_action_availability,
)
from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from tests.evaluation_support import HERO, WEAPON
from tests.test_stateless_attack import request_and_live


def _current_attack(*, attacker_prone=False, target_prone=False, target_ac=None):
    old, _, live = request_and_live()
    wire = old.model_dump(mode="json")
    wire["schema_version"] = "engine-evaluation/28"
    snapshot = wire["state_snapshot"]
    snapshot["snapshot_schema_version"] = "engine-snapshot/20"
    for actor in snapshot["character_states"]:
        actor.pop("spells_known")
        if (actor["entity_id"] == HERO and attacker_prone) or (
            actor["entity_id"] != HERO and target_prone
        ):
            actor["conditions"].append(
                dict(
                    condition="prone",
                    source_entity_id="implied:event",
                    scope="combat",
                    duration_rounds=None,
                    save_dc=None,
                    applied_round=0,
                    exhaustion_level=1,
                    source_effect_id=None,
                )
            )
        if actor["entity_id"] == HERO:
            actor["held_weapon_instance_id"] = "weapon:held"
        elif target_ac is not None:
            actor["ac"] = target_ac
    snapshot["inventory_state"]["entries"] = [
        dict(
            instance_id="weapon:held",
            owner_id=HERO,
            item_slug=WEAPON,
            quantity=1,
            charges_remaining_per_unit=0,
            accessible=True,
        )
    ]
    wire["payload"].pop("weapon_id")
    wire["payload"]["weapon_instance_id"] = "weapon:held"
    request = RuleEvaluationRequest.model_validate_json(to_json(wire))
    loader = live.ruleset_loader
    if attacker_prone or target_prone:
        loader = MemoryAssetLoader(
            items=[loader.get_item(WEAPON)],
            conditions=[BundledAssetLoader().get_condition("prone")],
        )
        request = request.model_copy(update={"ruleset_binding": ruleset_binding(loader)})
    return request, loader


def test_current_availability_preview_is_exact_and_opt_in():
    request, loader = _current_attack()
    query = ActionAvailabilityRequest(
        schema_version="engine-availability/17",
        session_id=request.session_id,
        operation_kind=request.operation_kind,
        actor_id=request.actor_id,
        payload=request.payload,
        state_snapshot=request.state_snapshot,
        ruleset_binding=request.ruleset_binding,
        include_hit_preview=True,
    )
    before = deepcopy(query)
    result = query_action_availability(query, loader=loader)
    assert result.status == "available"
    assert result.hit_preview.status == "known"
    assert result.hit_preview.mode == "normal"
    assert (
        result.hit_preview.probability_numerator,
        result.hit_preview.probability_denominator,
    ) == (4, 5)
    assert query == before
    hidden = query_action_availability(
        query.model_copy(update={"include_hit_preview": False}), loader=loader
    )
    assert hidden.status == "available"
    assert hidden.hit_preview.status == "unknown"
    old = query_action_availability(
        query.model_copy(update={"schema_version": "engine-availability/16"}), loader=loader
    )
    assert "hit_preview" not in old.model_dump(mode="json")


def test_prone_advantage_disadvantage_and_cancellation_use_exact_weights():
    cases = [
        (False, True, "advantage", (24, 25), ("target:Prone",), ()),
        (True, False, "disadvantage", (16, 25), (), ("attacker:Prone",)),
        (True, True, "normal", (4, 5), ("target:Prone",), ("attacker:Prone",)),
    ]
    for attacker_prone, target_prone, mode, fraction, advantage, disadvantage in cases:
        request, loader = _current_attack(attacker_prone=attacker_prone, target_prone=target_prone)
        query = ActionAvailabilityRequest(
            schema_version="engine-availability/17",
            session_id=request.session_id,
            operation_kind=request.operation_kind,
            actor_id=request.actor_id,
            payload=request.payload,
            state_snapshot=request.state_snapshot,
            ruleset_binding=request.ruleset_binding,
            include_hit_preview=True,
        )
        result = query_action_availability(query, loader=loader)
        assert result.status == "available", result.reason
        preview = result.hit_preview
        assert preview.status == "known"
        assert preview.mode == mode
        assert (preview.probability_numerator, preview.probability_denominator) == fraction
        assert preview.advantage_reasons == advantage
        assert preview.disadvantage_reasons == disadvantage


def test_natural_one_misses_and_natural_twenty_hits_even_at_extreme_ac():
    for target_ac, expected in ((1, (19, 20)), (99, (1, 20))):
        request, loader = _current_attack(target_ac=target_ac)
        query = ActionAvailabilityRequest(
            schema_version="engine-availability/17",
            session_id=request.session_id,
            operation_kind=request.operation_kind,
            actor_id=request.actor_id,
            payload=request.payload,
            state_snapshot=request.state_snapshot,
            ruleset_binding=request.ruleset_binding,
            include_hit_preview=True,
        )
        result = query_action_availability(query, loader=loader)
        assert result.status == "available"
        assert (
            result.hit_preview.probability_numerator,
            result.hit_preview.probability_denominator,
        ) == expected


def test_early_capability_failure_still_returns_unknown_preview(monkeypatch):
    request, loader = _current_attack()
    query = ActionAvailabilityRequest(
        schema_version="engine-availability/17",
        session_id=request.session_id,
        operation_kind=request.operation_kind,
        actor_id=request.actor_id,
        payload=request.payload,
        state_snapshot=request.state_snapshot,
        ruleset_binding=request.ruleset_binding,
        include_hit_preview=True,
    )
    monkeypatch.setattr(
        evaluation_availability, "snapshot_equipment_kind_failure", lambda *_: "blocked"
    )
    result = query_action_availability(query, loader=loader)
    assert result.status == "unknown"
    assert result.hit_preview.status == "unknown"
