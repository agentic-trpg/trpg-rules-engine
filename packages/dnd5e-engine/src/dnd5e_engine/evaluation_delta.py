"""Closed typed delta operations for the admitted basic attack slice."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from dnd5e_engine.evaluation_base import EvaluationModel
from dnd5e_engine.evaluation_state import (
    ClosureDeath,
    ConditionRecord,
    DeathSaveRecord,
    InventoryEntry,
    MovementLedgerState,
)
from dnd5e_engine.outcome import DeathRecord


class AttackBudgetState(EvaluationModel):
    action_available: bool
    bonus_action_available: bool
    reaction_available: bool
    action_taken_this_turn: bool
    bonus_action_taken_this_turn: bool
    attack_action_attacks_made: int
    attack_rolls_made_this_turn: int
    action_grants_spent: tuple[tuple[str, str], ...]
    action_grant_groups_spent: tuple[str, ...]
    movement_remaining: int
    disengaging_this_turn: bool
    sneak_attack_spent_this_turn: bool
    attacks_remaining: int
    attack_action_engaged: bool
    light_weapon_swing_slug: str | None
    restricted_light_weapon_swing_slug: str | None
    offhand_attack_spent: bool
    dodging: bool
    loading_weapon_fired_this_action: bool
    cleave_spent_this_turn: bool
    flurry_strikes_remaining: int
    extra_actions_remaining: int
    action_surge_used_this_turn: bool

    @model_validator(mode="after")
    def bounds(self) -> Self:
        counts = (
            self.attack_action_attacks_made,
            self.attack_rolls_made_this_turn,
            self.movement_remaining,
            self.attacks_remaining,
            self.flurry_strikes_remaining,
            self.extra_actions_remaining,
        )
        if any(value < 0 for value in counts):
            raise ValueError("negative action budget")
        return self


class DeathState(EvaluationModel):
    is_alive: bool
    death_saves: DeathSaveRecord | None


class TurnState(EvaluationModel):
    current_turn_index: Annotated[int, Field(ge=0)]
    round_number: Annotated[int, Field(ge=1)]
    turn_serial: Annotated[int, Field(ge=0)]
    monster_turn_start_done: tuple[int, str] | None
    last_ended_turn: tuple[int, str] | None
    departed_actor_id: str | None
    ended: bool


class HPDelta(EvaluationModel):
    kind: Literal["actor.hp_delta"]
    target_id: Annotated[str, Field(min_length=1)]
    expected_hp: Annotated[int, Field(ge=0)]
    amount: int
    resulting_hp: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def arithmetic(self) -> Self:
        if self.expected_hp + self.amount != self.resulting_hp:
            raise ValueError("HP delta arithmetic mismatch")
        return self


class TempHPSet(EvaluationModel):
    kind: Literal["actor.temp_hp_set"]
    target_id: Annotated[str, Field(min_length=1)]
    expected_temp_hp: Annotated[int, Field(ge=0)]
    temp_hp: Annotated[int, Field(ge=0)]


class DeathStateUpdate(EvaluationModel):
    kind: Literal["actor.death_state_update"]
    target_id: Annotated[str, Field(min_length=1)]
    expected: DeathState
    value: DeathState


class ConditionsUpdate(EvaluationModel):
    kind: Literal["actor.conditions_update"]
    target_id: Annotated[str, Field(min_length=1)]
    expected: tuple[ConditionRecord, ...]
    value: tuple[ConditionRecord, ...]


class DamageAttributionUpdate(EvaluationModel):
    kind: Literal["actor.damage_attribution_update"]
    target_id: Annotated[str, Field(min_length=1)]
    expected: str | None
    value: str | None


class ActionBudgetUpdate(EvaluationModel):
    kind: Literal["combat.action_budget_update"]
    actor_id: Annotated[str, Field(min_length=1)]
    expected: AttackBudgetState
    value: AttackBudgetState


class TurnUpdate(EvaluationModel):
    kind: Literal["combat.turn_update"]
    combat_id: Annotated[str, Field(min_length=1)]
    expected: TurnState
    value: TurnState


class MovementLedgerUpdate(EvaluationModel):
    kind: Literal["combat.movement_ledger_update"]
    actor_id: Annotated[str, Field(min_length=1)]
    expected: MovementLedgerState
    value: MovementLedgerState


class DamageSequenceUpdate(EvaluationModel):
    kind: Literal["combat.damage_sequence_update"]
    combat_id: Annotated[str, Field(min_length=1)]
    expected: Annotated[int, Field(ge=0)]
    value: Annotated[int, Field(ge=0)]


class ProcessedDamageUpdate(EvaluationModel):
    kind: Literal["combat.processed_damage_update"]
    combat_id: Annotated[str, Field(min_length=1)]
    expected: frozenset[tuple[str, str]]
    value: frozenset[tuple[str, str]]


class DeathLedgerUpdate(EvaluationModel):
    kind: Literal["combat.death_ledger_update"]
    combat_id: Annotated[str, Field(min_length=1)]
    expected_dead_ids: frozenset[str]
    dead_ids: frozenset[str]
    expected_records: tuple[DeathRecord, ...]
    records: tuple[DeathRecord, ...]


class HistoricalCombatOutcome(EvaluationModel):
    """Read-only balances/deaths already committed before closure; never mutations."""

    deaths: tuple[ClosureDeath, ...]
    residual_hp: dict[str, Annotated[int, Field(ge=0)]]
    residual_temp_hp: dict[str, Annotated[int, Field(gt=0)]]
    expended_resources: dict[str, dict[str, Annotated[int, Field(ge=0)]]]
    loot_drops: tuple[()]  # No loot operation is admitted by this version.


class CombatClose(EvaluationModel):
    """Only ended and XP increments mutate state, atomically under both fences."""

    kind: Literal["combat.close"]
    combat_id: Annotated[str, Field(min_length=1)]
    expected_ended: Literal[False]
    ended: Literal[True]
    reason: Literal["victory", "defeat_tpk"]
    xp_increments: dict[str, Annotated[int, Field(gt=0)]]
    historical: HistoricalCombatOutcome


class InventoryConsume(EvaluationModel):
    kind: Literal["inventory.consume"]
    expected: InventoryEntry
    value: InventoryEntry
    amount: Annotated[int, Field(ge=1, le=1)]

    @model_validator(mode="after")
    def consume_one_unit(self) -> Self:
        if self.expected.quantity < 1 or self.expected.charges_remaining_per_unit != 1:
            raise ValueError("consume requires an available single-use unit")
        if not self.expected.accessible or self.value != self.expected.model_copy(
            update={"quantity": self.expected.quantity - self.amount}
        ):
            raise ValueError(
                "consume must preserve owner, identity, access and remaining-unit charges"
            )
        return self


StateDeltaOperation = Annotated[
    HPDelta
    | TempHPSet
    | DeathStateUpdate
    | ConditionsUpdate
    | DamageAttributionUpdate
    | ActionBudgetUpdate
    | TurnUpdate
    | MovementLedgerUpdate
    | DamageSequenceUpdate
    | ProcessedDamageUpdate
    | DeathLedgerUpdate
    | CombatClose
    | InventoryConsume,
    Field(discriminator="kind"),
]


class StateDelta(EvaluationModel):
    operations: tuple[StateDeltaOperation, ...]
    expected_world_version: Annotated[int, Field(ge=0)]
