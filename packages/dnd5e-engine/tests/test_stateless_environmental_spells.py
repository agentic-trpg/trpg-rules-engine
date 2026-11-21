"""Public spatial spell proposals and the shared illumination-dispel rule."""

import json
from dataclasses import replace

import pytest
from dnd5e_srd_data import BundledAssetLoader
from pydantic import ValidationError

from dnd5e_engine.environment import EnvironmentalSource, environmental_dispels
from dnd5e_engine.evaluation_contracts import (
    ENVIRONMENTAL_SPELL_SCHEMA_VERSION,
    SCHEMA_VERSION,
    RuleEvaluationRequest,
    RuleEvaluationResult,
)
from dnd5e_engine.evaluation_delta import ResourceUpdate, WorldEnvironmentalEffectCreate
from dnd5e_engine.events import IntentSubmitted, SpellCast
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import execute
from tests.test_stateless_timed_spells import spell_wire


def _wire(spell_id, *, point=False, slot=None):
    """Supply a known spell, one ordinary slot when needed and fixed scene facts."""
    spell = BundledAssetLoader().get_spell(spell_id)
    wire, loader = spell_wire(spell)
    wire["schema_version"] = ENVIRONMENTAL_SPELL_SCHEMA_VERSION
    payload = wire["payload"]
    payload.pop("target_id")
    payload["target"] = (
        {"kind": "point", "cell_id": "1,0"} if point else {"kind": "item", "item_id": "stone"}
    )
    if not point:
        wire["spell_positions"]["target_item"] = dict(
            item_id="stone", size="large", worn_or_carried_by=None, opaque_cover=False
        )
    snapshot = wire["state_snapshot"]
    actor = snapshot["character_states"][0]
    if spell.level:
        payment_level = slot or spell.level
        class_level = 2 * payment_level - 1
        actor.update(character_level=class_level, classes={"wizard": class_level})
        actor["spell_slots"] = {payment_level: 2}
        snapshot["resource_state"]["pools"][0].update(
            pool_id=f"slots:{payment_level}", level=payment_level
        )
        payload["slot_level"] = payment_level
    else:
        actor["spell_slots"] = {}
        snapshot["resource_state"] = {"pools": []}
        selection = snapshot["casting_state"]["actors"][0]["sources"][0]["spells"][0]
        selection["prepared_order"] = None
    return wire, loader


def _request(wire):
    """Parse the real wire representation with the public request validator."""
    return RuleEvaluationRequest.model_validate_json(json.dumps(wire))


def test_light_casts_from_known_cantrip_without_payment():
    """Create the canonical item effect even over a covered, caster-held item."""
    wire, loader = _wire("light")
    wire["spell_positions"]["target_item"].update(worn_or_carried_by=HERO, opaque_cover=True)
    request = _request(wire)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    (creation,) = result.state_delta.operations
    assert isinstance(creation, WorldEnvironmentalEffectCreate)
    effect = creation.value
    assert effect.anchor == request.payload.target
    assert (effect.source_id, effect.spell_source_id) == (HERO, "class:wizard")
    assert (effect.environment.kind, effect.environment.light) == ("light", "bright")
    assert (effect.radius_ft, effect.environment.dim_extension_ft) == (20, 20)
    assert effect.duration_seconds == 3600
    assert effect.replace_same_caster_on_recast
    assert not effect.concentration
    intent, cast = result.proposed_events
    assert isinstance(intent, IntentSubmitted)
    assert intent.item_id == "stone"
    assert isinstance(cast, SpellCast)
    assert cast.slot_level is None
    assert not result.rng_transition.state_changed
    copy = RuleEvaluationResult.model_validate_json(result.model_dump_json())
    copy.verify_request(request)
    assert copy == execute(request, loader)


@pytest.mark.parametrize("point", [False, True])
def test_darkness_prices_one_slot_and_preserves_spatial_lifetime(point):
    """Use the selected ordinary slot and the canonical Darkness effect."""
    wire, loader = _wire("darkness", point=point, slot=3)
    if not point:
        wire["spell_positions"]["target_item"]["size"] = None
    request = _request(wire)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    spend, creation = result.state_delta.operations
    assert isinstance(spend, ResourceUpdate)
    assert spend.amount == -1
    assert spend.expected.level == 3
    assert spend.value.current == 1
    assert isinstance(creation, WorldEnvironmentalEffectCreate)
    effect = creation.value
    assert effect.anchor == request.payload.target
    assert effect.shape == ("sphere" if point else "emanation")
    assert (effect.radius_ft, effect.duration_seconds, effect.slot_level) == (15, 600, 3)
    assert effect.environment.kind == "magical_darkness"
    assert effect.environment.dispels_light_through_level == 2
    assert effect.concentration
    assert not effect.replace_same_caster_on_recast
    assert not result.rng_transition.state_changed


