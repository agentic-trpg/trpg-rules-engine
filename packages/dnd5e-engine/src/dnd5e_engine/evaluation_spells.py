"""Bounded real instant spells and Second Wind, without a Stateful executor."""

import random
from dataclasses import dataclass, replace
from typing import Literal

from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.common import (
    ActivationBlock,
    ActivityTiming,
    AttackActivity,
    AttackBlock,
    AttackTypeBlock,
    DamageCustomBlock,
    DamageScalingBlock,
    HealActivity,
    SaveActivity,
)
from dnd5e_srd_data.schema.spell import (
    CastingTimeUnit,
    Spell,
    SpellComponent,
    SpellDurationUnits,
    SpellMaterials,
    SpellRangeUnits,
)

from dnd5e_engine import attack_rules
from dnd5e_engine.action_economy_rules import action_economy_gate_failure
from dnd5e_engine.activities.actor_stats import ABILITY_CODES, save_modifier
from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.context import ActivityResolutionContext
from dnd5e_engine.activities.resolver import resolve_activity
from dnd5e_engine.activities.scale import build_scale_values
from dnd5e_engine.evaluation_actor import combatant
from dnd5e_engine.evaluation_computation import CombatComputation
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, RuleEvaluationRequest
from dnd5e_engine.evaluation_delta import ResourceUpdate
from dnd5e_engine.evaluation_paid_projection import payment_post_state, slot_ledger
from dnd5e_engine.evaluation_payments import feature_payments, spell_payment
from dnd5e_engine.evaluation_reactions import (
    prepare_counterspell,
    reaction_paid_delta,
    resolve_counterspell,
)
from dnd5e_engine.evaluation_rng import RNGTransition
from dnd5e_engine.evaluation_ruleset import get_default_ruleset
from dnd5e_engine.evaluation_speed_effects import (
    SpeedEffectComputation,
    attack_speed_effect_template,
    speed_effect_delta,
)
from dnd5e_engine.evaluation_spell_dependencies import (
    attack_spell_dependencies,
    invocation_dependencies,
)
from dnd5e_engine.evaluation_state import CharacterStateV2, CombatSnapshot
from dnd5e_engine.evaluation_turn import TurnEvaluation, _refuse
from dnd5e_engine.events import CastFailed, EffectApplied, IntentSubmitted, SpellCast
from dnd5e_engine.feature_runtime import DrawFreeRandom, FeatureInvocation, preflight_feature
from dnd5e_engine.lib_loader import scoped_lib_loader
from dnd5e_engine.rules.conditions import (
    active_condition_names,
    conditions_block_actions,
    project_passive_save_modifiers,
)
from dnd5e_engine.spatial import GridTopology
from dnd5e_engine.specs import GridScene
from dnd5e_engine.spell_delivery import DeliveryPlanningError, SpellDeliverySpec, plan_delivery
from dnd5e_engine.spell_execution import admission_failure, spell_review
from dnd5e_engine.spellcasting import (
    spell_component_failure,
    spell_component_metadata,
    spell_range_ft,
)
from dnd5e_engine.turn_rules import (
    bonus_action_payment,
    ordinary_action_payment,
    record_budget_changes,
)
from dnd5e_engine.types.combat import Combatant

type OrdinarySpellMechanism = Literal["healing", "save_damage", "attack_damage"]


@dataclass(frozen=True)
class InvocationPlan:
    """Retain an admitted spell/feature delivery plan and its payment inputs."""

    activity: HealActivity | SaveActivity | AttackActivity
    target_ids: tuple[str, ...]
    ability: str | None
    payment: tuple[ResourceUpdate, ...]
    spell: Spell | None
    slot_level: int | None
    attack_distance_ft: int | None = None
    hostile_adjacent: bool = False


def ordinary_spell_mechanism(loader: AssetLoader, spell_id: str) -> OrdinarySpellMechanism | None:
    """Classify one bound, complete instant spell without actor state or RNG.

    This is the ordinary-cast admission profile shared with the SM preflight.
    Source, target, resources and reaction legality still require evaluation.
    The separate Hold Person effect path is outside this profile.
    """
    if not spell_id:
        return None
    spell = loader.get_spell(spell_id)
    if spell is None or spell != get_default_ruleset().get_spell(spell_id):
        return None
    return _ordinary_spell_profile(spell)


