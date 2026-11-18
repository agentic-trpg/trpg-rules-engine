"""Stateless voluntary release of the acting caster's admitted Hold Person group."""

from collections.abc import Collection
from typing import cast

from dnd5e_srd_data.loader import AssetLoader

from dnd5e_engine.activities.passive_stats import CombatantMovementModes
from dnd5e_engine.concentration_rules import concentration_anchor, concentration_max_rounds
from dnd5e_engine.effect_lifecycle import EffectIdentity
from dnd5e_engine.evaluation_concentration import hold_person_member_failure
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, RuleEvaluationRequest
from dnd5e_engine.evaluation_delta import (
    ActionBudgetUpdate,
    AttackBudgetState,
    ConcentrationUpdate,
    ConditionsUpdate,
    EffectLifecycleUpdate,
    StateDelta,
    StateDeltaOperation,
)
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_projection import EvaluationInvariantError
from dnd5e_engine.evaluation_rng import RNGTransition
from dnd5e_engine.evaluation_ruleset import get_default_ruleset
from dnd5e_engine.evaluation_state import CharacterStateV2, CombatSnapshot
from dnd5e_engine.evaluation_turn import TurnEvaluation, _refuse
from dnd5e_engine.events import (
    CombatEvent,
    ConcentrationDropped,
    ConditionRemoved,
    ConditionType,
    EffectExpired,
    IntentSubmitted,
)
from dnd5e_engine.movement import project_speeds, speed_for_mode
from dnd5e_engine.rules.conditions import conditions_break_concentration


def _has_dependent_component(snapshot: CombatSnapshot, selected: set[EffectIdentity]) -> bool:
    """Check whether removing the selected effects would strand an unsupported reference."""
    state = snapshot.combat_state
    return (
        any(r.effect_identity in selected for r in state.active_reaction_responses)
        or any(selected.intersection(ids) for ids in state.reaction_effects_pending_expiry.values())
        or any(
            p.effect_identity in selected or p.concentration_identity in selected
            for p in state.timed_activities.pending
        )
        or any(a.concentration_identity in selected for a in state.persistent_areas.areas)
        or any(r.identity in selected for r in state.persistent_areas.next_turn_start_effects)
        or any(s.anchor in selected for s in state.summons.values())
        or any(c.anchor in selected for c in state.constructs.values())
        or any((t.entity_id, t.effect_id, t.origin) in selected for t in state.transforms.values())
    )


def _selected_effects(
    snapshot: CombatSnapshot,
    caster: CharacterStateV2,
    chain: tuple[EffectIdentity, ...],
    rounds: int,
    loader: AssetLoader,
) -> tuple[EffectLifecycleUpdate, ...] | str:
    """Validate the complete selected source graph and build guarded removals."""
    state = snapshot.combat_state
    source = caster.entity_id
    spell = loader.get_spell("hold-person")
    reviewed = get_default_ruleset()
    if spell is None or spell != reviewed.get_spell("hold-person"):
        return "Hold Person differs from the reviewed pinned declaration"
    limit = concentration_max_rounds(spell)
    if limit is None or not 1 <= rounds <= limit:
        return "concentration duration differs from the captured rule"
    if loader.get_condition("paralyzed") != reviewed.get_condition("paralyzed"):
        return "condition clauses differ from the reviewed pinned projection"

    selected = set(chain)
    if len(selected) != len(chain) or any(i[1] != caster.concentration_effect_id for i in chain):
        return "concentration members must uniquely match the caster's active reference"
    if any(
        selected.intersection(members)
        for other, members in state.concentration_chain.items()
        if other != source
    ) or _has_dependent_component(snapshot, selected):
        return "selected concentration has an unsupported dependent reference"

    anchor = effect_state(concentration_anchor(spell, source))
    supplied = {(e.target_id, e.id, e.origin): e for e in snapshot.effect_states}
    owned = {
        identity
        for identity, effect in supplied.items()
        if (effect.target_id == source and effect.flags.get("concentration_anchor"))
        or (
            effect.lifecycle is not None
            and effect.lifecycle.source_id == source
            and effect.flags.get("concentration")
        )
    }
    clocks = {r.identity: r for r in state.effect_lifecycles if r.identity in selected}
    links = {r.identity: r for r in state.conditions_by_effect if r.identity in selected}
    if (
        selected != owned
        or selected != links.keys()
        or len(links) != sum(r.identity in selected for r in state.conditions_by_effect)
        or len(clocks) != sum(r.identity in selected for r in state.effect_lifecycles)
    ):
        return "selected effect, clock or condition lineage is incomplete or duplicated"

    removals = []
    for identity in chain:
        effect, clock, link = supplied[identity], clocks.get(identity), links[identity]
        if effect == anchor:
            if len(chain) != 1 or clock is not None or link.statuses:
                return "Hold Person anchor must be its sole clock-free member"
        elif clock is None or link.statuses != ["paralyzed"]:
            return "Hold Person member requires a repeat clock and Paralyzed lineage"
        elif failure := hold_person_member_failure(effect, clock.state, state, source, spell):
            return failure
        removals.append(
            EffectLifecycleUpdate(
                kind="combat.effect_lifecycle_update",
                combat_id=state.combat_id,
                identity=identity,
                expected_effect=effect,
                expected_lifecycle=clock,
                expected_lineage=link,
                effect=None,
                lifecycle=None,
                lineage=None,
            )
        )
    return tuple(removals)


