"""Independent rule oracles for review defects, beyond Legacy parity."""

import asyncio
import json
import random
from copy import deepcopy

import pytest
from dnd5e_srd_data.schema.action_policy import ActionPolicy, RestrictedActionGrant
from dnd5e_srd_data.schema.common import DamagePartBlock
from dnd5e_srd_data.schema.lifecycle import EffectEndFollowUp, EffectLifecycleSpec, RepeatSaveSpec
from pydantic import BaseModel, TypeAdapter, ValidationError

from dnd5e_engine import orchestrator as orch
from dnd5e_engine.activities.apply import _apply_modifiers
from dnd5e_engine.effect_lifecycle import EffectLifecycleApplication
from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_engine.evaluation_snapshot import capture_combat_snapshot
from dnd5e_engine.evaluation_state import InventoryState
from dnd5e_engine.specs import GridScene
from dnd5e_engine.types.effects import ActiveEffect, ActiveEffectChange, ActiveEffectDuration
from tests.evaluation_support import FOE, HERO
from tests.test_npc_availability import NPC_TEMPLATE, npc_case
from tests.test_stateless_attack import apply_delta, execute, request_and_live


def recapture(request, live):
    return request.model_copy(
        update={
            "state_snapshot": capture_combat_snapshot(
                live,
                inventory_state=InventoryState(entries=()),
                grid=GridScene(width=3, height=3),
                world_version=7,
                combat_id="combat:synthetic",
            )
        }
    )


def test_resistance_rounds_down_before_vulnerability_in_both_paths():
    request, handle, live = request_and_live(seed=0, ac=1)
    live.initiative[1] = live.initiative[1].model_copy(
        update={"damage_resistances": ["slashing"], "damage_vulnerabilities": ["slashing"]}
    )
    request = recapture(request, live)
    result = execute(request)
    asyncio.run(orch.submit_player_intent(handle, HERO, request.payload))
    assert result.status == "accepted"
    # Seed 0 rolls 1d6+3 = 7. SRD: floor(7/2)*2 = 6, never 7.
    assert [e.amount for e in result.proposed_events if e.type == "damage_applied"] == [6]
    assert [e.amount for e in live.event_log if e.type == "damage_applied"] == [6]
    assert (
        next(
            a
            for a in apply_delta(request.state_snapshot, result).character_states
            if a.entity_id == FOE
        ).hp_current
        == 94
    )


def mixed_damage_case(*, hp=1, temp=0):
    request, handle, live, loader = npc_case(seed=0, hero_hp=hp, hero_max=10)
    action = loader.get_monster(NPC_TEMPLATE).actions[0]
    activity = action.activities[0]
    parts = [
        DamagePartBlock(number=1, denomination=4, bonus="4", types=[kind])
        for kind in ("slashing", "fire")
    ]
    action.activities[0] = activity.model_copy(
        update={"damage": activity.damage.model_copy(update={"parts": parts})}
    )
    live.initiative[0] = live.initiative[0].model_copy(update={"temp_hp": temp})
    live.tracked_temp_hp[HERO] = temp
    request = recapture(request, live).model_copy(
        update={"ruleset_binding": ruleset_binding(loader)}
    )
    return request, handle, live, loader


@pytest.mark.parametrize("hp,temp,dead", [(1, 0, True), (1, 2, True), (1, 3, False), (4, 0, False)])
def test_one_damage_instance_decides_massive_damage_after_all_types(hp, temp, dead):
    request, handle, live, loader = mixed_damage_case(hp=hp, temp=temp)
    before = deepcopy(request)
    result = execute(request, loader)
    assert result.status == "accepted"
    final = apply_delta(request.state_snapshot, result)
    hero = next(a for a in final.character_states if a.entity_id == HERO)
    damage = [e for e in result.proposed_events if e.type == "damage_applied"]
    assert [e.amount for e in damage] == [8, 5]
    assert len({e.damage_instance_id for e in damage}) == 1
    assert hero.hp_current == 0
    assert hero.is_alive is not dead
    assert (HERO in final.combat_state.dead_ids) is dead
    expected = random.Random(0)
    expected.randint(1, 20)
    expected.randint(1, 4)
    expected.randint(1, 4)
    if dead:
        assert [e.reason for e in result.proposed_events if e.type == "death"] == ["instant_kill"]
        assert not any(
            e.type in ("unconscious", "death_save_rolled") for e in result.proposed_events
        )
    else:
        expected.randint(1, 20)  # exactly one genuine next-turn death save
        assert sum(e.type == "death_save_rolled" for e in result.proposed_events) == 1
    assert result.rng_transition.next_state == RNGState.capture(expected)
    assert request == before
    asyncio.run(orch.submit_player_intent(handle, FOE, request.payload))
    assert tuple(live.event_log) == result.proposed_events
    assert recapture(request, live).state_snapshot == final
    assert RNGState.capture(live.rng) == result.rng_transition.next_state