def _reviewed_multi_healing(spell: Spell) -> bool:
    """Recognize the narrow rank-3 Bonus Action profile before common spell checks."""
    return (
        spell.level == 3
        and spell.casting_time.unit == CastingTimeUnit.BONUS
        and len(spell.activities) == 1
        and isinstance(activity := spell.activities[0], HealActivity)
        and activity.target.affects.count == "6"
    )


def _ordinary_spell_profile(spell: Spell) -> OrdinarySpellMechanism | None:
    """Select a complete typed ordinary activity from an already resolved spell."""
    multi_healing = _reviewed_multi_healing(spell)
    if (
        spell.level not in ((3,) if multi_healing else (0, 1))
        or spell.ritual
        or spell.concentration
        or spell.casting_time.unit not in (CastingTimeUnit.ACTION, CastingTimeUnit.BONUS)
        or spell.casting_time.value != 1
        or spell.duration.units != SpellDurationUnits.INSTANT
        or spell.range.units not in (SpellRangeUnits.TOUCH, SpellRangeUnits.FEET)
        or len(spell.activities) != 1
        or admission_failure(spell, spell.activities)
    ):
        return None
    activity = spell.activities[0]
    if (
        not isinstance(activity, (HealActivity, SaveActivity, AttackActivity))
        or activity.activation.type != "action"
        or (
            spell.casting_time.unit == CastingTimeUnit.BONUS
            and (
                not isinstance(activity, HealActivity)
                or activity.activation.override
                or spell.casting_time.condition
            )
        )
        or activity.duration.units != "inst"
        or activity.timing != ActivityTiming()
        or activity.applied_effects
        or activity.effect_selection is not None
        or activity.persistent_area is not None
        or activity.reaction is not None
        or activity.forced_movement is not None
        or activity.target.affects.count != ("6" if multi_healing else "1")
        or activity.target.affects.choice
        or activity.target.affects.special
        or activity.target.template.type
        or activity.target.area_semantics is not None
        or activity.target.creature_filter is not None
    ):
        return None
    if isinstance(activity, AttackActivity):
        if not _ordinary_spell_attack(spell, activity):
            return None
        if (activity.effects or spell.passive_effects) and attack_speed_effect_template(
            spell, activity
        ) is None:
            return None
        return "attack_damage"
    if activity.effects or spell.passive_effects:
        return None
    if activity.target.affects.type != "creature":
        return None
    if isinstance(activity, HealActivity):
        if spell.level == 0 or activity.healing.types != ["healing"]:
            return None
        return "healing"
    if multi_healing:
        return None
    if (
        len(activity.save.ability) != 1
        or activity.save.dc.calculation != "spellcasting"
        or len(activity.damage.parts) != 1
        or len(activity.damage.parts[0].types) != 1
        or activity.damage.on_save not in ("none", "half")
    ):
        return None
    return "save_damage"


def _ordinary_spell_attack(spell: Spell, activity: AttackActivity) -> bool:
    """Recognize a reviewed single ranged cantrip hit with ordinary typed damage dice."""
    default = AttackActivity()
    if (
        spell.level != 0
        or spell.casting_time.condition
        or spell.duration.value is not None
        or spell.range.units != SpellRangeUnits.FEET
        or not spell.range.value
        or spell.range.special
        or set(spell.components) != {SpellComponent.VOCAL, SpellComponent.SOMATIC}
        or spell.materials != SpellMaterials()
        or (review := spell_review(spell)) is None
        or review.classification != "executable"
        or len(review.activities) != 1
        or review.activities[0].role != "cast"
        or activity.activation != ActivationBlock(type="action")
        or activity.duration != default.duration
        or activity.range != default.range
        or activity.consumption != default.consumption
        or activity.uses != default.uses
        or activity.visibility != default.visibility
        or activity.flags
        or activity.action_type is not None
        or activity.target.override
        or activity.target.affects.type not in ("creature", "creatureOrObject")
        or activity.attack != AttackBlock(type=AttackTypeBlock(value="ranged"))
        or activity.damage.critical != default.damage.critical
        or len(activity.damage.parts) != 1
    ):
        return False
    part = activity.damage.parts[0]
    return (
        part.number == 1
        and part.denomination in (4, 6, 8, 10, 12)
        and len(part.types) == 1
        and bool(part.types[0])
        and not part.bonus
        and part.custom == DamageCustomBlock()
        and part.scaling == DamageScalingBlock(mode="whole", number=1)
    )


