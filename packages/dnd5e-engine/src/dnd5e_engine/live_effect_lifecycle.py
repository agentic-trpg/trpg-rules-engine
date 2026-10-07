"""Live effect lifecycle adapters, independent of spell and feature identities."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Literal

from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.save_primitive import SaveRoll, roll_save
from dnd5e_engine.effect_lifecycle import EffectIdentity, OngoingEffectLifecycle
from dnd5e_engine.events import (
    CombatEvent,
    EffectExpired,
    EffectModifiersConsumed,
    LegendaryResistanceUsed,
    SaveRolled,
)

if TYPE_CHECKING:
    from dnd5e_engine.activities.context import DamageInstanceContext
    from dnd5e_engine.events import EffectExpiryReason
    from dnd5e_engine.orchestrator import _LiveCombat
    from dnd5e_engine.types.combat import Combatant
    from dnd5e_engine.types.effects import ActiveEffect


def register_effect(live: _LiveCombat, effect: ActiveEffect) -> None:
    """Register explicit metadata only after actual attachment, without RNG."""
    app = effect.lifecycle
    identity = (effect.target_id, effect.id, effect.origin)
    if app is None or effect.disabled or effect.target_id in live.dead_ids:
        return
    if effect.statuses and not live.conditions_by_effect.get(identity):
        return
    live.effect_lifecycles[identity] = OngoingEffectLifecycle.from_application(
        identity, app, live.turn_serial, live.round_number
    )


def forget_effect(live: _LiveCombat, identity: EffectIdentity) -> bool:
    """Idempotent full-identity cleanup, including concentration target links."""
    managed = live.effect_lifecycles.pop(identity, None) is not None
    for source_id, chain in list(live.concentration_chain.items()):
        if identity not in chain:
            continue
        remaining = [entry for entry in chain if entry != identity]
        if remaining:
            live.concentration_chain[source_id] = remaining
        else:
            live.concentration_chain.pop(source_id, None)
            live.concentration_rounds_remaining.pop(source_id, None)
            from dnd5e_engine import orchestrator as orch

            orch._update_combatant(live, source_id, concentration_effect_id=None)
    return managed


def observe_modifier_consumption(live: _LiveCombat, event: CombatEvent) -> None:
    if not isinstance(event, EffectModifiersConsumed):
        return
    identity = (event.target_id, event.effect_id, event.origin)
    state = live.effect_lifecycles.get(identity)
    if state is not None and "flags.save.next_disadvantage" in event.keys:
        live.effect_lifecycles[identity] = replace(
            state,
            remaining_one_use_modifiers=tuple(
                modifier
                for modifier in state.remaining_one_use_modifiers
                if modifier != "next_save_disadvantage"
            ),
        )


def expire_effect(live: _LiveCombat, identity: EffectIdentity, reason: EffectExpiryReason) -> None:
    from dnd5e_engine import orchestrator as orch

    if identity not in live.effect_lifecycles:
        return
    target_id, effect_id, origin = identity
    orch._emit(
        live, EffectExpired(target_id=target_id, effect_id=effect_id, origin=origin, reason=reason)
    )


def roll_live_save(
    live: _LiveCombat, target: Combatant, ability: str, dc: int, *, is_magical: bool = False
) -> SaveRoll:
    """Fresh target defenses, shared activity primitive, captured source magic."""
    from dnd5e_engine import orchestrator as orch
    from dnd5e_engine.live_reactions import attach_reaction_hooks

    payload = orch._build_hydration_payload(live, caster=target)
    # AC and source spell DC bonuses are unrelated to this target's save and
    # may contain dice; do not eagerly evaluate them or shift the draw stream.
    saves = {
        key: {k: v for k, v in values.items() if k != "passive_ac_bonus"}
        for key, values in payload.get("save_modifiers", {}).items()
    }
    damage = {
        key: {k: v for k, v in values.items() if k != "passive_spell_dc_bonus"}
        for key, values in payload.get("passive_damage_modifiers", {}).items()
    }
    ctx = build_activity_context(
        target,
        [target],
        rng=live.rng,
        event_emitter=lambda event: orch._emit(live, event),
        slot_level=None,
        base_spell_level=None,
        spellcasting_ability=None,
        concentration=False,
        source_passive_effects=[],
        spell_book={},
        save_modifiers=saves,
        passive_damage_modifiers=damage,
        d20_test_penalty=payload["d20_test_penalty"],
        legendary_resistance_armed=payload["legendary_resistance_armed"],
        legendary_resistances_remaining_by_entity=payload[
            "legendary_resistances_remaining_by_entity"
        ],
    )
    ctx = attach_reaction_hooks(live, replace(ctx, save_is_magical=is_magical))
    result = roll_save(ctx, target, ability, dc, ignore_cover=True)
    return result


def run_repeats(live: _LiveCombat, actor_id: str) -> None:
    from dnd5e_engine import orchestrator as orch

    target = orch._find_combatant(live, actor_id)
    if target is None or actor_id in live.dead_ids:
        return
    for identity in list(live.effect_lifecycles):
        state = live.effect_lifecycles.get(identity)
        if state is None or not state.repeats_at(actor_id, live.turn_serial):
            continue
        app = state.application
        assert app.save_ability is not None
        assert app.save_dc is not None
        live.effect_lifecycles[identity] = state.repeated(live.turn_serial)
        result = roll_live_save(
            live, target, app.save_ability, app.save_dc, is_magical=app.is_magical
        )
        orch._emit(
            live,
            SaveRolled(
                target_id=actor_id,
                ability=app.save_ability,
                dc=app.save_dc,
                roll_total=result.total,
                succeeded=result.succeeded,
                advantage=result.mode,
                natural=result.natural,
                modifier=result.modifier,
                sources=list(result.sources),
            ),
        )
        if result.legendary_resistance_remaining is not None:
            orch._emit(
                live,
                LegendaryResistanceUsed(
                    actor_id=actor_id, uses_remaining=result.legendary_resistance_remaining
                ),
            )
            orch._sync_legendary_resistance(live, len(live.event_log) - 1)
        if result.succeeded:
            expire_effect(live, identity, "save_succeeded")
        target = orch._find_combatant(live, actor_id) or target


def expire_at_boundary(
    live: _LiveCombat, actor_id: str | None, phase: Literal["start", "end"]
) -> None:
    if actor_id is None:
        return
    for identity, state in list(live.effect_lifecycles.items()):
        if state.expires_at(actor_id, phase, live.turn_serial, live.round_number):
            expire_effect(live, identity, "duration")


def record_damage(live: _LiveCombat, target_id: str, instance_id: str | None, amount: int) -> None:
    """Record effective post-absorption damage, not raw damage-type events."""
    if instance_id is None:
        if amount > 0:
            expire_damaged(live, target_id)
    else:
        key = (target_id, instance_id)
        live.lifecycle_damage[key] = live.lifecycle_damage.get(key, 0) + max(0, amount)


def damage_instance_completed(live: _LiveCombat, damage: DamageInstanceContext) -> None:
    amount = live.lifecycle_damage.pop((damage.target_id, damage.damage_instance_id), 0)
    if amount > 0:
        expire_damaged(live, damage.target_id)


def expire_damaged(live: _LiveCombat, target_id: str) -> None:
    for identity, state in list(live.effect_lifecycles.items()):
        if identity[0] == target_id and state.application.spec.expire_on_positive_damage:
            expire_effect(live, identity, "damaged")


def actor_departed(live: _LiveCombat, actor_id: str) -> None:
    """Targets end; only an explicitly source-bound timer follows its source."""
    for identity, state in list(live.effect_lifecycles.items()):
        source_boundary = state.application.spec.expiry_boundary
        if identity[0] == actor_id or (
            state.application.source_id == actor_id
            and source_boundary is not None
            and source_boundary.startswith("source_")
        ):
            expire_effect(live, identity, "remove_ieffect")
    for key in [key for key in live.lifecycle_damage if key[0] == actor_id]:
        live.lifecycle_damage.pop(key, None)
