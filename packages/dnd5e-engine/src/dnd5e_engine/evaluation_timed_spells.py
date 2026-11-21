"""Admit direct exploration casts and resolve elapsed-time passive modifiers."""

from typing import cast

from dnd5e_srd_data.loader import AssetLoader, BundledAssetLoader
from dnd5e_srd_data.schema.class_ import SpellcastingProgression
from dnd5e_srd_data.schema.common import (
    ActivityTiming,
    AppliedEffectRef,
    ConsumptionBlock,
    DurationBlock,
    PassiveEffect,
    PassiveEffectChange,
    RangeBlock,
    UsesBlock,
    UtilityActivity,
    VisibilityBlock,
)
from dnd5e_srd_data.schema.spell import CastingTimeUnit, Spell, SpellDurationUnits, SpellRangeUnits

from dnd5e_engine.activities.check_pipeline import validate_check_effect_change
from dnd5e_engine.activities.effects import passive_effect_to_active_effect
from dnd5e_engine.activities.passive_stats import interpret_passive_stats
from dnd5e_engine.areas import has_line_of_effect
from dnd5e_engine.evaluation_casting import CastingStateV20
from dnd5e_engine.evaluation_contracts import (
    ExplorationSpellCastPayload,
    RuleEvaluationRequest,
    SpellWillingnessChoice,
)
from dnd5e_engine.evaluation_delta import StateDelta, WorldEffectCreate
from dnd5e_engine.evaluation_effects import EffectState, effect_state
from dnd5e_engine.evaluation_exploration_healing import (
    evaluate_exploration_healing,
    exploration_healing_activity,
)
from dnd5e_engine.evaluation_payments import spell_payment
from dnd5e_engine.evaluation_projection import EvaluationInvariantError
from dnd5e_engine.evaluation_rng import RNGTransition
from dnd5e_engine.evaluation_spells import source_failure
from dnd5e_engine.evaluation_state import CharacterStateV2, NonCombatSnapshot
from dnd5e_engine.evaluation_turn import TurnEvaluation, _refuse
from dnd5e_engine.evaluation_world_effect import EffectExpiryPolicy, WorldEffectApplication
from dnd5e_engine.events import EffectApplied, IntentSubmitted, SpellCast
from dnd5e_engine.rules.armor_class import ARMOR_CLASS_FLOOR_KEYS, armor_class_floor_value
from dnd5e_engine.rules.conditions import conditions_block_actions
from dnd5e_engine.spatial import GridTopology, canonical_cell_id
from dnd5e_engine.specs import GridScene
from dnd5e_engine.spellcasting import (
    SpellcastingProgression as ProgressionValue,
)
from dnd5e_engine.spellcasting import (
    effective_caster_level,
    resolve_target_count,
    slots_for_caster_level,
    spell_component_failure,
    spell_component_metadata,
    spell_range_ft,
)
from dnd5e_engine.types.effects import ActiveEffect, ActiveEffectChange, ActiveEffectDuration

_SECONDS_PER_UNIT = {
    SpellDurationUnits.MINUTE: 60,
    SpellDurationUnits.HOUR: 3600,
    SpellDurationUnits.DAY: 86400,
}


def _flat_skill_bonus(change: ActiveEffectChange) -> bool:
    """Identify one named skill's reviewed additive bonus without a dice expression."""
    return (
        change.key.startswith("check.skill.")
        and change.mode == "add"
        and isinstance(change.value, str)
        and "d" not in change.value
        and validate_check_effect_change(change)
    )


def _validate_passive_changes(passive: PassiveEffect, active: ActiveEffect) -> None:
    """Validate each elapsed utility modifier before proposing payment."""
    stat_changes = []
    for source, change in zip(passive.changes, active.changes, strict=True):
        if "@" in source.value:
            raise ValueError("timed spell modifiers require concrete values")
        if source.key.startswith(("system.attributes.movement.", "system.attributes.senses.")):
            stat_changes.append(source)
        elif change.key in ARMOR_CLASS_FLOOR_KEYS:
            armor_class_floor_value(change)
        elif not _flat_skill_bonus(change):
            raise ValueError("unsupported timed spell modifier")
    if (
        stat_changes
        and interpret_passive_stats(
            changes=stat_changes, trait_grants=(), species_senses=None
        ).skipped_keys
    ):
        raise ValueError("unsupported passive movement or sense modifier")


