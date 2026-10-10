"""Bounded explicit saving throws over supplied actor facts and private RNG."""

import random
from typing import get_args

from dnd5e_srd_data.loader import AssetLoader

from dnd5e_engine.activities.actor_stats import _SCORE_ATTR, save_modifier
from dnd5e_engine.activities.context import ActivityResolutionContext
from dnd5e_engine.activities.d20 import AdvantageSources
from dnd5e_engine.activities.save_primitive import roll_save
from dnd5e_engine.evaluation_actor import combatant
from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest, SavePayload
from dnd5e_engine.evaluation_delta import StateDelta
from dnd5e_engine.evaluation_rng import RNGTransition
from dnd5e_engine.evaluation_state import CharacterStateV2, NonCombatSnapshot
from dnd5e_engine.evaluation_turn import TurnEvaluation, _refuse
from dnd5e_engine.events import Ability, SaveRolled
from dnd5e_engine.rules.conditions import (
    _DECLARATIVE_CONDITION_EFFECTS,
    project_passive_save_modifiers,
)


def resolve_snapshot_save(
    state: CharacterStateV2, payload: SavePayload, rng: random.Random
) -> SaveRolled:
    """Shared calculation seam; caller must prove full modifier/lifecycle admission."""
    target = combatant(state)
    projected = project_passive_save_modifiers([c.condition for c in target.conditions])
    ctx = ActivityResolutionContext(
        rng=rng,
        caster=target,
        caster_abilities={ab: getattr(target, field) for ab, field in _SCORE_ATTR.items()},
        targets=[target],
        event_emitter=lambda event: None,
        passive_save_modifiers={
            target.entity_id: {ab: save_modifier(target, ab).total for ab in get_args(Ability)}
        },
        passive_save_adv={target.entity_id: projected["passive_save_adv"]},
        passive_save_dis={target.entity_id: projected["passive_save_dis"]},
        passive_save_auto_fail={target.entity_id: projected["passive_save_auto_fail"]},
        save_is_magical=payload.is_magical,
    )
    roll = roll_save(
        ctx,
        target,
        payload.ability,
        payload.dc,
        ignore_cover=True,
        additional_sources=AdvantageSources(
            advantage=payload.advantage, disadvantage=payload.disadvantage
        ),
    )
    return SaveRolled(
        target_id=target.entity_id,
        ability=payload.ability,
        dc=payload.dc,
        roll_total=roll.total,
        succeeded=roll.succeeded,
        advantage=roll.mode,
        natural=roll.natural,
        modifier=roll.modifier,
        sources=list(roll.sources),
    )


def save_actor_failure(state: CharacterStateV2, loader: AssetLoader) -> str | None:
    if (
        any(not 1 <= getattr(state, field) <= 30 for field in _SCORE_ATTR.values())
        or not 1 <= state.character_level <= 20
        or (state.proficiency_bonus_override is not None and state.proficiency_bonus_override < 0)
        or set(state.save_proficiencies) - set(get_args(Ability))
    ):
        return "invalid save ability/proficiency facts"
    if (
        state.worn_armor
        or state.shield_equipped
        or state.concentration_effect_id
        or state.trait_mechanics
        or state.classes
        or state.class_slug
        or state.subclass_slug
        or state.species_slug
        or state.granted_features
        or state.fighting_styles
        or state.legendary_resistances_max
        or state.legendary_resistances_remaining
    ):
        return "feature, trait, concentration and resource-dependent saves are not migrated"
    for condition in state.conditions:
        definition = loader.get_condition(condition.condition)
        if definition is None or tuple(definition.effects) != _DECLARATIVE_CONDITION_EFFECTS.get(
            condition.condition, ()
        ):
            return "condition clauses differ from the pinned compiled rule projection"
    for slug in state.carried_item_slugs:
        item = loader.get_item(slug)
        if item is None or item.passive_effects or item.requires_attunement:
            return "unmigrated equipment save modifiers"
    return None


def evaluate_save(request: RuleEvaluationRequest, loader: AssetLoader) -> TurnEvaluation:
    snapshot, payload = request.state_snapshot, request.payload
    assert isinstance(payload, SavePayload)
    if not isinstance(snapshot, NonCombatSnapshot) or snapshot.combat_setup is not None:
        return _refuse("unsupported", "save.snapshot", "standalone save requires noncombat facts")
    target = next((a for a in snapshot.character_states if a.entity_id == payload.target_id), None)
    if target is None or not target.is_alive or target.hp_current <= 0:
        return _refuse("rejected", "target_invalid", "saving actor is absent, dead or dying")
    if snapshot.scene_state.grid.cover_cells:
        return _refuse(
            "unsupported", "save.cover", "cover needs a complete source geometry contract"
        )
    if snapshot.effect_states:
        return _refuse(
            "unsupported", "save.effects", "active effect modifiers require closed deltas"
        )
    if set(payload.advantage + payload.disadvantage) - {"flag"}:
        return _refuse(
            "unsupported", "save.sources", "only explicitly adjudicated flags are admitted"
        )
    known = {
        "restrained",
        "paralyzed",
        "stunned",
        "petrified",
        "unconscious",
        "incapacitated",
        "prone",
    }
    if any(
        c.condition not in known
        or c.scope != "session"
        or c.duration_rounds is not None
        or c.save_dc is not None
        or c.source_effect_id is not None
        for c in target.conditions
    ):
        return _refuse(
            "unsupported", "save.conditions", "condition lifecycle dependencies are not migrated"
        )
    failure = save_actor_failure(target, loader)
    if failure:
        return _refuse("unsupported", "save.actor", failure)
    rng = request.rng_context.state.restore()
    event = resolve_snapshot_save(target, payload, rng)
    return dict(
        status="accepted",
        state_delta=StateDelta(expected_world_version=snapshot.world_version, operations=()),
        proposed_events=(event,),
        rng_transition=RNGTransition.between(request.rng_context, rng),
        choice=None,
        error=None,
    )
