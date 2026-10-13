"""Same Wizard Hold Person recast; full proof before the narrow dependency view."""

from typing import Literal, cast

from dnd5e_srd_data.loader import AssetLoader

from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd5e_engine.evaluation_delta import ResourceUpdate, SlotSpendLedgerUpdate, StateDelta
from dnd5e_engine.evaluation_effect_ownership import effect_ownership
from dnd5e_engine.evaluation_hold_replace_delta import (
    HoldPersonConditionFence,
    HoldPersonGraphReplace,
)
from dnd5e_engine.evaluation_hold_turn import hold_turn_support_failure
from dnd5e_engine.evaluation_paid_projection import paid_delta
from dnd5e_engine.evaluation_state import CombatSnapshot


def hold_replacement_view(
    snapshot: CombatSnapshot, source_id: str, loader: AssetLoader
) -> tuple[CombatSnapshot | None, str | None]:
    from dnd5e_engine.evaluation_composition import composition_support_failure

    try:
        ownership = effect_ownership(snapshot)
    except ValueError as exc:
        return None, str(exc)
    if ownership.hold_source_id != source_id or not 1 <= len(ownership.hold_identities) <= 2:
        return (
            None,
            "only the same existing one/two-target Hold Person caster can replace its chain",
        )
    # An active Shield cannot legitimately survive this caster's turn start.
    # Full policy validation rejects it; one canonical pending declaration survives.
    pending = snapshot.combat_state.pending_reactions
    failure = (
        composition_support_failure(snapshot, loader, turn=True)
        if pending or ownership.shield
        else hold_turn_support_failure(snapshot, loader)
    )
    if failure:
        return None, failure
    if any(
        clock.state.applied_turn_serial >= snapshot.combat_state.turn_serial
        or (
            clock.state.last_repeat_turn_serial is not None
            and clock.state.last_repeat_turn_serial >= snapshot.combat_state.turn_serial
        )
        for clock in snapshot.combat_state.effect_lifecycles
    ):
        return None, "replacement requires all old application/repeat clocks to precede this cast"
    # Full ownership/canonical closure was proved above. This view removes exactly
    # that old chain and its attachments for initial-cast dependency validation.
    # Movement, budgets, slots and every other original fact stay unchanged.
    state = snapshot.combat_state
    return snapshot.model_copy(
        update={
            "effect_states": (),
            "character_states": tuple(
                a.model_copy(
                    update={
                        "conditions": [],
                        "concentration_effect_id": None,
                    }
                )
                for a in snapshot.character_states
            ),
            "combat_state": state.model_copy(
                update={
                    "effect_lifecycles": (),
                    "conditions_by_effect": (),
                    "concentration_chain": {},
                    "concentration_rounds_remaining": {},
                    "pending_reactions": [],
                }
            ),
        }
    ), None


def hold_replace_delta(
    before: CombatSnapshot,
    view: CombatSnapshot,
    after: CombatSnapshot,
    intent: CombatIntentPayload,
    payments: tuple[ResourceUpdate, ...],
    ledgers: tuple[SlotSpendLedgerUpdate, ...],
    dc: int,
) -> StateDelta:
    from dnd5e_engine.evaluation_hold_cast import hold_created_graph, normalized_hold_graph

    state = before.combat_state
    assert intent.source_id is not None
    source = next(a for a in before.character_states if a.entity_id == intent.source_id)
    creation = hold_created_graph(view, after, intent, dc)
    op = HoldPersonGraphReplace(
        kind="combat.hold_person_graph_replace",
        combat_id=state.combat_id,
        source_id=intent.source_id,
        actor_ids=state.initiative_ids,
        expected_effects=before.effect_states,
        expected_clocks=state.effect_lifecycles,
        expected_lineages=state.conditions_by_effect,
        expected_conditions=tuple(
            HoldPersonConditionFence(
                actor_id=actor_id,
                conditions=tuple(
                    next(a for a in before.character_states if a.entity_id == actor_id).conditions
                ),
            )
            for actor_id in state.initiative_ids
        ),
        expected_concentration_id=cast(
            Literal["effect:paralyzed", "effect:hold-person"], source.concentration_effect_id
        ),
        expected_chain=tuple(state.concentration_chain[intent.source_id]),
        expected_rounds=state.concentration_rounds_remaining[intent.source_id],
        creation=creation,
    )
    # Only this transition owns old/new conditions, graph and caster mirror.
    # All other fields pass the original full-input payment/base omission guard
    # once, including movement released by old condition cleanup and pending Shield.
    base = paid_delta(before, normalized_hold_graph(before, after), payments, ledgers)
    return base.model_copy(update={"operations": (*base.operations, op)})
