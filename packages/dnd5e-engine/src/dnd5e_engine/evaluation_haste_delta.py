"""Closed combat proposals for canonical Haste and its captured follow-up."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Annotated, Literal, Self

from dnd5e_srd_data.schema.common import UtilityActivity
from pydantic import Field, model_validator

from dnd5e_engine.activities.effects import (
    bind_effect_lifecycle,
    capture_end_effects,
    passive_effect_to_active_effect,
)
from dnd5e_engine.concentration_rules import concentration_max_rounds
from dnd5e_engine.effect_lifecycle import (
    EffectIdentity,
    OngoingEffectLifecycle,
    concentration_rounds_after_turn_end,
)
from dnd5e_engine.evaluation_base import EvaluationModel
from dnd5e_engine.evaluation_effects import EffectState, effect_state
from dnd5e_engine.evaluation_ruleset import get_default_ruleset
from dnd5e_engine.evaluation_state import CombatSnapshot, ConditionLink, LifecycleRecord
from dnd5e_engine.events import CombatEvent, TurnEnded

if TYPE_CHECKING:
    from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest

HASTE_EFFECT_ID: Literal["effect:hasted"] = "effect:hasted"
LETHARGY_EFFECT_ID = "effect:lethargy"


def haste_concentration_rounds() -> int:
    """Read the caster-turn cap from the fixed Haste declaration."""
    spell = get_default_ruleset().get_spell("haste")
    assert spell is not None
    duration = concentration_max_rounds(spell)
    assert duration is not None
    return duration


def canonical_haste_effect(source_id: str, target_id: str) -> EffectState:
    """Build the checked Haste application using the same asset translation as casting."""
    spell = get_default_ruleset().get_spell("haste")
    assert spell is not None
    activity = spell.activities[0]
    assert isinstance(activity, UtilityActivity)
    binding = activity.effects[0]
    definitions = {definition.id: definition for definition in spell.passive_effects}
    assert binding.lifecycle is not None
    effect = bind_effect_lifecycle(
        passive_effect_to_active_effect(
            definitions[binding.id], target_id=target_id, caster_id=source_id, concentration=True
        ),
        binding.lifecycle,
        source_id=source_id,
        source_kind="spell",
        source_slug=spell.slug,
        activity_id=activity.id,
        is_magical=True,
    )
    return effect_state(capture_end_effects(effect, definitions))


def application_clock(effect: EffectState, serial: int, round_number: int) -> LifecycleRecord:
    """Register a captured application at the supplied combat boundary."""
    assert effect.lifecycle is not None
    identity = (effect.target_id, effect.id, effect.origin)
    return LifecycleRecord(
        identity=identity,
        state=OngoingEffectLifecycle.from_application(
            identity, effect.lifecycle, serial, round_number
        ),
    )


class HasteGraphCreate(EvaluationModel):
    """Propose one initial Haste application and its entire concentration capture."""

    kind: Literal["combat.haste_graph_create"]
    combat_id: Annotated[str, Field(min_length=1)]
    source_id: Annotated[str, Field(min_length=1)]
    spell_source_id: Annotated[str, Field(min_length=1)]
    target_id: Annotated[str, Field(min_length=1)]
    slot_level: Literal[3]
    actor_ids: tuple[str, ...]
    applied_turn_serial: Annotated[int, Field(ge=1)]
    applied_round: Annotated[int, Field(ge=1)]
    expected_effects: tuple[()]
    expected_clocks: tuple[()]
    expected_lineages: tuple[()]
    expected_concentration_id: None
    expected_chain: tuple[()]
    expected_rounds: None
    effect: EffectState
    lifecycle: LifecycleRecord
    lineage: ConditionLink
    concentration_id: Literal["effect:hasted"]
    chain: tuple[EffectIdentity, ...]
    rounds: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def closed_graph(self) -> Self:
        """Require the complete canonical initial application and matching clocks."""
        expected = canonical_haste_effect(self.source_id, self.target_id)
        clock = application_clock(expected, self.applied_turn_serial, self.applied_round)
        duration = haste_concentration_rounds()
        if (
            len(set(self.actor_ids)) != len(self.actor_ids)
            or not {self.source_id, self.target_id} <= set(self.actor_ids)
            or self.effect != expected
            or self.lifecycle != clock
            or self.lineage != ConditionLink(identity=clock.identity, statuses=[])
            or self.chain != (clock.identity,)
            or self.rounds not in (duration, duration - 1)
        ):
            raise ValueError("Haste graph differs from the canonical combat application")
        return self


class EffectFollowUpCreate(EvaluationModel):
    """Propose a captured effect after removal of its full parent identity."""

    kind: Literal["combat.effect_follow_up_create"]
    combat_id: Annotated[str, Field(min_length=1)]
    ended_identity: EffectIdentity
    effect: EffectState
    lifecycle: LifecycleRecord
    lineage: ConditionLink
    expected_effect: None
    expected_lifecycle: None
    expected_lineage: None

    @model_validator(mode="after")
    def captured_application(self) -> Self:
        """Check target, distinct identity, condition lineage and apply-time clock."""
        app = self.effect.lifecycle
        if app is None:
            raise ValueError("follow-up needs its captured application")
        clock = application_clock(
            self.effect,
            self.lifecycle.state.applied_turn_serial,
            self.lifecycle.state.applied_round,
        )
        if (
            self.ended_identity[0] != self.effect.target_id
            or self.ended_identity == clock.identity
            or self.lifecycle != clock
            or clock.state.applied_turn_serial < 1
            or clock.state.applied_round < 1
            or self.lineage.identity != clock.identity
            or self.lineage.statuses != sorted(set(self.lineage.statuses))
            or not set(self.lineage.statuses) <= self.effect.statuses
        ):
            raise ValueError("follow-up has contradictory identity, condition lineage or clock")
        return self


def verify_haste_creation(
    operation: HasteGraphCreate,
    request: RuleEvaluationRequest,
    events: Sequence[CombatEvent],
) -> None:
    """Bind initial graph and its duration to the exact request and cast turn closure."""
    from dnd5e_engine.evaluation_contracts import CombatIntentPayload

    snapshot, intent = request.state_snapshot, request.payload
    if not isinstance(snapshot, CombatSnapshot) or not isinstance(intent, CombatIntentPayload):
        raise TypeError("Haste creation requires a compatible fixed combat cast")
    state = snapshot.combat_state
    duration = haste_concentration_rounds()
    ended_turn = any(isinstance(e, TurnEnded) and e.actor_id == operation.source_id for e in events)
    expected_rounds = concentration_rounds_after_turn_end(duration) if ended_turn else duration
    if (
        request.operation_kind != "combat.intent"
        or intent.intent_type != "cast_spell"
        or intent.spell_id != "haste"
        or operation.combat_id != state.combat_id
        or operation.source_id != request.actor_id
        or operation.target_id != intent.target_id
        or operation.spell_source_id != intent.spell_source_id
        or operation.applied_turn_serial != state.turn_serial
        or operation.applied_round != state.round_number
        or operation.rounds != expected_rounds
    ):
        raise ValueError("Haste creation differs from the cast request and turn-end boundary")
