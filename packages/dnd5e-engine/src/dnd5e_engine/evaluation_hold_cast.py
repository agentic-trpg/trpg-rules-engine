"""One authorized Wizard Hold Person intent; private atomic graph initialization."""

from dataclasses import replace
from typing import Literal, cast

from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.common import SaveActivity

from dnd5e_engine.action_economy_rules import action_economy_gate_failure
from dnd5e_engine.activities.actor_stats import ABILITY_CODES, save_modifier
from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.effects import applicable_effect_statuses
from dnd5e_engine.activities.resolver import resolve_activity
from dnd5e_engine.concentration_rules import concentration_anchor, concentration_max_rounds
from dnd5e_engine.effect_lifecycle import (
    OngoingEffectLifecycle,
    conditions_after_effect_application,
)
from dnd5e_engine.evaluation_actor import combatant
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, RuleEvaluationRequest
from dnd5e_engine.evaluation_delta import ResourceUpdate, SlotSpendLedgerUpdate, StateDelta
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_hold_delta import HoldPersonCreatedEntry, HoldPersonGraphCreate
from dnd5e_engine.evaluation_hold_turn import HoldTurnComputation
from dnd5e_engine.evaluation_paid_projection import paid_delta, payment_post_state, slot_ledger
from dnd5e_engine.evaluation_payments import spell_payment
from dnd5e_engine.evaluation_projection import EvaluationInvariantError
from dnd5e_engine.evaluation_rng import RNGTransition
from dnd5e_engine.evaluation_ruleset import get_default_ruleset
from dnd5e_engine.evaluation_spell_dependencies import invocation_dependencies
from dnd5e_engine.evaluation_state import CombatSnapshot, ConditionLink, LifecycleRecord
from dnd5e_engine.evaluation_turn import TurnEvaluation, _refuse
from dnd5e_engine.events import CombatEvent, EffectApplied, IntentSubmitted
from dnd5e_engine.lib_loader import scoped_lib_loader
from dnd5e_engine.rules.conditions import conditions_block_actions
from dnd5e_engine.spatial import GridTopology
from dnd5e_engine.specs import GridScene
from dnd5e_engine.spell_delivery import DeliveryPlanningError, SpellDeliverySpec, plan_delivery
from dnd5e_engine.spellcasting import resolve_target_count, spell_component_failure, spell_range_ft
from dnd5e_engine.turn_rules import ordinary_action_payment, record_budget_changes


class HoldCastComputation(HoldTurnComputation):
    """Compute the admitted Hold Person creation using isolated concentration machinery."""

    def emit(self, event: CombatEvent) -> None:
        """Append mechanical event evidence and fold its consequences into this candidate
        computation.
        """
        if isinstance(event, EffectApplied):
            effect = event.effect
            identity = (effect.target_id, effect.id, effect.origin)
            if any((e.target_id, e.id, e.origin) == identity for e in self.effects):
                raise EvaluationInvariantError("Hold Person creation requires expected absence")
            actor = self.actors[effect.target_id]
            statuses = applicable_effect_statuses(actor, effect.statuses)
            conditions, _ = conditions_after_effect_application(
                actor.conditions, effect.id, statuses
            )
            self.actors[actor.entity_id] = actor.model_copy(update={"conditions": conditions})
            self.effects.append(effect_state(effect))
            clocks = self.state.effect_lifecycles
            if effect.lifecycle is not None:
                clocks += (
                    LifecycleRecord(
                        identity=identity,
                        state=OngoingEffectLifecycle.from_application(
                            identity,
                            effect.lifecycle,
                            self.state.turn_serial,
                            self.state.round_number,
                        ),
                    ),
                )
            self.state = self.state.model_copy(
                update={
                    "conditions_by_effect": (
                        *self.state.conditions_by_effect,
                        ConditionLink(identity=identity, statuses=statuses),
                    ),
                    "effect_lifecycles": clocks,
                }
            )
        super().emit(event)


