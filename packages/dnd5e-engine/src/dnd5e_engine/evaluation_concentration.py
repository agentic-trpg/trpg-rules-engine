"""Internal damage/lineage candidate, NOT a public rules.effect or source authority API.

Only already resolved, authorized positive single-packet damage instances and
canonical existing Hold Person are admitted. No source is created here. C-15/C-16
remain OPEN; callers cannot submit this through evaluate or the Bridge.
"""

import random
from copy import deepcopy
from dataclasses import replace
from typing import Annotated, Literal, TypedDict

from dnd5e_srd_data import BundledAssetLoader
from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.common import SaveActivity
from pydantic import Field

from dnd5e_engine.activities.context import DamageInstanceContext
from dnd5e_engine.activities.effects import bind_effect_lifecycle, passive_effect_to_active_effect
from dnd5e_engine.damage_rules import concentration_dc
from dnd5e_engine.effect_lifecycle import OngoingEffectLifecycle, conditions_after_effect_expiry
from dnd5e_engine.evaluation_actor import actor_state
from dnd5e_engine.evaluation_base import EvaluationModel
from dnd5e_engine.evaluation_computation import CombatComputation
from dnd5e_engine.evaluation_contracts import (
    EvaluationVersion,
    ReadVersion,
    RuleError,
    RuleEvaluationResult,
    SavePayload,
)
from dnd5e_engine.evaluation_delta import ConcentrationUpdate, EffectLifecycleUpdate, StateDelta
from dnd5e_engine.evaluation_dependencies import damage_save_identity_failure
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_preflight import snapshot_support_failure
from dnd5e_engine.evaluation_projection import EvaluationInvariantError, attack_delta
from dnd5e_engine.evaluation_rng import RNGContext, RNGTransition
from dnd5e_engine.evaluation_ruleset import RulesetBinding, _canonical, verify_ruleset
from dnd5e_engine.evaluation_saves import resolve_snapshot_save, save_actor_failure
from dnd5e_engine.evaluation_state import CombatSnapshot
from dnd5e_engine.events import (
    CombatEvent,
    ConcentrationCheck,
    ConcentrationDropped,
    ConditionRemoved,
    DamageApplied,
    DamageType,
    Death,
    EffectExpired,
    Unconscious,
)


class ResolvedDamageInstance(EvaluationModel):
    """Internal calculation input after defense resolution, exactly one packet."""

    amount: Annotated[int, Field(gt=0)]
    damage_type: DamageType
    is_crit: bool


class ConcentrationDamagePayload(EvaluationModel):
    target_id: Annotated[str, Field(min_length=1)]
    source_id: Annotated[str, Field(min_length=1)]
    instances: Annotated[tuple[ResolvedDamageInstance, ...], Field(min_length=1)]


class ConcentrationDamageRequest(EvaluationModel):
    schema_version: EvaluationVersion
    session_id: Annotated[str, Field(min_length=1)]
    command_id: Annotated[str, Field(min_length=1)]
    operation_kind: Literal["candidate.damage"]
    actor_id: Annotated[str, Field(min_length=1)]
    payload: ConcentrationDamagePayload
    state_snapshot: CombatSnapshot
    ruleset_binding: RulesetBinding
    rng_context: RNGContext