def _condition_changes(
    snapshot: CombatSnapshot,
    target: CharacterStateV2,
    effect_id: str,
    statuses: Collection[str],
    retained_statuses: Collection[str],
) -> tuple[list[StateDeltaOperation], set[str]] | str:
    """Remove the selected provider and project movement from the same expenditure ledger."""
    original = tuple(target.conditions)
    for status in statuses:
        linked = [c for c in original if c.source_effect_id == effect_id and c.condition == status]
        if (
            status in target.condition_immunities
            or len(linked) != 1
            or linked[0].source_entity_id != "implied:effect"
            or linked[0].scope != "combat"
            or linked[0].duration_rounds is not None
            or linked[0].save_dc is not None
            or linked[0].applied_round != 0
            or linked[0].exhaustion_level != 1
        ):
            return "target condition provenance does not match the admitted effect"
    removed_statuses = set(statuses).difference(retained_statuses)
    after = tuple(
        c
        for c in original
        if c.source_effect_id != effect_id or c.condition not in removed_statuses
    )
    if original == after:
        return [], set()
    conditions = ConditionsUpdate(
        kind="actor.conditions_update",
        target_id=target.entity_id,
        expected=original,
        value=after,
    )
    after_names = tuple(c.condition for c in after)
    gone = removed_statuses.difference(c.lower() for c in after_names)
    operations: list[StateDeltaOperation] = [conditions]
    if gone:
        speeds = project_speeds(
            target.base_speed,
            CombatantMovementModes.model_validate(target.movement_modes.model_dump()),
            condition_names=after_names,
            exhaustion_level=max(
                (c.exhaustion_level for c in after if c.condition.lower() == "exhaustion"),
                default=0,
            ),
        )
        ledger = snapshot.combat_state.movement_ledgers[target.entity_id]
        remaining = ledger.remaining(speed_for_mode(speeds, ledger.active_mode))
        if remaining != target.movement_remaining:
            expected = AttackBudgetState.model_validate(
                {name: getattr(target, name) for name in AttackBudgetState.model_fields}
            )
            operations.append(
                ActionBudgetUpdate(
                    kind="combat.action_budget_update",
                    actor_id=target.entity_id,
                    expected=expected,
                    value=expected.model_copy(update={"movement_remaining": remaining}),
                )
            )
    return operations, gone


