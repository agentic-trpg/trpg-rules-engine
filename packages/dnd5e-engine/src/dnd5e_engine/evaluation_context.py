"""Per-call legacy execution context, reconstructed exclusively from a snapshot."""

import asyncio
import copy
import random

from dnd5e_srd_data.loader import AssetLoader

from dnd5e_engine import orchestrator as orch
from dnd5e_engine.combat_objects import CombatObjectState
from dnd5e_engine.evaluation_state import CombatSnapshot, CombatState
from dnd5e_engine.persistent_areas import PersistentAreaState
from dnd5e_engine.spatial import GridTopology
from dnd5e_engine.specs import GridScene
from dnd5e_engine.timed_activities import TimedActivityState
from dnd5e_engine.types.combat import Combatant


def execution_context(snapshot: CombatSnapshot, loader: AssetLoader) -> orch._LiveCombat:
    """Only called after admission excludes complex sidecars, never start_combat.

    A fixed private RNG is a draw-free preflight sentinel, replaced by the
    explicit request RNG only when execution has been admitted.
    """
    actors = []
    for actor_state in snapshot.character_states:
        value = actor_state.model_dump(mode="python")
        for field in ("spell_slots", "pact_slots", "spells_known", "custom_counters"):
            del value[field]
        value["death_saves"] = value["death_saves"] or {}
        actors.append(Combatant.model_validate(value))
    by_id = {actor.entity_id: actor for actor in actors}
    state = snapshot.combat_state
    live = orch._LiveCombat(
        handle_id=state.combat_id,
        session_id=snapshot.session_id,
        initiative=[by_id[key] for key in state.initiative_ids],
        party_ids=set(state.party_ids),
        encounter_ids=set(state.encounter_ids),
        topology=GridTopology(GridScene.model_validate(snapshot.scene_state.grid.model_dump())),
        rng=random.Random(0),
        event_queue=asyncio.Queue(),
        scene_location_id=snapshot.scene_state.scene_id,
        ruleset_loader=loader,
    )
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
        "timed_activities",
    }
    for name in CombatState.model_fields:
        if name not in adapted:
            setattr(live, name, copy.deepcopy(getattr(state, name)))
    live.persistent_areas = PersistentAreaState(
        areas=[],
        next_sequence=state.persistent_areas.next_sequence,
        next_turn_start_effects={},
    )
    live.combat_objects = CombatObjectState(used_ids=set(state.combat_objects.used_ids))
    live.timed_activities = TimedActivityState(
        pending=[], next_sequence=state.timed_activities.next_sequence
    )
    live.event_log = list(copy.deepcopy(state.event_log))
    live.current_actor_id = state.initiative_ids[state.current_turn_index]
    live.scene_sunlight = snapshot.scene_state.grid.sunlight
    live.tracked_hp = {actor.entity_id: actor.hp_current for actor in actors}
    live.tracked_temp_hp = {actor.entity_id: actor.temp_hp for actor in actors}
    live.active_conditions = {
        actor.entity_id: {condition.condition for condition in actor.conditions} for actor in actors
    }
    for actor in snapshot.character_states:
        live.spell_slots_by_entity[actor.entity_id] = copy.deepcopy(actor.spell_slots)
        live.pact_slots_by_entity[actor.entity_id] = copy.deepcopy(actor.pact_slots)
        live.spells_known_by_entity[actor.entity_id] = list(actor.spells_known)
        live.custom_counters_by_entity[actor.entity_id] = copy.deepcopy(actor.custom_counters)
    orch._register_default_turn_hooks(live)
    return live
