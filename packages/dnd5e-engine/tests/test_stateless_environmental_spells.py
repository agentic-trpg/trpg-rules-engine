"""Public spatial spell proposals and the shared illumination-dispel rule."""

import json
from dataclasses import replace
from random import Random

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd5e_srd_data.schema.common import PassiveEffect, PassiveEffectChange
from pydantic import ValidationError

from dnd5e_engine.activities.effects import passive_effect_to_active_effect
from dnd5e_engine.environment import EnvironmentalSource, environmental_dispels
from dnd5e_engine.evaluation_contracts import (
    DISPEL_MAGIC_SCHEMA_VERSION,
    ENVIRONMENTAL_SPELL_SCHEMA_VERSION,
    FAERIE_FIRE_SCHEMA_VERSION,
    FOG_CLOUD_SCHEMA_VERSION,
    SCHEMA_VERSION,
    SPATIAL_EFFECT_END_SCHEMA_VERSION,
    ExplorationSpellPositions,
    RuleEvaluationRequest,
    RuleEvaluationResult,
)
from dnd5e_engine.evaluation_delta import (
    ResourceUpdate,
    WorldEnvironmentalEffectCreate,
    WorldEnvironmentalEffectEnd,
    WorldVisualEffectCreate,
)
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_environmental_contracts import (
    ExplorationEnvironmentalSpellCastPayload,
)
from dnd5e_engine.evaluation_environmental_spells import (
    environmental_spell_schema_version,
    evaluate_bound_faerie_step,
    prepare_environmental_spell,
    verify_bound_faerie_result,
)
from dnd5e_engine.evaluation_rng import RNGState, RNGTransition
from dnd5e_engine.evaluation_ruleset import freeze_ruleset, ruleset_binding
from dnd5e_engine.events import IntentSubmitted, RollDieModifier, SaveRolled, SpellCast
from dnd5e_engine.rules.conditions import CONDITION_IMPLIES, Condition
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import execute
from tests.test_stateless_saves import condition
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


@pytest.mark.parametrize("slot,radius", [(1, 20), (3, 60)])
def test_fog_cloud_uses_canonical_fixed_point_area(slot, radius):
    """Fog Cloud pays the chosen slot and proposes one typed obscuring area."""
    wire, loader = _wire("fog-cloud", point=True, slot=slot, caster_class="wizard")
    wire["schema_version"] = FOG_CLOUD_SCHEMA_VERSION
    request = _request(wire)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    spend, creation = result.state_delta.operations
    assert spend.expected.level == slot
    assert creation.value.anchor == request.payload.target
    assert creation.value.radius_ft == radius
    assert creation.value.environment.kind == "fog"
    assert creation.value.environment.obscurement == "heavy"
    assert creation.value.environment.strong_wind_dispersal
    assert creation.value.concentration
    assert creation.value.duration_seconds == 3600
    assert RuleEvaluationResult.model_validate_json(result.model_dump_json()) == result


def test_fog_cloud_requires_its_schema_and_fixed_point():
    """Legacy Light/Darkness input cannot quietly acquire Fog Cloud behavior."""
    wire, loader = _wire("fog-cloud", point=True)
    assert execute(_request(wire), loader).status == "unsupported"
    wire["schema_version"] = FOG_CLOUD_SCHEMA_VERSION
    wire["payload"]["target"] = {"kind": "item", "item_id": "stone"}
    wire["spell_positions"]["target_item"] = dict(
        item_id="stone", size="small", worn_or_carried_by=None, opaque_cover=False
    )
    result = execute(_request(wire), loader)
    assert (result.status, result.error.code) == ("rejected", "target_invalid")


def test_environmental_wire_selection_uses_typed_obscuration():
    """The host selects the fog wire from rules mechanics instead of spell identity."""
    loader = BundledAssetLoader()
    assert environmental_spell_schema_version("fog-cloud", loader) == FOG_CLOUD_SCHEMA_VERSION
    assert (
        environmental_spell_schema_version("darkness", loader) == ENVIRONMENTAL_SPELL_SCHEMA_VERSION
    )
    assert environmental_spell_schema_version("light", loader) == ENVIRONMENTAL_SPELL_SCHEMA_VERSION
    assert (
        environmental_spell_schema_version("fog-cloud", loader, ending=True)
        == DISPEL_MAGIC_SCHEMA_VERSION
    )


