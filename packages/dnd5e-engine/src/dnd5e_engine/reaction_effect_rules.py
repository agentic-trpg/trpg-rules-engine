"""Original reaction AC change fold and retained response, without live ownership."""

from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict
from typing import Protocol

from dnd5e_srd_data.schema.common import Activity, UtilityActivity
from dnd5e_srd_data.schema.spell import Spell

from dnd5e_engine.activities.arithmetic import parse_expression, scalar
from dnd5e_engine.activities.context import AttackHitContext
from dnd5e_engine.activities.effects import passive_effect_to_active_effect
from dnd5e_engine.events import ProvisionalAttackEvidence
from dnd5e_engine.reactions import ActiveReactionResponse
from dnd5e_engine.types.effects import ActiveEffect, ActiveEffectChange


class HasACChanges(Protocol):
    """Declare the effect-change surface needed by shared armor-class calculations."""

    @property
    def changes(self) -> Sequence[ActiveEffectChange]:
        """Expose the effect-change sequence required by shared AC calculations."""
        ...


def effect_ac_changes(effects: Sequence[HasACChanges]) -> Counter[str]:
    """Collect applicable armor-class effect changes."""
    return Counter(
        str(change.value)
        for effect in effects
        for change in effect.changes
        if change.key in ("ac.bonus", "system.attributes.ac.bonus") and change.mode == "add"
    )


def scalar_ac_change_delta(before: Counter[str], after: Counter[str]) -> int:
    """Resolve a supported scalar armor-class effect adjustment."""
    delta = sum(
        scalar(parse_expression(value, allow_dice=False)) * count
        for value, count in (after - before).items()
    )
    return delta - sum(
        scalar(parse_expression(value, allow_dice=False)) * count
        for value, count in (before - after).items()
    )


def provisional_attack_evidence(
    attack: AttackHitContext | None,
) -> ProvisionalAttackEvidence | None:
    """Build attack evidence used while resolving an intervening reaction."""
    return ProvisionalAttackEvidence(**asdict(attack), provisional_hit=True) if attack else None


def retained_reaction_response(
    activity: Activity, identity: tuple[str, str, str]
) -> ActiveReactionResponse:
    """Restore the captured reaction response mechanics."""
    assert activity.reaction is not None
    return ActiveReactionResponse(
        identity[0],
        identity,
        tuple(activity.activation.reaction_conditions),
        tuple(activity.reaction.responses),
    )


def shield_effect(spell: Spell, owner_id: str) -> ActiveEffect:
    """Construct the reviewed Shield armor-class effect."""
    activity = spell.activities[0]
    assert isinstance(activity, UtilityActivity)
    binding = activity.effects[0]
    definition = next(p for p in spell.passive_effects if p.id == binding.id)
    return passive_effect_to_active_effect(definition, target_id=owner_id, caster_id=owner_id)
