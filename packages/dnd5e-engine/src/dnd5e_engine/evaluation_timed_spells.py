"""Evaluate direct exploration casts that create one elapsed-time passive modifier."""

from dnd5e_srd_data.loader import AssetLoader, BundledAssetLoader
from dnd5e_srd_data.schema.class_ import SpellcastingProgression
from dnd5e_srd_data.schema.common import (
    ActivityTiming,
    AppliedEffectRef,
    ConsumptionBlock,
    DurationBlock,
    PassiveEffect,
    RangeBlock,
    UsesBlock,
    UtilityActivity,
    VisibilityBlock,
)
from dnd5e_srd_data.schema.spell import Spell, SpellDurationUnits, SpellRangeUnits

from dnd5e_engine.activities.effects import passive_effect_to_active_effect
from dnd5e_engine.activities.passive_stats import interpret_passive_stats
from dnd5e_engine.areas import has_line_of_effect
from dnd5e_engine.evaluation_casting import CastingStateV20
from dnd5e_engine.evaluation_contracts import ExplorationSpellCastPayload, RuleEvaluationRequest
from dnd5e_engine.evaluation_delta import StateDelta, WorldEffectCreate
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_payments import spell_payment
from dnd5e_engine.evaluation_projection import EvaluationInvariantError
from dnd5e_engine.evaluation_rng import RNGTransition
from dnd5e_engine.evaluation_state import CharacterStateV2, NonCombatSnapshot
from dnd5e_engine.evaluation_turn import TurnEvaluation, _refuse
from dnd5e_engine.evaluation_world_effect import EffectExpiryPolicy, WorldEffectApplication
from dnd5e_engine.events import EffectApplied, IntentSubmitted, SpellCast
from dnd5e_engine.rules.conditions import conditions_block_actions
from dnd5e_engine.spatial import GridTopology, canonical_cell_id
from dnd5e_engine.specs import GridScene
from dnd5e_engine.spellcasting import (
    effective_caster_level,
    resolve_target_count,
    slots_for_caster_level,
    spell_component_failure,
    spell_component_metadata,
    spell_range_ft,
)
from dnd5e_engine.types.effects import ActiveEffectDuration

_SECONDS_PER_UNIT = {
    SpellDurationUnits.MINUTE: 60,
    SpellDurationUnits.HOUR: 3600,
    SpellDurationUnits.DAY: 86400,
}


def _passive_profile(spell: Spell) -> tuple[UtilityActivity, PassiveEffect, int]:
    """Read the supported base-level utility, passive modifier and duration from rule data."""
    seconds_per_unit = _SECONDS_PER_UNIT.get(spell.duration.units)
    if (
        spell.review.requires_user_decision
        or spell.concentration
        or spell.ritual
        or spell.casting_time.unit != "action"
        or spell.casting_time.value != 1
        or spell.casting_time.condition
        or spell.range.units not in (SpellRangeUnits.TOUCH, SpellRangeUnits.SELF)
        or spell.range.special
        or spell.range.value is not None
        or spell.materials.cost
        or spell.materials.consumed
        or spell.materials.supply
        or seconds_per_unit is None
        or not spell.duration.value
        or len(spell.activities) != 1
        or len(spell.passive_effects) != 1
    ):
        raise ValueError("requires one finite nonconcentration touch or self spell")
    activity = spell.activities[0]
    passive = spell.passive_effects[0]
    if not isinstance(activity, UtilityActivity) or not activity.id or not passive.id:
        raise ValueError("requires one identifiable utility activity and its passive effect")
    target = activity.target
    template = target.template
    activation = activity.activation
    if (
        activation.type not in ("", "action")
        or activation.value not in (None, 1)
        or activation.condition
        or activation.override
        or activation.reaction_conditions
        or activity.consumption != ConsumptionBlock()
        or activity.duration != DurationBlock()
        or activity.range != RangeBlock()
        or activity.uses != UsesBlock()
        or activity.visibility != VisibilityBlock()
        or activity.timing != ActivityTiming()
        or activity.flags
        or activity.reaction is not None
        or activity.persistent_area is not None
        or activity.forced_movement is not None
        or activity.action_type is not None
        or activity.roll.formula
        or activity.effect_selection is not None
        or activity.effects != [AppliedEffectRef(id=passive.id)]
        or activity.applied_effects not in ([], [passive.id])
        or target.override
        or target.requires_willing
        or target.requires_sight
        or target.area_semantics is not None
        or target.creature_filter is not None
        or template.type
        or template.count
        or template.size
        or template.width
        or template.height
        or template.contiguous
        or template.stationary
        or target.affects.choice
        or target.affects.special
        or target.affects.type
        not in (
            ("creature", "self") if spell.range.units == SpellRangeUnits.SELF else ("creature",)
        )
        or resolve_target_count(target.affects.count, cast_level=spell.level) != 1
    ):
        raise ValueError(
            "utility must immediately apply exactly one passive effect to one creature"
        )
    seconds = spell.duration.value * seconds_per_unit
    # The Foundry import can retain a one-round default even when the spell has
    # an explicit elapsed duration. The world-time result carries seconds only.
    duration = passive.duration or {}
    if (
        not passive.name.strip()
        or passive.disabled
        or passive.transfer
        or passive.statuses
        or passive.action_policy is not None
        or duration not in ({"seconds": seconds}, {"seconds": seconds, "rounds": 1})
        or any(type(value) is not int for value in duration.values())
        or not passive.changes
    ):
        raise ValueError("requires an enabled passive effect with matching elapsed duration")
    modifiers = interpret_passive_stats(
        changes=passive.changes, trait_grants=(), species_senses=None
    )
    if modifiers.skipped_keys or any(
        not change.key.startswith(("system.attributes.movement.", "system.attributes.senses."))
        or "@" in change.value
        for change in passive.changes
    ):
        raise ValueError("requires concrete passive movement or sense modifiers")
    return activity, passive, seconds


