"""Draw-free feature preflight and immutable, fully priced invocation plans.

This module reads typed assets and actor carriers, never live orchestrator
state. All expression validation uses the same formula/dice implementation as
resolution. Missing resource or semantic carriers are refusals, not defaults.
"""

from __future__ import annotations

import random
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Literal, get_args

from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.common import (
    Activity,
    CheckActivity,
    DamageActivity,
    HealActivity,
    PassiveEffect,
    SaveActivity,
    UtilityActivity,
)
from dnd5e_srd_data.schema.feature import Feature, FeatureRuntimeOperation, FeatureTargetRule

from dnd5e_engine.activities.arithmetic import parse_expression, scalar
from dnd5e_engine.activities.check import check_dc, check_skill_ability
from dnd5e_engine.activities.context import ActivityResolutionContext, SourceUses
from dnd5e_engine.activities.dice import damage_part_to_expr, validate_expression
from dnd5e_engine.activities.effects import passive_effect_to_active_effect
from dnd5e_engine.activities.formula import resolve_damage_block, resolve_roll_data
from dnd5e_engine.activities.save import _governing_ability, _resolve_dc, _resolve_save_ability
from dnd5e_engine.events import DamageType

AuditClassification = Literal[
    "fully_resolvable",
    "unsupported_preflight",
    "semantic_special_case",
    "attack_rider_executable",
    "attack_rider_deferred",
]
FeatureOperation = Literal["activity"] | FeatureRuntimeOperation


class DrawFreeRandom(random.Random):
    def random(self) -> float:
        raise AssertionError("feature preflight attempted an RNG draw")

    def getrandbits(self, k: int) -> int:
        raise AssertionError("feature preflight attempted an RNG draw")

    def randint(self, a: int, b: int) -> int:
        raise AssertionError("feature preflight attempted an RNG draw")


class FeaturePreflightError(ValueError):
    def __init__(
        self, detail: str, *, classification: AuditClassification = "unsupported_preflight"
    ) -> None:
        super().__init__(detail)
        self.classification = classification


@dataclass(frozen=True)
class ResourcePayment:
    feature_slug: str
    cost: int
    maximum: int


@dataclass(frozen=True)
class FeatureInvocation:
    activities: list[Activity]
    passive_effects: list[PassiveEffect]
    is_bonus_action: bool
    is_free_action: bool = False
    extends_rage: bool = False
    leaves_form: bool = False
    use_cap: int | None = None
    use_cost: int = 0
    scaling_value: int | None = None
    scaling_maximum: int | None = None
    spellcasting_ability: str | None = None
    source_uses: SourceUses | None = None
    payments: tuple[ResourcePayment, ...] = ()
    operation: FeatureOperation = "activity"
    target_rule: FeatureTargetRule | None = None


# These activities need semantics beyond the typed instantaneous resolver.
# Kept as data so the audit and live gate report the very same decision.
_SPECIAL: dict[str, str] = {
    **dict.fromkeys(
        (
            "brutal-strike",
            "improved-brutal-strike",
            "cunning-strike",
            "devious-strikes",
            "sneak-attack",
            "frenzy",
            "eldritch-smite",
            "stunning-strike",
            "open-hand-technique",
            "lifedrinker",
            "hunters-prey",
            "superior-hunters-prey",
            "fires-burn",
            "frosts-chill",
            "hills-tumble",
            "quivering-palm",
        ),
        "requires an authoritative hit/rider binding and its lifecycle",
    ),
    "preserve-life": "requires divided healing, self inclusion and half-maximum HP cap",
    **dict.fromkeys(
        (
            "blessed-healer",
            "disciple-of-life",
            "improved-blessed-strikes",
            "blessed-strikes-divine-strike",
            "blessed-strikes-potent-spellcasting",
            "elemental-affinity",
            "elemental-fury-primal-strike",
            "empowered-evocation",
            "dark-ones-blessing",
            "foe-slayer",
            "overchannel",
        ),
        "requires a triggering spell/hit and source magnitude carrier",
    ),
    **dict.fromkeys(
        (
            "relentless-rage",
            "relentless-endurance",
            "survivor",
            "uncanny-metabolism",
            "persistent-rage",
            "font-of-inspiration",
            "superior-inspiration",
            "indomitable",
            "heightened-focus",
            "tactical-mind",
            "peerless-skill",
        ),
        "requires a damage, failed-test, initiative or rest trigger",
    ),
    "cunning-action": "use the dedicated dash/disengage/hide intent with use_bonus_action",
    "abjure-foes": "requires counted visible targets and break-on-damage conditions",
    "sear-undead": "requires Turn Undead eligibility and combined turn/damage semantics",
    "lands-aid": "requires linked damage area and selected healing target",
    "hurl-through-hell": "requires a hit binding, banishment and return lifecycle",
    "intimidating-presence": "requires a one-minute condition and repeated end-of-turn saves",
    "breath-weapon": "requires Attack replacement and an ancestry-bound damage type",
}


