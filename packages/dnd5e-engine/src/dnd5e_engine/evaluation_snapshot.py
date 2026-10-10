"""Internal, read-only DTO capture for parity tests and temporary contexts."""

import copy
from typing import Any

from dnd5e_engine.evaluation_state import (
    CharacterState,
    CombatSnapshot,
    CombatState,
    ConditionLink,
    ConstructState,
    EffectTurnRecord,
    LifecycleRecord,
    MechanicalGrid,
    ObjectState,
    PersistentAreasState,
    SceneState,
    SummonState,
    TransformState,
)
from dnd5e_engine.orchestrator import _LiveCombat
from dnd5e_engine.specs import GridScene
from dnd5e_engine.types.combat import Combatant


def character_state(live: _LiveCombat, actor: Combatant) -> CharacterState:
    value = actor.model_dump(mode="python")
    value["death_saves"] = value["death_saves"] or None
    value.update(
        spell_slots=copy.deepcopy(live.spell_slots_by_entity.get(actor.entity_id, {})),
        pact_slots=copy.deepcopy(live.pact_slots_by_entity.get(actor.entity_id, {})),
        spells_known=list(live.spells_known_by_entity.get(actor.entity_id, [])),
        custom_counters=copy.deepcopy(live.custom_counters_by_entity.get(actor.entity_id, {})),
    )
    return CharacterState.model_validate(value)


def capture_combat_snapshot(
    live: _LiveCombat, *, grid: GridScene, world_version: int, combat_id: str
) -> CombatSnapshot:
    """Capture a completed boundary; no registry lookup or RNG serialization.

    The caller supplies the authoritative static scene and version. Do not use
    this to manufacture missing State Machine dependencies in evaluate().
    """
    if (
        live.transaction_active
        or live.reaction_resolution_depth
        or live.undead_fortitude_holds
        or live.lifecycle_damage
    ):
        raise ValueError("cannot capture an in-flight execution boundary")
    actors = tuple(character_state(live, actor) for actor in live.initiative)
    by_id = {actor.entity_id: actor for actor in live.initiative}
    adapted = {
        "combat_id",
        "initiative_ids",
        "persistent_areas",
        "combat_objects",
        "conditions_by_effect",
        "effect_lifecycles",
        "event_log",
        "constructs",
        "transforms",
        "summons",
    }
    value: dict[str, Any] = {
        name: copy.deepcopy(getattr(live, name))
        for name in CombatState.model_fields
        if name not in adapted
    }
    transforms = {}
    for key, transform in live.transforms.items():
        original = by_id[key].model_copy(update=copy.deepcopy(transform.stash))
        metadata = {
            name: copy.deepcopy(getattr(transform, name))
            for name in TransformState.model_fields
            if name not in ("original_actor", "replaced_fields")
        }
        transforms[key] = TransformState(
            **metadata,
            original_actor=character_state(live, original),
            replaced_fields=tuple(transform.stash),
        )
    value.update(
        combat_id=combat_id,
        initiative_ids=tuple(actor.entity_id for actor in actors),
        persistent_areas=PersistentAreasState(
            areas=copy.deepcopy(live.persistent_areas.areas),
            next_sequence=live.persistent_areas.next_sequence,
            next_turn_start_effects=tuple(
                EffectTurnRecord(identity=key, turn=turn)
                for key, turn in live.persistent_areas.next_turn_start_effects.items()
            ),
        ),
        combat_objects=ObjectState(
            objects=copy.deepcopy(list(live.combat_objects.objects.values())),
            used_ids=set(live.combat_objects.used_ids),
        ),
        conditions_by_effect=tuple(
            ConditionLink(identity=key, statuses=list(statuses))
            for key, statuses in live.conditions_by_effect.items()
        ),
        effect_lifecycles=tuple(
            LifecycleRecord(identity=key, state=copy.deepcopy(state))
            for key, state in live.effect_lifecycles.items()
        ),
        event_log=tuple(copy.deepcopy(live.event_log)),
        constructs={
            key: ConstructState.model_validate(vars(state))
            for key, state in live.constructs.items()
        },
        transforms=transforms,
        summons={
            key: SummonState.model_validate(vars(state)) for key, state in live.summons.items()
        },
    )
    return CombatSnapshot(
        snapshot_kind="combat",
        snapshot_schema_version="engine-snapshot/1",
        session_id=live.session_id,
        world_version=world_version,
        character_states=actors,
        effect_states=tuple(
            copy.deepcopy(effect) for effects in live.active_effects.values() for effect in effects
        ),
        scene_state=SceneState(
            scene_id=live.scene_location_id,
            grid=MechanicalGrid.model_validate(grid.model_dump(mode="python")),
        ),
        combat_state=CombatState.model_validate(value),
    )