def hold_cast_source_failure(
    snapshot: CombatSnapshot, intent: CombatIntentPayload, actor_id: str, loader: AssetLoader
) -> str | None:
    """Check the bound source required by the admitted Hold Person cast."""
    spell = loader.get_spell("hold-person")
    assert spell is not None
    actor = next(a for a in snapshot.character_states if a.entity_id == actor_id)
    entry = (
        next((e for e in snapshot.casting_state.actors if e.actor_id == actor_id), None)
        if snapshot.casting_state
        else None
    )
    source = (
        next((s for s in entry.sources if s.source_id == intent.spell_source_id), None)
        if entry
        else None
    )
    if (
        actor.classes != {"wizard": 5}
        or actor.subclass_slug != "evoker"
        or actor.spellcasting_ability != "int"
        or source is None
        or source.class_slug != "wizard"
        or source.ability != "int"
        or spell.slug not in source.known_spells
        or spell.slug not in source.prepared_spells
    ):
        return "complete known/prepared Wizard 5 Evoker source is required"
    assert entry is not None
    material = next(
        (
            m
            for m in entry.materials
            if m.source_id == source.source_id and m.spell_slug == spell.slug
        ),
        None,
    )
    owned = next(
        (
            e
            for e in snapshot.inventory_state.entries
            if material and e.instance_id == material.inventory_instance_id
        ),
        None,
    )
    available = bool(
        material
        and material.available
        and owned
        and owned.owner_id == actor_id
        and owned.item_slug == "component-pouch"
        and owned.quantity == 1
        and owned.accessible
    )
    return spell_component_failure(
        spell,
        verbal_available=entry.verbal_available,
        somatic_hand_available=actor.weapon_in_hands is None and not actor.other_hand_occupied,
        material_available=available,
    )


def hold_create_delta(
    before: CombatSnapshot,
    after: CombatSnapshot,
    intent: CombatIntentPayload,
    payments: tuple[ResourceUpdate, ...],
    ledgers: tuple[SlotSpendLedgerUpdate, ...],
    dc: int,
) -> StateDelta:
    """Project the new Hold Person graph into guarded Delta operations."""
    assert intent.source_id is not None
    assert intent.spell_source_id is not None
    assert intent.target_id is not None
    if (
        before.effect_states
        or before.combat_state.effect_lifecycles
        or before.combat_state.conditions_by_effect
        or before.combat_state.concentration_chain
        or before.combat_state.concentration_rounds_remaining
    ):
        raise EvaluationInvariantError(
            "Hold Person graph creation requires complete expected absence"
        )
    source = next(a for a in after.character_states if a.entity_id == intent.source_id)
    entries = []
    for effect in after.effect_states:
        identity = (effect.target_id, effect.id, effect.origin)
        actor = next(a for a in after.character_states if a.entity_id == effect.target_id)
        entries.append(
            HoldPersonCreatedEntry(
                effect=effect,
                clock=next(
                    (c for c in after.combat_state.effect_lifecycles if c.identity == identity),
                    None,
                ),
                lineage=next(
                    c for c in after.combat_state.conditions_by_effect if c.identity == identity
                ),
                expected_effect=None,
                expected_clock=None,
                expected_lineage=None,
                expected_conditions=(),
                condition=actor.conditions[0] if effect.statuses else None,
            )
        )
    op = HoldPersonGraphCreate(
        kind="combat.hold_person_graph_create",
        combat_id=before.combat_state.combat_id,
        source_id=intent.source_id,
        spell_source_id=intent.spell_source_id,
        spell_slug="hold-person",
        actor_ids=before.combat_state.initiative_ids,
        selected_target_ids=intent.target_ids or (intent.target_id,),
        slot_level=cast(Literal[2, 3], intent.slot_level),
        save_dc=dc,
        applied_turn_serial=before.combat_state.turn_serial,
        applied_round=before.combat_state.round_number,
        expected_effects=(),
        expected_clocks=(),
        expected_lineages=(),
        expected_concentration_id=None,
        expected_chain=(),
        expected_rounds=None,
        entries=tuple(entries),
        concentration_id=cast(
            Literal["effect:paralyzed", "effect:hold-person"], source.concentration_effect_id
        ),
        chain=tuple(after.combat_state.concentration_chain[source.entity_id]),
        rounds=cast(
            Literal[9], after.combat_state.concentration_rounds_remaining[source.entity_id]
        ),
    )
    expected_conditions = {
        e.effect.target_id: [e.condition] if e.condition else [] for e in op.entries
    }
    for old, new_actor in zip(before.character_states, after.character_states, strict=True):
        if (
            old.conditions
            or old.concentration_effect_id is not None
            or new_actor.conditions != expected_conditions.get(old.entity_id, [])
            or new_actor.concentration_effect_id
            != (op.concentration_id if old.entity_id == op.source_id else None)
        ):
            raise EvaluationInvariantError("unrepresented Hold Person conditions or concentration")
    normalized = after.model_copy(
        update={
            "effect_states": before.effect_states,
            "character_states": tuple(
                a.model_copy(
                    update={
                        "conditions": old.conditions,
                        "concentration_effect_id": old.concentration_effect_id,
                    }
                )
                for old, a in zip(before.character_states, after.character_states, strict=True)
            ),
            "combat_state": after.combat_state.model_copy(
                update={
                    k: getattr(before.combat_state, k)
                    for k in (
                        "effect_lifecycles",
                        "conditions_by_effect",
                        "concentration_chain",
                        "concentration_rounds_remaining",
                    )
                }
            ),
        }
    )
    base = paid_delta(before, normalized, payments, ledgers)
    return base.model_copy(update={"operations": (*base.operations, op)})