def concentration_support_failure(
    snapshot: CombatSnapshot, target_id: str, loader: AssetLoader
) -> str | None:
    state = snapshot.combat_state
    actors = {a.entity_id: a for a in snapshot.character_states}
    caster = actors.get(target_id)
    if (
        caster is None
        or set(state.concentration_chain) != {target_id}
        or set(state.concentration_rounds_remaining) != {target_id}
    ):
        return "exactly one complete concentration source is required"
    chain = state.concentration_chain[target_id]
    if (
        not chain
        or len(set(chain)) != len(chain)
        or state.concentration_rounds_remaining[target_id] not in range(1, 11)
    ):
        return "invalid concentration duration or duplicate lineage"
    effects = {(e.target_id, e.id, e.origin): e for e in snapshot.effect_states}
    clocks = {r.identity: r for r in state.effect_lifecycles}
    links = {r.identity: r for r in state.conditions_by_effect}
    if (
        not (
            len(effects)
            == len(snapshot.effect_states)
            == len(chain)
            == len(clocks)
            == len(state.effect_lifecycles)
            == len(links)
            == len(state.conditions_by_effect)
        )
        or set(chain) != set(effects)
        or set(chain) != set(clocks)
        or set(chain) != set(links)
    ):
        return "effect graph is incomplete, duplicated or has unrelated members"
    if loader.get_condition("paralyzed") != BundledAssetLoader().get_condition("paralyzed"):
        return "condition clauses differ from the reviewed pinned projection"
    spell = loader.get_spell("hold-person")
    if spell is None or spell != BundledAssetLoader().get_spell("hold-person"):
        return "Hold Person differs from the reviewed pinned declaration"
    activity = spell.activities[0]
    assert isinstance(activity, SaveActivity)
    binding = activity.effects[0]
    assert binding.lifecycle is not None
    definition = next(p for p in spell.passive_effects if p.id == binding.id)
    for identity in chain:
        effect, clock, link = effects[identity], clocks[identity].state, links[identity]
        app = effect.lifecycle
        if app is None or (
            app.source_id != target_id
            or app.source_kind != "spell"
            or app.source_slug != spell.slug
            or app.activity_id != activity.id
            or app.save_ability != "wis"
            or app.save_dc is None
            or app.save_dc < 0
            or not app.is_magical
            or identity[0] not in actors
            or identity[0] == target_id
        ):
            return "wrong or incomplete effect source/target/save capture"
        expected = bind_effect_lifecycle(
            passive_effect_to_active_effect(definition, target_id=identity[0], caster_id=target_id),
            binding.lifecycle,
            source_id=target_id,
            source_kind="spell",
            source_slug=spell.slug,
            activity_id=activity.id,
            save_ability="wis",
            save_dc=app.save_dc,
            is_magical=True,
        )
        # Actual cast marks concentration on the applied passive effect.
        expected = expected.model_copy(update={"flags": {**expected.flags, "concentration": True}})
        if (
            effect != effect_state(expected)
            or caster.concentration_effect_id != effect.id
            or link.statuses != ["paralyzed"]
        ):
            return "effect or condition lineage differs from the captured pinned definition"
        if (
            _canonical(clock.application) != _canonical(app)
            or clock.identity != identity
            or not (
                1 <= clock.applied_turn_serial <= state.turn_serial
                and 1 <= clock.applied_round <= state.round_number
            )
        ):
            return "lifecycle application or clock mismatch"
        last = clock.last_repeat_turn_serial
        if last is not None and not clock.applied_turn_serial <= last <= state.turn_serial:
            return "repeat clock is outside combat history"
        expected_clock = OngoingEffectLifecycle.from_application(
            identity, clock.application, clock.applied_turn_serial, clock.applied_round
        )
        if clock != replace(expected_clock, last_repeat_turn_serial=last):
            return "lifecycle expiry contradicts captured rule"
    clean_actors = []
    for actor in actors.values():
        failure = damage_save_identity_failure(actor, loader)
        if failure:
            return failure
        expected_ids = [i[1] for i in chain if i[0] == actor.entity_id]
        if expected_ids:
            if "paralyzed" in actor.condition_immunities:
                return "linked condition contradicts target immunity"
            if len(expected_ids) != 1 or len(actor.conditions) != 1:
                return "overlapping conditions require a wider lineage contract"
            c = actor.conditions[0]
            if (
                c.condition != "paralyzed"
                or c.source_effect_id != expected_ids[0]
                or c.source_entity_id != "implied:effect"
                or c.scope != "combat"
                or c.duration_rounds is not None
                or c.save_dc is not None
                or c.applied_round != 0
                or c.exhaustion_level != 1
            ):
                return "condition provenance mismatch"
        elif actor.conditions:
            return "unreviewed condition dependency"
        if actor.entity_id != target_id and actor.concentration_effect_id is not None:
            return "unlinked concentration source"
        plain = actor.model_copy(
            update={
                "conditions": [],
                "concentration_effect_id": None,
                "class_slug": None,
                "classes": {},
                "granted_features": (),
            }
        )
        failure = save_actor_failure(plain, loader)
        if failure:
            return failure
        clean_actors.append(plain)
    plain_snapshot = snapshot.model_copy(
        update={
            "effect_states": (),
            "character_states": tuple(clean_actors),
            "combat_state": state.model_copy(
                update={
                    "concentration_chain": {},
                    "concentration_rounds_remaining": {},
                    "effect_lifecycles": (),
                    "conditions_by_effect": (),
                }
            ),
        }
    )
    return snapshot_support_failure(plain_snapshot)


