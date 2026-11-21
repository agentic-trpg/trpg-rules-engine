"""Evaluate exploration Light and Darkness from fixed spatial and casting facts."""

from functools import lru_cache
from typing import Literal

from dnd5e_srd_data.loader import AssetLoader, BundledAssetLoader
from dnd5e_srd_data.schema.environment import EnvironmentalSpec
from dnd5e_srd_data.schema.spell import Spell, SpellDurationUnits

from dnd5e_engine.areas import area_activity, area_template
from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest, RuleEvaluationResult
from dnd5e_engine.evaluation_delta import ResourceUpdate, StateDelta, StateDeltaOperation
from dnd5e_engine.evaluation_environmental_contracts import (
    EnvironmentalItemTarget,
    ExplorationEnvironmentalSpellCastPayload,
    WorldEnvironmentalEffect,
    WorldEnvironmentalEffectCreate,
)
from dnd5e_engine.evaluation_payments import spell_payment
from dnd5e_engine.evaluation_projection import EvaluationInvariantError
from dnd5e_engine.evaluation_rng import RNGTransition
from dnd5e_engine.evaluation_state import NonCombatSnapshot
from dnd5e_engine.evaluation_timed_spells import (
    exploration_source_failure,
    exploration_spell_reaches,
)
from dnd5e_engine.evaluation_turn import TurnEvaluation, _refuse
from dnd5e_engine.events import IntentSubmitted, SpellCast
from dnd5e_engine.rules.conditions import conditions_block_actions
from dnd5e_engine.spellcasting import spell_component_metadata

_CLASSES = {
    "light": frozenset({"bard", "cleric", "sorcerer", "wizard"}),
    "darkness": frozenset({"sorcerer", "warlock", "wizard"}),
}
_LIGHT = EnvironmentalSpec(kind="light", light="bright", dim_extension_ft=20, object_anchoring=True)
_SECONDS_PER_UNIT = {SpellDurationUnits.MINUTE: 60, SpellDurationUnits.HOUR: 3600}


@lru_cache(maxsize=len(_CLASSES))
def _reviewed_spell(spell_id: str) -> Spell | None:
    """Read only the supported bundled spell, kept private for exact profile checks."""
    return BundledAssetLoader().get_spell(spell_id) if spell_id in _CLASSES else None


def _effect(
    actor_id: str, payload: ExplorationEnvironmentalSpellCastPayload, spell: Spell
) -> WorldEnvironmentalEffectCreate:
    """Price the ruleset's illumination and lifecycle policy independently of World."""
    recasts = spell.slug == "light"
    shape: Literal["sphere", "emanation"]
    if recasts:
        # SRD 5.2.1 p.144. The corpus activity represents a VTT summon profile.
        # The spell instead enchants an existing object and creates no creature.
        shape, radius, environment = "sphere", 20, _LIGHT
    else:
        activity = area_activity(spell.activities)
        if activity is None:
            raise EvaluationInvariantError("canonical Darkness lacks an area activity")
        template = area_template(activity, cast_level=payload.slot_level, base_level=spell.level)
        environment = activity.persistent_area.environment
        if template is None or environment is None:
            raise EvaluationInvariantError("canonical Darkness lacks an environmental area")
        shape = "emanation" if isinstance(payload.target, EnvironmentalItemTarget) else "sphere"
        radius = template.size_ft
    duration = spell.duration.value
    if not duration:
        raise EvaluationInvariantError("canonical illumination spell requires elapsed duration")
    return WorldEnvironmentalEffectCreate(
        kind="world.environmental_effect_create",
        value=WorldEnvironmentalEffect(
            source_id=actor_id,
            spell_source_id=payload.spell_source_id,
            spell_id=spell.slug,
            slot_level=payload.slot_level,
            duration_seconds=_SECONDS_PER_UNIT[spell.duration.units] * duration,
            concentration=spell.concentration,
            replace_same_caster_on_recast=recasts,
            anchor=payload.target,
            environment=environment,
            radius_ft=radius,
            shape=shape,
        ),
    )


def _events(
    actor_id: str, payload: ExplorationEnvironmentalSpellCastPayload, spell: Spell
) -> tuple[IntentSubmitted, SpellCast]:
    """Describe the cast and its component evidence using the existing event vocabulary."""
    components, material, consumed, cost = spell_component_metadata(spell)
    return (
        IntentSubmitted(
            actor_id=actor_id,
            intent_type="cast_spell",
            spell_id=spell.slug,
            item_id=payload.target.item_id
            if isinstance(payload.target, EnvironmentalItemTarget)
            else None,
            source_id=payload.spell_source_id,
        ),
        SpellCast(
            actor_id=actor_id,
            spell_id=spell.slug,
            slot_level=payload.slot_level or None,
            ritual=False,
            components=list(components),
            material=material,
            material_consumed=consumed,
            material_cost_gp=cost,
        ),
    )


def _payment(snapshot: NonCombatSnapshot, actor_id: str, level: int) -> ResourceUpdate:
    """Select an owned ordinary slot from the fixed snapshot."""
    resources = snapshot.resource_state
    if resources is None:
        raise ValueError("no owned ordinary slot remains")
    payment = spell_payment(resources, actor_id, level)
    if payment.expected.kind != "spell_slot":
        raise ValueError("no owned ordinary slot remains")
    return payment