def _source_failure(
    snapshot: NonCombatSnapshot,
    actor: CharacterStateV2,
    payload: ExplorationSpellCastPayload,
    spell: Spell,
    loader: AssetLoader,
) -> str | None:
    """Check the selected class repertoire and its fixed physical component facts."""
    casting = snapshot.casting_state
    if not isinstance(casting, CastingStateV20):
        return "canonical casting source is missing"
    entry = next((item for item in casting.actors if item.actor_id == actor.entity_id), None)
    if entry is None:
        return "actor has no casting source"
    source = next(
        (item for item in entry.sources if item.source_id == payload.spell_source_id), None
    )
    if source is None:
        return "selected spell source does not belong to the caster"
    levels = actor.classes or (
        {actor.class_slug: actor.character_level} if actor.class_slug else {}
    )
    level = levels.get(source.class_slug, 0)
    class_asset = loader.get_class(source.class_slug)
    if (
        not 1 <= level <= 20
        or class_asset is None
        or class_asset.spellcasting.ability != source.ability
        or class_asset.spellcasting.progression
        in (SpellcastingProgression.NONE, SpellcastingProgression.PACT)
    ):
        return "casting source conflicts with the actor's class and bound casting rules"
    class_slots = slots_for_caster_level(
        effective_caster_level(class_asset.spellcasting.progression, level)
    )
    if spell.level not in class_slots:
        return "the selected class cannot yet prepare spells at this level"
    selection = next((item for item in source.spells if item.spell_id == spell.slug), None)
    if selection is None or selection.prepared_order is None:
        return "spell is not known and prepared from the selected source"
    material = selection.material
    hand_available = actor.weapon_grip != "two_handed" and (
        actor.weapon_in_hands is None or not actor.other_hand_occupied
    )
    material_available = False
    if hand_available and material is not None and material.available and "M" in spell.components:
        owned = next(
            (
                item
                for item in snapshot.inventory_state.entries
                if item.instance_id == material.inventory_instance_id
            ),
            None,
        )
        if (
            owned is not None
            and owned.owner_id == actor.entity_id
            and owned.item_slug == "component-pouch"
            and owned.quantity == 1
            and owned.accessible
            and owned.charges_remaining_per_unit == 0
        ):
            pouch = loader.get_item(owned.item_slug)
            material_available = pouch is not None and pouch == BundledAssetLoader().get_item(
                owned.item_slug
            )
    return spell_component_failure(
        spell,
        verbal_available=entry.verbal_available,
        somatic_hand_available=hand_available,
        material_available=material_available,
    )


