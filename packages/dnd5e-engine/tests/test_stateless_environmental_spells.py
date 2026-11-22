"""Public spatial spell proposals and the shared illumination-dispel rule."""

import json
from dataclasses import replace

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from pydantic import ValidationError

from dnd5e_engine.environment import EnvironmentalSource, environmental_dispels
from dnd5e_engine.evaluation_contracts import (
    DISPEL_MAGIC_SCHEMA_VERSION,
    ENVIRONMENTAL_SPELL_SCHEMA_VERSION,
    SCHEMA_VERSION,
    SPATIAL_EFFECT_END_SCHEMA_VERSION,
    RuleEvaluationRequest,
    RuleEvaluationResult,
)
from dnd5e_engine.evaluation_delta import (
    ResourceUpdate,
    WorldEnvironmentalEffectCreate,
    WorldEnvironmentalEffectEnd,
)
from dnd5e_engine.evaluation_ruleset import freeze_ruleset, ruleset_binding
from dnd5e_engine.events import IntentSubmitted, SpellCast
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import execute
from tests.test_stateless_timed_spells import spell_wire


def _wire(spell_id, *, point=False, slot=None, caster_class="wizard"):
    """Supply a known spell, one ordinary slot when needed and fixed scene facts."""
    spell = BundledAssetLoader().get_spell(spell_id)
    payment_level = slot or spell.level
    class_level = max(1, 2 * payment_level - 1)
    wire, loader = spell_wire(spell, caster_class=caster_class, caster_level=class_level)
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


def test_darkness_uses_higher_level_pact_slot_for_prepared_warlock_spell():
    """A level-five Warlock can prepare Darkness and pays its owned level-three slot."""
    wire, loader = _wire("darkness", point=True, slot=3, caster_class="warlock")
    snapshot = wire["state_snapshot"]
    actor = snapshot["character_states"][0]
    actor.update(spell_slots={}, pact_slots={3: 2})
    (pool,) = snapshot["resource_state"]["pools"]
    pool.update(kind="pact_slot", pool_id="pact:3")

    request = _request(wire)
    result = execute(request, loader)

    assert result.status == "accepted", result.error
    spend, creation = result.state_delta.operations
    assert isinstance(spend, ResourceUpdate)
    assert (spend.expected.kind, spend.expected.level, spend.pool_id) == ("pact_slot", 3, "pact:3")
    assert (spend.expected.current, spend.value.current) == (2, 1)
    assert creation.value.slot_level == result.proposed_events[1].slot_level == 3
    result.verify_request(request)


def test_darkness_refuses_underleveled_warlock_source_even_with_ordinary_slot():
    """A separate class's higher slot cannot raise the Warlock's preparation limit."""
    wire, loader = _wire("darkness", point=True, slot=3, caster_class="warlock")
    actor = wire["state_snapshot"]["character_states"][0]
    actor.update(character_level=7, classes={"warlock": 2, "wizard": 5})

    result = execute(_request(wire), loader)

    assert result.status == "rejected"
    assert result.error.code == "spell.source"
    assert result.state_delta is None
    assert result.proposed_events == ()
    assert result.rng_transition is None


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


@pytest.mark.parametrize("obstruction", ["outside", "total-cover"])
def test_result_cannot_be_reused_against_unreachable_fixed_geometry(obstruction):
    """Require the same spatial admission for evaluation and result verification."""
    wire, loader = _wire("darkness", point=True)
    result = execute(_request(wire), loader)
    assert result.status == "accepted", result.error
    if obstruction == "outside":
        wire["spell_positions"]["source_cell"] = "400,400"
    else:
        wire["state_snapshot"]["scene_state"]["grid"]["cover_cells"] = {"1,0": "total"}
    unreachable = _request(wire)
    assert execute(unreachable, loader).status == "rejected"
    with pytest.raises(ValueError, match="differs from its fixed spell input"):
        result.verify_request(unreachable)


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


def _dispel_wire():
    """Build a fixed request with the host-selected Darkness application."""
    wire, loader = _wire("dispel-magic", point=True)
    wire["schema_version"] = SPATIAL_EFFECT_END_SCHEMA_VERSION
    wire["payload"]["target"] = {"kind": "effect", "effect_id": "spatial:dark"}
    wire["spell_positions"]["target_effect"] = {
        "effect_id": "spatial:dark",
        "caster_id": FOE,
        "spell_source_id": "class:wizard",
        "spell_id": "darkness",
        "slot_level": 2,
        "cell_id": "1,0",
        "expires_tick": 600,
        "concentration_id": "spatial:dark",
    }
    return wire, loader