class ConcentrationComputation(CombatComputation):
    """Only this evaluation's explicit existing graph and damaged actors."""

    def __init__(self, snapshot: CombatSnapshot, rng: random.Random) -> None:
        super().__init__(snapshot, rng)
        self.effects = list(snapshot.effect_states)

    def emit(self, event: CombatEvent) -> None:
        # Original ordering checks concentration before terminal NPC death.
        if isinstance(event, DamageApplied):
            start = len(self.events)
            super().emit(event)
            terminal = [e for e in self.events[start:] if isinstance(e, Death)]
            self.events[start:] = [e for e in self.events[start:] if not isinstance(e, Death)]
            if event.target_id in self.state.concentration_chain:
                original = next(
                    a for a in self.snapshot.character_states if a.entity_id == event.target_id
                )
                save = resolve_snapshot_save(
                    actor_state(self.actors[event.target_id], original),
                    SavePayload(
                        kind="rules.save",
                        target_id=event.target_id,
                        ability="con",
                        dc=concentration_dc(event.amount),
                        advantage=(),
                        disadvantage=(),
                        is_magical=False,
                    ),
                    self.rng,
                )
                self.events.append(
                    ConcentrationCheck(**save.model_dump(exclude={"type", "ability"}))
                )
                if not save.succeeded:
                    self.drop(event.target_id)
            for death in terminal:
                assert isinstance(death, Death)
                if death.target_id in self.state.concentration_chain:
                    self.drop(death.target_id)
            self.events.extend(terminal)
            return
        super().emit(event)
        if (
            isinstance(event, (Death, Unconscious))
            and event.target_id in self.state.concentration_chain
        ):
            self.drop(event.target_id)

    def drop(self, source_id: str) -> None:
        for identity in self.state.concentration_chain[source_id]:
            target, effect_id, origin = identity
            self.events.extend(
                (
                    ConcentrationDropped(target_id=source_id, effect_name=effect_id),
                    EffectExpired(
                        target_id=target,
                        effect_id=effect_id,
                        origin=origin,
                        reason="concentration_drop",
                    ),
                )
            )
            self.effects = [e for e in self.effects if (e.target_id, e.id, e.origin) != identity]
            actor = self.actors[target]
            self.actors[target] = actor.model_copy(
                update={
                    "conditions": conditions_after_effect_expiry(
                        actor.conditions, effect_id, ["paralyzed"], []
                    )
                }
            )
            actor = self.actors[target]
            ledger = self.state.movement_ledgers[target]
            self.actors[target] = actor.model_copy(
                update={"movement_remaining": ledger.remaining(self.speed(actor))}
            )
            self.events.append(ConditionRemoved(target_id=target, condition="paralyzed"))
        self.actors[source_id] = self.actors[source_id].model_copy(
            update={"concentration_effect_id": None}
        )
        self.state = self.state.model_copy(
            update={
                "concentration_chain": {},
                "concentration_rounds_remaining": {},
                "effect_lifecycles": (),
                "conditions_by_effect": (),
            }
        )

    def result(self) -> CombatSnapshot:
        return super().result().model_copy(update={"effect_states": tuple(self.effects)})