def feature_operation(feature: Feature, activity: Activity) -> FeatureOperation:
    if rider := feature.attack_riders.get(activity.id):
        classification: AuditClassification = (
            "attack_rider_deferred" if rider.deferred_reason else "attack_rider_executable"
        )
        rider_reason = (
            f"standalone use_feature rejected; attack rider deferred: {rider.deferred_reason}"
            if rider.deferred_reason
            else "standalone use_feature rejected; attack-triggered execution supported"
        )
        raise FeaturePreflightError(rider_reason, classification=classification)
    if reason := _SPECIAL.get(feature.slug):
        raise FeaturePreflightError(reason, classification="semantic_special_case")
    key = (feature.slug, activity.id)
    if operation := feature.runtime_operations.get(activity.id):
        return operation
    if key == ("monks-focus", "0MuRZ0Ur95xQTKFq"):
        raise FeaturePreflightError(
            "Step of the Wind requires Dash/Disengage and doubled jump distance",
            classification="semantic_special_case",
        )
    if key == ("channel-divinity-cleric", "aOptL5pMaj3WtR8S"):
        raise FeaturePreflightError(
            "Turn Undead requires undead targeting, flee and break-on-damage semantics",
            classification="semantic_special_case",
        )
    if activity.activation.type not in ("action", "bonus"):
        raise FeaturePreflightError(
            "activation requires an unmodeled trigger/reaction/rest",
            classification="semantic_special_case",
        )
    if isinstance(activity, (HealActivity, SaveActivity, CheckActivity)):
        return "activity"
    raise FeaturePreflightError("activity has no executable feature semantic carrier")


def resource_identity(reference: str, source: Feature, loader: AssetLoader) -> str:
    """Resolve own, canonical feat:slug and full Foundry resource identities."""
    if not reference:
        return source.slug
    if reference.startswith("feat:"):
        slug = reference.removeprefix("feat:")
        if loader.get_feature(slug) is not None:
            return slug
    if reference.startswith("Compendium.dnd5e.classes24.Item."):
        foundry_id = reference.rsplit(".", 1)[-1]
        matches = [
            slug
            for slug in loader.list_slugs("features")
            if (feature := loader.get_feature(slug)) is not None
            and feature.foundry_id == foundry_id
        ]
        if len(matches) == 1:
            return matches[0]
    raise FeaturePreflightError(f"unresolved feature resource reference: {reference!r}")


def scalar_formula(expr: str, ctx: ActivityResolutionContext) -> int:
    return scalar(
        parse_expression(
            resolve_roll_data(expr, ctx, ability=ctx.spellcasting_ability), allow_dice=False
        )
    )


def resource_payments(
    feature: Feature,
    activity: Activity,
    ctx: ActivityResolutionContext,
    loader: AssetLoader,
    repertoire: tuple[str, ...],
) -> tuple[ResourcePayment, ...]:
    costs: dict[str, int] = {}
    for target in activity.consumption.targets:
        if target.type != "itemUses":
            raise FeaturePreflightError(f"unsupported consumption type: {target.type}")
        slug = resource_identity(target.target, feature, loader)
        if slug not in repertoire:
            raise FeaturePreflightError(f"resource owner is not in repertoire: {slug}")
        cost = scalar_formula(target.value, ctx)
        if target.scaling.mode == "amount":
            if ctx.scaling_value is None:
                raise FeaturePreflightError("amount cost has no scaling carrier")
            cost += ctx.scaling_value - 1
        elif target.scaling.mode:
            raise FeaturePreflightError(f"unsupported resource scaling: {target.scaling.mode}")
        if cost <= 0:
            raise FeaturePreflightError(
                "resource recovery/zero costs need a separate semantic operation"
            )
        costs[slug] = costs.get(slug, 0) + cost
    if (
        not costs
        and feature.uses
        and feature.uses.max
        and not any(a.consumption.targets for a in feature.activities)
    ):
        raise FeaturePreflightError("limited-use activity has no declared cost")
    payments = []
    for slug, cost in costs.items():
        pool = loader.get_feature(slug)
        if pool is None or pool.uses is None or not pool.uses.max:
            raise FeaturePreflightError(f"resource has no resolvable maximum: {slug}")
        maximum = scalar_formula(pool.uses.max, ctx)
        if maximum < 0:
            raise FeaturePreflightError(f"negative resource maximum: {slug}")
        payments.append(ResourcePayment(slug, cost, maximum))
    return tuple(payments)


