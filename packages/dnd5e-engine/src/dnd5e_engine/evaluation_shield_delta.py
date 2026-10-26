"""Closed local Shield effect/response/expiry transition; shared ABI remains OPEN."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from dnd5e_engine.evaluation_base import EvaluationModel
from dnd5e_engine.evaluation_effects import EffectState, effect_state
from dnd5e_engine.evaluation_reaction_state import ReactionConditionState, ReactionResponseState
from dnd5e_engine.evaluation_ruleset import get_default_ruleset
from dnd5e_engine.evaluation_state import ConditionLink
from dnd5e_engine.reaction_effect_rules import retained_reaction_response, shield_effect
from dnd5e_engine.reactions import ActiveReactionResponse


class ShieldResponseState(EvaluationModel):
    """Represent the reviewed Shield reaction response mechanics."""

    owner_id: str
    effect_identity: tuple[str, str, str]
    conditions: tuple[ReactionConditionState, ...]
    responses: tuple[ReactionResponseState, ...]

    def response(self) -> ActiveReactionResponse:
        """Validate the supported reaction response profile."""
        from dnd5e_srd_data.schema.common import ReactionCondition, ReactionResponse

        return ActiveReactionResponse(
            self.owner_id,
            self.effect_identity,
            tuple(ReactionCondition.model_validate(c.model_dump()) for c in self.conditions),
            tuple(ReactionResponse.model_validate(r.model_dump()) for r in self.responses),
        )


def response_state(response: ActiveReactionResponse) -> ShieldResponseState:
    """Project the reaction response into the guarded Shield state representation."""
    return ShieldResponseState(
        owner_id=response.owner_id,
        effect_identity=response.effect_identity,
        conditions=tuple(
            ReactionConditionState.model_validate(c.model_dump()) for c in response.conditions
        ),
        responses=tuple(
            ReactionResponseState.model_validate(r.model_dump()) for r in response.responses
        ),
    )


class ShieldGraphValue(EvaluationModel):
    """Represent the complete canonical Shield effect graph."""

    effect: EffectState
    lineage: ConditionLink
    response: ShieldResponseState | None
    expiry_owner_id: str
    expiry_identity: tuple[str, str, str]
    expires_at: Literal["owner_next_turn_start"]
    owner_dead: bool

    @model_validator(mode="after")
    def canonical_graph(self) -> Self:
        """Require the supplied Shield graph to match the canonical effect profile."""
        spell = get_default_ruleset().get_spell("shield")
        assert spell is not None
        effect = shield_effect(spell, self.expiry_owner_id)
        identity = (effect.target_id, effect.id, effect.origin)
        expected_response = response_state(
            retained_reaction_response(spell.activities[0], identity)
        )
        if (
            self.effect != effect_state(effect)
            or self.expiry_identity != identity
            or self.lineage != ConditionLink(identity=identity, statuses=[])
            or self.response != (None if self.owner_dead else expected_response)
        ):
            raise ValueError(
                "Shield effect/AC/source/response/expiry graph differs from canonical rule"
            )
        return self


class ShieldStateTransition(EvaluationModel):
    """Propose a guarded transition of the complete Shield component."""

    kind: Literal["combat.shield_state_transition"]
    combat_id: Annotated[str, Field(min_length=1)]
    owner_id: Annotated[str, Field(min_length=1)]
    spell_source_id: Annotated[str, Field(min_length=1)]
    actor_ids: tuple[str, ...]
    expected: ShieldGraphValue | None
    value: ShieldGraphValue | None

    @model_validator(mode="after")
    def closed_transition(self) -> Self:
        """Validate the enclosing component's guarded, closed state transition."""
        if (
            not self.actor_ids
            or len(set(self.actor_ids)) != len(self.actor_ids)
            or self.owner_id not in self.actor_ids
            or self.expected == self.value
            or any(v.expiry_owner_id != self.owner_id for v in (self.expected, self.value) if v)
        ):
            raise ValueError(
                "Shield transition requires unique present references and a changed value"
            )
        if (
            self.expected is not None
            and self.value is not None
            and not (
                not self.expected.owner_dead
                and self.value.owner_dead
                and self.expected.effect == self.value.effect
            )
        ):
            raise ValueError("retained Shield can only remove a dead owner's response")
        return self
