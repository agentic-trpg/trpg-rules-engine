"""One SM-authorized Shield declaration and its persistent scalar AC lifecycle."""

import random
from collections import Counter

from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.common import ReactionTriggerKind, UtilityActivity

from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.context import AttackHitContext
from dnd5e_engine.activities.resolver import resolve_activity
from dnd5e_engine.evaluation_class_dependencies import operation_dependencies
from dnd5e_engine.evaluation_computation import CombatComputation
from dnd5e_engine.evaluation_delta import ResourceUpdate, StateDelta
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_equipment_dependencies import wizard_pouch_dependencies
from dnd5e_engine.evaluation_paid_projection import payment_post_state, slot_ledger
from dnd5e_engine.evaluation_payments import spell_payment
from dnd5e_engine.evaluation_projection import EvaluationInvariantError
from dnd5e_engine.evaluation_reaction_state import PendingReactionState
from dnd5e_engine.evaluation_reactions import reaction_paid_delta
from dnd5e_engine.evaluation_ruleset import get_default_ruleset
from dnd5e_engine.evaluation_shield_delta import (
    ShieldGraphValue,
    ShieldStateTransition,
    response_state,
)
from dnd5e_engine.evaluation_state import CombatSnapshot, ConditionLink
from dnd5e_engine.events import (
    CombatEvent,
    Death,
    EffectApplied,
    EffectExpired,
    ReactionTriggered,
    TurnPhase,
)
from dnd5e_engine.reaction_effect_rules import (
    effect_ac_changes,
    provisional_attack_evidence,
    retained_reaction_response,
    scalar_ac_change_delta,
)
from dnd5e_engine.reactions import (
    ReactionOpportunity,
    matching_conditions,
    reaction_support,
    reaction_target_id,
)
from dnd5e_engine.rules.conditions import conditions_block_actions
from dnd5e_engine.spell_execution import admission_failure
from dnd5e_engine.spellcasting import spell_component_failure


def has_shield(snapshot: CombatSnapshot) -> bool:
    """Check whether the actor has the reviewed Shield effect."""
    state = snapshot.combat_state
    return bool(
        state.active_reaction_responses
        or state.reaction_effects_pending_expiry
        or any(e.id == "effect:imperceptible_barrier" for e in snapshot.effect_states)
        or any(
            isinstance(p, PendingReactionState) and p.spell_id == "shield"
            for p in state.pending_reactions
        )
    )


def shield_graph(snapshot: CombatSnapshot) -> ShieldGraphValue | None:
    """Resolve the complete Shield effect and lineage graph."""
    state = snapshot.combat_state
    if not snapshot.effect_states:
        if (
            state.conditions_by_effect
            or state.active_reaction_responses
            or state.reaction_effects_pending_expiry
        ):
            raise ValueError("Shield graph has dangling lineage, response or expiry")
        return None
    if (
        len(snapshot.effect_states) != 1
        or len(state.conditions_by_effect) != 1
        or len(state.active_reaction_responses) > 1
    ):
        raise ValueError("only one complete Shield effect graph is reviewed")
    effect = snapshot.effect_states[0]
    identity = (effect.target_id, effect.id, effect.origin)
    if state.reaction_effects_pending_expiry != {effect.target_id: [identity]}:
        raise ValueError("Shield requires the exact owner's next-start expiry ledger")
    return ShieldGraphValue(
        effect=effect,
        lineage=state.conditions_by_effect[0],
        response=response_state(state.active_reaction_responses[0])
        if state.active_reaction_responses
        else None,
        expiry_owner_id=effect.target_id,
        expiry_identity=identity,
        expires_at="owner_next_turn_start",
        owner_dead=effect.target_id in state.dead_ids,
    )