def invocation_context(
    computation: CombatComputation,
    plan: InvocationPlan,
    loader: AssetLoader,
    actor_id: str,
    rng: random.Random,
) -> ActivityResolutionContext:
    """Construct an isolated activity context for the admitted invocation."""
    actor = computation.actors[actor_id]
    levels = actor.classes or (
        {actor.class_slug: actor.character_level} if actor.class_slug else {}
    )
    targets = [computation.actors[target_id] for target_id in plan.target_ids]
    is_attack = isinstance(plan.activity, AttackActivity)
    save_modifiers = {}
    target_dodging = {}
    if isinstance(plan.activity, (AttackActivity, SaveActivity)):
        (target,) = targets
        dodging = attack_rules.dodge_benefit_active(
            target, effective_speed=computation.speed(target)
        )
        target_dodging[target.entity_id] = dodging
        if not is_attack:
            projection = project_passive_save_modifiers(active_condition_names(target.conditions))
            if dodging:
                projection["passive_save_adv"].append("DEX")
            save_modifiers[target.entity_id] = {
                "saves": {ab: save_modifier(target, ab).total for ab in ABILITY_CODES},
                **projection,
            }
    context = build_activity_context(
        actor,
        targets,
        rng=rng,
        event_emitter=computation.emit,
        slot_level=plan.slot_level,
        base_spell_level=plan.spell.level if plan.spell else None,
        spellcasting_ability=plan.ability,
        concentration=False,
        source_passive_effects=plan.spell.passive_effects if plan.spell else [],
        spell_book={},
        passive_damage_modifiers={}
        if isinstance(plan.activity, HealActivity)
        else attack_rules.damage_modifiers(computation.actors.values()),
        save_modifiers=save_modifiers,
        target_dodging=target_dodging,
        target_distance_ft=(
            {plan.target_ids[0]: plan.attack_distance_ft}
            if plan.attack_distance_ft is not None
            else {}
        ),
        attack_origin="spell" if is_attack else "default",
        damage_instance_id_provider=computation.next_damage_id,
        damage_instance_resolved=computation.damage_completed,
        class_levels=levels,
        scale_values=build_scale_values(
            class_slug=actor.class_slug,
            subclass_slug=None,
            species_slug=None,
            level=actor.character_level,
            classes=levels,
            loader=loader,
        ),
    )
    return replace(
        context,
        attacker_ranged_spell_in_melee=plan.hostile_adjacent,
        save_is_magical=plan.spell is not None,
        activity_source_id=f"spell:{plan.spell.slug}:{plan.activity.id}" if plan.spell else None,
        lifecycle_source_kind="spell" if plan.spell else "feature",
        lifecycle_source_slug=plan.spell.slug if plan.spell else "",
    )


def source_failure(
    snapshot: CombatSnapshot,
    actor_id: str,
    source_id: str | None,
    spell: Spell,
    mechanism: OrdinarySpellMechanism,
) -> str | None:
    """Check that the invocation source is present and supported by the bound dataset."""
    component = snapshot.casting_state
    actor = next(a for a in snapshot.character_states if a.entity_id == actor_id)
    entry = (
        next((a for a in component.actors if a.actor_id == actor_id), None) if component else None
    )
    source = next((s for s in entry.sources if s.source_id == source_id), None) if entry else None
    attack = mechanism == "attack_damage"
    multi_healing = _reviewed_multi_healing(spell)
    if multi_healing:
        class_slug, ability, level = "bard", "cha", 5
    elif attack:
        class_slug, ability, level = "wizard", "int", 1
    else:
        class_slug, ability, level = "cleric", "wis", 1
    if (
        entry is None
        or source is None
        or source.class_slug != class_slug
        or source.ability != ability
        or (attack and source.source_id != "class:wizard")
    ):
        return f"explicit reviewed {class_slug.capitalize()} casting source is required"
    levels = actor.classes or (
        {actor.class_slug: actor.character_level} if actor.class_slug else {}
    )
    if levels != {class_slug: level} or actor.spellcasting_ability != source.ability:
        return "casting source contradicts class/ability facts"
    if attack and (
        actor.attack_bonus is not None or actor.proficiency_bonus_override not in (None, 2)
    ):
        return "Wizard spell attack requires the ordinary Intelligence and proficiency formula"
    if spell.slug not in source.known_spells or (
        spell.level > 0 and spell.slug not in source.prepared_spells
    ):
        return "spell is not known and prepared from the selected source"
    return spell_component_failure(
        spell,
        verbal_available=entry.verbal_available,
        somatic_hand_available=actor.weapon_grip != "two_handed"
        and (actor.weapon_in_hands is None or not actor.other_hand_occupied),
        # The admitted instant spells use V/S components without materials.
        # No material or focus contract is invented for other spells.
        material_available=False,
    )


