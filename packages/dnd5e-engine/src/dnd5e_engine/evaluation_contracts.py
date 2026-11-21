"""Versioned local request/result contracts with explicit failure boundaries."""

from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, ValidationInfo, field_validator, model_validator
from pydantic_core import to_json

from dnd5e_engine.evaluation_base import EvaluationModel
from dnd5e_engine.evaluation_delta import CombatEffectCreate, InventorySpendCharge, StateDelta
from dnd5e_engine.evaluation_environmental_contracts import (
    EnvironmentalItemFacts as EnvironmentalItemFacts,
)
from dnd5e_engine.evaluation_environmental_contracts import (
    EnvironmentalItemTarget as EnvironmentalItemTarget,
)
from dnd5e_engine.evaluation_environmental_contracts import (
    EnvironmentalPointTarget as EnvironmentalPointTarget,
)
from dnd5e_engine.evaluation_environmental_contracts import (
    ExplorationEnvironmentalSpellCastPayload as ExplorationEnvironmentalSpellCastPayload,
)
from dnd5e_engine.evaluation_environmental_contracts import (
    WorldEnvironmentalEffectCreate,
)
from dnd5e_engine.evaluation_resources import HitDieSpend
from dnd5e_engine.evaluation_rng import RNGContext, RNGTransition
from dnd5e_engine.evaluation_ruleset import RulesetBinding
from dnd5e_engine.evaluation_stages import EventStageFacts
from dnd5e_engine.evaluation_state import CombatSnapshot, NonCombatSnapshot, StateSnapshot
from dnd5e_engine.events import (
    ALL_COMBAT_EVENT_TYPES,
    Ability,
    AdvantageSource,
    CheckContext,
    CombatEvent,
    RequiredSense,
)
from dnd5e_engine.intents import PlayerIntent
from dnd5e_engine.rules.skills import Skill
from dnd5e_engine.types.checks import CheckRequest, GrantedDie

SCHEMA_VERSION = "engine-evaluation/30"
ENVIRONMENTAL_SPELL_SCHEMA_VERSION = "engine-evaluation/31"
CHARGED_ITEM_SCHEMA_VERSION = "engine-evaluation/32"
MULTI_SPELL_SCHEMA_VERSION = "engine-evaluation/33"
MAX_EXPLORATION_SPELL_TARGETS = 10
EvaluationVersion = Literal[
    "engine-evaluation/24",
    "engine-evaluation/25",
    "engine-evaluation/26",
    "engine-evaluation/27",
    "engine-evaluation/28",
    "engine-evaluation/29",
    "engine-evaluation/30",
    "engine-evaluation/31",
    "engine-evaluation/32",
    "engine-evaluation/33",
]


class ItemUsePayload(EvaluationModel):
    """Select an owned inventory instance, activity and target for combat item evaluation."""

    kind: Literal["combat.item"]
    instance_id: Annotated[str, Field(min_length=1)]
    activity_id: Annotated[str, Field(min_length=1)]
    target_id: Annotated[str, Field(min_length=1)]


class ExplorationItemUsePayload(EvaluationModel):
    """Select an owned inventory instance, activity and target for exploration item use."""

    kind: Literal["world.use_item"]
    instance_id: Annotated[str, Field(min_length=1)]
    activity_id: Annotated[str, Field(min_length=1)]
    target_id: Annotated[str, Field(min_length=1)]


class ExplorationSpellCastPayload(EvaluationModel):
    """Select one prepared spell source, slot and target for exploration casting."""

    kind: Literal["world.cast_spell"]
    spell_id: Annotated[str, Field(min_length=1)]
    spell_source_id: Annotated[str, Field(min_length=1)]
    slot_level: Annotated[int, Field(ge=1, le=9)]
    target_id: Annotated[str, Field(min_length=1)]


