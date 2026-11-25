"""Resolve an existing SRD Burning application at its target's TurnStart."""

from functools import partial

from dnd5e_srd_data.loader import AssetLoader

from dnd5e_engine.activities.apply import apply_damage
from dnd5e_engine.activities.context import ActivityResolutionContext
from dnd5e_engine.activities.effects import passive_effect_to_active_effect
from dnd5e_engine.attack_rules import static_damage_modifiers
from dnd5e_engine.evaluation_computation import CombatComputation, TurnStartConflictError
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_ruleset import get_default_ruleset
from dnd5e_engine.evaluation_state import CombatSnapshot
from dnd5e_engine.lib_loader import scoped_lib_loader

BURNING_ITEM = "alchemists-fire"
BURNING_EFFECT_ID = "effect:burning."
BURNING_DAMAGE_SOURCE = f"item:{BURNING_ITEM}"
_ORIGIN_PREFIX = f"{BURNING_DAMAGE_SOURCE}:"


def burning_turn_input(
    snapshot: CombatSnapshot, loader: AssetLoader
) -> tuple[CombatSnapshot, dict[str, str]]:
    """Validate the fixed Burning application and separate it from other mechanical effects.

    Return the remaining dependency profile and the Burning target-to-source mapping.
    This application has no clock or condition lineage. Invalid application facts raise
    ValueError before evaluation. The caller retains the original supplied application.
    """
    burning = tuple(
        e
        for e in snapshot.effect_states
        if e.id == BURNING_EFFECT_ID or e.origin.startswith(_ORIGIN_PREFIX)
    )
    if not burning:
        return snapshot, {}
    # RUO-45-F1: the host's reviewed TurnStart adoption covers one damage instance.
    if len(burning) != 1:
        raise ValueError("RUO-45-F1: TurnStart adoption supports one Burning application")
    (effect,) = burning
    item = loader.get_item(BURNING_ITEM)
    if (
        item is None
        or len(item.passive_effects) != 1
        or item != get_default_ruleset().get_item(BURNING_ITEM)
    ):
        raise ValueError("Burning requires the reviewed Alchemist's Fire declaration")
    actors = {a.entity_id for a in snapshot.character_states}
    source_id = effect.origin.removeprefix(_ORIGIN_PREFIX)
    if not effect.origin.startswith(_ORIGIN_PREFIX) or not {source_id, effect.target_id} <= actors:
        raise ValueError("Burning requires explicit item provenance and combat source and target")
    active = passive_effect_to_active_effect(
        item.passive_effects[0], target_id=effect.target_id, caster_id=source_id
    )
    if effect != effect_state(active.model_copy(update={"origin": effect.origin})):
        raise ValueError("Burning application differs from its bound item effect")
    remaining = tuple(e for e in snapshot.effect_states if e != effect)
    return snapshot.model_copy(update={"effect_states": remaining}), {effect.target_id: source_id}


def apply_burning_turn_start(
    sources: dict[str, str],
    loader: AssetLoader,
    computation: CombatComputation,
    actor_id: str,
    death_save_eligible: bool,
) -> None:
    """Apply SRD 5.2.1 p. 178 Burning through the shared candidate damage path."""
    source_id = sources.get(actor_id)
    if source_id is None:
        return
    if death_save_eligible:
        raise TurnStartConflictError(
            "Burning and a simultaneous death save require an ordering decision"
        )
    target = computation.actors[actor_id]
    with scoped_lib_loader(loader):
        context = ActivityResolutionContext(
            rng=computation.rng,
            caster=computation.actors[source_id],
            targets=[target],
            event_emitter=computation.emit,
            caster_abilities={},
            passive_damage_modifiers={actor_id: static_damage_modifiers(target)},
            damage_instance_id_provider=partial(computation.next_damage_id, actor_id=source_id),
            damage_instance_resolved=computation.damage_completed,
            activity_source_id=BURNING_DAMAGE_SOURCE,
        )
        apply_damage(target, {"fire": computation.rng.randint(1, 4)}, context)
