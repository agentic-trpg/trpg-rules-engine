"""Evaluate reviewed spell attacks through the current stateless combat wire."""

from copy import deepcopy
from json import dumps
from random import Random

import pytest

from dnd5e_engine.evaluation_availability import (
    ActionAvailabilityRequest,
    query_action_availability,
)
from dnd5e_engine.evaluation_contracts import SCHEMA_VERSION, RuleEvaluationRequest
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import get_default_ruleset, ruleset_binding
from dnd5e_engine.evaluation_spells import ordinary_spell_mechanism
from dnd5e_engine.events import AttackRolled, DamageApplied, SpellCast
from dnd5e_engine.lib_loader import scoped_lib_loader
from dnd5e_engine.orchestrator import _hostile_adjacent_to_attacker
from tests.c20_support import act, combatant, events, pc, start
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import apply_delta, execute, request_and_live

SPELLS = (("fire-bolt", "fire", 10, 120), ("poison-spray", "poison", 12, 30))


def spell_attack_wire(spell_id, *, seed=0, distance=15, dodge=False, immune=False):
    """Build a fixed Wizard 1 turn with canonical sources and an explicit target."""
    initial, _, _ = request_and_live(seed=seed, ac=15, hp=40)
    wire = initial.model_dump(mode="json")
    wire.update(
        schema_version=SCHEMA_VERSION,
        ruleset_binding=ruleset_binding(get_default_ruleset()).model_dump(mode="json"),
        payload={
            "intent_type": "cast_spell",
            "spell_id": spell_id,
            "target_id": FOE,
            "spell_source_id": "class:wizard",
        },
    )
    snapshot = wire["state_snapshot"]
    snapshot["snapshot_schema_version"] = "engine-snapshot/20"
    for actor in snapshot["character_states"]:
        actor.pop("spells_known")
    caster, target = snapshot["character_states"]
    caster.update(
        class_slug="wizard",
        classes={"wizard": 1},
        character_level=1,
        granted_features=["spellcasting", "ritual-adept", "arcane-recovery"],
        intelligence=16,
        spellcasting_ability="int",
        attack_bonus=None,
        save_proficiencies=["int", "wis"],
        carried_item_slugs=[],
        weapon_in_hands=None,
        weapon_grip="none",
        custom_counters={"feature_use:arcane-recovery": {"spent": 0}},
        spell_slots={"1": 0},
    )
    target.update(dodging=dodge, damage_immunities=["poison"] if immune else [])
    snapshot["combat_state"]["actor_zone"][FOE] = f"{distance // 5},0"
    snapshot["scene_state"]["grid"]["width"] = 26
    snapshot["casting_state"] = {
        "actors": [
            {
                "actor_id": HERO,
                "verbal_available": True,
                "sources": [
                    {
                        "source_id": "class:wizard",
                        "class_slug": "wizard",
                        "ability": "int",
                        "spells": [
                            {"spell_id": slug, "prepared_order": None, "material": None}
                            for slug, _, _, _ in SPELLS
                        ],
                    }
                ],
            }
        ]
    }
    snapshot["resource_state"] = {
        "pools": [
            {
                "owner_id": HERO,
                "kind": "spell_slot",
                "pool_id": "spell:1",
                "level": 1,
                "maximum": 2,
                "current": 0,
            },
            {
                "owner_id": HERO,
                "kind": "feature_uses",
                "pool_id": "arcane-recovery",
                "feature_slug": "arcane-recovery",
                "maximum": 1,
                "current": 1,
            },
        ]
    }
    return wire


