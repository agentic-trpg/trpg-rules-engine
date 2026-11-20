"""Exercise spell hit, speed, and expiry through the public stateless wire."""

from copy import deepcopy
from json import dumps
from random import Random

import pytest
from pydantic import ValidationError

from dnd5e_engine.evaluation_contracts import (
    CombatIntentPayload,
    RuleEvaluationRequest,
    RuleEvaluationResult,
)
from dnd5e_engine.evaluation_delta import CombatEffectCreate, EffectLifecycleUpdate
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import get_default_ruleset
from dnd5e_engine.evaluation_speed_effects import spell_attack_has_speed_effect
from dnd5e_engine.evaluation_spells import ordinary_spell_mechanism
from dnd5e_engine.events import (
    ActorMoved,
    AttackRolled,
    DamageApplied,
    Death,
    EffectApplied,
    EffectExpired,
)
from dnd5e_engine.lib_loader import scoped_lib_loader
from dnd5e_engine.live_movement import effective_speed
from tests.c20_support import act, combatant, events, pc, start
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import apply_delta, execute
from tests.test_stateless_spell_attack import spell_attack_wire


def cast_request(*, seed=0, hit_points=40, immune=False):
    wire = spell_attack_wire("ray-of-frost", seed=seed)
    snapshot = wire["state_snapshot"]
    snapshot["combat_state"]["opportunity_attack_weapons"] = {}
    snapshot["casting_state"]["actors"][0]["sources"][0]["spells"].append(
        {"spell_id": "ray-of-frost", "prepared_order": None, "material": None}
    )
    target = snapshot["character_states"][1]
    target["hp_current"] = hit_points
    target["damage_immunities"] = ["cold"] if immune else []
    return RuleEvaluationRequest.model_validate_json(dumps(wire))


def adopt(snapshot, result):
    """Apply the two effect operations and the established test consumer to a fixed input."""
    effect_ops = (CombatEffectCreate, EffectLifecycleUpdate)
    ordinary = result.state_delta.model_copy(
        update={
            "operations": tuple(
                op for op in result.state_delta.operations if not isinstance(op, effect_ops)
            )
        }
    )
    adopted = apply_delta(snapshot, result.model_copy(update={"state_delta": ordinary}))
    for op in result.state_delta.operations:
        if isinstance(op, CombatEffectCreate):
            assert not adopted.effect_states
            effect, clock, lineage = op.effect, op.lifecycle, op.lineage
        elif isinstance(op, EffectLifecycleUpdate):
            assert adopted.effect_states == (op.expected_effect,)
            assert adopted.combat_state.effect_lifecycles == (op.expected_lifecycle,)
            assert adopted.combat_state.conditions_by_effect == (op.expected_lineage,)
            effect, clock, lineage = op.effect, op.lifecycle, op.lineage
        else:
            continue
        adopted = adopted.model_copy(
            update={
                "effect_states": (effect,) if effect is not None else (),
                "combat_state": adopted.combat_state.model_copy(
                    update={
                        "effect_lifecycles": (clock,) if clock is not None else (),
                        "conditions_by_effect": (lineage,) if lineage is not None else (),
                    }
                ),
            }
        )
    return adopted


def next_request(request, snapshot, result, intent, **fields):
    """Feed only the adopted Delta and RNG successor into another public evaluation."""
    state = snapshot.combat_state
    return request.model_copy(
        update={
            "command_id": request.command_id + ":" + intent,
            "actor_id": state.initiative_ids[state.current_turn_index],
            "payload": CombatIntentPayload(intent_type=intent, **fields),
            "state_snapshot": snapshot,
            "rng_context": request.rng_context.model_copy(
                update={"state": result.rng_transition.next_state}
            ),
        }
    )


