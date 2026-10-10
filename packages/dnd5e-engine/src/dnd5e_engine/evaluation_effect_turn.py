"""One reviewed, already-authorized Poison lifecycle; no creation or live hooks."""

from dataclasses import replace
from typing import Literal

from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.lifecycle import EffectLifecycleSpec

from dnd5e_engine.activities.effects import bind_effect_lifecycle, passive_effect_to_active_effect
from dnd5e_engine.effect_lifecycle import OngoingEffectLifecycle, conditions_after_effect_expiry
from dnd5e_engine.evaluation_actor import actor_state
from dnd5e_engine.evaluation_computation import CombatComputation
from dnd5e_engine.evaluation_contracts import SavePayload
from dnd5e_engine.evaluation_delta import EffectLifecycleUpdate, StateDelta
from dnd5e_engine.evaluation_effects import EffectState, effect_state
from dnd5e_engine.evaluation_preflight import snapshot_support_failure, template_support_failure
from dnd5e_engine.evaluation_projection import EvaluationInvariantError, attack_delta
from dnd5e_engine.evaluation_ruleset import _canonical
from dnd5e_engine.evaluation_saves import resolve_snapshot_save, save_actor_failure
from dnd5e_engine.evaluation_state import CombatSnapshot, LifecycleRecord
from dnd5e_engine.events import CombatEvent, ConditionRemoved, EffectExpired, TurnPhase

POISON_FEATURE = "cunning-strike"
POISON_ACTIVITY = "n64fvJMT9fPUy7DH"


def _definition_failure(effect: EffectState, loader: AssetLoader) -> str | None:
    app = effect.lifecycle
    if app is None or (
        app.source_kind != "feature"
        or app.source_slug != POISON_FEATURE
        or app.activity_id != POISON_ACTIVITY
        or app.save_ability != "con"
        or app.save_dc is None
        or app.save_dc < 0
        or app.is_magical
    ):
        return "only captured nonmagical Cunning Strike Poison is migrated"
    feature = loader.get_feature(POISON_FEATURE)
    rider = feature.attack_riders.get(POISON_ACTIVITY) if feature else None
    if feature is None or rider is None or len(rider.effects) != 1:
        return "pinned Poison declaration is absent or ambiguous"
    binding = rider.effects[0]
    spec = binding.lifecycle
    if spec is None or spec.repeat_save is None or spec.maximum_rounds is None:
        return "pinned Poison repeat/expiry declaration is incomplete"
    # Admit the reviewed repeat/cap profile only, never child/damage/one-use mechanics.
    basic = EffectLifecycleSpec(repeat_save=spec.repeat_save, maximum_rounds=spec.maximum_rounds)
    if spec != basic or binding.expiry != "none":
        return "additional effect lifecycle mechanics are not migrated"
    definitions = [p for p in feature.passive_effects if p.id == binding.effect_id]
    if len(definitions) != 1:
        return "pinned Poison effect identity is absent or ambiguous"
    expected = bind_effect_lifecycle(
        passive_effect_to_active_effect(
            definitions[0], target_id=effect.target_id, caster_id=app.source_id
        ),
        spec,
        source_id=app.source_id,
        source_kind="feature",
        source_slug=POISON_FEATURE,
        activity_id=POISON_ACTIVITY,
        save_ability="con",
        save_dc=app.save_dc,
        is_magical=False,
    )
    if effect != effect_state(expected) or effect.statuses != {"poisoned"}:
        return "effect facts differ from the complete pinned Poison definition"
    if (
        effect.disabled
        or effect.transfer
        or effect.changes
        or effect.action_policy
        or effect.end_effects
    ):
        return "suppressed, transferred, numeric or child effects are not migrated"
    return None


def _clock_failure(snapshot: CombatSnapshot, effect: EffectState) -> str | None:
    state = snapshot.combat_state
    if len(state.effect_lifecycles) != 1 or len(state.conditions_by_effect) != 1:
        return "exactly one lifecycle and condition lineage record is required"
    record, link = state.effect_lifecycles[0], state.conditions_by_effect[0]
    identity = (effect.target_id, effect.id, effect.origin)
    clock = record.state
    app = effect.lifecycle
    assert app is not None
    if (
        record.identity != identity
        or clock.identity != identity
        or link.identity != identity
        or link.statuses != ["poisoned"]
        or _canonical(clock.application) != _canonical(app)
    ):
        return "full effect identity, application or lineage mismatch"
    if not (
        1 <= clock.applied_turn_serial <= state.turn_serial
        and 1 <= clock.applied_round <= state.round_number
    ):
        return "effect application clock is outside the supplied combat history"
    last = clock.last_repeat_turn_serial
    if last is not None and not clock.applied_turn_serial <= last <= state.turn_serial:
        return "repeat clock is outside the supplied combat history"
    expected = OngoingEffectLifecycle.from_application(
        identity, clock.application, clock.applied_turn_serial, clock.applied_round
    )
    if clock != replace(expected, last_repeat_turn_serial=last):
        return "effect expiry clock contradicts its captured declaration"
    return None


