"""Public exploration spell proposals over bound local casting facts."""

import json

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd5e_srd_data.schema.spell import Spell
from pydantic import ValidationError

from dnd5e_engine.evaluation_contracts import (
    SCHEMA_VERSION,
    CombatIntentPayload,
    RuleEvaluationRequest,
    RuleEvaluationResult,
    SpellWillingnessChoice,
)
from dnd5e_engine.evaluation_delta import ResourceUpdate, WorldEffectCreate
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_ruleset import freeze_ruleset, ruleset_binding
from dnd5e_engine.evaluation_speed_effects import world_walk_bonus
from dnd5e_engine.evaluation_world_spell_modifiers import world_spell_combat_modifiers
from dnd5e_engine.events import AttackRolled, EffectApplied, IntentSubmitted, SpellCast
from dnd5e_engine.reaction_effect_rules import effective_armor_class
from tests.evaluation_support import FOE, HERO, WEAPON
from tests.test_stateless_attack import execute, request_and_live

_POUCH = "inventory:component-pouch"


def spell_wire(
    spell: Spell, *, target_id: str = FOE, caster_class: str = "wizard", caster_level: int = 1
):
    """Build fixed /20 exploration facts for one prepared caster and a local target."""
    canonical = BundledAssetLoader()
    class_asset = canonical.get_class(caster_class)
    ability = class_asset.spellcasting.ability
    source_id = f"class:{caster_class}"
    combat, _, _ = request_and_live()
    wire = json.loads(combat.model_dump_json())
    wire.update(
        schema_version="engine-evaluation/29",
        operation_kind="world.cast_spell",
        payload={
            "kind": "world.cast_spell",
            "spell_id": spell.slug,
            "spell_source_id": source_id,
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
        class_slug=caster_class,
        classes={caster_class: caster_level},
        character_level=caster_level,
        spellcasting_ability=ability,
        spell_slots={spell.level: 2},
        carried_item_slugs=["component-pouch"],
    )
    snapshot["resource_state"] = {
        "pools": [
            {
                "owner_id": HERO,
                "pool_id": f"slots:{spell.level}",
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
                        "source_id": source_id,
                        "class_slug": caster_class,
                        "ability": ability,
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
    loader = freeze_ruleset(
        MemoryAssetLoader(
            items=[canonical.get_item("component-pouch")],
            spells=[spell],
            classes=[class_asset],
        )
    )
    wire["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    return wire, loader


def _longstrider() -> Spell:
    """Read the example spell from the bundled rules."""
    return BundledAssetLoader().get_spell("longstrider")


@pytest.mark.parametrize("schema_version", ["engine-evaluation/29", SCHEMA_VERSION])
def test_timed_touch_spell_proposes_one_paid_effect_and_replays(schema_version) -> None:
    """Publish only guarded slot/effect operations and deterministic cast evidence."""
    wire, loader = spell_wire(_longstrider())
    wire["schema_version"] = schema_version
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


def _darkvision_wire(*, target_id: str = FOE):
    """Build a fixed exploration cast with a Wizard able to prepare a second-level spell."""
    spell = BundledAssetLoader().get_spell("darkvision")
    wire, loader = spell_wire(spell, target_id=target_id)
    caster = wire["state_snapshot"]["character_states"][0]
    caster.update(classes={"wizard": 3}, character_level=3)
    return wire, loader


@pytest.mark.parametrize(("target_id", "verified"), [(HERO, False), (FOE, True), (FOE, False)])
def test_canonical_darkvision_requires_willing_target(target_id, verified, monkeypatch) -> None:
    """Resolve self or a verified target and request missing willingness before payment."""
    wire, loader = _darkvision_wire(target_id=target_id)
    if verified:
        wire["spell_positions"]["willing_target_id"] = target_id
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    if target_id != HERO and not verified:
        monkeypatch.setattr(
            "dnd5e_engine.evaluation_timed_spells.spell_payment",
            lambda *_: pytest.fail("payment evaluated before willingness was established"),
        )

    result = execute(request, loader)

    if target_id != HERO and not verified:
        assert result.status == "needs_choice"
        assert result.choice == SpellWillingnessChoice(
            kind="spell.target_willingness", actor_id=HERO, target_id=FOE
        )
        assert result.error is None
        assert result.state_delta is None
        assert result.rng_transition is None
        assert result.proposed_events == ()
        assert result.stage_facts is None
        assert result == RuleEvaluationResult.model_validate_json(result.model_dump_json())
        result.verify_request(request)
        return

    assert result.status == "accepted", result.error
    payment, creation = result.state_delta.operations
    assert isinstance(payment, ResourceUpdate)
    assert payment.owner_id == HERO
    assert payment.expected.level == payment.value.level == 2
    assert payment.pool_id == "slots:2"
    assert payment.amount == -1
    assert (payment.expected.current, payment.value.current) == (2, 1)
    assert isinstance(creation, WorldEffectCreate)
    assert creation.value.identity == (target_id, "effect:darkvision", f"cast:darkvision:{HERO}")
    assert creation.value.duration_seconds == 8 * 3600
    assert creation.value.effect.duration.rounds is None
    assert creation.value.expiry.action == "remove"
    assert not creation.value.effect.disabled
    (change,) = creation.value.effect.changes
    assert (change.key, change.mode, change.value) == (
        "system.attributes.senses.darkvision",
        "upgrade",
        "150",
    )
    intent, cast, applied = result.proposed_events
    assert isinstance(intent, IntentSubmitted)
    assert (intent.actor_id, intent.target_id) == (HERO, target_id)
    assert isinstance(cast, SpellCast)
    assert (cast.spell_id, cast.slot_level) == ("darkvision", 2)
    assert isinstance(applied, EffectApplied)
    assert effect_state(applied.effect) == creation.value.effect
    result.rng_transition.verify_input(request.rng_context)
    assert result.rng_transition.next_state == request.rng_context.state
    assert not result.rng_transition.state_changed


def _barkskin_wire():
    """Use the actual Druid and original canonical bonus-action spell."""
    spell = BundledAssetLoader().get_spell("barkskin")
    return spell_wire(spell, caster_class="druid", caster_level=3)


def test_canonical_barkskin_waits_then_proposes_one_paid_armor_floor(monkeypatch) -> None:
    """Admit the canonical touch spell while retaining the pre-payment consent seam."""
    wire, loader = _barkskin_wire()
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    with monkeypatch.context() as waiting:
        waiting.setattr(
            "dnd5e_engine.evaluation_timed_spells.spell_payment",
            lambda *_: pytest.fail("payment evaluated without verified willingness"),
        )
        pending = execute(request, loader)
    assert pending.status == "needs_choice"
    assert pending.choice == SpellWillingnessChoice(
        kind="spell.target_willingness", actor_id=HERO, target_id=FOE
    )
    assert pending.state_delta is None
    assert pending.rng_transition is None
    assert pending.proposed_events == ()

    wire["spell_positions"]["willing_target_id"] = FOE
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    payment, creation = result.state_delta.operations
    assert isinstance(payment, ResourceUpdate)
    assert (payment.pool_id, payment.amount, payment.expected.current, payment.value.current) == (
        "slots:2",
        -1,
        2,
        1,
    )
    assert isinstance(creation, WorldEffectCreate)
    assert creation.value.identity == (FOE, "effect:bark-like_skin", f"cast:bark-like_skin:{HERO}")
    assert creation.value.duration_seconds == 3600
    assert creation.value.expiry.action == "remove"
    (change,) = creation.value.effect.changes
    assert (change.key, change.mode, change.value) == ("system.attributes.ac.min", "upgrade", "17")
    _, cast, applied = result.proposed_events
    assert isinstance(cast, SpellCast)
    assert (cast.spell_id, cast.slot_level) == ("barkskin", 2)
    assert isinstance(applied, EffectApplied)
    assert effect_state(applied.effect) == creation.value.effect
    result.verify_request(request)
    assert not result.rng_transition.state_changed

    # Ordinary AC already includes the +2 from Half Cover in the last example.
    assert effective_armor_class(14, (creation.value.effect,)) == 17
    assert effective_armor_class(19, (creation.value.effect,)) == 19
    assert effective_armor_class(16 + 2, (creation.value.effect,)) == 18
    assert effective_armor_class(14, ()) == 14
    bonus = creation.value.effect.model_copy(
        update={
            "changes": [change.model_copy(update={"key": "ac.bonus", "mode": "add", "value": "+2"})]
        }
    )
    assert effective_armor_class(16, (creation.value.effect, bonus)) == 18
    assert effective_armor_class(13, (creation.value.effect, bonus)) == 17


def test_canonical_barkskin_changes_ordinary_attack_outcome() -> None:
    """Apply the emitted effect in the normal attack evaluator without changing base AC."""
    cast_wire, loader = _barkskin_wire()
    cast_wire["spell_positions"]["willing_target_id"] = FOE
    cast = execute(RuleEvaluationRequest.model_validate_json(json.dumps(cast_wire)), loader)
    assert cast.status == "accepted", cast.error
    creation = cast.state_delta.operations[1]
    assert isinstance(creation, WorldEffectCreate)

    combat, _, live = request_and_live(seed=7, ac=14)
    wire = combat.model_dump(mode="json")
    wire["schema_version"] = "engine-evaluation/28"
    snapshot = wire["state_snapshot"]
    snapshot["snapshot_schema_version"] = "engine-snapshot/20"
    caster, target = snapshot["character_states"]
    caster.pop("spells_known")
    target.pop("spells_known")
    caster["held_weapon_instance_id"] = "weapon:held"
    snapshot["inventory_state"]["entries"] = [
        {
            "instance_id": "weapon:held",
            "owner_id": HERO,
            "item_slug": WEAPON,
            "quantity": 1,
            "charges_remaining_per_unit": 0,
            "accessible": True,
        }
    ]
    wire["payload"] = CombatIntentPayload(
        intent_type="attack", weapon_instance_id="weapon:held", target_id=FOE
    ).model_dump(mode="json")
    original = execute(
        RuleEvaluationRequest.model_validate_json(json.dumps(wire)), live.ruleset_loader
    )
    assert original.status == "accepted", original.error
    snapshot["effect_states"] = [creation.value.effect.model_dump(mode="json")]
    protected = execute(
        RuleEvaluationRequest.model_validate_json(json.dumps(wire)), live.ruleset_loader
    )
    assert protected.status == "accepted", protected.error
    baseline_roll = next(
        event for event in original.proposed_events if isinstance(event, AttackRolled)
    )
    protected_roll = next(
        event for event in protected.proposed_events if isinstance(event, AttackRolled)
    )
    assert baseline_roll.roll_total == protected_roll.roll_total == 16
    assert baseline_roll.is_hit
    assert not protected_roll.is_hit
    assert target["ac"] == 14


def test_timed_walk_profile_keeps_armor_floor_outside_its_dependencies() -> None:
    """Reject a combined floor when the speed profile has no Armor Class consumer."""
    data = _longstrider().model_dump(mode="json")
    data["passive_effects"][0]["changes"].append(
        {"key": "system.attributes.ac.min", "mode": 4, "value": "17"}
    )
    wire, loader = spell_wire(Spell.model_validate(data), target_id=HERO)
    cast = execute(RuleEvaluationRequest.model_validate_json(json.dumps(wire)), loader)
    assert cast.status == "accepted", cast.error
    creation = cast.state_delta.operations[1]
    assert isinstance(creation, WorldEffectCreate)
    with pytest.raises(ValueError, match="timed walking cannot compose an Armor Class floor"):
        world_walk_bonus(creation.value.effect)


@pytest.mark.parametrize(
    ("location", "willing_target_id", "reason"),
    [
        ("spell_positions", HERO, "must match the spell's target_id"),
        ("spell_positions", True, "valid string"),
        ("spell_positions", "", "at least 1 character"),
        ("payload", FOE, "Extra inputs are not permitted"),
    ],
)
def test_invalid_willingness_cannot_enter_evaluation(location, willing_target_id, reason) -> None:
    """Require exact host evidence and keep the cast payload closed."""
    wire, _ = _darkvision_wire()
    wire[location]["willing_target_id"] = willing_target_id
    with pytest.raises(ValueError, match=reason):
        RuleEvaluationRequest.model_validate_json(json.dumps(wire))


@pytest.mark.parametrize(("field", "bad_id"), [("target_id", HERO), ("actor_id", FOE)])
def test_willingness_choice_must_match_the_original_cast(field: str, bad_id: str) -> None:
    """Reject a serialized choice that identifies a different caster or responder."""
    wire, loader = _darkvision_wire()
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    result_wire = json.loads(execute(request, loader).model_dump_json())
    result_wire["choice"][field] = bad_id
    mismatched = RuleEvaluationResult.model_validate_json(json.dumps(result_wire))
    with pytest.raises(ValueError, match="must match an unverified cast target"):
        mismatched.verify_request(request)


@pytest.mark.parametrize("verified", [False, True])
def test_willingness_does_not_override_touch_admission(verified: bool) -> None:
    """Return the ordinary reach refusal before asking for or acting on consent."""
    wire, loader = _darkvision_wire()
    wire["spell_positions"]["target_cell"] = "99,99"
    if verified:
        wire["spell_positions"]["willing_target_id"] = FOE
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    result = execute(request, loader)
    assert result.status == "rejected"
    assert result.error.code == "target_invalid"
    assert result.choice is None
    assert result.state_delta is None
    assert result.rng_transition is None
    assert result.proposed_events == ()


def test_willingness_requirement_follows_authored_target_type() -> None:
    """Use the same preflight choice for a different, otherwise admitted utility spell."""
    data = _longstrider().model_dump(mode="json")
    data["activities"][0]["target"]["affects"]["type"] = "willing"
    wire, loader = spell_wire(Spell.model_validate(data))
    wire["schema_version"] = SCHEMA_VERSION
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    result = execute(request, loader)
    assert result.status == "needs_choice"
    assert isinstance(result.choice, SpellWillingnessChoice)

    wire["spell_positions"]["willing_target_id"] = FOE
    verified_request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    verified_result = execute(verified_request, loader)
    assert verified_result.status == "accepted", verified_result.error
    payment, creation = verified_result.state_delta.operations
    assert payment.amount == -1
    assert creation.value.identity == (FOE, "effect:longstrider", f"cast:longstrider:{HERO}")


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


@pytest.mark.parametrize("with_movement", [False, True])
def test_timed_spell_proposes_a_flat_skill_check_bonus(with_movement: bool) -> None:
    """Pass a skill bonus and optional movement change through one typed timed effect."""
    data = _longstrider().model_dump(mode="json")
    data.update(slug="keen-notice-test", name="Keen Notice", foundry_uuid="")
    changes = [{"key": "check.skill.perception.bonus", "mode": 2, "value": "+2"}]
    if with_movement:
        changes.extend(data["passive_effects"][0]["changes"])
    data["passive_effects"][0].update(
        name="Keen Notice",
        changes=changes,
    )
    wire, loader = spell_wire(Spell.model_validate(data))
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))

    result = execute(request, loader)

    assert result.status == "accepted", result.error
    payment, creation = result.state_delta.operations
    assert isinstance(payment, ResourceUpdate)
    assert payment.amount == -1
    assert isinstance(creation, WorldEffectCreate)
    assert creation.value.identity == (FOE, "effect:keen_notice", f"cast:keen_notice:{HERO}")
    assert creation.value.duration_seconds == 3600
    assert [
        (change.key, change.mode, change.value) for change in creation.value.effect.changes
    ] == [(change["key"], "add", change["value"]) for change in changes]
    result.rng_transition.verify_input(request.rng_context)
    assert not result.rng_transition.state_changed


def test_elapsed_concentration_binds_the_proposal_and_effect_flag() -> None:
    """Admit a flat authored skill bonus and reject mismatched or extra flags."""
    data = _longstrider().model_dump(mode="json")
    data.update(slug="keen-focus-test", name="Keen Focus", concentration=True, foundry_uuid="")
    data["passive_effects"][0].update(
        name="Keen Focus",
        changes=[{"key": "check.skill.perception.bonus", "mode": 2, "value": "+2"}],
    )
    wire, loader = spell_wire(Spell.model_validate(data))
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    result.verify_request(request)
    payment, creation = result.state_delta.operations
    assert isinstance(payment, ResourceUpdate)
    assert payment.amount == -1
    assert isinstance(creation, WorldEffectCreate)
    assert creation.value.concentration
    assert creation.value.effect.flags == {"concentration": True}
    assert not result.rng_transition.state_changed

    forged = creation.model_dump(mode="json")
    forged["value"]["concentration"] = False
    with pytest.raises(ValidationError, match="concentration differs"):
        WorldEffectCreate.model_validate_json(json.dumps(forged))
    forged["value"]["concentration"] = True
    forged["value"]["effect"]["flags"]["other"] = True
    with pytest.raises(ValidationError, match="world-time-only passive effect"):
        WorldEffectCreate.model_validate_json(json.dumps(forged))


@pytest.mark.parametrize("another_caster", [False, True])
def test_longstrider_overlaps_remain_independent_and_grant_one_walk_bonus(
    another_caster: bool,
) -> None:
    """Admit a second reviewed cast without stacking its walking contribution."""
    wire, loader = spell_wire(_longstrider())
    initial_request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    initial = execute(initial_request, loader)
    assert initial.status == "accepted"
    first = initial.state_delta.operations[1].value.effect
    if another_caster:
        first = first.model_copy(update={"origin": "cast:longstrider:another-caster"})
    wire["state_snapshot"]["effect_states"].append(first.model_dump(mode="json"))
    wire["command_id"] = "recast-longstrider"
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    result = execute(request, loader)

    assert result.status == "accepted"
    payment, creation = result.state_delta.operations
    assert isinstance(payment, ResourceUpdate)
    assert isinstance(creation, WorldEffectCreate)
    assert payment.expected.current - payment.value.current == 1
    assert not result.rng_transition.state_changed
    second = creation.value.effect
    assert second.origin != first.origin
    assert second == first.model_copy(update={"origin": second.origin})
    if another_caster:
        assert second.origin == f"cast:longstrider:{HERO}"
    assert world_spell_combat_modifiers((first, second)) == {FOE: (10, None)}
    assert world_spell_combat_modifiers((second,)) == {FOE: (10, None)}
    assert world_spell_combat_modifiers((first,)) == {FOE: (10, None)}
    with pytest.raises(ValueError, match="overlapping combat modifiers"):
        world_spell_combat_modifiers((first, second, first))


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
            prior_effect["changes"][0]["value"] = "11"
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
        (
            ("passive_effects", 0, "changes", 0),
            {"key": "check.skill.unknown.bonus", "mode": 2, "value": "+2"},
        ),
        (
            ("passive_effects", 0, "changes", 0),
            {"key": "check.wis.bonus", "mode": 2, "value": "+2"},
        ),
        (
            ("passive_effects", 0, "changes", 0),
            {"key": "check.skill.perception.bonus", "mode": 4, "value": "+2"},
        ),
        (
            ("passive_effects", 0, "changes", 0),
            {"key": "check.skill.perception.bonus", "mode": 2, "value": "+1d4"},
        ),
        (
            ("passive_effects", 0, "changes", 0),
            {"key": "check.skill.perception.bonus", "mode": 2, "value": "@prof"},
        ),
        (
            ("passive_effects", 0, "changes", 0),
            {"key": "check.skill.perception.bonus", "mode": 2, "value": "2.5"},
        ),
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