@pytest.mark.parametrize(
    "spell_id,target,schema_version,actor_ids",
    [
        (
            "faerie-fire",
            {"kind": "point", "cell_id": "1,0"},
            FAERIE_FIRE_SCHEMA_VERSION,
            (HERO, "second", FOE),
        ),
        ("fog-cloud", {"kind": "point", "cell_id": "1,0"}, FOG_CLOUD_SCHEMA_VERSION, (HERO,)),
        (
            "dispel-magic",
            {"kind": "creature", "target_id": FOE},
            DISPEL_MAGIC_SCHEMA_VERSION,
            (HERO, FOE),
        ),
        (
            "dispel-magic",
            {"kind": "effect", "effect_id": "effect"},
            DISPEL_MAGIC_SCHEMA_VERSION,
            (HERO,),
        ),
    ],
)
def test_environmental_preparation_declares_exact_actor_scope(
    spell_id, target, schema_version, actor_ids
):
    """Keep World order, deduplicate actors, and ignore object occupants."""
    payload = ExplorationEnvironmentalSpellCastPayload.model_validate(
        dict(
            kind="world.cast_spell",
            spell_id=spell_id,
            spell_source_id="class:druid",
            slot_level=1,
            target=target,
        )
    )
    positions = ExplorationSpellPositions.model_validate(
        dict(
            source_cell="0,0",
            target_cell="1,0",
            visual_occupants=tuple(
                {"target_kind": kind, "target_id": target_id, "cell_id": "1,0"}
                for kind, target_id in [
                    ("creature", "second"),
                    ("object", "scenery"),
                    ("creature", HERO),
                    ("creature", FOE),
                    ("creature", "second"),
                ]
            ),
        )
    )
    preparation = prepare_environmental_spell(payload, HERO, positions, BundledAssetLoader())
    assert preparation.actor_ids == actor_ids
    assert preparation.schema_version == schema_version


def _faerie_wire():
    """Build a cast that includes a creature and an object."""
    wire, loader = _wire("faerie-fire", point=True, caster_class="druid")
    wire["schema_version"] = FAERIE_FIRE_SCHEMA_VERSION
    wire["state_snapshot"]["character_states"][0]["wisdom"] = 16
    wire["spell_positions"]["visual_occupants"] = [
        {"target_kind": "object", "target_id": "scenery", "cell_id": "1,0"},
        {"target_kind": "creature", "target_id": FOE, "cell_id": "1,0"},
    ]
    return wire, loader


def test_faerie_preparation_requires_a_typed_area_save_for_occupant_facts():
    """Keep the Faerie Fire wire while deriving creature scope from its activity."""
    wire, loader = _faerie_wire()
    request = _request(wire)
    spell = loader.get_spell("faerie-fire")
    spell.activities.clear()
    preparation = prepare_environmental_spell(
        request.payload, HERO, request.spell_positions, MemoryAssetLoader(spells=[spell])
    )
    assert preparation.schema_version == FAERIE_FIRE_SCHEMA_VERSION
    assert preparation.actor_ids == (HERO,)


def test_faerie_fire_uses_its_bound_activity_and_exact_rng():
    """Use the druid's spell DC, rolling once for the creature and paying once."""
    wire, loader = _faerie_wire()
    request = _request(wire)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    intent, cast, save = result.proposed_events
    assert isinstance(intent, IntentSubmitted)
    assert isinstance(cast, SpellCast)
    assert isinstance(save, SaveRolled)
    assert (save.target_id, save.ability, save.dc) == (FOE, "dex", 13)
    expected_rng = request.rng_context.state.restore()
    assert save.natural == expected_rng.randint(1, 20)
    assert result.rng_transition == RNGTransition.between(request.rng_context, expected_rng)
    spend, visual = result.state_delta.operations
    assert isinstance(spend, ResourceUpdate)
    assert (spend.expected.current, spend.value.current) == (2, 1)
    assert isinstance(visual, WorldVisualEffectCreate)
    assert [target.target_id for target in visual.targets] == (
        ["scenery"] if save.succeeded else [FOE, "scenery"]
    )
    RuleEvaluationResult.model_validate_json(result.model_dump_json()).verify_request(request)