def _operations(
    request: RuleEvaluationRequest,
    snapshot: NonCombatSnapshot,
    payload: ExplorationEnvironmentalSpellCastPayload,
    spell: Spell,
) -> tuple[StateDeltaOperation, ...]:
    """Pair a slot spend when needed with the one proposed effect application."""
    effect = _effect(request.actor_id, payload, spell)
    if not payload.slot_level:
        return (effect,)
    return (_payment(snapshot, request.actor_id, payload.slot_level), effect)


def _target_failure(request: RuleEvaluationRequest, spell: Spell) -> str | None:
    """Check the SRD target-specific size and possession requirements."""
    positions = request.spell_positions
    assert positions is not None
    item = positions.target_item
    if spell.slug == "light":
        if item is None or item.size not in ("tiny", "small", "medium", "large"):
            return "Light requires a Large or smaller object of known size"
        if item.worn_or_carried_by not in (None, request.actor_id):
            return "Light cannot target an object worn or carried by someone else"
    elif item is not None and item.worn_or_carried_by is not None:
        return "Darkness cannot target a worn or carried object"
    return None


def _slot_valid(payload: ExplorationEnvironmentalSpellCastPayload, spell: Spell) -> bool:
    """Recognize a slotless cantrip or a valid base-level or higher ordinary slot."""
    return payload.slot_level == 0 if spell.level == 0 else payload.slot_level >= spell.level


def evaluate_environmental_spell(
    request: RuleEvaluationRequest, loader: AssetLoader
) -> TurnEvaluation:
    """Propose illumination and payment after draw-free mechanical admission."""
    snapshot, payload = request.state_snapshot, request.payload
    if not isinstance(snapshot, NonCombatSnapshot) or not isinstance(
        payload, ExplorationEnvironmentalSpellCastPayload
    ):
        raise EvaluationInvariantError("environmental spell requires a spatial noncombat request")
    classes = _CLASSES.get(payload.spell_id)
    spell = loader.get_spell(payload.spell_id) if classes is not None else None
    if spell is None or classes is None or spell != _reviewed_spell(payload.spell_id):
        return _refuse(
            "unsupported", "spell.asset", "requires a reviewed canonical environmental spell"
        )
    if not _slot_valid(payload, spell):
        return _refuse("rejected", "spell.slot", "slot level is invalid for this spell")

    actor = next(
        (item for item in snapshot.character_states if item.entity_id == request.actor_id), None
    )
    if actor is None:
        return _refuse("rejected", "actor_missing", "caster is absent from the snapshot")
    if (
        not actor.is_alive
        or actor.hp_current <= 0
        or conditions_block_actions([condition.condition for condition in actor.conditions])
    ):
        return _refuse("rejected", "actor_incapacitated", "caster must be alive and able to act")
    if any(
        effect.target_id == actor.entity_id and not effect.disabled and effect.action_policy
        for effect in snapshot.effect_states
    ):
        return _refuse("unsupported", "spell.action_policy", "caster has an active action policy")
    if failure := _target_failure(request, spell):
        return _refuse("rejected", "target_invalid", failure)
    if not exploration_spell_reaches(request, snapshot, spell):
        return _refuse("rejected", "target_invalid", "target requires range and a clear path")
    if failure := exploration_source_failure(
        snapshot, actor, payload.spell_source_id, spell, loader, allowed_classes=classes
    ):
        return _refuse("rejected", "spell.source", failure)
    try:
        operations = _operations(request, snapshot, payload, spell)
    except ValueError as exc:
        return _refuse("rejected", "spell.slot", str(exc))
    return dict(
        status="accepted",
        state_delta=StateDelta(
            expected_world_version=snapshot.world_version, operations=operations
        ),
        proposed_events=_events(request.actor_id, payload, spell),
        rng_transition=RNGTransition(
            stream_id=request.rng_context.stream_id,
            input_version=request.rng_context.version,
            input_state=request.rng_context.state,
            next_state=request.rng_context.state,
            state_changed=False,
        ),
        choice=None,
        error=None,
    )


def verify_environmental_cast(result: RuleEvaluationResult, request: RuleEvaluationRequest) -> None:
    """Bind the exact profile, target, slot payment and draw-free result to its input."""
    payload, snapshot = request.payload, request.state_snapshot
    if not isinstance(payload, ExplorationEnvironmentalSpellCastPayload) or not isinstance(
        snapshot, NonCombatSnapshot
    ):
        raise TypeError("environmental effect requires its original spatial world request")
    spell = _reviewed_spell(payload.spell_id)
    if (
        spell is None
        or not _slot_valid(payload, spell)
        or _target_failure(request, spell)
        or result.state_delta is None
        or result.state_delta.operations != _operations(request, snapshot, payload, spell)
        or result.proposed_events != _events(request.actor_id, payload, spell)
        or result.rng_transition is None
        or result.rng_transition.state_changed
    ):
        raise ValueError("environmental cast result differs from its fixed spell input")