@pytest.mark.parametrize("spell_id, damage_type, die, feet", SPELLS)
@pytest.mark.parametrize(
    "seed,natural,hit,crit", [(0, 13, True, False), (31, 1, False, False), (5, 20, True, True)]
)
def test_spell_attack_rng_damage_and_turn(
    spell_id, damage_type, die, feet, seed, natural, hit, crit
):
    loader = get_default_ruleset()
    request = RuleEvaluationRequest.model_validate_json(
        dumps(spell_attack_wire(spell_id, seed=seed, distance=feet))
    )
    before = deepcopy(request)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert execute(request, loader) == result
    assert request == before
    assert result.input_ruleset_binding.evaluator_version == "dnd5e-evaluation/54"
    events = result.proposed_events
    assert [e.type for e in events] == [
        "intent_submitted",
        "spell_cast",
        "attack_rolled",
        *(["damage_applied"] if hit else []),
        "turn_phase",
        "turn_ended",
        "turn_started",
        "turn_phase",
    ]
    cast, roll = events[1:3]
    assert isinstance(cast, SpellCast)
    assert cast.slot_level is None
    assert cast.spell_id == spell_id
    assert isinstance(roll, AttackRolled)
    assert (roll.attacker_id, roll.target_id, roll.natural, roll.modifier) == (
        HERO,
        FOE,
        natural,
        5,
    )
    assert (roll.is_hit, roll.is_crit, roll.advantage) == (hit, crit, "normal")
    reference = Random(seed)
    assert reference.randint(1, 20) == natural
    amount = 0
    if hit:
        amount = sum(reference.randint(1, die) for _ in range(2 if crit else 1))
        damage = events[3]
        assert isinstance(damage, DamageApplied)
        assert (damage.amount, damage.damage_type, damage.is_crit) == (amount, damage_type, crit)
        assert damage.source_id == f"spell:{spell_id}:{loader.get_spell(spell_id).activities[0].id}"
        assert damage.target_id == FOE
        assert damage.source_actor_id == HERO
        assert damage.source_parent_id is None
        assert damage.damage_instance_id is not None
    assert result.rng_transition.next_state == RNGState.capture(reference)
    after = apply_delta(request.state_snapshot, result)
    assert after.resource_state == request.state_snapshot.resource_state
    assert after.casting_state == request.state_snapshot.casting_state
    caster, target = after.character_states
    assert target.hp_current == 40 - amount
    assert caster.action_taken_this_turn
    assert not caster.action_available
    assert after.combat_state.initiative_ids[after.combat_state.current_turn_index] == FOE


def test_poison_immunity_emits_zero_damage_with_only_normal_hit_draws():
    request = RuleEvaluationRequest.model_validate_json(
        dumps(spell_attack_wire("poison-spray", immune=True))
    )
    result = execute(request, get_default_ruleset())
    assert result.status == "accepted", result.error
    attack = next(e for e in result.proposed_events if isinstance(e, AttackRolled))
    damage = next(e for e in result.proposed_events if isinstance(e, DamageApplied))
    assert attack.is_hit
    assert damage.amount == 0
    assert damage.damage_type == "poison"
    assert apply_delta(request.state_snapshot, result).character_states[1].hp_current == 40
    reference = Random(0)
    reference.randint(1, 20)
    reference.randint(1, 12)
    assert result.rng_transition.next_state == RNGState.capture(reference)


@pytest.mark.parametrize(
    "distance,dodge,sources",
    [
        (5, False, ["ranged_in_melee"]),
        (15, True, ["dodge"]),
        (5, True, ["ranged_in_melee", "dodge"]),
    ],
)
def test_spell_attack_uses_adjacent_hostile_and_dodge(distance, dodge, sources):
    request = RuleEvaluationRequest.model_validate_json(
        dumps(spell_attack_wire("fire-bolt", distance=distance, dodge=dodge))
    )
    result = execute(request, get_default_ruleset())
    assert result.status == "accepted", result.error
    roll = next(e for e in result.proposed_events if isinstance(e, AttackRolled))
    assert roll.advantage == "disadvantage"
    assert set(roll.disadvantage_sources) == set(sources)
    reference = Random(0)
    assert roll.natural == min(reference.randint(1, 20), reference.randint(1, 20))
    assert roll.is_hit
    reference.randint(1, 10)
    assert result.rng_transition.next_state == RNGState.capture(reference)


