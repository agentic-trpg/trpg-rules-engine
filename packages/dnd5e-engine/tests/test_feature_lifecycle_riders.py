"""Equipment-gated Sneak riders and resource-priced feature save lifecycles."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from random import Random

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader, MemoryAssetLoader
from dnd5e_srd_data.schema.common import UtilityActivity

from dnd5e_engine import AttackRiderRequest
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.activities.apply import apply_damage
from dnd5e_engine.activities.context import ActivityResolutionContext
from dnd5e_engine.activities.save import _resolve_dc
from dnd5e_engine.attack_riders import plan_attack_riders
from dnd5e_engine.events import (
    AttackFailed,
    AttackRiderTriggered,
    AttackRolled,
    CastFailed,
    ConcentrationCheck,
    ConcentrationDropped,
    ConditionApplied,
    ConditionRemoved,
    DamageApplied,
    EffectApplied,
    EffectExpired,
    ReactionTriggered,
    SaveRolled,
)
from dnd5e_engine.feature_runtime import (
    DrawFreeRandom,
    FeatureInvocation,
    FeaturePreflightError,
    preflight_feature,
    resource_payments,
    validate_feature_formulas,
)
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.live_reactions import attach_reaction_hooks
from dnd5e_engine.types.combat import Combatant
from dnd5e_engine.types.effects import ActiveEffect
from tests.c20_support import act, combatant, events, foe, monster_turn, pc, start

HERO, FOE = "char:hero", "mon:foe"
POISON = ("cunning-strike", "n64fvJMT9fPUy7DH")
KNOCK_OUT = ("devious-strikes", "3eq7lcmpkJJBU2KO")
OBSCURE = ("devious-strikes", "ki4lIPVGNA0HjEzH")
PRESENCE, RECHARGE = "ZRHT8mOlea6T8XpP", "Qr0vi4CEDfbdMNMa"


@pytest.fixture(autouse=True)
def _loader():
    set_lib_loader_for_tests(BundledAssetLoader())
    yield
    set_lib_loader_for_tests(None)


def _request(option):
    return AttackRiderRequest(feature_id=option[0], activity_id=option[1])


def _rogue(*, kit=True, **fields):
    return pc(
        **(
            dict(
                class_slug="rogue",
                character_level=14,
                dexterity=18,
                hp_current=200,
                hp_max=200,
                equipment=("dagger", "poisoners-kit") if kit else ("dagger",),
            )
            | fields
        )
    )


def _combat(*, kit=True, **enemy):
    handle, live = start(
        [_rogue(kit=kit), pc("char:ally", initiative=10, zone_id="1,1")],
        seed=4,
        encounter=[foe(**enemy)],
    )
    orch._update_combatant(live, FOE, constitution=-20)
    return handle, live


def _attack(handle, option, **fields):
    act(
        handle,
        HERO,
        **(
            dict(
                intent_type="attack",
                weapon_id="dagger",
                target_id=FOE,
                attack_riders=(_request(option),),
            )
            | fields
        ),
    )


def _rules_snapshot(live):
    return deepcopy(
        (
            live.initiative,
            live.active_effects,
            live.conditions_by_effect,
            live.custom_counters_by_entity,
            live.rider_uses,
            live.rng.getstate(),
        )
    )


def _target_turn_end(handle, live, target_id=FOE):
    """Drive actual public turn boundaries, including every intervening actor."""
    for _ in range(len(live.initiative) + 1):
        current = live.current_actor_id
        if current.startswith("mon:"):
            monster_turn(handle)
        else:
            act(handle, current, intent_type="pass")
        if current == target_id:
            return
    raise AssertionError("target did not receive a turn")


def _deal_types(live, parts):
    ctx = attach_reaction_hooks(
        live,
        ActivityResolutionContext(
            rng=live.rng,
            caster=combatant(live),
            targets=[combatant(live, FOE)],
            event_emitter=lambda event: orch._emit(live, event),
            caster_abilities={"dex": 18},
        ),
    )
    apply_damage(combatant(live, FOE), parts, ctx, source_id="lifecycle:test")


def test_poison_requires_real_carried_kit_before_attack_payment_or_dice() -> None:
    handle, live = _combat(kit=False)
    before, offset = _rules_snapshot(live), len(live.event_log)
    _attack(handle, POISON)
    assert _rules_snapshot(live) == before
    [failure] = live.event_log[offset:]
    assert isinstance(failure, AttackFailed)
    assert failure.reason == "unsupported_rider"


@pytest.mark.parametrize(
    ("option", "cost", "status"), [(POISON, 1, "poisoned"), (KNOCK_OUT, 6, "unconscious")]
)
def test_initial_rider_save_captures_exact_lifecycle_and_sacrifices_before_dice(
    option, cost, status
) -> None:
    handle, live = _combat()
    expected = Random()
    expected.setstate(live.rng.getstate())
    natural = expected.randint(1, 20)
    multiplier = 2 if natural == 20 else 1
    damage = sum(expected.randint(1, 4) for _ in range(multiplier)) + 4
    damage += sum(expected.randint(1, 6) for _ in range((7 - cost) * multiplier))
    expected.randint(1, 20)
    _attack(handle, option)
    [save] = events(live, SaveRolled)
    assert (save.ability, save.dc, save.succeeded) == ("con", 17, False)
    assert sum(event.amount for event in events(live, DamageApplied)) == damage
    assert events(live, AttackRiderTriggered)[-1].sacrificed_sneak_dice == cost
    assert combatant(live).sneak_attack_spent_this_turn
    [effect] = events(live, EffectApplied)
    application = effect.effect.lifecycle
    assert application is not None
    assert application.spec.maximum_rounds == 10
    assert application.spec.repeat_save is not None
    assert application.spec.expire_on_positive_damage == (option == KNOCK_OUT)
    assert (application.source_kind, application.source_id, application.source_slug) == (
        "feature",
        HERO,
        option[0],
    )
    assert (application.save_ability, application.save_dc, application.is_magical) == (
        "con",
        17,
        False,
    )
    assert status in {condition.condition for condition in combatant(live, FOE).conditions}
    assert live.rng.getstate() == expected.getstate()
    assert combatant(live).carried_item_slugs == ("dagger", "poisoners-kit")


@pytest.mark.parametrize(("option", "status"), [(POISON, "poisoned"), (KNOCK_OUT, "unconscious")])
def test_condition_immunity_still_pays_rider_and_saves_without_orphan_effect(
    option, status
) -> None:
    handle, live = _combat(condition_immunities=[status])
    _attack(handle, option)
    assert len(events(live, SaveRolled)) == 1
    assert len([e for e in events(live, AttackRiderTriggered) if e.feature_id == option[0]]) == 1
    assert combatant(live).sneak_attack_spent_this_turn
    assert not events(live, EffectApplied)
    assert not events(live, ConditionApplied)
    assert not live.active_effects.get(FOE)


@pytest.mark.parametrize("option", [POISON, KNOCK_OUT])
def test_successful_initial_save_still_sacrifices_sneak_dice(option) -> None:
    handle, live = _combat()
    orch._update_combatant(live, FOE, constitution=50)
    _attack(handle, option)
    assert events(live, SaveRolled)[0].succeeded
    assert events(live, AttackRiderTriggered)[-1].save_outcome == "success"
    assert combatant(live).sneak_attack_spent_this_turn
    assert not events(live, EffectApplied)
    assert not events(live, ConditionApplied)


@pytest.mark.parametrize("option", [POISON, KNOCK_OUT])
def test_missed_attack_neither_sacrifices_nor_rolls_the_declared_rider(option) -> None:
    handle, live = _combat(ac=100)
    expected = Random()
    expected.setstate(live.rng.getstate())
    expected.randint(1, 20)
    _attack(handle, option)
    assert not events(live, AttackRolled)[0].is_hit
    assert not events(live, SaveRolled)
    assert not events(live, AttackRiderTriggered)
    assert not combatant(live).sneak_attack_spent_this_turn
    assert live.rng.getstate() == expected.getstate()


@pytest.mark.parametrize("option", [POISON, KNOCK_OUT])
def test_shield_final_miss_does_not_apply_rider_save_or_lifecycle(option) -> None:
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
        [shielded, _rogue(attack_bonus=4), pc("char:ally", initiative=10, zone_id="1,1")],
        seed=7,
        encounter=[foe(zone_id="4,4")],
    )
    act(handle, "char:shielded", intent_type="ready", spell_id="shield")
    expected = Random()
    expected.setstate(live.rng.getstate())
    natural = expected.randint(1, 20)
    _attack(handle, option, target_id="char:shielded")
    [attack] = events(live, AttackRolled)
    assert (attack.natural, attack.is_hit) == (natural, False)
    assert len(events(live, ReactionTriggered)) == 1
    assert not events(live, DamageApplied)
    assert not events(live, SaveRolled)
    assert not events(live, AttackRiderTriggered)
    assert not combatant(live).sneak_attack_spent_this_turn
    assert not [effect for effect in events(live, EffectApplied) if effect.effect.lifecycle]
    assert live.rng.getstate() == expected.getstate()


@pytest.mark.parametrize(("option", "status"), [(POISON, "poisoned"), (KNOCK_OUT, "unconscious")])
def test_failed_repeat_retains_rider_and_success_ends_only_its_captured_lifecycle(
    option, status
) -> None:
    handle, live = _combat()
    _attack(handle, option)
    [effect] = events(live, EffectApplied)
    identity = (FOE, effect.effect.id, effect.effect.origin)
    assert identity in live.effect_lifecycles
    assert len(events(live, SaveRolled)) == 1
    orch._update_combatant(live, HERO, dexterity=6)
    _target_turn_end(handle, live)
    assert [save.dc for save in events(live, SaveRolled)] == [17, 17]
    assert not events(live, SaveRolled)[-1].succeeded
    assert identity in live.effect_lifecycles
    assert status in {c.condition for c in combatant(live, FOE).conditions}
    orch._update_combatant(live, FOE, constitution=50)
    _target_turn_end(handle, live)
    assert events(live, SaveRolled)[-1].succeeded
    assert events(live, SaveRolled)[-1].dc == 17
    assert identity not in live.effect_lifecycles
    assert status not in {c.condition for c in combatant(live, FOE).conditions}
    [expiry] = [e for e in events(live, EffectExpired) if e.effect_id == identity[1]]
    assert expiry.reason == "save_succeeded"
    after = live.rng.getstate()
    orch._run_end_of_turn_saves(live, FOE)
    assert live.rng.getstate() == after


@pytest.mark.parametrize(("option", "status"), [(POISON, "poisoned"), (KNOCK_OUT, "unconscious")])
def test_ten_full_round_cap_expires_after_final_repeat_without_success(option, status) -> None:
    handle, live = _combat()
    _attack(handle, option)
    [state] = live.effect_lifecycles.values()
    assert state.expires_round == state.applied_round + 10
    for _ in range(10):
        _target_turn_end(handle, live)
        assert status in {c.condition for c in combatant(live, FOE).conditions}
    assert live.round_number == state.expires_round
    _target_turn_end(handle, live)
    assert status not in {c.condition for c in combatant(live, FOE).conditions}
    assert not live.effect_lifecycles
    [expiry] = [e for e in events(live, EffectExpired) if e.effect_id == state.identity[1]]
    assert expiry.reason == "duration"
    assert live.event_log.index(events(live, SaveRolled)[-1]) < live.event_log.index(expiry)


def test_knock_out_zero_and_immune_damage_leave_sleep_but_multitype_damage_wakes_once() -> None:
    handle, live = _combat()
    _attack(handle, KNOCK_OUT)
    [state] = live.effect_lifecycles.values()
    orch._update_combatant(live, FOE, damage_immunities=["fire"])
    before = live.rng.getstate()
    _deal_types(live, {"fire": 9, "cold": 0})
    assert state.identity in live.effect_lifecycles
    assert "unconscious" in {c.condition for c in combatant(live, FOE).conditions}
    assert live.rng.getstate() == before
    offset = len(live.event_log)
    _deal_types(live, {"fire": 9, "cold": 2, "acid": 3})
    emitted = live.event_log[offset:]
    damage = [e for e in emitted if isinstance(e, DamageApplied)]
    assert [e.amount for e in damage] == [0, 2, 3]
    assert len({e.damage_instance_id for e in damage}) == 1
    [expiry] = [e for e in emitted if isinstance(e, EffectExpired)]
    assert expiry.reason == "damaged"
    assert emitted.index(expiry) > max(emitted.index(e) for e in damage)
    assert (
        len(
            [e for e in emitted if isinstance(e, ConditionRemoved) and e.condition == "unconscious"]
        )
        == 1
    )
    assert state.identity not in live.effect_lifecycles
    assert "unconscious" not in {c.condition for c in combatant(live, FOE).conditions}


def test_knock_out_incapacitation_drops_concentration_after_successful_damage_check() -> None:
    handle, live = _combat()
    anchor = ActiveEffect(
        id="effect:concentration-test",
        name="Concentration test",
        target_id=HERO,
        origin=f"cast:test:{FOE}",
    )
    orch._emit(live, EffectApplied(effect=anchor))
    live.concentration_chain[FOE] = [(HERO, anchor.id, anchor.origin)]
    orch._update_combatant(live, FOE, constitution=10, concentration_effect_id=anchor.id)
    _attack(handle, KNOCK_OUT)
    [check] = events(live, ConcentrationCheck)
    assert check.succeeded
    assert not events(live, SaveRolled)[0].succeeded
    assert FOE not in live.concentration_chain
    assert combatant(live, FOE).concentration_effect_id is None
    [dropped] = events(live, ConcentrationDropped)
    assert dropped.target_id == FOE
    [expiry] = [e for e in events(live, EffectExpired) if e.effect_id == anchor.id]
    assert expiry.reason == "concentration_drop"
    assert live.event_log.index(check) < live.event_log.index(events(live, SaveRolled)[0])
    assert live.event_log.index(events(live, SaveRolled)[0]) < live.event_log.index(dropped)
    assert "unconscious" in {c.condition for c in combatant(live, FOE).conditions}


def test_combined_knock_out_and_obscure_refuse_insufficient_sneak_dice_atomically() -> None:
    handle, live = _combat()
    before, offset = _rules_snapshot(live), len(live.event_log)
    act(
        handle,
        HERO,
        intent_type="attack",
        weapon_id="dagger",
        target_id=FOE,
        attack_riders=(_request(KNOCK_OUT), _request(OBSCURE)),
    )
    assert _rules_snapshot(live) == before
    assert isinstance(live.event_log[offset], AttackFailed)


def test_unknown_carried_item_binding_fails_closed_even_if_actor_has_that_token() -> None:
    bundled = BundledAssetLoader()
    feature = bundled.get_feature(POISON[0]).model_copy(deep=True)
    feature.attack_riders[POISON[1]] = feature.attack_riders[POISON[1]].model_copy(
        update={"requires_carried_items": ("made-up-kit",)}
    )
    cls = bundled.get_class("rogue")
    features = [feature, bundled.get_feature("sneak-attack")]
    loader = MemoryAssetLoader(classes=[cls], features=features)
    actor = Combatant(
        entity_id=HERO,
        entity_type="Character",
        name="Rogue",
        initiative=20,
        hp_current=30,
        class_slug="rogue",
        character_level=14,
        carried_item_slugs=("made-up-kit",),
    )
    ctx = ActivityResolutionContext(
        rng=DrawFreeRandom(4),
        caster=actor,
        targets=[],
        event_emitter=lambda _e: None,
        caster_abilities={"dex": 18},
        caster_proficiency_bonus=5,
        scale_values={"rogue.sneak-attack": "7d6"},
    )
    before = ctx.rng.getstate()
    with pytest.raises(FeaturePreflightError, match="canonical identity"):
        plan_attack_riders(
            actor,
            (_request(POISON),),
            weapon=bundled.get_weapon("dagger"),
            origin="action",
            ctx=ctx,
            loader=loader,
            spent={},
            used_features=set(),
            cell_size_ft=5,
        )
    assert ctx.rng.getstate() == before


def _presence_preflight(*, strength=18, proficiency=5):
    loader = BundledAssetLoader()
    feature = loader.get_feature("intimidating-presence")
    activity = next(a for a in feature.activities if a.id == PRESENCE)
    actor = Combatant(
        entity_id=HERO, entity_type="Character", name="Barbarian", initiative=20, hp_current=30
    )
    ctx = ActivityResolutionContext(
        rng=DrawFreeRandom(4),
        caster=actor,
        targets=[],
        event_emitter=lambda _e: None,
        caster_abilities={"str": strength},
        caster_proficiency_bonus=proficiency,
    )
    invocation = FeatureInvocation(
        activities=[activity], passive_effects=feature.passive_effects, is_bonus_action=True
    )
    planned = preflight_feature(
        feature,
        invocation,
        ctx,
        loader=loader,
        repertoire=(feature.slug, "rage"),
        spent={},
    )
    return loader, feature, activity, ctx, planned


@pytest.mark.parametrize(("strength", "proficiency", "dc"), [(18, 5, 17), (10, 2, 10)])
def test_formula_dc_and_empty_own_use_reference_are_generic_draw_free_contracts(
    strength, proficiency, dc
) -> None:
    loader, feature, activity, ctx, planned = _presence_preflight(
        strength=strength, proficiency=proficiency
    )
    assert _resolve_dc(activity, ctx) == dc
    assert [(p.feature_slug, p.cost, p.maximum) for p in planned.payments] == [(feature.slug, 1, 1)]
    assert planned.use_cost == 1
    assert planned.use_cap == 1
    assert planned.operation == "activity"
    assert activity.save.dc.calculation == ""
    assert planned.source_uses.maximum == 1
    recharge = next(a for a in feature.activities if a.id == RECHARGE)
    with pytest.raises(FeaturePreflightError, match="recovery/zero costs"):
        resource_payments(feature, recharge, ctx, loader, (feature.slug, "rage"))


def test_dice_dc_formula_fails_preflight_without_drawing() -> None:
    loader, feature, activity, ctx, planned = _presence_preflight()
    malformed = activity.model_copy(
        update={
            "save": activity.save.model_copy(
                update={"dc": activity.save.dc.model_copy(update={"formula": "8 + 1d4"})}
            )
        }
    )
    before = ctx.rng.getstate()
    with pytest.raises(FeaturePreflightError):
        preflight_feature(
            feature,
            replace(planned, activities=[malformed]),
            ctx,
            loader=loader,
            repertoire=(feature.slug,),
            spent={},
        )
    assert ctx.rng.getstate() == before


def test_repeat_save_cannot_bind_to_a_utility_feature_without_a_save_carrier() -> None:
    _loader, feature, activity, ctx, _planned = _presence_preflight()
    malformed = UtilityActivity(
        id=activity.id,
        activation=activity.activation,
        effects=activity.effects,
        target=activity.target,
    )
    before = ctx.rng.getstate()
    with pytest.raises(FeaturePreflightError, match="requires a triggering save"):
        validate_feature_formulas(feature, malformed, ctx)
    assert ctx.rng.getstate() == before


def test_intimidating_presence_initial_activation_has_feature_provenance_and_own_use() -> None:
    barbarian = pc(
        class_slug="barbarian", subclass_slug="berserker", character_level=14, strength=18
    )
    handle, live = start([barbarian], seed=4)
    orch._update_combatant(live, FOE, wisdom=-20)
    act(
        handle,
        HERO,
        intent_type="use_feature",
        feature_id="intimidating-presence",
        activity_id=PRESENCE,
        excluded_target_ids=(HERO,),
    )
    assert not events(live, CastFailed)
    [save] = events(live, SaveRolled)
    assert (save.ability, save.dc, save.succeeded) == ("wis", 17, False)
    [effect] = events(live, EffectApplied)
    application = effect.effect.lifecycle
    assert application is not None
    assert application.spec.maximum_rounds == 10
    assert (application.source_kind, application.source_id, application.is_magical) == (
        "feature",
        HERO,
        False,
    )
    assert "frightened" in {c.condition for c in combatant(live, FOE).conditions}
    assert orch._condition_source_entity(live, combatant(live, FOE), "frightened") == HERO
    assert orch._feature_use_spent(live, HERO, "intimidating-presence") == 1
    assert orch._feature_use_spent(live, HERO, "rage") == 0
    assert not combatant(live).bonus_action_available
    assert combatant(live).action_available


def test_intimidating_presence_targets_repeat_independently_with_captured_formula_dc() -> None:
    barbarian = pc(
        class_slug="barbarian", subclass_slug="berserker", character_level=14, strength=18
    )
    second, outside = "mon:second", "mon:outside"
    handle, live = start(
        [barbarian],
        seed=4,
        encounter=[
            foe(),
            foe(entity_id=second, initiative=0, zone_id="2,0"),
            foe(entity_id=outside, initiative=-1, zone_id="8,0"),
        ],
    )
    for target in (FOE, second, outside):
        orch._update_combatant(live, target, wisdom=-20)
    act(
        handle,
        HERO,
        intent_type="use_feature",
        feature_id="intimidating-presence",
        activity_id=PRESENCE,
        excluded_target_ids=(HERO,),
    )
    assert [save.target_id for save in events(live, SaveRolled)] == [FOE, second]
    assert {key[0] for key in live.effect_lifecycles} == {FOE, second}
    for target in (FOE, second):
        assert orch._condition_source_entity(live, combatant(live, target), "frightened") == HERO
    orch._update_combatant(live, HERO, strength=4)
    orch._update_combatant(live, FOE, wisdom=50)
    _target_turn_end(handle, live, FOE)
    assert {key[0] for key in live.effect_lifecycles} == {second}
    assert "frightened" not in {c.condition for c in combatant(live, FOE).conditions}
    _target_turn_end(handle, live, second)
    assert {key[0] for key in live.effect_lifecycles} == {second}
    assert [save.dc for save in events(live, SaveRolled)] == [17, 17, 17, 17]
    assert not events(live, SaveRolled)[-1].succeeded
    assert orch._feature_use_spent(live, HERO, "intimidating-presence") == 1
    assert orch._feature_use_spent(live, HERO, "rage") == 0


def test_intimidating_presence_fear_immunity_creates_no_lifecycle() -> None:
    barbarian = pc(
        class_slug="barbarian", subclass_slug="berserker", character_level=14, strength=18
    )
    handle, live = start([barbarian], seed=4, encounter=[foe(condition_immunities=["frightened"])])
    orch._update_combatant(live, FOE, wisdom=-20)
    act(
        handle,
        HERO,
        intent_type="use_feature",
        feature_id="intimidating-presence",
        activity_id=PRESENCE,
        excluded_target_ids=(HERO,),
    )
    assert not events(live, SaveRolled)[0].succeeded
    assert not events(live, EffectApplied)
    assert not live.effect_lifecycles
    assert orch._feature_use_spent(live, HERO, "intimidating-presence") == 1


def test_intimidating_presence_exhausted_own_pool_refuses_before_action_or_rng() -> None:
    barbarian = pc(class_slug="barbarian", subclass_slug="berserker", character_level=14)
    handle, live = start([barbarian], seed=4)
    orch._increment_feature_use(live, HERO, "intimidating-presence", 1)
    before, offset = _rules_snapshot(live), len(live.event_log)
    act(
        handle,
        HERO,
        intent_type="use_feature",
        feature_id="intimidating-presence",
        activity_id=PRESENCE,
        excluded_target_ids=(HERO,),
    )
    assert _rules_snapshot(live) == before
    [failure] = live.event_log[offset:]
    assert isinstance(failure, CastFailed)
    assert failure.reason == "no_uses_remaining"
    assert combatant(live).bonus_action_available
    assert orch._feature_use_spent(live, HERO, "rage") == 0


def test_intimidating_presence_duration_uses_repeat_then_ten_full_round_boundary() -> None:
    barbarian = pc(class_slug="barbarian", subclass_slug="berserker", character_level=14)
    handle, live = start([barbarian], seed=4)
    orch._update_combatant(live, FOE, wisdom=-20)
    act(
        handle,
        HERO,
        intent_type="use_feature",
        feature_id="intimidating-presence",
        activity_id=PRESENCE,
        excluded_target_ids=(HERO,),
    )
    [state] = live.effect_lifecycles.values()
    assert state.expires_round == state.applied_round + 10
    for _ in range(10):
        _target_turn_end(handle, live)
        assert state.identity in live.effect_lifecycles
    _target_turn_end(handle, live)
    assert not live.effect_lifecycles
    assert "frightened" not in {c.condition for c in combatant(live, FOE).conditions}
    [expiry] = events(live, EffectExpired)
    assert expiry.reason == "duration"
    assert live.event_log.index(events(live, SaveRolled)[-1]) < live.event_log.index(expiry)


def test_intimidating_presence_recharge_remains_atomic_draw_free_refusal() -> None:
    barbarian = pc(class_slug="barbarian", subclass_slug="berserker", character_level=14)
    handle, live = start([barbarian], seed=4)
    orch._increment_feature_use(live, HERO, "intimidating-presence", 1)
    before, offset = _rules_snapshot(live), len(live.event_log)
    act(
        handle,
        HERO,
        intent_type="use_feature",
        feature_id="intimidating-presence",
        activity_id=RECHARGE,
    )
    assert _rules_snapshot(live) == before
    [failure] = live.event_log[offset:]
    assert isinstance(failure, CastFailed)
    assert failure.reason == "unsupported_feature"
    assert orch._feature_use_spent(live, HERO, "intimidating-presence") == 1
    assert orch._feature_use_spent(live, HERO, "rage") == 0
