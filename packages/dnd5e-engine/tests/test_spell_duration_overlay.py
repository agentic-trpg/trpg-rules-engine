"""Exercise duration overlays through public cast and long-rest evaluation."""

import json
from pathlib import Path

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from pydantic import ValidationError

from dnd5e_engine.evaluation_aid import aid_profile
from dnd5e_engine.evaluation_contracts import RestPayload, RuleEvaluationRequest
from dnd5e_engine.evaluation_delta import HPDelta, WorldRestEffectEnd
from dnd5e_engine.evaluation_effects import EffectDurationState, effect_state
from dnd5e_engine.evaluation_rest import rest_inert_elapsed_effects
from dnd5e_engine.evaluation_ruleset import (
    FixedSpellDuration,
    RulesetBindingError,
    RulesetOverlay,
    freeze_ruleset,
)
from dnd5e_engine.evaluation_spell_termination import SpellApplicationFacts, plan_spell_dismissal
from dnd5e_engine.evaluation_timed_spells import verify_timed_spell_effect
from dnd5e_engine.evaluation_world_effect import (
    WorldEffectApplicationRequest,
    plan_longstrider_application,
    world_spell_duration,
)
from dnd5e_engine.evaluation_world_spell_modifiers import world_spell_combat_modifiers
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import execute
from tests.test_stateless_healing_modifiers import BEACON, _beacon_cast
from tests.test_stateless_long_rest_payments import long_case
from tests.test_stateless_timed_spells import spell_wire


def project_overlay():
    config = Path(__file__).resolve().parents[3] / "examples" / "project-duration-overlay.json"
    return RulesetOverlay.model_validate_json(config.read_text(encoding="utf-8"))


def cast_request(spell, loader, wire):
    wire["ruleset_binding"] = loader.binding.model_dump(mode="json")
    if wire.get("spell_positions"):
        wire["spell_positions"]["willing_target_id"] = wire["payload"]["target_id"]
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    creation = result.state_delta.operations[-1]
    verify_timed_spell_effect(request, spell, creation, loader=loader)
    return creation.value


@pytest.mark.parametrize(
    ("slug", "class_slug", "level", "seconds"),
    [("aid", "cleric", 3, 28800), ("longstrider", "wizard", 1, 3600)],
)
def test_public_cast_selects_its_bound_clock(slug, class_slug, level, seconds):
    source = BundledAssetLoader()
    spell = source.get_spell(slug)
    wire, _ = spell_wire(spell, caster_class=class_slug, caster_level=level)
    vanilla = freeze_ruleset(source)
    overlay = freeze_ruleset(source, overlay=project_overlay())
    assert overlay.binding.data_revision != vanilla.binding.data_revision
    ordinary = cast_request(spell, vanilla, wire)
    assert ordinary.duration_seconds == seconds
    assert "until_long_rest" not in ordinary.effect.duration.model_dump()
    project = cast_request(spell, overlay, wire)
    assert project.duration_seconds is None
    assert project.effect.duration.until_long_rest is True
    assert project.effect.changes == ordinary.effect.changes


def test_explicit_override_and_threshold_are_detached_and_bound():
    source = BundledAssetLoader()
    overlay = project_overlay()
    config = overlay.spell_durations.model_copy(
        update={
            "minimum_seconds": 28801,
            "overrides": {"longstrider": FixedSpellDuration(rounds=10)},
        }
    )
    override = overlay.model_copy(update={"spell_durations": config})
    frozen = freeze_ruleset(source, overlay=override)
    config.overrides["longstrider"] = "canonical"
    assert frozen.spell_duration_policy("aid") == "canonical"
    spell = source.get_spell("longstrider")
    wire, _ = spell_wire(spell)
    elapsed = cast_request(spell, frozen, wire)
    assert elapsed.duration_seconds == 60
    assert elapsed.effect.duration.until_long_rest is None
    assert world_spell_combat_modifiers([elapsed.effect]) == {FOE: (10, None)}
    without_override = freeze_ruleset(source, overlay=override)
    assert without_override.binding != frozen.binding
    assert cast_request(spell, without_override, wire).duration_seconds == 3600


def test_overlay_leaves_actual_concentration_cast_alone():
    source = BundledAssetLoader()
    spell = source.get_spell(BEACON)
    request, _ = _beacon_cast(FOE)
    wire = json.loads(request.model_dump_json())
    vanilla = cast_request(spell, freeze_ruleset(source), wire)
    project = cast_request(spell, freeze_ruleset(source, overlay=project_overlay()), wire)
    assert project.effect == vanilla.effect
    assert project.duration_seconds == vanilla.duration_seconds == 60
    assert project.concentration is True