def spell_event(actor_id: str, spell: Spell, slot_level: int | None) -> SpellCast:
    """Construct event evidence for the selected spell invocation."""
    components, material, consumed, cost = spell_component_metadata(spell)
    review = spell_review(spell)
    return SpellCast(
        actor_id=actor_id,
        spell_id=spell.slug,
        slot_level=slot_level,
        ritual=spell.ritual,
        components=list(components),
        material=material,
        material_consumed=consumed,
        material_cost_gp=cost,
        execution_class=review.classification if review else None,
    )


def spell_plan(
    request: RuleEvaluationRequest,
    loader: AssetLoader,
    spell: Spell | None,
    mechanism: OrdinarySpellMechanism | None,
    actors: dict[str, CharacterStateV2],
) -> InvocationPlan | TurnEvaluation:
    """Prepare the admitted spell delivery and payment plan."""
    snapshot, intent = request.state_snapshot, request.payload
    assert isinstance(snapshot, CombatSnapshot)
    assert isinstance(intent, CombatIntentPayload)
    if mechanism is None:
        return _refuse(
            "unsupported",
            "spell.capability",
            "ordinary spell lacks a complete reviewed instant mechanism",
        )
    assert spell is not None
    multi_healing = _reviewed_multi_healing(spell)
    basic = CombatIntentPayload(
        intent_type="cast_spell",
        source_id=intent.source_id,
        spell_id=intent.spell_id,
        target_id=intent.target_id,
        target_ids=intent.target_ids if multi_healing else None,
        slot_level=intent.slot_level,
        spell_source_id=intent.spell_source_id,
    )
    if intent != basic:
        return _refuse("unsupported", "spell.inputs", "additional casting choices are not migrated")
    if intent.slot_level not in ((None, 0) if spell.level == 0 else (spell.level,)):
        return _refuse(
            "rejected",
            "no_slot",
            "Cantrips require no slot; a leveled spell requires its explicit base slot level",
        )
    assert intent.target_id is not None  # The public evaluator already admitted the primary.
    selected = intent.target_ids if intent.target_ids is not None else (intent.target_id,)
    if not selected or selected[0] != intent.target_id:
        return _refuse("rejected", "target_invalid", "first selected creature must be the primary")
    if multi_healing:
        if (
            snapshot.snapshot_schema_version != "engine-snapshot/20"
            or snapshot.combat_state.pending_reactions
        ):
            return _refuse(
                "unsupported", "spell.inputs", "multiple-target heal requires a plain v20 cast"
            )
        if any(
            (creature := actors.get(identity)) is None
            or not creature.is_alive
            or creature.hp_current <= 0
            or identity in snapshot.combat_state.dead_ids
            for identity in selected
        ):
            return _refuse("rejected", "target_invalid", "every target must be a living creature")
    if failure := source_failure(
        snapshot, request.actor_id, intent.spell_source_id, spell, mechanism
    ):
        return _refuse("rejected", "spell.source", failure)
    actor = actors[request.actor_id]
    # An empty encounter expenditure proves no paid spell here. Once nonempty,
    # this ledger cannot place a payment on the current turn when a budget was used.
    if (
        spell.level > 0
        and any(snapshot.combat_state.expended_resources.get(request.actor_id, {}).values())
        and (
            actor.action_taken_this_turn
            or actor.bonus_action_taken_this_turn
            or not actor.reaction_available
        )
    ):
        return _refuse(
            "unsupported", "spell.turn_history", "first-slot casting window cannot be proved"
        )
    creatures = [combatant(a) for a in snapshot.character_states]
    topology = GridTopology(GridScene.model_validate(snapshot.scene_state.grid.model_dump()))
    state = snapshot.combat_state
    caster_is_party = request.actor_id in state.party_ids
    enemies = state.encounter_ids if caster_is_party else state.party_ids
    allies = state.party_ids if caster_is_party else state.encounter_ids
    if mechanism == "attack_damage" and intent.target_id not in enemies:
        return _refuse("rejected", "target_invalid", "target must be a present opposing actor")
    try:
        delivery = plan_delivery(
            spell.activities,
            SpellDeliverySpec(primary_target_id=intent.target_id, selected_target_ids=selected),
            actor_id=request.actor_id,
            topology=topology,
            positions=state.actor_zone,
            creatures=creatures,
            enemy_ids=frozenset(enemies),
            ally_ids=frozenset(allies),
            range_ft=spell_range_ft(spell),
            execution=True,
            cast_level=spell.level,
            base_level=spell.level,
        )
        if len(delivery.activities) != 1 or delivery.activities[0].target_ids != selected:
            return _refuse("rejected", "target_invalid", "every target must be in range")
        target_ids = delivery.activities[0].target_ids
        assert snapshot.resource_state is not None
        payments = (
            (spell_payment(snapshot.resource_state, request.actor_id, spell.level),)
            if spell.level
            else ()
        )
    except DeliveryPlanningError as exc:
        return _refuse("rejected", exc.reason, str(exc))
    except ValueError:
        return _refuse("rejected", "no_slot", "no owned slot remains")
    activity = spell.activities[0]
    if isinstance(activity, AttackActivity):
        distance = topology.distance_ft(
            state.actor_zone[request.actor_id], state.actor_zone[intent.target_id]
        )
        assert distance is not None
        adjacent = attack_rules.hostile_adjacent_to_attacker(
            next(creature for creature in creatures if creature.entity_id == request.actor_id),
            creatures,
            allies=allies,
            dead_ids=state.dead_ids,
            positions=state.actor_zone,
            topology=topology,
            can_see=lambda hostile, attacker: True,
        )  # Dependencies admit only bright, unobstructed geometry.
        return InvocationPlan(
            activity, target_ids, "int", payments, spell, None, distance, adjacent
        )
    assert isinstance(activity, (HealActivity, SaveActivity))
    return InvocationPlan(
        activity, target_ids, "cha" if multi_healing else "wis", payments, spell, spell.level
    )


