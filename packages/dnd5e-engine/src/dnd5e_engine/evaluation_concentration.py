"""Internal damage/lineage candidate, NOT a public rules.effect or source authority API.

Actual Attack damage and the internal resolved-damage candidate share the completed
instance fold for canonical existing Hold Person. No source is created here. C-15/C-16
remain OPEN; the resolved-damage candidate is not exposed through evaluate or Bridge.
"""

import random
from copy import deepcopy
from functools import partial
from typing import Annotated, Literal, TypedDict

from dnd5e_srd_data import BundledAssetLoader
from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.common import SaveActivity
from pydantic import Field

from dnd5e_engine.activities.context import DamageInstanceContext
from dnd5e_engine.activities.effects import bind_effect_lifecycle, passive_effect_to_active_effect
from dnd5e_engine.concentration_rules import concentration_anchor
from dnd5e_engine.effect_lifecycle import (
    EffectIdentity,
)
from dnd5e_engine.effect_lifecycle import OngoingEffectLifecycle as OngoingEffectLifecycle
from dnd5e_engine.evaluation_actor import actor_state
from dnd5e_engine.evaluation_base import EvaluationModel
from dnd5e_engine.evaluation_computation import CombatComputation
from dnd5e_engine.evaluation_concentration_mechanics import ConcentrationMechanics
from dnd5e_engine.evaluation_contracts import (
    EvaluationVersion,
    ReadVersion,
    RuleError,
    RuleEvaluationResult,
)
from dnd5e_engine.evaluation_delta import ConcentrationUpdate, EffectLifecycleUpdate, StateDelta
from dnd5e_engine.evaluation_dependencies import damage_save_identity_failure
from dnd5e_engine.evaluation_effect_ownership import effect_graph_index, lifecycle_clock_failure
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_preflight import snapshot_support_failure
from dnd5e_engine.evaluation_projection import EvaluationInvariantError, attack_delta
from dnd5e_engine.evaluation_rng import RNGContext, RNGTransition
from dnd5e_engine.evaluation_ruleset import RulesetBinding, verify_ruleset
from dnd5e_engine.evaluation_saves import resolve_snapshot_save as resolve_snapshot_save
from dnd5e_engine.evaluation_saves import save_actor_failure
from dnd5e_engine.evaluation_stages import EventStageFacts
from dnd5e_engine.evaluation_state import CharacterStateV2, CombatSnapshot
from dnd5e_engine.events import (
    CombatEvent,
    DamageApplied,
    DamageType,
    Death,
    EffectExpiryReason,
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
    snapshot: CombatSnapshot,
    target_id: str,
    loader: AssetLoader,
    *,
    composed: bool = False,
    turn_lifecycle: bool = False,
) -> str | None:
    state = snapshot.combat_state
    if (
        composed
        and not turn_lifecycle
        and state.initiative_ids[state.current_turn_index] == target_id
    ):
        return "self-damage with source turn-end concentration expiry is not migrated"
    actors = {a.entity_id: a for a in snapshot.character_states}
    caster = actors.get(target_id)
    if (
        caster is None
        or not caster.is_alive
        or caster.hp_current <= 0
        or target_id in state.dead_ids
        or set(state.concentration_chain) != {target_id}
        or set(state.concentration_rounds_remaining) != {target_id}
    ):
        return "exactly one living conscious complete concentration source is required"
    chain = state.concentration_chain[target_id]
    if (
        not chain
        or len(set(chain)) != len(chain)
        or state.concentration_rounds_remaining[target_id] not in range(1, 11)
    ):
        return "invalid concentration duration or duplicate lineage"
    try:
        graph = effect_graph_index(snapshot)
    except ValueError as exc:
        return str(exc)
    effects, clocks, links = graph.effects, graph.clocks, graph.links
    anchor = len(snapshot.effect_states) == 1 and bool(
        snapshot.effect_states[0].flags.get("concentration_anchor")
    )
    clock_ids = set() if anchor else set(chain)
    if (
        not (
            len(effects)
            == len(snapshot.effect_states)
            == len(chain)
            == len(links)
            == len(state.conditions_by_effect)
        )
        or set(chain) != set(effects)
        or set(clocks) != clock_ids
        or len(state.effect_lifecycles) != len(clock_ids)
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
    if anchor and (
        snapshot.effect_states[0] != effect_state(concentration_anchor(spell, target_id))
        or links[chain[0]].statuses
        or caster.concentration_effect_id != snapshot.effect_states[0].id
    ):
        return "anchor differs from the exact canonical empty caster-side effect"
    for identity in () if anchor else chain:
        effect, link = effects[identity], links[identity]
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
        if failure := lifecycle_clock_failure(snapshot, effect, clocks[identity]):
            return failure

    identity_failure = partial(
        concentration_identity_failure, snapshot=snapshot, loader=loader, composed=composed
    )
    for actor in actors.values():
        failure = identity_failure(actor)
        if failure:
            return failure
        expected_ids = [i[1] for i in chain if i[0] == actor.entity_id and not anchor]
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
        failure = save_actor_failure(
            actor, loader, reviewed_identity=True, reviewed_concentration=True
        )
        if failure:
            return failure
    return snapshot_support_failure(
        snapshot,
        actor_dependencies=identity_failure,
        reviewed_effects=True,
        # The exact linked Paralyzed conditions were validated above. R11's
        # separate dying-condition profile is not this positive-HP turn slice.
        turn_lifecycle=False,
        movement=turn_lifecycle,
    )


def concentration_identity_failure(
    actor: CharacterStateV2, *, snapshot: CombatSnapshot, loader: AssetLoader, composed: bool
) -> str | None:
    if actor.weapon_mastery_slugs:
        return "trained weapon mastery execution is not migrated"
    if composed and (actor.class_slug or actor.classes):
        from dnd5e_engine.evaluation_class_dependencies import class_dependencies

        return class_dependencies(actor, snapshot, loader, BundledAssetLoader(), equipped=True)
    if actor.fighting_styles:
        return "fighting style dependencies require a reviewed class profile"
    return damage_save_identity_failure(actor, loader)


class ConcentrationComputation(CombatComputation):
    """Only this evaluation's explicit existing graph and damaged actors."""

    def __init__(self, snapshot: CombatSnapshot, rng: random.Random) -> None:
        super().__init__(snapshot, rng)
        self.effects = list(snapshot.effect_states)
        self.concentration = ConcentrationMechanics(self)

    def actor_snapshot(self, actor_id: str) -> CharacterStateV2:
        original = next(a for a in self.snapshot.character_states if a.entity_id == actor_id)
        return actor_state(self.actors[actor_id], original)

    def repeat_boundary(self, actor_id: str) -> None:
        from dnd5e_engine.evaluation_effect_turn import run_snapshot_effect_boundary

        run_snapshot_effect_boundary(self, actor_id, self.expire_linked_effect)

    def emit(self, event: CombatEvent) -> None:
        super().emit(event)
        self.concentration.after_event(event)

    def record_death(self, event: Death, killer: str | None) -> None:
        if event.target_id in self.state.concentration_chain:
            self.drop(event.target_id)
        super().record_death(event, killer)

    def damage_completed(self, damage: DamageInstanceContext) -> None:
        self.concentration.damage_completed(damage)
        super().damage_completed(damage)

    def drop(self, source_id: str, *, reason: EffectExpiryReason = "concentration_drop") -> None:
        self.concentration.drop(source_id, reason=reason)

    def expire_linked_effect(self, identity: EffectIdentity, reason: EffectExpiryReason) -> None:
        self.concentration.expire_linked_effect(identity, reason)

    def candidate_state(self) -> CombatSnapshot:
        return super().candidate_state().model_copy(update={"effect_states": tuple(self.effects)})


def concentration_delta(before: CombatSnapshot, after: CombatSnapshot) -> StateDelta:
    state, new = before.combat_state, after.combat_state
    source = next(iter(state.concentration_chain))
    identities = {(e.target_id, e.id, e.origin) for e in after.effect_states}
    original_ids = {(e.target_id, e.id, e.origin) for e in before.effect_states}
    clock_ids = {(e.target_id, e.id, e.origin) for e in after.effect_states if e.lifecycle}
    chain = tuple(new.concentration_chain.get(source, ()))
    if (
        not identities <= original_ids
        or len(identities) != len(after.effect_states)
        or set(chain) != identities
        or len(chain) != len(identities)
        or {r.identity for r in new.effect_lifecycles} != clock_ids
        or len(new.effect_lifecycles) != len(clock_ids)
        or {r.identity for r in new.conditions_by_effect} != identities
        or len(new.conditions_by_effect) != len(identities)
        or set(new.concentration_chain) != ({source} if chain else set())
        or set(new.concentration_rounds_remaining) != ({source} if chain else set())
    ):
        raise EvaluationInvariantError("incomplete concentration graph transition")
    for old, new_actor in zip(before.character_states, after.character_states, strict=True):
        expected_concentration = (
            None if old.entity_id == source and not chain else old.concentration_effect_id
        )
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
    ops = list(base.operations)
    next_id = actor.concentration_effect_id if chain else None
    next_rounds = new.concentration_rounds_remaining.get(source)
    if (next_id, chain, next_rounds) != (
        actor.concentration_effect_id,
        tuple(state.concentration_chain[source]),
        state.concentration_rounds_remaining[source],
    ):
        ops.append(
            ConcentrationUpdate(
                kind="combat.concentration_update",
                combat_id=state.combat_id,
                source_id=source,
                expected_effect_id=actor.concentration_effect_id,
                expected_chain=tuple(state.concentration_chain[source]),
                expected_rounds=state.concentration_rounds_remaining[source],
                effect_id=next_id,
                chain=chain,
                rounds=next_rounds,
            )
        )
    for effect in before.effect_states:
        identity = (effect.target_id, effect.id, effect.origin)
        old_clock = next((r for r in state.effect_lifecycles if r.identity == identity), None)
        old_link = next(r for r in state.conditions_by_effect if r.identity == identity)
        new_effect = next(
            (e for e in after.effect_states if (e.target_id, e.id, e.origin) == identity), None
        )
        new_clock = next((r for r in new.effect_lifecycles if r.identity == identity), None)
        new_link = next((r for r in new.conditions_by_effect if r.identity == identity), None)
        if (effect, old_clock, old_link) == (new_effect, new_clock, new_link):
            continue
        ops.append(
            EffectLifecycleUpdate(
                kind="combat.effect_lifecycle_update",
                combat_id=state.combat_id,
                identity=identity,
                expected_effect=effect,
                effect=new_effect,
                expected_lifecycle=old_clock,
                lifecycle=new_clock,
                expected_lineage=old_link,
                lineage=new_link,
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
            stage_facts=None,
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
    computed = computation.result()
    after = computed.candidate_state
    return RuleEvaluationResult(
        **common,
        status="accepted",
        state_delta=concentration_delta(snapshot, after),
        proposed_events=computed.proposed_events,
        stage_facts=EventStageFacts.uncovered("complex_effects_or_reactions"),
        rng_transition=RNGTransition.between(request.rng_context, rng),
        choice=None,
        error=None,
    )
