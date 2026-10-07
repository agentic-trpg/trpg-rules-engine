"""Typed optional attack declarations and draw-free canonical rider plans."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING, get_args

from dnd5e_srd_data.schema.common import SaveActivity, UtilityActivity
from dnd5e_srd_data.schema.item import WeaponProperty
from pydantic import BaseModel, ConfigDict, Field

from dnd5e_engine.activities.arithmetic import parse_expression, scalar
from dnd5e_engine.activities.effects import passive_effect_to_active_effect
from dnd5e_engine.events import ConditionType
from dnd5e_engine.feature_repertoire import feature_repertoire
from dnd5e_engine.feature_runtime import (
    FeaturePreflightError,
    ResourcePayment,
    resource_payments,
    validate_feature_formulas,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from dnd5e_srd_data.loader import AssetLoader
    from dnd5e_srd_data.schema.common import Activity
    from dnd5e_srd_data.schema.feature import (
        AttackRiderQualification,
        AttackRiderSemantics,
        Feature,
    )
    from dnd5e_srd_data.schema.item import Weapon

    from dnd5e_engine.activities.context import (
        ActivityResolutionContext,
        AttackOrigin,
        AttackResolutionContext,
    )
    from dnd5e_engine.types.combat import Combatant


class AttackRiderRequest(BaseModel):
    """Content identities and choices only; mechanics never come from the host."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    feature_id: str = Field(min_length=1)
    activity_id: str = Field(min_length=1)
    push_distance_ft: int | None = Field(default=None, ge=0, strict=True)


@dataclass(frozen=True)
class AttackRiderPlan:
    request: AttackRiderRequest
    feature: Feature
    activity: Activity
    semantics: AttackRiderSemantics
    payments: tuple[ResourcePayment, ...]


def sneak_dice_budget(ctx: ActivityResolutionContext) -> int:
    """Use the existing resolved Rogue scale, never a host-supplied count."""
    value = ctx.scale_values.get("rogue.sneak-attack")
    match = re.fullmatch(r"(\d+)d6", str(value or ""))
    return int(match[1]) if match else 0


def _validate_effects(
    feature: Feature, semantics: AttackRiderSemantics, ctx: ActivityResolutionContext
) -> None:
    effects = {effect.id: effect for effect in feature.passive_effects}
    for binding in semantics.effects:
        if binding.effect_id not in effects:
            raise FeaturePreflightError("rider references a missing effect")
        active = passive_effect_to_active_effect(
            effects[binding.effect_id],
            target_id="preflight:target",
            caster_id="preflight:source",
            ctx=ctx,
        )
        if not active.statuses <= set(get_args(ConditionType)):
            raise FeaturePreflightError("unsupported rider condition status")
        for change in active.changes:
            if change.key == "speed.multiplier":
                if change.mode != "multiply" or Fraction(str(change.value)) <= 0:
                    raise FeaturePreflightError("invalid speed multiplier")
            elif change.key in ("speed.reduction", "attack.next_bonus"):
                if change.mode != "add":
                    raise FeaturePreflightError("invalid scalar modifier mode")
                scalar(parse_expression(str(change.value), allow_dice=False))
            elif change.key in (
                "flags.attack.next_advantage",
                "flags.cannot_make_opportunity_attacks",
            ):
                if change.mode != "override" or not (
                    change.value is True or change.value == "true"
                ):
                    raise FeaturePreflightError("invalid boolean rider modifier")
            else:
                raise FeaturePreflightError("unsupported rider effect change")


def _requested_rider(
    owned: Mapping[str, Feature | None], request: AttackRiderRequest
) -> tuple[Feature, Activity, AttackRiderSemantics]:
    feature = owned.get(request.feature_id)
    if feature is None:
        raise FeaturePreflightError("rider feature is not in the validated repertoire")
    semantics = feature.attack_riders.get(request.activity_id)
    activity = next((a for a in feature.activities if a.id == request.activity_id), None)
    if semantics is None or activity is None or semantics.deferred_reason or semantics.automatic:
        raise FeaturePreflightError("rider option has no executable optional contract")
    _validate_rider_carrier(activity, semantics)
    return feature, activity, semantics


