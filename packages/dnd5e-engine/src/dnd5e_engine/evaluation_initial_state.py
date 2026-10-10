"""Fresh bounded combat component, not an execution runtime or defaulted input."""

from dnd5e_engine.evaluation_state import (
    CombatSetupState,
    CombatState,
    MovementLedgerState,
    ObjectState,
    PersistentAreasState,
    TimedActivitiesState,
)


def initial_combat_state(setup: CombatSetupState, initiative: tuple[str, ...]) -> CombatState:
    """Explicit fresh-component invariants; existing world components stay outside."""
    return CombatState(
        combat_id=setup.combat_id,
        initiative_ids=initiative,
        party_ids=set(setup.party_ids),
        encounter_ids=set(setup.encounter_ids),
        current_turn_index=0,
        round_number=1,
        turn_serial=1,
        timed_activities=TimedActivitiesState(pending=[], next_sequence=0),
        persistent_areas=PersistentAreasState(
            areas=[], next_sequence=0, next_turn_start_effects=()
        ),
        combat_objects=ObjectState(objects=[], used_ids=set()),
        ended=False,
        actor_zone=dict(setup.actor_zone),
        movement_ledgers={
            a: MovementLedgerState(spent_ft=0, distance_ft=0, active_mode="walk", dash_count=0)
            for a in initiative
        },
        opportunity_attack_weapons=dict(setup.opportunity_attack_weapons),
        monster_slug_by_entity={b.actor_id: b.monster_slug for b in setup.npc_stat_blocks},
        xp_value_by_entity=dict(setup.xp_value_by_entity),
        event_log=(),
        deaths_recorded=[],
        dead_ids=set(),
        expended_resources={},
        concentration_chain={},
        concentration_rounds_remaining={},
        conditions_by_effect=(),
        effect_lifecycles=(),
        pending_reactions=[],
        active_reaction_responses=[],
        damage_instance_sequence=0,
        rider_uses=set(),
        processed_zero_hp_damage_instances=set(),
        reaction_effects_pending_expiry={},
        help_grants={},
        help_check_grants=[],
        hidden_entities=set(),
        vex_grants={},
        rage_bonus_extensions=set(),
        sap_marks={},
        slow_marks={},
        monster_action_uses_by_entity={b.actor_id: {} for b in setup.npc_stat_blocks},
        monster_turn_start_done=None,
        last_ended_turn=None,
        departed_actor_id=None,
        legendary_windows_used=set(),
        legendary_resistance_armed={},
        legendary_resistance_applied_event_indices=set(),
        constructs={},
        transforms={},
        summons={},
        summon_counts={},
    )