def concentration_delta(before: CombatSnapshot, after: CombatSnapshot) -> StateDelta:
    if before.effect_states == after.effect_states:
        return attack_delta(before, after)
    state, new = before.combat_state, after.combat_state
    if (
        after.effect_states
        or new.concentration_chain
        or new.concentration_rounds_remaining
        or new.effect_lifecycles
        or new.conditions_by_effect
    ):
        raise EvaluationInvariantError("incomplete concentration graph cleanup")
    source = next(iter(state.concentration_chain))
    for old, new_actor in zip(before.character_states, after.character_states, strict=True):
        expected_concentration = None if old.entity_id == source else old.concentration_effect_id
        if new_actor.concentration_effect_id != expected_concentration:
            raise EvaluationInvariantError("unrepresented concentration source change")
    normalized = after.model_copy(
        update={
            "effect_states": before.effect_states,
            "character_states": tuple(
                a.model_copy(update={"concentration_effect_id": old.concentration_effect_id})
                for old, a in zip(before.character_states, after.character_states, strict=True)
            ),
            "combat_state": new.model_copy(
                update={
                    "concentration_chain": state.concentration_chain,
                    "concentration_rounds_remaining": state.concentration_rounds_remaining,
                    "effect_lifecycles": state.effect_lifecycles,
                    "conditions_by_effect": state.conditions_by_effect,
                }
            ),
        }
    )
    base = attack_delta(before, normalized)
    actor = next(a for a in before.character_states if a.entity_id == source)
    assert actor.concentration_effect_id is not None
    ops = [
        *base.operations,
        ConcentrationUpdate(
            kind="combat.concentration_update",
            combat_id=state.combat_id,
            source_id=source,
            expected_effect_id=actor.concentration_effect_id,
            expected_chain=tuple(state.concentration_chain[source]),
            expected_rounds=state.concentration_rounds_remaining[source],
            effect_id=None,
            chain=(),
            rounds=None,
        ),
    ]
    for effect in before.effect_states:
        identity = (effect.target_id, effect.id, effect.origin)
        ops.append(
            EffectLifecycleUpdate(
                kind="combat.effect_lifecycle_update",
                combat_id=state.combat_id,
                identity=identity,
                expected_effect=effect,
                effect=None,
                expected_lifecycle=next(
                    r for r in state.effect_lifecycles if r.identity == identity
                ),
                lifecycle=None,
                expected_lineage=next(
                    r for r in state.conditions_by_effect if r.identity == identity
                ),
                lineage=None,
            )
        )
    return StateDelta(expected_world_version=before.world_version, operations=tuple(ops))


def evaluate_concentration_candidate(
    request: ConcentrationDamageRequest, *, loader: AssetLoader
) -> RuleEvaluationResult:
    request = ConcentrationDamageRequest.model_validate(deepcopy(request))
    verify_ruleset(request.ruleset_binding, loader)
    snapshot, payload = request.state_snapshot, request.payload
    if request.session_id != snapshot.session_id or request.actor_id != payload.source_id:
        raise ValueError("candidate source/session identity mismatch")

    class Identity(TypedDict):
        schema_version: EvaluationVersion
        session_id: str
        command_id: str
        input_world_version: int
        input_ruleset_binding: RulesetBinding
        read_set: tuple[ReadVersion, ...]

    common: Identity = dict(
        schema_version=request.schema_version,
        session_id=request.session_id,
        command_id=request.command_id,
        input_world_version=snapshot.world_version,
        input_ruleset_binding=request.ruleset_binding,
        read_set=(
            ReadVersion(kind="world", ref=request.session_id, version=snapshot.world_version),
        ),
    )
    actors = {a.entity_id: a for a in snapshot.character_states}
    if (
        payload.source_id not in actors
        or payload.target_id not in actors
        or not actors[payload.source_id].is_alive
    ):
        failure: str | None = "damage source/target is absent or source is dead"
    else:
        failure = concentration_support_failure(snapshot, payload.target_id, loader)
    if failure:
        return RuleEvaluationResult(
            **common,
            status="unsupported",
            state_delta=None,
            proposed_events=(),
            rng_transition=None,
            choice=None,
            error=RuleError(code="concentration.dependencies", reason=failure),
        )
    rng = request.rng_context.state.restore()
    computation = ConcentrationComputation(snapshot, rng)
    for instance in payload.instances:
        identity = computation.next_damage_id(payload.target_id, payload.source_id)
        computation.emit(
            DamageApplied(
                target_id=payload.target_id,
                amount=instance.amount,
                damage_type=instance.damage_type,
                is_overkill=False,
                source_actor_id=payload.source_id,
                source_id="candidate:resolved-damage",
                damage_instance_id=identity,
                is_crit=instance.is_crit,
            )
        )
        computation.damage_completed(
            DamageInstanceContext(
                target_id=payload.target_id,
                damage_instance_id=identity,
                source_id="candidate:resolved-damage",
                source_actor_id=payload.source_id,
                amount=instance.amount,
                damage_types=(instance.damage_type,),
            )
        )
    after = computation.result()
    return RuleEvaluationResult(
        **common,
        status="accepted",
        state_delta=concentration_delta(snapshot, after),
        proposed_events=tuple(computation.events),
        rng_transition=RNGTransition.between(request.rng_context, rng),
        choice=None,
        error=None,
    )