def validate_feature_formulas(
    feature: Feature, activity: Activity, ctx: ActivityResolutionContext
) -> None:
    """Validate every executable branch before any draws, including riders."""
    ability = ctx.spellcasting_ability
    parts = []
    if isinstance(activity, SaveActivity):
        if len(activity.save.ability) != 1:
            raise FeaturePreflightError("feature save requires exactly one ability")
        _resolve_save_ability(activity)
        if activity.save.dc.calculation in ("", "flat"):
            scalar_formula(activity.save.dc.formula, ctx)
        _resolve_dc(activity, ctx)  # Scalar or ability-code DC: guaranteed draw-free.
        ability = _governing_ability(activity, ctx)
        parts = activity.damage.parts
        if activity.damage.on_save not in ("half", "none", "full"):
            raise FeaturePreflightError("unsupported save damage policy")
    elif isinstance(activity, HealActivity):
        parts = [activity.healing]
        if activity.healing.types not in (["healing"], ["temphp"]):
            raise FeaturePreflightError("unsupported healing type")
    elif isinstance(activity, DamageActivity):
        parts = activity.damage.parts
    elif isinstance(activity, CheckActivity):
        check_skill_ability(activity)
        check_dc(activity, ctx, allow_dice=False)
    elif isinstance(activity, UtilityActivity) and activity.roll.formula:
        validate_expression(resolve_roll_data(activity.roll.formula, ctx, ability=ability))
    for part in parts:
        if not part.types:
            raise FeaturePreflightError("untyped damage/healing part")
        if not isinstance(activity, HealActivity) and not set(part.types) <= set(
            get_args(DamageType)
        ):
            raise FeaturePreflightError("unsupported damage type")
        resolved = resolve_damage_block(part, ctx, ability=ability)
        validate_expression(damage_part_to_expr(resolved))
        if resolved.scaling.formula:
            validate_expression(resolved.scaling.formula)
    effects = {effect.id: effect for effect in feature.passive_effects}
    for ref in getattr(activity, "effects", ()):
        effect = effects.get(ref.id)
        if effect is None:
            raise FeaturePreflightError(f"missing effect rider: {ref.id}")
        active = passive_effect_to_active_effect(
            effect, target_id=ctx.caster.entity_id, caster_id=ctx.caster.entity_id, ctx=ctx
        )
        for change in active.changes:
            if _NUMERIC_BONUS_KEY.fullmatch(change.key) and isinstance(change.value, str):
                validate_expression(change.value)


_NUMERIC_BONUS_KEY = re.compile(
    r"system\.(?:bonuses\.(?:mwak|rwak|msak|rsak)\.(?:attack|damage)|"
    r"bonuses\.abilities\.(?:check|save|skill)|(?:abilities|skills)\.[a-z]{3}\.bonuses\.(?:check|save))"
    r"|(?:attack\.roll|damage|save\.[a-z]+|check\.[a-z_]+)\.bonus"
)


def preflight_feature(
    feature: Feature,
    invocation: FeatureInvocation,
    ctx: ActivityResolutionContext,
    *,
    loader: AssetLoader,
    repertoire: tuple[str, ...],
    spent: Mapping[str, int],
) -> FeatureInvocation:
    activity = invocation.activities[0]
    if activity.id in feature.attack_riders:
        feature_operation(feature, activity)
    if activity.activation.type not in (
        "action",
        "bonus",
        "special",
    ) or activity.activation.value not in (None, 1):
        raise FeaturePreflightError(
            "unsupported activation/payment category", classification="semantic_special_case"
        )
    if activity.uses.max:
        raise FeaturePreflightError("activity-local use pools have no runtime carrier")
    if feature.slug == "rage" and ctx.caster.worn_armor == "heavy":
        raise FeaturePreflightError("Rage cannot be entered or maintained in Heavy armor")
    try:
        operation = feature_operation(feature, activity)
        cap = scalar_formula(feature.uses.max, ctx) if feature.uses and feature.uses.max else None
        _validate_maximum(cap)
        uses = SourceUses(spent.get(feature.slug, 0), cap) if cap is not None else None
        ctx = replace(ctx, source_uses=uses)
        scaling_maximum = _scaling_maximum(activity, ctx)
        payments = resource_payments(feature, activity, ctx, loader, repertoire)
        if invocation.extends_rage or invocation.leaves_form:
            payments = ()
        validate_feature_formulas(feature, activity, ctx)
        return replace(
            invocation,
            operation=operation,
            target_rule=feature.target_rules.get(activity.id),
            payments=payments,
            source_uses=uses,
            spellcasting_ability=ctx.spellcasting_ability,
            use_cap=cap,
            scaling_maximum=scaling_maximum,
            use_cost=sum(p.cost for p in payments if p.feature_slug == feature.slug),
        )
    except FeaturePreflightError:
        raise
    except ValueError as error:
        raise FeaturePreflightError(str(error)) from error


def _validate_maximum(cap: int | None) -> None:
    if cap is not None and cap < 0:
        raise FeaturePreflightError("negative feature use maximum")


def _scaling_maximum(activity: Activity, ctx: ActivityResolutionContext) -> int | None:
    if activity.consumption.scaling.max:
        maximum = scalar_formula(activity.consumption.scaling.max, ctx)
        _validate_maximum(maximum)
        return maximum
    return None