def plain_shield(snapshot: CombatSnapshot) -> CombatSnapshot:
    """Check that the effect matches the reviewed Shield profile."""
    return snapshot.model_copy(
        update={
            "effect_states": (),
            "combat_state": snapshot.combat_state.model_copy(
                update={
                    "conditions_by_effect": (),
                    "active_reaction_responses": [],
                    "reaction_effects_pending_expiry": {},
                }
            ),
        }
    )


def shield_source_failure(
    snapshot: CombatSnapshot, owner_id: str, source_id: str | None, loader: AssetLoader
) -> str | None:
    """Check source identity and dependencies for the Shield reaction."""
    actor = next((a for a in snapshot.character_states if a.entity_id == owner_id), None)
    if actor is None:
        return "Shield owner is absent"
    entry = (
        next((e for e in snapshot.casting_state.actors if e.actor_id == owner_id), None)
        if snapshot.casting_state
        else None
    )
    source = next((s for s in entry.sources if s.source_id == source_id), None) if entry else None
    if (
        actor.classes != {"wizard": 5}
        or actor.subclass_slug != "evoker"
        or source is None
        or source.class_slug != "wizard"
        or source.ability != "int"
        or actor.spellcasting_ability != "int"
        or "shield" not in source.known_spells
        or "shield" not in source.prepared_spells
    ):
        return "complete prepared/known Wizard 5 Evoker Shield source is required"
    if failure := wizard_pouch_dependencies(actor, snapshot, loader, get_default_ruleset()):
        return failure
    assert entry is not None
    spell = loader.get_spell("shield")
    assert spell is not None
    return spell_component_failure(
        spell,
        verbal_available=entry.verbal_available,
        somatic_hand_available=actor.weapon_in_hands is None and not actor.other_hand_occupied,
        material_available=False,
    )


def shield_source_id(snapshot: CombatSnapshot, owner_id: str) -> str | None:
    """Resolve the admitted Shield source identity."""
    entry = (
        next((e for e in snapshot.casting_state.actors if e.actor_id == owner_id), None)
        if snapshot.casting_state
        else None
    )
    source = next((s for s in entry.sources if s.class_slug == "wizard"), None) if entry else None
    return source.source_id if source else None


def shield_support_failure(
    snapshot: CombatSnapshot, loader: AssetLoader, *, turn: bool = False
) -> str | None:
    """Return a reason for unsupported Shield dependencies."""
    spell = loader.get_spell("shield")
    if spell is None or spell != get_default_ruleset().get_spell("shield"):
        return "canonical Shield definition is required"
    activity = spell.activities[0]
    if (
        not isinstance(activity, UtilityActivity)
        or reaction_support(activity)
        or admission_failure(spell, [activity])
    ):
        return "complete reviewed Shield utility payload is required"
    try:
        graph = shield_graph(snapshot)
    except ValueError as exc:
        return str(exc)
    pending = snapshot.combat_state.pending_reactions
    source: str | None
    if len(pending) > 1 or (pending and graph):
        return "competing or repeated reactions with active Shield are not migrated"
    if pending:
        declaration = pending[0]
        if (
            not isinstance(declaration, PendingReactionState)
            or declaration.spell_id != "shield"
            or declaration.activity_id != activity.id
            or declaration.slot_level != 1
            or declaration.declaration().conditions
            != tuple(activity.activation.reaction_conditions)
            or declaration.declaration().semantics != activity.reaction
        ):
            return "Shield predeclaration differs from the exact canonical trigger/source/response"
        owner, source = declaration.owner_id, declaration.spell_source_id
    elif graph:
        owner = graph.expiry_owner_id
        source = shield_source_id(snapshot, owner)
        if owner == snapshot.combat_state.initiative_ids[snapshot.combat_state.current_turn_index]:
            return "active Shield cannot survive its reviewed owner's turn-start boundary"
    else:
        return "Shield admission requires a declaration or complete existing graph"
    if failure := shield_source_failure(snapshot, owner, source, loader):
        return failure
    return operation_dependencies(
        plain_shield(snapshot),
        loader,
        attack=True,
        equipped=True,
        turn_lifecycle=turn,
        movement=turn,
        reviewed_reactions=bool(pending),
    )


