"""Typed, draw-free admission; delivery and the Activity Resolver still execute.

The packaged review is evidence about canonical semantics, not executable spell
code. Unknown sources/activities and missing mandatory mechanisms fail closed.
Bounded support never means the entire SRD spell has been implemented.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from functools import cache
from importlib.resources import files
from types import MappingProxyType
from typing import Literal, TypedDict

from dnd5e_srd_data.schema.common import Activity, ActivityKind
from dnd5e_srd_data.schema.spell import Spell
from pydantic import BaseModel, ConfigDict

ExecutionClass = Literal["executable", "bounded", "host_narrative", "deferred"]
ActivityRole = Literal["cast", "alternative", "delayed", "persistent"]
MechanismCode = Literal[
    "area_lifecycle",
    "area_terrain",
    "attack_disadvantage",
    "attack_replacement",
    "authoritative_hit_binding",
    "bonus_action_dash",
    "condition_removal",
    "conditional_attack_damage",
    "conditional_damage",
    "conditional_scheduler",
    "controlled_messenger",
    "divided_healing",
    "effect_choice",
    "falling",
    "host_and_mechanical_modes",
    "illusion_entity",
    "item_enchantment",
    "light_and_obscurement",
    "multi_origin_geometry",
    "next_attack_advantage",
    "next_attack_disadvantage",
    "opportunity_attack_suppression",
    "planar_portal",
    "recurring_temporary_hp",
    "resurrection",
    "sensory_illusion",
    "short_rest_transaction",
    "spell_specific_action_rules",
    "spell_suppression",
    "summon_entity",
    "teleportation",
    "transformation",
    "typed_creature_defense",
    "underwater_breathing",
    "wall_geometry",
    "world_object_state",
    "reviewed_execution_contract",
    "validated_conjuration_inputs",
    "formula_inputs",
    "maximum_hp_reduction",
    "sleep_transition",
    "condition_specific_defense",
    "ongoing_spell_activation",
]


class ExecutionFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    code: Literal[
        "unreviewed_spell",
        "unreviewed_activity",
        "missing_mechanism",
        "missing_effect",
        "missing_carrier",
        "empty_payload",
        "manual_timing",
        "invalid_formula",
    ]
    spell_id: str
    activity_id: str | None = None
    mechanisms: tuple[MechanismCode, ...] = ()


class ActivityReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    activity_id: str
    kind: ActivityKind
    role: ActivityRole
    required_mechanisms: tuple[MechanismCode, ...] = ()
    # Explicit setup/host work is allowed to emit no mechanical events.
    host_operation: bool = False


class SpellReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    slug: str
    uuid: str
    canonical_sha256: str
    source_url: str
    classification: ExecutionClass
    required_mechanisms: tuple[MechanismCode, ...] = ()
    missing_details: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    requires_direct_carrier: bool = False
    activities: tuple[ActivityReview, ...]


class _ActivityIdentity(TypedDict):
    spell_id: str
    activity_id: str


@cache
def spell_reviews() -> Mapping[str, SpellReview]:
    """Load immutable review records once, without consulting live state."""
    raw = json.loads(
        files("dnd5e_engine").joinpath("spell_execution_reviews.json").read_text("utf-8")
    )
    reviews = [SpellReview.model_validate(row) for row in raw]
    if len({row.uuid for row in reviews}) != len(reviews):
        raise ValueError("duplicate spell capability identity")
    return MappingProxyType({row.uuid: row for row in reviews})


def spell_review(spell: Spell) -> SpellReview | None:
    review = spell_reviews().get(spell.foundry_uuid)
    # A copied identity cannot turn a different source into an admitted spell.
    return review if review is not None and review.slug == spell.slug else None


def admission_failure(
    spell: Spell,
    activities: Sequence[Activity],
    *,
    direct_carrier: bool = False,
) -> ExecutionFailure | None:
    """Inspect the selected payload only; no actor, event sink, state or RNG.

    Critical same-cast dependencies belong to the spell review, so choosing a
    damage activity cannot bypass an unimplemented summon/teleport/sequence.
    Alternative activities are not required merely because they exist.
    Existing C21 direct inputs retain their separate pre-payment legality gate;
    delegated/monster paths cannot claim that unavailable carrier.
    """
    review = spell_review(spell)
    if review is None:
        return ExecutionFailure(code="unreviewed_spell", spell_id=spell.slug)
    if review.required_mechanisms or review.classification == "deferred":
        return ExecutionFailure(
            code="missing_mechanism",
            spell_id=spell.slug,
            mechanisms=review.required_mechanisms or ("reviewed_execution_contract",),
        )
    if review.requires_direct_carrier and not direct_carrier:
        return ExecutionFailure(
            code="missing_carrier",
            spell_id=spell.slug,
            mechanisms=("validated_conjuration_inputs",),
        )
    by_id = {a.activity_id: a for a in review.activities}
    present_ids = {a.id for a in spell.activities}
    for required in review.activities:
        if required.role != "alternative" and required.activity_id not in present_ids:
            return ExecutionFailure(
                code="unreviewed_activity", spell_id=spell.slug, activity_id=required.activity_id
            )
    effects = {e.id for e in spell.passive_effects}
    if not activities:
        return ExecutionFailure(code="empty_payload", spell_id=spell.slug)
    for activity in activities:
        identity: _ActivityIdentity = {"spell_id": spell.slug, "activity_id": activity.id}
        if activity.id not in by_id or activity.kind != by_id[activity.id].kind:
            return ExecutionFailure(code="unreviewed_activity", **identity)
        if by_id[activity.id].required_mechanisms:
            return ExecutionFailure(
                code="missing_mechanism",
                mechanisms=by_id[activity.id].required_mechanisms,
                **identity,
            )
        if activity.timing.trigger == "manual":
            return ExecutionFailure(
                code="manual_timing", mechanisms=("conditional_scheduler",), **identity
            )
        refs = getattr(activity, "effects", ())
        if any(ref.id not in effects for ref in refs):
            return ExecutionFailure(code="missing_effect", **identity)
        if activity.kind in ("summon", "enchant", "transform") and not (
            review.requires_direct_carrier and direct_carrier
        ):
            return ExecutionFailure(code="missing_carrier", **identity)
        if (
            activity.kind == "utility"
            and not refs
            and activity.persistent_area is None
            and not by_id[activity.id].host_operation
        ):
            return ExecutionFailure(code="empty_payload", **identity)
        if (
            activity.kind in ("attack", "damage", "save")
            and not activity.damage.parts
            and not refs
            and activity.reaction is None
            and activity.forced_movement is None
            and not review.requires_direct_carrier
            and not (activity.kind == "save" and by_id[activity.id].host_operation)
        ):
            return ExecutionFailure(code="empty_payload", **identity)
    return None
