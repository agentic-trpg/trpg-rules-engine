"""Live adapters for canonical attack riders and shared effect consumers."""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from typing import TYPE_CHECKING, Any, Literal, cast

from dnd5e_engine.activities.arithmetic import parse_expression, scalar
from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.context import AttackResolutionContext, AttackRollModifier
from dnd5e_engine.activities.effects import (
    applicable_effect_statuses,
    passive_effect_to_active_effect,
)
from dnd5e_engine.activities.resolver import resolve_activity
from dnd5e_engine.attack_riders import AttackRiderPlan, matches_rider, plan_attack_riders
from dnd5e_engine.events import (
    AttackFailed,
    AttackRiderTriggered,
    ConditionApplied,
    EffectApplied,
    EffectExpired,
    EffectModifiersConsumed,
    RiderResourceSpent,
    SaveRolled,
)
from dnd5e_engine.feature_repertoire import feature_repertoire
from dnd5e_engine.feature_runtime import DrawFreeRandom, FeaturePreflightError
from dnd5e_engine.lib_loader import get_lib_loader
from dnd5e_engine.live_features import validate_feature_sidecars
from dnd5e_engine.spatial import GridTopology
from dnd5e_engine.types.effects import ActiveEffectDuration

if TYPE_CHECKING:
    from dnd5e_srd_data.schema.feature import Feature
    from dnd5e_srd_data.schema.item import Weapon

    from dnd5e_engine.activities.context import ActivityResolutionContext, AttackOrigin
    from dnd5e_engine.events import CombatEvent, ConditionType
    from dnd5e_engine.orchestrator import AttackFunding, PlayerIntent, _LiveCombat
    from dnd5e_engine.types.combat import Combatant


def _context(
    live: _LiveCombat,
    actor: Combatant,
    targets: list[Combatant],
    feature: Feature | None = None,
    *,
    preflight: bool = False,
) -> ActivityResolutionContext:
    from dnd5e_engine import orchestrator as orch

    payload = orch._build_hydration_payload(live, caster=actor)
    # These eager context bonuses concern attack AC / spellcasting DC. A
    # feature save uses its canonical ability, and preflight draws no dice.
    saves = {
        key: {k: v for k, v in entry.items() if k != "passive_ac_bonus"}
        for key, entry in payload.get("save_modifiers", {}).items()
    }
    damage = {
        key: {k: v for k, v in entry.items() if k != "passive_spell_dc_bonus"}
        for key, entry in payload.get("passive_damage_modifiers", {}).items()
    }
    return build_activity_context(
        actor,
        targets,
        rng=DrawFreeRandom(0) if preflight else live.rng,
        event_emitter=lambda event: orch._emit(live, event),
        slot_level=None,
        base_spell_level=None,
        spellcasting_ability=None,
        concentration=False,
        source_passive_effects=list(feature.passive_effects) if feature else [],
        spell_book={},
        passive_damage_modifiers=damage,
        save_modifiers=saves,
        scale_values=orch._scale_values_of(actor),
        class_levels=orch._class_levels(actor),
        is_feature_invocation=True,
    )


def preflight_attack_riders(
    live: _LiveCombat,
    actor: Combatant,
    intent: PlayerIntent,
    weapon: Weapon | None,
    origin: AttackOrigin,
) -> tuple[AttackRiderPlan, ...]:
    from dnd5e_engine import orchestrator as orch

    if not intent.attack_riders:
        return ()
    if intent.intent_type != "attack":
        raise FeaturePreflightError("riders require an attack intent")
    validate_feature_sidecars(live, actor)
    if any(
        request.push_distance_ft is not None for request in intent.attack_riders
    ) and not isinstance(live.topology, GridTopology):
        raise FeaturePreflightError("rider forced movement requires grid geometry")
    owners = feature_repertoire(actor, get_lib_loader())
    target = orch._find_combatant(live, intent.target_id) if intent.target_id else None
    used = {
        slug
        for entity, slug, serial in live.rider_uses
        if entity == actor.entity_id and serial == live.turn_serial
    }
    return plan_attack_riders(
        actor,
        intent.attack_riders,
        weapon=weapon,
        origin=origin,
        ctx=_context(live, actor, [], preflight=True),
        loader=get_lib_loader(),
        spent={
            owner.slug: orch._feature_use_spent(live, actor.entity_id, owner.slug)
            for owner in owners
        },
        used_features=used,
        cell_size_ft=live.topology.cell_size_ft if isinstance(live.topology, GridTopology) else 1,
        target_size=target.creature_size if target is not None else None,
    )