def evaluate_hold_cast(request: RuleEvaluationRequest, loader: AssetLoader) -> TurnEvaluation:
    """Evaluate the admitted Hold Person cast and return an uncommitted graph candidate."""
    from dnd5e_engine.evaluation_spells import spell_event

    snapshot, intent = request.state_snapshot, request.payload
    assert isinstance(snapshot, CombatSnapshot)
    assert isinstance(intent, CombatIntentPayload)
    state = snapshot.combat_state
    actors = {a.entity_id: a for a in snapshot.character_states}
    if state.ended or request.actor_id not in actors:
        return _refuse("rejected", "actor_invalid", "living combat source is required")
    if request.actor_id != state.initiative_ids[state.current_turn_index]:
        return _refuse("rejected", "not_actor_turn", "actor does not own this turn")
    actor = combatant(actors[request.actor_id])
    if (
        not actor.is_alive
        or actor.hp_current <= 0
        or actor.entity_id in state.dead_ids
        or conditions_block_actions([c.condition for c in actor.conditions])
    ):
        return _refuse("rejected", "actor_incapacitated", "caster must be alive and conscious")
    basic = CombatIntentPayload(
        intent_type="cast_spell",
        source_id=intent.source_id,
        spell_id="hold-person",
        target_id=intent.target_id,
        target_ids=intent.target_ids,
        slot_level=intent.slot_level,
        spell_source_id=intent.spell_source_id,
    )
    if intent != basic:
        return _refuse("unsupported", "spell.inputs", "only named Hold Person targets are reviewed")
    targets = intent.target_ids or ((intent.target_id,) if intent.target_id else ())
    if (
        not targets
        or intent.target_id != targets[0]
        or actor.entity_id in targets
        or any(
            t not in actors
            or actors[t].creature_type != "humanoid"
            or not actors[t].is_alive
            or actors[t].hp_current <= 0
            for t in targets
        )
    ):
        return _refuse(
            "unsupported", "spell.targets", "explicit living Humanoid targets are required"
        )
    if intent.slot_level not in (2, 3):
        return _refuse("rejected", "no_slot", "reviewed level 2 or 3 slot is required")
    spell = loader.get_spell("hold-person")
    canonical = get_default_ruleset()
    if (
        spell is None
        or spell != canonical.get_spell("hold-person")
        or loader.get_condition("paralyzed") != canonical.get_condition("paralyzed")
    ):
        return _refuse("unsupported", "spell.binding", "canonical spell and condition are required")
    if state.pending_reactions or snapshot.effect_states or state.concentration_chain:
        return _refuse(
            "unsupported", "spell.graph", "initial cast requires no existing effect/reaction graph"
        )
    if failure := invocation_dependencies(snapshot, loader, targets[0], damaging=True):
        return _refuse("unsupported", "spell.dependencies", failure)
    if failure := hold_cast_source_failure(snapshot, intent, actor.entity_id, loader):
        return _refuse("rejected", "spell.source", failure)
    if action_economy_gate_failure(actor, intent, is_bonus_action=False, is_reaction_cast=False):
        return _refuse("rejected", "no_action_economy", "ordinary Action is unavailable")
    if (
        actor.action_taken_this_turn
        or actor.bonus_action_taken_this_turn
        or not actor.reaction_available
    ):
        return _refuse("unsupported", "spell.turn_history", "first slot casting window is required")
    topology = GridTopology(GridScene.model_validate(snapshot.scene_state.grid.model_dump()))
    try:
        count = resolve_target_count(
            spell.activities[0].target.affects.count, cast_level=intent.slot_level
        )
        if count is None or len(targets) > count or len(set(targets)) != len(targets):
            return _refuse(
                "rejected", "target_invalid", "named targets exceed the shared target count"
            )
        delivery = plan_delivery(
            spell.activities,
            SpellDeliverySpec(
                primary_target_id=intent.target_id, selected_target_ids=intent.target_ids
            ),
            actor_id=actor.entity_id,
            topology=topology,
            positions=state.actor_zone,
            creatures=[combatant(a) for a in snapshot.character_states],
            enemy_ids=frozenset(
                state.encounter_ids if actor.entity_id in state.party_ids else state.party_ids
            ),
            ally_ids=frozenset(
                state.party_ids if actor.entity_id in state.party_ids else state.encounter_ids
            ),
            range_ft=spell_range_ft(spell),
            default_target_ids=targets,
            execution=False,
            cast_level=intent.slot_level,
            base_level=spell.level,
        )
        if len(delivery.activities) != 1 or delivery.activities[0].target_ids != targets:
            return _refuse(
                "rejected", "target_invalid", "delivery must preserve the complete named targets"
            )
        assert snapshot.resource_state is not None
        payments = (spell_payment(snapshot.resource_state, actor.entity_id, intent.slot_level),)
    except DeliveryPlanningError as exc:
        return _refuse("rejected", exc.reason, str(exc))
    except ValueError as exc:
        return _refuse("rejected", "no_slot", str(exc))
    activity = spell.activities[0]
    assert isinstance(activity, SaveActivity)
    with scoped_lib_loader(loader):
        rng = request.rng_context.state.restore()
        computation = HoldCastComputation(snapshot, rng)
        computation.emit(
            IntentSubmitted(
                actor_id=actor.entity_id,
                intent_type="cast_spell",
                spell_id=spell.slug,
                target_id=intent.target_id,
            )
        )
        computation.actors[actor.entity_id] = actor.model_copy(
            update=ordinary_action_payment(actor)
        )
        computation.emit(spell_event(actor.entity_id, spell, intent.slot_level))
        ctx = build_activity_context(
            computation.actors[actor.entity_id],
            [computation.actors[t] for t in targets],
            rng=rng,
            event_emitter=computation.emit,
            slot_level=intent.slot_level,
            base_spell_level=spell.level,
            spellcasting_ability="int",
            concentration=True,
            source_passive_effects=spell.passive_effects,
            spell_book={},
            passive_damage_modifiers={},
            save_modifiers={
                t: {
                    "saves": {
                        ab: save_modifier(computation.actors[t], ab).total for ab in ABILITY_CODES
                    }
                }
                for t in targets
            },
        )
        ctx = replace(
            ctx,
            lifecycle_source_kind="spell",
            lifecycle_source_slug=spell.slug,
            save_is_magical=True,
        )
        resolve_activity(activity, ctx)
        if not computation.effects:
            computation.emit(EffectApplied(effect=concentration_anchor(spell, actor.entity_id)))
        chain = [(e.target_id, e.id, e.origin) for e in computation.effects]
        computation.actors[actor.entity_id] = computation.actors[actor.entity_id].model_copy(
            update={"concentration_effect_id": chain[-1][1]}
        )
        computation.state = computation.state.model_copy(
            update={
                "concentration_chain": {actor.entity_id: chain},
                "concentration_rounds_remaining": {
                    actor.entity_id: concentration_max_rounds(spell)
                },
            }
        )
        computation.finish_turn(actor.entity_id)
        computation.actors[actor.entity_id] = record_budget_changes(
            actor, computation.actors[actor.entity_id]
        )
        ledgers = (slot_ledger(snapshot, payments[0]),)
        computation.state = computation.state.model_copy(
            update={
                "expended_resources": {
                    **state.expended_resources,
                    actor.entity_id: ledgers[0].value,
                }
            }
        )
        computed = computation.result()
        after = payment_post_state(computed.candidate_state, payments)
        # The shared SaveActivity captured this DC; no second save/DC formula.
        dc = next(e.dc for e in computation.events if e.type == "save_rolled")
        sourced = intent.model_copy(update={"source_id": actor.entity_id})
        return dict(
            status="accepted",
            state_delta=hold_create_delta(snapshot, after, sourced, payments, ledgers, dc),
            proposed_events=computed.proposed_events,
            rng_transition=RNGTransition.between(request.rng_context, rng),
            choice=None,
            error=None,
        )
