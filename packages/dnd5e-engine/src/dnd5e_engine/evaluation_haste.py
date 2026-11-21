"""Isolated combat application, attack support and expiry for canonical Haste."""

import random
from collections.abc import Sequence
from typing import cast

from dnd5e_srd_data.loader import AssetLoader

from dnd5e_engine.action_policy import grant_group
from dnd5e_engine.activities.effects import applicable_effect_statuses
from dnd5e_engine.effect_lifecycle import EffectIdentity, conditions_after_effect_application
from dnd5e_engine.evaluation_actor import combatant
from dnd5e_engine.evaluation_class_dependencies import operation_dependencies
from dnd5e_engine.evaluation_concentration import concentration_delta
from dnd5e_engine.evaluation_delta import StateDelta
from dnd5e_engine.evaluation_dependencies import concentration_save_identity_failure
from dnd5e_engine.evaluation_effect_turn import effect_turn_delta
from dnd5e_engine.evaluation_effects import EffectState, explicit_effect_fields
from dnd5e_engine.evaluation_haste_delta import (
    HASTE_EFFECT_ID,
    LETHARGY_EFFECT_ID,
    EffectFollowUpCreate,
    application_clock,
    canonical_haste_effect,
    haste_concentration_rounds,
)
from dnd5e_engine.evaluation_hold_cast import HoldCastComputation
from dnd5e_engine.evaluation_preflight import template_support_failure
from dnd5e_engine.evaluation_projection import EvaluationInvariantError
from dnd5e_engine.evaluation_ruleset import get_default_ruleset
from dnd5e_engine.evaluation_saves import save_actor_failure
from dnd5e_engine.evaluation_state import CharacterStateV2, CombatSnapshot, ConditionRecord
from dnd5e_engine.events import (
    ConditionApplied,
    ConditionType,
    EffectApplied,
    EffectExpiryReason,
)
from dnd5e_engine.live_movement import project_effect_speed
from dnd5e_engine.movement import MovementMode, project_speeds, speed_for_mode
from dnd5e_engine.rules.conditions import active_condition_names, exhaustion_level_of
from dnd5e_engine.types.combat import Combatant
from dnd5e_engine.types.effects import ActiveEffect


def haste_speed(
    actor: CharacterStateV2 | Combatant, effects: Sequence[EffectState], mode: MovementMode = "walk"
) -> int:
    """Project condition and active-effect movement using the shared mechanical rules."""
    if isinstance(actor, CharacterStateV2):
        actor = combatant(actor)
    base = speed_for_mode(
        project_speeds(
            actor.base_speed,
            actor.movement_modes,
            condition_names=active_condition_names(actor.conditions),
            exhaustion_level=exhaustion_level_of(actor.conditions),
        ),
        mode,
    )
    return project_effect_speed(
        base,
        mode,
        tuple(
            ActiveEffect.model_validate(explicit_effect_fields(e))
            for e in effects
            if e.target_id == actor.entity_id
        ),
    )


def has_haste_effects(snapshot: CombatSnapshot) -> bool:
    """Select Haste admission whenever a Haste or Lethargy identity is supplied."""
    return any(
        e.id in {HASTE_EFFECT_ID, LETHARGY_EFFECT_ID}
        or (e.lifecycle is not None and e.lifecycle.source_slug == "haste")
        for e in snapshot.effect_states
    )


def _without_haste(snapshot: CombatSnapshot) -> CombatSnapshot:
    """Expose the non-effect dependencies after separately validating the full Haste graph."""
    return snapshot.model_copy(
        update={
            "effect_states": (),
            "character_states": tuple(
                actor.model_copy(
                    update={
                        "concentration_effect_id": None,
                        "conditions": [],
                        **(
                            {"action_grants_spent": (), "action_grant_groups_spent": ()}
                            if snapshot.effect_states
                            else {}
                        ),
                    }
                )
                for actor in snapshot.character_states
            ),
            "combat_state": snapshot.combat_state.model_copy(
                update={
                    "effect_lifecycles": (),
                    "conditions_by_effect": (),
                    "concentration_chain": {},
                    "concentration_rounds_remaining": {},
                }
            ),
        }
    )


def _graph_failure(snapshot: CombatSnapshot) -> str | None:
    """Require the exact single effect, registered clock, conditions and concentration graph."""
    state = snapshot.combat_state
    if len(snapshot.effect_states) != 1 or len(state.conditions_by_effect) != 1:
        return "Haste and Lethargy require a single complete effect graph"
    effect = snapshot.effect_states[0]
    app = effect.lifecycle
    if app is None or len(state.effect_lifecycles) != 1:
        return "Haste and Lethargy require a single captured application clock"
    actors = {a.entity_id: a for a in snapshot.character_states}
    source, target = app.source_id, effect.target_id
    if source not in actors or target not in actors:
        return "Haste application requires explicit source and target actors"
    parent = canonical_haste_effect(source, target)
    hasted = effect == parent
    if not hasted and effect != parent.end_effects[0]:
        return "Haste or Lethargy differs from its captured canonical definition"
    clock = state.effect_lifecycles[0]
    serial, round_number = clock.state.applied_turn_serial, clock.state.applied_round
    if (
        not 1 <= serial <= state.turn_serial
        or not 1 <= round_number <= state.round_number
        or clock != application_clock(effect, serial, round_number)
    ):
        return "Haste effect has a mismatched application clock"
    chain = state.concentration_chain
    rounds = state.concentration_rounds_remaining
    if hasted:
        if (
            chain != {source: [clock.identity]}
            or set(rounds) != {source}
            or not 1 <= rounds[source] <= haste_concentration_rounds()
        ):
            return "Haste requires its complete, finite concentration lineage"
    elif chain or rounds:
        return "Lethargy has no concentration dependency"
    link = state.conditions_by_effect[0]
    statuses = applicable_effect_statuses(combatant(actors[target]), effect.statuses)
    conditions, _ = conditions_after_effect_application([], effect.id, statuses)
    captured = [ConditionRecord.model_validate(c.model_dump()) for c in conditions]
    if link.identity != clock.identity or link.statuses != statuses:
        return "Haste effect condition lineage contradicts the target's immunities"
    group = grant_group(parent)
    spend_marks = (((parent.id, parent.origin),), (group,) if group is not None else ())
    for actor in actors.values():
        actual_marks = (actor.action_grants_spent, actor.action_grant_groups_spent)
        if (
            not actor.is_alive
            or actor.hp_current <= 0
            or actor.entity_id in state.dead_ids
            or actor.conditions != (captured if actor.entity_id == target else [])
            or actor.concentration_effect_id
            != (effect.id if hasted and actor.entity_id == source else None)
            or (
                actual_marks != ((), ())
                and (actor.entity_id != target or actual_marks != spend_marks)
            )
        ):
            return "Haste graph requires living actors and exact effect/condition provenance"
    return None


