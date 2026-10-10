"""Closed typed delta operations for the admitted basic attack slice."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from dnd5e_engine.evaluation_base import EvaluationModel
from dnd5e_engine.evaluation_effects import EffectState
from dnd5e_engine.evaluation_resources import ResourcePool
from dnd5e_engine.evaluation_state import (
    ClosureDeath,
    CombatSetupState,
    CombatState,
    ConditionLink,
    ConditionRecord,
    DeathSaveRecord,
    InventoryEntry,
    LifecycleRecord,
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


class PositionUpdate(EvaluationModel):
    """One computed route, fenced by scene/combat/actor and the old cell."""

    kind: Literal["combat.position_update"]
    combat_id: Annotated[str, Field(min_length=1)]
    scene_id: Annotated[str, Field(min_length=1)]
    actor_id: Annotated[str, Field(min_length=1)]
    expected: str
    value: str
    path: Annotated[tuple[str, ...], Field(min_length=2)]

    @model_validator(mode="after")
    def route_identity(self) -> Self:
        from dnd5e_engine.spatial import canonical_cell_id

        if (
            self.path[0] != self.expected
            or self.path[-1] != self.value
            or self.expected == self.value
        ):
            raise ValueError("position route endpoints disagree")
        if any(canonical_cell_id(cell) != cell for cell in self.path):
            raise ValueError("position route cells must be canonical")
        return self


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


class InitialActorState(EvaluationModel):
    initiative: int
    budget: AttackBudgetState


class InitialActorUpdate(EvaluationModel):
    actor_id: Annotated[str, Field(min_length=1)]
    expected: InitialActorState
    value: InitialActorState


class CombatCreationCandidate(EvaluationModel):
    """Local closed creation proposal, never a whole-world replacement."""

    kind: Literal["combat.create_candidate"]
    expected_absent: Literal[True]
    expected_setup: CombatSetupState
    value: CombatState
    actor_updates: tuple[InitialActorUpdate, ...]

    @model_validator(mode="after")
    def initial_components(self) -> Self:
        from dnd5e_engine.evaluation_initial_state import initial_combat_state

        ids = self.expected_setup.roll_order
        if (
            self.expected_setup.timed_activities != self.value.timed_activities
            or self.expected_setup.persistent_areas != self.value.persistent_areas
            or self.expected_setup.combat_objects != self.value.combat_objects
            or tuple(update.actor_id for update in self.actor_updates) != ids
            or set(self.value.initiative_ids) != set(ids)
            or self.value != initial_combat_state(self.expected_setup, self.value.initiative_ids)
        ):
            raise ValueError("creation candidate is not a complete fresh combat component")
        return self


class ResourceUpdate(EvaluationModel):
    kind: Literal["resource.update"]
    owner_id: Annotated[str, Field(min_length=1)]
    pool_id: Annotated[str, Field(min_length=1)]
    expected: ResourcePool
    value: ResourcePool
    amount: int

    @model_validator(mode="after")
    def owned_transition(self) -> Self:
        if (
            self.owner_id != self.expected.owner_id
            or self.pool_id != self.expected.pool_id
            or self.expected.model_dump(exclude={"current"})
            != self.value.model_dump(exclude={"current"})
            or self.expected.current + self.amount != self.value.current
        ):
            raise ValueError("resource update changes identity/capacity or has invalid arithmetic")
        return self


class EffectLifecycleUpdate(EvaluationModel):
    """Local C-15 candidate: atomic selected effect/clock/lineage transition."""

    kind: Literal["combat.effect_lifecycle_update"]
    combat_id: Annotated[str, Field(min_length=1)]
    identity: tuple[str, str, str]
    expected_effect: EffectState
    effect: EffectState | None
    expected_lifecycle: LifecycleRecord
    lifecycle: LifecycleRecord | None
    expected_lineage: ConditionLink
    lineage: ConditionLink | None

    @model_validator(mode="after")
    def complete_identity(self) -> Self:
        effect = self.expected_effect
        if (
            self.identity != (effect.target_id, effect.id, effect.origin)
            or self.expected_lifecycle.identity != self.identity
            or self.expected_lifecycle.state.identity != self.identity
            or self.expected_lineage.identity != self.identity
        ):
            raise ValueError("effect transition expected identities differ")
        removed = (self.effect is None, self.lifecycle is None, self.lineage is None)
        if any(removed) and not all(removed):
            raise ValueError("effect, clock and lineage must be removed atomically")
        if self.effect is not None:
            assert self.lifecycle is not None
            assert self.lineage is not None
            old, new = self.expected_lifecycle.state, self.lifecycle.state
            from dataclasses import replace

            if (
                self.effect != self.expected_effect
                or self.lineage != self.expected_lineage
                or self.lifecycle.identity != self.identity
                or new.last_repeat_turn_serial is None
                or new.last_repeat_turn_serial < old.applied_turn_serial
                or (
                    old.last_repeat_turn_serial is not None
                    and new.last_repeat_turn_serial <= old.last_repeat_turn_serial
                )
                or new != replace(old, last_repeat_turn_serial=new.last_repeat_turn_serial)
            ):
                raise ValueError("retained effect may only advance its repeat clock")
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
    | PositionUpdate
    | DamageSequenceUpdate
    | ProcessedDamageUpdate
    | DeathLedgerUpdate
    | CombatClose
    | InventoryConsume
    | CombatCreationCandidate
    | ResourceUpdate
    | EffectLifecycleUpdate,
    Field(discriminator="kind"),
]


class StateDelta(EvaluationModel):
    operations: tuple[StateDeltaOperation, ...]
    expected_world_version: Annotated[int, Field(ge=0)]
