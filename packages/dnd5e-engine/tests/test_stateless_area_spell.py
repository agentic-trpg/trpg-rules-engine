"""Test deterministic point-area resolution through the public stateless evaluator."""

from json import dumps
from random import Random

import pytest

from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest
from dnd5e_engine.evaluation_delta import CombatClose
from dnd5e_engine.evaluation_region_effect import plan_region_condition_transition
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import get_default_ruleset, ruleset_binding
from dnd5e_engine.events import AreaTargeted, DamageApplied, SaveRolled
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import execute
from tests.test_stateless_spell_attack import spell_attack_wire


def area_spell_wire(origin):
    """Use the reviewed Wizard encounter with a canonically learned Acid Splash."""
    wire = spell_attack_wire("acid-splash", seed=2, distance=10)
    del wire["payload"]["target_id"]
    wire["payload"]["target_zone_id"] = origin
    wire["state_snapshot"]["character_states"][1]["granted_features"] = []
    source = wire["state_snapshot"]["casting_state"]["actors"][0]["sources"][0]
    source["spells"].append({"spell_id": "acid-splash", "prepared_order": None, "material": None})
    return wire


def test_standalone_area_spell_proposes_bound_placement_save_and_damage():
    request = RuleEvaluationRequest.model_validate_json(dumps(area_spell_wire("2,0")))
    result = execute(request, get_default_ruleset())
    assert result.status == "accepted", result.error
    assert result == execute(request, get_default_ruleset())
    (area,) = [e for e in result.proposed_events if isinstance(e, AreaTargeted)]
    (save,) = [e for e in result.proposed_events if isinstance(e, SaveRolled)]
    (damage,) = [e for e in result.proposed_events if isinstance(e, DamageApplied)]
    assert (area.origin, area.shape, area.affected_ids) == ("2,0", "sphere", [FOE])
    rolls = Random(2)
    assert (damage.damage_type, damage.amount) == ("acid", rolls.randint(1, 6))
    assert (save.target_id, save.ability, save.dc, save.natural) == (
        FOE,
        "dex",
        13,
        rolls.randint(1, 20),
    )
    assert result.rng_transition.next_state == RNGState.capture(rolls)


@pytest.mark.parametrize("origin,code", [("25,0", "out_of_range"), ("0,0", "spell.area_targets")])
def test_area_spell_rejects_invalid_or_unsupported_placement_before_rng(origin, code):
    request = RuleEvaluationRequest.model_validate_json(dumps(area_spell_wire(origin)))
    result = execute(request, get_default_ruleset())
    assert result.error.code == code
    assert result.proposed_events == ()
    assert result.state_delta is None
    assert result.rng_transition is None


def test_closure_preserves_the_fixed_world_region_after_terminal_damage():
    """Close while a defeated enemy still occupies a fixed Poisoned source."""
    loader = get_default_ruleset()
    wire = area_spell_wire("2,0")
    wire.update(
        operation_kind="combat.close",
        payload={"kind": "combat.close"},
        schema_version="engine-evaluation/30",
    )
    snapshot = wire["state_snapshot"]
    scene = snapshot["scene_state"]["scene_id"]
    transition = plan_region_condition_transition(
        scene_id=scene,
        source_id="toxic.area",
        target_id=FOE,
        entering=True,
        present=False,
        condition_immunities=(),
        ruleset_binding=ruleset_binding(loader),
        loader=loader,
    )
    enemy = snapshot["character_states"][1]
    enemy.update(hp_current=0, is_alive=False)
    enemy["conditions"] = [
        {
            "condition": "poisoned",
            "source_entity_id": "implied:effect",
            "source_effect_id": transition.identity[1],
            "scope": "session",
            "duration_rounds": None,
            "save_dc": None,
            "applied_round": 0,
            "exhaustion_level": 1,
        }
    ]
    snapshot["effect_states"] = [transition.effect.model_dump(mode="json")]
    snapshot["combat_state"].update(
        dead_ids=[FOE],
        deaths_recorded=[
            {
                "target_id": FOE,
                "target_kind": enemy["entity_type"].lower(),
                "location_id": scene,
                "reason": "damage",
                "killer_id": HERO,
            }
        ],
        conditions_by_effect=[{"identity": transition.identity, "statuses": ["poisoned"]}],
    )
    request = RuleEvaluationRequest.model_validate_json(dumps(wire))
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    (closure,) = result.state_delta.operations
    assert isinstance(closure, CombatClose)
    assert closure.reason == "victory"
    assert not result.rng_transition.state_changed