def attack_origin(
    intent: PlayerIntent, weapon: Weapon | None, funding: AttackFunding
) -> AttackOrigin:
    if funding == "construct_bonus":
        return "construct"
    if intent.stat_block_action_id:
        return "monster"
    if intent.intent_type == "cast_spell":
        return "spell"
    if funding == "light_offhand" and weapon is not None and weapon.mastery == "nick":
        return "nick"
    return funding


def _reject(live: _LiveCombat, actor_id: str, intent: PlayerIntent) -> None:
    from dnd5e_engine import orchestrator as orch

    orch._emit(
        live,
        AttackFailed(actor_id=actor_id, target_id=intent.target_id, reason="unsupported_rider"),
    )


def reject_nonattack_riders(live: _LiveCombat, actor_id: str, intent: PlayerIntent) -> bool:
    if intent.attack_riders and intent.intent_type != "attack":
        _reject(live, actor_id, intent)
        return True
    return False


def prepare_intent_riders(
    live: _LiveCombat,
    actor: Combatant,
    intent: PlayerIntent,
    weapon: Weapon | None,
    origin: AttackOrigin,
) -> tuple[AttackRiderPlan, ...] | None:
    try:
        return preflight_attack_riders(live, actor, intent, weapon, origin)
    except (ValueError, ZeroDivisionError):
        _reject(live, actor.entity_id, intent)
        return None


def _enabled(value: bool | int | str) -> bool:
    return value is True or value == "true"


def rider_speed(live: _LiveCombat, actor_id: str, speed: int) -> int:
    """Project generic speed changes without inspecting feature identities."""
    # Repeated instances of the same canonical feature effect do not multiply
    # its potency. Independent origins retain their own expiry/lineage.
    latest = {
        effect.id: effect for effect in live.active_effects.get(actor_id, []) if not effect.disabled
    }
    for effect in latest.values():
        for change in effect.changes:
            if change.key == "speed.multiplier" and change.mode == "multiply":
                speed = int(speed * Fraction(str(change.value)))
            elif change.key == "speed.reduction" and change.mode == "add":
                speed -= scalar(parse_expression(str(change.value), allow_dice=False))
    return max(0, speed)


def prevents_opportunity_attacks(live: _LiveCombat, actor_id: str) -> bool:
    return any(
        change.key == "flags.cannot_make_opportunity_attacks"
        and change.mode == "override"
        and _enabled(change.value)
        for effect in live.active_effects.get(actor_id, [])
        if not effect.disabled
        for change in effect.changes
    )


def observe_rider_event(live: _LiveCombat, event: CombatEvent) -> None:
    if not isinstance(event, EffectModifiersConsumed):
        return
    effects = live.active_effects.get(event.target_id, [])
    for index, effect in enumerate(effects):
        if effect.id == event.effect_id and effect.origin == event.origin:
            effects[index] = effect.model_copy(
                update={
                    "changes": [change for change in effect.changes if change.key not in event.keys]
                }
            )
            break


def expire_rider_effects(
    live: _LiveCombat, actor_id: str | None, phase: Literal["start", "end"]
) -> None:
    from dnd5e_engine import orchestrator as orch

    for target_id, effects in list(live.active_effects.items()):
        for effect in list(effects):
            flags = effect.flags
            if (
                flags.get("rider_expiry_actor_id") == actor_id
                and flags.get("rider_expiry_phase") == phase
                and live.turn_serial > flags.get("rider_applied_turn_serial", live.turn_serial)
            ):
                orch._emit(
                    live,
                    EffectExpired(
                        target_id=target_id,
                        effect_id=effect.id,
                        origin=effect.origin,
                        reason="duration",
                    ),
                )


def register_rider_hooks(live: _LiveCombat) -> None:
    live.lifecycle.register(
        "turn_end",
        lambda combat, actor: expire_rider_effects(combat, actor, "end"),
        key="engine:attack-rider-expiry",
    )


