"""Resolve one reviewed exploration spell's selected independent condition."""

from dnd5e_srd_data.schema.common import UtilityActivity
from dnd5e_srd_data.schema.spell import CastingTimeUnit, Spell, SpellDurationUnits, SpellRangeUnits

from dnd5e_engine.evaluation_contracts import CONDITION_CHOICE_SCHEMA_VERSION, RuleEvaluationRequest
from dnd5e_engine.evaluation_delta import ConditionsUpdate, StateDelta
from dnd5e_engine.evaluation_rng import RNGTransition
from dnd5e_engine.evaluation_ruleset import get_default_ruleset
from dnd5e_engine.evaluation_spells import spell_event
from dnd5e_engine.evaluation_state import CharacterStateV2
from dnd5e_engine.evaluation_turn import TurnEvaluation, _refuse
from dnd5e_engine.events import ConditionRemoved, IntentSubmitted
from dnd5e_engine.spell_execution import admission_failure


def condition_choice_profile(spell: Spell) -> tuple[UtilityActivity, frozenset[str]] | None:
    """Read a complete bound selection rule; leave unreviewed spells unsupported."""
    canonical = get_default_ruleset().get_spell("lesser-restoration")
    if (
        spell != canonical
        or spell.slug != "lesser-restoration"
        or admission_failure(spell, spell.activities, condition_removal=True)
    ):
        return None
    if (
        spell.level != 2
        or spell.casting_time.unit != CastingTimeUnit.BONUS
        or spell.casting_time.value != 1
        or spell.concentration
        or spell.ritual
        or spell.duration.units != SpellDurationUnits.INSTANT
        or spell.range.units != SpellRangeUnits.TOUCH
        or len(spell.activities) != 1
        or not isinstance(activity := spell.activities[0], UtilityActivity)
        or activity.target.affects.type != "creature"
        or activity.target.affects.count != "1"
        or activity.effects
        or activity.applied_effects
        or spell.passive_effects
    ):
        return None
    return activity, frozenset({"blinded", "deafened", "paralyzed", "poisoned"})


def evaluate_condition_choice(
    request: RuleEvaluationRequest,
    spell: Spell,
    actor: CharacterStateV2,
    target: CharacterStateV2,
    payment,
    profile: tuple[UtilityActivity, frozenset[str]],
) -> TurnEvaluation:
    """Remove one independent source after legal selection, without drawing RNG."""
    payload = request.payload
    choice = payload.selected_condition
    activity, eligible = profile
    if request.schema_version != CONDITION_CHOICE_SCHEMA_VERSION or choice not in eligible:
        return _refuse("rejected", "spell.condition_choice", "selected condition is ineligible")
    matches = [
        i
        for i, record in enumerate(target.conditions)
        if record.condition == choice and record.source_effect_id is None
    ]
    if not matches:
        return _refuse(
            "rejected", "spell.condition_choice", "selected independent condition is absent"
        )
    # LF1122-F40-01: the public name cannot identify one of multiple independent sources.
    if len(matches) != 1:
        return _refuse(
            "rejected",
            "spell.condition_choice_ambiguous",
            "multiple independent sources require a source-bound selection",
        )
    index = matches[0]
    proposed = (*target.conditions[:index], *target.conditions[index + 1 :])
    return dict(
        status="accepted",
        state_delta=StateDelta(
            expected_world_version=request.state_snapshot.world_version,
            operations=(
                payment,
                ConditionsUpdate(
                    kind="actor.conditions_update",
                    target_id=target.entity_id,
                    expected=tuple(target.conditions),
                    value=proposed,
                ),
            ),
        ),
        proposed_events=(
            IntentSubmitted(
                actor_id=actor.entity_id,
                intent_type="cast_spell",
                spell_id=spell.slug,
                target_id=target.entity_id,
                source_id=payload.spell_source_id,
                activity_id=activity.id,
            ),
            spell_event(actor.entity_id, spell, payload.slot_level),
            ConditionRemoved(target_id=target.entity_id, condition=choice),
        ),
        rng_transition=RNGTransition.between(
            request.rng_context, request.rng_context.state.restore()
        ),
        choice=None,
        error=None,
    )