def test_effect_snapshot_does_not_fill_missing_mechanics():
    request, _, _ = request_and_live()
    raw = request.model_dump(mode="json")
    raw["state_snapshot"]["effect_states"] = [
        {"id": "effect:probe", "name": "Probe", "origin": "cast:probe", "target_id": HERO}
    ]
    with pytest.raises(ValidationError):
        RuleEvaluationRequest.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize("field", ["spent_ft", "distance_ft", "dash_count"])
def test_nested_ledger_rejects_numeric_string(field):
    request, _, _ = request_and_live()
    raw = request.model_dump(mode="json")
    raw["state_snapshot"]["combat_state"]["movement_ledgers"][HERO][field] = "5"
    with pytest.raises(ValidationError):
        RuleEvaluationRequest.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize("amount,expected", [(0, 0), (1, 0), (3, 2), (7, 6), (8, 8)])
def test_combined_defenses_have_independent_srd_oracle(amount, expected):
    assert _apply_modifiers(amount, "fire", {"fire"}, set(), {"fire"}) == expected
    assert _apply_modifiers(amount, "fire", {"all"}, set(), {"all"}) == expected
    assert _apply_modifiers(amount, "fire", {"all"}, {"fire"}, {"all"}) == 0


def complete_effect():
    return effect_state(
        ActiveEffect(
            id="effect:probe",
            name="Probe",
            origin="cast:probe",
            target_id=HERO,
            duration=ActiveEffectDuration(rounds=2),
            changes=[ActiveEffectChange(key="ac.bonus", mode="add", value=2)],
            action_policy=ActionPolicy(extra_action=RestrictedActionGrant(actions=("attack",))),
            lifecycle=EffectLifecycleApplication(
                spec=EffectLifecycleSpec(
                    maximum_rounds=2,
                    repeat_save=RepeatSaveSpec(),
                    on_end=(
                        EffectEndFollowUp(
                            effect_id="follow", expiry_boundary="target_next_turn_end"
                        ),
                    ),
                ),
                source_id=FOE,
                source_kind="spell",
                source_slug="synthetic",
                activity_id="attack",
                save_ability="wis",
                save_dc=12,
            ),
            end_effects=(
                ActiveEffect(id="follow", name="Follow", origin="cast:probe", target_id=HERO),
            ),
        )
    )


def test_entire_effect_state_family_requires_explicit_fields_and_roundtrips():
    state = complete_effect()
    request, _, _ = request_and_live()
    snapshot = request.state_snapshot.model_copy(update={"effect_states": (state,)})
    request = request.model_copy(update={"state_snapshot": snapshot})
    assert RuleEvaluationRequest.model_validate_json(request.model_dump_json()) == request

    def visit(value):
        if isinstance(value, BaseModel):
            yield value
            for field in type(value).model_fields:
                yield from visit(getattr(value, field))
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from visit(item)

    for model in visit(state):
        raw = model.model_dump(mode="json")
        assert set(raw) == set(type(model).model_fields)
        schema = type(model).model_json_schema()
        definition = (
            schema["$defs"][schema["$ref"].rsplit("/", 1)[1]] if "$ref" in schema else schema
        )
        assert set(definition["required"]) == set(raw)
        adapter = TypeAdapter(type(model))
        for field in raw:
            missing = raw.copy()
            del missing[field]
            with pytest.raises(ValidationError):
                adapter.validate_json(json.dumps(missing))
        with pytest.raises(ValidationError):
            adapter.validate_json(json.dumps(raw | {"unknown": True}))


@pytest.mark.parametrize(
    "nested,field,bad",
    [
        ("duration", "rounds", "2"),
        ("change", "priority", "20"),
        ("policy", "deny_actions", "false"),
    ],
)
def test_effect_nested_values_are_strict_and_revalidated(nested, field, bad, monkeypatch):
    request, _, _ = request_and_live()
    state = complete_effect()
    target = {
        "duration": state.duration,
        "change": state.changes[0],
        "policy": state.action_policy,
    }[nested]
    target.__dict__[field] = bad
    request = request.model_copy(
        update={
            "state_snapshot": request.state_snapshot.model_copy(update={"effect_states": (state,)})
        }
    )
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("restored execution RNG"))
    with pytest.raises(ValidationError):
        execute(request)


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_fault_between_damage_types_rolls_back_private_and_legacy_state(monkeypatch, failure):
    request, handle, live, loader = mixed_damage_case()
    before = deepcopy(request)
    initial_rng = live.rng.getstate()
    observed = []
    live.event_listeners.append(observed.append)
    original = orch._emit

    def interrupted(context, event):
        original(context, event)
        if event.type == "damage_applied":
            raise failure("interrupted damage packet")

    monkeypatch.setattr(orch, "_emit", interrupted)
    with pytest.raises(failure, match="interrupted damage packet"):
        execute(request, loader)
    with pytest.raises(failure, match="interrupted damage packet"):
        asyncio.run(orch.submit_player_intent(handle, FOE, request.payload))
    assert request == before
    assert live.rng.getstate() == initial_rng
    assert not live.character_damage_instances
    assert not live.lifecycle_damage
    assert live.event_log == []
    assert observed == []
    assert recapture(request, live).state_snapshot == request.state_snapshot