class ShieldComputation(CombatComputation):
    """Resolve the admitted Shield reaction in isolated combat candidate state."""

    def __init__(self, snapshot: CombatSnapshot, rng: random.Random, loader: AssetLoader) -> None:
        """Initialize isolated Shield candidate effects and a separate resource payment
        list.
        """
        super().__init__(snapshot, rng)
        self.effects = list(snapshot.effect_states)
        self.loader = loader
        self.payments: list[ResourceUpdate] = []

    def emit(self, event: CombatEvent) -> None:
        """Append mechanical event evidence and fold its consequences into this candidate
        computation.
        """
        if isinstance(event, EffectApplied):
            effect = event.effect
            self.effects.append(effect_state(effect))
            identity = (effect.target_id, effect.id, effect.origin)
            self.state = self.state.model_copy(
                update={
                    "conditions_by_effect": (
                        *self.state.conditions_by_effect,
                        ConditionLink(identity=identity, statuses=[]),
                    )
                }
            )
        super().emit(event)
        if isinstance(event, TurnPhase) and event.phase == "turn_start":
            pending = self.state.reaction_effects_pending_expiry.get(event.actor_id or "", [])
            if pending:
                self.state = self.state.model_copy(
                    update={
                        "reaction_effects_pending_expiry": {
                            k: v
                            for k, v in self.state.reaction_effects_pending_expiry.items()
                            if k != event.actor_id
                        }
                    }
                )
            for identity in pending:
                self.effects = [
                    e for e in self.effects if (e.target_id, e.id, e.origin) != identity
                ]
                self.state = self.state.model_copy(
                    update={
                        "conditions_by_effect": tuple(
                            link
                            for link in self.state.conditions_by_effect
                            if link.identity != identity
                        ),
                        "active_reaction_responses": [
                            r
                            for r in self.state.active_reaction_responses
                            if r.effect_identity != identity
                        ],
                    }
                )
                super().emit(
                    EffectExpired(
                        target_id=identity[0],
                        effect_id=identity[1],
                        origin=identity[2],
                        reason="duration",
                    )
                )

    def record_death(self, event: Death, killer: str | None) -> None:
        """Record candidate death and remove the dead owner's pending reactions and
        responses.
        """
        super().record_death(event, killer)
        self.state = self.state.model_copy(
            update={
                "pending_reactions": [
                    p for p in self.state.pending_reactions if p.owner_id != event.target_id
                ],
                "active_reaction_responses": [
                    r for r in self.state.active_reaction_responses if r.owner_id != event.target_id
                ],
            }
        )

    def ac_bonus(self, target_id: str) -> int:
        """Read the candidate armor-class bonus applicable to the actor."""
        return scalar_ac_change_delta(
            Counter(), effect_ac_changes([e for e in self.effects if e.target_id == target_id])
        )

    def hit_reaction(self, attack: AttackHitContext) -> int:
        """Resolve a supported hit-triggered reaction against the candidate attack."""
        from dnd5e_engine.evaluation_spells import spell_event

        if not self.state.pending_reactions:
            return attack.effective_ac
        pending = self.state.pending_reactions[0]
        assert isinstance(pending, PendingReactionState)
        opportunity = ReactionOpportunity(
            ReactionTriggerKind.HIT_BY_ATTACK,
            attack.attacker_id,
            affected_target_id=attack.target_id,
            source_activity_id=attack.source_activity_id,
            attack=attack,
        )
        declaration = pending.declaration()
        owner = self.actors[pending.owner_id]
        if (
            not matching_conditions(declaration, opportunity)
            or reaction_target_id(declaration, opportunity) != owner.entity_id
            or not owner.reaction_available
            or not owner.is_alive
            or owner.hp_current <= 0
            or owner.entity_id in self.state.dead_ids
            or conditions_block_actions([c.condition for c in owner.conditions])
        ):
            return attack.effective_ac
        assert self.snapshot.resource_state is not None
        try:
            payment = spell_payment(
                self.snapshot.resource_state, owner.entity_id, pending.slot_level
            )
        except ValueError:
            return attack.effective_ac
        spell = self.loader.get_spell("shield")
        assert spell is not None
        activity = spell.activities[0]
        before = effect_ac_changes([e for e in self.effects if e.target_id == attack.target_id])
        self.actors[owner.entity_id] = owner.model_copy(update={"reaction_available": False})
        self.state = self.state.model_copy(update={"pending_reactions": []})
        self.payments.append(payment)
        self.emit(
            ReactionTriggered(
                actor_id=owner.entity_id,
                reaction_name=spell.slug,
                trigger_kind=opportunity.kind,
                triggering_actor_id=attack.attacker_id,
                affected_target_id=attack.target_id,
                source_spell_id=spell.slug,
                source_activity_id=activity.id,
                provisional_attack=provisional_attack_evidence(attack),
            )
        )
        self.emit(spell_event(owner.entity_id, spell, pending.slot_level))
        ctx = build_activity_context(
            self.actors[owner.entity_id],
            [self.actors[owner.entity_id]],
            rng=self.rng,
            event_emitter=self.emit,
            slot_level=pending.slot_level,
            base_spell_level=spell.level,
            spellcasting_ability="int",
            concentration=False,
            source_passive_effects=spell.passive_effects,
            spell_book={},
            passive_damage_modifiers={},
            save_modifiers={},
        )
        resolve_activity(activity, ctx)
        effect = self.effects[-1]
        identity = (effect.target_id, effect.id, effect.origin)
        ledger = slot_ledger(self.snapshot, payment)
        self.state = self.state.model_copy(
            update={
                "active_reaction_responses": [retained_reaction_response(activity, identity)],
                "reaction_effects_pending_expiry": {owner.entity_id: [identity]},
                "expended_resources": {
                    **self.state.expended_resources,
                    owner.entity_id: ledger.value,
                },
            }
        )
        after = effect_ac_changes([e for e in self.effects if e.target_id == attack.target_id])
        return attack.effective_ac + scalar_ac_change_delta(before, after)

    def candidate_state(self) -> CombatSnapshot:
        """Project this computation's working actors and components into a new candidate
        snapshot.
        """
        value = super().candidate_state().model_copy(update={"effect_states": tuple(self.effects)})
        return payment_post_state(value, tuple(self.payments))


