"""Bounded real Cure Wounds and Second Wind, without a Stateful executor."""

import random
from dataclasses import dataclass

from dnd5e_srd_data import BundledAssetLoader
from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.common import HealActivity
from dnd5e_srd_data.schema.spell import Spell

from dnd5e_engine.action_economy_rules import action_economy_gate_failure
from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.context import ActivityResolutionContext
from dnd5e_engine.activities.resolver import resolve_activity
from dnd5e_engine.activities.scale import build_scale_values
from dnd5e_engine.evaluation_actor import combatant
from dnd5e_engine.evaluation_computation import CombatComputation
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, RuleEvaluationRequest
from dnd5e_engine.evaluation_delta import ResourceUpdate
from dnd5e_engine.evaluation_paid_projection import paid_delta, payment_post_state, slot_ledger
from dnd5e_engine.evaluation_payments import feature_payments, spell_payment
from dnd5e_engine.evaluation_rng import RNGTransition
from dnd5e_engine.evaluation_spell_dependencies import healing_dependencies
from dnd5e_engine.evaluation_state import CombatSnapshot
from dnd5e_engine.evaluation_turn import TurnEvaluation, _refuse
from dnd5e_engine.events import IntentSubmitted, SpellCast
from dnd5e_engine.feature_runtime import DrawFreeRandom, FeatureInvocation, preflight_feature
from dnd5e_engine.lib_loader import scoped_lib_loader
from dnd5e_engine.spatial import GridTopology
from dnd5e_engine.specs import GridScene
from dnd5e_engine.spell_delivery import DeliveryPlanningError, SpellDeliverySpec, plan_delivery
from dnd5e_engine.spell_execution import admission_failure, spell_review
from dnd5e_engine.spellcasting import spell_component_metadata, spell_range_ft
from dnd5e_engine.turn_rules import (
    bonus_action_payment,
    ordinary_action_payment,
    record_budget_changes,
)


@dataclass(frozen=True)
class HealingPlan:
    activity: HealActivity
    target_id: str
    ability: str | None
    payment: tuple[ResourceUpdate, ...]
    spell: Spell | None
    slot_level: int | None