def feature_plan(
    request: RuleEvaluationRequest, loader: AssetLoader
) -> InvocationPlan | TurnEvaluation:
    """Prepare the admitted feature invocation and payment plan."""
    snapshot, intent = request.state_snapshot, request.payload
    assert isinstance(snapshot, CombatSnapshot)
    assert isinstance(intent, CombatIntentPayload)
    basic = CombatIntentPayload(
        intent_type="use_feature",
        source_id=intent.source_id,
        feature_id=intent.feature_id,
        activity_id=intent.activity_id,
        target_id=intent.target_id,
    )
    if (
        intent != basic
        or intent.feature_id != "second-wind"
        or intent.target_id != request.actor_id
    ):
        return _refuse(
            "unsupported", "feature.inputs", "only explicit self-targeted Second Wind is migrated"
        )
    feature = loader.get_feature("second-wind")
    actor = next(a for a in snapshot.character_states if a.entity_id == request.actor_id)
    if (
        feature is None
        or feature != get_default_ruleset().get_feature("second-wind")
        or "second-wind" not in (actor.granted_features or ())
    ):
        return _refuse("rejected", "feature.source", "actual reviewed Fighter grant is required")
    activity = feature.activities[0]
    if not isinstance(activity, HealActivity) or intent.activity_id != activity.id:
        return _refuse(
            "rejected", "feature.activity", "explicit canonical healing activity is required"
        )
    stub = InvocationPlan(activity, (request.actor_id,), None, (), None, None)
    context = invocation_context(
        CombatComputation(snapshot, DrawFreeRandom(0)),
        stub,
        loader,
        request.actor_id,
        DrawFreeRandom(0),
    )
    invocation = preflight_feature(
        feature,
        FeatureInvocation(activities=[activity], passive_effects=[], is_bonus_action=True),
        context,
        loader=loader,
        repertoire=actor.granted_features or (),
        spent={
            k.removeprefix("feature_use:"): v["spent"] for k, v in actor.custom_counters.items()
        },
    )
    assert snapshot.resource_state is not None
    try:
        payments = feature_payments(snapshot.resource_state, request.actor_id, invocation.payments)
    except ValueError as exc:
        return _refuse("rejected", "feature.exhausted", str(exc))
    return InvocationPlan(activity, (request.actor_id,), None, payments, None, None)


