"""Typed attack declarations and authoritative first-roll bookkeeping."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from dnd5e_srd_data.schema.lifecycle import EffectLifecycleSpec

from dnd5e_engine.activities.effects import bind_effect_lifecycle, passive_effect_to_active_effect
from dnd5e_engine.events import EffectApplied
from dnd5e_engine.feature_repertoire import feature_repertoire
from dnd5e_engine.feature_runtime import FeaturePreflightError
from dnd5e_engine.lib_loader import get_lib_loader

if TYPE_CHECKING:
    from dnd5e_srd_data.schema.feature import Feature

    from dnd5e_engine.activities.context import ActivityResolutionContext
    from dnd5e_engine.events import AttackRolled
    from dnd5e_engine.orchestrator import _LiveCombat
    from dnd5e_engine.types.combat import Combatant

AttackDeclaration = Literal["reckless_attack"]


def declaration_feature(actor: Combatant, declaration: AttackDeclaration) -> Feature | None:
    for owner in feature_repertoire(actor, get_lib_loader()):
        feature = get_lib_loader().get_feature(owner.slug)
        if (
            feature is not None
            and (semantics := feature.attack_rider_context) is not None
            and semantics.declaration == declaration
            and not semantics.deferred_reason
        ):
            return feature
    return None


def declaration_active(live: _LiveCombat, actor: Combatant, declaration: AttackDeclaration) -> bool:
    feature = declaration_feature(actor, declaration)
    if feature is None:
        return False
    return any(
        not effect.disabled
        and effect.lifecycle is not None
        and effect.lifecycle.source_id == actor.entity_id
        and effect.lifecycle.source_kind == "feature"
        and effect.lifecycle.source_slug == feature.slug
        for effect in live.active_effects.get(actor.entity_id, [])
    )


def validate_declaration(
    live: _LiveCombat, actor: Combatant, declaration: AttackDeclaration
) -> None:
    feature = declaration_feature(actor, declaration)
    if (
        feature is None
        or actor.entity_id != live.current_actor_id
        or actor.attack_rolls_made_this_turn != 0
    ):
        raise FeaturePreflightError(
            "declaration requires an owned foundation and first own-turn roll"
        )
    semantics = feature.attack_rider_context
    assert semantics is not None
    effects = {effect.id: effect for effect in feature.passive_effects}
    if (
        semantics.inventory_role != "foundation"
        or semantics.target_role != "attacker"
        or semantics.related_activity_ids
        or semantics.forced_movement is not None
        or semantics.movement_grant is not None
        or semantics.sneak_dice_cost
        or len(semantics.effects) != 1
    ):
        raise FeaturePreflightError("unsupported attack declaration carrier")
    if any(
        b.effect_id not in effects
        or b.lifecycle != EffectLifecycleSpec(expiry_boundary="source_next_turn_start")
        or b.outcome != "always"
        or b.expiry != "none"
        for b in semantics.effects
    ):
        raise FeaturePreflightError("attack declaration has no complete typed effect binding")
    passive = effects[semantics.effects[0].effect_id]
    supported_keys = {"flags.advantage.attack.strength", "flags.advantage.attack_against"}
    if (
        passive.disabled
        or passive.transfer
        or passive.statuses
        or len(passive.changes) != len(supported_keys)
        or {change.key for change in passive.changes} != supported_keys
        or any(change.mode != 5 or change.value != "true" for change in passive.changes)
    ):
        raise FeaturePreflightError("unsupported attack declaration effect changes")


def apply_declaration(
    live: _LiveCombat,
    actor: Combatant,
    declaration: AttackDeclaration,
    ctx: ActivityResolutionContext,
) -> None:
    from dnd5e_engine import orchestrator as orch

    validate_declaration(live, actor, declaration)
    feature = declaration_feature(actor, declaration)
    assert feature is not None
    assert feature.attack_rider_context is not None
    for binding in feature.attack_rider_context.effects:
        passive = next(
            effect for effect in feature.passive_effects if effect.id == binding.effect_id
        )
        effect = passive_effect_to_active_effect(
            passive, target_id=actor.entity_id, caster_id=actor.entity_id, ctx=ctx
        ).model_copy(
            update={
                "id": f"effect:rider:{feature.slug}:{binding.effect_id}",
                "origin": f"rider:{feature.slug}::{actor.entity_id}",
            }
        )
        assert binding.lifecycle is not None
        effect = bind_effect_lifecycle(
            effect,
            binding.lifecycle,
            source_id=actor.entity_id,
            source_kind="feature",
            source_slug=feature.slug,
            activity_id="",
            is_magical=False,
        )
        orch._emit(live, EffectApplied(effect=effect))


def observe_attack_roll(live: _LiveCombat, event: AttackRolled) -> None:
    from dnd5e_engine import orchestrator as orch

    if event.attacker_id != live.current_actor_id:
        return
    actor = orch._find_combatant(live, event.attacker_id)
    if actor is not None:
        orch._update_combatant(
            live, actor.entity_id, attack_rolls_made_this_turn=actor.attack_rolls_made_this_turn + 1
        )
