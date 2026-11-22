"""Resolve a reviewed instantaneous healing spell during exploration."""

from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.common import HealActivity
from dnd5e_srd_data.schema.spell import CastingTimeUnit, Spell, SpellRangeUnits

from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.heal import HealingModifiers, resolve_heal
from dnd5e_engine.damage_rules import healing_balance
from dnd5e_engine.evaluation_actor import combatant
from dnd5e_engine.evaluation_beacon import is_reviewed_beacon_effect
from dnd5e_engine.evaluation_class_dependencies import class_dependencies
from dnd5e_engine.evaluation_contracts import ExplorationSpellCastPayload, RuleEvaluationRequest
from dnd5e_engine.evaluation_delta import HPDelta, ResourceUpdate, StateDelta
from dnd5e_engine.evaluation_equipment_dependencies import reviewed_healing_potion_owners
from dnd5e_engine.evaluation_projection import EvaluationInvariantError
from dnd5e_engine.evaluation_resources import SlotResource
from dnd5e_engine.evaluation_rng import RNGTransition
from dnd5e_engine.evaluation_ruleset import get_default_ruleset
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


def exploration_healing_maximizes(
    snapshot: NonCombatSnapshot,
    actor_id: str,
    target_id: str,
    loader: AssetLoader,
) -> bool:
    """Check scoped passive effects and detect maximum healing on the recipient.

    Unknown active mechanics raise ValueError before any RNG is used.
    """
    from dnd5e_engine.evaluation_timed_spells import is_healing_neutral_elapsed_spell

    maximize = False
    for effect in snapshot.effect_states:
        if effect.disabled or effect.target_id not in (actor_id, target_id):
            continue
        if is_reviewed_beacon_effect(effect, loader):
            maximize |= effect.target_id == target_id
        elif not is_healing_neutral_elapsed_spell(effect):
            raise ValueError("unreviewed healing effect")
    return maximize


def _exploration_healing_modifiers(
    snapshot: NonCombatSnapshot,
    actor: CharacterStateV2,
    target: CharacterStateV2,
    payment: ResourceUpdate,
    loader: AssetLoader,
) -> HealingModifiers:
    """Combine the verified caster's slotted spell rider with target healing effects."""
    if actor.trait_mechanics or target.trait_mechanics:
        raise ValueError("unreviewed healing trait")
    canonical = get_default_ruleset()
    potion_owners = reviewed_healing_potion_owners(snapshot, loader, canonical)
    if failure := class_dependencies(
        actor,
        snapshot,
        loader,
        canonical,
        equipped=True,
        allow_stowed_healing_potions=actor.entity_id in potion_owners,
        reviewed_life_healing=True,
    ):
        raise ValueError(failure)
    maximize = exploration_healing_maximizes(snapshot, actor.entity_id, target.entity_id, loader)
    bonus = 0
    if (
        isinstance(payment.expected, SlotResource)
        and payment.expected.kind == "spell_slot"
        and actor.subclass_slug == "life"
        and "disciple-of-life" in (actor.granted_features or ())
    ):
        # This is the same-turn spell restoration, with a verified ordinary slot spend.
        # The reviewed SRD grant is keyed to the level of the actual spent slot.
        bonus = 2 + payment.expected.level
    return HealingModifiers(bonus=bonus, maximize_dice=maximize)


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
    try:
        modifiers = _exploration_healing_modifiers(snapshot, actor, target, payment, loader)
    except ValueError as exc:
        return _refuse("unsupported", "healing.dependencies", str(exc))

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
    resolve_heal(activity, context, modifiers={target.entity_id: modifiers})
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
