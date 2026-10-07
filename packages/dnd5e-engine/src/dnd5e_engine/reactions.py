"""Canonical reaction declarations and opportunities, without live state or RNG."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from dnd5e_srd_data.schema.common import (
    Activity,
    ReactionCondition,
    ReactionResponse,
    ReactionSemantics,
    ReactionTriggerKind,
    SaveActivity,
    UtilityActivity,
)

from dnd5e_engine.activities.context import AttackHitContext

SUPPORTED_OPPORTUNITY_KINDS = frozenset(
    {
        ReactionTriggerKind.HIT_BY_ATTACK,
        ReactionTriggerKind.TARGETED_BY_SPELL,
        ReactionTriggerKind.SEES_SPELL_CAST,
        ReactionTriggerKind.TAKES_DAMAGE,
    }
)

# Input compatibility only. These aliases never supply an opportunity or add a
# condition to a spell. New callers omit this deprecated field entirely.
ReactionTriggerCompatibility = (
    ReactionTriggerKind | Literal["cast_spell", "targeted_by_magic_missile"]
)


@dataclass(frozen=True)
class PendingReaction:
    owner_id: str
    spell_id: str
    activity_id: str
    slot_level: int
    conditions: tuple[ReactionCondition, ...]
    semantics: ReactionSemantics
    source_kind: Literal["spell"] = "spell"


@dataclass(frozen=True)
class ReactionOpportunity:
    kind: ReactionTriggerKind
    triggering_actor_id: str | None
    affected_target_id: str | None = None
    triggering_spell_slug: str | None = None
    source_activity_id: str | None = None
    damage_source_actor_id: str | None = None
    damage_instance_id: str | None = None
    attack: AttackHitContext | None = None


@dataclass(frozen=True)
class ReactionResult:
    cancel_cast: bool = False
    negated_damage_targets: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ActiveReactionResponse:
    """A typed response retained only while its concrete effect is live."""

    owner_id: str
    effect_identity: tuple[str, str, str]
    conditions: tuple[ReactionCondition, ...]
    responses: tuple[ReactionResponse, ...]


def reaction_support(activity: Activity, *, source_kind: str = "spell") -> str | None:
    """Explain missing executable semantics; never infer them from prose."""
    conditions = activity.activation.reaction_conditions
    if not conditions:
        return "no typed reaction conditions"
    if any(c.kind not in SUPPORTED_OPPORTUNITY_KINDS for c in conditions):
        return "typed trigger exists but runtime opportunity producer unavailable"
    if source_kind != "spell":
        return "pre-arm API currently supports spell sources only"
    if activity.reaction is None:
        return "no typed reaction targeting/response semantics"
    if activity.uses.max or activity.consumption.targets:
        return "reaction activity has an unsupported additional resource cost"
    if not isinstance(activity, (SaveActivity, UtilityActivity)):
        return "reaction activity kind has no executable response contract"
    if isinstance(activity, UtilityActivity) and not activity.effects:
        return "utility reaction has no executable effect"
    if activity.timing.trigger != "immediate" or activity.persistent_area is not None:
        return "deferred/area reaction activity requires a separate lifecycle contract"
    for response in activity.reaction.responses:
        if response.kind == "cancel_triggering_spell_on_failed_save" and not isinstance(
            activity, SaveActivity
        ):
            return "failed-save cancellation requires a SaveActivity"
        if response.kind == "negate_triggering_spell_damage" and not any(
            c.kind == ReactionTriggerKind.TARGETED_BY_SPELL and c.target_spell_slug
            for c in conditions
        ):
            return "spell damage negation requires an explicit targeted-spell condition"
    return None


def compatible_trigger(
    trigger: ReactionTriggerCompatibility | None, conditions: tuple[ReactionCondition, ...]
) -> bool:
    if trigger is None:
        return True
    if trigger == "cast_spell":
        kind = ReactionTriggerKind.SEES_SPELL_CAST
    elif trigger == "targeted_by_magic_missile":
        return any(
            c.kind == ReactionTriggerKind.TARGETED_BY_SPELL
            and c.target_spell_slug == "magic-missile"
            for c in conditions
        )
    else:
        kind = ReactionTriggerKind(trigger)
    return any(c.kind == kind for c in conditions)


def matching_conditions(
    pending: PendingReaction, opportunity: ReactionOpportunity
) -> tuple[ReactionCondition, ...]:
    """OR matching preserves canonical order and the affected owner's identity."""
    if (
        opportunity.kind
        in (
            ReactionTriggerKind.HIT_BY_ATTACK,
            ReactionTriggerKind.TARGETED_BY_SPELL,
            ReactionTriggerKind.TAKES_DAMAGE,
        )
        and pending.owner_id != opportunity.affected_target_id
    ):
        return ()
    return tuple(
        condition
        for condition in pending.conditions
        if condition.kind == opportunity.kind
        and (
            condition.target_spell_slug is None
            or condition.target_spell_slug == opportunity.triggering_spell_slug
        )
    )


def reaction_target_id(pending: PendingReaction, opportunity: ReactionOpportunity) -> str | None:
    role = pending.semantics.target_role
    if role == "self":
        return pending.owner_id
    if role == "triggering_actor":
        return opportunity.triggering_actor_id
    if role == "damage_source":
        return opportunity.damage_source_actor_id
    return opportunity.affected_target_id
