"""Complete, explicit snapshot DTOs mapped from the Batch 1 inventory.

Every mechanical field is required, including empty collections and nullable
values. These local versioned types are not a cross-repository ABI decision.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from dnd5e_srd_data.schema.monster import CreatureSize, MonsterTraitMechanic
from dnd5e_srd_data.schema.spell import Spell
from pydantic import ConfigDict, Field, field_validator, model_validator

from dnd5e_engine.activities.conjuration import StatBlockMagnitudes, TransformSource
from dnd5e_engine.effect_lifecycle import OngoingEffectLifecycle
from dnd5e_engine.evaluation_base import EvaluationModel
from dnd5e_engine.evaluation_effects import EffectState
from dnd5e_engine.events import CombatEvent, DamageType
from dnd5e_engine.movement import MovementLedger
from dnd5e_engine.outcome import DeathRecord
from dnd5e_engine.persistent_areas import PersistentArea
from dnd5e_engine.reactions import ActiveReactionResponse, PendingReaction
from dnd5e_engine.specs import LightLevel, Obscurement, WallSegment
from dnd5e_engine.timed_activities import PendingTimedActivity
from dnd5e_engine.types.checks import HelpCheckGrant
from dnd5e_engine.types.combat import Combatant, FightingStyle, MonsterActionUses, WornArmor
from dnd5e_engine.types.conditions import ConditionScope
from dnd5e_engine.types.effects import ActiveEffectDuration
from dnd5e_engine.types.objects import CombatObject


class DeathSaveRecord(EvaluationModel):
    successes: Annotated[int, Field(ge=0, le=3)]
    failures: Annotated[int, Field(ge=0, le=4)]
    is_stable: bool


class ClosureDeath(EvaluationModel):
    """Strict boundary for the death records now admitted by combat.close."""

    target_id: Annotated[str, Field(min_length=1)]
    target_kind: Literal["character", "npc", "monster"]
    location_id: Annotated[str, Field(min_length=1)]
    reason: Literal["damage", "death_saves", "instant_kill"]
    killer_id: str | None


class ConditionRecord(EvaluationModel):
    condition: str
    source_entity_id: str
    scope: ConditionScope
    duration_rounds: int | None
    save_dc: int | None
    applied_round: int
    exhaustion_level: int
    source_effect_id: str | None


class SensesState(EvaluationModel):
    darkvision: int | None
    blindsight: int | None
    tremorsense: int | None
    truesight: int | None


class MovementModesState(EvaluationModel):
    climb: int | None
    swim: int | None
    fly: int | None
    burrow: int | None


class MovementLedgerState(MovementLedger):
    """Boundary fields cannot silently become a fresh turn's default ledger."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, revalidate_instances="always"
    )

    spent_ft: Annotated[int, Field(ge=0)] = Field(...)
    distance_ft: Annotated[int, Field(ge=0)] = Field(...)
    active_mode: Literal["walk", "crawl", "climb", "swim"] = Field(...)
    dash_count: Annotated[int, Field(ge=0)] = Field(...)


class TimedActivitiesState(EvaluationModel):
    pending: list[PendingTimedActivity]
    next_sequence: Annotated[int, Field(ge=0)]


class MechanicalGrid(EvaluationModel):
    width: Annotated[int, Field(ge=1)]
    height: Annotated[int, Field(ge=1)]
    cell_size_ft: Annotated[int, Field(ge=1)]
    blocked_cells: list[str]
    wall_segments: list[WallSegment]
    cover_cells: dict[str, Literal["half", "three_quarters", "total"]]
    difficult_terrain_cells: list[str]
    lighting: dict[str, LightLevel]
    default_lighting: LightLevel
    obscurement_cells: dict[str, Obscurement]
    sunlight: bool