def evaluate_effect_release(request: RuleEvaluationRequest, loader: AssetLoader) -> TurnEvaluation:
    """Propose the acting caster's release with no resource spend, turn step or RNG draw.

    A closed group and its condition attachments are required. Retained providers
    are kept in the supplied snapshot, including another origin for the same status.
    Returned Delta and event evidence remain uncommitted.
    """
    snapshot, intent = request.state_snapshot, request.payload
    if not isinstance(snapshot, CombatSnapshot) or not isinstance(intent, CombatIntentPayload):
        raise EvaluationInvariantError("concentration release requires a combat intent")
    source = request.actor_id
    if (
        request.operation_kind != "combat.intent"
        or intent.source_id not in (None, source)
        or intent
        != CombatIntentPayload(intent_type="drop_concentration", source_id=intent.source_id)
    ):
        return _refuse(
            "unsupported", "concentration.intent", "release requires the actor's plain intent"
        )

    state = snapshot.combat_state
    if state.ended:
        return _refuse("rejected", "combat_ended", "combat has ended")
    actors = {actor.entity_id: actor for actor in snapshot.character_states}
    caster = actors.get(source)
    if caster is None or source not in state.initiative_ids:
        return _refuse("rejected", "actor_invalid", "actor is absent from initiative")
    chain = state.concentration_chain.get(source)
    rounds = state.concentration_rounds_remaining.get(source)
    if chain is None and rounds is None and caster.concentration_effect_id is None:
        return _refuse("rejected", "concentration.absent", "actor has no current concentration")
    if not chain or rounds is None or caster.concentration_effect_id is None:
        return _refuse("unsupported", "concentration.graph", "incomplete source concentration")
    if (
        not caster.is_alive
        or caster.hp_current <= 0
        or source in state.dead_ids
        or conditions_break_concentration([c.condition for c in caster.conditions])
    ):
        return _refuse("unsupported", "concentration.graph", "source cannot retain concentration")

    from dnd5e_engine.evaluation_haste import (
        HasteComputation,
        has_haste_effects,
        haste_delta,
        haste_support_failure,
    )

    if has_haste_effects(snapshot) and caster.concentration_effect_id == "effect:hasted":
        if request.schema_version != "engine-evaluation/30":
            return _refuse(
                "unsupported", "concentration.haste", "Haste release requires evaluation/30"
            )
        if failure := haste_support_failure(snapshot, loader):
            return _refuse("unsupported", "concentration.haste", failure)
        rng = request.rng_context.state.restore()
        haste = HasteComputation(snapshot, rng)
        haste.emit(IntentSubmitted(actor_id=source, intent_type="drop_concentration"))
        haste.drop(source)
        computed = haste.result()
        return dict(
            status="accepted",
            state_delta=haste_delta(snapshot, computed.candidate_state, haste.follow_ups),
            proposed_events=computed.proposed_events,
            rng_transition=RNGTransition.between(request.rng_context, rng),
            choice=None,
            error=None,
        )

    member_removals = _selected_effects(snapshot, caster, tuple(chain), rounds, loader)
    if isinstance(member_removals, str):
        return _refuse("unsupported", "concentration.graph", member_removals)

    selected = set(chain)
    supplied = {(e.target_id, e.id, e.origin): e for e in snapshot.effect_states}
    affected_keys = {r.identity[:2] for r in member_removals if r.expected_lineage.statuses}
    retained: dict[tuple[str, str], set[str]] = {}
    retained_identities: set[EffectIdentity] = set()
    for link in state.conditions_by_effect:
        target_id, effect_id, _ = link.identity
        key = target_id, effect_id
        if link.identity in selected or key not in affected_keys:
            continue
        effect = supplied.get(link.identity)
        if (
            link.identity in retained_identities
            or effect is None
            or effect.disabled
            or not set(link.statuses) <= effect.statuses
        ):
            return _refuse("unsupported", "concentration.lineage", "ambiguous retained provider")
        retained_identities.add(link.identity)
        retained.setdefault(key, set()).update(link.statuses)

    conditions: list[StateDeltaOperation] = []
    events: list[CombatEvent] = [IntentSubmitted(actor_id=source, intent_type="drop_concentration")]
    for removal in member_removals:
        target_id, effect_id, origin = removal.identity
        statuses = removal.expected_lineage.statuses
        events.extend(
            (
                ConcentrationDropped(target_id=source, effect_name=effect_id),
                EffectExpired(
                    target_id=target_id,
                    effect_id=effect_id,
                    origin=origin,
                    reason="concentration_drop",
                ),
            )
        )
        if statuses:
            changed = _condition_changes(
                snapshot,
                actors[target_id],
                effect_id,
                statuses,
                retained.get((target_id, effect_id), set()),
            )
            if isinstance(changed, str):
                return _refuse("unsupported", "concentration.lineage", changed)
            updates, removed = changed
            conditions.extend(updates)
            events.extend(
                ConditionRemoved(target_id=target_id, condition=cast(ConditionType, status))
                for status in sorted(removed)
            )

    group_removal = ConcentrationUpdate(
        kind="combat.concentration_update",
        combat_id=state.combat_id,
        source_id=source,
        expected_effect_id=caster.concentration_effect_id,
        expected_chain=tuple(chain),
        expected_rounds=rounds,
        effect_id=None,
        chain=(),
        rounds=None,
    )
    return dict(
        status="accepted",
        state_delta=StateDelta(
            expected_world_version=snapshot.world_version,
            operations=(*conditions, group_removal, *member_removals),
        ),
        proposed_events=tuple(events),
        rng_transition=RNGTransition(
            stream_id=request.rng_context.stream_id,
            input_version=request.rng_context.version,
            input_state=request.rng_context.state,
            next_state=request.rng_context.state,
            state_changed=False,
        ),
        choice=None,
        error=None,
    )
