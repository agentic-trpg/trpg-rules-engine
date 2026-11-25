"""Exploration spell proposals with canonical source, geometry, and RNG evidence."""

import json

import pytest
from dnd5e_srd_data import MemoryAssetLoader

from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest, RuleEvaluationResult
from dnd5e_engine.evaluation_delta import (
    ConditionsUpdate,
    DeathStateUpdate,
    HPDelta,
    ResourceUpdate,
)
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_engine.events import (
    ConditionRemoved,
    HealingApplied,
    HealingDieDraw,
    HealingRollDetail,
    RollFixedModifier,
)
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import execute
from tests.test_stateless_casting_v20 import canonical_healing_wire


def exploration_wire():
    """Convert the bound Cleric combat fixture to fixed local exploration inputs."""
    wire, loader = canonical_healing_wire()
    wire.update(
        schema_version="engine-evaluation/29",
        operation_kind="world.cast_spell",
        payload={
            "kind": "world.cast_spell",
            "spell_id": "cure-wounds",
            "spell_source_id": "class:cleric",
            "slot_level": 1,
            "target_id": FOE,
        },
        spell_positions={"source_cell": "0,0", "target_cell": "1,0"},
    )
    snapshot = wire["state_snapshot"]
    snapshot.pop("combat_state")
    snapshot.update(snapshot_kind="non_combat", combat_setup=None)
    return wire, loader


def test_exploration_healing_has_only_payment_and_capped_hp_proposals() -> None:
    wire, loader = exploration_wire()
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert result == execute(request, loader)
    assert result == RuleEvaluationResult.model_validate_json(result.model_dump_json())
    spend, healing = result.state_delta.operations
    assert isinstance(spend, ResourceUpdate)
    assert (spend.owner_id, spend.amount, spend.value.current) == (HERO, -1, 1)
    assert isinstance(healing, HPDelta)
    target = next(a for a in request.state_snapshot.character_states if a.entity_id == FOE)
    rng = request.rng_context.state.restore()
    faces = (rng.randint(1, 8), rng.randint(1, 8))
    amount = sum(faces) + 3
    assert result.proposed_events[-1] == HealingApplied(
        target_id=FOE,
        amount=amount,
        roll_detail=HealingRollDetail(
            dice=tuple(HealingDieDraw(die_size=8, face=face) for face in faces),
            fixed_addends=(
                RollFixedModifier(source="spell_bonus", value=3),
                RollFixedModifier(source="recipient_bonus", value=0),
            ),
        ),
    )
    assert healing.target_id == FOE
    assert healing.expected_hp == target.hp_current
    assert healing.resulting_hp == min(target.hp_max, target.hp_current + amount)
    assert result.rng_transition.next_state == RNGState.capture(rng)


def test_living_zero_hp_character_heal_resets_saves_and_only_downed_unconscious():
    """Keep independent conditions while proposing one typed recovery transition."""
    wire, loader = exploration_wire()
    target = wire["state_snapshot"]["character_states"][1]
    target.update(
        entity_type="Character",
        hp_current=0,
        is_alive=True,
        death_saves={"successes": 1, "failures": 2, "is_stable": False},
        conditions=[
            {
                "condition": name,
                "source_entity_id": source,
                "scope": "combat",
                "duration_rounds": None,
                "save_dc": None,
                "applied_round": 0,
                "exhaustion_level": 1,
                "source_effect_id": None,
            }
            for name, source in (
                ("unconscious", "implied:event"),
                ("unconscious", "implied:other"),
                ("prone", "implied:event"),
            )
        ],
    )
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert result == RuleEvaluationResult.model_validate_json(result.model_dump_json())
    payment, hp, life, conditions = result.state_delta.operations
    assert isinstance(payment, ResourceUpdate)
    assert isinstance(hp, HPDelta)
    assert hp.expected_hp == 0
    assert hp.resulting_hp > 0
    assert isinstance(life, DeathStateUpdate)
    assert (life.value.is_alive, life.value.death_saves.model_dump()) == (
        True,
        {"successes": 0, "failures": 0, "is_stable": False},
    )
    assert isinstance(conditions, ConditionsUpdate)
    assert [(c.condition, c.source_entity_id) for c in conditions.value] == [
        ("unconscious", "implied:other"),
        ("prone", "implied:event"),
    ]
    assert result.proposed_events[-1] == ConditionRemoved(target_id=FOE, condition="unconscious")
    assert result.rng_transition.state_changed


def test_dead_target_cannot_receive_ordinary_exploration_healing():
    wire, loader = exploration_wire()
    target = wire["state_snapshot"]["character_states"][1]
    target.update(hp_current=0, is_alive=False)
    result = execute(RuleEvaluationRequest.model_validate_json(json.dumps(wire)), loader)
    assert (result.status, result.error.code) == ("rejected", "target_invalid")
    assert result.state_delta is None
    assert result.proposed_events == ()
    assert result.rng_transition is None


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        ("blocked", "target_invalid"),
        ("upcast", "spell.slot"),
        ("alternate_formula", "spell.mechanism"),
    ],
)
def test_exploration_healing_refuses_unadmitted_inputs(fault, code) -> None:
    wire, loader = exploration_wire()
    if fault == "blocked":
        wire["state_snapshot"]["scene_state"]["grid"]["wall_segments"] = [
            {"x1": 1, "y1": 0, "x2": 1, "y2": 2}
        ]
    elif fault == "upcast":
        wire["payload"]["slot_level"] = 2
    else:
        spell = loader.get_spell("cure-wounds")
        activity = spell.activities[0]
        altered = spell.model_copy(
            update={
                "activities": [
                    activity.model_copy(
                        update={"healing": activity.healing.model_copy(update={"number": 1})}
                    )
                ]
            }
        )
        loader = MemoryAssetLoader(
            classes=[loader.get_class("cleric")],
            features=[loader.get_feature(slug) for slug in ("spellcasting", "divine-order")],
            spells=[altered],
        )
        wire["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    result = execute(RuleEvaluationRequest.model_validate_json(json.dumps(wire)), loader)
    assert result.error.code == code
    assert result.state_delta is None
    assert result.proposed_events == ()
    assert result.rng_transition is None
