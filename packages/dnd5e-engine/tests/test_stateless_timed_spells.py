"""Public exploration spell proposals over bound local casting facts."""

import json

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd5e_srd_data.schema.spell import Spell

from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest, RuleEvaluationResult
from dnd5e_engine.evaluation_delta import ResourceUpdate, WorldEffectCreate
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_ruleset import freeze_ruleset, ruleset_binding
from dnd5e_engine.events import EffectApplied, IntentSubmitted, SpellCast
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import execute, request_and_live

_POUCH = "inventory:component-pouch"


def spell_wire(spell: Spell, *, target_id: str = FOE):
    """Build fixed /20 exploration facts for one Wizard and one locally placed target."""
    combat, _, _ = request_and_live()
    wire = json.loads(combat.model_dump_json())
    wire.update(
        schema_version="engine-evaluation/29",
        operation_kind="world.cast_spell",
        payload={
            "kind": "world.cast_spell",
            "spell_id": spell.slug,
            "spell_source_id": "class:wizard",
            "slot_level": spell.level,
            "target_id": target_id,
        },
    )
    if target_id != HERO:
        wire["spell_positions"] = {"source_cell": "0,0", "target_cell": "1,0"}
    snapshot = wire["state_snapshot"]
    snapshot.pop("combat_state")
    snapshot.update(
        snapshot_kind="non_combat", snapshot_schema_version="engine-snapshot/20", combat_setup=None
    )
    caster, target = snapshot["character_states"]
    for actor in (caster, target):
        actor.pop("spells_known")
        actor.update(
            weapon_in_hands=None,
            weapon_grip="none",
            carried_item_slugs=[],
            spell_slots={},
            pact_slots={},
        )
    caster.update(
        class_slug="wizard",
        classes={"wizard": 1},
        character_level=1,
        spellcasting_ability="int",
        spell_slots={spell.level: 2},
        carried_item_slugs=["component-pouch"],
    )
    snapshot["resource_state"] = {
        "pools": [
            {
                "owner_id": HERO,
                "pool_id": "slots:1",
                "kind": "spell_slot",
                "level": spell.level,
                "current": 2,
                "maximum": 2,
            }
        ]
    }
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
                            {
                                "spell_id": spell.slug,
                                "prepared_order": 0,
                                "material": {"inventory_instance_id": _POUCH, "available": True},
                            }
                        ],
                    }
                ],
            }
        ]
    }
    snapshot["inventory_state"] = {
        "entries": [
            {
                "instance_id": _POUCH,
                "owner_id": HERO,
                "item_slug": "component-pouch",
                "quantity": 1,
                "charges_remaining_per_unit": 0,
                "accessible": True,
            }
        ]
    }
    canonical = BundledAssetLoader()
    loader = freeze_ruleset(
        MemoryAssetLoader(
            items=[canonical.get_item("component-pouch")],
            spells=[spell],
            classes=[canonical.get_class("wizard")],
        )
    )
    wire["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    return wire, loader


def _longstrider() -> Spell:
    """Read the example spell from the bundled rules."""
    return BundledAssetLoader().get_spell("longstrider")


def test_timed_touch_spell_proposes_one_paid_effect_and_replays() -> None:
    """Publish only guarded slot/effect operations and deterministic cast evidence."""
    wire, loader = spell_wire(_longstrider())
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert result == execute(request, loader)
    assert result == RuleEvaluationResult.model_validate_json(result.model_dump_json())
    assert request == RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    result.verify_request(request)
    assert result.state_delta.expected_world_version == request.state_snapshot.world_version
    payment, creation = result.state_delta.operations
    assert isinstance(payment, ResourceUpdate)
    assert payment.owner_id == HERO
    assert payment.pool_id == "slots:1"
    assert payment.amount == -1
    assert payment.expected.current == 2
    assert payment.value.current == 1
    assert isinstance(creation, WorldEffectCreate)
    assert creation.expected_absent
    assert creation.value.identity == (FOE, "effect:longstrider", f"cast:longstrider:{HERO}")
    assert creation.value.duration_seconds == 3600
    assert creation.value.effect.duration.rounds is None
    assert creation.value.expiry.action == "remove"
    intent, cast, applied = result.proposed_events
    assert isinstance(intent, IntentSubmitted)
    assert (intent.actor_id, intent.target_id, intent.source_id) == (HERO, FOE, "class:wizard")
    assert isinstance(cast, SpellCast)
    assert (cast.spell_id, cast.slot_level, cast.components) == ("longstrider", 1, ["V", "S", "M"])
    assert isinstance(applied, EffectApplied)
    assert effect_state(applied.effect) == creation.value.effect
    result.rng_transition.verify_input(request.rng_context)
    assert result.rng_transition.next_state == request.rng_context.state
    assert not result.rng_transition.state_changed


def test_mechanically_equivalent_asset_with_different_id_and_values() -> None:
    """Admit self casting from the same mechanism without a named-spell branch."""
    data = _longstrider().model_dump(mode="json")
    data.update(slug="fleet-step-test", name="Fleet Step", foundry_uuid="")
    data["range"]["units"] = "self"
    data["duration"] = {"units": "minute", "value": 2}
    data["passive_effects"][0].update(name="Fleet Pace", duration={"seconds": 120})
    data["passive_effects"][0]["changes"] = [
        {"key": "system.attributes.senses.darkvision", "mode": 4, "value": "60"},
        {"key": "system.attributes.senses.blindsight", "mode": 2, "value": "5"},
    ]
    spell = Spell.model_validate(data)
    wire, loader = spell_wire(spell, target_id=HERO)
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    creation = result.state_delta.operations[1]
    assert isinstance(creation, WorldEffectCreate)
    assert creation.value.identity == (HERO, "effect:fleet_pace", f"cast:fleet_pace:{HERO}")
    assert [
        (change.key, change.mode, change.value) for change in creation.value.effect.changes
    ] == [
        ("system.attributes.senses.darkvision", "upgrade", "60"),
        ("system.attributes.senses.blindsight", "add", "5"),
    ]
    assert creation.value.duration_seconds == 120
    other, _ = spell_wire(spell)
    other["spell_positions"]["target_cell"] = other["spell_positions"]["source_cell"]
    refused = execute(RuleEvaluationRequest.model_validate_json(json.dumps(other)), loader)
    assert refused.status == "rejected"
    assert refused.error.code == "target_invalid"


@pytest.mark.parametrize(
    ("invalid", "code"),
    [
        ("missing-position", "target_invalid"),
        ("far-position", "target_invalid"),
        ("blocked-touch", "target_invalid"),
        ("blocked-cell-alias", "target_invalid"),
        ("nonexistent-target", "target_invalid"),
        ("nonexistent-caster", "actor_missing"),
        ("absent-source", "spell.source"),
        ("unprepared", "spell.source"),
        ("silenced", "spell.source"),
        ("borrowed-pouch", "spell.source"),
        ("empty-slot", "spell.slot"),
        ("pact-slot", "spell.slot"),
        ("upcast", "spell.slot"),
        ("duplicate", "spell.effect_exists"),
    ],
)
def test_timed_spell_admission_fails_without_cost_or_rng(invalid: str, code: str) -> None:
    """Refuse incomplete local admission without any partial result."""
    wire, loader = spell_wire(_longstrider())
    snapshot = wire["state_snapshot"]
    actor = snapshot["character_states"][0]
    casting = snapshot["casting_state"]["actors"][0]
    match invalid:
        case "missing-position":
            del wire["spell_positions"]
        case "far-position":
            wire["spell_positions"]["target_cell"] = "2,0"
        case "blocked-touch":
            snapshot["scene_state"]["grid"]["wall_segments"] = [
                {"x1": 1, "y1": 0, "x2": 1, "y2": 2}
            ]
        case "blocked-cell-alias":
            wire["spell_positions"]["target_cell"] = "01,0"
            snapshot["scene_state"]["grid"]["blocked_cells"] = ["1,0"]
        case "nonexistent-target":
            wire["payload"]["target_id"] = "absent-target"
        case "nonexistent-caster":
            wire["actor_id"] = "absent-caster"
        case "absent-source":
            wire["payload"]["spell_source_id"] = "trusted-source-not-present"
        case "unprepared":
            casting["sources"][0]["spells"][0]["prepared_order"] = None
        case "silenced":
            casting["verbal_available"] = False
        case "borrowed-pouch":
            snapshot["inventory_state"]["entries"][0]["owner_id"] = FOE
        case "empty-slot":
            actor["spell_slots"][1] = 0
            snapshot["resource_state"]["pools"][0]["current"] = 0
        case "pact-slot":
            actor["pact_slots"] = actor["spell_slots"]
            actor["spell_slots"] = {}
            snapshot["resource_state"]["pools"][0]["kind"] = "pact_slot"
        case "upcast":
            wire["payload"]["slot_level"] = 2
        case "duplicate":
            prior_request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
            prior = execute(prior_request, loader).state_delta.operations[1]
            prior_effect = prior.value.effect.model_dump(mode="json")
            prior_effect["origin"] = "cast:longstrider:another-caster"
            snapshot["effect_states"].append(prior_effect)
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    result = execute(request, loader)
    assert result.status in ("rejected", "unsupported")
    assert result.error.code == code
    assert result.state_delta is None
    assert result.rng_transition is None
    assert result.proposed_events == ()


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("concentration",), True),
        (("materials", "consumed"), True),
        (("activities", 0, "roll", "formula"), "1d6"),
        (("activities", 0, "target", "requires_willing"), True),
        (("passive_effects", 0, "duration", "seconds"), 12),
        (("passive_effects", 0, "statuses"), ["prone"]),
        (("passive_effects", 0, "changes", 0, "key"), "unsupported.modifier"),
    ],
)
def test_same_spell_id_cannot_bypass_mechanism_admission(path: tuple, value) -> None:
    """Unsupported structured mechanics fail closed even under the example identity."""
    data = _longstrider().model_dump(mode="json")
    component = data
    for key in path[:-1]:
        component = component[key]
    component[path[-1]] = value
    wire, loader = spell_wire(Spell.model_validate(data))
    result = execute(RuleEvaluationRequest.model_validate_json(json.dumps(wire)), loader)
    assert result.status == "unsupported"
    assert result.error.code == "spell.mechanism"
    assert result.state_delta is None
    assert result.rng_transition is None
    assert result.proposed_events == ()
