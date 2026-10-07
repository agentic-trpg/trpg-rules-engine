"""Canonical Feature model: a class/subclass/species feature document."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    BaseModel,
    Field,
    NonNegativeInt,
    PositiveInt,
    SerializerFunctionWrapHandler,
    model_serializer,
)

from dnd5e_srd_data.schema.advancement import AdvancementEntry
from dnd5e_srd_data.schema.common import Activity, PassiveEffect, Provenance, ReviewState

FeatureType = Literal["class_feature", "subclass_feature", "species_trait"]
FeatureRuntimeOperation = Literal[
    "native", "flurry", "remove_poison", "disengage", "dodge_disengage", "dash_heal"
]
FeatureTargetRule = Literal["other_visible_creature", "perceives_caster"]
AttackRiderTrigger = Literal["final_hit", "sneak_attack_damage", "flurry_hit", "reckless_hit"]
AttackRiderQualification = Literal[
    "any_attack",
    "any_weapon",
    "finesse_or_ranged",
    "monk_weapon_or_unarmed",
    "flurry_unarmed",
    "strength",
    "pact_weapon",
    "spell_attack",
    "unarmed",
    "melee_weapon_or_unarmed",
]
AttackRiderPhase = Literal["final_hit_before_damage", "after_damage", "damage_preparation"]
RiderEffectExpiry = Literal[
    "none", "source_next_turn_start", "target_next_turn_start", "target_next_turn_end"
]


class RiderEffectSpec(BaseModel, frozen=True):
    effect_id: str
    outcome: Literal["always", "failure", "success"] = "always"
    expiry: RiderEffectExpiry = "none"


class RiderForcedMovement(BaseModel, frozen=True):
    max_distance_ft: NonNegativeInt
    requires_distance_choice: bool = True
    on_save: Literal["always", "failure", "success"] = "failure"


class AttackRiderSemantics(BaseModel, frozen=True):
    """Reviewed attack binding; numeric costs and saves stay on its Activity.

    Exact ingestion identities supply these contracts. ``deferred_reason``
    explains a refusal and never authorizes partial execution. Feature-level
    context records foundations without a standalone activity, while concrete
    executable riders live in ``Feature.attack_riders`` keyed by activity id.
    """

    trigger: AttackRiderTrigger
    qualification: AttackRiderQualification
    phase: AttackRiderPhase
    target_role: Literal["attack_target", "attacker"] = "attack_target"
    choice_group: str | None = None
    once_per_turn: bool = False
    sneak_dice_cost: NonNegativeInt = 0
    inherit_damage_type: bool = False
    effects: tuple[RiderEffectSpec, ...] = ()
    forced_movement: RiderForcedMovement | None = None
    automatic: bool = False
    deferred_reason: str | None = None
    deferred_options: dict[str, str] = Field(default_factory=dict)
    inventory_role: Literal["rider", "foundation", "producer", "passive", "defensive"] = "rider"
    related_activity_ids: tuple[str, ...] = ()


# SRD 5.2 rest / recharge periods a limited-use feature recovers on. Foundry's
# ``uses.recovery[].period`` vocabulary observed across the SRD feature corpus;
# typed (closed set) per the typed-semantics rule.
RecoveryPeriod = Literal["sr", "lr", "day", "dawn", "dusk", "recharge", "initiative"]
# ``uses.recovery[].type`` — how a recovery entry refills the pool.
RecoveryType = Literal["recoverAll", "formula"]


class RecoveryRule(BaseModel):
    """One ``uses.recovery[]`` entry: on ``period``, refill by ``type``.

    ``recoverAll`` restores the pool to ``max``; ``formula`` regains
    ``formula``-many uses (e.g. Second Wind's Short-Rest ``formula: "1"``).
    """

    period: RecoveryPeriod = "lr"
    type: RecoveryType = "recoverAll"
    formula: str = ""


class FeatureUses(BaseModel):
    """A feature's top-level limited-use cap (Foundry ``shared/uses-field.mjs``).

    ``max`` is Foundry's raw expression (a bare integer like ``"1"`` or a roll-data
    token like ``"@scale.fighter.second-wind"`` / ``"@prof"``); the engine parses a
    literal integer where it can and otherwise applies a conservative floor of 1.
    ``spent`` is the seed spend (always 0 in canonical). ``recovery`` lists the rests
    that recharge it. Distinct from the activity-level ``UsesBlock`` on
    ``common.py`` — this is the feature document's OWN cap (Second Wind's rest-
    recharged use pool), mirroring how ``Feature.advancement`` mirrors the class-level
    advancement field.
    """

    max: str = ""
    spent: int = 0
    recovery: list[RecoveryRule] = Field(default_factory=list)


class Feature(BaseModel):
    slug: str
    name: str
    description: str = ""
    feature_type: FeatureType
    foundry_id: str = ""
    source_slug: str = ""
    activities: list[Activity] = Field(default_factory=list)
    passive_effects: list[PassiveEffect] = Field(default_factory=list)
    # A feature's OWN ScaleValue advancement table (e.g. Channel Divinity's
    # Divine Spark die count, keyed by Cleric level) — distinct from the
    # granting class/subclass/species' advancement. Mirrors ``Class`` /
    # ``Subclass`` / ``Species``'s existing ``advancement`` field (same
    # ``AdvancementEntry`` shape); resolves feature-owned ``@scale.<feature-
    # slug>.<key>`` tokens (``activities/scale.py::build_scale_values``).
    advancement: list[AdvancementEntry] = Field(default_factory=list)
    # A feature's OWN limited-use cap (Second Wind's rest-recharged pool), carried
    # from Foundry's top-level ``system.uses``. ``None`` when the source feature has
    # no meaningful cap (no ``max`` and no ``recovery``). Mirrors the ``advancement``
    # precedent: additive, populated by the translator, byte-stable regen.
    uses: FeatureUses | None = None
    # Audited activity-id bindings for semantics absent from Foundry's utility
    # payload. Ingestion owns the exact identities; runtime consumes this type.
    runtime_operations: dict[str, FeatureRuntimeOperation] = Field(default_factory=dict)
    target_rules: dict[str, FeatureTargetRule] = Field(default_factory=dict)
    attack_riders: dict[str, AttackRiderSemantics] = Field(default_factory=dict)
    attack_rider_choice_limits: dict[str, PositiveInt] = Field(default_factory=dict)
    attack_rider_context: AttackRiderSemantics | None = None
    provenance: Provenance
    review: ReviewState = Field(default_factory=ReviewState)

    entry_kind: Literal["feature"] = "feature"

    @model_serializer(mode="wrap")
    def _serialize_feature(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if not self.runtime_operations:
            data.pop("runtime_operations", None)
        if not self.target_rules:
            data.pop("target_rules", None)
        if not self.attack_riders:
            data.pop("attack_riders", None)
        if not self.attack_rider_choice_limits:
            data.pop("attack_rider_choice_limits", None)
        if self.attack_rider_context is None:
            data.pop("attack_rider_context", None)
        return data