class CharacterState(EvaluationModel):
    entity_id: str
    entity_type: Literal["Character", "Monster", "NPC"]
    name: str
    initiative: int
    hp_current: int
    temp_hp: int
    is_alive: bool
    conditions: list[ConditionRecord]
    hp_max: int
    ac: int
    attack_bonus: int | None
    attack_rolls_made_this_turn: int
    damage_dice: str
    damage_type: DamageType
    behavior_profile: str
    strength: int
    dexterity: int
    constitution: int
    intelligence: int
    wisdom: int
    charisma: int
    proficiency_bonus_override: int | None
    save_proficiencies: list[str]
    skill_proficiencies: list[str]
    skill_expertise: list[str]
    skill_check_bonuses: dict[str, int]
    jack_of_all_trades: bool
    reliable_talent: bool
    stealth_disadvantage: bool
    tool_proficiencies: tuple[str, ...]
    weapon_proficiencies: list[str] | None
    death_saves: DeathSaveRecord | None
    creature_type: str | None
    creature_size: CreatureSize
    damage_resistances: list[DamageType]
    damage_immunities: list[DamageType]
    damage_vulnerabilities: list[DamageType]
    condition_immunities: list[str]
    senses: SensesState
    concentration_effect_id: str | None
    character_level: int
    action_available: bool
    bonus_action_available: bool
    reaction_available: bool
    action_taken_this_turn: bool
    bonus_action_taken_this_turn: bool
    attack_action_attacks_made: int
    action_grants_spent: tuple[tuple[str, str], ...]
    action_grant_groups_spent: tuple[str, ...]
    base_speed: int
    movement_remaining: int
    movement_modes: MovementModesState
    melee_reach_ft: int
    granted_features: tuple[str, ...] | None
    class_slug: str | None
    classes: dict[str, int]
    fighting_styles: tuple[FightingStyle, ...]
    carried_item_slugs: tuple[str, ...]
    worn_armor: WornArmor | None
    shield_equipped: bool
    subclass_slug: str | None
    species_slug: str | None
    last_damaged_by: str | None
    disengaging_this_turn: bool
    sneak_attack_spent_this_turn: bool
    attacks_remaining: int
    attack_action_engaged: bool
    light_weapon_swing_slug: str | None
    restricted_light_weapon_swing_slug: str | None
    offhand_attack_spent: bool
    dodging: bool
    trait_mechanics: list[MonsterTraitMechanic]
    physical_resistances_nonmagical_only: bool
    legendary_actions_max: int
    legendary_actions_remaining: int
    legendary_resistances_max: int
    legendary_resistances_remaining: int
    has_fled: bool
    spellcasting_ability: str | None
    loading_weapon_fired_this_action: bool
    cleave_spent_this_turn: bool
    flurry_strikes_remaining: int
    extra_actions_remaining: int
    action_surge_used_this_turn: bool
    spell_slots: dict[int, int]
    pact_slots: dict[int, int]
    spells_known: list[str]
    custom_counters: dict[str, dict[str, int]]

    @model_validator(mode="after")
    def mechanical_bounds(self) -> Self:
        if not self.entity_id or self.hp_max <= 0 or not 0 <= self.hp_current <= self.hp_max:
            raise ValueError("invalid actor identity or HP bounds")
        numeric = (
            self.temp_hp,
            self.attacks_remaining,
            self.attack_rolls_made_this_turn,
            self.attack_action_attacks_made,
            self.movement_remaining,
            self.flurry_strikes_remaining,
            self.extra_actions_remaining,
        )
        if any(value < 0 for value in numeric):
            raise ValueError("negative actor mechanical counter")
        if any(
            level not in range(1, 10) or count < 0
            for pool in (self.spell_slots, self.pact_slots)
            for level, count in pool.items()
        ):
            raise ValueError("invalid spell slot pool")
        return self


class CharacterStateV2(CharacterState):
    weapon_in_hands: Annotated[str, Field(min_length=1)] | None
    weapon_grip: Literal["none", "one_handed", "two_handed"]
    other_hand_occupied: bool
    weapon_mastery_slugs: tuple[Annotated[str, Field(min_length=1)], ...]

    @model_validator(mode="after")
    def equipment_consistency(self) -> Self:
        if (self.weapon_in_hands is None) != (self.weapon_grip == "none"):
            raise ValueError("weapon identity and declared grip disagree")
        if self.weapon_in_hands is not None and self.weapon_in_hands not in self.carried_item_slugs:
            raise ValueError("held weapon is absent from supplied equipment")
        if self.weapon_grip == "two_handed" and (self.other_hand_occupied or self.shield_equipped):
            raise ValueError("two-handed grip conflicts with an occupied other hand")
        if len(set(self.weapon_mastery_slugs)) != len(self.weapon_mastery_slugs):
            raise ValueError("duplicate weapon mastery identity")
        return self


class SceneState(EvaluationModel):
    scene_id: Annotated[str, Field(min_length=1)]
    grid: MechanicalGrid


class ConditionLink(EvaluationModel):
    identity: tuple[str, str, str]
    statuses: list[str]


class LifecycleRecord(EvaluationModel):
    identity: tuple[str, str, str]
    state: OngoingEffectLifecycle


class EffectTurnRecord(EvaluationModel):
    identity: tuple[str, str, str]
    turn: int


class PersistentAreasState(EvaluationModel):
    areas: list[PersistentArea]
    next_sequence: int
    next_turn_start_effects: tuple[EffectTurnRecord, ...]


class ObjectState(EvaluationModel):
    objects: list[CombatObject]
    used_ids: set[str]


class ConstructState(EvaluationModel):
    construct_id: str
    owner_id: str
    spell_id: str
    cell: str
    slot_level: int
    anchor: tuple[str, str, str]
    cast_round: int


