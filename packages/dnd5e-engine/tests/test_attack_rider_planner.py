"""Draw-free rider plans reject unsupported custom carriers before payment."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader, MemoryAssetLoader
from dnd5e_srd_data.schema.common import (
    ActivityTiming,
    PassiveEffectChange,
    PersistentAreaSpec,
    ReactionCondition,
    ReactionSemantics,
    ReactionTriggerKind,
    UsesBlock,
    UsesRecoveryEntry,
)

from dnd5e_engine.activities.context import ActivityResolutionContext, AttackResolutionContext
from dnd5e_engine.attack_riders import AttackRiderRequest, matches_rider, plan_attack_riders
from dnd5e_engine.feature_runtime import DrawFreeRandom, FeaturePreflightError
from dnd5e_engine.types.combat import Combatant

STUN = ("stunning-strike", "Xto99a8Zt46VLwaR")
ADDLE = ("open-hand-technique", "1jdSaWanuRrdkVs3")
PUSH = ("open-hand-technique", "XoaS0RtDCGAqrQsf")
OBSCURE = ("devious-strikes", "ki4lIPVGNA0HjEzH")


def _fixture(option=STUN):
    bundled = BundledAssetLoader()
    feature = bundled.get_feature(option[0])
    assert feature is not None
    rogue = option == OBSCURE
    class_slug, level = ("rogue", 14) if rogue else ("monk", 5)
    cls = bundled.get_class(class_slug)
    assert cls is not None
    subclass = bundled.get_subclass("hand") if option[0] == ADDLE[0] else None
    actor = Combatant(
        entity_id="actor",
        entity_type="Character",
        name="Actor",
        initiative=10,
        hp_current=100,
        hp_max=100,
        class_slug=class_slug,
        character_level=level,
        classes={class_slug: level},
        subclass_slug="hand" if subclass else None,
    )
    features = [feature]
    for slug in ("monks-focus", "sneak-attack", "improved-cunning-strike"):
        extra = bundled.get_feature(slug)
        assert extra is not None
        features.append(extra)
    loader = MemoryAssetLoader(
        classes=[cls], subclasses=[subclass] if subclass else [], features=features
    )
    events: list[object] = []
    ctx = ActivityResolutionContext(
        rng=DrawFreeRandom(11),
        caster=actor,
        targets=[],
        event_emitter=events.append,
        caster_abilities={"str": 10, "dex": 18, "wis": 18},
        caster_proficiency_bonus=3,
        class_levels={class_slug: level},
        scale_values={"monk.focus": 5, "rogue.sneak-attack": "7d6"},
    )
    weapon = bundled.get_weapon("dagger" if rogue else "unarmed-strike")
    assert weapon is not None
    return SimpleNamespace(
        actor=actor,
        ctx=ctx,
        loader=loader,
        feature=feature,
        activity=next(activity for activity in feature.activities if activity.id == option[1]),
        request=AttackRiderRequest(feature_id=option[0], activity_id=option[1]),
        weapon=weapon,
        origin="flurry" if subclass else "action",
        events=events,
    )


def _plan(fixture, requests=None, **overrides):
    arguments = {
        "weapon": fixture.weapon,
        "origin": fixture.origin,
        "ctx": fixture.ctx,
        "loader": fixture.loader,
        "spent": {},
        "used_features": set(),
        "cell_size_ft": 5,
    }
    arguments.update(overrides)
    return plan_attack_riders(
        fixture.actor, (fixture.request,) if requests is None else requests, **arguments
    )


def _replace_activity(fixture, activity):
    fixture.feature.activities = [
        activity if candidate.id == activity.id else candidate
        for candidate in fixture.feature.activities
    ]
    fixture.activity = activity


def _assert_rejected_without_mutation(fixture, **overrides):
    before_actor = fixture.actor.model_dump()
    before_ctx_spent = fixture.ctx.sneak_attack_spent.copy()
    before_rng = fixture.ctx.rng.getstate()
    before_feature = fixture.feature.model_dump()
    with pytest.raises(FeaturePreflightError):
        _plan(fixture, **overrides)
    assert fixture.actor.model_dump() == before_actor
    assert fixture.ctx.sneak_attack_spent == before_ctx_spent
    assert fixture.ctx.rng.getstate() == before_rng
    assert fixture.feature.model_dump() == before_feature
    assert fixture.events == []


def test_valid_plan_is_draw_free_priced_and_owns_independent_snapshots() -> None:
    fixture = _fixture()
    before_rng = fixture.ctx.rng.getstate()
    [plan] = _plan(fixture)
    [payment] = plan.payments
    assert (payment.feature_slug, payment.cost, payment.maximum) == ("monks-focus", 1, 5)
    assert fixture.ctx.rng.getstate() == before_rng
    assert fixture.events == []
    assert plan.feature is not fixture.feature
    assert plan.activity is not fixture.activity
    assert plan.semantics is not fixture.feature.attack_riders[fixture.request.activity_id]
    original_ability = list(plan.activity.save.ability)
    fixture.activity.save.ability.clear()
    fixture.feature.passive_effects[0].statuses.append("changed-after-planning")
    assert plan.activity.save.ability == original_ability
    assert "changed-after-planning" not in plan.feature.passive_effects[0].statuses


@pytest.mark.parametrize(
    "carrier",
    [
        "uses_max",
        "uses_spent",
        "uses_recovery",
        "turn_start",
        "manual",
        "recurring",
        "area",
        "reaction",
        "reaction_conditions",
    ],
)
def test_unsupported_activity_carriers_fail_before_any_draw_or_payment(carrier: str) -> None:
    fixture = _fixture()
    activity = fixture.activity
    if carrier == "uses_max":
        activity = activity.model_copy(update={"uses": UsesBlock(max="2")})
    elif carrier == "uses_spent":
        activity = activity.model_copy(update={"uses": UsesBlock(spent=1)})
    elif carrier == "uses_recovery":
        activity = activity.model_copy(update={"uses": UsesBlock(recovery=[UsesRecoveryEntry()])})
    elif carrier in ("turn_start", "manual"):
        activity = activity.model_copy(update={"timing": ActivityTiming(trigger=carrier)})
    elif carrier == "recurring":
        activity = activity.model_copy(update={"timing": ActivityTiming(recurring=True)})
    elif carrier == "area":
        activity = activity.model_copy(
            update={"persistent_area": PersistentAreaSpec(triggers=("turn-start-inside",))}
        )
    elif carrier == "reaction":
        activity = activity.model_copy(update={"reaction": ReactionSemantics(target_role="self")})
    else:
        activity = activity.model_copy(
            update={
                "activation": activity.activation.model_copy(
                    update={
                        "reaction_conditions": [
                            ReactionCondition(kind=ReactionTriggerKind.HIT_BY_ATTACK)
                        ]
                    }
                )
            }
        )
    _replace_activity(fixture, activity)
    _assert_rejected_without_mutation(fixture)


@pytest.mark.parametrize(
    "update",
    [
        {"trigger": "reckless_hit"},
        {"phase": "damage_preparation"},
        {"inherit_damage_type": True},
        {"related_activity_ids": ("follow-up-save",)},
        {"inventory_role": "foundation"},
        {"target_role": "attacker"},
        {"automatic": True},
        {"deferred_reason": "missing lifecycle"},
        {"sneak_dice_cost": 1},
    ],
)
def test_unimplemented_semantic_carriers_fail_closed(update) -> None:
    fixture = _fixture()
    fixture.feature.attack_riders[fixture.request.activity_id] = fixture.feature.attack_riders[
        fixture.request.activity_id
    ].model_copy(update=update)
    _assert_rejected_without_mutation(fixture)


@pytest.mark.parametrize("expression", ["0", "1d4", "@not-a-carrier"])
def test_invalid_resource_payment_is_rejected_without_draws(expression: str) -> None:
    fixture = _fixture()
    target = fixture.activity.consumption.targets[0].model_copy(update={"value": expression})
    _replace_activity(
        fixture,
        fixture.activity.model_copy(
            update={
                "consumption": fixture.activity.consumption.model_copy(update={"targets": [target]})
            }
        ),
    )
    _assert_rejected_without_mutation(fixture)


@pytest.mark.parametrize("carrier", ["ability", "status", "speed_fraction", "boolean"])
def test_malformed_custom_save_and_effects_fail_in_preflight(carrier: str) -> None:
    fixture = _fixture()
    if carrier == "ability":
        _replace_activity(
            fixture,
            fixture.activity.model_copy(
                update={"save": fixture.activity.save.model_copy(update={"ability": ["fortitude"]})}
            ),
        )
    else:
        effect = fixture.feature.passive_effects[0]
        if carrier == "status":
            effect.statuses = ["not-a-condition"]
        elif carrier == "speed_fraction":
            effect.changes = [PassiveEffectChange(key="speed.multiplier", mode=1, value="1/0")]
        else:
            effect.changes = [
                PassiveEffectChange(key="flags.attack.next_advantage", mode=5, value="1")
            ]
    _assert_rejected_without_mutation(fixture)


def test_scaled_effect_value_uses_the_same_resolved_context_as_execution() -> None:
    fixture = _fixture()
    fixture.feature.passive_effects[0].changes = [
        PassiveEffectChange(key="speed.reduction", mode=2, value="@scale.monk.focus")
    ]
    [plan] = _plan(fixture)
    assert plan.feature.slug == STUN[0]


def test_custom_utility_cannot_ask_for_an_unavailable_save_outcome() -> None:
    fixture = _fixture(ADDLE)
    semantics = fixture.feature.attack_riders[fixture.request.activity_id]
    fixture.feature.attack_riders[fixture.request.activity_id] = semantics.model_copy(
        update={"effects": (semantics.effects[0].model_copy(update={"outcome": "failure"}),)}
    )
    _assert_rejected_without_mutation(fixture)


def test_custom_utility_roll_cannot_silently_be_ignored() -> None:
    fixture = _fixture(ADDLE)
    _replace_activity(
        fixture,
        fixture.activity.model_copy(
            update={"roll": fixture.activity.roll.model_copy(update={"formula": "1d6"})}
        ),
    )
    _assert_rejected_without_mutation(fixture)


def test_every_activity_effect_reference_requires_a_reviewed_outcome_binding() -> None:
    fixture = _fixture()
    semantics = fixture.feature.attack_riders[fixture.request.activity_id]
    fixture.feature.attack_riders[fixture.request.activity_id] = semantics.model_copy(
        update={"effects": semantics.effects[:1]}
    )
    _assert_rejected_without_mutation(fixture)


@pytest.mark.parametrize("distance", [0, 5, 10, 15])
def test_push_accepts_a_predeclared_legal_distance_including_zero(distance: int) -> None:
    fixture = _fixture(PUSH)
    fixture.request = fixture.request.model_copy(update={"push_distance_ft": distance})
    [plan] = _plan(fixture)
    assert plan.request.push_distance_ft == distance


@pytest.mark.parametrize("distance, cell_size", [(None, 5), (1, 5), (20, 5), (10, 0)])
def test_push_rejects_missing_unquantized_or_unsupported_grid_choices(distance, cell_size) -> None:
    fixture = _fixture(PUSH)
    fixture.request = fixture.request.model_copy(update={"push_distance_ft": distance})
    _assert_rejected_without_mutation(fixture, cell_size_ft=cell_size)


def test_duplicate_and_mutually_exclusive_options_are_rejected() -> None:
    fixture = _fixture(ADDLE)
    _assert_rejected_without_mutation(fixture, requests=(fixture.request, fixture.request))
    push = AttackRiderRequest(feature_id=PUSH[0], activity_id=PUSH[1], push_distance_ft=5)
    _assert_rejected_without_mutation(fixture, requests=(fixture.request, push))


def test_resource_costs_are_aggregated_before_any_requested_option_can_commit() -> None:
    fixture = _fixture()
    second = fixture.activity.model_copy(update={"id": "another-hit-option"})
    fixture.feature.activities.append(second)
    fixture.feature.attack_riders[second.id] = fixture.feature.attack_riders[
        fixture.request.activity_id
    ].model_copy(update={"once_per_turn": False})
    second_request = AttackRiderRequest(feature_id=STUN[0], activity_id=second.id)
    requests = (fixture.request, second_request)
    _assert_rejected_without_mutation(fixture, requests=requests, spent={"monks-focus": 4})


def test_nonfinesse_ranged_weapon_uses_the_correct_weapon_category() -> None:
    fixture = _fixture(OBSCURE)
    shortbow = BundledAssetLoader().get_weapon("shortbow")
    assert shortbow is not None
    [plan] = _plan(fixture, weapon=shortbow)
    assert plan.semantics.sneak_dice_cost == 3


def test_sneak_cost_budget_and_live_turn_cap_are_preflight_constraints() -> None:
    fixture = _fixture(OBSCURE)
    _assert_rejected_without_mutation(
        fixture, ctx=replace(fixture.ctx, scale_values={"rogue.sneak-attack": "2d6"})
    )
    fixture.actor.sneak_attack_spent_this_turn = True
    _assert_rejected_without_mutation(fixture)


def _actual(**changes) -> AttackResolutionContext:
    base = AttackResolutionContext(
        attacker_id="actor",
        target_id="target",
        source_activity_id="attack",
        weapon_slug="unarmed-strike",
        weapon_category="simple_melee",
        unarmed=True,
        governing_ability="dex",
        advantage_mode="normal",
        advantage_sources=(),
        disadvantage_sources=(),
        is_hit=True,
        is_crit=False,
        attack_origin="flurry",
        turn_serial=1,
        sneak_eligible=False,
        sneak_will_fire=False,
        sneak_dice_count=0,
        damage_types=("bludgeoning",),
        monk_weapon=True,
    )
    return replace(base, **changes)


@pytest.mark.parametrize("option", [STUN, ADDLE])
def test_actual_weapon_or_funding_mismatch_cannot_release_a_valid_plan(option) -> None:
    fixture = _fixture(option)
    [plan] = _plan(fixture)
    assert matches_rider(plan, _actual())
    assert not matches_rider(plan, _actual(is_hit=False))
    assert not matches_rider(
        plan,
        _actual(
            unarmed=False,
            monk_weapon=False,
            weapon_slug="longsword",
            weapon_category="martial_melee",
        ),
    )
    if option == ADDLE:
        assert not matches_rider(plan, _actual(attack_origin="action"))


def test_actual_sneak_plan_and_damage_carrier_are_required_for_obscure() -> None:
    fixture = _fixture(OBSCURE)
    [plan] = _plan(fixture)
    actual = _actual(
        weapon_slug="dagger",
        unarmed=False,
        sneak_eligible=True,
        sneak_will_fire=True,
        sneak_dice_count=7,
    )
    assert matches_rider(plan, actual)
    assert not matches_rider(plan, replace(actual, sneak_will_fire=False))
    assert not matches_rider(plan, replace(actual, sneak_eligible=False))
    assert not matches_rider(plan, replace(actual, weapon_slug=None, weapon_category=None))
