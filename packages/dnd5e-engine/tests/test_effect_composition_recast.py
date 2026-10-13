"""R41 real cast -> Shield -> turns -> guarded replacement -> more turns."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from pydantic import TypeAdapter, ValidationError

from dnd5e_engine.evaluation_contracts import CombatIntentPayload, RuleEvaluationRequest
from dnd5e_engine.evaluation_delta import StateDeltaOperation
from dnd5e_engine.evaluation_hold_replace_delta import HoldPersonGraphReplace
from dnd5e_engine.evaluation_rng import RNGState
from tests.effect_composition_support import (
    SECOND_TARGET,
    apply_composed,
    composed_case,
    continued,
    declare_shield,
)
from tests.evaluation_support import FOE, HERO, WEAPON
from tests.test_stateless_attack import execute
from tests.test_stateless_attack_concentration import ATTACKER
from tests.test_stateless_hold_turn import isolated


def recast_case(*, seed=78, multi=True):
    request, loader, first = composed_case(seed=seed, multi=multi, target_wisdom=1)
    attack = execute(request, loader)
    assert attack.status == "accepted", attack.error
    shield = apply_composed(request.state_snapshot, attack)
    assert any(e.id == "effect:imperceptible_barrier" for e in shield.effect_states)
    request = continued(request, attack)
    steps = [first, attack]
    while request.actor_id != FOE:
        result = execute(
            RuleEvaluationRequest.model_validate_json(request.model_dump_json()), loader
        )
        assert result.status == "accepted", result.error
        steps.append(result)
        request = continued(request, result)
    assert request.state_snapshot.combat_state.concentration_chain
    assert not any(
        e.id == "effect:imperceptible_barrier" for e in request.state_snapshot.effect_states
    )
    return request, loader, steps


def recast_intent(targets):
    return CombatIntentPayload(
        intent_type="cast_spell",
        spell_id="hold-person",
        spell_source_id="class:wizard",
        target_id=targets[0],
        target_ids=targets if len(targets) > 1 else None,
        slot_level=3 if len(targets) > 1 else 2,
    )


@pytest.mark.parametrize("targets", [(HERO,), (ATTACKER,), (HERO, SECOND_TARGET), (ATTACKER, HERO)])
def test_continuous_recast_closed_delta_new_clock_and_independent_rng(monkeypatch, targets):
    request, loader, _ = recast_case()
    request = request.model_copy(update={"payload": recast_intent(targets)})
    request = RuleEvaluationRequest.model_validate_json(request.model_dump_json())
    before = deepcopy(request)
    old = request.state_snapshot
    old_clocks = {c.identity: c for c in old.combat_state.effect_lifecycles}
    rng = request.rng_context.state.restore()
    naturals = [rng.randint(1, 20) for _ in targets]
    isolated(monkeypatch)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    result.verify_request(request)
    assert result.stage_facts.coverage == "uncovered"
    assert result.rng_transition.next_state == RNGState.capture(rng)
    saves = [e for e in result.proposed_events if e.type == "save_rolled"]
    assert [(e.target_id, e.natural, e.dc) for e in saves] == list(
        zip(targets, naturals, [15] * len(targets), strict=True)
    )
    kinds = [e.type for e in result.proposed_events]
    assert (
        kinds.index("concentration_dropped")
        < kinds.index("spell_cast")
        < kinds.index("save_rolled")
    )
    assert kinds.index("condition_removed") < kinds.index("save_rolled")
    op = next(o for o in result.state_delta.operations if isinstance(o, HoldPersonGraphReplace))
    assert op.expected_effects == old.effect_states
    assert op.expected_clocks == old.combat_state.effect_lifecycles
    after = apply_composed(old, result)
    assert after.combat_state.concentration_rounds_remaining[FOE] == 9
    assert after.combat_state.turn_serial == old.combat_state.turn_serial + 1
    assert after.resource_state != old.resource_state
    for actor in after.character_states:
        original = next(a for a in old.character_states if a.entity_id == actor.entity_id)
        if actor.entity_id == FOE:
            assert (
                actor.spell_slots[request.payload.slot_level]
                == original.spell_slots[request.payload.slot_level] - 1
            )
        else:
            assert actor.spell_slots == original.spell_slots
        expected = actor.entity_id in targets and naturals[targets.index(actor.entity_id)] < 15
        assert bool(actor.conditions) is expected
    for clock in after.combat_state.effect_lifecycles:
        assert clock.state.applied_turn_serial == old.combat_state.turn_serial
        assert clock.state.applied_round == old.combat_state.round_number
        assert clock.state.last_repeat_turn_serial is None
        if clock.identity in old_clocks:
            assert (
                clock.state.applied_turn_serial
                > old_clocks[clock.identity].state.applied_turn_serial
            )
    assert request == before
    assert result == execute(request, loader)
    request = continued(request, result)
    for _ in range(5):
        result = execute(
            RuleEvaluationRequest.model_validate_json(request.model_dump_json()), loader
        )
        assert result.status == "accepted", result.error
        request = continued(request, result)


def test_all_new_saves_succeed_pays_drops_old_targets_and_creates_empty_anchor_then_recasts():
    request, loader, _ = recast_case(seed=67)
    request = request.model_copy(update={"payload": recast_intent((HERO,))})
    before = request.state_snapshot
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    saves = [e for e in result.proposed_events if e.type == "save_rolled"]
    assert [(e.natural, e.roll_total, e.succeeded) for e in saves] == [(20, 15, True)]
    after = apply_composed(before, result)
    assert len(after.effect_states) == 1
    assert after.effect_states[0].flags["concentration_anchor"]
    assert after.effect_states[0].target_id == FOE
    assert not after.combat_state.effect_lifecycles
    assert all(not a.conditions for a in after.character_states)
    assert after.combat_state.concentration_chain[FOE] == [
        (FOE, "effect:hold-person", f"cast:hold-person:{FOE}")
    ]
    assert (
        next(a for a in after.character_states if a.entity_id == FOE).spell_slots[2]
        == next(a for a in before.character_states if a.entity_id == FOE).spell_slots[2] - 1
    )
    request = continued(request, result)
    while request.actor_id != FOE:
        result = execute(request, loader)
        assert result.status == "accepted", result.error
        request = continued(request, result)
    request = request.model_copy(update={"payload": recast_intent((ATTACKER,))})
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    op = next(o for o in result.state_delta.operations if isinstance(o, HoldPersonGraphReplace))
    assert op.expected_concentration_id == "effect:hold-person"
    assert not op.expected_clocks
    after = apply_composed(request.state_snapshot, result)
    assert all(e.id != "effect:hold-person" for e in after.effect_states)
    assert next(a for a in after.character_states if a.entity_id == ATTACKER).conditions


def test_pending_shield_survives_replacement_and_subsequent_attack_uses_one_fresh_payment():
    request, loader, _ = recast_case(seed=67)
    # Trusted test Host supplies only this canonical declaration at the explicit
    # accepted-proposal boundary; no mechanical state is rebuilt.
    snapshot = declare_shield(request.state_snapshot, loader)
    request = request.model_copy(
        update={"state_snapshot": snapshot, "payload": recast_intent((HERO,))}
    )
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    after = apply_composed(snapshot, result)
    assert after.combat_state.pending_reactions == snapshot.combat_state.pending_reactions
    assert after.combat_state.active_reaction_responses == []
    request = continued(
        request,
        result,
        intent=CombatIntentPayload(intent_type="attack", weapon_id=WEAPON, target_id=FOE),
    )
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    after = apply_composed(request.state_snapshot, result)
    assert len([e for e in result.proposed_events if e.type == "attack_rolled"]) == 1
    assert len([e for e in result.proposed_events if e.type == "reaction_triggered"]) == 1
    assert not after.combat_state.pending_reactions
    assert next(a for a in after.character_states if a.entity_id == FOE).spell_slots[1] == 2


@pytest.mark.parametrize(
    "fault",
    [
        "source",
        "material",
        "slot",
        "action",
        "target",
        "count",
        "unknown",
        "orphan",
        "clock",
        "clock_current",
        "repeat_current",
        "pending_owner",
    ],
)
def test_invalid_recast_never_drops_old_concentration_or_draws_rng(fault, monkeypatch):
    request, loader, _ = recast_case()
    intent = recast_intent((HERO,))
    snapshot = request.state_snapshot
    actor = next(a for a in snapshot.character_states if a.entity_id == FOE)
    if fault == "source":
        intent = intent.model_copy(update={"spell_source_id": "unknown:source"})
    elif fault == "material":
        snapshot = snapshot.model_copy(
            update={
                "inventory_state": snapshot.inventory_state.model_copy(
                    update={
                        "entries": tuple(
                            e.model_copy(update={"accessible": False})
                            for e in snapshot.inventory_state.entries
                        )
                    }
                )
            }
        )
    elif fault == "slot":
        actor = actor.model_copy(update={"spell_slots": {**actor.spell_slots, 2: 0}})
        snapshot = snapshot.model_copy(
            update={
                "resource_state": snapshot.resource_state.model_copy(
                    update={
                        "pools": tuple(
                            p.model_copy(update={"current": 0})
                            if getattr(p, "level", None) == 2
                            else p
                            for p in snapshot.resource_state.pools
                        )
                    }
                )
            }
        )
    elif fault == "action":
        actor = actor.model_copy(update={"action_available": False})
    elif fault == "target":
        intent = intent.model_copy(update={"target_id": FOE})
    elif fault == "count":
        intent = intent.model_copy(update={"target_ids": (HERO, SECOND_TARGET)})
    elif fault == "unknown":
        snapshot = snapshot.model_copy(
            update={
                "effect_states": (
                    *snapshot.effect_states,
                    snapshot.effect_states[0].model_copy(update={"id": "unknown:effect"}),
                )
            }
        )
    elif fault == "orphan":
        snapshot = snapshot.model_copy(
            update={
                "combat_state": snapshot.combat_state.model_copy(
                    update={"conditions_by_effect": ()}
                )
            }
        )
    elif fault in ("clock", "clock_current", "repeat_current"):
        from dataclasses import replace

        clocks = snapshot.combat_state.effect_lifecycles
        clocks = (
            clocks[0].model_copy(
                update={
                    "state": replace(
                        clocks[0].state,
                        **(
                            {"last_repeat_turn_serial": snapshot.combat_state.turn_serial}
                            if fault == "repeat_current"
                            else {
                                "applied_turn_serial": snapshot.combat_state.turn_serial
                                + (fault == "clock"),
                            }
                        ),
                    )
                }
            ),
            *clocks[1:],
        )
        snapshot = snapshot.model_copy(
            update={
                "combat_state": snapshot.combat_state.model_copy(
                    update={"effect_lifecycles": clocks}
                )
            }
        )
    else:
        snapshot = declare_shield(snapshot, loader)
        pending = snapshot.combat_state.pending_reactions[0].model_copy(update={"owner_id": HERO})
        snapshot = snapshot.model_copy(
            update={
                "combat_state": snapshot.combat_state.model_copy(
                    update={"pending_reactions": [pending]}
                )
            }
        )
    snapshot = snapshot.model_copy(
        update={
            "character_states": tuple(
                actor if a.entity_id == FOE else a for a in snapshot.character_states
            )
        }
    )
    request = request.model_copy(update={"state_snapshot": snapshot, "payload": intent})
    original = deepcopy(request)
    monkeypatch.setattr(RNGState, "restore", lambda *_: pytest.fail("RNG before refusal"))
    result = execute(request, loader)
    assert result.status in ("unsupported", "rejected")
    assert result.state_delta is result.rng_transition is result.stage_facts is None
    assert result.proposed_events == ()
    assert request == original


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("point", ["drop", "save"])
def test_failure_after_old_drop_or_new_save_cannot_publish_partial_proposal(
    error, point, monkeypatch
):
    from dnd5e_engine import evaluation_hold_cast

    request, loader, _ = recast_case()
    request = request.model_copy(update={"payload": recast_intent((HERO,))})
    original = deepcopy(request)
    if point == "drop":
        method = evaluation_hold_cast.HoldCastComputation.drop

        def broken(self, *args, **kwargs):
            method(self, *args, **kwargs)
            assert not self.state.concentration_chain
            assert not self.effects
            raise error("after real drop")

        monkeypatch.setattr(evaluation_hold_cast.HoldCastComputation, "drop", broken)
    else:
        method = evaluation_hold_cast.resolve_activity

        def broken(*args, **kwargs):
            method(*args, **kwargs)
            assert any(e.type == "save_rolled" for e in args[1].event_emitter.__self__.events)
            raise error("after real save")

        monkeypatch.setattr(evaluation_hold_cast, "resolve_activity", broken)
    with pytest.raises(error, match="after real"):
        execute(request, loader)
    assert request == original


def test_closed_replacement_schema_missing_extra_wrong_refs_and_generation_fences():
    request, loader, _ = recast_case()
    request = request.model_copy(update={"payload": recast_intent((HERO,))})
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    op = next(o for o in result.state_delta.operations if isinstance(o, HoldPersonGraphReplace))
    adapter = TypeAdapter(StateDeltaOperation)
    assert adapter.validate_json(op.model_dump_json()) == op
    schema = adapter.json_schema()
    assert schema["discriminator"]["mapping"][op.kind].endswith("HoldPersonGraphReplace")
    for model in (op, op.creation, *op.expected_conditions):
        wire = model.model_dump(mode="json")
        for field in type(model).model_fields:
            missing = {k: v for k, v in wire.items() if k != field}
            with pytest.raises(ValidationError):
                type(model).model_validate_json(json.dumps(missing))
        with pytest.raises(ValidationError):
            type(model).model_validate_json(json.dumps({**wire, "path": "/effect_states"}))
    for changes in (
        {"source_id": HERO},
        {"expected_rounds": "9"},
        {"expected_chain": ()},
        {"expected_clocks": ()},
        {"expected_conditions": ()},
        {"expected_effects": (*op.expected_effects, op.expected_effects[0])},
        {
            "creation": op.creation.model_copy(
                update={"applied_turn_serial": op.expected_clocks[0].state.applied_turn_serial}
            )
        },
    ):
        with pytest.raises(ValidationError):
            HoldPersonGraphReplace.model_validate(op.model_copy(update=changes))
    after = apply_composed(request.state_snapshot, result)
    with pytest.raises(AssertionError):
        apply_composed(after, result)  # Even if identity is the same, old clocks no longer match.
    next_request = continued(request, result)
    with pytest.raises(AssertionError):
        apply_composed(next_request.state_snapshot, result)
    # Even restoring the old world version cannot make old-generation cleanup
    # applicable: its complete expected clock and chain footprint still differ.
    from dnd5e_engine.evaluation_delta import EffectLifecycleUpdate

    old_clock = op.expected_clocks[0]
    old_effect = next(
        e for e in op.expected_effects if (e.target_id, e.id, e.origin) == old_clock.identity
    )
    cleanup = EffectLifecycleUpdate(
        kind="combat.effect_lifecycle_update",
        combat_id=op.combat_id,
        identity=old_clock.identity,
        expected_effect=old_effect,
        effect=None,
        expected_lifecycle=old_clock,
        lifecycle=None,
        expected_lineage=next(c for c in op.expected_lineages if c.identity == old_clock.identity),
        lineage=None,
    )
    stale = result.model_copy(
        update={"state_delta": result.state_delta.model_copy(update={"operations": (cleanup,)})}
    )
    with pytest.raises(AssertionError):
        apply_composed(after, stale)


def test_recast_interleaved_sessions_are_deterministic_without_shared_state():
    request, loader, _ = recast_case()
    request = request.model_copy(update={"payload": recast_intent((HERO,))})
    other = request.model_copy(
        update={
            "session_id": "session:other",
            "state_snapshot": request.state_snapshot.model_copy(
                update={"session_id": "session:other"}
            ),
        }
    )
    expected = (execute(request, loader), execute(other, loader))
    assert all(r.status == "accepted" for r in expected)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda i: execute((request, other)[i % 2], loader), range(8)))
    assert results == [expected[i % 2] for i in range(8)]


@pytest.mark.parametrize("targets", [(HERO,), (HERO, ATTACKER)])
def test_one_target_old_chain_can_replace_with_one_or_two_targets(targets):
    request, loader, _ = recast_case(seed=3, multi=False)
    assert len(request.state_snapshot.effect_states) == 1
    request = request.model_copy(update={"payload": recast_intent(targets)})
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    op = next(o for o in result.state_delta.operations if isinstance(o, HoldPersonGraphReplace))
    assert len(op.expected_effects) == 1
    assert op.creation.selected_target_ids == targets
    after = apply_composed(request.state_snapshot, result)
    assert set(e.target_id for e in after.effect_states) <= {*targets, FOE}


def test_replacement_projector_checks_all_other_fields_for_unrepresented_changes():
    from dnd5e_engine.evaluation_delta import ResourceUpdate, SlotSpendLedgerUpdate
    from dnd5e_engine.evaluation_hold_replace import hold_replace_delta, hold_replacement_view
    from dnd5e_engine.evaluation_projection import EvaluationInvariantError

    request, loader, _ = recast_case()
    request = request.model_copy(update={"payload": recast_intent((HERO,))})
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    after = apply_composed(request.state_snapshot, result)
    view, failure = hold_replacement_view(request.state_snapshot, FOE, loader)
    assert not failure
    after = after.model_copy(
        update={
            "character_states": tuple(
                a.model_copy(update={"ac": a.ac + 1}) if a.entity_id == FOE else a
                for a in after.character_states
            )
        }
    )
    payments = tuple(o for o in result.state_delta.operations if isinstance(o, ResourceUpdate))
    ledgers = tuple(
        o for o in result.state_delta.operations if isinstance(o, SlotSpendLedgerUpdate)
    )
    with pytest.raises(EvaluationInvariantError, match="unrepresented"):
        hold_replace_delta(
            request.state_snapshot,
            view,
            after,
            request.payload.model_copy(update={"source_id": FOE}),
            payments,
            ledgers,
            15,
        )


@pytest.mark.parametrize("field", ["character_states", "effect_states", "both"])
def test_legal_snapshot_array_order_does_not_change_recast_semantics(field):
    request, loader, _ = recast_case()
    request = request.model_copy(update={"payload": recast_intent((HERO,))})
    expected = execute(request, loader)
    snapshot = request.state_snapshot
    updates = {}
    if field in ("character_states", "both"):
        updates["character_states"] = tuple(reversed(snapshot.character_states))
    if field in ("effect_states", "both"):
        updates["effect_states"] = tuple(reversed(snapshot.effect_states))
    request = request.model_copy(update={"state_snapshot": snapshot.model_copy(update=updates)})
    request = RuleEvaluationRequest.model_validate_json(request.model_dump_json())
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert result.proposed_events == expected.proposed_events
    assert result.rng_transition == expected.rng_transition
    after = apply_composed(request.state_snapshot, result)
    reference = apply_composed(snapshot, expected)
    assert {a.entity_id: a for a in after.character_states} == {
        a.entity_id: a for a in reference.character_states
    }
    assert after.combat_state == reference.combat_state
    assert after.effect_states == reference.effect_states


def test_r41_union_does_not_accept_r40_envelope_or_evaluator_binding():
    from dnd5e_engine.evaluation_contracts import RuleEvaluationResult
    from dnd5e_engine.evaluation_ruleset import RulesetBindingError

    request, loader, _ = recast_case()
    request = request.model_copy(update={"payload": recast_intent((HERO,))})
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    for model in (request, result):
        wire = model.model_dump(mode="json")
        wire["schema_version"] = "engine-evaluation/24"
        with pytest.raises(ValidationError):
            type(model).model_validate_json(json.dumps(wire))
    assert RuleEvaluationResult.model_validate_json(result.model_dump_json()) == result
    request = request.model_copy(
        update={
            "ruleset_binding": request.ruleset_binding.model_copy(
                update={"evaluator_version": "dnd5e-evaluation/40"}
            )
        }
    )
    with pytest.raises(RulesetBindingError):
        execute(request, loader)
