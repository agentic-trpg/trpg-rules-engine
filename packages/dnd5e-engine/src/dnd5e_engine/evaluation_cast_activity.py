"""Evaluate a bounded self-cast at its start and exact world-time completion."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal, Self

from dnd5e_srd_data.schema.spell import CastingTimeUnit, Spell
from pydantic import Field, model_validator

from dnd5e_engine.evaluation_base import EvaluationModel
from dnd5e_engine.spellcasting import resolve_ritual_cast

if TYPE_CHECKING:
    from dnd5e_srd_data.loader import AssetLoader

    from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest, RuleEvaluationResult
    from dnd5e_engine.evaluation_turn import TurnEvaluation


CAST_ACTIVITY_SCHEMA_VERSION = "engine-evaluation/36"


class ExplorationCastActivityPayload(EvaluationModel):
    """Fix one source, casting mode and selection for a self-targeted timed spell."""

    kind: Literal["world.cast_activity"]
    phase: Literal["start", "finish"]
    activity_id: Annotated[str, Field(min_length=1, max_length=256)]
    spell_id: Annotated[str, Field(min_length=1, max_length=256)]
    spell_source_id: Annotated[str, Field(min_length=1, max_length=256)]
    as_ritual: bool
    slot_level: Annotated[int, Field(ge=1, le=9)] | None
    elapsed_seconds: Annotated[int, Field(gt=0)] | None

    @model_validator(mode="after")
    def timing_selection(self) -> Self:
        """Only a finished cast carries elapsed time. Rituals select no slot."""
        if self.as_ritual != (self.slot_level is None):
            raise ValueError("only a Ritual leaves the spell slot unselected")
        if (self.phase == "finish") != (self.elapsed_seconds is not None):
            raise ValueError("only casting completion has fixed elapsed time")
        return self


class WorldCastActivityBegin(EvaluationModel):
    """Bind the uncommitted start of a cast to its selected Activity and elapsed duration."""

    kind: Literal["world.cast_activity_begin"]
    activity_id: Annotated[str, Field(min_length=1, max_length=256)]
    duration_seconds: Annotated[int, Field(gt=0)]


def casting_activity_seconds(spell: Spell, *, as_ritual: bool) -> int:
    """Calculate a reviewed minutes-or-hours cast after checking its prepared source."""
    time = spell.casting_time
    if time.condition or time.value is None or time.value <= 0:
        raise ValueError("casting requires a fixed positive duration")
    seconds_per_unit = {CastingTimeUnit.MINUTE: 60, CastingTimeUnit.HOUR: 3600}
    scale = seconds_per_unit.get(time.unit)
    if scale is None:
        raise ValueError("casting activity requires a spell of at least one minute")
    if as_ritual:
        return resolve_ritual_cast(spell, prepared=True).casting_time_minutes * 60
    return scale * time.value


def evaluate_cast_activity(request: RuleEvaluationRequest, loader: AssetLoader) -> TurnEvaluation:
    """Admit a prepared self-cast and price/effect only the completed phase."""
    from dnd5e_engine.evaluation_delta import StateDelta, WorldEffectCreate
    from dnd5e_engine.evaluation_effects import effect_state
    from dnd5e_engine.evaluation_payments import spell_payment
    from dnd5e_engine.evaluation_rng import RNGTransition
    from dnd5e_engine.evaluation_state import NonCombatSnapshot
    from dnd5e_engine.evaluation_timed_spells import _passive_profile, exploration_source_failure
    from dnd5e_engine.evaluation_turn import _refuse
    from dnd5e_engine.evaluation_world_effect import EffectExpiryPolicy, WorldEffectApplication
    from dnd5e_engine.events import EffectApplied, IntentSubmitted, SpellCast
    from dnd5e_engine.rules.conditions import conditions_block_actions
    from dnd5e_engine.spellcasting import spell_component_metadata

    snapshot, payload = request.state_snapshot, request.payload
    assert isinstance(snapshot, NonCombatSnapshot)
    assert isinstance(payload, ExplorationCastActivityPayload)
    if snapshot.combat_setup is not None:
        return _refuse("unsupported", "spell.snapshot", "casting needs exploration")
    spell = loader.get_spell(payload.spell_id)
    if spell is None or spell.slug != payload.spell_id:
        return _refuse("unsupported", "spell.asset", "selected spell is absent from the ruleset")
    if not payload.as_ritual and payload.slot_level != spell.level:
        return _refuse("unsupported", "spell.slot", "timed cast requires an ordinary base slot")
    if spell.concentration:
        return _refuse(
            "unsupported", "spell.mechanism", "ongoing concentration after casting is not admitted"
        )
    actor = next(
        (actor for actor in snapshot.character_states if actor.entity_id == request.actor_id), None
    )
    if (
        actor is None
        or not actor.is_alive
        or actor.hp_current <= 0
        or conditions_block_actions([condition.condition for condition in actor.conditions])
    ):
        return _refuse("rejected", "actor_incapacitated", "caster must be alive and able to act")
    if any(
        effect.target_id == actor.entity_id and not effect.disabled and effect.action_policy
        for effect in snapshot.effect_states
    ):
        return _refuse("unsupported", "spell.action_policy", "caster has an active action policy")
    if failure := exploration_source_failure(
        snapshot, actor, payload.spell_source_id, spell, loader
    ):
        return _refuse("rejected", "spell.source", failure)
    try:
        seconds = casting_activity_seconds(spell, as_ritual=payload.as_ritual)
        activity, active, _ = _passive_profile(
            spell, target_id=actor.entity_id, caster_id=actor.entity_id, casting_activity=True
        )
    except ValueError as exc:
        return _refuse("unsupported", "spell.mechanism", str(exc))
    if any(
        current.target_id == actor.entity_id and current.id == active.id
        for current in snapshot.effect_states
    ):
        return _refuse("rejected", "spell.effect_exists", "caster already has this effect")
    payment = None
    if payload.slot_level is not None:
        if snapshot.resource_state is None or actor.spell_slots.get(payload.slot_level, 0) <= 0:
            return _refuse("rejected", "spell.slot", "no owned ordinary slot remains")
        payment = spell_payment(snapshot.resource_state, actor.entity_id, payload.slot_level)
        if payment.expected.kind != "spell_slot":
            return _refuse("unsupported", "spell.slot", "timed cast needs an ordinary spell slot")
    transition = RNGTransition(
        stream_id=request.rng_context.stream_id,
        input_version=request.rng_context.version,
        input_state=request.rng_context.state,
        next_state=request.rng_context.state,
        state_changed=False,
    )
    if payload.phase == "start":
        return dict(
            status="accepted",
            state_delta=StateDelta(
                expected_world_version=snapshot.world_version,
                operations=(
                    WorldCastActivityBegin(
                        kind="world.cast_activity_begin",
                        activity_id=payload.activity_id,
                        duration_seconds=seconds,
                    ),
                ),
            ),
            proposed_events=(),
            rng_transition=transition,
            choice=None,
            error=None,
        )
    if payload.elapsed_seconds != seconds:
        return _refuse("rejected", "activity.duration", "casting did not reach its bound deadline")
    creation = WorldEffectCreate(
        kind="world.effect_create",
        expected_absent=True,
        value=WorldEffectApplication(
            session_id=request.session_id,
            command_id=request.command_id,
            source_id=actor.entity_id,
            effect=effect_state(active),
            expiry=EffectExpiryPolicy(action="remove"),
            ruleset_binding=request.ruleset_binding,
        ),
    )
    components, material, consumed, cost = spell_component_metadata(spell)
    return dict(
        status="accepted",
        state_delta=StateDelta(
            expected_world_version=snapshot.world_version,
            operations=(creation,) if payment is None else (payment, creation),
        ),
        proposed_events=(
            IntentSubmitted(
                actor_id=actor.entity_id,
                intent_type="cast_spell",
                spell_id=spell.slug,
                target_id=actor.entity_id,
                source_id=payload.spell_source_id,
                activity_id=activity.id,
            ),
            SpellCast(
                actor_id=actor.entity_id,
                spell_id=spell.slug,
                slot_level=payload.slot_level,
                ritual=payload.as_ritual,
                components=list(components),
                material=material,
                material_consumed=consumed,
                material_cost_gp=cost,
            ),
            EffectApplied(effect=active),
        ),
        rng_transition=transition,
        choice=None,
        error=None,
    )


def verify_cast_activity(result: RuleEvaluationResult, request: RuleEvaluationRequest) -> None:
    """Tie the closed phase proposal, evidence and payment to the fixed cast selection."""
    from dnd5e_engine.evaluation_delta import ResourceUpdate, WorldEffectCreate
    from dnd5e_engine.evaluation_effects import effect_state
    from dnd5e_engine.events import EffectApplied, IntentSubmitted, SpellCast

    payload = request.payload
    assert isinstance(payload, ExplorationCastActivityPayload)
    if result.status == "needs_choice":
        raise ValueError("timed self-cast cannot open a separate target choice")
    if result.status != "accepted":
        return
    assert result.state_delta is not None
    assert result.rng_transition is not None
    operations = result.state_delta.operations
    if result.rng_transition.state_changed:
        raise ValueError("timed passive casting carries no random draws")
    if payload.phase == "start":
        if (
            len(operations) != 1
            or not isinstance(operations[0], WorldCastActivityBegin)
            or operations[0].activity_id != payload.activity_id
            or result.proposed_events
        ):
            raise ValueError("timed casting start proposal does not match its activity")
        return
    if len(operations) != (1 if payload.as_ritual else 2):
        raise ValueError("timed casting completion has an invalid operation count")
    creation = operations[-1]
    if not isinstance(creation, WorldEffectCreate) or (
        creation.value.session_id,
        creation.value.command_id,
        creation.value.ruleset_binding,
        creation.value.source_id,
        creation.value.effect.target_id,
        creation.value.expiry.action,
        creation.value.concentration,
        creation.expected_absent,
    ) != (
        request.session_id,
        request.command_id,
        request.ruleset_binding,
        request.actor_id,
        request.actor_id,
        "remove",
        False,
        True,
    ):
        raise ValueError("timed casting effect differs from its closed self application")
    if not payload.as_ritual:
        payment = operations[0]
        resources = request.state_snapshot.resource_state
        if (
            not isinstance(payment, ResourceUpdate)
            or resources is None
            or payment.expected not in resources.pools
            or payment.expected.kind != "spell_slot"
            or payment.expected.level != payload.slot_level
            or payment.owner_id != request.actor_id
            or payment.amount != -1
        ):
            raise ValueError("timed spell payment differs from its selection")
    if len(result.proposed_events) != 3:
        raise ValueError("timed casting completion lacks its exact cast evidence")
    intent, cast, applied = result.proposed_events
    if (
        not isinstance(intent, IntentSubmitted)
        or intent.intent_type != "cast_spell"
        or intent.actor_id != request.actor_id
        or intent.target_id != request.actor_id
        or intent.spell_id != payload.spell_id
        or intent.source_id != payload.spell_source_id
        or not isinstance(cast, SpellCast)
        or cast.actor_id != request.actor_id
        or cast.spell_id != payload.spell_id
        or cast.slot_level != payload.slot_level
        or cast.ritual != payload.as_ritual
        or not isinstance(applied, EffectApplied)
        or effect_state(applied.effect) != creation.value.effect
    ):
        raise ValueError("timed casting evidence differs from the adopted application")