class ExplorationSpellPositions(EvaluationModel):
    """Carry fixed caster and target cells and facts verified by the host.

    Willingness applies to the named creature in this cast only. An omitted
    willingness fact leaves another creature's willingness unknown.
    """

    source_cell: Annotated[str, Field(min_length=1)]
    target_cell: Annotated[str, Field(min_length=1)]
    target_item: EnvironmentalItemFacts | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    willing_target_id: Annotated[str, Field(min_length=1)] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class ExplorationMultiSpellCastPayload(EvaluationModel):
    """Select a slotted exploration spell and a bounded list of distinct creatures."""

    kind: Literal["world.cast_spell_many"]
    spell_id: Annotated[str, Field(min_length=1)]
    spell_source_id: Annotated[str, Field(min_length=1)]
    slot_level: Annotated[int, Field(ge=1, le=9)]
    target_ids: Annotated[
        tuple[Annotated[str, Field(min_length=1)], ...],
        Field(min_length=1, max_length=MAX_EXPLORATION_SPELL_TARGETS),
    ]

    @field_validator("target_ids")
    @classmethod
    def unique_targets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject duplicated creatures instead of creating repeated effects."""
        if len(set(value)) != len(value):
            raise ValueError("spell targets must be distinct")
        return value


class ExplorationMultiSpellTarget(EvaluationModel):
    """Bind one selected creature to its position and verified willingness fact."""

    target_id: Annotated[str, Field(min_length=1)]
    cell_id: Annotated[str, Field(min_length=1)]
    willing: bool


class ExplorationMultiSpellPositions(EvaluationModel):
    """Carry Host-verified source and target facts outside the public cast payload."""

    source_cell: Annotated[str, Field(min_length=1)]
    targets: Annotated[
        tuple[ExplorationMultiSpellTarget, ...],
        Field(min_length=1, max_length=MAX_EXPLORATION_SPELL_TARGETS),
    ]


class CheckPayload(CheckRequest):
    """Every adjudication fact is explicit, including intentional nulls."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, revalidate_instances="always"
    )

    skill: Skill | None = Field(...)
    tool: str | None = Field(...)
    dc: Annotated[int, Field(ge=0)] | None = Field(...)
    target_id: str | None = Field(...)
    context: CheckContext = Field(...)
    required_sense: RequiredSense = Field(...)
    social_interaction: bool = Field(...)
    advantage: tuple[AdvantageSource, ...] = Field(...)
    disadvantage: tuple[AdvantageSource, ...] = Field(...)
    redeem_granted_die: GrantedDie | None = Field(...)


class SavePayload(EvaluationModel):
    """Actor makes its own explicitly adjudicated save; no system source ABI."""

    kind: Literal["rules.save"]
    target_id: Annotated[str, Field(min_length=1)]
    ability: Ability
    dc: Annotated[int, Field(ge=0)]
    advantage: tuple[AdvantageSource, ...]
    disadvantage: tuple[AdvantageSource, ...]
    is_magical: bool


class RestPayload(EvaluationModel):
    """Describe the requested rest and explicit owned hit-dice spends."""

    kind: Literal["rules.rest"]
    target_id: Annotated[str, Field(min_length=1)]
    rest_type: Literal["short", "long"]
    hit_dice: tuple[HitDieSpend, ...]

    @model_validator(mode="after")
    def unique_spend(self) -> Self:
        """Reject duplicate resource spends within the request."""
        if len({s.pool_id for s in self.hit_dice}) != len(self.hit_dice):
            raise ValueError("a rest must name each spent Hit Dice pool once")
        return self


class CombatClosePayload(EvaluationModel):
    """No caller-supplied end reason, reward or claimed death."""

    kind: Literal["combat.close"]