def test_overlay_rejects_unknown_capability_and_missing_base_spell():
    overlay = project_overlay()
    wire = overlay.model_dump(mode="json")
    wire["spell_durations"]["applies_to"].append("barkskin")
    with pytest.raises(ValidationError, match="longstrider"):
        RulesetOverlay.model_validate_json(json.dumps(wire))
    with pytest.raises(RulesetBindingError, match="reviewed aid"):
        freeze_ruleset(MemoryAssetLoader(), overlay=overlay)


def rest_case_with_aid():
    request, base = long_case(hp=1)
    source = BundledAssetLoader()
    aid = source.get_spell("aid")
    longstrider = source.get_spell("longstrider")
    loader = freeze_ruleset(
        MemoryAssetLoader(
            items=[base.get_item(s) for s in base.list_slugs("items")],
            classes=[base.get_class(s) for s in base.list_slugs("classes")],
            features=[base.get_feature(s) for s in base.list_slugs("features")],
            spells=[aid, longstrider],
        ),
        overlay=project_overlay(),
    )
    duration = world_spell_duration(aid, loader)
    aid_states = []
    for level, caster, target in ((2, HERO, HERO), (3, FOE, HERO), (2, FOE, FOE)):
        _, effect = aid_profile(aid, level=level, caster_id=caster, target_id=target)
        aid_states.append(effect_state(effect.model_copy(update={"duration": duration})))
    walk = plan_longstrider_application(
        WorldEffectApplicationRequest(
            schema_version="engine-world-effect/1",
            session_id=request.session_id,
            command_id="cast-longstrider",
            source_id=HERO,
            target_id=HERO,
            ruleset_binding=loader.binding,
        ),
        loader=loader,
    ).effect
    snapshot = request.state_snapshot
    hero, foe = snapshot.character_states
    effects = (*aid_states, walk)
    return request.model_copy(
        update={
            "ruleset_binding": loader.binding,
            "payload": RestPayload(
                kind="rules.rest",
                target_id=HERO,
                rest_type="long",
                hit_dice=(),
                completed_at_tick=28800,
            ),
            "state_snapshot": snapshot.model_copy(
                update={
                    "character_states": (hero.model_copy(update={"hp_max": 50}), foe),
                    "effect_states": effects,
                }
            ),
        }
    ), loader


def test_long_rest_ends_exact_recipient_effects_and_recovers_to_the_final_cap_once():
    request, loader = rest_case_with_aid()
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    endings = tuple(
        op for op in result.state_delta.operations if isinstance(op, WorldRestEffectEnd)
    )
    (end,) = endings
    assert end.completed_at_tick == 28800
    assert end.expected == tuple(
        effect for effect in request.state_snapshot.effect_states if effect.target_id == HERO
    )
    assert (
        end.hp.expected_current,
        end.hp.expected_maximum,
        end.hp.current,
        end.hp.maximum,
        end.hp.old_bonus,
        end.hp.new_bonus,
        end.hp.zero_consequence,
    ) == (1, 50, 40, 40, 10, 0, "none")
    assert not any(isinstance(op, HPDelta) for op in result.state_delta.operations)
    assert result.proposed_events[0].hp_regained == 40
    assert result.proposed_events[1].amount == 40
    assert not result.rng_transition.state_changed

    tampered = end.model_copy(update={"completed_at_tick": 28801})
    operations = tuple(tampered if op is end else op for op in result.state_delta.operations)
    bad = result.model_copy(
        update={"state_delta": result.state_delta.model_copy(update={"operations": operations})}
    )
    with pytest.raises(ValueError, match="fixed completion"):
        bad.verify_request(request, loader=loader)


def test_rest_requires_completion_and_rejects_mismatched_bound_duration():
    request, loader = rest_case_with_aid()
    missing = request.model_copy(
        update={"payload": request.payload.model_copy(update={"completed_at_tick": None})}
    )
    result = execute(missing, loader)
    assert result.status == "unsupported"
    assert "completed Long Rest" in result.error.reason
    unmodified = freeze_ruleset(BundledAssetLoader())
    wrong_bound = request.model_copy(update={"ruleset_binding": unmodified.binding})
    result = execute(wrong_bound, unmodified)
    assert result.status == "unsupported"
    assert "bound duration policy" in result.error.reason


def test_rest_rejects_forged_canonical_clock_under_the_project_overlay():
    request, loader = rest_case_with_aid()
    snapshot = request.state_snapshot
    *aid, walk = snapshot.effect_states
    wrong_duration = EffectDurationState(
        seconds=3600, rounds=None, turns=None, start_round=None, start_turn=None
    )
    forged = walk.model_copy(update={"duration": wrong_duration})
    request = request.model_copy(
        update={"state_snapshot": snapshot.model_copy(update={"effect_states": (*aid, forged)})}
    )
    result = execute(request, loader)
    assert result.status == "unsupported"
    assert "bound duration policy" in result.error.reason


