"""Damage balances and zero-HP rules shared by stateless and legacy folds."""

from dataclasses import dataclass
from typing import Literal

from dnd5e_engine.death_saves import DeathSaveState
from dnd5e_engine.events import CombatEvent, ConditionApplied, DamageApplied, Death, Unconscious
from dnd5e_engine.outcome import DeathRecord
from dnd5e_engine.rules.conditions import active_condition_names
from dnd5e_engine.types.combat import Combatant
from dnd5e_engine.types.conditions import ActiveCondition


def damage_balances(hp: int, temp_hp: int, amount: int) -> tuple[int, int, int]:
    absorbed = min(temp_hp, amount)
    remaining = amount - absorbed
    return max(0, hp - remaining), temp_hp - absorbed, remaining


@dataclass(frozen=True)
class ZeroHPResult:
    actor: Combatant
    events: tuple[CombatEvent, ...]
    processed: frozenset[tuple[str, str]]


def zero_hp_damage(
    target: Combatant,
    event: DamageApplied,
    *,
    hp_before: int,
    damage_after_temp: int,
    processed: frozenset[tuple[str, str]],
) -> ZeroHPResult:
    """One completed damage instance: massive damage, unconsciousness, failures."""
    hp_max = target.hp_max or target.hp_current
    remainder = damage_after_temp - hp_before if hp_before > 0 else damage_after_temp
    if remainder >= hp_max:
        return ZeroHPResult(
            target.model_copy(update={"is_alive": False}),
            (Death(target_id=target.entity_id, reason="instant_kill"),),
            processed,
        )
    identity = (
        (event.target_id, event.damage_instance_id)
        if event.damage_instance_id is not None
        else None
    )
    if hp_before > 0:
        if identity is not None and damage_after_temp > 0:
            processed = processed | {identity}
        events: tuple[CombatEvent, ...] = ()
        if "unconscious" not in active_condition_names(target.conditions):
            events = (
                Unconscious(target_id=target.entity_id),
                ConditionApplied(target_id=target.entity_id, condition="unconscious"),
            )
        return ZeroHPResult(target, events, processed)
    if damage_after_temp <= 0 or (identity is not None and identity in processed):
        return ZeroHPResult(target, (), processed)
    if identity is not None:
        processed = processed | {identity}
    state = DeathSaveState.from_dict(target.death_saves) if target.death_saves else DeathSaveState()
    outcome = state.apply_damage_while_unconscious(event.is_crit)
    actor = target.model_copy(
        update={
            "death_saves": state.to_dict(),
            **({"is_alive": False} if outcome == "dead" else {}),
        }
    )
    return ZeroHPResult(
        actor,
        (Death(target_id=target.entity_id, reason="death_saves"),) if outcome == "dead" else (),
        processed,
    )


def death_record(
    actor: Combatant, event: Death, *, location_id: str, killer_id: str | None
) -> DeathRecord:
    kind: Literal["character", "npc", "monster"] = (
        "character"
        if actor.entity_type == "Character"
        else "npc"
        if actor.entity_type == "NPC"
        else "monster"
    )
    return DeathRecord(
        target_id=event.target_id,
        target_kind=kind,
        location_id=location_id,
        reason=event.reason,
        killer_id=killer_id if killer_id != event.target_id else None,
    )


def with_event_condition(
    actor: Combatant,
    condition: str,
    *,
    round_number: int,
    save_dc: int | None = None,
    source_effect_id: str | None = None,
) -> Combatant:
    if any(c.condition == condition for c in actor.conditions):
        return actor
    return actor.model_copy(
        update={
            "conditions": [
                *actor.conditions,
                ActiveCondition(
                    condition=condition,
                    source_entity_id="implied:event",
                    scope="combat",
                    applied_round=round_number,
                    save_dc=save_dc,
                    source_effect_id=source_effect_id,
                ),
            ]
        }
    )
