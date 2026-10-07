"""Live reaction boundary: validate, select, pay and resolve canonical activities."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from typing import TYPE_CHECKING

from dnd5e_srd_data.schema.common import ReactionCondition, ReactionTriggerKind, SaveActivity

from dnd5e_engine.activities.arithmetic import parse_expression, scalar
from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.context import ActivityResolutionContext, AttackHitContext
from dnd5e_engine.activities.dice import damage_part_to_expr, validate_expression
from dnd5e_engine.activities.effects import passive_effect_to_active_effect
from dnd5e_engine.activities.formula import resolve_damage_block, resolve_roll_data
from dnd5e_engine.activities.resolver import resolve_activity
from dnd5e_engine.activities.save import _resolve_dc, _resolve_save_ability
from dnd5e_engine.events import (
    CastFailed,
    CombatantLeft,
    CombatEvent,
    Death,
    EffectApplied,
    EffectExpired,
    ReactionTriggered,
    SaveRolled,
)
from dnd5e_engine.feature_runtime import DrawFreeRandom
from dnd5e_engine.lib_loader import get_lib_loader
from dnd5e_engine.reactions import (
    ActiveReactionResponse,
    PendingReaction,
    ReactionOpportunity,
    ReactionResult,
    compatible_trigger,
    matching_conditions,
    reaction_support,
    reaction_target_id,
)
from dnd5e_engine.rules.conditions import conditions_block_actions

if TYPE_CHECKING:
    from dnd5e_srd_data.schema.common import Activity
    from dnd5e_srd_data.schema.spell import Spell

    from dnd5e_engine.activities.context import DamageInstanceContext
    from dnd5e_engine.orchestrator import PlayerIntent, _LiveCombat
    from dnd5e_engine.types.combat import Combatant
    from dnd5e_engine.types.effects import ActiveEffect


def can_take_reaction(live: _LiveCombat, actor: Combatant, *, spell: bool = False) -> bool:
    from dnd5e_engine import orchestrator as orch

    return (
        actor.is_alive
        and actor.hp_current > 0
        and actor.entity_id not in live.dead_ids
        and actor.reaction_available
        and not conditions_block_actions(orch._condition_names(actor))
        and (
            not spell
            or (
                actor.entity_id not in live.transforms
                and orch._rage_effect(live, actor.entity_id) is None
            )
        )
    )


def pay_reaction(live: _LiveCombat, actor: Combatant) -> Combatant:
    from dnd5e_engine import orchestrator as orch

    orch._update_combatant(live, actor.entity_id, reaction_available=False)
    return orch._find_combatant(live, actor.entity_id) or actor


def _activity(spell: Spell, activity_id: str | None) -> Activity:
    candidates = [a for a in spell.activities if a.activation.reaction_conditions]
    if activity_id is not None:
        candidates = [a for a in candidates if a.id == activity_id]
    if len(candidates) != 1:
        raise ValueError("reaction declaration requires one typed activity")
    activity = candidates[0]
    reason = reaction_support(activity)
    if reason is not None:
        raise ValueError(reason)
    return activity


def _context(
    live: _LiveCombat,
    reactor: Combatant,
    targets: list[Combatant],
    spell: Spell,
    slot_level: int,
    *,
    draw_free: bool = False,
) -> ActivityResolutionContext:
    from dnd5e_engine import orchestrator as orch

    payload = orch._build_hydration_payload(live, caster=reactor)
    # Supported reaction activities never attack. AC bonus dice are irrelevant
    # here and must not be eagerly re-rolled while building their save context.
    saves = {
        entity: {key: value for key, value in entry.items() if key != "passive_ac_bonus"}
        for entity, entry in payload["save_modifiers"].items()
    }
    damage_modifiers = payload["passive_damage_modifiers"]
    dc_bonus = damage_modifiers.get(reactor.entity_id, {}).get("passive_spell_dc_bonus")
    if draw_free and dc_bonus:
        if not isinstance(dc_bonus, str):
            raise ValueError("reaction spell DC bonus must be an expression")
        validate_expression(dc_bonus)
    # The builder evaluates a save-DC bonus eagerly. Preflight validates its
    # complete grammar without drawing; the committed save reaction builds
    # its real DC once. A utility response has no save and needs no DC roll.
    if draw_free or not any(isinstance(activity, SaveActivity) for activity in spell.activities):
        damage_modifiers = {
            entity: {key: value for key, value in entry.items() if key != "passive_spell_dc_bonus"}
            for entity, entry in damage_modifiers.items()
        }
    ctx = build_activity_context(
        reactor,
        targets,
        rng=DrawFreeRandom() if draw_free else live.rng,
        event_emitter=lambda event: orch._emit(live, event),
        slot_level=slot_level,
        base_spell_level=spell.level,
        spellcasting_ability=orch._resolve_caster_spellcasting_ability(reactor),
        concentration=spell.concentration,
        source_passive_effects=list(spell.passive_effects),
        spell_book={},
        passive_damage_modifiers=damage_modifiers,
        save_modifiers=saves,
        check_modifiers=payload["check_modifiers"],
        check_states=payload["check_states"],
        d20_test_penalty=payload["d20_test_penalty"],
        target_distance_ft=orch._target_distance_map(live, reactor.entity_id, targets),
        target_cover=orch._target_cover_map(live, reactor.entity_id, targets),
        legendary_resistance_armed=payload["legendary_resistance_armed"],
        legendary_resistances_remaining_by_entity=payload[
            "legendary_resistances_remaining_by_entity"
        ],
        undead_fortitude_holds=live.undead_fortitude_holds,
    )
    return replace(ctx, lifecycle_source_kind="spell", lifecycle_source_slug=spell.slug)


def _validate_payload(spell: Spell, activity: Activity, ctx: ActivityResolutionContext) -> None:
    if isinstance(activity, SaveActivity):
        if len(activity.save.ability) != 1:
            raise ValueError("reaction save requires one ability")
        _resolve_save_ability(activity)
        if activity.save.dc.calculation in ("", "flat"):
            scalar(
                parse_expression(resolve_roll_data(activity.save.dc.formula, ctx), allow_dice=False)
            )
        _resolve_dc(activity, ctx)
        for part in activity.damage.parts:
            resolved = resolve_damage_block(part, ctx, ability=ctx.spellcasting_ability)
            validate_expression(damage_part_to_expr(resolved))
            if resolved.scaling.formula:
                validate_expression(resolved.scaling.formula)
    effects = {effect.id: effect for effect in spell.passive_effects}
    for ref in getattr(activity, "effects", ()):
        if ref.id not in effects:
            raise ValueError("reaction references a missing effect")
        active = passive_effect_to_active_effect(
            effects[ref.id], target_id=ctx.caster.entity_id, caster_id=ctx.caster.entity_id, ctx=ctx
        )
        for change in active.changes:
            if change.key in ("ac.bonus", "system.attributes.ac.bonus"):
                scalar(parse_expression(str(change.value), allow_dice=False))


def _declaration(live: _LiveCombat, actor: Combatant, intent: PlayerIntent) -> PendingReaction:
    spell = get_lib_loader().get_spell(intent.spell_id or "")
    if spell is None:
        raise ValueError("unknown reaction spell")
    activity = _activity(spell, intent.activity_id)
    conditions = tuple(
        condition.model_copy(deep=True) for condition in activity.activation.reaction_conditions
    )
    if not compatible_trigger(intent.reaction_trigger, conditions):
        raise ValueError("compatibility trigger does not match canonical conditions")
    level = spell.level if intent.slot_level is None else intent.slot_level
    if level < spell.level or level > 9 or (spell.level > 0 and level < 1):
        raise ValueError("invalid reaction slot level")
    _validate_payload(spell, activity, _context(live, actor, [actor], spell, level, draw_free=True))
    assert activity.reaction is not None
    return PendingReaction(
        actor.entity_id,
        spell.slug,
        activity.id,
        level,
        conditions,
        activity.reaction.model_copy(deep=True),
    )


def prearm_failure(live: _LiveCombat, actor: Combatant, intent: PlayerIntent) -> CastFailed | None:
    if intent.intent_type == "ready":
        try:
            _declaration(live, actor, intent)
        except ValueError:
            return CastFailed(
                actor_id=actor.entity_id,
                spell_id=intent.spell_id or "",
                reason="unsupported_reaction",
            )
    # A damage-source target exists only at a damage opportunity. An explicit
    # on-turn cast cannot reconstruct it from a previous attacker's identity.
    if intent.intent_type == "cast_spell":
        spell = get_lib_loader().get_spell(intent.spell_id or "")
        if spell and any(
            a.reaction is not None and a.reaction.target_role == "damage_source"
            for a in spell.activities
        ):
            return CastFailed(
                actor_id=actor.entity_id, spell_id=spell.slug, reason="unsupported_reaction"
            )
    return None


def register_pending_reaction(live: _LiveCombat, actor_id: str, intent: PlayerIntent) -> None:
    from dnd5e_engine import orchestrator as orch

    if intent.intent_type != "ready":
        return
    actor = orch._find_combatant(live, actor_id)
    if actor is None:
        raise ValueError("reaction owner is not in combat")
    pending = _declaration(live, actor, intent)
    live.pending_reactions[:] = [p for p in live.pending_reactions if p.owner_id != actor_id]
    live.pending_reactions.append(pending)


def _condition_eligible(
    live: _LiveCombat,
    reactor: Combatant,
    spell: Spell,
    condition: ReactionCondition,
    opportunity: ReactionOpportunity,
) -> bool:
    from dnd5e_engine import orchestrator as orch

    subject_id = (
        opportunity.damage_source_actor_id
        if opportunity.kind == ReactionTriggerKind.TAKES_DAMAGE
        else opportunity.triggering_actor_id
    )
    subject = orch._find_combatant(live, subject_id) if subject_id else None
    if opportunity.kind in (
        ReactionTriggerKind.SEES_SPELL_CAST,
        ReactionTriggerKind.TAKES_DAMAGE,
    ):
        if subject is None or not subject.is_alive or subject.hp_current <= 0:
            return False
        if not orch._combatant_can_see(live, reactor, subject):
            return False
    distance = condition.max_range_ft
    if distance is None and opportunity.kind == ReactionTriggerKind.SEES_SPELL_CAST:
        distance = spell.range.value
    if distance is not None:
        origin, target = (
            live.actor_zone.get(reactor.entity_id),
            live.actor_zone.get(subject_id or ""),
        )
        if origin is None or target is None:
            return False
        if not live.topology.within_range(origin, target, int(distance)):
            return False
    return True


def _eligible(
    live: _LiveCombat,
    reactor: Combatant,
    pending: PendingReaction,
    opportunity: ReactionOpportunity,
) -> tuple[Spell, Activity, Combatant] | None:
    from dnd5e_engine import orchestrator as orch

    if not can_take_reaction(live, reactor, spell=True):
        return None
    if (
        opportunity.kind == ReactionTriggerKind.SEES_SPELL_CAST
        and reactor.entity_id == opportunity.triggering_actor_id
    ):
        return None
    spell = get_lib_loader().get_spell(pending.spell_id)
    if spell is None or not orch._slot_available(live, reactor.entity_id, pending.slot_level):
        return None
    try:
        activity = _activity(spell, pending.activity_id)
        conditions = matching_conditions(pending, opportunity)
        if not any(_condition_eligible(live, reactor, spell, c, opportunity) for c in conditions):
            return None
        target_id = reaction_target_id(pending, opportunity)
        target = orch._find_combatant(live, target_id) if target_id else None
        if target is None or not target.is_alive or target.entity_id in live.dead_ids:
            return None
        _validate_payload(
            spell,
            activity,
            _context(live, reactor, [target], spell, pending.slot_level, draw_free=True),
        )
    except ValueError:
        return None
    return spell, activity, target


def fire_reaction(live: _LiveCombat, opportunity: ReactionOpportunity) -> ReactionResult:
    from dnd5e_engine import orchestrator as orch
    from dnd5e_engine.timed_activities import begin_spell_cast

    # Nested reaction chains are deliberately bounded to zero nested releases.
    # Their opportunities may exist, but declarations remain armed for later.
    if live.reaction_resolution_depth:
        return ReactionResult()
    for reactor in tuple(live.initiative):
        pending = next((p for p in live.pending_reactions if p.owner_id == reactor.entity_id), None)
        if pending is None:
            continue
        candidate = _eligible(live, reactor, pending, opportunity)
        if candidate is None:
            continue
        spell, activity, target = candidate
        live.pending_reactions.remove(pending)
        reactor = pay_reaction(live, reactor)
        if spell.level > 0:
            orch._take_spell_slot(live, reactor.entity_id, pending.slot_level)
        orch._emit(
            live,
            ReactionTriggered(
                actor_id=reactor.entity_id,
                reaction_name=spell.slug,
                trigger_kind=opportunity.kind,
                triggering_actor_id=opportunity.triggering_actor_id,
                affected_target_id=opportunity.affected_target_id,
                source_spell_id=spell.slug,
                source_activity_id=activity.id,
                damage_instance_id=opportunity.damage_instance_id,
            ),
        )
        orch._emit_spell_cast(live, reactor.entity_id, spell, pending.slot_level)
        reactor = begin_spell_cast(live, reactor, spell)
        ctx = _context(live, reactor, [target], spell, pending.slot_level)
        ctx = attach_reaction_hooks(live, ctx)
        before = len(live.event_log)
        live.reaction_resolution_depth += 1
        try:
            resolve_activity(activity, ctx)
        finally:
            live.reaction_resolution_depth -= 1
        orch._fold_resolution_outcome(
            live,
            reactor,
            spell=spell,
            actx=ctx,
            pre_event_count=before,
            concentration_max_rounds=orch._concentration_max_rounds(spell),
        )
        orch._sync_legendary_resistance(live, before)
        emitted = live.event_log[before:]
        record_reaction_effects(live, ctx, activity, emitted)
        cancel_cast = False
        negated: frozenset[str] = frozenset()
        for response in pending.semantics.responses:
            if response.trigger_kind is not None and response.trigger_kind != opportunity.kind:
                continue
            if response.kind == "cancel_triggering_spell_on_failed_save":
                cancel_cast = any(
                    isinstance(event, SaveRolled)
                    and event.target_id == target.entity_id
                    and not event.succeeded
                    for event in emitted
                )
            elif response.kind == "negate_triggering_spell_damage":
                negated = frozenset({target.entity_id})
        return ReactionResult(cancel_cast, negated)
    return ReactionResult()


def record_reaction_effects(
    live: _LiveCombat,
    ctx: ActivityResolutionContext,
    activity: Activity,
    emitted: list[CombatEvent],
) -> None:
    semantics = activity.reaction
    if semantics is None:
        return
    caster_id = ctx.caster.entity_id
    referenced_ids = {ref.id for ref in getattr(activity, "effects", ())}
    # Canonical references carry Foundry ids; emitted effects use the shared
    # name-derived runtime id and origin. Reuse that conversion rather than
    # comparing two different identity namespaces or duplicating its slug rules.
    identities = {
        (converted.id, converted.origin)
        for passive in ctx.source_passive_effects
        if passive.id in referenced_ids
        for converted in [
            passive_effect_to_active_effect(passive, target_id=caster_id, caster_id=caster_id)
        ]
    }
    for event in emitted:
        if not isinstance(event, EffectApplied):
            continue
        if (event.effect.id, event.effect.origin) not in identities:
            continue
        effect = event.effect
        identity = (effect.target_id, effect.id, effect.origin)
        if semantics.responses and not any(
            response.effect_identity == identity for response in live.active_reaction_responses
        ):
            live.active_reaction_responses.append(
                ActiveReactionResponse(
                    effect.target_id,
                    identity,
                    tuple(activity.activation.reaction_conditions),
                    tuple(semantics.responses),
                )
            )
        if semantics.effect_expiry == "owner_next_turn_start":
            pending = live.reaction_effects_pending_expiry.setdefault(caster_id, [])
            if identity not in pending:
                pending.append(identity)


def spell_cast_opportunity(live: _LiveCombat, caster: Combatant, spell: Spell) -> bool:
    from dnd5e_engine import orchestrator as orch

    result = fire_reaction(
        live,
        ReactionOpportunity(
            ReactionTriggerKind.SEES_SPELL_CAST,
            caster.entity_id,
            triggering_spell_slug=spell.slug,
        ),
    )
    if result.cancel_cast:
        orch._emit(
            live, CastFailed(actor_id=caster.entity_id, spell_id=spell.slug, reason="countered")
        )
    return result.cancel_cast


def targeted_spell_opportunities(
    live: _LiveCombat, caster: Combatant, spell: Spell, targets: list[Combatant]
) -> frozenset[str]:
    negated: set[str] = set()
    for target in targets:
        for active in live.active_reaction_responses:
            if active.owner_id != target.entity_id:
                continue
            if any(
                condition.kind == ReactionTriggerKind.TARGETED_BY_SPELL
                and condition.target_spell_slug == spell.slug
                for condition in active.conditions
            ) and any(
                response.kind == "negate_triggering_spell_damage"
                and response.trigger_kind in (None, ReactionTriggerKind.TARGETED_BY_SPELL)
                for response in active.responses
            ):
                negated.add(target.entity_id)
        result = fire_reaction(
            live,
            ReactionOpportunity(
                ReactionTriggerKind.TARGETED_BY_SPELL,
                caster.entity_id,
                affected_target_id=target.entity_id,
                triggering_spell_slug=spell.slug,
            ),
        )
        negated.update(result.negated_damage_targets)
    return frozenset(negated)


def observe_reaction_lifecycle(live: _LiveCombat, event: CombatEvent) -> None:
    if isinstance(event, EffectExpired):
        identity = (event.target_id, event.effect_id, event.origin)
        live.active_reaction_responses[:] = [
            response
            for response in live.active_reaction_responses
            if response.effect_identity != identity
        ]
    elif isinstance(event, (Death, CombatantLeft)):
        owner_id = event.target_id if isinstance(event, Death) else event.entity_id
        live.pending_reactions[:] = [p for p in live.pending_reactions if p.owner_id != owner_id]
        live.active_reaction_responses[:] = [
            response for response in live.active_reaction_responses if response.owner_id != owner_id
        ]


def can_continue_resolution(live: _LiveCombat, actor_id: str) -> bool:
    from dnd5e_engine import orchestrator as orch

    actor = orch._find_combatant(live, actor_id)
    return (
        actor is not None
        and actor.is_alive
        and actor.hp_current > 0
        and actor_id not in live.dead_ids
        and not conditions_block_actions(orch._condition_names(actor))
    )


def _ac_changes(effects: list[ActiveEffect]) -> Counter[str]:
    return Counter(
        str(change.value)
        for effect in effects
        for change in effect.changes
        if change.key in ("ac.bonus", "system.attributes.ac.bonus") and change.mode == "add"
    )


def _attack_opportunity(
    live: _LiveCombat, attack: AttackHitContext, baseline: dict[str, Counter[str]]
) -> int:
    before = baseline.get(attack.target_id, Counter())
    fire_reaction(
        live,
        ReactionOpportunity(
            ReactionTriggerKind.HIT_BY_ATTACK,
            attack.attacker_id,
            affected_target_id=attack.target_id,
            source_activity_id=attack.source_activity_id,
            attack=attack,
        ),
    )
    after = _ac_changes(live.active_effects.get(attack.target_id, []))
    # Existing AC dice were already evaluated for this attack. Only newly
    # applied scalar changes adjust its AC; they never repeat those draws.
    delta = sum(
        scalar(parse_expression(value, allow_dice=False)) * count
        for value, count in (after - before).items()
    )
    delta -= sum(
        scalar(parse_expression(value, allow_dice=False)) * count
        for value, count in (before - after).items()
    )
    return attack.effective_ac + delta


def _damage_opportunity(live: _LiveCombat, damage: DamageInstanceContext) -> None:
    from dnd5e_engine.live_effect_lifecycle import damage_instance_completed

    damage_instance_completed(live, damage)
    if damage.amount <= 0:
        return
    fire_reaction(
        live,
        ReactionOpportunity(
            ReactionTriggerKind.TAKES_DAMAGE,
            damage.source_actor_id,
            affected_target_id=damage.target_id,
            source_activity_id=damage.source_id,
            damage_source_actor_id=damage.source_actor_id,
            damage_instance_id=damage.damage_instance_id,
        ),
    )


def attach_reaction_hooks(
    live: _LiveCombat, ctx: ActivityResolutionContext
) -> ActivityResolutionContext:
    from dnd5e_engine.live_attack_riders import attach_attack_riders
    from dnd5e_engine.live_save_modifiers import consume_next_save_modifier

    baseline = {
        target.entity_id: _ac_changes(live.active_effects.get(target.entity_id, []))
        for target in [*ctx.targets, *([ctx.cleave_candidate] if ctx.cleave_candidate else [])]
    }

    def next_instance(target_id: str, source_id: str | None) -> str:
        live.damage_instance_sequence += 1
        return f"damage:{live.damage_instance_sequence}:{ctx.caster.entity_id}:{target_id}"

    return attach_attack_riders(
        live,
        replace(
            ctx,
            attack_hit_reaction=lambda attack: _attack_opportunity(live, attack, baseline),
            damage_instance_id_provider=next_instance,
            damage_instance_resolved=lambda damage: _damage_opportunity(live, damage),
            consume_next_save_modifier=lambda target_id: consume_next_save_modifier(
                live, target_id
            ),
            attack_continuation_allowed=lambda: can_continue_resolution(live, ctx.caster.entity_id),
        ),
    )