def evaluate_invocation(request: RuleEvaluationRequest, loader: AssetLoader) -> TurnEvaluation:
    """Admit and evaluate a supported spell or feature against fixed combat input.

    Check actor/turn, targets, action funding and complete bound source/dependency
    profile before payment and RNG. Resolve through shared activity/candidate machinery,
    with the reviewed Hold Person path delegated explicitly. Return guarded Delta,
    ordered events and RNG transition; unsupported mechanics are refused rather than
    approximated.
    """
    snapshot, intent = request.state_snapshot, request.payload
    assert isinstance(snapshot, CombatSnapshot)
    assert isinstance(intent, CombatIntentPayload)
    if intent.intent_type == "cast_spell" and intent.spell_id == "haste":
        from dnd5e_engine.evaluation_haste_cast import evaluate_haste_cast

        return evaluate_haste_cast(request, loader)
    if intent.intent_type == "cast_spell" and intent.spell_id == "hold-person":
        from dnd5e_engine.evaluation_hold_cast import evaluate_hold_cast

        return evaluate_hold_cast(request, loader)
    state = snapshot.combat_state
    if state.ended or request.actor_id not in state.initiative_ids:
        return _refuse("rejected", "combat_ended", "live combat actor is required")
    if request.actor_id != state.initiative_ids[state.current_turn_index]:
        return _refuse("rejected", "not_actor_turn", "actor does not own this turn")
    current = next(a for a in snapshot.character_states if a.entity_id == request.actor_id)
    if (
        not current.is_alive
        or current.hp_current <= 0
        or request.actor_id in state.dead_ids
        or conditions_block_actions([c.condition for c in current.conditions])
    ):
        return _refuse("rejected", "actor_incapacitated", "caster must be alive and conscious")
    targets = {a.entity_id: a for a in snapshot.character_states}
    if (
        intent.target_id not in targets
        or targets[intent.target_id].hp_current <= 0
        or not targets[intent.target_id].is_alive
    ):
        return _refuse(
            "rejected",
            "target_invalid",
            "living positive-HP target is required; revival is not migrated",
        )
    if state.pending_reactions and intent.intent_type != "cast_spell":
        return _refuse(
            "unsupported",
            "reaction.trigger",
            "only the reviewed spell-cast opportunity is migrated",
        )
    spell = None
    mechanism = None
    if intent.intent_type == "cast_spell" and intent.spell_id:
        spell = loader.get_spell(intent.spell_id)
        if spell is not None:
            canonical_spell = get_default_ruleset().get_spell(intent.spell_id)
            canonical_mechanism = (
                _ordinary_spell_profile(canonical_spell) if canonical_spell is not None else None
            )
            if canonical_mechanism is not None and spell != canonical_spell:
                return _refuse(
                    "unsupported",
                    "spell.binding",
                    "spell definition differs from reviewed canonical data",
                )
            if spell == canonical_spell:
                mechanism = canonical_mechanism
    if intent.intent_type == "cast_spell" and mechanism is None:
        return _refuse(
            "unsupported",
            "spell.capability",
            "ordinary spell lacks a complete reviewed instant mechanism",
        )
    creates_speed_effect = (
        mechanism == "attack_damage"
        and spell is not None
        and isinstance(spell.activities[0], AttackActivity)
        and attack_speed_effect_template(spell, spell.activities[0]) is not None
    )
    if creates_speed_effect and (
        request.schema_version != "engine-evaluation/30" or snapshot.effect_states
    ):
        return _refuse(
            "unsupported",
            "spell.effect_dependencies",
            "initial speed rider requires plain /30 combat",
        )
    if mechanism == "attack_damage":
        if failure := attack_spell_dependencies(snapshot, loader, intent.target_id):
            return _refuse("unsupported", "spell.attack_dependencies", failure)
    elif failure := invocation_dependencies(
        snapshot,
        loader,
        intent.target_id,
        damaging=mechanism == "save_damage",
        ordinary_spell=mechanism is not None,
        lore_healing_caster_id=(
            request.actor_id if spell is not None and _reviewed_multi_healing(spell) else None
        ),
    ):
        return _refuse("unsupported", "healing.dependencies", failure)
    actor = combatant(current)
    feature = intent.intent_type == "use_feature"
    # An inherited activity activation describes its payload, not the spell's cost.
    bonus = feature or (spell is not None and spell.casting_time.unit == CastingTimeUnit.BONUS)
    if action_economy_gate_failure(actor, intent, is_bonus_action=bonus, is_reaction_cast=False):
        return _refuse("rejected", "no_action_economy", "required action budget is unavailable")
    with scoped_lib_loader(loader):
        plan = (
            feature_plan(request, loader)
            if feature
            else spell_plan(request, loader, spell, mechanism, targets)
        )
        if not isinstance(plan, InvocationPlan):
            return plan
        return _resolve_invocation(
            request,
            loader,
            plan,
            actor,
            bonus=bonus,
            creates_speed_effect=creates_speed_effect,
        )