class TransformState(EvaluationModel):
    entity_id: str
    form_slug: str
    source: TransformSource
    effect_id: str
    origin: str
    original_actor: CharacterStateV2 | CharacterState
    replaced_fields: tuple[str, ...]
    original_monster_slug: str | None
    original_action_uses: dict[str, MonsterActionUses] | None
    form_proficiency_bonus: int
    attacks_per_action: int
    clears_temp_hp_on_end: bool


class SummonState(EvaluationModel):
    entity_id: str
    owner_id: str
    spell_id: str
    stat_block_slug: str
    slot_level: int
    anchor: tuple[str, str, str]
    magnitudes: StatBlockMagnitudes
    attacks_per_action: int


class CombatState(EvaluationModel):
    combat_id: Annotated[str, Field(min_length=1)]
    initiative_ids: tuple[str, ...]
    party_ids: set[str]
    encounter_ids: set[str]
    current_turn_index: int
    round_number: int
    turn_serial: int
    timed_activities: TimedActivitiesState
    persistent_areas: PersistentAreasState
    combat_objects: ObjectState
    ended: bool
    actor_zone: dict[str, str]
    movement_ledgers: dict[str, MovementLedgerState]
    opportunity_attack_weapons: dict[str, str]
    monster_slug_by_entity: dict[str, str]
    xp_value_by_entity: dict[str, Annotated[int, Field(ge=0)]]
    event_log: tuple[CombatEvent, ...]
    deaths_recorded: list[DeathRecord]
    dead_ids: set[str]
    expended_resources: dict[str, dict[str, int]]
    concentration_chain: dict[str, list[tuple[str, str, str]]]
    concentration_rounds_remaining: dict[str, int]
    conditions_by_effect: tuple[ConditionLink, ...]
    effect_lifecycles: tuple[LifecycleRecord, ...]
    pending_reactions: list[PendingReaction]
    active_reaction_responses: list[ActiveReactionResponse]
    damage_instance_sequence: int
    rider_uses: set[tuple[str, str, int]]
    processed_zero_hp_damage_instances: set[tuple[str, str]]
    reaction_effects_pending_expiry: dict[str, list[tuple[str, str, str]]]
    help_grants: dict[str, list[str]]
    help_check_grants: list[HelpCheckGrant]
    hidden_entities: set[str]
    vex_grants: dict[str, dict[str, int]]
    rage_bonus_extensions: set[str]
    sap_marks: dict[str, str]
    slow_marks: dict[str, set[str]]
    monster_action_uses_by_entity: dict[str, dict[str, MonsterActionUses]]
    monster_turn_start_done: tuple[int, str] | None
    last_ended_turn: tuple[int, str] | None
    departed_actor_id: str | None
    legendary_windows_used: set[tuple[int, str, str]]
    legendary_resistance_armed: dict[str, int]
    legendary_resistance_applied_event_indices: set[int]
    constructs: dict[str, ConstructState]
    transforms: dict[str, TransformState]
    summons: dict[str, SummonState]
    summon_counts: dict[str, int]

    @field_validator("deaths_recorded", mode="before")
    @classmethod
    def explicit_deaths(cls, value: object) -> object:
        if isinstance(value, list):
            return [
                DeathRecord.model_validate(
                    ClosureDeath.model_validate(
                        record.model_dump() if isinstance(record, DeathRecord) else record
                    ).model_dump()
                )
                for record in value
            ]
        return value

    @model_validator(mode="after")
    def turn_bounds(self) -> Self:
        if not self.initiative_ids or len(set(self.initiative_ids)) != len(self.initiative_ids):
            raise ValueError("initiative must contain unique actor identities")
        if not 0 <= self.current_turn_index < len(self.initiative_ids):
            raise ValueError("current turn is outside initiative")
        if self.round_number < 1 or self.turn_serial < 0 or self.damage_instance_sequence < 0:
            raise ValueError("invalid combat serial or round")
        if self.party_ids & self.encounter_ids:
            raise ValueError("combat sides overlap")
        return self


class InventoryEntry(EvaluationModel):
    """One owned homogeneous stack; charge balance is per remaining unit."""

    instance_id: Annotated[str, Field(min_length=1)]
    owner_id: Annotated[str, Field(min_length=1)]
    item_slug: Annotated[str, Field(min_length=1)]
    quantity: Annotated[int, Field(ge=0)]
    charges_remaining_per_unit: Annotated[int, Field(ge=0)]
    accessible: bool


class InventoryState(EvaluationModel):
    """Explicit mechanical component shared by both scene contexts."""

    entries: tuple[InventoryEntry, ...]

    @model_validator(mode="after")
    def unique_instances(self) -> Self:
        if len({entry.instance_id for entry in self.entries}) != len(self.entries):
            raise ValueError("duplicate inventory instance identity")
        return self


