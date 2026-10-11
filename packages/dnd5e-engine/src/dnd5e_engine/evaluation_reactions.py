"""One predeclared Counterspell, resolved inside its triggering evaluation."""

import random
from dataclasses import dataclass

from dnd5e_srd_data import BundledAssetLoader
from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.common import ReactionTriggerKind, SaveActivity
from dnd5e_srd_data.schema.spell import Spell

from dnd5e_engine.activities.actor_stats import ABILITY_CODES, save_modifier
from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.resolver import resolve_activity
from dnd5e_engine.evaluation_computation import CombatComputation
from dnd5e_engine.evaluation_delta import (
    PendingReactionUpdate,
    ResourceUpdate,
    SlotSpendLedgerUpdate,
    StateDelta,
)
from dnd5e_engine.evaluation_paid_projection import paid_delta
from dnd5e_engine.evaluation_payments import spell_payment
from dnd5e_engine.evaluation_projection import EvaluationInvariantError
from dnd5e_engine.evaluation_reaction_state import PendingReactionState
from dnd5e_engine.evaluation_state import CombatSnapshot
from dnd5e_engine.events import ReactionTriggered, SaveRolled
from dnd5e_engine.reactions import (
    ReactionOpportunity,
    matching_conditions,
    reaction_support,
    reaction_target_id,
)
from dnd5e_engine.spatial import GridTopology
from dnd5e_engine.specs import GridScene
from dnd5e_engine.spell_execution import admission_failure
from dnd5e_engine.spellcasting import spell_range_ft


@dataclass(frozen=True)
class CounterspellPlan:
    declaration: PendingReactionState
    spell: Spell
    activity: SaveActivity
    target_id: str
    payment: ResourceUpdate


def reaction_dependencies(snapshot: CombatSnapshot, loader: AssetLoader) -> str | None:
    declarations = snapshot.combat_state.pending_reactions
    if not declarations:
        return None
    if len(declarations) != 1:
        return "only one predeclared reaction is migrated; chains and priorities remain OPEN"
    pending = declarations[0]
    spell = loader.get_spell(pending.spell_id)
    if (
        spell is None
        or spell.slug != "counterspell"
        or spell != BundledAssetLoader().get_spell("counterspell")
    ):
        return "only canonical Counterspell is reviewed"
    if len(spell.activities) != 1 or not isinstance(spell.activities[0], SaveActivity):
        return "complete canonical save activity required"
    activity = spell.activities[0]
    declaration = pending.declaration()
    if (
        pending.activity_id != activity.id
        or pending.slot_level != 3
        or declaration.conditions != tuple(activity.activation.reaction_conditions)
        or declaration.semantics != activity.reaction
        or reaction_support(activity)
        or admission_failure(spell, [activity])
    ):
        return "reaction declaration differs from reviewed typed trigger/response"
    actor = next(a for a in snapshot.character_states if a.entity_id == pending.owner_id)
    levels = actor.classes or (
        {actor.class_slug: actor.character_level} if actor.class_slug else {}
    )
    component = snapshot.casting_state
    entry = (
        next((a for a in component.actors if a.actor_id == pending.owner_id), None)
        if component
        else None
    )
    source = (
        next((s for s in entry.sources if s.source_id == pending.spell_source_id), None)
        if entry
        else None
    )
    if (
        levels != {"wizard": 5}
        or actor.subclass_slug != "evoker"
        or actor.proficiency_bonus_override is not None
        or source is None
        or source.class_slug != "wizard"
        or source.ability != "int"
        or actor.spellcasting_ability != source.ability
        or spell.slug not in source.known_spells
        or spell.slug not in source.prepared_spells
        or actor.weapon_in_hands is not None
    ):
        return "explicit known/prepared Wizard 5 Evoker source and free Somatic hand required"
    return None


