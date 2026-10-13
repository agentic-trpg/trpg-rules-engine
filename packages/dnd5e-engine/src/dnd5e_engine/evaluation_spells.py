"""Bounded real instant spells and Second Wind, without a Stateful executor."""

import random
from dataclasses import dataclass, replace

from dnd5e_srd_data import BundledAssetLoader
from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.common import HealActivity, SaveActivity
from dnd5e_srd_data.schema.spell import Spell

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
from dnd5e_engine.evaluation_spell_dependencies import invocation_dependencies
from dnd5e_engine.evaluation_state import CombatSnapshot
from dnd5e_engine.evaluation_turn import TurnEvaluation, _refuse
from dnd5e_engine.events import CastFailed, IntentSubmitted, SpellCast
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


@dataclass(frozen=True)
class InvocationPlan:
    activity: HealActivity | SaveActivity
    target_id: str
    ability: str | None
    payment: tuple[ResourceUpdate, ...]
    spell: Spell | None
    slot_level: int | None


def invocation_context(
    computation: CombatComputation,
    plan: InvocationPlan,
    loader: AssetLoader,
    actor_id: str,
    rng: random.Random,
) -> ActivityResolutionContext:
    actor = computation.actors[actor_id]
    levels = actor.classes or (
        {actor.class_slug: actor.character_level} if actor.class_slug else {}
    )
    target = computation.actors[plan.target_id]
    save_projection = project_passive_save_modifiers(active_condition_names(target.conditions))
    if attack_rules.dodge_benefit_active(target, effective_speed=computation.speed(target)):
        save_projection["passive_save_adv"].append("DEX")
    context = build_activity_context(
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
        passive_damage_modifiers=attack_rules.damage_modifiers(computation.actors.values()),
        save_modifiers={
            target.entity_id: {
                "saves": {ab: save_modifier(target, ab).total for ab in ABILITY_CODES},
                **save_projection,
            }
        },
        target_dodging={
            target.entity_id: attack_rules.dodge_benefit_active(
                target, effective_speed=computation.speed(target)
            )
        },
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
        save_is_magical=plan.spell is not None,
        activity_source_id=f"spell:{plan.spell.slug}:{plan.activity.id}" if plan.spell else None,
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
    # The reviewed canonical Cleric list includes Cure Wounds and Sacred Flame.
    if entry is None or source is None or source.class_slug != "cleric" or source.ability != "wis":
        return "explicit reviewed Cleric casting source is required"
    levels = actor.classes or (
        {actor.class_slug: actor.character_level} if actor.class_slug else {}
    )
    if levels != {"cleric": 1} or actor.spellcasting_ability != source.ability:
        return "casting source contradicts class/ability facts"
    if spell.slug not in source.known_spells or (
        spell.level > 0 and spell.slug not in source.prepared_spells
    ):
        return "spell is not known and prepared from the selected source"
    return spell_component_failure(
        spell,
        verbal_available=entry.verbal_available,
        somatic_hand_available=actor.weapon_grip != "two_handed"
        and (actor.weapon_in_hands is None or not actor.other_hand_occupied),
        # R30's two canonical V/S spells need no materials. No material or focus
        # contract is invented for other spells.
        material_available=False,
    )


def spell_event(actor_id: str, spell: Spell, slot_level: int | None) -> SpellCast:
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
    request: RuleEvaluationRequest, loader: AssetLoader
) -> InvocationPlan | TurnEvaluation:
    snapshot, intent = request.state_snapshot, request.payload
    assert isinstance(snapshot, CombatSnapshot)
    assert isinstance(intent, CombatIntentPayload)
    if intent.spell_id not in {"cure-wounds", "sacred-flame"}:
        return _refuse(
            "unsupported",
            "spell.capability",
            "only reviewed immediate Cure Wounds and Sacred Flame are migrated",
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
    if spell is None or spell != BundledAssetLoader().get_spell(intent.spell_id):
        return _refuse(
            "unsupported", "spell.binding", "spell definition differs from reviewed canonical data"
        )
    if (
        admission_failure(spell, spell.activities)
        or len(spell.activities) != 1
        or not isinstance(spell.activities[0], (HealActivity, SaveActivity))
    ):
        return _refuse(
            "unsupported", "spell.activity", "spell lacks reviewed complete instant payload"
        )
    if intent.slot_level not in ((None, 0) if spell.level == 0 else (1,)):
        return _refuse(
            "rejected", "no_slot", "Cantrip requires no slot; Cure Wounds requires explicit level 1"
        )
    if failure := source_failure(snapshot, request.actor_id, intent.spell_source_id, spell):
        return _refuse("rejected", "spell.source", failure)
    actor = next(a for a in snapshot.character_states if a.entity_id == request.actor_id)
    if spell.level > 0 and (
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
            cast_level=spell.level,
            base_level=spell.level,
        )
        if len(delivery.activities) != 1 or delivery.activities[0].target_ids != (
            intent.target_id,
        ):
            return _refuse("rejected", "target_invalid", "one in-range creature is required")
        assert snapshot.resource_state is not None
        payments = (
            (spell_payment(snapshot.resource_state, request.actor_id, 1),) if spell.level else ()
        )
    except DeliveryPlanningError as exc:
        return _refuse("rejected", exc.reason, str(exc))
    except ValueError:
        return _refuse("rejected", "no_slot", "no owned slot remains")
    assert intent.target_id is not None
    return InvocationPlan(
        spell.activities[0], intent.target_id, "wis", payments, spell, spell.level
    )


def feature_plan(
    request: RuleEvaluationRequest, loader: AssetLoader
) -> InvocationPlan | TurnEvaluation:
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
    stub = InvocationPlan(activity, request.actor_id, None, (), None, None)
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
    return InvocationPlan(activity, request.actor_id, None, payments, None, None)


def evaluate_invocation(request: RuleEvaluationRequest, loader: AssetLoader) -> TurnEvaluation:
    snapshot, intent = request.state_snapshot, request.payload
    assert isinstance(snapshot, CombatSnapshot)
    assert isinstance(intent, CombatIntentPayload)
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
    if failure := invocation_dependencies(
        snapshot, loader, intent.target_id, damaging=intent.spell_id == "sacred-flame"
    ):
        return _refuse("unsupported", "healing.dependencies", failure)
    actor = combatant(current)
    bonus = intent.intent_type == "use_feature"
    if action_economy_gate_failure(actor, intent, is_bonus_action=bonus, is_reaction_cast=False):
        return _refuse("rejected", "no_action_economy", "required action budget is unavailable")
    with scoped_lib_loader(loader):
        plan = feature_plan(request, loader) if bonus else spell_plan(request, loader)
        if not isinstance(plan, InvocationPlan):
            return plan
        reaction = (
            prepare_counterspell(snapshot, request.actor_id, plan.spell, loader)
            if plan.spell
            else None
        )
        rng = request.rng_context.state.restore()
        if snapshot.effect_states:
            from dnd5e_engine.evaluation_concentration import ConcentrationComputation

            computation: CombatComputation = ConcentrationComputation(snapshot, rng)
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
        return dict(
            status="accepted",
            state_delta=reaction_paid_delta(snapshot, after, payments, ledgers),
            proposed_events=computed.proposed_events,
            rng_transition=RNGTransition.between(request.rng_context, rng),
            choice=None,
            error=None,
        )
