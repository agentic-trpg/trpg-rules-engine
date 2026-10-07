"""Typed spell activity scheduling at turn boundaries, independent of dispatch.

Registration order (cast, canonical activity, target) is the stable execution
order. Source magnitudes are captured at cast time; target defenses are hydrated
at execution. No spell slugs, activity names or prose drive runtime behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from dnd5e_srd_data.schema.common import Activity, ActivityTiming

from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.context import ActivityResolutionContext
from dnd5e_engine.activities.effects import passive_effect_to_active_effect
from dnd5e_engine.activities.resolver import resolve_activity
from dnd5e_engine.events import (
    CombatantLeft,
    CombatEvent,
    ConcentrationDropped,
    Death,
    EffectApplied,
    EffectExpired,
    SaveRolled,
)

if TYPE_CHECKING:
    from dnd5e_srd_data.schema.spell import Spell

    from dnd5e_engine.orchestrator import PlayerIntent, _LiveCombat
    from dnd5e_engine.types.combat import Combatant
    from dnd5e_engine.types.effects import ActiveEffect, ActiveEffectDuration

EffectIdentity = tuple[str, str, str]


@dataclass(frozen=True)
class PendingTimedActivity:
    """One target-bound execution record. Area ownership lives exclusively in
    PersistentAreaState, including recurring boundary activities.
    """

    sequence: int
    spell: Spell
    activity: Activity
    caster: Combatant
    target_id: str
    slot_level: int | None
    spellcasting_ability: str | None
    save_dc_override: int | None
    timing: ActivityTiming
    not_before_turn: int
    effect_identity: EffectIdentity | None
    duration: ActiveEffectDuration | None
    concentration: bool
    concentration_identity: EffectIdentity | None


@dataclass
class TimedActivityState:
    """Per-combat ordered state. All cancellation is idempotent and RNG-free."""

    pending: list[PendingTimedActivity] = field(default_factory=list)
    next_sequence: int = 0

    def observe(self, event: CombatEvent) -> None:
        if isinstance(event, EffectExpired):
            identity = (event.target_id, event.effect_id, event.origin)
            self.pending[:] = [
                p
                for p in self.pending
                if p.effect_identity != identity and p.concentration_identity != identity
            ]
        elif isinstance(event, ConcentrationDropped):
            self.pending[:] = [
                p
                for p in self.pending
                if not (p.concentration and p.caster.entity_id == event.target_id)
            ]
        elif isinstance(event, (Death, CombatantLeft)):
            entity_id = event.target_id if isinstance(event, Death) else event.entity_id
            self.pending[:] = [
                p
                for p in self.pending
                if p.target_id != entity_id
                and not (p.concentration and p.caster.entity_id == entity_id)
            ]

    def owns_repeat(self, identity: EffectIdentity) -> bool:
        return any(p.effect_identity == identity for p in self.pending)

    def waiting_for_next_turn(self, identity: EffectIdentity, turn: int) -> bool:
        return any(p.effect_identity == identity and p.not_before_turn > turn for p in self.pending)


def begin_spell_cast(live: _LiveCombat, caster: Combatant, spell: Spell) -> Combatant:
    """End old concentration when a legal new concentration cast begins,
    including a subsequently countered cast. Never called for refused intents.
    """
    from dnd5e_engine import orchestrator as orch

    if spell.concentration:
        orch._drop_concentration(live, caster.entity_id)
    return orch._find_combatant(live, caster.entity_id) or caster


def resolve_spell_activities(
    live: _LiveCombat,
    spell: Spell,
    ctx: ActivityResolutionContext,
    *,
    intent: PlayerIntent | None = None,
    area_origin: str | None = None,
) -> None:
    """Resolve only immediate activities, then register deferred work.

    Effect attachment events determine eligibility, so only failed saves/hits
    carrying the named source effect schedule its later damage. Immediate
    source order is preserved, even when Foundry lists deferred work first.
    """
    from dnd5e_engine import orchestrator as orch
    from dnd5e_engine.live_reactions import (
        attach_reaction_hooks,
        can_continue_resolution,
        record_reaction_effects,
        targeted_spell_opportunities,
    )
    from dnd5e_engine.persistent_areas import register_area
    from dnd5e_engine.types.effects import ActiveEffectDuration

    if ctx.attack_hit_reaction is None:
        ctx = attach_reaction_hooks(live, ctx)
    ctx = replace(
        ctx,
        negated_spell_damage_targets=targeted_spell_opportunities(
            live, ctx.caster, spell, ctx.targets
        ),
    )
    before = len(live.event_log)
    for activity in spell.activities:
        if not can_continue_resolution(live, ctx.caster.entity_id):
            break
        if activity.persistent_area is not None:
            register_area(
                live,
                activity,
                ctx,
                source_id=spell.slug,
                spell=spell,
                intent=intent,
                origin=area_origin,
            )
        elif activity.timing.trigger == "immediate":
            resolve_activity(activity, ctx)
    emitted = live.event_log[before:]
    for activity in spell.activities:
        record_reaction_effects(live, ctx, activity, emitted)
    applied = [e.effect for e in emitted if isinstance(e, EffectApplied)]
    for activity in spell.activities:
        if activity.persistent_area is not None:
            continue
        timing = activity.timing
        if timing.trigger not in ("turn_start", "turn_end") or timing.subject == "area":
            continue
        for target in ctx.targets:
            linked = _linked_effect(spell, ctx, timing, target, applied)
            if timing.effect_id is not None and linked is None:
                continue
            identity = (linked.target_id, linked.id, linked.origin) if linked else None
            if timing.requires_condition and (
                identity is None
                or timing.requires_condition not in live.conditions_by_effect.get(identity, [])
            ):
                continue
            if not target.is_alive or target.entity_id in live.dead_ids:
                continue
            state = live.timed_activities
            state.pending.append(
                PendingTimedActivity(
                    sequence=state.next_sequence,
                    spell=spell,
                    activity=activity,
                    caster=ctx.caster,
                    target_id=target.entity_id,
                    slot_level=ctx.slot_level,
                    spellcasting_ability=ctx.spellcasting_ability,
                    save_dc_override=ctx.save_dc_override,
                    timing=timing,
                    not_before_turn=live.turn_serial + int(timing.next_turn),
                    effect_identity=identity,
                    duration=linked.duration
                    if linked
                    else ActiveEffectDuration(rounds=orch._concentration_max_rounds(spell)),
                    concentration=spell.concentration,
                    concentration_identity=(
                        identity or orch._anchor_identity(spell.slug, ctx.caster.entity_id)
                    )
                    if spell.concentration
                    else None,
                )
            )
            state.next_sequence += 1


def _linked_effect(
    spell: Spell,
    ctx: ActivityResolutionContext,
    timing: ActivityTiming,
    target: Combatant | None,
    applied: list[ActiveEffect],
) -> ActiveEffect | None:
    if timing.effect_id is None or target is None:
        return None
    source = next((p for p in spell.passive_effects if p.id == timing.effect_id), None)
    if source is None:
        return None
    expected = passive_effect_to_active_effect(
        source,
        target_id=target.entity_id,
        caster_id=ctx.caster.entity_id,
    )
    return next(
        (
            e
            for e in applied
            if (e.target_id, e.id, e.origin) == (expected.target_id, expected.id, expected.origin)
        ),
        None,
    )


def _eligible(live: _LiveCombat, pending: PendingTimedActivity, actor_id: str) -> bool:
    if pending.not_before_turn > live.turn_serial:
        return False
    subject = pending.caster.entity_id if pending.timing.subject == "caster" else pending.target_id
    return subject == actor_id


def run_timed_activities(live: _LiveCombat, phase: str, actor_id: str | None) -> None:
    """Run a stable snapshot; cancellation from an earlier activity takes effect
    before a later activity can draw RNG, including death/concentration cascades.
    """
    from dnd5e_engine import orchestrator as orch

    if actor_id is None:
        return
    for pending in tuple(live.timed_activities.pending):
        if pending not in live.timed_activities.pending or pending.timing.trigger != phase:
            continue
        if not _eligible(live, pending, actor_id):
            continue
        target = orch._find_combatant(live, pending.target_id)
        caster = orch._find_combatant(live, pending.caster.entity_id)
        if target is None or target.entity_id in live.dead_ids:
            live.timed_activities.pending.remove(pending)
            continue
        if pending.concentration and not live.concentration_chain.get(pending.caster.entity_id):
            live.timed_activities.pending.remove(pending)
            continue
        _execute(live, pending, target, caster or pending.caster)


def _execute(
    live: _LiveCombat, pending: PendingTimedActivity, target: Combatant, caster: Combatant
) -> None:
    from dnd5e_engine import orchestrator as orch
    from dnd5e_engine.live_reactions import attach_reaction_hooks

    before = len(live.event_log)
    payload = orch._build_hydration_payload(live, caster=caster)
    ctx = build_activity_context(
        pending.caster,
        [target],
        rng=live.rng,
        event_emitter=lambda e: orch._emit(live, e),
        slot_level=pending.slot_level if pending.timing.scale_with_slot else pending.spell.level,
        base_spell_level=pending.spell.level,
        spellcasting_ability=pending.spellcasting_ability,
        save_dc_override=pending.save_dc_override,
        concentration=pending.concentration and pending.timing.effects_concentration,
        source_passive_effects=pending.spell.passive_effects,
        spell_book=orch._build_cast_spell_book(pending.spell.activities),
        **orch._monster_context_kwargs(live, caster, [target], payload),
    )
    ctx = attach_reaction_hooks(live, ctx)
    resolve_activity(pending.activity, ctx)
    orch._sync_legendary_resistance(live, before)
    if pending.concentration and live.concentration_chain.get(caster.entity_id):
        chain = live.concentration_chain[caster.entity_id]
        for event in live.event_log[before:]:
            if isinstance(event, EffectApplied):
                effect = event.effect
                identity = (effect.target_id, effect.id, effect.origin)
                if identity not in chain:
                    chain.append(identity)
    succeeded = any(isinstance(e, SaveRolled) and e.succeeded for e in live.event_log[before:])
    if succeeded and pending.timing.ends_effect_on_success and pending.effect_identity:
        target_id, effect_id, origin = pending.effect_identity
        chain = live.concentration_chain.get(caster.entity_id, [])
        if pending.concentration and chain == [pending.effect_identity]:
            orch._drop_concentration(live, caster.entity_id, reason="duration")
        else:
            orch._emit(
                live,
                EffectExpired(
                    target_id=target_id, effect_id=effect_id, origin=origin, reason="duration"
                ),
            )
            if pending.effect_identity in chain:
                chain.remove(pending.effect_identity)
    if not pending.timing.recurring and pending in live.timed_activities.pending:
        live.timed_activities.pending.remove(pending)


def register_timed_activity_hooks(live: _LiveCombat) -> None:
    live.lifecycle.register(
        "turn_start",
        lambda combat, actor: run_timed_activities(combat, "turn_start", actor),
        key="engine:timed-activities-start",
    )
    live.lifecycle.register(
        "turn_end",
        lambda combat, actor: run_timed_activities(combat, "turn_end", actor),
        key="engine:timed-activities-end",
    )