def shield_delta(before: CombatSnapshot, after: CombatSnapshot) -> StateDelta:
    """Project Shield graph changes into guarded Delta operations."""
    old, new = shield_graph(before), shield_graph(after)
    pending = before.combat_state.pending_reactions
    payments: tuple[ResourceUpdate, ...] = ()
    if before.resource_state != after.resource_state:
        if len(pending) != 1 or not isinstance(pending[0], PendingReactionState):
            raise EvaluationInvariantError("Shield payment lacks its complete predeclaration")
        assert before.resource_state is not None
        payments = (
            spell_payment(before.resource_state, pending[0].owner_id, pending[0].slot_level),
        )
    ledgers = tuple(slot_ledger(before, p) for p in payments)
    base = reaction_paid_delta(plain_shield(before), plain_shield(after), payments, ledgers)
    if old == new:
        return base
    graph = new or old
    assert graph is not None
    source = shield_source_id(before, graph.expiry_owner_id)
    if source is None:
        raise EvaluationInvariantError("Shield graph has no explicit casting source")
    op = ShieldStateTransition(
        kind="combat.shield_state_transition",
        combat_id=before.combat_state.combat_id,
        owner_id=graph.expiry_owner_id,
        spell_source_id=source,
        actor_ids=before.combat_state.initiative_ids,
        expected=old,
        value=new,
    )
    return base.model_copy(update={"operations": (*base.operations, op)})