def test_dispel_binds_the_exact_effect_and_spends_one_slot_without_rng():
    wire, loader = _dispel_wire()
    request = _request(wire)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    spend, ending = result.state_delta.operations
    assert (spend.expected.level, spend.amount) == (3, -1)
    assert ending == WorldEnvironmentalEffectEnd(
        kind="world.environmental_effect_end",
        expected=request.spell_positions.target_effect,
        reason="dispelled",
    )
    assert not result.rng_transition.state_changed
    copy = RuleEvaluationResult.model_validate_json(result.model_dump_json())
    copy.verify_request(request)
    forged = json.loads(result.model_dump_json())
    forged["state_delta"]["operations"][1]["expected"]["effect_id"] = "spatial:unrelated"
    with pytest.raises(ValueError, match="differs from its fixed spell input"):
        RuleEvaluationResult.model_validate_json(json.dumps(forged)).verify_request(request)


def test_dispel_rejects_mismatched_or_unsupported_spatial_selection():
    wire, loader = _dispel_wire()
    effect = wire["spell_positions"]["target_effect"]
    effect["effect_id"] = "spatial:mismatch"
    with pytest.raises(ValidationError, match="selected effect"):
        _request(wire)
    effect["effect_id"] = "spatial:dark"
    wire["schema_version"] = ENVIRONMENTAL_SPELL_SCHEMA_VERSION
    with pytest.raises(ValidationError, match="evaluation/34"):
        _request(wire)
    wire["schema_version"] = SPATIAL_EFFECT_END_SCHEMA_VERSION
    effect["slot_level"] = 4
    result = execute(_request(wire), loader)
    assert (result.status, result.error.code, result.state_delta) == (
        "rejected",
        "target_invalid",
        None,
    )


@pytest.mark.parametrize("obstruction", ["range", "total_cover"])
def test_dispel_target_requires_reachable_geometry(obstruction):
    wire, loader = _dispel_wire()
    grid = wire["state_snapshot"]["scene_state"]["grid"]
    if obstruction == "range":
        grid["width"] = 30
        wire["spell_positions"]["target_cell"] = "26,0"
        wire["spell_positions"]["target_effect"]["cell_id"] = "26,0"
    else:
        grid["cover_cells"] = {"1,0": "total"}
    result = execute(_request(wire), loader)
    assert (result.status, result.error.code, result.state_delta) == (
        "rejected",
        "target_invalid",
        None,
    )


