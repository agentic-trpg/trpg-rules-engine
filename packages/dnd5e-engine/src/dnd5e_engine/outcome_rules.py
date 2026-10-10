"""Shared terminal reason, encounter XP and historical outcome projection.

Only explicit value inputs; no runtime, handle objects, callbacks or registry.
"""

from collections.abc import Mapping, Sequence, Set
from typing import Literal

from dnd5e_engine.outcome import CombatOutcome, DeathRecord
from dnd5e_engine.types.combat import Combatant


def derive_ended_reason(
    actors: Sequence[Combatant], party_ids: Set[str], encounter_ids: Set[str], dead_ids: Set[str]
) -> Literal["victory", "defeat_tpk", "flee", "forced"]:
    if encounter_ids and all(eid in dead_ids for eid in encounter_ids):
        return "victory"
    if party_ids and all(eid in dead_ids for eid in party_ids):
        return "defeat_tpk"
    living_foes = [
        a for a in actors if a.entity_id in encounter_ids and a.entity_id not in dead_ids
    ]
    if living_foes and all(a.has_fled for a in living_foes):
        return "flee"
    return "forced"


def project_outcome(
    *,
    combat_id: str,
    actors: Sequence[Combatant],
    party_ids: Set[str],
    encounter_ids: Set[str],
    dead_ids: Set[str],
    hp: Mapping[str, int],
    temp_hp: Mapping[str, int],
    vanishing_temp_hp: Set[str],
    xp_values: Mapping[str, int],
    deaths: Sequence[DeathRecord],
    expended_resources: Mapping[str, Mapping[str, int]],
) -> CombatOutcome:
    total_xp = sum(xp_values.get(eid, 0) for eid in dead_ids if eid in encounter_ids)
    survivors = [eid for eid in party_ids if eid not in dead_ids]
    per_pc = total_xp // len(survivors) if survivors and total_xp > 0 else 0
    return CombatOutcome(
        handle_id=combat_id,
        ended_reason=derive_ended_reason(actors, party_ids, encounter_ids, dead_ids),
        deaths=list(deaths),
        residual_hp={eid: value for eid, value in hp.items() if eid in party_ids},
        residual_temp_hp={
            eid: value
            for eid, value in temp_hp.items()
            if eid in party_ids and value > 0 and eid not in vanishing_temp_hp
        },
        xp_awarded={eid: per_pc for eid in survivors} if per_pc > 0 else {},
        expended_resources={eid: dict(resources) for eid, resources in expended_resources.items()},
        loot_drops=[],
    )
