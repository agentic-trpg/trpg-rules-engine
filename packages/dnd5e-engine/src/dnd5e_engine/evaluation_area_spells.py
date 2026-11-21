"""Plan instant point-targeted saving-throw spells from fixed combat inputs."""

from dnd5e_srd_data.schema.common import SaveActivity
from dnd5e_srd_data.schema.spell import Spell, SpellRangeUnits

from dnd5e_engine.areas import area_template
from dnd5e_engine.evaluation_actor import combatant
from dnd5e_engine.evaluation_state import CombatSnapshot
from dnd5e_engine.events import AreaTargeted
from dnd5e_engine.spatial import GridTopology
from dnd5e_engine.specs import GridScene
from dnd5e_engine.spell_delivery import ActivityDeliveryPlan, SpellDeliverySpec, plan_delivery
from dnd5e_engine.spellcasting import spell_range_ft


def is_point_save_cantrip(spell: Spell, activity: SaveActivity) -> bool:
    """Identify a fixed sphere placed by a cantrip whose targets follow its geometry."""
    template = area_template(activity)
    return (
        spell.level == 0
        and spell.range.units == SpellRangeUnits.FEET
        and not activity.target.affects.count
        and template is not None
        and template.shape == "sphere"
        and template.anchor == "target"
        and template.includes_origin
    )


def point_save_delivery(
    snapshot: CombatSnapshot, spell: Spell, actor_id: str, origin: str
) -> ActivityDeliveryPlan:
    """Place a bound spell from its point and select affected creatures in initiative order.

    The caller has already admitted a single typed sphere activity. The planner raises
    a delivery error for an invalid or unreachable point. It does not spend an Action
    or use randomness.
    """
    state = snapshot.combat_state
    actors = {a.entity_id: a for a in snapshot.character_states}
    creatures = tuple(
        combatant(actors[identity])
        for identity in state.initiative_ids
        if identity not in state.dead_ids
    )
    caster_in_party = actor_id in state.party_ids
    delivery = plan_delivery(
        spell.activities,
        SpellDeliverySpec(origin_cell=origin),
        actor_id=actor_id,
        topology=GridTopology(GridScene.model_validate(snapshot.scene_state.grid.model_dump())),
        positions=state.actor_zone,
        creatures=creatures,
        enemy_ids=frozenset(state.encounter_ids if caster_in_party else state.party_ids),
        ally_ids=frozenset(state.party_ids if caster_in_party else state.encounter_ids),
        range_ft=spell_range_ft(spell),
        execution=True,
        cast_level=0,
        base_level=0,
    )
    (placement,) = delivery.activities
    return placement


def point_save_event(placement: ActivityDeliveryPlan, actor_id: str, spell_id: str) -> AreaTargeted:
    """Bind the placed sphere and its exact creature set to ordered cast evidence."""
    assert placement.template is not None
    assert placement.origin is not None
    return AreaTargeted(
        actor_id=actor_id,
        source_id=f"spell:{spell_id}:{placement.activity_id}",
        shape=placement.template.shape,
        size_ft=placement.template.size_ft,
        origin=placement.origin,
        direction=placement.direction,
        affected_ids=list(placement.target_ids),
        excluded_ids=list(placement.spared_ids),
        source_spell_id=spell_id,
        source_activity_id=placement.activity_id,
    )
