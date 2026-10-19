import asyncio

import pytest
from pydantic import ValidationError

from dnd5e_engine.evaluation_openable import OpenableCheckRequest, evaluate_openable
from dnd5e_engine.evaluation_state import InventoryEntry
from tests.evaluation_support import HERO, synthetic_loader
from tests.test_stateless_checks import check_case


def case(*, tool="thieves-tools", proficient=False, skill=False, expertise=False):
    req = check_case(
        skill="sleight_of_hand",
        ability="dex",
        actor_updates={
            "tool_proficiencies": (tool,) if proficient else (),
            "skill_proficiencies": ["sleight_of_hand"] if skill else [],
            "skill_expertise": ["sleight_of_hand"] if expertise else [],
        },
    )
    snapshot = req.state_snapshot.model_copy(
        update={
            "inventory_state": req.state_snapshot.inventory_state.model_copy(
                update={
                    "entries": (
                        InventoryEntry(
                            instance_id="tool",
                            owner_id=HERO,
                            item_slug=tool,
                            quantity=1,
                            charges_remaining_per_unit=0,
                            accessible=True,
                        ),
                    )
                }
            )
        }
    )
    return OpenableCheckRequest(
        schema_version="engine-openable/1",
        request=req.model_copy(update={"state_snapshot": snapshot}),
        operation="pick_lock",
        tool_instance_id="tool",
        required_tool="thieves-tools",
        leverage_applicable=False,
    )


@pytest.mark.parametrize(
    "tool_prof,skill_prof,expertise,bonus,mode",
    [
        (False, False, False, 0, "normal"),
        (True, False, False, 2, "normal"),
        (False, True, False, 2, "normal"),
        (True, True, False, 2, "advantage"),
        (True, True, True, 4, "advantage"),
    ],
)
def test_actual_tool_skill_combination(tool_prof, skill_prof, expertise, bonus, mode):
    spec = case(proficient=tool_prof, skill=skill_prof, expertise=expertise)
    result = asyncio.run(evaluate_openable(spec, loader=synthetic_loader()))
    assert result.status == "accepted", result
    event = result.proposed_events[0]
    actor = spec.request.state_snapshot.character_states[0]
    assert event.modifier == (actor.dexterity - 10) // 2 + bonus
    assert event.advantage == mode
    assert result.rng_transition.state_changed
    assert spec.request.state_snapshot.character_states[0] == actor
    assert asyncio.run(evaluate_openable(spec, loader=synthetic_loader())) == result


def test_wrong_tool_is_rejected_without_rng():
    result = asyncio.run(evaluate_openable(case(tool="crowbar"), loader=synthetic_loader()))
    assert result.error is not None
    assert result.error.code == "tool_unavailable"
    assert result.rng_transition is None


def test_leverage_cannot_be_forged_for_dexterity():
    spec = case()
    data = spec.model_dump(mode="python")
    data["leverage_applicable"] = True
    with pytest.raises(ValidationError):
        OpenableCheckRequest.model_validate(data)


def test_crowbar_equipment_is_not_a_tool_proficiency_bonus():
    spec = case(tool="crowbar", proficient=True)
    req = spec.request.model_copy(
        update={
            "payload": spec.request.payload.model_copy(update={"ability": "str", "skill": None})
        }
    )
    data = spec.model_dump(mode="python")
    data.update(
        request=req, operation="force_open", required_tool="crowbar", leverage_applicable=True
    )
    force = OpenableCheckRequest.model_validate(data)
    result = asyncio.run(evaluate_openable(force, loader=synthetic_loader()))
    assert result.status == "accepted"
    actor = req.state_snapshot.character_states[0]
    assert result.proposed_events[0].modifier == (actor.strength - 10) // 2
    assert result.proposed_events[0].advantage == "advantage"