class CombatIntentPayload(PlayerIntent):
    """Reuse the legacy typed intent vocabulary; admission is evaluator-specific."""

    model_config = ConfigDict(extra="forbid", strict=True, revalidate_instances="always")

    stat_block_action_id: Annotated[str, Field(strict=True, min_length=1)] | None = None
    spell_source_id: Annotated[str, Field(strict=True, min_length=1)] | None = None
    weapon_instance_id: Annotated[str, Field(min_length=1)] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class RuleEvaluationRequest(EvaluationModel):
    """Bind a typed operation to fixed mechanical inputs, rules identity and explicit RNG."""

    schema_version: EvaluationVersion
    session_id: Annotated[str, Field(min_length=1)]
    command_id: Annotated[str, Field(min_length=1)]
    operation_kind: Literal[
        "combat.intent",
        "rules.check",
        "combat.close",
        "combat.item",
        "rules.save",
        "rules.rest",
        "world.use_item",
        "world.cast_spell",
        "world.cast_spell_many",
    ]
    actor_id: Annotated[str, Field(min_length=1)]
    payload: (
        CombatIntentPayload
        | CheckPayload
        | CombatClosePayload
        | ItemUsePayload
        | ExplorationItemUsePayload
        | ExplorationSpellCastPayload
        | ExplorationMultiSpellCastPayload
        | ExplorationEnvironmentalSpellCastPayload
        | SavePayload
        | RestPayload
    )
    state_snapshot: StateSnapshot
    ruleset_binding: RulesetBinding
    rng_context: RNGContext
    spell_positions: ExplorationSpellPositions | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    multi_spell_positions: ExplorationMultiSpellPositions | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @field_validator("payload", mode="before")
    @classmethod
    def strict_check_payload(cls, value: object, info: ValidationInfo) -> object:
        """Restore and validate the operation's closed check payload."""
        if info.mode == "json" and info.data.get("operation_kind") == "world.cast_spell_many":
            return ExplorationMultiSpellCastPayload.model_validate_json(to_json(value))
        if info.mode == "json" and info.data.get("operation_kind") == "combat.intent":
            return CombatIntentPayload.model_validate_json(to_json(value))
        if info.data.get("operation_kind") == "rules.rest":
            if info.mode == "json":
                return RestPayload.model_validate_json(to_json(value))
            return RestPayload.model_validate(value)
        if info.data.get("operation_kind") == "rules.save":
            if info.mode == "json":
                return SavePayload.model_validate_json(to_json(value))
            return SavePayload.model_validate(value)
        if info.data.get("operation_kind") == "rules.check":
            # Preserve strict adjudication types before validating the payload union.
            if info.mode == "json":
                return CheckPayload.model_validate_json(to_json(value))
            return CheckPayload.model_validate(value)
        return value

    @model_validator(mode="after")
    def operation_payload(self) -> Self:
        """Require the payload type and shape matching the operation tag."""
        if self.session_id != self.state_snapshot.session_id:
            raise ValueError("request and snapshot session identities differ")
        version_pairs = {
            "engine-evaluation/24": "engine-snapshot/17",
            "engine-evaluation/25": "engine-snapshot/18",
            "engine-evaluation/26": "engine-snapshot/19",
            "engine-evaluation/27": "engine-snapshot/20",
            "engine-evaluation/28": "engine-snapshot/20",
            "engine-evaluation/29": "engine-snapshot/20",
            "engine-evaluation/30": "engine-snapshot/20",
            ENVIRONMENTAL_SPELL_SCHEMA_VERSION: "engine-snapshot/20",
            CHARGED_ITEM_SCHEMA_VERSION: "engine-snapshot/20",
            MULTI_SPELL_SCHEMA_VERSION: "engine-snapshot/20",
        }
        if self.state_snapshot.snapshot_schema_version != version_pairs[self.schema_version]:
            raise ValueError("evaluation and snapshot versions differ")
        if self._validate_versioned_operation():
            return self
        if self.spell_positions is not None and self.spell_positions.target_item is not None:
            raise ValueError("item facts require evaluation/31 and a spatial world spell")
        if self.operation_kind == "rules.rest":
            if not isinstance(self.payload, RestPayload) or self.payload.target_id != self.actor_id:
                raise ValueError("rules.rest requires the actual resting actor")
            return self
        if self.operation_kind == "rules.save":
            if not isinstance(self.payload, SavePayload) or self.payload.target_id != self.actor_id:
                raise ValueError("rules.save requires the real saving actor as target")
            return self
        if self.operation_kind == "combat.item":
            if not isinstance(self.payload, ItemUsePayload) or not isinstance(
                self.state_snapshot, CombatSnapshot
            ):
                raise ValueError("combat.item requires a typed item payload and combat snapshot")
            return self
        if self.operation_kind == "world.use_item":
            if (
                self.schema_version not in {"engine-evaluation/28", "engine-evaluation/30"}
                or not isinstance(self.payload, ExplorationItemUsePayload)
                or not isinstance(self.state_snapshot, NonCombatSnapshot)
            ):
                raise ValueError(
                    "world.use_item requires evaluation/28 or /30 and a noncombat item request"
                )
            return self
        if self.operation_kind == "world.cast_spell":
            self._validate_creature_spell()
            return self
        if self.operation_kind == "combat.intent":
            if not isinstance(self.payload, CombatIntentPayload):
                raise ValueError("combat.intent requires a typed combat payload")
            if not isinstance(self.state_snapshot, CombatSnapshot):
                raise ValueError("combat.intent requires a combat snapshot")
            if self.payload.source_id is not None and self.payload.source_id != self.actor_id:
                raise ValueError("payload source_id differs from actor_id")
        elif self.operation_kind == "combat.close":
            if not isinstance(self.payload, CombatClosePayload) or not isinstance(
                self.state_snapshot, CombatSnapshot
            ):
                raise ValueError("combat.close requires a closure payload and combat snapshot")
        elif not isinstance(self.payload, CheckPayload) or self.payload.actor_id != self.actor_id:
            raise ValueError("rules.check requires a matching actor and explicit adjudication")
        return self

    def _validate_versioned_operation(self) -> bool:
        """Validate specialist evaluation schemas and reject misplaced spatial facts."""
        if self.schema_version == MULTI_SPELL_SCHEMA_VERSION:
            self._validate_multi_spell()
            return True
        if self.multi_spell_positions is not None or self.operation_kind == "world.cast_spell_many":
            raise ValueError("multi-target spell facts require evaluation/33")
        if self.schema_version == CHARGED_ITEM_SCHEMA_VERSION:
            self._validate_charged_item()
            return True
        if self.spell_positions is not None and self.operation_kind != "world.cast_spell":
            raise ValueError("spell positions require world.cast_spell")
        if self.schema_version == ENVIRONMENTAL_SPELL_SCHEMA_VERSION:
            self._validate_environmental_spell()
            return True
        return False

    def _validate_multi_spell(self) -> None:
        """Match ordered selections to independent trusted target facts."""
        positions = self.multi_spell_positions
        if (
            self.operation_kind != "world.cast_spell_many"
            or not isinstance(self.payload, ExplorationMultiSpellCastPayload)
            or not isinstance(self.state_snapshot, NonCombatSnapshot)
            or self.state_snapshot.combat_setup is not None
            or self.spell_positions is not None
            or positions is None
            or tuple(target.target_id for target in positions.targets) != self.payload.target_ids
            or any(
                target.target_id == self.actor_id
                and (target.cell_id != positions.source_cell or not target.willing)
                for target in positions.targets
            )
        ):
            raise ValueError("evaluation/33 requires matching exploration spell target facts")

    def _validate_charged_item(self) -> None:
        """Require an exploration charged cast with only creature position facts."""
        if (
            self.operation_kind != "world.use_item"
            or not isinstance(self.payload, ExplorationItemUsePayload)
            or not isinstance(self.state_snapshot, NonCombatSnapshot)
            or self.state_snapshot.combat_setup is not None
        ):
            raise ValueError("evaluation/32 requires a noncombat item cast without pending combat")
        positions = self.spell_positions
        if positions is not None and (
            positions.target_item is not None or positions.willing_target_id is not None
        ):
            raise ValueError("charged item casts accept only creature positions")

    def _validate_creature_spell(self) -> None:
        """Require a noncombat creature cast and bind any willingness to its target."""
        if (
            self.schema_version not in {"engine-evaluation/29", "engine-evaluation/30"}
            or not isinstance(self.payload, ExplorationSpellCastPayload)
            or not isinstance(self.state_snapshot, NonCombatSnapshot)
            or self.state_snapshot.combat_setup is not None
        ):
            raise ValueError(
                "world.cast_spell requires evaluation/29 or /30 and a noncombat spell request"
            )
        positions = self.spell_positions
        if (
            positions is not None
            and positions.willing_target_id is not None
            and positions.willing_target_id != self.payload.target_id
        ):
            raise ValueError("verified willingness must match the spell's target_id")

    def _validate_environmental_spell(self) -> None:
        """Bind the selected item or point identity to its fixed spatial facts."""
        positions = self.spell_positions
        if (
            self.operation_kind != "world.cast_spell"
            or not isinstance(self.payload, ExplorationEnvironmentalSpellCastPayload)
            or not isinstance(self.state_snapshot, NonCombatSnapshot)
            or self.state_snapshot.combat_setup is not None
            or positions is None
        ):
            raise ValueError("evaluation/31 requires a spatial world spell without pending combat")
        if positions.willing_target_id is not None:
            raise ValueError("verified willingness requires a creature spell target")
        target = self.payload.target
        if isinstance(target, EnvironmentalPointTarget):
            if positions.target_item is not None or positions.target_cell != target.cell_id:
                raise ValueError("fixed point must match the supplied target cell")
        elif isinstance(target, EnvironmentalItemTarget) and (
            positions.target_item is None or positions.target_item.item_id != target.item_id
        ):
            raise ValueError("item target must match the supplied fixed item facts")