def _validate_rider_carrier(activity: Activity, semantics: AttackRiderSemantics) -> None:
    if not isinstance(activity, (SaveActivity, UtilityActivity)):
        raise FeaturePreflightError("unsupported rider activity")
    if (
        activity.uses.max
        or activity.uses.spent
        or activity.uses.recovery
        or activity.timing.trigger != "immediate"
        or activity.timing.recurring
        or activity.persistent_area is not None
        or activity.reaction is not None
        or activity.activation.reaction_conditions
    ):
        raise FeaturePreflightError("unsupported rider lifecycle or activity-local uses")
    if semantics.target_role != "attack_target":
        raise FeaturePreflightError("unsupported rider target role")
    if isinstance(activity, UtilityActivity) and activity.roll.formula:
        raise FeaturePreflightError("utility rider formula has no execution carrier")
    if {ref.id for ref in activity.effects} - {binding.effect_id for binding in semantics.effects}:
        raise FeaturePreflightError("rider effect reference has no reviewed outcome binding")
    if (
        semantics.trigger not in ("final_hit", "sneak_attack_damage", "flurry_hit")
        or semantics.phase not in ("final_hit_before_damage", "after_damage")
        or semantics.inherit_damage_type
        or semantics.related_activity_ids
        or semantics.inventory_role != "rider"
    ):
        raise FeaturePreflightError("unsupported rider execution contract")
    if semantics.sneak_dice_cost and (
        semantics.trigger != "sneak_attack_damage" or semantics.phase != "after_damage"
    ):
        raise FeaturePreflightError("Sneak Attack dice cost requires its after-damage trigger")
    if isinstance(activity, UtilityActivity) and (
        any(binding.outcome != "always" for binding in semantics.effects)
        or (semantics.forced_movement is not None and semantics.forced_movement.on_save != "always")
    ):
        raise FeaturePreflightError("conditional rider outcome requires a saving throw")


def _potential_attack_qualification(
    qualification: AttackRiderQualification, weapon: Weapon | None, origin: AttackOrigin
) -> bool:
    from dnd5e_engine.activities.attack import _is_monk_weapon

    unarmed = weapon is not None and weapon.slug == "unarmed-strike"
    if qualification == "monk_weapon_or_unarmed":
        return unarmed or _is_monk_weapon(weapon)
    if qualification == "flurry_unarmed":
        return unarmed and origin == "flurry"
    if qualification == "finesse_or_ranged":
        return weapon is not None and (
            WeaponProperty.FINESSE in weapon.properties
            or weapon.weapon_category in ("simple_ranged", "martial_ranged")
        )
    return qualification == "any_weapon" and weapon is not None


def _choice_limits(owned: Mapping[str, Feature | None]) -> dict[str, int]:
    limits: dict[str, int] = {}
    for feature in owned.values():
        if feature is not None:
            for group, limit in feature.attack_rider_choice_limits.items():
                limits[group] = max(limits.get(group, 1), limit)
    return limits


def _validate_choice(
    request: AttackRiderRequest,
    semantics: AttackRiderSemantics,
    *,
    seen: set[tuple[str, str]],
    groups: Counter[str],
    limits: Mapping[str, int],
    cell_size_ft: int,
) -> None:
    identity = (request.feature_id, request.activity_id)
    if identity in seen:
        raise FeaturePreflightError("duplicate rider option")
    seen.add(identity)
    if semantics.choice_group:
        groups[semantics.choice_group] += 1
        if groups[semantics.choice_group] > limits.get(semantics.choice_group, 1):
            raise FeaturePreflightError("too many mutually exclusive rider options")
    movement = semantics.forced_movement
    if movement is None:
        if request.push_distance_ft is not None:
            raise FeaturePreflightError("distance choice is not used by this option")
        return
    if (
        not movement.requires_distance_choice
        or cell_size_ft <= 0
        or request.push_distance_ft is None
        or request.push_distance_ft > movement.max_distance_ft
        or request.push_distance_ft % cell_size_ft
    ):
        raise FeaturePreflightError("push distance must be declared in legal grid increments")