def test_legacy_spell_attack_retains_its_existing_adjacent_roll():
    """The Legacy PC cast still supplies weapon geometry to the shared resolver."""
    with scoped_lib_loader(get_default_ruleset()):
        handle, live = start(
            [pc(class_slug="wizard", intelligence=16, spells_known=["fire-bolt"])], seed=0
        )
        caster = combatant(live)
        assert _hostile_adjacent_to_attacker(live, caster)
        live.rng.seed(0)
        act(
            handle,
            caster.entity_id,
            intent_type="cast_spell",
            spell_id="fire-bolt",
            target_id="mon:foe",
        )
    (roll,) = events(live, AttackRolled)
    assert roll.advantage == "normal"
    assert "ranged_in_melee" not in roll.disadvantage_sources
    reference = Random(0)
    assert roll.natural == reference.randint(1, 20)
    reference.randint(1, 10)
    assert RNGState.capture(live.rng) == RNGState.capture(reference)


@pytest.mark.parametrize(
    "mutation,code",
    [
        (lambda w: w["payload"].update(target_id=HERO), "target_invalid"),
        (lambda w: w["payload"].update(target_id="object:chair"), "target_invalid"),
        (lambda w: w["payload"].update(target_object_id="object:chair"), "spell.inputs"),
        (lambda w: w["payload"].update(target_ids=[FOE, HERO]), "spell.inputs"),
        (lambda w: w["payload"].update(spell_source_id="class:cleric"), "spell.source"),
        (lambda w: w["payload"].update(slot_level=1), "no_slot"),
        (
            lambda w: w["state_snapshot"]["casting_state"]["actors"][0].update(
                verbal_available=False
            ),
            "spell.source",
        ),
        (
            lambda w: w["state_snapshot"]["casting_state"]["actors"][0]["sources"][0].update(
                spells=[]
            ),
            "spell.source",
        ),
        (
            lambda w: w["state_snapshot"]["combat_state"]["actor_zone"].update({FOE: "25,0"}),
            "target_invalid",
        ),
        (
            lambda w: w["state_snapshot"]["character_states"][0].update(action_available=False),
            "no_action_economy",
        ),
        (
            lambda w: w["state_snapshot"]["character_states"][0].update(attack_bonus=99),
            "spell.source",
        ),
        (
            lambda w: w["state_snapshot"]["character_states"][0].update(
                proficiency_bonus_override=6
            ),
            "spell.source",
        ),
        (
            lambda w: w["state_snapshot"]["character_states"][0].update(
                granted_features=["spellcasting"]
            ),
            "spell.attack_dependencies",
        ),
        (
            lambda w: w["state_snapshot"]["scene_state"]["grid"].update(default_lighting="dim"),
            "spell.attack_dependencies",
        ),
    ],
)
def test_spell_attack_refusals_never_draw_or_propose_changes(mutation, code):
    wire = spell_attack_wire("fire-bolt")
    mutation(wire)
    request = RuleEvaluationRequest.model_validate_json(dumps(wire))
    result = execute(request, get_default_ruleset())
    assert result.error is not None
    assert result.error.code == code
    assert result.state_delta is None
    assert result.rng_transition is None
    assert result.proposed_events == ()


@pytest.mark.parametrize("spell_id", ["eldritch-blast", "sorcerous-burst", "ray-of-sickness"])
def test_profile_refuses_reviewed_attack_assets_with_additional_mechanics(spell_id):
    loader = get_default_ruleset()
    assert ordinary_spell_mechanism(loader, spell_id) is None
    request = RuleEvaluationRequest.model_validate_json(dumps(spell_attack_wire(spell_id)))
    result = execute(request, loader)
    assert result.status == "unsupported"
    assert result.error.code == "spell.capability"
    assert result.state_delta is None
    assert result.rng_transition is None
    assert result.proposed_events == ()


def test_mechanism_classification_does_not_extend_weapon_availability():
    loader = get_default_ruleset()
    request = RuleEvaluationRequest.model_validate_json(dumps(spell_attack_wire("fire-bolt")))
    assert ordinary_spell_mechanism(loader, "fire-bolt") == "attack_damage"
    assert ordinary_spell_mechanism(loader, "poison-spray") == "attack_damage"
    query = ActionAvailabilityRequest(
        schema_version="engine-availability/16",
        session_id=request.session_id,
        operation_kind="combat.intent",
        actor_id=request.actor_id,
        payload=request.payload,
        state_snapshot=request.state_snapshot,
        ruleset_binding=request.ruleset_binding,
    )
    assert query_action_availability(query, loader=loader).status == "unknown"
