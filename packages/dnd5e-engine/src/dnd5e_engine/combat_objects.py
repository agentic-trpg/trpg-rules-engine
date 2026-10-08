"""Minimal combat object authority. Host owns legality/costs of world interactions.

No inventory, equipment synchronization, capacity, physics or prose interpretation.
Spell sources continue to live exclusively in PersistentAreaState.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from dnd5e_engine.types.objects import (
    CombatObject,
    CombatObjectView,
    ObjectChangeOperation,
    ObjectMutation,
)

if TYPE_CHECKING:
    from dnd5e_engine.events import CombatEvent
    from dnd5e_engine.orchestrator import CombatHandle, _LiveCombat


def object_position(obj: CombatObject, positions: Mapping[str, str]) -> str | None:
    return obj.position if obj.holder_id is None else positions.get(obj.holder_id)


@dataclass
class CombatObjectState:
    objects: dict[str, CombatObject] = field(default_factory=dict)
    # Identity is never reused, even after departure/removal, within this combat.
    used_ids: set[str] = field(default_factory=set)

    def views(self, live: _LiveCombat) -> tuple[CombatObjectView, ...]:
        from dnd5e_engine.persistent_areas import FollowObjectEmanation

        return tuple(
            CombatObjectView(
                object=obj,
                position=position,
                area_ids=tuple(
                    a.id
                    for a in live.persistent_areas.areas
                    if isinstance(a.geometry, FollowObjectEmanation)
                    and a.geometry.object_id == obj.id
                ),
            )
            for obj in self.objects.values()
            if (position := object_position(obj, live.actor_zone)) is not None
        )

    def remove(
        self, live: _LiveCombat, obj: CombatObject, source_id: str, reason: ObjectChangeOperation
    ) -> None:
        from dnd5e_engine import orchestrator as orch
        from dnd5e_engine.persistent_areas import FollowObjectEmanation

        del self.objects[obj.id]
        _changed(live, obj, None, source_id, reason)
        for area in tuple(live.persistent_areas.areas):
            if (
                isinstance(area.geometry, FollowObjectEmanation)
                and area.geometry.object_id == obj.id
            ):
                live.persistent_areas.expire(live, area, "source_removed", cause_id=obj.id)
                if area.concentration_identity:
                    orch._drop_concentration(live, area.source_entity_id)

    def observe(self, live: _LiveCombat, event: CombatEvent) -> None:
        from dnd5e_engine.events import CombatantLeft, CombatEnded, Death

        if not isinstance(event, (Death, CombatantLeft, CombatEnded)):
            return
        entity_id = (
            event.target_id
            if isinstance(event, Death)
            else event.entity_id
            if isinstance(event, CombatantLeft)
            else None
        )
        changed = False
        for obj in tuple(self.objects.values()):
            if isinstance(event, CombatEnded):
                self.remove(live, obj, "lifecycle:combat_end", "combat_end")
                changed = True
            elif obj.holder_id == entity_id:
                position = object_position(obj, live.actor_zone)
                if isinstance(event, Death) and position is not None:
                    dropped = obj.model_copy(
                        update={
                            "holder_id": None,
                            "disposition": "unattended",
                            "position": position,
                        }
                    )
                    self.objects[obj.id] = dropped
                    _changed(live, obj, dropped, "lifecycle:holder_death", "holder_died")
                    changed = True
                else:
                    self.remove(live, obj, "lifecycle:holder_left", "holder_left")
                    changed = True
        from dnd5e_engine.environment import refresh_environment

        if changed:
            refresh_environment(live)


def _changed(
    live: _LiveCombat,
    before: CombatObject | None,
    after: CombatObject | None,
    source_id: str,
    operation: ObjectChangeOperation,
) -> None:
    from dnd5e_engine import orchestrator as orch
    from dnd5e_engine.events import CombatObjectChanged

    obj = after or before
    assert obj is not None
    orch._emit(
        live,
        CombatObjectChanged(
            object_id=obj.id,
            source_id=source_id,
            operation=operation,
            before=before,
            after=after,
        ),
    )


def _position_valid(live: _LiveCombat, position: str | None) -> bool:
    from dnd5e_engine.spatial import canonical_cell_id

    try:
        return (
            position is not None
            and canonical_cell_id(position) == position
            and live.topology.is_valid_cell(position)
        )
    except ValueError:
        return False


def _holder_valid(live: _LiveCombat, holder_id: str | None) -> bool:
    return (
        any(
            c.entity_id == holder_id and c.is_alive and c.entity_id not in live.dead_ids
            for c in live.initiative
        )
        and holder_id in live.actor_zone
    )


async def register_combat_objects(
    handle: CombatHandle, *, owner_id: str, objects: tuple[CombatObject, ...]
) -> None:
    """Validate a complete atomic batch of new Host records; no replacements.

    owner_id is an authority claim inside a trusted Host process, not authentication.
    Unattended foreign-owned objects remain legal spell targets.
    """
    from dnd5e_engine import orchestrator as orch

    live = orch._get_live(handle)
    if live.ended:
        raise orch.IntentRejectedError("combat_ended", "combat has ended")
    records = tuple(CombatObject.model_validate(obj.model_dump()) for obj in objects)
    if (
        not records
        or len({obj.id for obj in records}) != len(records)
        or any(
            obj.owner_id != owner_id
            or obj.id in live.combat_objects.used_ids
            or obj.id in live.actor_zone
            or not (
                _position_valid(live, obj.position)
                if obj.holder_id is None
                else _holder_valid(live, obj.holder_id)
            )
            for obj in records
        )
    ):
        raise orch.IntentRejectedError(
            "target_invalid", "invalid object identity, owner, position or holder"
        )
    with orch._execution_transaction(live):
        for obj in records:
            live.combat_objects.objects[obj.id] = obj
            live.combat_objects.used_ids.add(obj.id)
            _changed(live, None, obj, obj.source_id, "created")


async def mutate_combat_object(
    handle: CombatHandle, *, owner_id: str, mutation: ObjectMutation
) -> None:
    """Apply a completed Host operation atomically, then reconcile all sources.

    Pickup/wear require a living co-located holder. Movement is Host-attested
    placement of an unattended object, not a creature's free move/teleport action.
    """
    from dnd5e_engine import orchestrator as orch
    from dnd5e_engine.environment import reconcile_environment

    live = orch._get_live(handle)
    if live.ended:
        raise orch.IntentRejectedError("combat_ended", "combat has ended")
    mutation = ObjectMutation.model_validate(mutation.model_dump())
    obj = live.combat_objects.objects.get(mutation.object_id)
    if obj is None or obj.owner_id != owner_id:
        raise orch.IntentRejectedError(
            "target_invalid", "object is absent or belongs to another mutation authority"
        )
    changes: dict[str, object] = {}
    op = mutation.operation
    if op == "move":
        valid = obj.holder_id is None and _position_valid(live, mutation.position)
        changes = {"position": mutation.position}
    elif op in ("pickup", "wear"):
        valid = (
            _holder_valid(live, mutation.holder_id)
            and object_position(obj, live.actor_zone)
            == live.actor_zone.get(mutation.holder_id or "")
            and (obj.holder_id is None or (op == "wear" and obj.holder_id == mutation.holder_id))
        )
        changes = {
            "position": None,
            "holder_id": mutation.holder_id,
            "disposition": "carried" if op == "pickup" else "worn",
        }
    elif op == "drop":
        valid = obj.holder_id is not None and _position_valid(
            live, object_position(obj, live.actor_zone)
        )
        changes = {
            "position": object_position(obj, live.actor_zone),
            "holder_id": None,
            "disposition": "unattended",
        }
    elif op in ("cover", "uncover"):
        valid = True
        changes = {"opaque_cover": op == "cover"}
    else:
        valid = True
    if not valid:
        raise orch.IntentRejectedError(
            "target_invalid", "illegal object operation, holder or position"
        )
    with orch._execution_transaction(live):
        if op == "remove":
            live.combat_objects.remove(live, obj, mutation.source_id, "removed")
        else:
            updated = CombatObject.model_validate(obj.model_dump() | changes)
            live.combat_objects.objects[obj.id] = updated
            _changed(live, obj, updated, mutation.source_id, op)
        reconcile_environment(live)


__all__ = [
    "CombatObject",
    "CombatObjectView",
    "ObjectMutation",
    "mutate_combat_object",
    "register_combat_objects",
]