def _touch_reaches(
    request: RuleEvaluationRequest, snapshot: NonCombatSnapshot, spell: Spell
) -> bool:
    """Check distinct-creature touch using the fixed scene and shared geometry rules."""
    positions = request.spell_positions
    reach = spell_range_ft(spell)
    if positions is None or reach is None:
        return False
    try:
        topology = GridTopology(GridScene.model_validate(snapshot.scene_state.grid.model_dump()))
        source = canonical_cell_id(positions.source_cell)
        target = canonical_cell_id(positions.target_cell)
    except ValueError:
        return False
    return (
        topology.is_valid_cell(source)
        and topology.is_valid_cell(target)
        and topology.within_range(source, target, reach)
        and has_line_of_effect(topology, source, target)
    )


def evaluate_timed_spell(request: RuleEvaluationRequest, loader: AssetLoader) -> TurnEvaluation:
    """Admit one exploration cast and propose its atomic slot and effect changes.

    The result carries ordered cast evidence and an unchanged RNG successor. The
    host owns effect persistence, elapsed time and expiry.
    """
    snapshot, payload = request.state_snapshot, request.payload
    if not isinstance(snapshot, NonCombatSnapshot) or not isinstance(
        payload, ExplorationSpellCastPayload
    ):
        raise EvaluationInvariantError("world spell evaluation requires a noncombat spell request")
    if snapshot.combat_setup is not None:
        return _refuse("unsupported", "spell.snapshot", "exploration requires no pending combat")
    spell = loader.get_spell(payload.spell_id)
    if spell is None or spell.slug != payload.spell_id:
        return _refuse("unsupported", "spell.asset", "selected spell is absent from the ruleset")
    if not 1 <= spell.level == payload.slot_level <= 9:
        return _refuse(
            "unsupported", "spell.slot", "cast requires a single ordinary base-level slot"
        )
    try:
        activity, passive, seconds = _passive_profile(spell)
    except ValueError as exc:
        return _refuse("unsupported", "spell.mechanism", str(exc))

    actors = {actor.entity_id: actor for actor in snapshot.character_states}
    actor = actors.get(request.actor_id)
    target = actors.get(payload.target_id)
    if actor is None:
        return _refuse("rejected", "actor_missing", "caster is absent from the snapshot")
    if (
        not actor.is_alive
        or actor.hp_current <= 0
        or conditions_block_actions([condition.condition for condition in actor.conditions])
    ):
        return _refuse("rejected", "actor_incapacitated", "caster must be alive and able to act")
    if target is None or not target.is_alive:
        return _refuse("rejected", "target_invalid", "cast requires a living creature")
    if target.entity_id != actor.entity_id:
        if spell.range.units == SpellRangeUnits.SELF:
            return _refuse("rejected", "target_invalid", "self spell must target the caster")
        if not _touch_reaches(request, snapshot, spell):
            return _refuse("rejected", "target_invalid", "target must be reachable by touch")
    if any(
        active.target_id == actor.entity_id and not active.disabled and active.action_policy
        for active in snapshot.effect_states
    ):
        return _refuse("unsupported", "spell.action_policy", "caster has an active action policy")
    if failure := _source_failure(snapshot, actor, payload, spell, loader):
        return _refuse("rejected", "spell.source", failure)

    active = passive_effect_to_active_effect(
        passive, target_id=target.entity_id, caster_id=actor.entity_id
    )
    active = active.model_copy(update={"duration": ActiveEffectDuration(seconds=seconds)})
    if any(
        current.target_id == target.entity_id and current.id == active.id
        for current in snapshot.effect_states
    ):
        return _refuse("rejected", "spell.effect_exists", "target already has this effect")
    if snapshot.resource_state is None or actor.spell_slots.get(payload.slot_level, 0) <= 0:
        return _refuse("rejected", "spell.slot", "no owned ordinary slot remains")
    payment = spell_payment(snapshot.resource_state, actor.entity_id, payload.slot_level)
    if payment.expected.kind != "spell_slot":
        raise EvaluationInvariantError("world spell payment must use an ordinary slot")
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
            expected_world_version=snapshot.world_version, operations=(payment, creation)
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
            SpellCast(
                actor_id=actor.entity_id,
                spell_id=spell.slug,
                slot_level=payload.slot_level,
                ritual=False,
                components=list(components),
                material=material,
                material_consumed=consumed,
                material_cost_gp=cost,
            ),
            EffectApplied(effect=active),
        ),
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