def _resolve_invocation(
    request: RuleEvaluationRequest,
    loader: AssetLoader,
    plan: InvocationPlan,
    actor: Combatant,
    *,
    bonus: bool,
    creates_speed_effect: bool,
) -> TurnEvaluation:
    """Resolve the admitted activity and project its private candidate and payments."""
    snapshot, intent = request.state_snapshot, request.payload
    assert isinstance(snapshot, CombatSnapshot)
    assert isinstance(intent, CombatIntentPayload)
    state = snapshot.combat_state
    reaction = (
        prepare_counterspell(snapshot, request.actor_id, plan.spell, loader) if plan.spell else None
    )
    rng = request.rng_context.state.restore()
    if creates_speed_effect:
        computation: CombatComputation = SpeedEffectComputation(snapshot, rng)
    elif snapshot.effect_states:
        from dnd5e_engine.evaluation_concentration import ConcentrationComputation

        computation = ConcentrationComputation(snapshot, rng)
    else:
        computation = CombatComputation(snapshot, rng)
    computation.emit(
        IntentSubmitted(
            actor_id=request.actor_id,
            intent_type=intent.intent_type,
            spell_id=intent.spell_id,
            target_id=intent.target_id,
            item_id=intent.item_id,
        )
    )
    computation.actors[request.actor_id] = actor.model_copy(
        update=bonus_action_payment() if bonus else ordinary_action_payment(actor)
    )
    cancelled = resolve_counterspell(computation, reaction, rng) if reaction else False
    payments: tuple[ResourceUpdate, ...] = (reaction.payment,) if reaction else ()
    if cancelled:
        computation.emit(
            CastFailed(actor_id=request.actor_id, spell_id=intent.spell_id, reason="countered")
        )
    else:
        payments += plan.payment
        if plan.spell is not None:
            computation.emit(spell_event(request.actor_id, plan.spell, plan.slot_level or None))
        resolve_activity(
            plan.activity, invocation_context(computation, plan, loader, request.actor_id, rng)
        )
    computation.finish_turn(request.actor_id, allow_movement=bonus)
    computation.actors[request.actor_id] = record_budget_changes(
        actor, computation.actors[request.actor_id]
    )
    ledgers = tuple(
        slot_ledger(snapshot, payment)
        for payment in payments
        if payment.value.kind in ("spell_slot", "pact_slot")
    )
    if ledgers:
        computation.state = computation.state.model_copy(
            update={
                "expended_resources": {
                    **state.expended_resources,
                    **{ledger.owner_id: ledger.value for ledger in ledgers},
                }
            }
        )
    if computation.damage:
        raise RuntimeError("invocation left an incomplete damage instance")
    computed = computation.result()
    after = payment_post_state(computed.candidate_state, payments)
    if creates_speed_effect:
        if payments or ledgers:
            raise RuntimeError("speed effect spell attack attempted an unsupported payment")
        if not after.effect_states and any(
            isinstance(event, EffectApplied) for event in computed.proposed_events
        ):
            return _refuse(
                "unsupported",
                "spell.effect_consequence",
                "initial effect would also expire during this attack's turn advance",
            )
    return dict(
        status="accepted",
        state_delta=(
            speed_effect_delta(snapshot, after)
            if creates_speed_effect
            else reaction_paid_delta(snapshot, after, payments, ledgers)
        ),
        proposed_events=computed.proposed_events,
        rng_transition=RNGTransition.between(request.rng_context, rng),
        choice=None,
        error=None,
    )
