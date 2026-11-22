"""Independent rules admission for fixed caster-sight evidence."""

import json

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from pydantic import ValidationError

from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import execute
from tests.test_stateless_exploration_healing import exploration_wire


def healing_word_wire():
    """Select the first-level canonical Healing Word with real rule binding."""
    wire, caster = exploration_wire()
    loader = MemoryAssetLoader(
        classes=[caster.get_class("cleric")],
        features=[caster.get_feature(slug) for slug in ("spellcasting", "divine-order")],
        spells=[BundledAssetLoader().get_spell("healing-word")],
    )
    wire["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    wire["payload"]["spell_id"] = "healing-word"
    wire["state_snapshot"]["casting_state"]["actors"][0]["sources"][0]["spells"][0]["spell_id"] = (
        "healing-word"
    )
    wire["spell_positions"]["visible_target_id"] = FOE
    wire["spell_positions"]["target_cell"] = "12,0"
    wire["state_snapshot"]["scene_state"]["grid"]["width"] = 15
    return wire, loader


@pytest.mark.parametrize("fault", [None, "absent", "beyond", "wall"])
def test_healing_word_requires_exact_visibility_and_clear_path(fault):
    wire, loader = healing_word_wire()
    if fault == "absent":
        wire["spell_positions"].pop("visible_target_id")
    elif fault == "beyond":
        wire["spell_positions"]["target_cell"] = "13,0"
    elif fault == "wall":
        grid = wire["state_snapshot"]["scene_state"]["grid"]
        grid["wall_segments"] = [{"x1": 9, "y1": 0, "x2": 9, "y2": 1}]
    req = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    result = execute(req, loader)
    if fault is None:
        assert result.status == "accepted", result.error
        assert result.rng_transition.state_changed
    else:
        assert (result.status, result.error.code) == ("rejected", "target_invalid")
        assert result.state_delta is None
        assert result.rng_transition is None
        assert result.proposed_events == ()


def test_healing_word_cannot_supply_visibility_of_a_different_creature():
    wire, _ = healing_word_wire()
    wire["spell_positions"]["visible_target_id"] = HERO
    with pytest.raises(ValidationError, match="visibility must match"):
        RuleEvaluationRequest.model_validate_json(json.dumps(wire))
