"""Admit direct exploration casts and resolve elapsed-time passive modifiers."""

from functools import lru_cache
from hashlib import sha256
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
from dnd5e_engine.evaluation_beacon import beacon_effect, reviewed_beacon_spell
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
    exploration_healing_requires_sight,
)
from dnd5e_engine.evaluation_payments import spell_payment
from dnd5e_engine.evaluation_projection import EvaluationInvariantError
from dnd5e_engine.evaluation_rng import RNGTransition
from dnd5e_engine.evaluation_spell_materials import consumed_material_slug, spell_material_payment
from dnd5e_engine.evaluation_spell_termination import reviewed_invisibility
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
    derive_pact_slots,
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
    spell: Spell,
    *,
    target_id: str,
    caster_id: str,
    max_target_capacity: int = 1,
    allow_consumed_material: bool = False,
    casting_activity: bool = False,
) -> tuple[UtilityActivity, ActiveEffect, int]:
    """Build an admitted utility effect, including an inert Ritual concentration anchor."""
    if reviewed_beacon_spell(spell) and not casting_activity and not allow_consumed_material:
        activity = spell.activities[0]
        assert isinstance(activity, UtilityActivity)
        return activity, beacon_effect(spell, target_id, caster_id), 1
    seconds_per_unit = _SECONDS_PER_UNIT.get(spell.duration.units)
    time = spell.casting_time
    ritual_anchor = (
        casting_activity
        and spell.ritual
        and spell.concentration
        and len(spell.passive_effects) == 1
        and not spell.passive_effects[0].changes
    )
    ready_timing = (
        (
            time.unit in (CastingTimeUnit.MINUTE, CastingTimeUnit.HOUR)
            or (ritual_anchor and time.unit == CastingTimeUnit.ACTION)
        )
        and time.value is not None
        and time.value > 0
        if casting_activity
        else not spell.ritual
        and time.unit in (CastingTimeUnit.ACTION, CastingTimeUnit.BONUS)
        and time.value == 1
    )
    if (
        spell.review.requires_user_decision
        or not ready_timing
        or time.condition
        or spell.range.units not in (SpellRangeUnits.TOUCH, SpellRangeUnits.SELF)
        or spell.range.special
        or spell.range.value is not None
        or (
            not allow_consumed_material
            and (spell.materials.cost or spell.materials.consumed or spell.materials.supply)
        )
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
    if allow_consumed_material:
        consumed_material_slug(spell, activity.consumption)
    target = activity.target
    target_capacity = resolve_target_count(target.affects.count, cast_level=spell.level)
    if ritual_anchor and target.affects.type == "self" and not target.affects.count:
        target_capacity = 1
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
        or (
            not allow_consumed_material
            and activity.consumption
            not in (
                (ConsumptionBlock(), ConsumptionBlock(spell_slot=True))
                if ritual_anchor
                else (ConsumptionBlock(),)
            )
        )
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
        or (template.type and not ritual_anchor)
        or (ritual_anchor and template.type != "radius")
        or template.count
        or (template.size and not ritual_anchor)
        or (ritual_anchor and (not template.size.isdecimal() or int(template.size) <= 0))
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
    invisible = reviewed_invisibility(spell)
    if (
        not passive.name.strip()
        or passive.disabled
        or passive.transfer
        or passive.statuses != (["invisible"] if invisible else [])
        or passive.action_policy is not None
        or duration not in ({"seconds": seconds}, {"seconds": seconds, "rounds": 1})
        or any(type(value) is not int for value in duration.values())
        or (not invisible and not passive.changes and not ritual_anchor)
    ):
        raise ValueError("requires an enabled passive effect with matching elapsed duration")
    active = passive_effect_to_active_effect(
        passive, target_id=target_id, caster_id=caster_id, concentration=spell.concentration
    )
    if invisible:
        active = active.model_copy(
            update={"id": "effect:invisibility", "origin": f"cast:invisibility:{caster_id}"}
        )
    _validate_passive_changes(passive, active)
    if (
        spell.concentration
        and not ritual_anchor
        and not all(_flat_skill_bonus(change) for change in active.changes)
    ):
        raise ValueError("elapsed concentration supports only flat skill bonuses")
    return (
        activity,
        active.model_copy(update={"duration": ActiveEffectDuration(seconds=seconds)}),
        target_capacity,
    )


@lru_cache(maxsize=1)
def _reviewed_longstrider() -> tuple[Spell, EffectState]:
    """Cache the bundled definition and normalized elapsed passive effect."""
    spell = BundledAssetLoader().get_spell("longstrider")
    if spell is None:
        raise EvaluationInvariantError("bundled Longstrider is missing")
    _, active, _ = _passive_profile(spell, target_id="reviewed-target", caster_id="reviewed-source")
    return spell, effect_state(active)


def is_canonical_elapsed_longstrider(effect: EffectState) -> bool:
    """Recognize the reviewed flat walk effect independently of its cast identity."""
    prefix = "cast:longstrider:"
    if (
        effect.id != "effect:longstrider"
        or not effect.origin.startswith(prefix)
        or not effect.origin.removeprefix(prefix).strip()
    ):
        return False
    _, template = _reviewed_longstrider()
    return effect == template.model_copy(
        update={"target_id": effect.target_id, "origin": effect.origin}
    )


def _admit_elapsed_overlap(
    active: ActiveEffect,
    spell: Spell,
    actor: CharacterStateV2,
    snapshot: NonCombatSnapshot,
    command_id: str,
) -> ActiveEffect | None:
    """Keep distinct reviewed overlaps and refuse unsupported same-name effects.

    Preserve the original source identity for the first active cast from that
    source. Qualify overlapping recasts by their command. The host keeps each
    exact instance and its own elapsed deadline.
    """
    overlapping = tuple(
        effect
        for effect in snapshot.effect_states
        if effect.target_id == active.target_id and effect.id == active.id
    )
    if not overlapping or (
        spell.concentration
        and all(
            actor.concentration_effect_id == effect.id
            and effect.origin == active.origin
            and effect.flags.get("concentration") is True
            for effect in overlapping
        )
    ):
        return active
    if (
        spell.slug != "longstrider"
        or spell != _reviewed_longstrider()[0]
        or not all(is_canonical_elapsed_longstrider(effect) for effect in overlapping)
    ):
        return None
    origins = {effect.origin for effect in overlapping}
    if active.origin not in origins:
        return active
    suffix = sha256(command_id.encode("utf-8")).hexdigest()
    origin = f"{active.origin}:recast:{suffix}"
    return None if origin in origins else active.model_copy(update={"origin": origin})


def _timed_effect_creation(
    request: RuleEvaluationRequest, spell: Spell, active: ActiveEffect
) -> WorldEffectCreate:
    """Build the effect proposal with the host association and terminal policy."""
    return WorldEffectCreate(
        kind="world.effect_create",
        expected_absent=True,
        value=WorldEffectApplication(
            session_id=request.session_id,
            command_id=request.command_id,
            source_id=request.actor_id,
            effect=effect_state(active),
            expiry=EffectExpiryPolicy(action="remove"),
            concentration=spell.concentration,
            ruleset_binding=request.ruleset_binding,
        ),
    )


def verify_timed_spell_effect(
    request: RuleEvaluationRequest, spell: Spell, creation: WorldEffectCreate
) -> None:
    """Verify one timed effect against its bound definition and frozen overlap facts."""
    snapshot, payload = request.state_snapshot, request.payload
    if (
        not isinstance(snapshot, NonCombatSnapshot)
        or not isinstance(payload, ExplorationSpellCastPayload)
        or spell.slug != payload.spell_id
    ):
        raise ValueError("timed effect requires its bound exploration spell")
    actor = next(
        (actor for actor in snapshot.character_states if actor.entity_id == request.actor_id),
        None,
    )
    if actor is None or not any(
        target.entity_id == payload.target_id for target in snapshot.character_states
    ):
        raise ValueError("timed effect requires its caster and target")
    _, active, _ = _passive_profile(
        spell,
        target_id=payload.target_id,
        caster_id=request.actor_id,
        allow_consumed_material=payload.material_instance_id is not None,
    )
    admitted = _admit_elapsed_overlap(active, spell, actor, snapshot, request.command_id)
    if admitted is None or creation != _timed_effect_creation(request, spell, admitted):
        raise ValueError("timed effect differs from its bound consequences")


def validate_elapsed_spell_shape(effect: EffectState, *, allow_concentration: bool = False) -> None:
    """Require an enabled elapsed passive or an explicitly supported concentration marker."""
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
        or (
            effect.flags
            and not (
                allow_concentration
                and len(effect.flags) == 1
                and effect.flags.get("concentration") is True
            )
        )
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
        raise ValueError("requires an enabled elapsed-time passive spell effect")


def is_healing_neutral_elapsed_spell(effect: EffectState) -> bool:
    """Check the elapsed movement, senses, Armor Class and skill-only vocabulary."""
    validate_elapsed_spell_shape(effect, allow_concentration=True)
    stat_changes = []
    for change in effect.changes:
        if change.key.startswith(("system.attributes.movement.", "system.attributes.senses.")):
            if not isinstance(change.value, str) or change.mode not in ("add", "upgrade"):
                return False
            stat_changes.append(
                PassiveEffectChange(
                    key=change.key, mode=2 if change.mode == "add" else 4, value=change.value
                )
            )
        elif change.key in ARMOR_CLASS_FLOOR_KEYS:
            armor_class_floor_value(change)
        elif not _flat_skill_bonus(change):
            return False
    return (
        not stat_changes
        or not interpret_passive_stats(
            changes=stat_changes, trait_grants=(), species_senses=None
        ).skipped_keys
    )


def attack_world_spell_modifiers(effect: EffectState) -> tuple[int, int | None]:
    """Verify elapsed passive changes used by attacks and return walk and Armor Class."""
    validate_elapsed_spell_shape(effect)
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
    allow_pact: bool = False,
    has_selected_material: bool = False,
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
        or class_asset.spellcasting.progression == SpellcastingProgression.NONE
        or (
            class_asset.spellcasting.progression == SpellcastingProgression.PACT
            and (
                not allow_pact
                or source.class_slug != "warlock"
                or class_asset != BundledAssetLoader().get_class("warlock")
            )
        )
    ):
        return "casting source conflicts with the actor's class and bound casting rules"
    progression = cast(ProgressionValue, str(class_asset.spellcasting.progression))
    # The single Pact slot level is the highest spell level the Warlock can prepare.
    can_prepare_level = (
        spell.level <= max(derive_pact_slots(level))
        if progression == "pact"
        else spell.level in slots_for_caster_level(effective_caster_level(progression, level))
    )
    if spell.level and not can_prepare_level:
        return "the selected class cannot yet prepare spells at this level"
    selection = next((item for item in source.spells if item.spell_id == spell.slug), None)
    if selection is None or (spell.level and selection.prepared_order is None):
        return "spell is not known and prepared from the selected source"
    material = selection.material
    hand_available = actor.weapon_grip != "two_handed" and (
        actor.weapon_in_hands is None or not actor.other_hand_occupied
    )
    material_available = hand_available and has_selected_material
    if (
        not has_selected_material
        and hand_available
        and material is not None
        and material.available
        and "M" in spell.components
    ):
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
    request: RuleEvaluationRequest,
    snapshot: NonCombatSnapshot,
    spell: Spell,
    *,
    visible_target_id: str | None = None,
) -> bool:
    """Check fixed range, clear path and any required host-confirmed sight."""
    positions = request.spell_positions
    reach = spell_range_ft(spell)
    if (
        positions is None
        or reach is None
        or (visible_target_id is not None and positions.visible_target_id != visible_target_id)
    ):
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

    A reviewed touch or visible-target heal resolves immediately. The timed branch supports
    movement, senses, Armor Class floors and flat skill bonuses with unchanged RNG.
    Flat skill bonuses and reviewed single-target Invisibility may require
    concentration. A willing-creature profile requires self-targeting or verified willingness.
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
    healing = exploration_healing_activity(loader, spell)
    upcast = (
        payload.slot_level > spell.level
        and spell == _reviewed_longstrider()[0]
        and resolve_target_count(
            spell.activities[0].target.affects.count, cast_level=payload.slot_level
        )
    )
    healing_upcast = healing is not None and spell.slug == "cure-wounds" and payload.slot_level == 2
    if not 1 <= spell.level <= payload.slot_level <= 9 or (
        spell.level != payload.slot_level and not (upcast or healing_upcast)
    ):
        return _refuse(
            "unsupported", "spell.slot", "cast requires an ordinary slot of a reviewed level"
        )
    material_instance = payload.material_instance_id
    material_debit = None
    if healing is None or material_instance is not None:
        try:
            activity, active, _ = _passive_profile(
                spell,
                target_id=payload.target_id,
                caster_id=request.actor_id,
                allow_consumed_material=material_instance is not None,
            )
        except ValueError as exc:
            return _refuse("unsupported", "spell.mechanism", str(exc))
        if material_instance is not None:
            try:
                material_debit = spell_material_payment(
                    spell,
                    activity.consumption,
                    snapshot.inventory_state,
                    actor_id=request.actor_id,
                    instance_id=material_instance,
                    loader=loader,
                )
            except ValueError:
                return _refuse("rejected", "spell.material", "selected material is not available")

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
    needs_sight = healing is not None and exploration_healing_requires_sight(spell)
    positions = request.spell_positions
    if needs_sight or target.entity_id != actor.entity_id:
        if spell.range.units == SpellRangeUnits.SELF:
            return _refuse("rejected", "target_invalid", "self spell must target the caster")
        if not exploration_spell_reaches(
            request, snapshot, spell, visible_target_id=target.entity_id if needs_sight else None
        ):
            return _refuse("rejected", "target_invalid", "required spell delivery is unproven")
    if any(
        effect.target_id == actor.entity_id and not effect.disabled and effect.action_policy
        for effect in snapshot.effect_states
    ):
        return _refuse("unsupported", "spell.action_policy", "caster has an active action policy")
    failure = (
        source_failure(
            snapshot,
            actor.entity_id,
            payload.spell_source_id,
            spell,
            "healing",
            reviewed_exploration_cleric=True,
        )
        if healing is not None
        else exploration_source_failure(
            snapshot,
            actor,
            payload.spell_source_id,
            spell,
            loader,
            has_selected_material=material_debit is not None,
        )
    )
    if failure:
        return _refuse("rejected", "spell.source", failure)

    if healing is None:
        admitted = _admit_elapsed_overlap(active, spell, actor, snapshot, request.command_id)
        if admitted is None:
            return _refuse("rejected", "spell.effect_exists", "target already has this effect")
        active = admitted
    if snapshot.resource_state is None or actor.spell_slots.get(payload.slot_level, 0) <= 0:
        return _refuse("rejected", "spell.slot", "no owned ordinary slot remains")
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
    creation = _timed_effect_creation(request, spell, active)
    components, material, consumed, cost = spell_component_metadata(spell)
    return dict(
        status="accepted",
        state_delta=StateDelta(
            expected_world_version=snapshot.world_version,
            operations=(
                (payment, creation)
                if material_debit is None
                else (payment, material_debit, creation)
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
