"""Exact ingestion bindings for reaction targeting and closed responses.

Foundry records these behaviors in prose. Keep the reviewed spell/activity
identity mapping at ingestion so runtime consumes only typed canonical data.
Unlisted activities receive no semantics and must be explicitly deferred by
the runtime audit; matching a trigger alone does not prove executability.
"""

from dnd5e_srd_data.schema.common import (
    Activity,
    ReactionResponse,
    ReactionSemantics,
    ReactionTriggerKind,
)

_REACTION_SEMANTICS: dict[tuple[str, str], ReactionSemantics] = {
    # SRD 5.2: +5 AC is the canonical ActiveEffect. Damage negation is limited
    # to the matching targeted-spell opportunity, never broad force immunity.
    ("shield", "dnd5eactivity000"): ReactionSemantics(
        target_role="self",
        effect_expiry="owner_next_turn_start",
        responses=[
            ReactionResponse(
                kind="negate_triggering_spell_damage",
                trigger_kind=ReactionTriggerKind.TARGETED_BY_SPELL,
            )
        ],
    ),
    # SRD 5.2: the triggering caster makes the canonical Constitution save.
    ("counterspell", "dnd5eactivity000"): ReactionSemantics(
        target_role="triggering_actor",
        responses=[ReactionResponse(kind="cancel_triggering_spell_on_failed_save")],
    ),
    ("hellish-rebuke", "dnd5eactivity000"): ReactionSemantics(target_role="damage_source"),
    # Target metadata does not assert a falling lifecycle exists in runtime.
    ("feather-fall", "dnd5eactivity000"): ReactionSemantics(target_role="affected_creature"),
}


def apply_reaction_semantics(slug: str, activities: list[Activity]) -> list[Activity]:
    """Attach reviewed metadata by exact identity, preserving source order."""
    return [
        activity.model_copy(update={"reaction": semantics.model_copy(deep=True)})
        if (semantics := _REACTION_SEMANTICS.get((slug, activity.id))) is not None
        else activity
        for activity in activities
    ]
