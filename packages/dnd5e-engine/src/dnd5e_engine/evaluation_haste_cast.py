"""Stateless Haste cast over an explicitly willing visible combat target."""

from dataclasses import replace

from dnd5e_srd_data.loader import AssetLoader

from dnd5e_engine.action_economy_rules import action_economy_gate_failure
from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.resolver import resolve_activity
from dnd5e_engine.evaluation_actor import combatant
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, RuleEvaluationRequest
from dnd5e_engine.evaluation_haste import HasteComputation, _without_haste
from dnd5e_engine.evaluation_haste_delta import (
    HASTE_EFFECT_ID,
    HasteGraphCreate,
    haste_concentration_rounds,
)
from dnd5e_engine.evaluation_hold_cast import hold_cast_source_failure
from dnd5e_engine.evaluation_paid_projection import paid_delta, payment_post_state, slot_ledger
from dnd5e_engine.evaluation_payments import spell_payment
from dnd5e_engine.evaluation_rng import RNGTransition
from dnd5e_engine.evaluation_ruleset import get_default_ruleset
from dnd5e_engine.evaluation_spell_dependencies import invocation_dependencies
from dnd5e_engine.evaluation_state import CombatSnapshot
from dnd5e_engine.evaluation_turn import TurnEvaluation, _refuse
from dnd5e_engine.events import IntentSubmitted
from dnd5e_engine.lib_loader import scoped_lib_loader
from dnd5e_engine.live_spell_delivery import delivery_activities
from dnd5e_engine.spatial import GridTopology
from dnd5e_engine.specs import GridScene
from dnd5e_engine.spell_delivery import DeliveryPlanningError, SpellDeliverySpec, plan_delivery
from dnd5e_engine.spellcasting import spell_range_ft
from dnd5e_engine.turn_rules import ordinary_action_payment, record_budget_changes