def _validated_payments(
    feature: Feature,
    activity: Activity,
    semantics: AttackRiderSemantics,
    ctx: ActivityResolutionContext,
    loader: AssetLoader,
    repertoire: tuple[str, ...],
) -> tuple[ResourcePayment, ...]:
    try:
        validate_feature_formulas(feature, activity, ctx)
        _validate_effects(feature, semantics, ctx)
        return resource_payments(feature, activity, ctx, loader, repertoire)
    except (ValueError, ZeroDivisionError) as error:
        raise FeaturePreflightError(str(error)) from error


def plan_attack_riders(
    actor: Combatant,
    requests: tuple[AttackRiderRequest, ...],
    *,
    weapon: Weapon | None,
    origin: AttackOrigin,
    ctx: ActivityResolutionContext,
    loader: AssetLoader,
    spent: Mapping[str, int],
    used_features: set[str],
    cell_size_ft: int,
) -> tuple[AttackRiderPlan, ...]:
    """Validate all declarations and aggregate costs before the attack is paid."""
    owners = feature_repertoire(actor, loader)
    repertoire = tuple(owner.slug for owner in owners)
    owned = {owner.slug: loader.get_feature(owner.slug) for owner in owners}
    limits = _choice_limits(owned)
    groups: Counter[str] = Counter()
    seen: set[tuple[str, str]] = set()
    payments: Counter[str] = Counter()
    maxima: dict[str, int] = {}
    sacrifice = 0
    plans = []
    for request in requests:
        feature, activity, semantics = _requested_rider(owned, request)
        if semantics.once_per_turn and feature.slug in used_features:
            raise FeaturePreflightError("rider already used on this turn")
        if not _potential_attack_qualification(semantics.qualification, weapon, origin):
            raise FeaturePreflightError("attack category or funding cannot qualify for this rider")
        _validate_choice(
            request,
            semantics,
            seen=seen,
            groups=groups,
            limits=limits,
            cell_size_ft=cell_size_ft,
        )
        if semantics.sneak_dice_cost:
            if "sneak-attack" not in repertoire or actor.sneak_attack_spent_this_turn:
                raise FeaturePreflightError("no unspent Sneak Attack carrier")
            sacrifice += semantics.sneak_dice_cost
        priced = _validated_payments(feature, activity, semantics, ctx, loader, repertoire)
        for payment in priced:
            payments[payment.feature_slug] += payment.cost
            maxima[payment.feature_slug] = payment.maximum
        plans.append(
            AttackRiderPlan(
                request,
                feature.model_copy(deep=True),
                activity.model_copy(deep=True),
                semantics.model_copy(deep=True),
                priced,
            )
        )
    if sacrifice > sneak_dice_budget(ctx):
        raise FeaturePreflightError("Sneak Attack dice sacrifice exceeds its canonical scale")
    if any(spent.get(slug, 0) + cost > maxima[slug] for slug, cost in payments.items()):
        raise FeaturePreflightError("rider resource pool is exhausted")
    return tuple(plans)


def matches_rider(plan: AttackRiderPlan, attack: AttackResolutionContext) -> bool:
    """Only the resolver's final result can release a planned optional rider."""
    if not attack.is_hit or not _actual_attack_qualification(plan.semantics.qualification, attack):
        return False
    semantics = plan.semantics
    if semantics.trigger == "sneak_attack_damage":
        return attack.sneak_eligible and attack.sneak_will_fire
    if semantics.trigger == "flurry_hit":
        return attack.attack_origin == "flurry" and attack.unarmed
    return semantics.trigger == "final_hit"


def _actual_attack_qualification(
    qualification: AttackRiderQualification, attack: AttackResolutionContext
) -> bool:
    if qualification == "monk_weapon_or_unarmed":
        return attack.unarmed or attack.monk_weapon
    if qualification == "flurry_unarmed":
        return attack.unarmed and attack.attack_origin == "flurry"
    if qualification == "finesse_or_ranged":
        return attack.weapon_slug is not None and (
            attack.sneak_eligible or attack.weapon_category in ("simple_ranged", "martial_ranged")
        )
    return qualification == "any_weapon" and attack.weapon_slug is not None