@pytest.mark.parametrize(
    "seed, hit, immune", [(0, True, False), (31, False, False), (0, True, True)]
)
def test_cast_hit_creates_one_initial_graph_and_miss_creates_none(seed, hit, immune):
    loader = get_default_ruleset()
    request = cast_request(seed=seed, immune=immune)
    before = deepcopy(request)
    assert ordinary_spell_mechanism(loader, "ray-of-frost") == "attack_damage"
    assert spell_attack_has_speed_effect(loader, "ray-of-frost")
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert result == execute(request, loader)
    assert result == RuleEvaluationResult.model_validate_json(result.model_dump_json())
    assert request == before
    attack = next(event for event in result.proposed_events if isinstance(event, AttackRolled))
    assert attack.is_hit is hit
    creations = [op for op in result.state_delta.operations if isinstance(op, CombatEffectCreate)]
    damages = [event for event in result.proposed_events if isinstance(event, DamageApplied)]
    applications = [event for event in result.proposed_events if isinstance(event, EffectApplied)]
    reference = Random(seed)
    reference.randint(1, 20)
    if hit:
        expected = reference.randint(1, 8)
        (damage,) = damages
        assert (damage.amount, damage.damage_type) == (0 if immune else expected, "cold")
        (creation,) = creations
        (application,) = applications
        assert creation.expected_absent
        assert creation.identity[0] == FOE
        assert creation.lineage.statuses == []
        assert (
            creation.lifecycle.state.applied_turn_serial
            == before.state_snapshot.combat_state.turn_serial
        )
        assert creation.lifecycle.state.expiry_actor_id == HERO
        assert result.proposed_events.index(attack) < result.proposed_events.index(damage)
        assert result.proposed_events.index(damage) < result.proposed_events.index(application)
        assert not any(isinstance(event, EffectExpired) for event in result.proposed_events)
    else:
        assert not creations
        assert not damages
        assert not applications
    assert result.rng_transition.next_state == RNGState.capture(reference)
    snapshot = adopt(request.state_snapshot, result)
    target = snapshot.character_states[1]
    assert target.movement_remaining == target.base_speed - (10 if hit else 0)
    assert snapshot.resource_state == before.state_snapshot.resource_state


def test_movement_uses_reduced_budget_and_source_start_restores_unspent_speed():
    loader = get_default_ruleset()
    request = cast_request()
    cast = execute(request, loader)
    assert cast.status == "accepted", cast.error
    slowed = adopt(request.state_snapshot, cast)
    target = slowed.character_states[1]
    assert target.entity_id == FOE
    assert target.base_speed == 30
    assert target.movement_remaining == 20
    move_too_far = next_request(request, slowed, cast, "move", target_zone_id="8,0")
    refused = execute(move_too_far, loader)
    assert refused.status == "rejected"
    assert refused.error.code == "movement.insufficient"
    assert refused.rng_transition is None
    assert refused.proposed_events == ()
    move = next_request(request, slowed, cast, "move", target_zone_id="6,0")
    moved = execute(move, loader)
    assert moved.status == "accepted", moved.error
    (movement,) = [event for event in moved.proposed_events if isinstance(event, ActorMoved)]
    assert movement.movement_cost_ft == 15
    advanced = adopt(slowed, moved)
    assert advanced.character_states[1].movement_remaining == 5
    assert advanced.effect_states == slowed.effect_states
    assert advanced.combat_state.effect_lifecycles == slowed.combat_state.effect_lifecycles
    ending = next_request(move, advanced, moved, "pass")
    passed = execute(ending, loader)
    assert passed.status == "accepted", passed.error
    assert [event.type for event in passed.proposed_events] == [
        "intent_submitted",
        "turn_phase",
        "turn_ended",
        "round_started",
        "turn_phase",
        "turn_started",
        "turn_phase",
        "effect_expired",
    ]
    assert passed.proposed_events[5].actor_id == HERO
    assert passed.proposed_events[6].phase == "turn_start"
    assert passed.proposed_events[7].reason == "duration"
    removed = adopt(advanced, passed)
    assert removed.effect_states == ()
    assert removed.combat_state.effect_lifecycles == ()
    assert removed.combat_state.conditions_by_effect == ()
    assert removed.combat_state.movement_ledgers[FOE].spent_ft == 15
    assert removed.character_states[1].movement_remaining == 15
    assert passed.rng_transition.next_state == moved.rng_transition.next_state