def haste_support_failure(
    snapshot: CombatSnapshot, loader: AssetLoader, *, attack: bool = False
) -> str | None:
    """Check the exact Haste graph and supported remaining dependencies before evaluation."""
    reviewed = get_default_ruleset()
    if loader.get_spell("haste") != reviewed.get_spell("haste") or loader.get_condition(
        "incapacitated"
    ) != reviewed.get_condition("incapacitated"):
        return "Haste spell or Incapacitated differs from the reviewed declaration"
    if failure := _graph_failure(snapshot):
        return failure
    if attack and snapshot.combat_state.concentration_chain:
        source = next(iter(snapshot.combat_state.concentration_chain))
        caster = next(actor for actor in snapshot.character_states if actor.entity_id == source)
        if failure := concentration_save_identity_failure(caster, loader) or save_actor_failure(
            caster, loader, reviewed_identity=True, reviewed_concentration=True
        ):
            return failure
    plain = _without_haste(snapshot)
    return operation_dependencies(
        plain, loader, turn_lifecycle=True, movement=not attack, equipped=True, attack=attack
    ) or template_support_failure(plain, loader)


class HasteComputation(HoldCastComputation):
    """Apply and advance an isolated Haste/Lethargy graph through shared turn boundaries."""

    def __init__(self, snapshot: CombatSnapshot, rng: random.Random) -> None:
        """Prepare the isolated candidate with no newly attached end effects."""
        super().__init__(snapshot, rng)
        self.follow_ups: list[EffectFollowUpCreate] = []

    def speed(self, actor: Combatant, mode: MovementMode = "walk") -> int:
        """Project the candidate's still-attached movement effects."""
        return haste_speed(actor, self.effects, mode)

    def refresh_movement(self, target: str) -> None:
        """Reproject remaining movement using the existing expenditure ledger."""
        actor = self.actors[target]
        ledger = self.state.movement_ledgers[target]
        self.actors[target] = actor.model_copy(
            update={"movement_remaining": ledger.remaining(self.speed(actor, ledger.active_mode))}
        )

    def expire_linked_effect(self, identity: EffectIdentity, reason: EffectExpiryReason) -> None:
        """Return the captured end effect only after removing the parent application."""
        parent = next(e for e in self.effects if (e.target_id, e.id, e.origin) == identity)
        super().expire_linked_effect(identity, reason)
        if parent.disabled or parent.target_id in self.state.dead_ids:
            return
        for child in parent.end_effects:
            self.emit(
                EffectApplied(effect=ActiveEffect.model_validate(explicit_effect_fields(child)))
            )
            target = self.actors[child.target_id]
            statuses = applicable_effect_statuses(target, child.statuses)
            for status in statuses:
                self.emit(
                    ConditionApplied(
                        target_id=target.entity_id, condition=cast(ConditionType, status)
                    )
                )
            self.refresh_movement(child.target_id)
            self.follow_ups.append(
                EffectFollowUpCreate(
                    kind="combat.effect_follow_up_create",
                    combat_id=self.state.combat_id,
                    ended_identity=identity,
                    effect=child,
                    lifecycle=self.state.effect_lifecycles[-1],
                    lineage=self.state.conditions_by_effect[-1],
                    expected_effect=None,
                    expected_lifecycle=None,
                    expected_lineage=None,
                )
            )


def haste_delta(
    before: CombatSnapshot, after: CombatSnapshot, follow_ups: Sequence[EffectFollowUpCreate]
) -> StateDelta:
    """Project a normal guarded removal/clock change plus explicit follow-up applications."""
    if not before.combat_state.concentration_chain:
        if follow_ups:
            raise EvaluationInvariantError("Lethargy has no captured follow-up")
        return effect_turn_delta(before, after)
    added = {op.lifecycle.identity for op in follow_ups}
    state = after.combat_state
    normalized = after.model_copy(
        update={
            "effect_states": tuple(
                e for e in after.effect_states if (e.target_id, e.id, e.origin) not in added
            ),
            "combat_state": state.model_copy(
                update={
                    "effect_lifecycles": tuple(
                        r for r in state.effect_lifecycles if r.identity not in added
                    ),
                    "conditions_by_effect": tuple(
                        r for r in state.conditions_by_effect if r.identity not in added
                    ),
                }
            ),
        }
    )
    base = concentration_delta(before, normalized)
    return base.model_copy(update={"operations": (*base.operations, *follow_ups)})
