"""Explicit migration of complete /16 snapshots; no implicit request fallback."""

import json

from dnd5e_srd_data.loader import AssetLoader
from pydantic import BaseModel, TypeAdapter

from dnd5e_engine.evaluation_legacy_history import validate_legacy_history
from dnd5e_engine.evaluation_ruleset import RulesetBinding, ruleset_binding
from dnd5e_engine.evaluation_state import CombatSnapshot, StateSnapshot
from dnd5e_engine.events import CombatEvent


def _validate_references(value: object, actor_ids: set[str]) -> None:
    """Known actor-reference fields, including typed nested event evidence.

    Spell/activity/effect/source identities are deliberately not actor IDs.
    Departed roster references cannot be proven from a current-only closure and
    require a separately reviewed migration rather than silent acceptance.
    """
    actor_fields = {
        "actor_id",
        "target_id",
        "attacker_id",
        "source_actor_id",
        "killer_id",
        "triggering_actor_id",
        "affected_target_id",
        "monster_id",
        "entity_id",
        "origin_caster_id",
        "after_entity_id",
        "holder_id",
    }
    if isinstance(value, BaseModel):
        for name in type(value).model_fields:
            child = getattr(value, name)
            if name in actor_fields and child is not None and child not in actor_ids:
                raise ValueError("Legacy history references an actor outside the supplied closure")
            if name in {"affected_ids", "excluded_ids"} and not set(child) <= actor_ids:
                raise ValueError("Legacy history references actors outside the supplied closure")
            _validate_references(child, actor_ids)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _validate_references(child, actor_ids)


def migrate_snapshot_16(
    snapshot_json: str, *, source_binding: RulesetBinding, loader: AssetLoader
) -> StateSnapshot:
    """Convert only the known complete predecessor with the same effective data.

    The owner supplies its original binding and full serialized snapshot at a
    completed boundary. Old envelopes are still rejected by normal evaluation.
    Earlier formats lack required facts and are not defaulted. Audit history
    remains the owner's durable evidence and is not copied into the new DTO.
    """
    source_binding = RulesetBinding.model_validate(source_binding)
    target = ruleset_binding(loader)
    if (
        source_binding.evaluator_version != "dnd5e-evaluation/35"
        or source_binding.ruleset_id != target.ruleset_id
        or source_binding.data_revision != target.data_revision
    ):
        raise ValueError("unsupported Legacy source binding or different effective rules data")
    value = json.loads(snapshot_json)
    if not isinstance(value, dict) or value.get("snapshot_schema_version") != "engine-snapshot/16":
        raise ValueError("migration requires the complete engine-snapshot/16 source")
    if value.get("snapshot_kind") == "combat":
        combat = value.get("combat_state")
        if (
            not isinstance(combat, dict)
            or not {"event_log", "legendary_resistance_applied_event_indices"} <= combat.keys()
        ):
            raise ValueError("Legacy history and event-index facts must be explicit")
        history = combat.pop("event_log")
        indices = combat.pop("legendary_resistance_applied_event_indices")
        if not isinstance(history, list) or not isinstance(indices, list) or indices:
            raise ValueError("Legacy history/index encoding is missing or unconvertible")
        events = TypeAdapter(tuple[CombatEvent, ...]).validate_json(
            json.dumps(history), strict=True, extra="forbid"
        )
        ended = combat.get("ended")
        if not isinstance(ended, bool):
            raise ValueError("Legacy ended fact must be explicit")
        validate_legacy_history(events, ended=ended, applied_indices=set())
    value["snapshot_schema_version"] = "engine-snapshot/17"
    converted: StateSnapshot = TypeAdapter(StateSnapshot).validate_json(
        json.dumps(value), strict=True
    )
    if isinstance(converted, CombatSnapshot):
        _validate_references(events, {a.entity_id for a in converted.character_states})
    return converted