def effect_turn_support_failure(snapshot: CombatSnapshot, loader: AssetLoader) -> str | None:
    if len(snapshot.effect_states) != 1:
        return "only one reviewed ongoing effect is migrated"
    effect = snapshot.effect_states[0]
    failure = _definition_failure(effect, loader) or _clock_failure(snapshot, effect)
    if failure:
        return failure
    assert effect.lifecycle is not None
    actors = {a.entity_id: a for a in snapshot.character_states}
    if effect.target_id not in actors or effect.lifecycle.source_id not in actors:
        return "effect source and target must be explicit actors"
    for actor in actors.values():
        if not actor.is_alive or actor.hp_current <= 0:
            return "effect turn excludes dying/dead and concentration cleanup branches"
        expected_conditions = actor.conditions
        if actor.entity_id == effect.target_id:
            if len(expected_conditions) != 1:
                return "exactly one linked Poison condition is required"
            condition = expected_conditions[0]
            if (
                condition.condition != "poisoned"
                or condition.source_effect_id != effect.id
                or condition.source_entity_id != "implied:effect"
                or condition.scope != "combat"
                or condition.duration_rounds is not None
                or condition.save_dc is not None
                or condition.applied_round != 0
                or condition.exhaustion_level != 1
            ):
                return "Poison condition provenance differs from the captured attachment"
        elif expected_conditions:
            return "other conditions are not admitted in this effect slice"
        failure = save_actor_failure(actor, loader)
        if failure:
            return failure
    # Remove only the completely validated slice to reuse existing conservative gates.
    plain = snapshot.model_copy(
        update={
            "effect_states": (),
            "character_states": tuple(
                a.model_copy(update={"conditions": []}) for a in actors.values()
            ),
            "combat_state": snapshot.combat_state.model_copy(
                update={
                    "effect_lifecycles": (),
                    "conditions_by_effect": (),
                }
            ),
        }
    )
    return snapshot_support_failure(
        plain, turn_lifecycle=True, movement=True
    ) or template_support_failure(plain, loader)


class EffectTurnComputation(CombatComputation):
    """Per-call selected effect record; all other turn calculations are R11's core."""

    def emit(self, event: CombatEvent) -> None:
        super().emit(event)
        if isinstance(event, TurnPhase) and event.phase == "turn_end":
            assert event.actor_id is not None
            self.effect_boundary(event.actor_id)

    def effect_boundary(self, actor_id: str) -> None:
        if not self.state.effect_lifecycles:
            return
        clock = self.state.effect_lifecycles[0].state
        if clock.repeats_at(actor_id, self.state.turn_serial):
            clock = clock.repeated(self.state.turn_serial)
            self.state = self.state.model_copy(
                update={
                    "effect_lifecycles": (LifecycleRecord(identity=clock.identity, state=clock),)
                }
            )
            app = clock.application
            assert app.save_ability is not None
            assert app.save_dc is not None
            original = next(a for a in self.snapshot.character_states if a.entity_id == actor_id)
            event = resolve_snapshot_save(
                actor_state(self.actors[actor_id], original),
                SavePayload(
                    kind="rules.save",
                    target_id=actor_id,
                    ability=app.save_ability,
                    dc=app.save_dc,
                    advantage=(),
                    disadvantage=(),
                    is_magical=app.is_magical,
                ),
                self.rng,
            )
            self.emit(event)
            if event.succeeded:
                self.expire_effect("save_succeeded")
        if self.state.effect_lifecycles and clock.expires_at(
            actor_id, "end", self.state.turn_serial, self.state.round_number
        ):
            self.expire_effect("duration")

    def expire_effect(self, reason: Literal["duration", "save_succeeded"]) -> None:

        effect = self.snapshot.effect_states[0]
        self.events.append(
            EffectExpired(
                target_id=effect.target_id,
                effect_id=effect.id,
                origin=effect.origin,
                reason=reason,
            )
        )
        actor = self.actors[effect.target_id]
        self.actors[effect.target_id] = actor.model_copy(
            update={
                "conditions": conditions_after_effect_expiry(
                    actor.conditions, effect.id, {"poisoned"}, set()
                )
            }
        )
        self.state = self.state.model_copy(
            update={"effect_lifecycles": (), "conditions_by_effect": ()}
        )
        self.emit(ConditionRemoved(target_id=effect.target_id, condition="poisoned"))

    def result(self) -> CombatSnapshot:
        return (
            super()
            .result()
            .model_copy(
                update={
                    "effect_states": self.snapshot.effect_states
                    if self.state.effect_lifecycles
                    else (),
                }
            )
        )


def effect_turn_delta(before: CombatSnapshot, after: CombatSnapshot) -> StateDelta:
    old, new = before.combat_state, after.combat_state
    normalized = after.model_copy(
        update={
            "effect_states": before.effect_states,
            "combat_state": new.model_copy(
                update={
                    "effect_lifecycles": old.effect_lifecycles,
                    "conditions_by_effect": old.conditions_by_effect,
                }
            ),
        }
    )
    base = attack_delta(before, normalized)
    if (
        before.effect_states == after.effect_states
        and old.effect_lifecycles == new.effect_lifecycles
        and old.conditions_by_effect == new.conditions_by_effect
    ):
        return base
    if len(before.effect_states) != 1 or len(after.effect_states) > 1:
        raise EvaluationInvariantError("effect projector requires its admitted single effect")
    effect = before.effect_states[0]
    operation = EffectLifecycleUpdate(
        kind="combat.effect_lifecycle_update",
        combat_id=old.combat_id,
        identity=(effect.target_id, effect.id, effect.origin),
        expected_effect=effect,
        effect=after.effect_states[0] if after.effect_states else None,
        expected_lifecycle=old.effect_lifecycles[0],
        lifecycle=new.effect_lifecycles[0] if new.effect_lifecycles else None,
        expected_lineage=old.conditions_by_effect[0],
        lineage=new.conditions_by_effect[0] if new.conditions_by_effect else None,
    )
    return base.model_copy(update={"operations": (*base.operations, operation)})
