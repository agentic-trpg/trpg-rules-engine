"""Ordinary held-weapon attacks with world Longstrider or Hold Person."""

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd5e_srd_data.schema.spell import Spell
from pydantic_core import to_json

from dnd5e_engine.evaluation_availability import (
    ActionAvailabilityRequest,
    query_action_availability,
)
from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest
from dnd5e_engine.evaluation_delta import TurnUpdate, WorldEffectCreate
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_engine.events import AttackRolled
from tests.evaluation_support import FOE, HERO, WEAPON, synthetic_loader
from tests.test_stateless_attack import apply_delta, execute, request_and_live
from tests.test_stateless_attack_concentration import ATTACKER, attack_concentration_case
from tests.test_stateless_timed_spells import spell_wire


def _instance_wire(request, loader, *, target_id):
    wire = request.model_dump(mode="json")
    wire["schema_version"] = "engine-evaluation/30"
    wire["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    snapshot = wire["state_snapshot"]
    snapshot["snapshot_schema_version"] = "engine-snapshot/20"
    for actor in snapshot["character_states"]:
        actor.pop("spells_known")
        if actor["weapon_in_hands"]:
            owner = actor["entity_id"]
            held_id = "weapon:held" if owner == request.actor_id else f"weapon:{owner}"
            actor["held_weapon_instance_id"] = held_id
            snapshot["inventory_state"]["entries"].append(
                {
                    "instance_id": held_id,
                    "owner_id": owner,
                    "item_slug": actor["weapon_in_hands"],
                    "quantity": 1,
                    "charges_remaining_per_unit": 0,
                    "accessible": True,
                }
            )
    casting = snapshot["casting_state"]
    for caster in casting["actors"] if casting else ():
        assert caster.pop("materials") == []
        for source in caster["sources"]:
            prepared = {slug: order for order, slug in enumerate(source.pop("prepared_spells"))}
            source["spells"] = [
                {"spell_id": slug, "prepared_order": prepared.get(slug), "material": None}
                for slug in source.pop("known_spells")
            ]
    del wire["payload"]["weapon_id"]
    wire["payload"].update(weapon_instance_id="weapon:held", target_id=target_id)
    return wire


def _availability(request, loader):
    return query_action_availability(
        ActionAvailabilityRequest(
            schema_version="engine-availability/16",
            session_id=request.session_id,
            operation_kind="combat.intent",
            actor_id=request.actor_id,
            payload=request.payload,
            state_snapshot=request.state_snapshot,
            ruleset_binding=request.ruleset_binding,
        ),
        loader=loader,
    )


def _world_effect_wire(target_id, *, skill_effect_name=None):
    base, _, _ = request_and_live()
    spell = BundledAssetLoader().get_spell("longstrider")
    if skill_effect_name:
        data = spell.model_dump(mode="json")
        data.update(slug="fixture-keen-perception", name="Keen Perception", foundry_uuid="")
        data["passive_effects"][0].update(
            name=skill_effect_name,
            changes=[{"key": "check.skill.perception.bonus", "mode": 2, "value": "+2"}],
        )
        spell = Spell.model_validate(data)
    loader = MemoryAssetLoader(
        items=[synthetic_loader().get_weapon(WEAPON)],
        spells=[spell],
    )
    wire = _instance_wire(base, loader, target_id=FOE)
    cast, cast_loader = spell_wire(spell, target_id=target_id)
    result = execute(RuleEvaluationRequest.model_validate_json(to_json(cast)), cast_loader)
    assert result.status == "accepted", result.error
    effect = next(
        op.value.effect for op in result.state_delta.operations if isinstance(op, WorldEffectCreate)
    )
    return wire, effect.model_dump(mode="json"), loader


@pytest.mark.parametrize("target_id", [HERO, FOE])
@pytest.mark.parametrize("name", [None, "A Keen Eye", "Imperceptible Barrier"])
def test_world_passive_spell_keeps_unrelated_attack_delta_events_and_rng(target_id, name):
    wire, effect, loader = _world_effect_wire(target_id, skill_effect_name=name)
    plain = RuleEvaluationRequest.model_validate_json(to_json(wire))
    if name:
        assert effect["origin"] == f"cast:{name.lower().replace(' ', '_')}:{HERO}"
    wire["state_snapshot"]["effect_states"] = [effect]
    buffed = RuleEvaluationRequest.model_validate_json(to_json(wire))
    fixed = buffed.model_dump_json()
    normal, result = execute(plain, loader), execute(buffed, loader)

    assert (normal.status, result.status) == ("accepted", "accepted"), result.error
    assert _availability(buffed, loader).status == "available"
    assert buffed.model_dump_json() == fixed
    assert result.proposed_events == normal.proposed_events
    assert result.rng_transition == normal.rng_transition
    assert result.stage_facts.coverage == "uncovered"
    assert result.state_delta == normal.state_delta
    assert not any(isinstance(op, TurnUpdate) for op in result.state_delta.operations)


def test_world_longstrider_makes_dodge_work_when_existing_walk_speed_is_zero():
    wire, effect, loader = _world_effect_wire(FOE)
    foe = next(a for a in wire["state_snapshot"]["character_states"] if a["entity_id"] == FOE)
    foe.update(base_speed=0, movement_remaining=0, dodging=True)
    plain = RuleEvaluationRequest.model_validate_json(to_json(wire))
    wire["state_snapshot"]["effect_states"] = [effect]
    buffed = RuleEvaluationRequest.model_validate_json(to_json(wire))

    normal, result = execute(plain, loader), execute(buffed, loader)
    assert (normal.status, result.status) == ("accepted", "accepted")
    assert _availability(buffed, loader).status == "available"
    assert _attack_roll(normal).advantage == "normal"
    assert _attack_roll(result).advantage == "disadvantage"
    assert normal.rng_transition != result.rng_transition


def _attack_roll(result):
    return next(event for event in result.proposed_events if isinstance(event, AttackRolled))


def _hold_wire():
    original, _, _, loader = attack_concentration_case(pc=True)
    wire = _instance_wire(original, loader, target_id=HERO)
    combat = wire["state_snapshot"]["combat_state"]
    combat.update(party_ids=[ATTACKER, FOE], encounter_ids=[HERO], xp_value_by_entity={HERO: 0})
    return wire, loader


def test_hold_person_non_caster_attack_applies_advantage_and_close_critical():
    wire, loader = _hold_wire()
    held = RuleEvaluationRequest.model_validate_json(to_json(wire))
    fixed = held.model_dump_json()
    wire["state_snapshot"]["effect_states"] = []
    state = wire["state_snapshot"]["combat_state"]
    for name in ("concentration_chain", "concentration_rounds_remaining"):
        state[name] = {}
    for name in ("conditions_by_effect", "effect_lifecycles"):
        state[name] = []
    for actor in wire["state_snapshot"]["character_states"]:
        actor.update(conditions=[], concentration_effect_id=None, movement_remaining=30)
    plain = RuleEvaluationRequest.model_validate_json(to_json(wire))

    normal, result = execute(plain, loader), execute(held, loader)
    assert (normal.status, result.status) == ("accepted", "accepted"), (normal.error, result.error)
    assert _availability(held, loader).status == "available"
    assert held.actor_id == ATTACKER
    assert held.model_dump_json() == fixed
    normal_roll, held_roll = _attack_roll(normal), _attack_roll(result)
    assert not normal_roll.is_crit
    assert normal_roll.advantage == "normal"
    assert normal_roll.natural == 13
    assert held_roll.is_crit
    assert held_roll.advantage == "advantage"
    assert held_roll.natural == 14
    assert normal_roll.is_hit
    assert held_roll.is_hit
    assert normal.rng_transition != result.rng_transition
    prior = held.state_snapshot
    after = apply_delta(prior, result)
    target = next(actor for actor in after.character_states if actor.entity_id == HERO)
    assert target.movement_remaining == 0
    assert after.combat_state.current_turn_index == prior.combat_state.current_turn_index
    assert after.combat_state.turn_serial == prior.combat_state.turn_serial
    assert after.combat_state.last_ended_turn == prior.combat_state.last_ended_turn
    assert after.combat_state.effect_lifecycles == prior.combat_state.effect_lifecycles
    assert after.combat_state.concentration_chain == prior.combat_state.concentration_chain
    assert after.effect_states == prior.effect_states


@pytest.mark.parametrize("armor_class, status", [(10, "unsupported"), (40, "accepted")])
def test_hold_target_at_one_hp_refuses_only_the_zero_hp_consequence(armor_class, status):
    wire, loader = _hold_wire()
    target = next(a for a in wire["state_snapshot"]["character_states"] if a["entity_id"] == HERO)
    target.update(hp_current=1, ac=armor_class)
    request = RuleEvaluationRequest.model_validate_json(to_json(wire))
    fixed = request.model_dump_json()
    assert _availability(request, loader).status == "available"

    result = execute(request, loader)
    assert result.status == status, result.error
    assert request.model_dump_json() == fixed
    if armor_class == 10:
        assert result.error.code == "attack.effect_consequence"
        assert result.state_delta is result.rng_transition is result.stage_facts is None
        assert result.proposed_events == ()
    else:
        assert not _attack_roll(result).is_hit
        assert result.rng_transition.state_changed


@pytest.mark.parametrize(
    "profile",
    ["walk-formula", "attack-bonus", "round-timer", "reaction", "hold-clock", "hold-caster-turn"],
)
def test_malformed_active_effect_refuses_before_rng_or_payment(profile, monkeypatch):
    if profile.startswith("hold-"):
        wire, loader = _hold_wire()
        state = wire["state_snapshot"]["combat_state"]
        if profile == "hold-clock":
            state["effect_lifecycles"] = []
        else:
            wire["actor_id"] = FOE
            state["current_turn_index"] = state["initiative_ids"].index(FOE)
    else:
        wire, effect, loader = _world_effect_wire(FOE)
        if profile == "walk-formula":
            effect["changes"][0]["value"] = "@level"
        elif profile == "attack-bonus":
            effect["changes"][0]["key"] = "system.bonuses.mwak.attack"
        elif profile == "round-timer":
            effect["duration"]["rounds"] = 1
        else:
            wire["state_snapshot"]["combat_state"]["pending_reactions"] = [
                {
                    "source_kind": "opportunity_attack",
                    "owner_id": HERO,
                    "mover_id": FOE,
                    "weapon_id": WEAPON,
                    "two_handed": False,
                    "trigger": "voluntary_leave_reach",
                    "expiry": "owner_next_turn_start",
                }
            ]
        wire["state_snapshot"]["effect_states"] = [effect]
    request = RuleEvaluationRequest.model_validate_json(to_json(wire))

    monkeypatch.setattr(RNGState, "restore", lambda *args: pytest.fail("refusal restored RNG"))
    availability, result = _availability(request, loader), execute(request, loader)
    assert (availability.status, result.status) == ("unknown", "unsupported")
    assert availability.reason == result.error
    if profile == "hold-caster-turn":
        assert "source turn-end" in result.error.reason
    assert result.state_delta is result.rng_transition is result.stage_facts is None
    assert result.proposed_events == ()