def test_same_command_application_and_expiry_is_unsupported():
    request = cast_request(hit_points=1)
    before = deepcopy(request)
    control = request.model_copy(
        update={"payload": request.payload.model_copy(update={"spell_id": "fire-bolt"})}
    )
    ordinary = execute(control, get_default_ruleset())
    assert ordinary.status == "accepted", ordinary.error
    assert (
        next(event for event in ordinary.proposed_events if event.type == "turn_started").actor_id
        == HERO
    )
    result = execute(request, get_default_ruleset())
    assert result.status == "unsupported"
    assert result.error.code == "spell.effect_consequence"
    assert result.state_delta is None
    assert result.rng_transition is None
    assert result.proposed_events == ()
    assert request == before


@pytest.mark.parametrize("seed, slowed", [(6, True), (31, False)])
def test_opposing_wizard_cast_requires_a_plain_snapshot(seed, slowed):
    loader = get_default_ruleset()
    wire = cast_request(seed=seed).model_dump(mode="json")
    snapshot = wire["state_snapshot"]
    source, target = snapshot["character_states"]
    target.update({**source, "entity_id": FOE})
    source["hp_current"] = 1
    casters = snapshot["casting_state"]["actors"]
    casters.append({**casters[0], "actor_id": FOE})
    pools = snapshot["resource_state"]["pools"]
    pools.extend([{**pool, "owner_id": FOE} for pool in pools])
    snapshot["combat_state"].update(
        party_ids=[FOE], encounter_ids=[HERO], xp_value_by_entity={HERO: 0}
    )
    initial = RuleEvaluationRequest.model_validate_json(dumps(wire))
    ray = execute(initial, loader)
    assert ray.status == "accepted", ray.error
    current = adopt(initial.state_snapshot, ray)
    assert bool(current.effect_states) is slowed
    fire_bolt = next_request(
        initial,
        current,
        ray,
        "cast_spell",
        spell_id="fire-bolt",
        target_id=HERO,
        spell_source_id="class:wizard",
    )
    assert fire_bolt.actor_id == FOE
    before = deepcopy(fire_bolt)
    result = execute(fire_bolt, loader)
    assert fire_bolt == before
    if not slowed:
        assert result.status == "accepted", result.error
        assert any(
            isinstance(event, Death) and event.target_id == HERO for event in result.proposed_events
        )
        return
    assert result.status == "unsupported"
    assert result.error.code == "spell.attack_dependencies"
    assert result.state_delta is None
    assert result.proposed_events == ()
    assert result.rng_transition is None


def test_lethal_hit_keeps_the_effect_until_source_start_when_another_actor_is_alive():
    request = cast_request(hit_points=1)
    wire = request.model_dump(mode="json")
    snapshot = wire["state_snapshot"]
    second = "npc:second"
    target = snapshot["character_states"][1]
    snapshot["character_states"].append({**target, "entity_id": second, "hp_current": 40})
    state = snapshot["combat_state"]
    state["initiative_ids"].append(second)
    state["encounter_ids"].append(second)
    state["actor_zone"][second] = "4,0"
    state["movement_ledgers"][second] = state["movement_ledgers"][FOE]
    state["xp_value_by_entity"][second] = state["xp_value_by_entity"][FOE]
    request = RuleEvaluationRequest.model_validate_json(dumps(wire))
    loader = get_default_ruleset()
    cast = execute(request, loader)
    assert cast.status == "accepted", cast.error
    slain = adopt(request.state_snapshot, cast)
    assert slain.combat_state.dead_ids == {FOE}
    assert slain.effect_states[0].target_id == FOE
    after = next_request(request, slain, cast, "pass")
    assert after.actor_id == second
    ended = execute(after, loader)
    assert ended.status == "accepted", ended.error
    assert not adopt(slain, ended).effect_states


def test_imported_speed_effect_rejects_a_dead_source():
    loader = get_default_ruleset()
    initial = cast_request()
    cast = execute(initial, loader)
    assert cast.status == "accepted", cast.error
    slowed = adopt(initial.state_snapshot, cast)
    wire = next_request(initial, slowed, cast, "pass").model_dump(mode="json")
    snapshot = wire["state_snapshot"]
    snapshot["character_states"][0].update(hp_current=0, is_alive=False)
    state = snapshot["combat_state"]
    state["dead_ids"] = [HERO]
    state["deaths_recorded"] = [
        {
            "target_id": HERO,
            "target_kind": "character",
            "location_id": snapshot["scene_state"]["scene_id"],
            "reason": "damage",
            "killer_id": FOE,
        }
    ]
    result = execute(RuleEvaluationRequest.model_validate_json(dumps(wire)), loader)
    assert result.status == "unsupported"
    assert result.error.reason == "speed effect source has no future initiative turn"
    assert result.state_delta is None
    assert result.proposed_events == ()
    assert result.rng_transition is None