def _roll_modifier(live: _LiveCombat, attacker_id: str, target_id: str) -> AttackRollModifier:
    from dnd5e_engine import orchestrator as orch

    advantage = False
    bonuses: dict[str, int] = {}
    for effect in list(live.active_effects.get(target_id, [])):
        if effect.disabled:
            continue
        keys: list[Literal["flags.attack.next_advantage", "attack.next_bonus"]] = []
        for change in effect.changes:
            if (
                change.key == "flags.attack.next_advantage"
                and change.mode == "override"
                and _enabled(change.value)
            ):
                advantage = True
                keys.append("flags.attack.next_advantage")
            elif change.key == "attack.next_bonus" and change.mode == "add":
                bonuses[effect.id] = scalar(parse_expression(str(change.value), allow_dice=False))
                keys.append("attack.next_bonus")
        if keys:
            orch._emit(
                live,
                EffectModifiersConsumed(
                    target_id=target_id, effect_id=effect.id, origin=effect.origin, keys=tuple(keys)
                ),
            )
    return AttackRollModifier(
        advantage_sources=("feature_rider",) if advantage else (), flat_bonus=sum(bonuses.values())
    )


def _pay(live: _LiveCombat, attack: AttackResolutionContext, plan: AttackRiderPlan) -> bool:
    from dnd5e_engine import orchestrator as orch

    key = (attack.attacker_id, plan.feature.slug, live.turn_serial)
    if plan.semantics.once_per_turn and key in live.rider_uses:
        return False
    if any(
        orch._feature_use_spent(live, attack.attacker_id, p.feature_slug) + p.cost > p.maximum
        for p in plan.payments
    ):
        return False
    for payment in plan.payments:
        orch._increment_feature_use(live, attack.attacker_id, payment.feature_slug, payment.cost)
    if plan.semantics.once_per_turn:
        live.rider_uses.add(key)
    return True


def _resolve(live: _LiveCombat, attack: AttackResolutionContext, plan: AttackRiderPlan) -> None:
    from dnd5e_engine import orchestrator as orch
    from dnd5e_engine.activities.effects import bind_effect_lifecycle
    from dnd5e_engine.live_reactions import attach_reaction_hooks

    actor = orch._find_combatant(live, attack.attacker_id)
    target = orch._find_combatant(live, attack.target_id)
    if actor is None or target is None:
        return
    ctx = attach_reaction_hooks(live, _context(live, actor, [target], plan.feature))
    start = len(live.event_log)
    # Foundry on_save does not distinguish success-only from failure-only.
    # Reviewed outcome bindings below own all effects of this rider.
    resolve_activity(plan.activity.model_copy(update={"effects": []}), ctx)
    save = next(
        (
            event
            for event in live.event_log[start:]
            if isinstance(event, SaveRolled) and event.target_id == target.entity_id
        ),
        None,
    )
    outcome: Literal["success", "failure"] | None = (
        ("success" if save.succeeded else "failure") if save else None
    )
    for binding in plan.semantics.effects:
        if binding.outcome != "always" and binding.outcome != outcome:
            continue
        passive = next(
            effect for effect in plan.feature.passive_effects if effect.id == binding.effect_id
        )
        effect = passive_effect_to_active_effect(
            passive, target_id=target.entity_id, caster_id=actor.entity_id, ctx=ctx
        )
        flags = dict(effect.flags)
        updates: dict[str, Any] = {
            "id": f"effect:rider:{plan.feature.slug}:{binding.effect_id}",
            "origin": f"rider:{plan.feature.slug}:{plan.activity.id}:{actor.entity_id}",
        }
        if binding.expiry != "none":
            flags.update(
                rider_expiry_actor_id=actor.entity_id
                if binding.expiry == "source_next_turn_start"
                else target.entity_id,
                rider_expiry_phase="end" if binding.expiry == "target_next_turn_end" else "start",
                rider_applied_turn_serial=live.turn_serial,
            )
            updates.update(duration=ActiveEffectDuration(), flags=flags)
        effect = effect.model_copy(update=updates)
        statuses = applicable_effect_statuses(target, effect.statuses)
        if binding.lifecycle is not None:
            # A suppressed condition cannot own a repeat-save/damage lifecycle.
            # The save and its already-paid Sneak Attack dice still occurred.
            if effect.statuses and not statuses and not effect.changes:
                continue
            effect = bind_effect_lifecycle(
                effect,
                binding.lifecycle,
                source_id=actor.entity_id,
                source_kind="feature",
                source_slug=plan.feature.slug,
                activity_id=plan.activity.id,
                save_ability=save.ability if save else None,
                save_dc=save.dc if save else None,
                is_magical=False,
            )
        for existing in list(live.active_effects.get(target.entity_id, [])):
            if existing.id == effect.id and existing.origin == effect.origin:
                orch._emit(
                    live,
                    EffectExpired(
                        target_id=target.entity_id,
                        effect_id=existing.id,
                        origin=existing.origin,
                        reason="remove_ieffect",
                    ),
                )
        orch._emit(live, EffectApplied(effect=effect))
        for status in statuses:
            orch._emit(
                live,
                ConditionApplied(
                    target_id=target.entity_id, condition=cast("ConditionType", status)
                ),
            )
    movement = plan.semantics.forced_movement
    if movement is not None and (movement.on_save == "always" or movement.on_save == outcome):
        origin = live.actor_zone.get(actor.entity_id)
        if origin is not None:
            orch.push_combatant(live, target.entity_id, origin, plan.request.push_distance_ft or 0)
    orch._emit(
        live,
        AttackRiderTriggered(
            attacker_id=actor.entity_id,
            target_id=target.entity_id,
            feature_id=plan.feature.slug,
            activity_id=plan.activity.id,
            source_activity_id=attack.source_activity_id,
            trigger=plan.semantics.trigger,
            phase=plan.semantics.phase,
            resource_spent=tuple(
                RiderResourceSpent(feature_id=p.feature_slug, cost=p.cost, maximum=p.maximum)
                for p in plan.payments
            ),
            sacrificed_sneak_dice=plan.semantics.sneak_dice_cost,
            save_outcome=outcome,
        ),
    )