class ReadVersion(EvaluationModel):
    """Identify the fixed world version consumed by evaluation."""

    kind: Literal["world", "actor", "combat", "scene", "effects", "history"]
    ref: Annotated[str, Field(min_length=1)]
    version: Annotated[int, Field(ge=0)]


class RuleError(EvaluationModel):
    """Carry a structured reason for a rejected or unsupported rules operation."""

    code: Annotated[str, Field(min_length=1)]
    reason: Annotated[str, Field(min_length=1)]


class PreflightChoice(EvaluationModel):
    """Describe a required explicit choice before mechanical resolution can proceed."""

    kind: Literal["attack.weapon"]
    actor_id: Annotated[str, Field(min_length=1)]
    allowed_weapon_ids: Annotated[tuple[str, ...], Field(min_length=1)] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    allowed_weapon_instance_ids: Annotated[tuple[str, ...], Field(min_length=1)] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def unique_options(self) -> Self:
        """Reject duplicate option identities."""
        if (self.allowed_weapon_ids is None) == (self.allowed_weapon_instance_ids is None):
            raise ValueError("supply exactly one weapon identity vocabulary")
        options = self.allowed_weapon_ids or self.allowed_weapon_instance_ids or ()
        if any(not option for option in options) or len(set(options)) != len(options):
            raise ValueError("choice options must be nonempty and unique")
        return self