class SnapshotBase(EvaluationModel):
    snapshot_schema_version: Literal["engine-snapshot/6"]
    session_id: Annotated[str, Field(min_length=1)]
    world_version: Annotated[int, Field(ge=0)]
    character_states: tuple[CharacterStateV2, ...]
    effect_states: tuple[EffectState, ...]
    inventory_state: InventoryState
    scene_state: SceneState

    @model_validator(mode="after")
    def identities(self) -> Self:
        ids = [actor.entity_id for actor in self.character_states]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate snapshot actor identity")
        identities = [(effect.target_id, effect.id, effect.origin) for effect in self.effect_states]
        if len(set(identities)) != len(identities) or any(key[0] not in ids for key in identities):
            raise ValueError("duplicate effect identity or absent effect target")
        if any(entry.owner_id not in ids for entry in self.inventory_state.entries):
            raise ValueError("inventory owner is absent from snapshot")
        return self


class CombatSnapshot(SnapshotBase):
    snapshot_kind: Literal["combat"]
    combat_state: CombatState

    @model_validator(mode="after")
    def dependency_closure(self) -> Self:
        ids = {actor.entity_id for actor in self.character_states}
        state = self.combat_state
        if set(state.initiative_ids) != ids or set(state.actor_zone) != ids:
            raise ValueError("snapshot roster/position closure is incomplete")
        if not (state.party_ids | state.encounter_ids | set(state.summons)) <= ids:
            raise ValueError("snapshot sides reference missing actors")
        if set(state.movement_ledgers) != ids:
            raise ValueError("snapshot movement ledger closure is incomplete")
        if not set(state.monster_slug_by_entity) <= ids:
            raise ValueError("stat-block binding references an absent actor")
        if not state.encounter_ids <= set(state.xp_value_by_entity) <= ids:
            raise ValueError("snapshot Host XP closure is incomplete")
        for transform in state.transforms.values():
            if any(field not in CharacterState.model_fields for field in transform.replaced_fields):
                raise ValueError("unknown transformed actor field")
        return self


class CombatSetupState(EvaluationModel):
    """Explicit selected encounter facts; local candidate, not creation permission."""

    combat_id: Annotated[str, Field(min_length=1)]
    party_ids: tuple[str, ...]
    encounter_ids: tuple[str, ...]
    roll_order: tuple[str, ...]
    actor_zone: dict[str, str]
    fixed_initiative: dict[str, int | None]
    initiative_modifiers: dict[str, int | None]
    surprised_ids: set[str]
    xp_value_by_entity: dict[str, Annotated[int, Field(ge=0)]]
    opportunity_attack_weapons: dict[str, str]
    timed_activities: TimedActivitiesState
    persistent_areas: PersistentAreasState
    combat_objects: ObjectState

    @model_validator(mode="after")
    def closure(self) -> Self:
        from dnd5e_engine.spatial import canonical_cell_id

        ids = set(self.roll_order)
        if (
            not self.party_ids
            or not self.encounter_ids
            or self.roll_order != self.party_ids + self.encounter_ids
            or len(ids) != len(self.roll_order)
            or any(
                set(facts) != ids
                for facts in (self.actor_zone, self.fixed_initiative, self.initiative_modifiers)
            )
            or not self.surprised_ids <= ids
            or set(self.xp_value_by_entity) != set(self.encounter_ids)
            or not set(self.opportunity_attack_weapons) <= ids
            or any(canonical_cell_id(c) != c for c in self.actor_zone.values())
        ):
            raise ValueError("incomplete/noncanonical combat setup closure")
        return self


class NonCombatSnapshot(SnapshotBase):
    snapshot_kind: Literal["non_combat"]
    combat_setup: CombatSetupState | None

    @model_validator(mode="after")
    def setup_actors(self) -> Self:
        if self.combat_setup is not None and set(self.combat_setup.roll_order) != {
            a.entity_id for a in self.character_states
        }:
            raise ValueError("combat setup and complete snapshot actors disagree")
        return self


StateSnapshot = Annotated[
    CombatSnapshot | NonCombatSnapshot,
    Field(discriminator="snapshot_kind"),
]

# Legacy retained dataclasses use TYPE_CHECKING-only forward references. Resolve
# them explicitly for the portable schema; importing a runtime combat is unnecessary.
_SNAPSHOT_TYPES = {
    "Spell": Spell,
    "Combatant": Combatant,
    "ActiveEffectDuration": ActiveEffectDuration,
}
for _snapshot_model in (
    TimedActivitiesState,
    PersistentAreasState,
    CombatState,
    CombatSnapshot,
    NonCombatSnapshot,
):
    _snapshot_model.model_rebuild(_types_namespace=_SNAPSHOT_TYPES)