def attach_attack_riders(
    live: _LiveCombat,
    ctx: ActivityResolutionContext,
    plans: tuple[AttackRiderPlan, ...] = (),
    *,
    origin: AttackOrigin | None = None,
) -> ActivityResolutionContext:
    """Bind resolver facts to payments, saves and effects in stable order."""
    from dnd5e_engine import orchestrator as orch

    pending: dict[tuple[str, str], list[AttackRiderPlan]] = {}
    current_attack: list[AttackResolutionContext] = []

    def prepare(attack: AttackResolutionContext) -> int:
        current_attack[:] = [attack]
        selected = []
        for plan in plans:
            if not matches_rider(plan, attack):
                continue
            if not _pay(live, attack, plan):
                continue
            if plan.semantics.phase == "final_hit_before_damage":
                _resolve(live, attack, plan)
            else:
                selected.append(plan)
        pending[(attack.source_activity_id, attack.target_id)] = selected
        return sum(plan.semantics.sneak_dice_cost for plan in selected)

    def resolved(attack: AttackResolutionContext) -> None:
        for plan in pending.pop((attack.source_activity_id, attack.target_id), []):
            _resolve(live, attack, plan)

    def commit_sneak(attacker_id: str, target_id: str) -> None:
        orch._update_combatant(live, attacker_id, sneak_attack_spent_this_turn=True)
        actor = orch._find_combatant(live, attacker_id)
        if actor is None:
            return
        for owner in feature_repertoire(actor, get_lib_loader()):
            feature = get_lib_loader().get_feature(owner.slug)
            if feature is None:
                continue
            for activity_id, semantics in feature.attack_riders.items():
                if semantics.automatic:
                    orch._emit(
                        live,
                        AttackRiderTriggered(
                            attacker_id=attacker_id,
                            target_id=target_id,
                            feature_id=feature.slug,
                            activity_id=activity_id,
                            source_activity_id=current_attack[0].source_activity_id,
                            trigger=semantics.trigger,
                            phase=semantics.phase,
                        ),
                    )

    return replace(
        ctx,
        attack_origin=origin or ("opportunity" if ctx.is_opportunity_attack else ctx.attack_origin),
        turn_serial=live.turn_serial,
        attack_roll_modifier=lambda attacker, target: _roll_modifier(live, attacker, target),
        attack_rider_prepare=prepare,
        attack_rider_resolved=resolved,
        sneak_attack_commit=commit_sneak,
    )