def healing_context(
    computation: CombatComputation,
    plan: HealingPlan,
    loader: AssetLoader,
    actor_id: str,
    rng: random.Random,
) -> ActivityResolutionContext:
    actor = computation.actors[actor_id]
    levels = actor.classes or (
        {actor.class_slug: actor.character_level} if actor.class_slug else {}
    )
    return build_activity_context(
        actor,
        [computation.actors[plan.target_id]],
        rng=rng,
        event_emitter=computation.emit,
        slot_level=plan.slot_level,
        base_spell_level=plan.spell.level if plan.spell else None,
        spellcasting_ability=plan.ability,
        concentration=False,
        source_passive_effects=[],
        spell_book={},
        passive_damage_modifiers={},
        save_modifiers={},
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


def source_failure(
    snapshot: CombatSnapshot, actor_id: str, source_id: str | None, spell: Spell
) -> str | None:
    component = snapshot.casting_state
    actor = next(a for a in snapshot.character_states if a.entity_id == actor_id)
    entry = (
        next((a for a in component.actors if a.actor_id == actor_id), None) if component else None
    )
    source = next((s for s in entry.sources if s.source_id == source_id), None) if entry else None
    # The canonical Cleric list includes Cure Wounds (SRD 5.2.1 p38).
    if entry is None or source is None or source.class_slug != "cleric" or source.ability != "wis":
        return "explicit reviewed Cleric casting source is required"
    levels = actor.classes or (
        {actor.class_slug: actor.character_level} if actor.class_slug else {}
    )
    if levels != {"cleric": 1} or actor.spellcasting_ability != source.ability:
        return "casting source contradicts class/ability facts"
    if spell.slug not in source.known_spells or spell.slug not in source.prepared_spells:
        return "spell is not known and prepared from the selected source"
    if not entry.verbal_available or actor.weapon_in_hands is not None:
        return "Verbal and free Somatic components are unavailable"
    return None


def spell_event(actor_id: str, spell: Spell, slot_level: int) -> SpellCast:
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


def spell_plan(request: RuleEvaluationRequest, loader: AssetLoader) -> HealingPlan | TurnEvaluation:
    snapshot, intent = request.state_snapshot, request.payload
    assert isinstance(snapshot, CombatSnapshot)
    assert isinstance(intent, CombatIntentPayload)
    if intent.spell_id != "cure-wounds":
        return _refuse(
            "unsupported", "spell.capability", "only reviewed immediate Cure Wounds is migrated"
        )
    basic = CombatIntentPayload(
        intent_type="cast_spell",
        source_id=intent.source_id,
        spell_id=intent.spell_id,
        target_id=intent.target_id,
        slot_level=intent.slot_level,
        spell_source_id=intent.spell_source_id,
    )
    if intent != basic:
        return _refuse("unsupported", "spell.inputs", "additional casting choices are not migrated")
    spell = loader.get_spell(intent.spell_id)
    if spell is None or spell != BundledAssetLoader().get_spell("cure-wounds"):
        return _refuse(
            "unsupported", "spell.binding", "spell definition differs from reviewed canonical data"
        )
    if (
        admission_failure(spell, spell.activities)
        or len(spell.activities) != 1
        or not isinstance(spell.activities[0], HealActivity)
    ):
        return _refuse(
            "unsupported", "spell.activity", "spell lacks reviewed complete healing payload"
        )
    if intent.slot_level != 1:
        return _refuse(
            "rejected", "no_slot", "this Cleric 1 profile requires an explicit level 1 slot"
        )
    if failure := source_failure(snapshot, request.actor_id, intent.spell_source_id, spell):
        return _refuse("rejected", "spell.source", failure)
    actor = next(a for a in snapshot.character_states if a.entity_id == request.actor_id)
    if (
        actor.action_taken_this_turn
        or actor.bonus_action_taken_this_turn
        or not actor.reaction_available
    ):
        return _refuse(
            "unsupported", "spell.turn_history", "first-slot casting window cannot be proved"
        )
    creatures = [combatant(a) for a in snapshot.character_states]
    topology = GridTopology(GridScene.model_validate(snapshot.scene_state.grid.model_dump()))
    state = snapshot.combat_state
    try:
        delivery = plan_delivery(
            spell.activities,
            SpellDeliverySpec(primary_target_id=intent.target_id),
            actor_id=request.actor_id,
            topology=topology,
            positions=state.actor_zone,
            creatures=creatures,
            enemy_ids=frozenset(
                state.encounter_ids if request.actor_id in state.party_ids else state.party_ids
            ),
            ally_ids=frozenset(
                state.party_ids if request.actor_id in state.party_ids else state.encounter_ids
            ),
            range_ft=spell_range_ft(spell),
            default_target_ids=(intent.target_id,) if intent.target_id else (),
            execution=True,
            cast_level=1,
            base_level=1,
        )
        if len(delivery.activities) != 1 or delivery.activities[0].target_ids != (
            intent.target_id,
        ):
            return _refuse("rejected", "target_invalid", "one in-range creature is required")
        assert snapshot.resource_state is not None
        payment = spell_payment(snapshot.resource_state, request.actor_id, 1)
    except DeliveryPlanningError as exc:
        return _refuse("rejected", exc.reason, str(exc))
    except ValueError:
        return _refuse("rejected", "no_slot", "no owned slot remains")
    assert intent.target_id is not None
    return HealingPlan(spell.activities[0], intent.target_id, "wis", (payment,), spell, 1)


def feature_plan(
    request: RuleEvaluationRequest, loader: AssetLoader
) -> HealingPlan | TurnEvaluation:
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
        or feature != BundledAssetLoader().get_feature("second-wind")
        or "second-wind" not in (actor.granted_features or ())
    ):
        return _refuse("rejected", "feature.source", "actual reviewed Fighter grant is required")
    activity = feature.activities[0]
    if not isinstance(activity, HealActivity) or intent.activity_id != activity.id:
        return _refuse(
            "rejected", "feature.activity", "explicit canonical healing activity is required"
        )
    stub = HealingPlan(activity, request.actor_id, None, (), None, None)
    context = healing_context(
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
    return HealingPlan(activity, request.actor_id, None, payments, None, None)


def evaluate_healing(request: RuleEvaluationRequest, loader: AssetLoader) -> TurnEvaluation:
    snapshot, intent = request.state_snapshot, request.payload
    assert isinstance(snapshot, CombatSnapshot)
    assert isinstance(intent, CombatIntentPayload)
    state = snapshot.combat_state
    if state.ended or request.actor_id not in state.initiative_ids:
        return _refuse("rejected", "combat_ended", "live combat actor is required")
    if request.actor_id != state.initiative_ids[state.current_turn_index]:
        return _refuse("rejected", "not_actor_turn", "actor does not own this turn")
    current = next(a for a in snapshot.character_states if a.entity_id == request.actor_id)
    if not current.is_alive or current.hp_current <= 0 or request.actor_id in state.dead_ids:
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
    if failure := healing_dependencies(snapshot, loader, intent.target_id):
        return _refuse("unsupported", "healing.dependencies", failure)
    actor = combatant(current)
    bonus = intent.intent_type == "use_feature"
    if action_economy_gate_failure(actor, intent, is_bonus_action=bonus, is_reaction_cast=False):
        return _refuse("rejected", "no_action_economy", "required action budget is unavailable")
    with scoped_lib_loader(loader):
        plan = feature_plan(request, loader) if bonus else spell_plan(request, loader)
        if not isinstance(plan, HealingPlan):
            return plan
        rng = request.rng_context.state.restore()
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
        if plan.spell is not None:
            computation.emit(spell_event(request.actor_id, plan.spell, 1))
        resolve_activity(
            plan.activity, healing_context(computation, plan, loader, request.actor_id, rng)
        )
        computation.finish_turn(request.actor_id, allow_movement=bonus)
        computation.actors[request.actor_id] = record_budget_changes(
            actor, computation.actors[request.actor_id]
        )
        after = payment_post_state(computation.result(), plan.payment)
        ledgers = (slot_ledger(snapshot, plan.payment[0]),) if plan.spell else ()
        if ledgers:
            computation.state = computation.state.model_copy(
                update={
                    "expended_resources": {
                        **state.expended_resources,
                        request.actor_id: ledgers[0].value,
                    }
                }
            )
            after = after.model_copy(
                update={
                    "combat_state": computation.state.model_copy(
                        update={"event_log": after.combat_state.event_log}
                    )
                }
            )
        return dict(
            status="accepted",
            state_delta=paid_delta(snapshot, after, plan.payment, ledgers),
            proposed_events=tuple(computation.events),
            rng_transition=RNGTransition.between(request.rng_context, rng),
            choice=None,
            error=None,
        )