def evaluate_haste_cast(request: RuleEvaluationRequest, loader: AssetLoader) -> TurnEvaluation:
    """Propose payment and a complete reviewed Haste graph through the shared resolver."""
    from dnd5e_engine.evaluation_spells import spell_event

    snapshot, intent = request.state_snapshot, request.payload
    assert isinstance(snapshot, CombatSnapshot)
    assert isinstance(intent, CombatIntentPayload)
    state = snapshot.combat_state
    source, target = request.actor_id, intent.target_id
    if state.ended or source not in state.initiative_ids:
        return _refuse("rejected", "actor_invalid", "living combat source is required")
    if source != state.initiative_ids[state.current_turn_index]:
        return _refuse("rejected", "not_actor_turn", "actor does not own this turn")
    actors = {a.entity_id: a for a in snapshot.character_states}
    caster = combatant(actors[source])
    if target not in actors or target is None:
        return _refuse("rejected", "target_invalid", "Haste needs one explicit target")
    if intent.willing_target_ids != (target,):
        return _refuse(
            "rejected", "target_invalid", "the selected target must be explicitly willing"
        )
    if intent.slot_level != 3:
        return _refuse("rejected", "no_slot", "reviewed level 3 slot is required")
    if (
        request.schema_version != "engine-evaluation/30"
        or not intent.spell_source_id
        or intent.target_ids not in (None, (target,))
        or intent
        != CombatIntentPayload(
            intent_type="cast_spell",
            source_id=intent.source_id,
            spell_id="haste",
            target_id=target,
            target_ids=intent.target_ids,
            willing_target_ids=(target,),
            slot_level=3,
            spell_source_id=intent.spell_source_id,
        )
    ):
        return _refuse("unsupported", "spell.inputs", "Haste requires the /30 named combat cast")
    if any(
        not actor.is_alive or actor.hp_current <= 0 or actor.entity_id in state.dead_ids
        for actor in actors.values()
    ):
        return _refuse("unsupported", "spell.graph", "Haste cast requires living combat actors")
    spell = loader.get_spell("haste")
    canonical = get_default_ruleset()
    if (
        spell is None
        or spell != canonical.get_spell("haste")
        or loader.get_condition("incapacitated") != canonical.get_condition("incapacitated")
    ):
        return _refuse(
            "unsupported", "spell.binding", "canonical Haste and Incapacitated are required"
        )
    if (
        state.pending_reactions
        or snapshot.effect_states
        or state.concentration_chain
        or state.concentration_rounds_remaining
        or state.effect_lifecycles
        or state.conditions_by_effect
    ):
        return _refuse("unsupported", "spell.graph", "initial Haste requires an empty effect graph")
    if failure := invocation_dependencies(snapshot, loader, target, damaging=True):
        return _refuse("unsupported", "spell.dependencies", failure)
    if failure := hold_cast_source_failure(snapshot, intent, source, loader):
        return _refuse("rejected", "spell.source", failure)
    if action_economy_gate_failure(caster, intent, is_bonus_action=False, is_reaction_cast=False):
        return _refuse("rejected", "no_action_economy", "ordinary Action is unavailable")
    if (
        caster.action_taken_this_turn
        or caster.bonus_action_taken_this_turn
        or not caster.reaction_available
    ):
        return _refuse("unsupported", "spell.turn_history", "first slot casting window is required")

    topology = GridTopology(GridScene.model_validate(snapshot.scene_state.grid.model_dump()))
    spec = SpellDeliverySpec(primary_target_id=target, willing_target_ids=(target,))
    try:
        activities = delivery_activities(spell, spec)
        allies = state.party_ids if source in state.party_ids else state.encounter_ids
        enemies = state.encounter_ids if source in state.party_ids else state.party_ids
        delivery = plan_delivery(
            activities,
            spec,
            actor_id=source,
            topology=topology,
            positions=state.actor_zone,
            creatures=[combatant(a) for a in actors.values()],
            ally_ids=frozenset(allies),
            enemy_ids=frozenset(enemies),
            range_ft=spell_range_ft(spell),
            default_target_ids=(target,),
            execution=False,
            cast_level=3,
            base_level=spell.level,
        )
        if len(delivery.activities) != 1 or delivery.activities[0].target_ids != (target,):
            return _refuse("rejected", "target_invalid", "Haste delivery must preserve the target")
        if not topology.has_line_of_sight(state.actor_zone[source], state.actor_zone[target]):
            return _refuse("rejected", "target_invalid", "source must see the willing target")
        assert snapshot.resource_state is not None
        payment = spell_payment(snapshot.resource_state, source, 3)
    except DeliveryPlanningError as exc:
        return _refuse("rejected", exc.reason, str(exc))
    except ValueError as exc:
        return _refuse("rejected", "no_slot", str(exc))

    with scoped_lib_loader(loader):
        rng = request.rng_context.state.restore()
        computation = HasteComputation(snapshot, rng)
        computation.emit(
            IntentSubmitted(
                actor_id=source, intent_type="cast_spell", spell_id=spell.slug, target_id=target
            )
        )
        computation.actors[source] = caster.model_copy(update=ordinary_action_payment(caster))
        computation.emit(spell_event(source, spell, 3))
        ctx = build_activity_context(
            computation.actors[source],
            [computation.actors[target]],
            rng=rng,
            event_emitter=computation.emit,
            slot_level=3,
            base_spell_level=spell.level,
            spellcasting_ability="int",
            concentration=True,
            source_passive_effects=spell.passive_effects,
            spell_book={},
            passive_damage_modifiers={},
            save_modifiers={},
        )
        resolve_activity(
            activities[0],
            replace(ctx, lifecycle_source_kind="spell", lifecycle_source_slug=spell.slug),
        )
        [effect] = computation.effects
        identity = (effect.target_id, effect.id, effect.origin)
        computation.actors[source] = computation.actors[source].model_copy(
            update={"concentration_effect_id": HASTE_EFFECT_ID}
        )
        computation.state = computation.state.model_copy(
            update={
                "concentration_chain": {source: [identity]},
                "concentration_rounds_remaining": {source: haste_concentration_rounds()},
            }
        )
        computation.refresh_movement(target)
        computation.actors[source] = record_budget_changes(caster, computation.actors[source])
        ledger = slot_ledger(snapshot, payment)
        computation.state = computation.state.model_copy(
            update={"expended_resources": {**state.expended_resources, source: ledger.value}}
        )
        computed = computation.result()
        after = payment_post_state(computed.candidate_state, (payment,))
        graph = HasteGraphCreate(
            kind="combat.haste_graph_create",
            combat_id=state.combat_id,
            source_id=source,
            spell_source_id=intent.spell_source_id,
            target_id=target,
            slot_level=3,
            actor_ids=state.initiative_ids,
            applied_turn_serial=state.turn_serial,
            applied_round=state.round_number,
            expected_effects=(),
            expected_clocks=(),
            expected_lineages=(),
            expected_concentration_id=None,
            expected_chain=(),
            expected_rounds=None,
            effect=effect,
            lifecycle=after.combat_state.effect_lifecycles[0],
            lineage=after.combat_state.conditions_by_effect[0],
            concentration_id=HASTE_EFFECT_ID,
            chain=(identity,),
            rounds=after.combat_state.concentration_rounds_remaining[source],
        )
        delta = paid_delta(snapshot, _without_haste(after), (payment,), (ledger,))
        return dict(
            status="accepted",
            state_delta=delta.model_copy(update={"operations": (*delta.operations, graph)}),
            proposed_events=computed.proposed_events,
            rng_transition=RNGTransition.between(request.rng_context, rng),
            choice=None,
            error=None,
        )