ProposedEvents = tuple[CombatEvent, ...]


class CheckAdjudicationChoice(EvaluationModel):
    """Request missing check adjudication fields without drawing RNG."""

    kind: Literal["check.adjudication"]
    actor_id: Annotated[str, Field(min_length=1)]
    required_fields: tuple[Literal["dc"]]


class SpellWillingnessChoice(EvaluationModel):
    """Identify the caster and creature whose willingness the host must establish."""

    kind: Literal["spell.target_willingness"]
    actor_id: Annotated[str, Field(min_length=1)]
    target_id: Annotated[str, Field(min_length=1)]


class MultiSpellWillingnessChoice(EvaluationModel):
    """Name every selected creature whose willingness is still unknown."""

    kind: Literal["spell.targets_willingness"]
    actor_id: Annotated[str, Field(min_length=1)]
    target_ids: Annotated[
        tuple[Annotated[str, Field(min_length=1)], ...],
        Field(min_length=1, max_length=MAX_EXPLORATION_SPELL_TARGETS),
    ]


class RuleEvaluationResult(EvaluationModel):
    """Return uncommitted Delta/events/RNG and verify their association with the request."""

    schema_version: EvaluationVersion
    session_id: Annotated[str, Field(min_length=1)]
    command_id: Annotated[str, Field(min_length=1)]
    input_world_version: Annotated[int, Field(ge=0)]
    input_ruleset_binding: RulesetBinding
    status: Literal["accepted", "rejected", "needs_choice", "unsupported"]
    read_set: tuple[ReadVersion, ...]
    state_delta: StateDelta | None
    proposed_events: ProposedEvents
    stage_facts: EventStageFacts | None
    rng_transition: RNGTransition | None
    choice: (
        PreflightChoice
        | CheckAdjudicationChoice
        | SpellWillingnessChoice
        | MultiSpellWillingnessChoice
        | None
    )
    error: RuleError | None

    @field_validator("proposed_events", mode="before")
    @classmethod
    def closed_event_fields(cls, value: object) -> object:
        """Reject event payload fields outside the declared event model."""
        if isinstance(value, (list, tuple)):
            kinds = {
                model.model_fields["type"].default: set(model.model_fields)
                for model in ALL_COMBAT_EVENT_TYPES
            }
            for event in value:
                if isinstance(event, dict) and set(event) - kinds.get(event.get("type"), set()):
                    raise ValueError("unknown proposed event field or authoritative event identity")
            return tuple(value)
        return value

    @model_validator(mode="after")
    def status_fields(self) -> Self:
        """Require result fields to agree with accepted, choice or refusal status."""
        if self.status == "accepted":
            if self.state_delta is None or self.rng_transition is None or self.stage_facts is None:
                raise ValueError("accepted requires delta, RNG transition and stage coverage")
            if self.stage_facts.coverage == "complete" and len(self.proposed_events) != len(
                self.stage_facts.event_references
            ):
                raise ValueError("stage references differ from actual proposed_events")
            if self.choice is not None or self.error is not None:
                raise ValueError("accepted forbids choice/error")
            if self.state_delta.expected_world_version != self.input_world_version:
                raise ValueError("delta and result world versions differ")
            creations = sum(
                isinstance(op, CombatEffectCreate) for op in self.state_delta.operations
            )
            if creations and (self.schema_version != "engine-evaluation/30" or creations != 1):
                raise ValueError("one combat effect creation requires evaluation/30")
            environmental = sum(
                isinstance(op, WorldEnvironmentalEffectCreate) for op in self.state_delta.operations
            )
            if environmental != int(self.schema_version == ENVIRONMENTAL_SPELL_SCHEMA_VERSION):
                raise ValueError("evaluation/31 requires exactly one environmental effect creation")
        else:
            if (
                self.state_delta is not None
                or self.proposed_events
                or self.rng_transition is not None
                or self.stage_facts is not None
            ):
                raise ValueError("non-accepted result cannot carry mechanical results")
            if self.status == "needs_choice":
                if self.choice is None or self.error is not None:
                    raise ValueError("needs_choice requires choice and forbids error")
            elif self.error is None or self.choice is not None:
                raise ValueError("rejected/unsupported require error and forbid choice")
        if len({(read.kind, read.ref) for read in self.read_set}) != len(self.read_set):
            raise ValueError("duplicate read-set entry")
        return self

    def verify_request(self, request: RuleEvaluationRequest) -> None:
        """Validate result association and invariants against its original fixed request."""
        if (
            self.schema_version,
            self.session_id,
            self.command_id,
            self.input_world_version,
            self.input_ruleset_binding,
        ) != (
            request.schema_version,
            request.session_id,
            request.command_id,
            request.state_snapshot.world_version,
            request.ruleset_binding,
        ):
            raise ValueError("result does not match request identity/version/binding")
        if isinstance(self.choice, SpellWillingnessChoice):
            positions = request.spell_positions
            if (
                request.operation_kind != "world.cast_spell"
                or not isinstance(request.payload, ExplorationSpellCastPayload)
                or self.choice.actor_id != request.actor_id
                or self.choice.target_id != request.payload.target_id
                or self.choice.target_id == request.actor_id
                or positions is None
                or positions.willing_target_id is not None
            ):
                raise ValueError("spell willingness choice must match an unverified cast target")
        if request.schema_version == MULTI_SPELL_SCHEMA_VERSION:
            from dnd5e_engine.evaluation_multi_spells import verify_multi_spell_cast

            verify_multi_spell_cast(self, request)
        elif isinstance(self.choice, MultiSpellWillingnessChoice):
            raise ValueError("multi-target willingness choice requires evaluation/33")
        if self.state_delta is not None:
            from dnd5e_engine.evaluation_haste_delta import (
                EffectFollowUpCreate,
                HasteGraphCreate,
                verify_haste_creation,
            )
            from dnd5e_engine.evaluation_speed_effects import verify_speed_effect_creation

            if request.schema_version == ENVIRONMENTAL_SPELL_SCHEMA_VERSION:
                from dnd5e_engine.evaluation_environmental_spells import verify_environmental_cast

                verify_environmental_cast(self, request)

            if request.schema_version == CHARGED_ITEM_SCHEMA_VERSION:
                from dnd5e_engine.evaluation_charged_item import verify_charged_item_cast

                verify_charged_item_cast(self, request)

            for operation in self.state_delta.operations:
                if isinstance(operation, (HasteGraphCreate, EffectFollowUpCreate)):
                    if request.schema_version != "engine-evaluation/30":
                        raise ValueError("new combat effect operations require evaluation/30")
                    if isinstance(operation, HasteGraphCreate):
                        verify_haste_creation(operation, request, self.proposed_events)
                elif isinstance(operation, InventorySpendCharge):
                    if request.schema_version != CHARGED_ITEM_SCHEMA_VERSION:
                        raise ValueError("inventory.spend_charge requires evaluation/32")
                elif isinstance(operation, CombatEffectCreate):
                    verify_speed_effect_creation(operation, request, self.proposed_events)
        if self.rng_transition is not None:
            self.rng_transition.verify_input(request.rng_context)
        if self.stage_facts is not None:
            from dnd5e_engine.evaluation_stage_validation import verify_stage_result

            verify_stage_result(self.stage_facts, request, self.proposed_events, self.state_delta)