def _passive_profile(
    spell: Spell, *, target_id: str, caster_id: str, max_target_capacity: int = 1
) -> tuple[UtilityActivity, ActiveEffect, int]:
    """Build an admitted base-level utility and elapsed-time effect from rule data."""
    seconds_per_unit = _SECONDS_PER_UNIT.get(spell.duration.units)
    if (
        spell.review.requires_user_decision
        or spell.ritual
        or spell.casting_time.unit not in (CastingTimeUnit.ACTION, CastingTimeUnit.BONUS)
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
        raise ValueError("requires one finite touch or self spell")
    activity = spell.activities[0]
    passive = spell.passive_effects[0]
    if not isinstance(activity, UtilityActivity) or not activity.id or not passive.id:
        raise ValueError("requires one identifiable utility activity and its passive effect")
    target = activity.target
    target_capacity = resolve_target_count(target.affects.count, cast_level=spell.level)
    allowed_targets: tuple[str, ...] = ("creature", "willing")
    if spell.range.units == SpellRangeUnits.SELF:
        allowed_targets += ("self",)
    template = target.template
    activation = activity.activation
    if (
        activation.type not in ("", "action", spell.casting_time.unit)
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
        or target.affects.type not in allowed_targets
        or target_capacity is None
        or not 1 <= target_capacity <= max_target_capacity
    ):
        raise ValueError(
            "utility must immediately apply one passive effect within its supported target limit"
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
    active = passive_effect_to_active_effect(
        passive, target_id=target_id, caster_id=caster_id, concentration=spell.concentration
    )
    _validate_passive_changes(passive, active)
    if spell.concentration and not all(_flat_skill_bonus(change) for change in active.changes):
        raise ValueError("elapsed concentration supports only flat skill bonuses")
    return (
        activity,
        active.model_copy(update={"duration": ActiveEffectDuration(seconds=seconds)}),
        target_capacity,
    )


def attack_world_spell_modifiers(effect: EffectState) -> tuple[int, int | None]:
    """Verify the elapsed spell shape and return its walking bonus and Armor Class floor.

    The fixed effect carries the resolved cast data. Its origin names the application,
    while its host owns the world deadline. Unreviewed modifiers or lifecycle fields
    raise ValueError before payment or RNG use.
    """
    label = effect.id.removeprefix("effect:")
    prefix = f"cast:{label}:"
    duration = effect.duration
    if (
        not effect.id.startswith("effect:")
        or not label
        or not effect.origin.startswith(prefix)
        or not effect.origin.removeprefix(prefix).strip()
        or effect.disabled
        or effect.transfer
        or not effect.changes
        or effect.action_policy is not None
        or effect.statuses
        or effect.flags
        or effect.lifecycle is not None
        or effect.end_effects
        or duration.seconds is None
        or duration.seconds <= 0
        or any(
            value is not None
            for value in (
                duration.rounds,
                duration.turns,
                duration.start_round,
                duration.start_turn,
            )
        )
    ):
        raise ValueError("attack requires an enabled elapsed-time passive spell effect")
    walk = []
    floors = []
    for change in effect.changes:
        if (
            change.key == "system.attributes.movement.walk"
            and change.mode == "add"
            and isinstance(change.value, str)
        ):
            walk.append(PassiveEffectChange(key=change.key, mode=2, value=change.value))
        elif change.key in ARMOR_CLASS_FLOOR_KEYS:
            floors.append(armor_class_floor_value(change))
        elif not _flat_skill_bonus(change):
            raise ValueError("world spell may affect unreviewed attack dependencies")
    stats = interpret_passive_stats(changes=walk, trait_grants=(), species_senses=None)
    if stats.skipped_keys:
        raise ValueError("world spell requires a flat walking-speed bonus")
    return stats.walk_speed_bonus, max(floors, default=None)


def exploration_source_failure(
    snapshot: NonCombatSnapshot,
    actor: CharacterStateV2,
    spell_source_id: str,
    spell: Spell,
    loader: AssetLoader,
    *,
    allowed_classes: frozenset[str] | None = None,
) -> str | None:
    """Check class repertoire, spell-level access and fixed physical component facts."""
    casting = snapshot.casting_state
    if not isinstance(casting, CastingStateV20):
        return "canonical casting source is missing"
    entry = next((item for item in casting.actors if item.actor_id == actor.entity_id), None)
    if entry is None:
        return "actor has no casting source"
    source = next((item for item in entry.sources if item.source_id == spell_source_id), None)
    if source is None:
        return "selected spell source does not belong to the caster"
    if allowed_classes is not None and source.class_slug not in allowed_classes:
        return "the selected class cannot learn this spell"
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
    progression = cast(ProgressionValue, str(class_asset.spellcasting.progression))
    class_slots = slots_for_caster_level(effective_caster_level(progression, level))
    if spell.level and spell.level not in class_slots:
        return "the selected class cannot yet prepare spells at this level"
    selection = next((item for item in source.spells if item.spell_id == spell.slug), None)
    if selection is None or (spell.level and selection.prepared_order is None):
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


def exploration_spell_reaches(
    request: RuleEvaluationRequest, snapshot: NonCombatSnapshot, spell: Spell
) -> bool:
    """Check distance and a clear path using the fixed scene and shared geometry rules."""
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


def evaluate_exploration_spell(
    request: RuleEvaluationRequest, loader: AssetLoader
) -> TurnEvaluation:
    """Admit one exploration cast and propose its slot and mechanical changes.

    A reviewed touch heal resolves immediately. The timed branch supports
    movement, senses, Armor Class floors and flat skill bonuses with unchanged RNG.
    Flat skill bonuses may require concentration. A willing-creature profile
    requires self-targeting or verified willingness.
    A distinct target with unknown willingness yields a choice before payment.
    The host owns persistence, elapsed time and expiry.
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
    healing = exploration_healing_activity(loader, spell)
    if healing is None:
        try:
            activity, active, _ = _passive_profile(
                spell, target_id=payload.target_id, caster_id=request.actor_id
            )
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
        if not exploration_spell_reaches(request, snapshot, spell):
            return _refuse("rejected", "target_invalid", "target must be reachable by touch")
    if any(
        effect.target_id == actor.entity_id and not effect.disabled and effect.action_policy
        for effect in snapshot.effect_states
    ):
        return _refuse("unsupported", "spell.action_policy", "caster has an active action policy")
    failure = (
        source_failure(snapshot, actor.entity_id, payload.spell_source_id, spell, "healing")
        if healing is not None
        else exploration_source_failure(snapshot, actor, payload.spell_source_id, spell, loader)
    )
    if failure:
        return _refuse("rejected", "spell.source", failure)

    if healing is None and any(
        current.target_id == target.entity_id
        and current.id == active.id
        and not (
            spell.concentration
            and actor.concentration_effect_id == current.id
            and current.origin == active.origin
            and current.flags.get("concentration") is True
        )
        for current in snapshot.effect_states
    ):
        return _refuse("rejected", "spell.effect_exists", "target already has this effect")
    if snapshot.resource_state is None or actor.spell_slots.get(payload.slot_level, 0) <= 0:
        return _refuse("rejected", "spell.slot", "no owned ordinary slot remains")
    positions = request.spell_positions
    if (
        healing is None
        and activity.target.affects.type == "willing"
        and target.entity_id != actor.entity_id
        and (positions is None or positions.willing_target_id != target.entity_id)
    ):
        return dict(
            status="needs_choice",
            state_delta=None,
            proposed_events=(),
            rng_transition=None,
            choice=SpellWillingnessChoice(
                kind="spell.target_willingness",
                actor_id=actor.entity_id,
                target_id=target.entity_id,
            ),
            error=None,
        )
    payment = spell_payment(snapshot.resource_state, actor.entity_id, payload.slot_level)
    if payment.expected.kind != "spell_slot":
        raise EvaluationInvariantError("world spell payment must use an ordinary slot")
    if healing is not None:
        return evaluate_exploration_healing(request, spell, healing, actor, target, payment, loader)
    creation = WorldEffectCreate(
        kind="world.effect_create",
        expected_absent=True,
        value=WorldEffectApplication(
            session_id=request.session_id,
            command_id=request.command_id,
            source_id=actor.entity_id,
            effect=effect_state(active),
            expiry=EffectExpiryPolicy(action="remove"),
            concentration=spell.concentration,
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
