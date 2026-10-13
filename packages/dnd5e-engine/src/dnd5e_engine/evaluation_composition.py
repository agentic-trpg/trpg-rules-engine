"""One bounded Hold Person + Shield computation; explicit ordered rule folds."""

import random

from dnd5e_srd_data.loader import AssetLoader

from dnd5e_engine.activities.context import DamageInstanceContext
from dnd5e_engine.effect_lifecycle import EffectIdentity
from dnd5e_engine.evaluation_concentration import concentration_support_failure
from dnd5e_engine.evaluation_concentration_mechanics import ConcentrationMechanics
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd5e_engine.evaluation_delta import ResourceUpdate, StateDelta
from dnd5e_engine.evaluation_effect_ownership import effect_ownership, without_owned_shield
from dnd5e_engine.evaluation_paid_projection import slot_ledger
from dnd5e_engine.evaluation_payments import spell_payment
from dnd5e_engine.evaluation_projection import EvaluationInvariantError
from dnd5e_engine.evaluation_reaction_state import PendingReactionState
from dnd5e_engine.evaluation_reactions import reaction_paid_delta
from dnd5e_engine.evaluation_shield import (
    ShieldComputation,
    has_shield,
    shield_graph_operations,
    shield_policy_failure,
    shield_source_id,
)
from dnd5e_engine.evaluation_state import CombatSnapshot
from dnd5e_engine.events import CombatEvent, Death, EffectExpiryReason


def has_composed_effects(snapshot: CombatSnapshot) -> bool:
    return bool(snapshot.combat_state.concentration_chain) and has_shield(snapshot)


def composition_support_failure(
    snapshot: CombatSnapshot,
    loader: AssetLoader,
    *,
    target_id: str | None = None,
    turn: bool = False,
) -> str | None:
    from dnd5e_engine.evaluation_hold_cast import hold_cast_source_failure

    try:
        ownership = effect_ownership(snapshot)
    except ValueError as exc:
        return str(exc)
    source = ownership.hold_source_id
    if source is None or not ownership.hold_identities or len(ownership.hold_identities) > 2:
        return "one complete Hold Person chain with one/two reviewed targets is required"
    if failure := shield_policy_failure(snapshot, loader, ownership.shield):
        return failure
    pending = snapshot.combat_state.pending_reactions
    if pending and pending[0].owner_id != source:
        return "Shield declaration must belong to the Hold Person caster"
    if any(not a.is_alive or a.hp_current <= 0 for a in snapshot.character_states):
        return "composed input requires living positive-HP actors"
    current = snapshot.combat_state.initiative_ids[snapshot.combat_state.current_turn_index]
    if turn or current == source or any(i[0] == current for i in ownership.hold_identities):
        return "R39 does not yet admit Hold repeat-save/caster turn-end boundaries"
    if target_id != source:
        return "R39 composed attack must target the concentrating Shield owner"
    # Full partition and canonical Shield policy precede this narrowly owned view.
    # Remove only that reviewed pending declaration, not unknown reaction records.
    hold_view = without_owned_shield(snapshot, ownership)
    hold_view = hold_view.model_copy(
        update={"combat_state": hold_view.combat_state.model_copy(update={"pending_reactions": []})}
    )
    if failure := concentration_support_failure(hold_view, source, loader, composed=True):
        return failure
    spell_source = shield_source_id(snapshot, source)
    intent = CombatIntentPayload(
        intent_type="cast_spell",
        source_id=source,
        spell_id="hold-person",
        spell_source_id=spell_source,
    )
    return hold_cast_source_failure(snapshot, intent, source, loader)


class ComposedEffectComputation(ShieldComputation):
    """Single inheritance and one state; no two-evaluator Delta concatenation."""

    def __init__(self, snapshot: CombatSnapshot, rng: random.Random, loader: AssetLoader) -> None:
        super().__init__(snapshot, rng, loader)
        self.concentration = ConcentrationMechanics(self)

    def emit(self, event: CombatEvent) -> None:
        super().emit(event)
        self.concentration.after_event(event)

    def record_death(self, event: Death, killer: str | None) -> None:
        if event.target_id in self.state.concentration_chain:
            self.concentration.drop(event.target_id)
        super().record_death(event, killer)

    def damage_completed(self, damage: DamageInstanceContext) -> None:
        self.concentration.damage_completed(damage)
        super().damage_completed(damage)

    def drop(self, source_id: str, *, reason: EffectExpiryReason = "concentration_drop") -> None:
        self.concentration.drop(source_id, reason=reason)

    def expire_linked_effect(self, identity: EffectIdentity, reason: EffectExpiryReason) -> None:
        self.concentration.expire_linked_effect(identity, reason)


def composition_delta(before: CombatSnapshot, after: CombatSnapshot) -> StateDelta:
    old, new = effect_ownership(before), effect_ownership(after)
    payments: tuple[ResourceUpdate, ...] = ()
    if before.resource_state != after.resource_state:
        pending = before.combat_state.pending_reactions
        if (
            len(pending) != 1
            or not isinstance(pending[0], PendingReactionState)
            or before.resource_state is None
        ):
            raise EvaluationInvariantError("composed Shield payment lacks its one declaration")
        payments = (
            spell_payment(before.resource_state, pending[0].owner_id, pending[0].slot_level),
        )
    ledgers = tuple(slot_ledger(before, payment) for payment in payments)
    # The resource/reaction layer invokes concentration_delta and its one base
    # omission guard. Shield contributes only its disjoint typed graph transition.
    base = reaction_paid_delta(
        without_owned_shield(before, old), without_owned_shield(after, new), payments, ledgers
    )
    graph_ops = shield_graph_operations(before, old.shield, new.shield)
    return base.model_copy(update={"operations": (*base.operations, *graph_ops)})