def test_dismissal_checks_the_exact_bound_fixed_clock():
    source = BundledAssetLoader()
    config = project_overlay()
    config.spell_durations.overrides["longstrider"] = FixedSpellDuration(rounds=10)
    loader = freeze_ruleset(source, overlay=config)
    spell = source.get_spell("longstrider")
    wire, _ = spell_wire(spell)
    applied = cast_request(spell, loader, wire)
    fact = SpellApplicationFacts(
        effect=applied.effect,
        caster_id=HERO,
        spell_id="longstrider",
        cast_command_id=applied.command_id,
        cast_slot_level=1,
        concentration_id=None,
        expires_tick=60,
    )
    kwargs = {"actor_id": HERO, "actor_conditions": (), "binding": loader.binding, "loader": loader}
    assert plan_spell_dismissal(fact, **kwargs).expected == fact
    duration = applied.effect.duration.model_copy(update={"seconds": 61})
    effect = applied.effect.model_copy(update={"duration": duration})
    with pytest.raises(ValueError, match="bound duration policy"):
        plan_spell_dismissal(fact.model_copy(update={"effect": effect}), **kwargs)


def test_elapsed_longstrider_is_inert_at_rest_but_elapsed_aid_cannot_be_removed():
    request, _ = rest_case_with_aid()
    source = BundledAssetLoader()
    config = project_overlay()
    config.spell_durations.overrides["longstrider"] = FixedSpellDuration(rounds=10)
    config.spell_durations.overrides["aid"] = "canonical"
    loader = freeze_ruleset(source, overlay=config)
    snapshot = request.state_snapshot
    effects = []
    for effect in snapshot.effect_states:
        spell = source.get_spell(effect.id.removeprefix("effect:"))
        clock = world_spell_duration(spell, loader)
        if clock is None:
            duration = effect.duration.model_copy(
                update={"seconds": 28800, "until_long_rest": None}
            )
        else:
            duration = EffectDurationState.model_validate(clock.model_dump())
        effects.append(effect.model_copy(update={"duration": duration}))
    request = request.model_copy(
        update={
            "ruleset_binding": loader.binding,
            "state_snapshot": snapshot.model_copy(update={"effect_states": tuple(effects)}),
        }
    )
    assert rest_inert_elapsed_effects(request, loader) == (effects[-1],)


def test_reviewed_life_cleric_can_restore_channel_divinity_with_the_project_rules():
    request, _ = _beacon_cast(FOE)
    wire = json.loads(request.model_dump_json())
    wire.pop("spell_positions")
    wire.update(
        operation_kind="rules.rest",
        payload={
            "kind": "rules.rest",
            "target_id": HERO,
            "rest_type": "long",
            "hit_dice": [],
            "completed_at_tick": 28800,
        },
    )
    snapshot = wire["state_snapshot"]
    actor = snapshot["character_states"][0]
    actor["custom_counters"]["feature_use:channel-divinity-cleric"]["spent"] = 2
    actor["spell_slots"]["3"] = 0
    pools = snapshot["resource_state"]["pools"]
    for pool in pools:
        if pool["pool_id"] in {"spell:3", "feature:channel-divinity-cleric"}:
            pool["current"] = 0
    pools.append(
        {
            "kind": "hit_dice",
            "owner_id": HERO,
            "pool_id": "hd:cleric",
            "current": 5,
            "maximum": 5,
            "class_slug": "cleric",
            "die_size": 8,
        }
    )
    loader = freeze_ruleset(BundledAssetLoader(), overlay=project_overlay())
    wire["ruleset_binding"] = loader.binding.model_dump(mode="json")
    result = execute(RuleEvaluationRequest.model_validate_json(json.dumps(wire)), loader)
    assert result.status == "accepted", result.error
    change = next(
        op
        for op in result.state_delta.operations
        if op.pool_id == "feature:channel-divinity-cleric"
    )
    assert change.value.current == 2

    wire["state_snapshot"]["resource_state"]["pools"] = [
        pool for pool in pools if pool["pool_id"] != "feature:channel-divinity-cleric"
    ]
    actor["custom_counters"] = {}
    missing = execute(RuleEvaluationRequest.model_validate_json(json.dumps(wire)), loader)
    assert missing.status == "unsupported"
    assert "finite feature pool closure is incomplete" in missing.error.reason