@pytest.mark.parametrize("level", [0, 3, 4])
def test_generic_dispel_uses_actual_spatial_level_and_only_draws_for_higher_spell(level):
    """Accept a moving object anchor and preserve one check for a higher spell."""
    wire, loader = _dispel_wire()
    wire["schema_version"] = DISPEL_MAGIC_SCHEMA_VERSION
    effect = wire["spell_positions"]["target_effect"]
    effect.update(slot_level=level, anchor_item_id="stone")
    effect["spell_id"] = "light" if level == 0 else "darkness"
    canonical = BundledAssetLoader()
    loader = freeze_ruleset(
        MemoryAssetLoader(
            spells=[canonical.get_spell("dispel-magic"), canonical.get_spell(effect["spell_id"])],
            classes=[canonical.get_class("wizard")],
            items=[canonical.get_item("component-pouch")],
        )
    )
    wire["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    request = _request(wire)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert result.rng_transition.state_changed is (level > 3)
    assert len(result.proposed_events) == 2 + int(level > 3)
    if level <= 3 or result.proposed_events[-1].succeeded:
        assert len(result.state_delta.operations) == 2
    else:
        assert len(result.state_delta.operations) == 1
    RuleEvaluationResult.model_validate_json(result.model_dump_json()).verify_request(request)


def test_generic_dispel_refuses_spatial_level_below_bound_spell():
    """Level zero is valid for Light but cannot describe a Darkness cast."""
    wire, loader = _dispel_wire()
    wire["schema_version"] = DISPEL_MAGIC_SCHEMA_VERSION
    wire["spell_positions"]["target_effect"]["slot_level"] = 0
    result = execute(_request(wire), loader)
    assert (result.status, result.error.code, result.state_delta) == (
        "rejected",
        "spell.slot",
        None,
    )


def test_generic_dispel_checks_each_high_creature_spell_and_pays_once():
    """One payment resolves independent cast identities in stable order."""
    wire, loader = _dispel_wire()
    long_wire, long_loader = spell_wire(BundledAssetLoader().get_spell("longstrider"))
    long_result = execute(_request(long_wire), long_loader)
    effect = long_result.state_delta.operations[-1].value.effect.model_dump(mode="json")
    second = dict(effect, origin=effect["origin"] + ":recast:second")
    third = dict(effect, origin=effect["origin"] + ":recast:third")
    wire["schema_version"] = DISPEL_MAGIC_SCHEMA_VERSION
    wire["payload"]["target"] = {"kind": "creature", "target_id": FOE}
    positions = wire["spell_positions"]
    positions.pop("target_effect")
    positions["visible_target_id"] = FOE
    positions["target_applications"] = [
        dict(
            effect=effect,
            caster_id=HERO,
            spell_id="longstrider",
            cast_command_id="cast:first",
            expires_tick=600,
            concentration_id=None,
            cast_slot_level=1,
        ),
        dict(
            effect=second,
            caster_id=HERO,
            spell_id="longstrider",
            cast_command_id="cast:second",
            expires_tick=600,
            concentration_id=None,
            cast_slot_level=4,
        ),
        dict(
            effect=third,
            caster_id=HERO,
            spell_id="longstrider",
            cast_command_id="cast:third",
            expires_tick=600,
            concentration_id=None,
            cast_slot_level=5,
        ),
    ]
    wire["state_snapshot"]["effect_states"] = [effect, second, third]
    canonical = BundledAssetLoader()
    loader = freeze_ruleset(
        MemoryAssetLoader(
            spells=[canonical.get_spell("dispel-magic"), canonical.get_spell("longstrider")],
            classes=[canonical.get_class("wizard")],
            items=[canonical.get_item("component-pouch")],
        )
    )
    wire["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    request = _request(wire)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert result.state_delta.operations[0].amount == -1
    assert len(result.proposed_events) == 4
    assert result.rng_transition.state_changed
    ends = result.state_delta.operations[1:]
    assert ends[0].expected.identity == (FOE, effect["id"], effect["origin"])
    assert all(end.reason == "dispelled" for end in ends)
    assert len(ends) == 1 + sum(event.succeeded for event in result.proposed_events[2:])
    RuleEvaluationResult.model_validate_json(result.model_dump_json()).verify_request(request)


def test_dispel_and_cast_trigger_end_same_invisibility_only_once():
    """The /37 cast trigger yields to the selected /42 spell ending identity."""
    wire, _ = _dispel_wire()
    invisible_spell = BundledAssetLoader().get_spell("invisibility")
    invisible_wire, invisible_loader = spell_wire(invisible_spell, target_id=HERO, caster_level=5)
    invisible_result = execute(_request(invisible_wire), invisible_loader)
    effect = invisible_result.state_delta.operations[-1].value.effect.model_dump(mode="json")
    application = dict(
        effect=effect,
        caster_id=HERO,
        spell_id="invisibility",
        cast_command_id="cast:invisible",
        expires_tick=600,
        concentration_id="actor:cast:invisible",
        cast_slot_level=2,
    )
    wire["schema_version"] = "engine-evaluation/37"
    wire["spell_end_facts"] = {
        "cast_schema_version": DISPEL_MAGIC_SCHEMA_VERSION,
        "applications": [application],
    }
    wire["payload"]["target"] = {"kind": "creature", "target_id": HERO}
    wire["spell_positions"]["target_cell"] = "0,0"
    wire["spell_positions"].pop("target_effect")
    wire["spell_positions"]["visible_target_id"] = HERO
    wire["spell_positions"]["target_applications"] = [application]
    wire["state_snapshot"]["effect_states"] = [effect]
    canonical = BundledAssetLoader()
    loader = freeze_ruleset(
        MemoryAssetLoader(
            spells=[canonical.get_spell("dispel-magic"), invisible_spell],
            classes=[canonical.get_class("wizard")],
            items=[canonical.get_item("component-pouch")],
        )
    )
    wire["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    request = _request(wire)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert len(result.state_delta.operations) == 2
    assert result.state_delta.operations[1].reason == "dispelled"
    RuleEvaluationResult.model_validate_json(result.model_dump_json()).verify_request(request)
