"""Complete local predeclared reaction facts; no continuation or live queue."""

from typing import Annotated, Literal

from dnd5e_srd_data.schema.common import (
    ReactionCondition,
    ReactionResponse,
    ReactionSemantics,
    ReactionTriggerKind,
)
from pydantic import Field

from dnd5e_engine.evaluation_base import EvaluationModel
from dnd5e_engine.reactions import PendingReaction


class ReactionConditionState(EvaluationModel):
    kind: ReactionTriggerKind
    max_range_ft: Annotated[int, Field(ge=0)] | None
    target_spell_slug: Annotated[str, Field(min_length=1)] | None
    condition_text: str


class ReactionResponseState(EvaluationModel):
    kind: Literal["negate_triggering_spell_damage", "cancel_triggering_spell_on_failed_save"]
    trigger_kind: ReactionTriggerKind | None


class ReactionSemanticsState(EvaluationModel):
    target_role: Literal["self", "triggering_actor", "damage_source", "affected_creature"]
    responses: tuple[ReactionResponseState, ...]
    effect_expiry: Literal["owner_next_turn_start"] | None


class PendingReactionState(EvaluationModel):
    owner_id: Annotated[str, Field(min_length=1)]
    spell_id: Annotated[str, Field(min_length=1)]
    activity_id: Annotated[str, Field(min_length=1)]
    slot_level: Annotated[int, Field(ge=1, le=9)]
    conditions: Annotated[tuple[ReactionConditionState, ...], Field(min_length=1)]
    semantics: ReactionSemanticsState
    source_kind: Literal["spell"]
    spell_source_id: Annotated[str, Field(min_length=1)]

    def declaration(self) -> PendingReaction:
        """Reuse the original pure matching/targeting value object, never a queue."""
        return PendingReaction(
            owner_id=self.owner_id,
            spell_id=self.spell_id,
            activity_id=self.activity_id,
            slot_level=self.slot_level,
            source_kind=self.source_kind,
            conditions=tuple(
                ReactionCondition.model_validate(c.model_dump()) for c in self.conditions
            ),
            semantics=ReactionSemantics(
                target_role=self.semantics.target_role,
                responses=[
                    ReactionResponse.model_validate(r.model_dump())
                    for r in self.semantics.responses
                ],
                effect_expiry=self.semantics.effect_expiry,
            ),
        )


def reaction_state(pending: PendingReaction, *, spell_source_id: str) -> PendingReactionState:
    """Read-only regression capture with an explicitly supplied authorized source."""
    return PendingReactionState(
        owner_id=pending.owner_id,
        spell_id=pending.spell_id,
        activity_id=pending.activity_id,
        slot_level=pending.slot_level,
        source_kind=pending.source_kind,
        spell_source_id=spell_source_id,
        conditions=tuple(
            ReactionConditionState.model_validate(c.model_dump()) for c in pending.conditions
        ),
        semantics=ReactionSemanticsState(
            target_role=pending.semantics.target_role,
            responses=tuple(
                ReactionResponseState.model_validate(r.model_dump())
                for r in pending.semantics.responses
            ),
            effect_expiry=pending.semantics.effect_expiry,
        ),
    )
