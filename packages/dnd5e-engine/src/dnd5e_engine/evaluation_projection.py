"""Project all admitted mechanical changes, with an exhaustive omission guard."""

from dnd5e_engine import evaluation_delta as delta
from dnd5e_engine.evaluation_state import CombatSnapshot


class EvaluationInvariantError(RuntimeError):
    """Unexpected mechanical change outside the admitted closed delta vocabulary."""


def attack_delta(before: CombatSnapshot, after: CombatSnapshot) -> delta.StateDelta:
    operations: list[delta.StateDeltaOperation] = []
    if (
        before.snapshot_kind != after.snapshot_kind
        or before.snapshot_schema_version != after.snapshot_schema_version
        or before.session_id != after.session_id
        or before.world_version != after.world_version
        or before.effect_states != after.effect_states
        or before.inventory_state != after.inventory_state
        or before.scene_state != after.scene_state
        or tuple(a.entity_id for a in before.character_states)
        != tuple(a.entity_id for a in after.character_states)
    ):
        raise EvaluationInvariantError("unrepresented snapshot dependency change")
    for old, new in zip(before.character_states, after.character_states, strict=True):
        pending = {
            name for name in type(old).model_fields if getattr(old, name) != getattr(new, name)
        }
        target = old.entity_id
        if "hp_current" in pending:
            operations.append(
                delta.HPDelta(
                    kind="actor.hp_delta",
                    target_id=target,
                    expected_hp=old.hp_current,
                    amount=new.hp_current - old.hp_current,
                    resulting_hp=new.hp_current,
                )
            )
            pending.remove("hp_current")
        if "temp_hp" in pending:
            operations.append(
                delta.TempHPSet(
                    kind="actor.temp_hp_set",
                    target_id=target,
                    expected_temp_hp=old.temp_hp,
                    temp_hp=new.temp_hp,
                )
            )
            pending.remove("temp_hp")
        fields: tuple[str, ...] = ("is_alive", "death_saves")
        if pending.intersection(fields):
            operations.append(
                delta.DeathStateUpdate(
                    kind="actor.death_state_update",
                    target_id=target,
                    expected=delta.DeathState(is_alive=old.is_alive, death_saves=old.death_saves),
                    value=delta.DeathState(is_alive=new.is_alive, death_saves=new.death_saves),
                )
            )
            pending.difference_update(fields)
        fields = tuple(delta.AttackBudgetState.model_fields)
        if pending.intersection(fields):
            operations.append(
                delta.ActionBudgetUpdate(
                    kind="combat.action_budget_update",
                    actor_id=target,
                    expected=delta.AttackBudgetState.model_validate(
                        {name: getattr(old, name) for name in fields}
                    ),
                    value=delta.AttackBudgetState.model_validate(
                        {name: getattr(new, name) for name in fields}
                    ),
                )
            )
            pending.difference_update(fields)
        if "conditions" in pending:
            operations.append(
                delta.ConditionsUpdate(
                    kind="actor.conditions_update",
                    target_id=target,
                    expected=tuple(old.conditions),
                    value=tuple(new.conditions),
                )
            )
            pending.remove("conditions")
        if "last_damaged_by" in pending:
            operations.append(
                delta.DamageAttributionUpdate(
                    kind="actor.damage_attribution_update",
                    target_id=target,
                    expected=old.last_damaged_by,
                    value=new.last_damaged_by,
                )
            )
            pending.remove("last_damaged_by")
        if pending:
            raise EvaluationInvariantError(f"unrepresented actor changes: {sorted(pending)}")
    old_state, new_state = before.combat_state, after.combat_state
    pending = {
        name
        for name in type(old_state).model_fields
        if getattr(old_state, name) != getattr(new_state, name)
    }
    combat = old_state.combat_id
    fields = tuple(delta.TurnState.model_fields)
    if pending.intersection(fields):
        operations.append(
            delta.TurnUpdate(
                kind="combat.turn_update",
                combat_id=combat,
                expected=delta.TurnState.model_validate(
                    {name: getattr(old_state, name) for name in fields}
                ),
                value=delta.TurnState.model_validate(
                    {name: getattr(new_state, name) for name in fields}
                ),
            )
        )
        pending.difference_update(fields)
    for actor_id, ledger in old_state.movement_ledgers.items():
        if ledger != new_state.movement_ledgers[actor_id]:
            operations.append(
                delta.MovementLedgerUpdate(
                    kind="combat.movement_ledger_update",
                    actor_id=actor_id,
                    expected=ledger,
                    value=new_state.movement_ledgers[actor_id],
                )
            )
    if old_state.movement_ledgers.keys() != new_state.movement_ledgers.keys():
        raise EvaluationInvariantError("unrepresented movement roster change")
    pending.discard("movement_ledgers")
    if "damage_instance_sequence" in pending:
        operations.append(
            delta.DamageSequenceUpdate(
                kind="combat.damage_sequence_update",
                combat_id=combat,
                expected=old_state.damage_instance_sequence,
                value=new_state.damage_instance_sequence,
            )
        )
        pending.remove("damage_instance_sequence")
    if "processed_zero_hp_damage_instances" in pending:
        operations.append(
            delta.ProcessedDamageUpdate(
                kind="combat.processed_damage_update",
                combat_id=combat,
                expected=frozenset(old_state.processed_zero_hp_damage_instances),
                value=frozenset(new_state.processed_zero_hp_damage_instances),
            )
        )
        pending.remove("processed_zero_hp_damage_instances")
    if pending.intersection(("dead_ids", "deaths_recorded")):
        operations.append(
            delta.DeathLedgerUpdate(
                kind="combat.death_ledger_update",
                combat_id=combat,
                expected_dead_ids=frozenset(old_state.dead_ids),
                dead_ids=frozenset(new_state.dead_ids),
                expected_records=tuple(old_state.deaths_recorded),
                records=tuple(new_state.deaths_recorded),
            )
        )
        pending.difference_update(("dead_ids", "deaths_recorded"))
    if new_state.event_log[: len(old_state.event_log)] != old_state.event_log:
        raise EvaluationInvariantError("committed rule history was rewritten")
    pending.discard("event_log")  # returned as ordered proposals, SM appends only on commit
    if pending:
        raise EvaluationInvariantError(f"unrepresented combat changes: {sorted(pending)}")
    return delta.StateDelta(
        operations=tuple(operations), expected_world_version=before.world_version
    )
