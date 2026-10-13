"""Explicit local concentration folds shared by standalone and composed execution."""

import random
from typing import Protocol

from dnd5e_engine.activities.context import DamageInstanceContext
from dnd5e_engine.damage_rules import concentration_dc
from dnd5e_engine.effect_lifecycle import (
    EffectIdentity,
    concentration_chain_after_expiry,
    conditions_after_effect_expiry,
)
from dnd5e_engine.evaluation_contracts import SavePayload
from dnd5e_engine.evaluation_effects import EffectState
from dnd5e_engine.evaluation_state import CharacterStateV2, CombatSnapshot, CombatState
from dnd5e_engine.events import (
    CombatEvent,
    ConcentrationCheck,
    ConcentrationDropped,
    ConditionApplied,
    ConditionRemoved,
    EffectExpired,
    EffectExpiryReason,
)
from dnd5e_engine.movement import MovementMode
from dnd5e_engine.rules.conditions import conditions_break_concentration
from dnd5e_engine.types.combat import Combatant


class ConcentrationHost(Protocol):
    snapshot: CombatSnapshot
    state: CombatState
    actors: dict[str, Combatant]
    effects: list[EffectState]
    events: list[CombatEvent]
    rng: random.Random

    def speed(self, actor: Combatant, mode: MovementMode = "walk") -> int: ...
    def actor_snapshot(self, actor_id: str) -> CharacterStateV2: ...
    def drop(
        self, source_id: str, *, reason: EffectExpiryReason = "concentration_drop"
    ) -> None: ...
    def expire_linked_effect(
        self, identity: EffectIdentity, reason: EffectExpiryReason
    ) -> None: ...


class ConcentrationMechanics:
    """One computation's existing graph; no second candidate state or evaluator."""

    def __init__(self, host: ConcentrationHost) -> None:
        self.host = host

    def after_event(self, event: CombatEvent) -> None:
        host = self.host
        if (
            isinstance(event, ConditionApplied)
            and conditions_break_concentration([event.condition])
            and any(c.condition == event.condition for c in host.actors[event.target_id].conditions)
            and event.target_id in host.state.concentration_chain
        ):
            host.drop(event.target_id)

    def damage_completed(self, damage: DamageInstanceContext) -> None:
        from dnd5e_engine import evaluation_concentration

        host = self.host
        # Preserve existing completed-instance ordering: concentration check before
        # the zero-HP fold, including damage absorbed by temporary HP.
        if damage.amount > 0 and damage.target_id in host.state.concentration_chain:
            save = evaluation_concentration.resolve_snapshot_save(
                host.actor_snapshot(damage.target_id),
                SavePayload(
                    kind="rules.save",
                    target_id=damage.target_id,
                    ability="con",
                    dc=concentration_dc(damage.amount),
                    advantage=(),
                    disadvantage=(),
                    is_magical=False,
                ),
                host.rng,
            )
            host.events.append(ConcentrationCheck(**save.model_dump(exclude={"type", "ability"})))
            if not save.succeeded:
                host.drop(damage.target_id)

    def drop(self, source_id: str, *, reason: EffectExpiryReason = "concentration_drop") -> None:
        host = self.host
        for identity in tuple(host.state.concentration_chain[source_id]):
            host.events.append(ConcentrationDropped(target_id=source_id, effect_name=identity[1]))
            host.expire_linked_effect(identity, reason)

    def expire_linked_effect(self, identity: EffectIdentity, reason: EffectExpiryReason) -> None:
        host = self.host
        target, effect_id, origin = identity
        effect = next(e for e in host.effects if (e.target_id, e.id, e.origin) == identity)
        source_id = effect.lifecycle.source_id if effect.lifecycle else effect.target_id
        host.events.append(
            EffectExpired(target_id=target, effect_id=effect_id, origin=origin, reason=reason)
        )
        host.effects = [e for e in host.effects if (e.target_id, e.id, e.origin) != identity]
        actor = host.actors[target]
        actor = actor.model_copy(
            update={
                "conditions": conditions_after_effect_expiry(
                    actor.conditions, effect_id, effect.statuses, []
                )
            }
        )
        ledger = host.state.movement_ledgers[target]
        host.actors[target] = actor.model_copy(
            update={"movement_remaining": ledger.remaining(host.speed(actor))}
        )
        if "paralyzed" in effect.statuses:
            host.events.append(ConditionRemoved(target_id=target, condition="paralyzed"))
        remaining = concentration_chain_after_expiry(
            host.state.concentration_chain[source_id], identity
        )
        chains = {k: v for k, v in host.state.concentration_chain.items() if k != source_id}
        rounds = dict(host.state.concentration_rounds_remaining)
        if remaining:
            chains[source_id] = list(remaining)
        else:
            rounds.pop(source_id)
            host.actors[source_id] = host.actors[source_id].model_copy(
                update={"concentration_effect_id": None}
            )
        host.state = host.state.model_copy(
            update={
                "concentration_chain": chains,
                "concentration_rounds_remaining": rounds,
                "effect_lifecycles": tuple(
                    r for r in host.state.effect_lifecycles if r.identity != identity
                ),
                "conditions_by_effect": tuple(
                    r for r in host.state.conditions_by_effect if r.identity != identity
                ),
            }
        )
