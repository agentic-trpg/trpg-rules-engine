"""Bound feature riders: preflight, final hits, payments and effect lifetimes."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from pathlib import Path
from random import Random

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.monster import CreatureSize
from pydantic import BaseModel, ValidationError

from dnd5e_engine import ActiveEffect, PlayerIntent
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.resolver import resolve_activity
from dnd5e_engine.attack_rider_audit import audit_document
from dnd5e_engine.attack_riders import AttackRiderRequest
from dnd5e_engine.events import (
    ActorMoved,
    AttackFailed,
    AttackRiderTriggered,
    AttackRolled,
    CastFailed,
    CombatantMoved,
    ConditionApplied,
    DamageApplied,
    EffectApplied,
    EffectExpired,
    IntentSubmitted,
    ReactionTriggered,
    SaveRolled,
)
from dnd5e_engine.feature_repertoire import feature_repertoire
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.live_attack_riders import attach_attack_riders, preflight_attack_riders
from dnd5e_engine.persistent_areas import register_area
from dnd5e_engine.types.effects import ActiveEffectChange
from tests.c20_support import act, combatant, events, foe, pc, start

HERO = "char:hero"
FOE = "mon:foe"
STUN = ("stunning-strike", "Xto99a8Zt46VLwaR")
ADDLE = ("open-hand-technique", "1jdSaWanuRrdkVs3")
PUSH = ("open-hand-technique", "XoaS0RtDCGAqrQsf")
TOPPLE = ("open-hand-technique", "5Qgc0K3TfuonkPIG")
OBSCURE = ("devious-strikes", "ki4lIPVGNA0HjEzH")
TRIP = ("cunning-strike", "dWcCw1vTWRMx4YzD")
FLURRY = "2ghJTBhilLrFn9xT"


@pytest.fixture(autouse=True)
def _bundled_loader() -> Iterator[None]:
    # A new loader also isolates canonical model instances from other tests.
    set_lib_loader_for_tests(BundledAssetLoader())
    yield
    set_lib_loader_for_tests(None)


def _request(option=STUN, **fields):
    return AttackRiderRequest(feature_id=option[0], activity_id=option[1], **fields)


def _monk(**fields):
    return pc(
        **(
            {
                "class_slug": "monk",
                "character_level": 5,
                "dexterity": 16,
                "wisdom": 18,
                "strength": 10,
                "hp_current": 200,
                "hp_max": 200,
                "equipment": ("quarterstaff",),
            }
            | fields
        )
    )


def _rogue(**fields):
    return pc(
        **(
            {
                "class_slug": "rogue",
                "character_level": 14,
                "dexterity": 18,
                "equipment": ("dagger",),
                "hp_current": 200,
                "hp_max": 200,
            }
            | fields
        )
    )


def _ally(**fields):
    return pc("char:ally", **({"initiative": 10, "zone_id": "1,1"} | fields))


def _attack(handle, *requests, weapon="unarmed-strike", target=FOE, **fields):
    act(
        handle,
        HERO,
        intent_type="attack",
        weapon_id=weapon,
        target_id=target,
        attack_riders=requests,
        **fields,
    )


def _flurry(handle):
    act(handle, HERO, intent_type="use_feature", feature_id="monks-focus", activity_id=FLURRY)


def _focus_spent(live):
    return (
        live.custom_counters_by_entity.get(HERO, {})
        .get("feature_use:monks-focus", {})
        .get("spent", 0)
    )


def _conditions(live, entity=FOE):
    return {c.condition for c in combatant(live, entity).conditions}


def _rider_effects(live, target=FOE):
    return [e for e in live.active_effects.get(target, []) if "rider_expiry_actor_id" in e.flags]


def _normalize(value):
    if isinstance(value, BaseModel):
        return _normalize(value.model_dump(mode="json"))
    if dataclasses.is_dataclass(value):
        return _normalize(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((_normalize(v) for v in value), key=str)
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    return value


def _snapshot(live):
    return json.dumps(
        _normalize(
            (
                live.initiative,
                live.active_effects,
                live.custom_counters_by_entity,
                live.rider_uses,
                live.actor_zone,
                live.tracked_hp,
                live.tracked_temp_hp,
                live.current_actor_id,
                live.rng.getstate(),
            )
        ),
        sort_keys=True,
    ).encode()


def _assert_preflight_unchanged(live, before, offset):
    assert _snapshot(live) == before
    emitted = live.event_log[offset:]
    assert len(emitted) == 1
    assert isinstance(emitted[0], AttackFailed)
    assert emitted[0].reason == "unsupported_rider"
    assert not any(isinstance(e, IntentSubmitted) for e in emitted)


@pytest.mark.parametrize(
    "extra", ["dc", "damage_bonus", "cost", "trigger", "phase", "target_id", "duration"]
)
def test_request_rejects_host_mechanical_fields(extra):
    with pytest.raises(ValidationError):
        _request(**{extra: 1})


@pytest.mark.parametrize("value", [True, "5", 5.0])
def test_push_distance_is_a_strict_integer(value):
    with pytest.raises(ValidationError):
        _request(PUSH, push_distance_ft=value)


def test_request_is_frozen_and_player_intent_uses_an_immutable_tuple():
    request = _request()
    with pytest.raises(ValidationError):
        request.activity_id = "other"
    assert PlayerIntent(intent_type="attack").attack_riders == ()
    intent = PlayerIntent(intent_type="attack", attack_riders=[request.model_dump(mode="json")])
    assert intent.attack_riders == (request,)
    assert PlayerIntent.model_validate_json(intent.model_dump_json()) == intent


@pytest.mark.parametrize(
    "case",
    [
        "not_owned",
        "below_level",
        "unknown_feature",
        "unknown_option",
        "wrong_pair",
        "weapon",
        "resource",
    ],
)
def test_static_illegality_refuses_before_attack_payment_or_rng(case):
    member = _monk()
    if case == "not_owned":
        member = _monk(class_slug="fighter")
    elif case == "below_level":
        member = _monk(character_level=4)
    handle, live = start([member], seed=4)
    request = _request()
    weapon = "unarmed-strike"
    if case == "unknown_feature":
        request = _request(("unknown-feature", STUN[1]))
    elif case == "unknown_option":
        request = _request((STUN[0], "unknown-option"))
    elif case == "wrong_pair":
        request = _request((STUN[0], TOPPLE[1]))
    elif case == "weapon":
        weapon = "greatsword"
    elif case == "resource":
        orch._increment_feature_use(live, HERO, "monks-focus", 5)
    before, offset = _snapshot(live), len(live.event_log)
    _attack(handle, request, weapon=weapon)
    _assert_preflight_unchanged(live, before, offset)


@pytest.mark.parametrize("intent_type", ["dodge", "move", "cast_spell", "use_feature", "pass"])
def test_non_attack_intents_cannot_smuggle_a_rider(intent_type):
    handle, live = start([_monk()], seed=4)
    before, offset = _snapshot(live), len(live.event_log)
    act(handle, HERO, intent_type=intent_type, target_id=FOE, attack_riders=(_request(),))
    _assert_preflight_unchanged(live, before, offset)


@pytest.mark.parametrize("weapon", ["unarmed-strike", "quarterstaff"])
def test_stunning_strike_on_a_real_monk_hit_pays_once_before_damage(weapon):
    handle, live = start([_monk()], seed=4)
    _attack(handle, _request(), weapon=weapon)
    [attack] = events(live, AttackRolled)
    save = events(live, SaveRolled)[0]
    [rider] = events(live, AttackRiderTriggered)
    assert attack.is_hit
    assert (save.ability, save.dc, save.succeeded) == ("con", 15, False)
    assert _focus_spent(live) == 1
    assert "stunned" in _conditions(live)
    assert (rider.attacker_id, rider.target_id, rider.feature_id, rider.activity_id) == (
        HERO,
        FOE,
        *STUN,
    )
    assert rider.save_outcome == "failure"
    assert rider.sacrificed_sneak_dice == 0
    assert [(r.feature_id, r.cost, r.maximum) for r in rider.resource_spent] == [
        ("monks-focus", 1, 5)
    ]
    damage = events(live, DamageApplied)[0]
    assert live.event_log.index(attack) < live.event_log.index(save)
    assert live.event_log.index(save) < live.event_log.index(damage)
    assert damage.source_actor_id == HERO
    assert damage.damage_instance_id is not None


def test_stunning_strike_dc_uses_monk_wisdom_with_a_different_primary_class():
    handle, live = start(
        [
            _monk(
                class_slug="wizard",
                classes={"wizard": 1, "monk": 5},
                character_level=6,
                intelligence=8,
            )
        ],
        seed=4,
    )
    _attack(handle, _request())
    [save] = events(live, SaveRolled)
    assert save.dc == 15
    assert _focus_spent(live) == 1


def test_stunning_once_per_turn_failure_preserves_the_remaining_attack():
    handle, live = start([_monk()], seed=4)
    _attack(handle, _request())
    before, offset = _snapshot(live), len(live.event_log)
    _attack(handle, _request())
    _assert_preflight_unchanged(live, before, offset)
    assert len(events(live, AttackRolled)) == 1
    assert _focus_spent(live) == 1
    _attack(handle)
    assert len(events(live, AttackRolled)) == 2


def test_stunning_failure_expires_at_the_source_next_turn_start():
    handle, live = start([_monk()], seed=4)
    _attack(handle, _request())
    assert "stunned" in _conditions(live)
    act(handle, HERO, intent_type="pass")
    assert "stunned" in _conditions(live)
    act(handle, FOE, intent_type="pass")
    assert live.current_actor_id == HERO
    assert "stunned" not in _conditions(live)
    assert events(live, EffectExpired)
    # A new source turn also reopens the once-per-turn declaration.
    _attack(handle, _request())
    assert _focus_spent(live) == 2


@pytest.mark.parametrize("cancel_advantage", [False, True])
def test_stunning_success_halves_speed_and_consumes_next_attack_advantage(cancel_advantage):
    handle, live = start([_monk()], seed=4)
    orch._update_combatant(live, FOE, constitution=40)
    _attack(handle, _request())
    [save] = events(live, SaveRolled)
    assert save.succeeded
    assert "stunned" not in _conditions(live)
    assert orch._effective_speed(combatant(live, FOE), live) == 15
    assert combatant(live, FOE).movement_remaining == 15
    if cancel_advantage:
        orch._emit(
            live,
            EffectApplied(
                effect=ActiveEffect(
                    id="test:disadvantage",
                    name="Disadvantage",
                    origin="test:attack-rider",
                    target_id=HERO,
                    changes=[
                        ActiveEffectChange(
                            key="flags.disadvantage.attack", mode="override", value=True
                        )
                    ],
                )
            ),
        )
    _attack(handle, use_bonus_action=True)
    second = events(live, AttackRolled)[1]
    assert second.advantage == ("normal" if cancel_advantage else "advantage")
    assert second.advantage_sources
    _attack(handle)
    third = events(live, AttackRolled)[2]
    assert third.advantage == ("disadvantage" if cancel_advantage else "normal")
    assert not third.advantage_sources
    assert orch._effective_speed(combatant(live, FOE), live) == 15
    assert _focus_spent(live) == 1


def test_stunning_success_unused_grant_and_speed_expire_together():
    handle, live = start([_monk()], seed=4)
    orch._update_combatant(live, FOE, constitution=40)
    _attack(handle, _request())
    assert orch._effective_speed(combatant(live, FOE), live) == 15
    act(handle, HERO, intent_type="pass")
    assert orch._effective_speed(combatant(live, FOE), live) == 15
    act(handle, FOE, intent_type="pass")
    assert orch._effective_speed(combatant(live, FOE), live) == 30
    assert not _rider_effects(live)
    _attack(handle)
    assert events(live, AttackRolled)[-1].advantage == "normal"


def test_stunning_next_attack_grant_is_consumed_even_when_that_attack_misses():
    handle, live = start([_monk()], seed=4)
    orch._update_combatant(live, FOE, constitution=40)
    _attack(handle, _request())
    orch._update_combatant(live, FOE, ac=100)
    _attack(handle, use_bonus_action=True)
    second = events(live, AttackRolled)[1]
    assert second.advantage == "advantage"
    assert not second.is_hit
    _attack(handle)
    third = events(live, AttackRolled)[2]
    assert third.advantage == "normal"
    assert not third.is_hit
    assert orch._effective_speed(combatant(live, FOE), live) == 15
    assert len(events(live, SaveRolled)) == 1


def test_repeated_stunning_success_on_another_turn_refreshes_without_quartering_speed():
    handle, live = start([_monk(), _ally()], seed=4)
    orch._update_combatant(live, FOE, constitution=40)
    _attack(handle, _request())
    act(handle, HERO, intent_type="pass")
    assert live.current_actor_id == "char:ally"
    dagger = BundledAssetLoader().get_weapon("dagger")
    assert dagger is not None
    activity = next(a for a in dagger.activities if a.kind == "attack")
    # The shared off-turn attack seam receives a separately predeclared rider.
    # It uses real final attack facts and the other creature's turn serial.
    intent = PlayerIntent(intent_type="attack", attack_riders=(_request(),))
    plans = preflight_attack_riders(live, combatant(live), intent, dagger, "opportunity")

    def strike(selected=()):
        ctx = build_activity_context(
            combatant(live),
            [combatant(live, FOE)],
            rng=live.rng,
            slot_level=None,
            base_spell_level=None,
            concentration=False,
            event_emitter=lambda e: orch._emit(live, e),
            spellcasting_ability=None,
            source_passive_effects=[],
            spell_book={},
            passive_damage_modifiers={},
            save_modifiers=orch._build_hydration_payload(live, caster=combatant(live))[
                "save_modifiers"
            ],
            scale_values=orch._scale_values_of(combatant(live)),
            is_opportunity_attack=True,
        )
        resolve_activity(
            activity, attach_attack_riders(live, ctx, selected, origin="opportunity"), weapon=dagger
        )

    strike(plans)
    assert _focus_spent(live) == 2
    assert len(_rider_effects(live)) == 1
    assert orch._effective_speed(combatant(live, FOE), live) == 15
    assert events(live, EffectExpired)
    strike()
    strike()
    assert [e.advantage for e in events(live, AttackRolled)] == [
        "normal",
        "advantage",
        "advantage",
        "normal",
    ]
    assert orch._effective_speed(combatant(live, FOE), live) == 15


def test_same_stunning_speed_effect_from_two_sources_applies_once_and_consumes_both_grants():
    handle, live = start([_monk(), _ally()], seed=4)
    orch._update_combatant(live, FOE, constitution=40)
    _attack(handle, _request())
    [first] = _rider_effects(live)
    flags = dict(first.flags, rider_expiry_actor_id="char:ally")
    second = first.model_copy(
        update={"origin": first.origin.replace(HERO, "char:ally"), "flags": flags}, deep=True
    )
    orch._emit(live, EffectApplied(effect=second))
    assert len(_rider_effects(live)) == 2
    assert orch._effective_speed(combatant(live, FOE), live) == 15
    _attack(handle, use_bonus_action=True)
    _attack(handle)
    assert [e.advantage for e in events(live, AttackRolled)] == ["normal", "advantage", "normal"]
    assert orch._effective_speed(combatant(live, FOE), live) == 15


@pytest.mark.parametrize("seed", [31, 4])
def test_miss_does_not_pay_or_roll_a_rider_save(seed):
    # Seed 31 is a natural 1; AC 100 also covers an ordinary failed roll.
    handle, live = start([_monk()], seed=seed, encounter=[foe(ac=100)])
    expected = Random()
    expected.setstate(live.rng.getstate())
    natural = expected.randint(1, 20)
    _attack(handle, _request())
    [attack] = events(live, AttackRolled)
    assert (attack.natural, attack.is_hit) == (natural, False)
    assert not events(live, SaveRolled)
    assert not events(live, AttackRiderTriggered)
    assert _focus_spent(live) == 0
    assert not live.rider_uses
    assert live.rng.getstate() == expected.getstate()


def test_shield_converted_miss_does_not_trigger_stunning_strike():
    shielded = pc(
        "char:shielded",
        initiative=30,
        class_slug="wizard",
        character_level=5,
        zone_id="1,0",
        ac=12,
        spells_known=["shield"],
        spell_slots={1: 2},
    )
    handle, live = start([shielded, _monk(attack_bonus=4)], seed=7, encounter=[foe(zone_id="4,4")])
    act(handle, "char:shielded", intent_type="ready", spell_id="shield")
    expected = Random()
    expected.setstate(live.rng.getstate())
    natural = expected.randint(1, 20)
    _attack(handle, _request(), target="char:shielded")
    [attack] = events(live, AttackRolled)
    assert (attack.natural, attack.roll_total, attack.is_hit) == (natural, natural + 4, False)
    assert len(events(live, ReactionTriggered)) == 1
    assert not events(live, DamageApplied)
    assert not events(live, SaveRolled)
    assert not events(live, AttackRiderTriggered)
    assert _focus_spent(live) == 0
    assert not live.rider_uses
    assert live.rng.getstate() == expected.getstate()


def test_shield_converted_miss_preserves_sneak_attack_and_obscure_dice():
    shielded = pc(
        "char:shielded",
        initiative=30,
        class_slug="wizard",
        character_level=5,
        zone_id="1,0",
        ac=12,
        spells_known=["shield"],
        spell_slots={1: 2},
    )
    handle, live = start(
        [shielded, _rogue(attack_bonus=4), _ally()], seed=7, encounter=[foe(zone_id="4,4")]
    )
    act(handle, "char:shielded", intent_type="ready", spell_id="shield")
    expected = Random()
    expected.setstate(live.rng.getstate())
    natural = expected.randint(1, 20)
    _attack(handle, _request(OBSCURE), weapon="dagger", target="char:shielded")
    [attack] = events(live, AttackRolled)
    assert (attack.natural, attack.is_hit) == (natural, False)
    assert len(events(live, ReactionTriggered)) == 1
    assert not events(live, DamageApplied)
    assert not events(live, SaveRolled)
    assert not events(live, AttackRiderTriggered)
    assert not combatant(live).sneak_attack_spent_this_turn
    assert live.rng.getstate() == expected.getstate()


@pytest.mark.parametrize("option", [ADDLE, PUSH, TOPPLE])
@pytest.mark.parametrize("funding", ["action", "martial_arts_bonus"])
def test_open_hand_requires_a_paid_flurry_hit(option, funding):
    handle, live = start([_monk(subclass_slug="hand")], seed=4)
    request = _request(option, **({"push_distance_ft": 15} if option == PUSH else {}))
    before, offset = _snapshot(live), len(live.event_log)
    _attack(handle, request, use_bonus_action=funding == "martial_arts_bonus")
    _assert_preflight_unchanged(live, before, offset)


def test_open_hand_requires_the_owning_subclass():
    handle, live = start([_monk()], seed=4)
    _flurry(handle)
    before, offset = _snapshot(live), len(live.event_log)
    _attack(handle, _request(ADDLE))
    _assert_preflight_unchanged(live, before, offset)


def test_open_hand_cannot_apply_two_options_from_one_flurry_hit():
    handle, live = start([_monk(subclass_slug="hand")], seed=4)
    _flurry(handle)
    before, offset = _snapshot(live), len(live.event_log)
    _attack(handle, _request(ADDLE), _request(TOPPLE))
    _assert_preflight_unchanged(live, before, offset)


@pytest.mark.parametrize("option,distance", [(STUN, 5), (ADDLE, 5), (PUSH, None)])
def test_distance_choice_is_only_accepted_for_the_push_option(option, distance):
    handle, live = start([_monk(subclass_slug="hand")], seed=4)
    _flurry(handle)
    before, offset = _snapshot(live), len(live.event_log)
    _attack(handle, _request(option, push_distance_ft=distance))
    _assert_preflight_unchanged(live, before, offset)


@pytest.mark.parametrize("option,ability,status", [(ADDLE, None, None), (TOPPLE, "dex", "prone")])
def test_selected_open_hand_outcome_uses_the_already_paid_flurry(option, ability, status):
    handle, live = start([_monk(subclass_slug="hand")], seed=4)
    _flurry(handle)
    _attack(handle, _request(option))
    assert _focus_spent(live) == 1
    assert combatant(live).flurry_strikes_remaining == 1
    assert combatant(live).action_available
    [rider] = events(live, AttackRiderTriggered)
    assert (rider.feature_id, rider.activity_id) == option
    assert not rider.resource_spent
    if ability is None:
        assert not events(live, SaveRolled)
        assert not events(live, ConditionApplied)
    else:
        [save] = events(live, SaveRolled)
        assert (save.ability, save.dc, save.succeeded) == (ability, 15, False)
        assert status in _conditions(live)
    assert not events(live, CombatantMoved)


def test_addle_suppresses_only_opportunity_attacks_until_target_next_start():
    handle, live = start([_monk(subclass_slug="hand")], seed=4)
    _flurry(handle)
    _attack(handle, _request(ADDLE))
    assert combatant(live, FOE).reaction_available
    offset = len(live.event_log)
    act(handle, HERO, intent_type="move", target_zone_id="0,3")
    assert not [
        e
        for e in live.event_log[offset:]
        if isinstance(e, AttackRolled) and e.is_opportunity_attack
    ]
    assert combatant(live, FOE).reaction_available
    act(handle, HERO, intent_type="pass")
    assert live.current_actor_id == FOE
    assert not _rider_effects(live)
    assert events(live, EffectExpired)
    # At this target turn start, the ordinary threat geometry works again.
    assert orch._opportunity_attackers(live, mover_id=HERO, from_cell="0,0", to_cell="0,2") == [FOE]


@pytest.mark.parametrize("distance", [0, 5, 10, 15])
def test_open_hand_push_declared_distance_is_forced_and_costs_no_movement(distance):
    handle, live = start([_monk(subclass_slug="hand")], seed=4)
    _flurry(handle)
    before_budget = combatant(live, FOE).movement_remaining
    _attack(handle, _request(PUSH, push_distance_ft=distance))
    [save] = events(live, SaveRolled)
    assert (save.ability, save.dc, save.succeeded) == ("str", 15, False)
    assert live.actor_zone[FOE] == f"{1 + distance // 5},0"
    assert combatant(live, FOE).movement_remaining == before_budget
    assert not any(e.is_opportunity_attack for e in events(live, AttackRolled))
    assert not events(live, ActorMoved)
    assert _focus_spent(live) == 1
    moved = events(live, CombatantMoved)
    if distance:
        [movement] = moved
        assert movement.forced
        assert (movement.from_zone, movement.to_zone) == ("1,0", f"{1 + distance // 5},0")
    else:
        assert not moved


@pytest.mark.parametrize("distance", [1, 7, 20])
def test_open_hand_push_invalid_distance_refuses_before_consuming_flurry(distance):
    handle, live = start([_monk(subclass_slug="hand")], seed=4)
    _flurry(handle)
    before, offset = _snapshot(live), len(live.event_log)
    _attack(handle, _request(PUSH, push_distance_ft=distance))
    _assert_preflight_unchanged(live, before, offset)


@pytest.mark.parametrize("option,ability", [(PUSH, "str"), (TOPPLE, "dex")])
def test_open_hand_successful_save_has_no_push_or_prone(option, ability):
    handle, live = start([_monk(subclass_slug="hand")], seed=4)
    orch._update_combatant(live, FOE, strength=40, dexterity=40)
    _flurry(handle)
    _attack(handle, _request(option, **({"push_distance_ft": 15} if option == PUSH else {})))
    [save] = events(live, SaveRolled)
    assert (save.ability, save.succeeded) == (ability, True)
    assert live.actor_zone[FOE] == "1,0"
    assert "prone" not in _conditions(live)
    assert not events(live, CombatantMoved)
    assert _focus_spent(live) == 1


def test_open_hand_push_runs_persistent_area_entry_after_forced_movement():
    area_owner = pc(
        "char:area",
        initiative=5,
        zone_id="6,0",
        class_slug="cleric",
        character_level=5,
        wisdom=18,
    )
    handle, live = start([_monk(subclass_slug="hand"), area_owner], seed=4)
    spell = BundledAssetLoader().get_spell("spirit-guardians")
    assert spell is not None
    activity = next(a for a in spell.activities if a.persistent_area is not None)
    ctx = build_activity_context(
        combatant(live, "char:area"),
        [],
        rng=live.rng,
        event_emitter=lambda e: orch._emit(live, e),
        spellcasting_ability="wis",
        source_passive_effects=spell.passive_effects,
        slot_level=3,
        base_spell_level=3,
        concentration=True,
        spell_book={},
        passive_damage_modifiers={},
        save_modifiers={},
    )
    register_area(live, activity, ctx, source_id=spell.slug, spell=spell)
    assert not events(live, SaveRolled)
    _flurry(handle)
    _attack(handle, _request(PUSH, push_distance_ft=15))
    assert live.actor_zone[FOE] == "4,0"
    assert [e.ability for e in events(live, SaveRolled)] == ["str", "wis"]
    movement = events(live, CombatantMoved)[0]
    area_save = events(live, SaveRolled)[1]
    assert live.event_log.index(movement) < live.event_log.index(area_save)
    area_damage = [e for e in events(live, DamageApplied) if e.source_actor_id == "char:area"]
    assert area_damage
    assert area_damage[0].damage_instance_id is not None
    assert not any(e.is_opportunity_attack for e in events(live, AttackRolled))


@pytest.mark.parametrize("seed", [4, 5])
def test_obscure_sacrifices_three_real_sneak_dice_before_damage_and_crit(seed):
    handle, live = start([_rogue(), _ally()], seed=seed)
    expected = Random()
    expected.setstate(live.rng.getstate())
    natural = expected.randint(1, 20)
    crit = natural == 20
    weapon = sum(expected.randint(1, 4) for _ in range(2 if crit else 1))
    sneak = sum(expected.randint(1, 6) for _ in range(8 if crit else 4))
    save_natural = expected.randint(1, 20)
    _attack(handle, _request(OBSCURE), weapon="dagger")
    [damage] = events(live, DamageApplied)
    [save] = events(live, SaveRolled)
    rider = next(e for e in events(live, AttackRiderTriggered) if e.feature_id == OBSCURE[0])
    assert (damage.amount, damage.damage_type, damage.is_crit) == (
        weapon + 4 + sneak,
        "piercing",
        crit,
    )
    assert (save.ability, save.dc, save.natural) == ("dex", 17, save_natural)
    assert rider.sacrificed_sneak_dice == 3
    assert not rider.resource_spent
    assert combatant(live).sneak_attack_spent_this_turn
    assert live.rng.getstate() == expected.getstate()
    assert live.event_log.index(damage) < live.event_log.index(save)


@pytest.mark.parametrize("case", ["wrong_weapon", "spent", "duplicate", "wrong_level"])
def test_obscure_ineligible_declarations_preserve_attack_and_sneak_state(case):
    member = _rogue(character_level=13 if case == "wrong_level" else 14)
    handle, live = start([member, _ally()], seed=4)
    if case == "spent":
        orch._update_combatant(live, HERO, sneak_attack_spent_this_turn=True)
    requests = (_request(OBSCURE),) * (2 if case == "duplicate" else 1)
    before, offset = _snapshot(live), len(live.event_log)
    _attack(handle, *requests, weapon="longsword" if case == "wrong_weapon" else "dagger")
    _assert_preflight_unchanged(live, before, offset)


def test_obscure_successful_save_still_pays_sneak_dice_without_blinding():
    handle, live = start([_rogue(), _ally()], seed=4, encounter=[foe(dexterity=40)])
    _attack(handle, _request(OBSCURE), weapon="dagger")
    [save] = events(live, SaveRolled)
    assert save.succeeded
    assert "blinded" not in _conditions(live)
    assert (
        next(
            e for e in events(live, AttackRiderTriggered) if e.feature_id == OBSCURE[0]
        ).sacrificed_sneak_dice
        == 3
    )
    assert combatant(live).sneak_attack_spent_this_turn


@pytest.mark.parametrize("size", list(CreatureSize)[:4])
@pytest.mark.parametrize("seed", [4, 5])
def test_trip_uses_actual_size_and_sacrifices_one_die_before_critical_doubling(size, seed):
    handle, live = start(
        [_rogue(), _ally()], seed=seed, encounter=[foe(creature_size=size, dexterity=1)]
    )
    expected = Random()
    expected.setstate(live.rng.getstate())
    natural = expected.randint(1, 20)
    critical = natural == 20
    amount = sum(expected.randint(1, 4) for _ in range(2 if critical else 1)) + 4
    amount += sum(expected.randint(1, 6) for _ in range(12 if critical else 6))
    save_natural = expected.randint(1, 20)
    _attack(handle, _request(TRIP), weapon="dagger")
    [damage] = events(live, DamageApplied)
    [save] = events(live, SaveRolled)
    rider = next(e for e in events(live, AttackRiderTriggered) if e.feature_id == TRIP[0])
    assert damage.amount == amount
    assert (save.ability, save.dc, save.natural, save.succeeded) == ("dex", 17, save_natural, False)
    assert "prone" in _conditions(live)
    assert rider.sacrificed_sneak_dice == 1
    assert combatant(live).sneak_attack_spent_this_turn
    assert live.event_log.index(damage) < live.event_log.index(save)
    assert live.rng.getstate() == expected.getstate()


@pytest.mark.parametrize("size", [CreatureSize.HUGE, CreatureSize.GARGANTUAN])
def test_trip_size_refusal_preserves_payment_rng_turn_and_state(size):
    handle, live = start([_rogue(), _ally()], seed=4, encounter=[foe(creature_size=size)])
    before, offset = _snapshot(live), len(live.event_log)
    _attack(handle, _request(TRIP), weapon="dagger")
    _assert_preflight_unchanged(live, before, offset)


def test_trip_successful_save_sacrifices_the_die_and_does_not_apply_prone():
    handle, live = start(
        [_rogue(), _ally()], seed=4, encounter=[foe(creature_size=CreatureSize.LARGE, dexterity=40)]
    )
    _attack(handle, _request(TRIP), weapon="dagger")
    assert events(live, SaveRolled)[0].succeeded
    assert "prone" not in _conditions(live)
    assert combatant(live).sneak_attack_spent_this_turn
    assert (
        next(
            e for e in events(live, AttackRiderTriggered) if e.feature_id == TRIP[0]
        ).sacrificed_sneak_dice
        == 1
    )


def test_improved_cunning_combines_trip_and_obscure_in_declared_order():
    handle, live = start([_rogue(), _ally()], seed=4, encounter=[foe(dexterity=1)])
    _attack(handle, _request(TRIP), _request(OBSCURE), weapon="dagger")
    chosen = [e for e in events(live, AttackRiderTriggered) if e.feature_id != "sneak-attack"]
    assert [(e.feature_id, e.sacrificed_sneak_dice) for e in chosen] == [
        (TRIP[0], 1),
        (OBSCURE[0], 3),
    ]
    assert {"prone", "blinded"} <= _conditions(live)


def test_obscure_without_actual_sneak_eligibility_adds_no_save_cost_or_effect():
    handle, live = start([_rogue()], seed=4)
    expected = Random()
    expected.setstate(live.rng.getstate())
    expected.randint(1, 20)
    amount = expected.randint(1, 4) + 4
    _attack(handle, _request(OBSCURE), weapon="dagger")
    assert [e.amount for e in events(live, DamageApplied)] == [amount]
    assert not events(live, SaveRolled)
    assert not events(live, AttackRiderTriggered)
    assert "blinded" not in _conditions(live)
    assert not combatant(live).sneak_attack_spent_this_turn
    assert live.rng.getstate() == expected.getstate()


@pytest.mark.parametrize(
    "option,status", [(STUN, "stunned"), (TOPPLE, "prone"), (OBSCURE, "blinded"), (TRIP, "prone")]
)
def test_rider_conditions_honor_the_shared_immunity_gate(option, status):
    member = _rogue() if option in (OBSCURE, TRIP) else _monk(subclass_slug="hand")
    handle, live = start(
        [member, _ally()],
        seed=4,
        encounter=[foe(condition_immunities=[status], dexterity=1)],
    )
    orch._update_combatant(live, FOE, constitution=1)
    if option == TOPPLE:
        _flurry(handle)
    _attack(
        handle, _request(option), weapon="dagger" if option in (OBSCURE, TRIP) else "unarmed-strike"
    )
    assert events(live, SaveRolled)[0].succeeded is False
    assert status not in _conditions(live)
    assert not [e for e in events(live, ConditionApplied) if e.condition == status]
    assert any(e.feature_id == option[0] for e in events(live, AttackRiderTriggered))


def test_obscure_expires_at_target_next_turn_end():
    handle, live = start([_rogue(), _ally()], seed=4, encounter=[foe(dexterity=1)])
    _attack(handle, _request(OBSCURE), weapon="dagger")
    assert "blinded" in _conditions(live)
    # The rogue's attack ends its turn; the ally acts before the blinded foe.
    if live.current_actor_id == HERO:
        act(handle, HERO, intent_type="pass")
    assert live.current_actor_id == "char:ally"
    act(handle, "char:ally", intent_type="pass")
    assert live.current_actor_id == FOE
    assert "blinded" in _conditions(live)
    act(handle, FOE, intent_type="pass")
    assert "blinded" not in _conditions(live)
    assert events(live, EffectExpired)


def test_obscure_applied_on_targets_own_turn_survives_that_turn_end():
    handle, live = start([_rogue(), _ally()], seed=4, encounter=[foe(dexterity=1)])
    act(handle, HERO, intent_type="pass")
    act(handle, "char:ally", intent_type="pass")
    assert live.current_actor_id == FOE
    dagger = BundledAssetLoader().get_weapon("dagger")
    assert dagger is not None
    activity = next(a for a in dagger.activities if a.kind == "attack")
    intent = PlayerIntent(intent_type="attack", attack_riders=(_request(OBSCURE),))
    plans = preflight_attack_riders(live, combatant(live), intent, dagger, "opportunity")
    ctx = build_activity_context(
        combatant(live),
        [combatant(live, FOE)],
        rng=live.rng,
        slot_level=None,
        base_spell_level=None,
        concentration=False,
        event_emitter=lambda e: orch._emit(live, e),
        spellcasting_ability=None,
        source_passive_effects=[],
        spell_book={},
        passive_damage_modifiers={},
        save_modifiers=orch._build_hydration_payload(live, caster=combatant(live))[
            "save_modifiers"
        ],
        scale_values=orch._scale_values_of(combatant(live)),
        sneak_attack_ally_adjacent={FOE: True},
        is_opportunity_attack=True,
    )
    resolve_activity(
        activity, attach_attack_riders(live, ctx, plans, origin="opportunity"), weapon=dagger
    )
    assert "blinded" in _conditions(live)
    act(handle, FOE, intent_type="pass")
    assert "blinded" in _conditions(live)
    act(handle, HERO, intent_type="pass")
    act(handle, "char:ally", intent_type="pass")
    assert live.current_actor_id == FOE
    assert "blinded" in _conditions(live)
    act(handle, FOE, intent_type="pass")
    assert "blinded" not in _conditions(live)


def test_sneak_attack_commits_before_a_second_extra_attack():
    handle, live = start(
        [_rogue(classes={"rogue": 5, "fighter": 5}, character_level=10), _ally()], seed=4
    )
    expected = Random()
    expected.setstate(live.rng.getstate())
    expected.randint(1, 20)
    first_amount = expected.randint(1, 4) + 4 + sum(expected.randint(1, 6) for _ in range(3))
    _attack(handle, weapon="dagger")
    assert combatant(live).sneak_attack_spent_this_turn
    assert combatant(live).attacks_remaining == 1
    assert [e.amount for e in events(live, DamageApplied)] == [first_amount]
    natural = expected.randint(1, 20)
    second_amount = sum(expected.randint(1, 4) for _ in range(2 if natural == 20 else 1)) + 4
    _attack(handle, weapon="dagger")
    assert [e.amount for e in events(live, DamageApplied)] == [first_amount, second_amount]
    assert live.rng.getstate() == expected.getstate()


class _CleaveDaggerLoader(BundledAssetLoader):
    """A future qualifying Cleave weapon, without mutating cached canonical data."""

    def get_weapon(self, slug):
        weapon = super().get_weapon(slug)
        if slug == "dagger" and weapon is not None:
            return weapon.model_copy(update={"mastery": "cleave"}, deep=True)
        return weapon


def test_sneak_attack_commits_inside_resolution_before_cleave_continuation():
    set_lib_loader_for_tests(_CleaveDaggerLoader())
    handle, live = start(
        [_rogue(classes={"rogue": 5, "fighter": 5}, character_level=10), _ally()],
        seed=4,
        encounter=[foe(), foe(entity_id="mon:second", zone_id="0,1", initiative=0)],
    )
    expected = Random()
    expected.setstate(live.rng.getstate())
    expected.randint(1, 20)
    main_amount = expected.randint(1, 4) + 4 + sum(expected.randint(1, 6) for _ in range(3))
    chain_natural = expected.randint(1, 20)
    chain_amount = sum(expected.randint(1, 4) for _ in range(2 if chain_natural == 20 else 1))
    _attack(handle, weapon="dagger")
    attacks = events(live, AttackRolled)
    assert [(e.target_id, e.is_hit) for e in attacks] == [(FOE, True), ("mon:second", True)]
    damage = events(live, DamageApplied)
    assert [(e.source_id, e.amount) for e in damage] == [
        ("dagger", main_amount),
        ("mastery:cleave", chain_amount),
    ]
    assert combatant(live).sneak_attack_spent_this_turn
    assert live.rng.getstate() == expected.getstate()
    automatic = [e for e in events(live, AttackRiderTriggered) if e.feature_id == "sneak-attack"]
    [sneak_event] = automatic
    dagger = BundledAssetLoader().get_weapon("dagger")
    assert dagger is not None
    attack_activity = next(a for a in dagger.activities if a.kind == "attack")
    assert sneak_event.activity_id == "a1T6nHaqmvbLpyJr"
    assert sneak_event.source_activity_id == attack_activity.id
    assert live.event_log.index(damage[0]) < live.event_log.index(sneak_event)
    assert live.event_log.index(sneak_event) < live.event_log.index(attacks[1])


def test_sneak_attack_can_fire_again_on_another_creatures_turn_opportunity_attack():
    handle, live = start(
        [_rogue(classes={"rogue": 5, "fighter": 5}, character_level=10), _ally()], seed=4
    )
    _attack(handle, weapon="dagger")
    assert combatant(live).sneak_attack_spent_this_turn
    act(handle, HERO, intent_type="pass")
    act(handle, "char:ally", intent_type="pass")
    assert not combatant(live).sneak_attack_spent_this_turn
    expected = Random()
    expected.setstate(live.rng.getstate())
    natural = expected.randint(1, 20)
    crit = natural == 20
    amount = sum(expected.randint(1, 4) for _ in range(2 if crit else 1)) + 4
    amount += sum(expected.randint(1, 6) for _ in range(6 if crit else 3))
    offset = len(live.event_log)
    act(handle, FOE, intent_type="move", target_zone_id="4,0")
    [attack] = [
        e for e in live.event_log[offset:] if isinstance(e, AttackRolled) and e.attacker_id == HERO
    ]
    assert attack.is_opportunity_attack
    assert attack.is_hit
    [damage] = [
        e
        for e in live.event_log[offset:]
        if isinstance(e, DamageApplied) and e.source_actor_id == HERO
    ]
    assert damage.amount == amount
    assert combatant(live).sneak_attack_spent_this_turn


@pytest.mark.parametrize("mode", ["miss", "disadvantage", "cancelled"])
def test_sneak_attack_is_not_spent_by_a_miss_or_remaining_disadvantage(mode):
    changes = []
    if mode != "miss":
        changes.append(
            ActiveEffectChange(key="flags.disadvantage.attack", mode="override", value=True)
        )
    if mode == "cancelled":
        changes.append(
            ActiveEffectChange(key="flags.advantage.attack", mode="override", value=True)
        )
    effects = [
        ActiveEffect(
            id="test:attack-mode",
            name="Attack mode",
            origin="test:rider",
            target_id=HERO,
            changes=changes,
        )
    ]
    handle, live = start(
        [_rogue(classes={"rogue": 5, "fighter": 5}, character_level=10), _ally()],
        seed=4,
        encounter=[foe(ac=100 if mode == "miss" else 1)],
        active_effects=effects,
    )
    expected = Random()
    expected.setstate(live.rng.getstate())
    first = expected.randint(1, 20)
    natural = min(first, expected.randint(1, 20)) if mode == "disadvantage" else first
    # Opposing advantage and disadvantage cancel to normal. With an adjacent
    # ally, that normal roll qualifies for Sneak Attack.
    amount = 0
    if mode != "miss":
        amount = sum(expected.randint(1, 4) for _ in range(2 if natural == 20 else 1)) + 4
        if mode == "cancelled":
            amount += sum(expected.randint(1, 6) for _ in range(6 if natural == 20 else 3))
    _attack(handle, weapon="dagger")
    assert combatant(live).sneak_attack_spent_this_turn is (mode == "cancelled")
    assert sum(e.amount for e in events(live, DamageApplied)) == amount
    assert live.rng.getstate() == expected.getstate()


@pytest.mark.parametrize(
    "feature,activity,class_slug,level",
    [
        ("cunning-strike", "n64fvJMT9fPUy7DH", "rogue", 14),
        ("cunning-strike", "m2bRZ1YeD3yf9nV7", "rogue", 14),
        ("cunning-strike", "jR7KqMuPOZYUCDyO", "rogue", 14),
        ("devious-strikes", "4TnBjQTJzt9UjUos", "rogue", 14),
        ("devious-strikes", "3eq7lcmpkJJBU2KO", "rogue", 14),
        ("brutal-strike", "nN5gsB6AcSQ4uQPN", "barbarian", 17),
        ("improved-brutal-strike", "UmRlsf4QWW98I4FS", "barbarian", 17),
        ("improved-brutal-strike", "I30qGlPDcyKwz65H", "barbarian", 17),
    ],
)
def test_deferred_options_are_refused_as_bound_riders(feature, activity, class_slug, level):
    handle, live = start([_rogue(class_slug=class_slug, character_level=level), _ally()], seed=4)
    before, offset = _snapshot(live), len(live.event_log)
    _attack(handle, _request((feature, activity)), weapon="dagger")
    _assert_preflight_unchanged(live, before, offset)


@pytest.mark.parametrize(
    "feature,activity,maximum,fields",
    [
        (
            "hills-tumble",
            "I2wKOUDxhIb5hHb7",
            CreatureSize.LARGE,
            {"species_slug": "goliath", "granted_features": ("hills-tumble",)},
        ),
        (
            "eldritch-smite",
            "CXJlzDUkMYU9w9i9",
            CreatureSize.HUGE,
            {
                "class_slug": "warlock",
                "character_level": 5,
                "granted_features": ("pact-of-the-blade", "eldritch-smite"),
            },
        ),
        (
            "repelling-blast",
            "OXhI1TDQxORrGAgc",
            CreatureSize.LARGE,
            {
                "class_slug": "warlock",
                "character_level": 2,
                "granted_features": ("repelling-blast",),
            },
        ),
    ],
)
def test_completed_size_qualification_does_not_enable_other_deferred_mechanics(
    feature, activity, maximum, fields
):
    loader = BundledAssetLoader()
    handle, live = start([_rogue(**fields), _ally()], seed=4)
    assert feature in {owner.slug for owner in feature_repertoire(combatant(live), loader)}
    row = next(
        row
        for row in audit_document(loader)["rows"]
        if row["feature_slug"] == feature and row["activity_id"] == activity
    )
    assert row["target_size_max"] is maximum
    assert row["classification"] == "deferred_rider"
    assert not row["fully_executable"]
    assert row["standalone_use_feature_rejected"]
    assert "size" not in row["deferred_reason"]
    before, offset = _snapshot(live), len(live.event_log)
    _attack(handle, _request((feature, activity)), weapon="dagger")
    _assert_preflight_unchanged(live, before, offset)


@pytest.mark.parametrize("option", [STUN, ADDLE, PUSH, TOPPLE, OBSCURE, TRIP])
def test_executable_riders_still_refuse_standalone_use_feature(option):
    member = _rogue() if option in (OBSCURE, TRIP) else _monk(subclass_slug="hand")
    handle, live = start([member, _ally()], seed=4)
    before = _snapshot(live)
    act(
        handle,
        HERO,
        intent_type="use_feature",
        feature_id=option[0],
        activity_id=option[1],
        target_id=FOE,
    )
    assert _snapshot(live) == before
    assert events(live, CastFailed)[-1].reason == "unsupported_feature"
    assert not events(live, AttackRiderTriggered)


def test_rider_replay_preserves_events_resources_effects_and_rng():
    def replay():
        handle, live = start([_monk(subclass_slug="hand")], seed=4)
        _flurry(handle)
        _attack(handle, _request(STUN), _request(ADDLE))
        _attack(handle, _request(TOPPLE))
        act(handle, HERO, intent_type="pass")
        act(handle, FOE, intent_type="pass")
        return (
            _snapshot(live),
            json.dumps(
                [e.model_dump(mode="json") for e in live.event_log], sort_keys=True
            ).encode(),
        )

    assert replay() == replay()


def test_unexpected_rider_resolver_error_rolls_back_paid_attack_and_buffered_events(monkeypatch):
    import dnd5e_engine.live_attack_riders as rider_runtime

    handle, live = start([_monk()], seed=4)
    delivered = []
    live.event_listeners.append(delivered.append)
    before = _snapshot(live)
    log_before = json.dumps([e.model_dump(mode="json") for e in live.event_log], sort_keys=True)
    queue_size = live.event_queue.qsize()

    def malformed_rider(_activity, ctx):
        assert _focus_spent(live) == 1
        assert not combatant(live).action_available
        ctx.rng.randint(1, 20)
        orch._emit(
            live,
            EffectApplied(
                effect=ActiveEffect(
                    id="test:partial-rider",
                    name="Partial rider",
                    origin="test:rollback",
                    target_id=FOE,
                    changes=[ActiveEffectChange(key="speed.reduction", mode="add", value=15)],
                )
            ),
        )
        raise ValueError("malformed rider payload")

    monkeypatch.setattr(rider_runtime, "resolve_activity", malformed_rider)
    with pytest.raises(ValueError, match="malformed rider payload"):
        _attack(handle, _request())
    assert _snapshot(live) == before
    assert (
        json.dumps([e.model_dump(mode="json") for e in live.event_log], sort_keys=True)
        == log_before
    )
    assert live.event_queue.qsize() == queue_size
    assert not delivered


@pytest.mark.parametrize("ac", [13, 100])
def test_generic_next_attack_flat_bonus_is_consumed_once_while_speed_reduction_remains(ac):
    handle, live = start([_monk()], seed=1, encounter=[foe(ac=ac)])
    orch._emit(
        live,
        EffectApplied(
            effect=ActiveEffect(
                id="test:generic-modifiers",
                name="Generic modifiers",
                origin="test:rider",
                target_id=FOE,
                changes=[
                    ActiveEffectChange(key="attack.next_bonus", mode="add", value=5),
                    ActiveEffectChange(key="speed.reduction", mode="add", value=15),
                ],
            )
        ),
    )
    assert orch._effective_speed(combatant(live, FOE), live) == 15
    assert combatant(live, FOE).movement_remaining == 15
    _attack(handle)
    first = events(live, AttackRolled)[0]
    assert (first.natural, first.modifier, first.roll_total) == (5, 11, 16)
    assert first.is_hit is (ac == 13)
    _attack(handle, use_bonus_action=True)
    second = events(live, AttackRolled)[1]
    assert second.modifier == 6
    assert orch._effective_speed(combatant(live, FOE), live) == 15
    [effect] = live.active_effects[FOE]
    assert [change.key for change in effect.changes] == ["speed.reduction"]


def test_rider_audit_is_deterministic_matches_golden_and_separates_entrypoints():
    first = audit_document(BundledAssetLoader())
    second = audit_document(BundledAssetLoader())
    assert (
        json.dumps(first, ensure_ascii=False).encode()
        == json.dumps(second, ensure_ascii=False).encode()
    )
    golden = Path(__file__).resolve().parents[3] / "docs" / "dev" / "attack-rider-audit.json"
    assert first == json.loads(golden.read_text(encoding="utf8"))
    executable = {
        (row["feature_slug"], row["activity_id"])
        for row in first["rows"]
        if row["fully_executable"]
    }
    assert executable == {
        STUN,
        ADDLE,
        PUSH,
        TOPPLE,
        OBSCURE,
        TRIP,
        ("sneak-attack", "a1T6nHaqmvbLpyJr"),
    }
    for row in first["rows"]:
        assert row["trigger"]
        assert row["qualification"]
        assert row["execution_phase"]
        if row["classification"] == "deferred_rider":
            assert row["deferred_reason"]
        if row["fully_executable"]:
            assert row["standalone_use_feature_rejected"]