def prepare_counterspell(
    snapshot: CombatSnapshot, actor_id: str, spell: Spell, loader: AssetLoader
) -> CounterspellPlan | None:
    """Draw-free eligibility; unavailable legal declarations stay armed."""
    if not snapshot.combat_state.pending_reactions:
        return None
    pending = snapshot.combat_state.pending_reactions[0]
    actor = next(a for a in snapshot.character_states if a.entity_id == pending.owner_id)
    if (
        actor.entity_id == actor_id
        or not actor.is_alive
        or actor.hp_current <= 0
        or actor.entity_id in snapshot.combat_state.dead_ids
        or not actor.reaction_available
    ):
        return None
    opportunity = ReactionOpportunity(
        ReactionTriggerKind.SEES_SPELL_CAST, actor_id, triggering_spell_slug=spell.slug
    )
    declaration = pending.declaration()
    conditions = matching_conditions(declaration, opportunity)
    if not conditions or reaction_target_id(declaration, opportunity) != actor_id:
        return None
    topology = GridTopology(GridScene.model_validate(snapshot.scene_state.grid.model_dump()))
    positions = snapshot.combat_state.actor_zone
    origin, target = positions[actor.entity_id], positions[actor_id]
    if not topology.has_line_of_sight(origin, target) or not any(
        c.max_range_ft is None or topology.within_range(origin, target, c.max_range_ft)
        for c in conditions
    ):
        return None
    reaction_spell = loader.get_spell(pending.spell_id)
    assert reaction_spell is not None
    range_ft = spell_range_ft(reaction_spell)
    if range_ft is not None and not topology.within_range(origin, target, range_ft):
        return None
    assert snapshot.resource_state is not None
    try:
        payment = spell_payment(snapshot.resource_state, actor.entity_id, pending.slot_level)
    except ValueError:
        return None
    activity = reaction_spell.activities[0]
    assert isinstance(activity, SaveActivity)
    return CounterspellPlan(pending, reaction_spell, activity, actor_id, payment)


def resolve_counterspell(
    computation: CombatComputation, plan: CounterspellPlan, rng: random.Random
) -> bool:
    # Local computation only; caller publishes one complete delta or nothing.
    from dnd5e_engine.evaluation_spells import spell_event

    pending = plan.declaration
    reactor = computation.actors[pending.owner_id].model_copy(update={"reaction_available": False})
    computation.actors[reactor.entity_id] = reactor
    computation.state = computation.state.model_copy(update={"pending_reactions": []})
    computation.emit(
        ReactionTriggered(
            actor_id=reactor.entity_id,
            reaction_name=plan.spell.slug,
            trigger_kind=ReactionTriggerKind.SEES_SPELL_CAST,
            triggering_actor_id=plan.target_id,
            source_spell_id=plan.spell.slug,
            source_activity_id=plan.activity.id,
        )
    )
    computation.emit(spell_event(reactor.entity_id, plan.spell, pending.slot_level))
    context = build_activity_context(
        reactor,
        [computation.actors[plan.target_id]],
        rng=rng,
        event_emitter=computation.emit,
        slot_level=pending.slot_level,
        base_spell_level=plan.spell.level,
        spellcasting_ability="int",
        concentration=False,
        source_passive_effects=[],
        spell_book={},
        passive_damage_modifiers={},
        save_modifiers={
            plan.target_id: {
                "saves": {
                    ab: save_modifier(computation.actors[plan.target_id], ab).total
                    for ab in ABILITY_CODES
                }
            }
        },
    )
    start = len(computation.events)
    resolve_activity(plan.activity, context)
    saves = [e for e in computation.events[start:] if isinstance(e, SaveRolled)]
    if len(saves) != 1 or saves[0].target_id != plan.target_id:
        raise EvaluationInvariantError(
            "Counterspell failed to produce exactly the triggering caster save"
        )
    return not saves[0].succeeded


def reaction_paid_delta(
    before: CombatSnapshot,
    after: CombatSnapshot,
    payments: tuple[ResourceUpdate, ...],
    ledgers: tuple[SlotSpendLedgerUpdate, ...],
) -> StateDelta:
    old, new = before.combat_state.pending_reactions, after.combat_state.pending_reactions
    operations: tuple[PendingReactionUpdate, ...] = ()
    if old != new:
        operation = PendingReactionUpdate(
            kind="combat.pending_reaction_update",
            combat_id=before.combat_state.combat_id,
            owner_id=old[0].owner_id,
            expected=tuple(old),
            value=tuple(new),
        )
        operations = (operation,)
    normalized = after.model_copy(
        update={"combat_state": after.combat_state.model_copy(update={"pending_reactions": old})}
    )
    delta = paid_delta(before, normalized, payments, ledgers)
    return delta.model_copy(update={"operations": (*operations, *delta.operations)})
