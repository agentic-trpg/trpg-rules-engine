"""Resolve a reviewed instantaneous healing spell during exploration."""

from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.common import HealActivity
from dnd5e_srd_data.schema.spell import CastingTimeUnit, Spell, SpellRangeUnits

from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.resolver import resolve_activity
from dnd5e_engine.damage_rules import healing_balance
from dnd5e_engine.evaluation_actor import combatant
from dnd5e_engine.evaluation_class_dependencies import class_dependencies
from dnd5e_engine.evaluation_contracts import ExplorationSpellCastPayload, RuleEvaluationRequest
from dnd5e_engine.evaluation_delta import HPDelta, ResourceUpdate, StateDelta
from dnd5e_engine.evaluation_equipment_dependencies import reviewed_healing_potion_owners
from dnd5e_engine.evaluation_projection import EvaluationInvariantError
from dnd5e_engine.evaluation_rng import RNGTransition
from dnd5e_engine.evaluation_ruleset import get_default_ruleset
from dnd5e_engine.evaluation_speed_effects import world_walk_bonus
from dnd5e_engine.evaluation_spells import ordinary_spell_mechanism, spell_event
from dnd5e_engine.evaluation_state import CharacterStateV2, NonCombatSnapshot
from dnd5e_engine.evaluation_turn import TurnEvaluation, _refuse
from dnd5e_engine.events import CombatEvent, HealingApplied, IntentSubmitted


def exploration_healing_activity(loader: AssetLoader, spell: Spell) -> HealActivity | None:
    """Select a canonical first-level single-creature heal with reviewed delivery."""
    if ordinary_spell_mechanism(loader, spell.slug) != "healing" or spell.level != 1:
        return None
    if not (
        spell.casting_time.unit == CastingTimeUnit.ACTION
        and spell.range.units == SpellRangeUnits.TOUCH
    ) and not exploration_healing_requires_sight(spell):
        return None
    activity = spell.activities[0]
    assert isinstance(activity, HealActivity)
    return activity


def exploration_healing_requires_sight(spell: Spell) -> bool:
    """Identify the bound Healing Word invocation that requires caster sight."""
    return spell.slug == "healing-word" and spell == get_default_ruleset().get_spell(spell.slug)


def exploration_healing_hp_delta(target: CharacterStateV2, amount: int) -> tuple[HPDelta, ...]:
    """Project capped healing for a living target, omitting an unchanged HP balance."""
    hp = healing_balance(target.hp_current, target.hp_max, amount)
    if hp == target.hp_current:
        return ()
    return (
        HPDelta(
            kind="actor.hp_delta",
            target_id=target.entity_id,
            expected_hp=target.hp_current,
            amount=hp - target.hp_current,
            resulting_hp=hp,
        ),
    )


def _has_unreviewed_healing_dependencies(snapshot: NonCombatSnapshot) -> bool:
    """Check the fixed heal actors for effects outside the reviewed passive walk profile."""
    if any(actor.trait_mechanics for actor in snapshot.character_states):
        return True
    for effect in snapshot.effect_states:
        if effect.disabled:
            continue
        try:
            # The same complete shape checked for walking has no healing or action hook.
            world_walk_bonus(effect)
        except ValueError:
            return True
    return False


def evaluate_exploration_healing(
    request: RuleEvaluationRequest,
    spell: Spell,
    activity: HealActivity,
    actor: CharacterStateV2,
    target: CharacterStateV2,
    payment: ResourceUpdate,
    loader: AssetLoader,
) -> TurnEvaluation:
    """Run the shared heal over fixed actors and project the uncommitted payment and HP.

    A living creature at full HP is a legal target. It still produces the rolled
    evidence and uses one slot, with no HP edit when the capped HP is unchanged.
    """
    snapshot, payload = request.state_snapshot, request.payload
    assert isinstance(snapshot, NonCombatSnapshot)
    assert isinstance(payload, ExplorationSpellCastPayload)
    if target.hp_current <= 0:
        return _refuse("rejected", "target_invalid", "healing requires a living positive-HP target")
    if _has_unreviewed_healing_dependencies(snapshot):
        return _refuse("unsupported", "healing.dependencies", "unreviewed healing trait or effect")
    canonical = get_default_ruleset()
    potion_owners = reviewed_healing_potion_owners(snapshot, loader, canonical)
    if failure := class_dependencies(
        actor,
        snapshot,
        loader,
        canonical,
        equipped=True,
        allow_stowed_healing_potions=actor.entity_id in potion_owners,
    ):
        return _refuse("unsupported", "healing.dependencies", failure)

    rng = request.rng_context.state.restore()
    events: list[CombatEvent] = [
        IntentSubmitted(
            actor_id=actor.entity_id,
            intent_type="cast_spell",
            spell_id=spell.slug,
            target_id=target.entity_id,
            source_id=payload.spell_source_id,
            activity_id=activity.id,
        ),
        spell_event(actor.entity_id, spell, payload.slot_level),
    ]
    caster = combatant(actor)
    context = build_activity_context(
        caster,
        [caster if actor.entity_id == target.entity_id else combatant(target)],
        rng=rng,
        event_emitter=events.append,
        slot_level=payload.slot_level,
        base_spell_level=spell.level,
        spellcasting_ability=actor.spellcasting_ability,
        concentration=False,
        source_passive_effects=spell.passive_effects,
        spell_book={},
        passive_damage_modifiers={},
        save_modifiers={},
    )
    resolve_activity(activity, context)
    if (
        len(events) != 3
        or not isinstance(healing := events[2], HealingApplied)
        or healing.target_id != target.entity_id
    ):
        raise EvaluationInvariantError("exploration heal must produce one healing event")
    if healing.amount < 0:
        return _refuse("unsupported", "healing.amount", "negative healing is outside this profile")
    return dict(
        status="accepted",
        state_delta=StateDelta(
            expected_world_version=snapshot.world_version,
            operations=(payment, *exploration_healing_hp_delta(target, healing.amount)),
        ),
        proposed_events=tuple(events),
        rng_transition=RNGTransition.between(request.rng_context, rng),
        choice=None,
        error=None,
    )