@pytest.mark.parametrize("use", [True, False])
def test_faerie_continuation_keeps_original_save_and_exact_grant(use, monkeypatch):
    """A failed save pauses once, then uses only the held die on the original RNG path."""
    wire, _ = _faerie_wire()
    wire["state_snapshot"]["character_states"][1]["dexterity"] = 1
    earlier = dict(wire["state_snapshot"]["character_states"][1])
    earlier.update(entity_id="a-success", dexterity=20)
    wire["state_snapshot"]["character_states"].append(earlier)
    wire["spell_positions"]["visual_occupants"].append(
        {"target_kind": "creature", "target_id": "a-success", "cell_id": "1,0"}
    )
    canonical = BundledAssetLoader()
    feature = canonical.get_feature("bardic-inspiration")
    grant = effect_state(
        passive_effect_to_active_effect(
            feature.passive_effects[0],
            target_id=FOE,
            caster_id="bard",
            extra_flags={"bardic_inspiration": {"grant_id": "grant-1", "die_sides": 6}},
        )
    )
    wire["state_snapshot"]["effect_states"] = [grant.model_dump(mode="json")]
    loader = freeze_ruleset(
        MemoryAssetLoader(
            items=[canonical.get_item("component-pouch")],
            spells=[canonical.get_spell("faerie-fire")],
            classes=[canonical.get_class("druid")],
            features=[feature],
        )
    )
    wire["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    request = _request(wire)
    pending = evaluate_bound_faerie_step(request, loader=loader)
    assert pending.pending_target_id == FOE
    assert pending.result is None
    assert len(pending.steps) == 2
    assert pending.steps[0].target_id == "a-success"
    assert pending.steps[0].original.succeeded is True
    original = pending.steps[1].original
    assert original.succeeded is False
    from dnd5e_engine import evaluation_environmental_spells as module

    original_resolver = module.resolve_snapshot_save
    monkeypatch.setattr(module, "resolve_snapshot_save", lambda *a, **k: pytest.fail("redraw"))
    complete = evaluate_bound_faerie_step(
        request, prior_steps=pending.steps, choice=use, choice_die_sides=6, loader=loader
    )
    assert complete.pending_target_id is None
    assert complete.result is not None
    assert complete.steps[0] == pending.steps[0]
    assert complete.steps[1].original == original
    assert complete.steps[1].final is not None
    assert complete.spent_grants == ((grant,) if use else ())
    monkeypatch.setattr(module, "resolve_snapshot_save", original_resolver)
    assert verify_bound_faerie_result(complete.result, request, complete.steps, loader=loader) == (
        (grant,) if use else ()
    )
    altered = replace(complete.steps[1], original=complete.steps[0].original)
    with pytest.raises(ValueError, match="original save differs"):
        verify_bound_faerie_result(
            complete.result, request, (complete.steps[0], altered), loader=loader
        )


def test_faerie_success_with_held_grant_has_no_choice_or_spend():
    """An already successful save cannot open a Bardic choice."""
    wire, _ = _faerie_wire()
    wire["state_snapshot"]["character_states"][1]["dexterity"] = 20
    canonical = BundledAssetLoader()
    feature = canonical.get_feature("bardic-inspiration")
    grant = effect_state(
        passive_effect_to_active_effect(
            feature.passive_effects[0],
            target_id=FOE,
            caster_id="bard",
            extra_flags={"bardic_inspiration": {"grant_id": "grant-1", "die_sides": 6}},
        )
    )
    wire["state_snapshot"]["effect_states"] = [grant.model_dump(mode="json")]
    loader = freeze_ruleset(
        MemoryAssetLoader(
            items=[canonical.get_item("component-pouch")],
            spells=[canonical.get_spell("faerie-fire")],
            classes=[canonical.get_class("druid")],
            features=[feature],
        )
    )
    wire["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    request = _request(wire)
    progress = evaluate_bound_faerie_step(request, loader=loader)
    assert progress.pending_target_id is None
    assert progress.result is not None
    assert progress.spent_grants == ()
    assert progress.steps[0].original.succeeded is True
    assert verify_bound_faerie_result(progress.result, request, progress.steps, loader=loader) == ()


def _attached_save_effect(slug, caster_id):
    """Construct the same typed target application as the canonical spell effect."""
    spell = BundledAssetLoader().get_spell(slug)
    return effect_state(
        passive_effect_to_active_effect(
            spell.passive_effects[0], target_id=FOE, caster_id=caster_id, concentration=True
        )
    ).model_dump(mode="json")


def _faerie_save_loader(wire, *, include_bless=True, items=()):
    """Bind the fixed cast and every reviewed asset for the supplied save sources."""
    canonical = BundledAssetLoader()
    spells = ["faerie-fire", "bane", "bless"] if include_bless else ["faerie-fire", "bane"]
    names = {
        name
        for actor in wire["state_snapshot"]["character_states"]
        for record in actor["conditions"]
        for name in (
            record["condition"],
            *(
                implied.value
                for implied in CONDITION_IMPLIES.get(Condition(record["condition"]), [])
            ),
        )
    }
    loader = freeze_ruleset(
        MemoryAssetLoader(
            items=[canonical.get_item("component-pouch"), *items],
            spells=[canonical.get_spell(slug) for slug in spells],
            classes=[canonical.get_class("druid")],
            traits=[canonical.get_trait("magic-resistance")],
            conditions=[canonical.get_condition(name) for name in sorted(names)],
        )
    )
    wire["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    return loader


@pytest.mark.parametrize("held", [False, True])
def test_faerie_save_uses_bound_held_weapon_and_verifies_with_same_loader(held):
    """A carried authored weapon affects the save only while its exact instance is held."""
    wire, _ = _faerie_wire()
    wire["rng_context"]["state"] = RNGState.capture(Random(16)).model_dump(mode="json")
    slug, instance = "fixture-warding-quarterstaff", "staff.1"
    canonical = BundledAssetLoader()
    staff = canonical.get_weapon("quarterstaff").model_copy(
        update={
            "slug": slug,
            "name": "Fixture Warding Quarterstaff",
            "passive_effects": [
                PassiveEffect(
                    _id="fixture-save-bonus",
                    name="Held save bonus",
                    changes=[
                        PassiveEffectChange(key="system.bonuses.abilities.save", mode=2, value="+1")
                    ],
                )
            ],
        }
    )
    target = wire["state_snapshot"]["character_states"][1]
    target["carried_item_slugs"].append(slug)
    if held:
        target.update(
            held_weapon_instance_id=instance, weapon_in_hands=slug, weapon_grip="one_handed"
        )
    wire["state_snapshot"]["inventory_state"]["entries"].append(
        dict(
            instance_id=instance,
            owner_id=FOE,
            item_slug=slug,
            quantity=1,
            charges_remaining_per_unit=0,
            accessible=True,
        )
    )
    loader = _faerie_save_loader(wire, items=(staff,))
    request = _request(wire)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    save = result.proposed_events[-1]
    equipment = [
        modifier
        for modifier in save.roll_detail.modifiers
        if modifier.source.startswith("equipment:")
    ]
    assert [(part.source, part.value) for part in equipment] == (
        [(f"equipment:{instance}:{slug}", 1)] if held else []
    )
    assert (save.natural, save.dc, save.roll_total, save.succeeded) == (
        12,
        13,
        13 if held else 12,
        held,
    )
    restored = RuleEvaluationResult.model_validate_json(result.model_dump_json())
    restored.verify_request(request, loader=loader)
    if held:
        with pytest.raises(ValueError):
            restored.verify_request(request)


@pytest.mark.parametrize("latest_ended", [False, True])
def test_faerie_fire_tracks_distinct_spells_and_latest_same_name_effect(latest_ended):
    """Roll both families in effect order with one contributor per family."""
    wire, _ = _faerie_wire()
    snapshot = wire["state_snapshot"]
    older = _attached_save_effect("bless", "old-caster")
    bane = _attached_save_effect("bane", "bane-caster")
    newer = _attached_save_effect("bless", "new-caster")
    disabled_bane = {
        **_attached_save_effect("bane", "disabled-caster"),
        "disabled": True,
    }
    snapshot["effect_states"] = [older, bane, disabled_bane]
    if not latest_ended:
        snapshot["effect_states"].append(newer)
    snapshot["character_states"][1].update(
        dexterity=10,
        save_proficiencies=[],
        conditions=[condition("restrained").model_dump(mode="json")],
    )
    loader = _faerie_save_loader(wire)
    request = _request(wire)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    save = result.proposed_events[-1]
    expected_rng = request.rng_context.state.restore()
    d20_faces = [expected_rng.randint(1, 20) for _ in range(2)]
    assert save.roll_detail.d20.faces == d20_faces
    assert save.natural == min(d20_faces)
    sources = ((older, 1), (bane, -1)) if latest_ended else ((bane, -1), (newer, 1))
    expected_dice = []
    for effect, sign in sources:
        face = expected_rng.randint(1, 4)
        expected_dice.append(
            RollDieModifier(
                source=f"{effect['id']}@{effect['origin']}", sides=4, face=face, value=sign * face
            )
        )
    assert save.roll_detail.modifiers[2:] == expected_dice
    assert save.roll_total == save.natural + sum(die.value for die in expected_dice)
    assert result.rng_transition == RNGTransition.between(request.rng_context, expected_rng)
    copy = RuleEvaluationResult.model_validate_json(result.model_dump_json())
    copy.verify_request(request)
    altered = copy.proposed_events[-1].roll_detail.modifiers[-1]
    copy.proposed_events[-1].roll_detail.modifiers[-1] = altered.model_copy(
        update={"source": "effect:unrelated"}
    )
    with pytest.raises(ValueError, match="differs from fixed occupants and RNG"):
        copy.verify_request(request)


@pytest.mark.parametrize("name", ["restrained", "paralyzed"])
def test_faerie_fire_magic_resistance_cancels_restraining_but_not_automatic_failure(name):
    """Magic Resistance participates in the d20 test, if the condition permits rolling."""
    wire, _ = _faerie_wire()
    snapshot = wire["state_snapshot"]
    snapshot["effect_states"] = [_attached_save_effect("bless", "save-caster")]
    snapshot["character_states"][1].update(
        entity_type="NPC",
        trait_mechanics=["magic_resistance"],
        conditions=[condition(name).model_dump(mode="json")],
    )
    loader = _faerie_save_loader(wire)
    request = _request(wire)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    save = result.proposed_events[-1]
    expected_rng = request.rng_context.state.restore()
    assert save.advantage == "normal"
    if name == "restrained":
        assert set(save.sources) == {"trait", "condition:target"}
        assert save.roll_detail.d20.faces == [expected_rng.randint(1, 20)]
        assert save.roll_detail.modifiers[-1].face == expected_rng.randint(1, 4)
    else:
        assert save.roll_detail.status == "automatic_failure"
        assert not save.succeeded
        assert save.roll_detail.d20 is None
        assert save.roll_detail.modifiers == []
    assert result.rng_transition == RNGTransition.between(request.rng_context, expected_rng)


@pytest.mark.parametrize("problem", ["wrong_die", "missing_asset", "unknown_trait"])
def test_faerie_fire_rejects_unreviewed_save_source_before_any_target_roll(monkeypatch, problem):
    """Refuse even an unsupported modifier on a target ordered after another creature."""
    wire, _ = _faerie_wire()
    wire["spell_positions"]["visual_occupants"].append(
        {"target_kind": "creature", "target_id": HERO, "cell_id": "1,0"}
    )
    effect = _attached_save_effect("bless", "save-caster")
    if problem == "wrong_die":
        effect["changes"][0]["value"] = "1d6"
    if problem == "unknown_trait":
        wire["state_snapshot"]["character_states"][1]["trait_mechanics"] = ["pack_tactics"]
    wire["state_snapshot"]["effect_states"] = [effect]
    loader = _faerie_save_loader(wire, include_bless=problem != "missing_asset")
    request = _request(wire)
    monkeypatch.setattr(RNGState, "restore", lambda *a: pytest.fail("preflight accessed RNG"))
    result = execute(request, loader)
    assert result.status == "unsupported", result.error
    assert result.error.code == "spell.visual_targets"
    assert "RUO-54-F1" in result.error.reason
    assert result.proposed_events == ()
    assert result.rng_transition is None
    assert result.state_delta is None


@pytest.mark.parametrize("change", ["ability", "dc"])
def test_faerie_fire_rejects_bound_unreviewed_save_activity(change):
    """A consistent ruleset binding alone cannot admit a changed save profile."""
    wire, original_loader = _faerie_wire()
    original = execute(_request(wire), original_loader)
    assert original.status == "accepted"
    canonical = BundledAssetLoader()
    spell = canonical.get_spell("faerie-fire")
    spell_data = spell.model_dump()
    save = spell_data["activities"][0]["save"]
    if change == "ability":
        save["ability"] = ["con"]
    else:
        save["dc"].update(calculation="flat", formula="13")
    loader = freeze_ruleset(
        MemoryAssetLoader(
            items=[canonical.get_item("component-pouch")],
            spells=[type(spell).model_validate(spell_data)],
            classes=[canonical.get_class("druid")],
        )
    )
    wire["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    request = _request(wire)
    result = execute(request, loader)
    assert (result.status, result.error.code) == ("unsupported", "spell.asset")
    assert result.state_delta is None
    assert result.proposed_events == ()
    assert result.rng_transition is None
    proposal = original.model_copy(update={"input_ruleset_binding": request.ruleset_binding})
    with pytest.raises(ValueError, match="reviewed bound spell"):
        proposal.verify_request(request, loader=loader)


def test_faerie_verifier_rejects_caster_unable_to_act():
    """A final staged result is subject to the ordinary caster admission."""
    wire, loader = _faerie_wire()
    progress = evaluate_bound_faerie_step(_request(wire), loader=loader)
    assert progress.result is not None
    caster = next(
        actor for actor in wire["state_snapshot"]["character_states"] if actor["entity_id"] == HERO
    )
    caster["hp_current"] = 0
    refused = _request(wire)
    with pytest.raises(ValueError, match="fixed cast facts"):
        verify_bound_faerie_result(progress.result, refused, progress.steps, loader=loader)


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


@pytest.mark.parametrize("recorded_level, accepted", [(1, True), (0, False)])
def test_generic_dispel_verifies_bound_overlay_victim(recorded_level, accepted):
    """Replay an admitted overlay victim without resolving it from bundled assets."""
    wire, _ = _dispel_wire()
    canonical = BundledAssetLoader()
    victim = canonical.get_spell("longstrider").model_copy(
        update={"slug": "fixture-keen-perception"}
    )
    victim_wire, victim_loader = spell_wire(canonical.get_spell("longstrider"))
    victim_result = execute(_request(victim_wire), victim_loader)
    effect = victim_result.state_delta.operations[-1].value.effect.model_dump(mode="json")
    wire["schema_version"] = DISPEL_MAGIC_SCHEMA_VERSION
    wire["payload"]["target"] = {"kind": "creature", "target_id": FOE}
    positions = wire["spell_positions"]
    positions.pop("target_effect")
    positions["visible_target_id"] = FOE
    positions["target_applications"] = [
        dict(
            effect=effect,
            caster_id=HERO,
            spell_id=victim.slug,
            cast_command_id="cast:overlay",
            expires_tick=600,
            concentration_id=None,
            cast_slot_level=recorded_level,
        )
    ]
    wire["state_snapshot"]["effect_states"] = [effect]
    loader = freeze_ruleset(
        MemoryAssetLoader(
            spells=[canonical.get_spell("dispel-magic"), victim],
            classes=[canonical.get_class("wizard")],
            items=[canonical.get_item("component-pouch")],
        )
    )
    wire["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    request = _request(wire)
    result = execute(request, loader)
    if accepted:
        assert result.status == "accepted", result.error
        assert result.state_delta.operations[1].expected.spell_id == victim.slug
        RuleEvaluationResult.model_validate_json(result.model_dump_json()).verify_request(request)
    else:
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