def test_speed_graph_forgery_and_old_creation_versions_fail_closed():
    loader = get_default_ruleset()
    request = cast_request()
    result = execute(request, loader)
    wire = result.model_dump(mode="json")
    assert result.status == "accepted", result.error
    wire["schema_version"] = "engine-evaluation/29"
    with pytest.raises(ValidationError, match="creation requires evaluation/30"):
        RuleEvaluationResult.model_validate_json(dumps(wire))
    hit = next(event for event in result.proposed_events if isinstance(event, AttackRolled))
    wrong_rolls = ({"is_hit": False}, {"is_opportunity_attack": True})
    for wrong_roll in wrong_rolls:
        forged = result.model_copy(
            update={
                "proposed_events": tuple(
                    event.model_copy(update=wrong_roll) if event is hit else event
                    for event in result.proposed_events
                )
            }
        )
        with pytest.raises(ValueError, match="matching attack hit"):
            forged.verify_request(request)
    missing_damage = result.model_copy(
        update={
            "proposed_events": tuple(
                event for event in result.proposed_events if not isinstance(event, DamageApplied)
            )
        }
    )
    with pytest.raises(ValueError, match="matching attack hit, damage and effect"):
        missing_damage.verify_request(request)
    slowed = adopt(request.state_snapshot, result)
    rerouted = slowed.model_copy(
        update={"combat_state": slowed.combat_state.model_copy(update={"conditions_by_effect": ()})}
    )
    rejected = execute(next_request(request, rerouted, result, "pass"), loader)
    assert rejected.status == "unsupported"
    assert rejected.error.code == "turn.capability"
    assert rejected.rng_transition is None
    assert rejected.proposed_events == ()


def test_serialized_speed_clock_requires_a_reachable_application_turn():
    loader = get_default_ruleset()
    wire = cast_request().model_dump(mode="json")
    wire["state_snapshot"]["combat_state"].update(round_number=8, turn_serial=41)
    request = RuleEvaluationRequest.model_validate_json(dumps(wire))
    cast = execute(request, loader)
    assert cast.status == "accepted", cast.error
    slowed = adopt(request.state_snapshot, cast)
    assert slowed.combat_state.turn_serial == 42
    assert slowed.combat_state.effect_lifecycles[0].state.applied_turn_serial == 41
    ending = next_request(request, slowed, cast, "pass")
    assert execute(ending, loader).status == "accepted"

    forged = ending.model_dump(mode="json")
    clock = forged["state_snapshot"]["combat_state"]["effect_lifecycles"][0]["state"]
    clock["applied_turn_serial"] = 0
    result = execute(RuleEvaluationRequest.model_validate_json(dumps(forged)), loader)
    assert result.status == "unsupported"
    assert "reachable initiative slots" in result.error.reason
    assert result.state_delta is None
    assert result.proposed_events == ()
    assert result.rng_transition is None


def test_existing_legacy_cast_uses_the_same_reduction_and_exact_expiry():
    with scoped_lib_loader(get_default_ruleset()):
        handle, live = start(
            [pc(class_slug="wizard", intelligence=16, spells_known=["ray-of-frost"])], seed=0
        )
        caster = combatant(live)
        live.rng.seed(0)
        act(
            handle,
            caster.entity_id,
            intent_type="cast_spell",
            spell_id="ray-of-frost",
            target_id="mon:foe",
        )
        (application,) = events(live, EffectApplied)
        target = combatant(live, "mon:foe")
        assert effective_speed(target, "walk", live) == target.base_speed - 10
        assert events(live, EffectExpired) == []
        act(handle, target.entity_id, intent_type="pass")
        (expiry,) = events(live, EffectExpired)
        assert expiry.effect_id == application.effect.id
        assert effective_speed(target, "walk", live) == target.base_speed