@pytest.mark.parametrize(
    ("spell_id", "point", "item", "changes", "error"),
    [
        ("light", True, {}, {}, "target_invalid"),
        ("light", False, {"size": "huge"}, {}, "target_invalid"),
        ("light", False, {"size": None}, {}, "target_invalid"),
        ("light", False, {"worn_or_carried_by": FOE}, {}, "target_invalid"),
        ("darkness", False, {"worn_or_carried_by": HERO}, {}, "target_invalid"),
        ("darkness", False, {}, {"spell_source_id": "class:unknown"}, "spell.source"),
        ("darkness", True, {}, {"slot_level": 1}, "spell.slot"),
        ("darkness", True, {}, {"slot_level": 9}, "spell.slot"),
        ("light", False, {}, {"slot_level": 1}, "spell.slot"),
    ],
)
def test_invalid_environmental_cast_has_no_mechanical_results(
    spell_id, point, item, changes, error
):
    """Reject wrong target, source and slot facts before proposing payment or RNG."""
    wire, loader = _wire(spell_id, point=point)
    if not point:
        wire["spell_positions"]["target_item"].update(item)
    wire["payload"].update(changes)
    result = execute(_request(wire), loader)
    assert result.status == "rejected"
    assert result.error.code == error
    assert result.state_delta is None
    assert result.rng_transition is None
    assert result.proposed_events == ()


def test_explicit_target_must_match_context_and_protocol_version():
    """Reject item fact substitution and a spatial payload on the older protocol."""
    wire, _ = _wire("light")
    wire["spell_positions"]["target_item"]["item_id"] = "different"
    with pytest.raises(ValidationError, match="match the supplied fixed item facts"):
        _request(wire)
    wire["spell_positions"]["target_item"]["item_id"] = "stone"
    wire["schema_version"] = SCHEMA_VERSION
    with pytest.raises(ValidationError, match="evaluation/31"):
        _request(wire)


def test_fixed_point_outside_the_scene_is_rejected():
    """Check actual fixed geometry before proposing a spell slot or effect."""
    wire, loader = _wire("darkness", point=True)
    wire["payload"]["target"]["cell_id"] = "400,400"
    wire["spell_positions"]["target_cell"] = "400,400"
    result = execute(_request(wire), loader)
    assert result.status == "rejected"
    assert result.error.code == "target_invalid"
    assert result.state_delta is None


def test_result_with_forged_lifecycle_geometry_is_rejected():
    """Verify an accepted spatial result against the exact cast and canonical profile."""
    wire, loader = _wire("darkness", point=True)
    request = _request(wire)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    changed = json.loads(result.model_dump_json())
    changed["state_delta"]["operations"][1]["value"]["radius_ft"] = 45
    forged = RuleEvaluationResult.model_validate_json(json.dumps(changed))
    with pytest.raises(ValueError, match="differs from its fixed spell input"):
        forged.verify_request(request)


def test_dispels_use_surviving_dim_footprints_and_spell_levels():
    """A contained overlap dispels a qualifying spell, including in dim light alone."""
    light_wire, light_loader = _wire("light")
    darkness_wire, darkness_loader = _wire("darkness", point=True)
    profiles = []
    for wire, loader in ((light_wire, light_loader), (darkness_wire, darkness_loader)):
        result = execute(_request(wire), loader)
        assert result.status == "accepted", result.error
        profiles.append(result.state_delta.operations[-1].value.environment)
    light = EnvironmentalSource(
        "light",
        "light",
        HERO,
        0,
        profiles[0],
        frozenset({"0,0"}),
        "0,0",
        20,
        None,
        None,
        dim_cells=frozenset({"1,0"}),
    )
    darkness = EnvironmentalSource(
        "darkness",
        "darkness",
        FOE,
        2,
        profiles[1],
        frozenset({"1,0"}),
        "1,0",
        15,
        None,
        None,
    )
    assert environmental_dispels((light, darkness)) == {"light": "darkness"}
    assert not environmental_dispels((replace(light, spell_level=3), darkness))
    assert not environmental_dispels((replace(light, suppressed=True), darkness))
    assert not environmental_dispels((light, replace(darkness, cells=frozenset({"2,0"}))))
