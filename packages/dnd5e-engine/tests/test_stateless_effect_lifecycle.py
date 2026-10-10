"""R17 actual SRD Poison: isolated lifecycle, independent values and Legacy parity."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from random import Random

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader, MemoryAssetLoader
from pydantic import ValidationError
from pydantic_core import to_json

from dnd5e_engine import evaluation_effect_turn
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.activities.effects import bind_effect_lifecycle, passive_effect_to_active_effect
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, RuleEvaluationResult
from dnd5e_engine.evaluation_delta import EffectLifecycleUpdate
from dnd5e_engine.evaluation_projection import EvaluationInvariantError, attack_delta
from dnd5e_engine.evaluation_rng import RNGState, RNGTransition
from dnd5e_engine.evaluation_ruleset import RulesetBindingError, ruleset_binding
from dnd5e_engine.evaluation_snapshot import capture_evaluation_snapshot
from dnd5e_engine.evaluation_state import CombatSnapshot
from dnd5e_engine.events import (
    EffectApplied,
    EffectExpired,
    SaveRolled,
)
from dnd5e_engine.lib_loader import scoped_lib_loader
from dnd5e_engine.specs import GridScene
from tests.evaluation_support import FOE, HERO, WEAPON, synthetic_loader
from tests.test_stateless_attack import apply_delta, execute, request_and_live
from tests.test_stateless_item_closure_core import isolate

FEATURE, ACTIVITY = "cunning-strike", "n64fvJMT9fPUy7DH"


def effect_case(*, npc=True, seed=0, round_number=1, ended_target=True, dc=17):
    request, handle, live = request_and_live(seed=seed)
    bundled = BundledAssetLoader()
    feature = bundled.get_feature(FEATURE)
    loader = MemoryAssetLoader(
        items=[synthetic_loader().get_item(WEAPON)],
        features=[feature],
        conditions=[bundled.get_condition("poisoned")],
    )
    live.ruleset_loader = loader
    target = FOE if npc else HERO
    source = HERO if npc else FOE
    index = int(npc)
    live.initiative[index] = live.initiative[index].model_copy(
        update={
            "constitution": 14,
            "save_proficiencies": ["con"],
            "proficiency_bonus_override": 2,
        }
    )
    binding = feature.attack_riders[ACTIVITY].effects[0]
    definition = next(p for p in feature.passive_effects if p.id == binding.effect_id)
    effect = bind_effect_lifecycle(
        passive_effect_to_active_effect(definition, target_id=target, caster_id=source),
        binding.lifecycle,
        source_id=source,
        source_kind="feature",
        source_slug=FEATURE,
        activity_id=ACTIVITY,
        save_ability="con",
        save_dc=dc,
        is_magical=False,
    )
    with scoped_lib_loader(loader):
        orch._emit(live, EffectApplied(effect=effect))
    live.current_turn_index = index if ended_target else 1 - index
    live.current_actor_id = live.initiative[live.current_turn_index].entity_id
    live.round_number = round_number
    live.event_log.clear()
    while not live.event_queue.empty():
        live.event_queue.get_nowait()
    snapshot = capture_evaluation_snapshot(
        live, request.state_snapshot, GridScene(width=3, height=3)
    )
    return (
        request.model_copy(
            update={
                "actor_id": live.current_actor_id,
                "payload": CombatIntentPayload(intent_type="pass"),
                "ruleset_binding": ruleset_binding(loader),
                "state_snapshot": snapshot,
            }
        ),
        handle,
        live,
        loader,
    )


def apply_effect_delta(snapshot, result):
    """Independent consumer validates the complete effect bundle before publishing."""
    assert result.state_delta.expected_world_version == snapshot.world_version
    assert result.read_set[0].version == snapshot.world_version
    updates = [op for op in result.state_delta.operations if isinstance(op, EffectLifecycleUpdate)]
    for op in updates:
        assert op.combat_id == snapshot.combat_state.combat_id
        assert snapshot.effect_states == (op.expected_effect,)
        assert snapshot.combat_state.effect_lifecycles == (op.expected_lifecycle,)
        assert snapshot.combat_state.conditions_by_effect == (op.expected_lineage,)
    filtered = result.model_copy(
        update={
            "state_delta": result.state_delta.model_copy(
                update={
                    "operations": tuple(
                        op
                        for op in result.state_delta.operations
                        if not isinstance(op, EffectLifecycleUpdate)
                    ),
                }
            )
        }
    )
    after = apply_delta(snapshot, filtered)
    for op in updates:
        after = after.model_copy(
            update={
                "effect_states": (op.effect,) if op.effect else (),
                "combat_state": after.combat_state.model_copy(
                    update={
                        "effect_lifecycles": (op.lifecycle,) if op.lifecycle else (),
                        "conditions_by_effect": (op.lineage,) if op.lineage else (),
                    }
                ),
            }
        )
    return CombatSnapshot.model_validate_json(to_json(after.model_dump(mode="python")))


def parity(request, handle, live, loader, result):
    with scoped_lib_loader(loader):
        asyncio.run(orch.submit_player_intent(handle, request.actor_id, request.payload))
    expected = capture_evaluation_snapshot(
        live, request.state_snapshot, GridScene(width=3, height=3)
    )
    assert result.proposed_events == tuple(live.event_log)
    assert result.rng_transition.next_state == RNGState.capture(live.rng)
    assert apply_effect_delta(request.state_snapshot, result) == expected
    return expected


@pytest.mark.parametrize("npc", [False, True])
@pytest.mark.parametrize(
    "seed,natural,success", [(0, 13, True), (1, 5, False), (31, 1, False), (5, 20, True)]
)
def test_real_poison_pc_npc_save_expiry_and_full_legacy_parity(
    monkeypatch, npc, seed, natural, success
):
    request, handle, live, loader = effect_case(npc=npc, seed=seed)
    before = deepcopy(request)
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request, loader)
        assert result.status == "accepted", result.error
        assert result == execute(request, loader)
    [save] = [e for e in result.proposed_events if isinstance(e, SaveRolled)]
    assert (save.natural, save.modifier, save.roll_total, save.dc, save.succeeded) == (
        natural,
        4,
        natural + 4,
        17,
        success,
    )
    expected_rng = Random(seed)
    expected_rng.randint(1, 20)
    assert result.rng_transition.next_state == RNGState.capture(expected_rng)
    types = [e.type for e in result.proposed_events]
    assert types[:3] == ["intent_submitted", "turn_phase", "save_rolled"]
    assert types[3:5] == (
        ["effect_expired", "condition_removed"]
        if success
        else ["turn_ended", "round_started"]
        if npc
        else ["turn_ended", "turn_started"]
    )
    after = parity(request, handle, live, loader, result)
    if success:
        assert not after.effect_states
        assert not after.combat_state.effect_lifecycles
        assert not next(
            a for a in after.character_states if a.entity_id == request.actor_id
        ).conditions
    else:
        assert after.effect_states == request.state_snapshot.effect_states
        assert after.combat_state.effect_lifecycles[0].state.last_repeat_turn_serial == 1
    assert request == before
    assert RuleEvaluationResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "round_number,seed,reason", [(10, 1, None), (11, 1, "duration"), (11, 0, "save_succeeded")]
)
def test_repeat_precedes_duration_cap_at_real_boundary(monkeypatch, round_number, seed, reason):
    request, handle, live, loader = effect_case(round_number=round_number, seed=seed)
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request, loader)
    assert result.status == "accepted", result.error
    expired = [e for e in result.proposed_events if isinstance(e, EffectExpired)]
    assert [e.reason for e in expired] == ([reason] if reason else [])
    assert len([e for e in result.proposed_events if isinstance(e, SaveRolled)]) == 1
    parity(request, handle, live, loader, result)


def test_source_turn_and_already_processed_target_boundary_do_not_draw(monkeypatch):
    request, handle, live, loader = effect_case(ended_target=False)
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert result.rng_transition.next_state == request.rng_context.state
    assert not any(isinstance(e, SaveRolled) for e in result.proposed_events)
    parity(request, handle, live, loader, result)
    request, handle, live, loader = effect_case()
    identity = next(iter(live.effect_lifecycles))
    live.effect_lifecycles[identity] = live.effect_lifecycles[identity].repeated(live.turn_serial)
    snapshot = capture_evaluation_snapshot(
        live, request.state_snapshot, GridScene(width=3, height=3)
    )
    request = request.model_copy(update={"state_snapshot": snapshot})
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert not result.rng_transition.state_changed
    parity(request, handle, live, loader, result)


def test_multiple_turns_use_only_committed_snapshot_and_explicit_rng(monkeypatch):
    request, _, _, loader = effect_case(seed=1)
    original = deepcopy(request)
    with monkeypatch.context() as scoped:
        isolate(scoped)
        for index in range(5):
            result = execute(request, loader)
            assert result.status == "accepted", result.error
            after = apply_effect_delta(request.state_snapshot, result)
            request = request.model_copy(
                update={
                    "command_id": f"command:step-{index}",
                    "actor_id": after.combat_state.initiative_ids[
                        after.combat_state.current_turn_index
                    ],
                    "state_snapshot": after,
                    "rng_context": request.rng_context.model_copy(
                        update={
                            "state": result.rng_transition.next_state,
                            "version": request.rng_context.version + 1,
                        }
                    ),
                }
            )
    assert not request.state_snapshot.effect_states  # seed 1 rolls 5, then 19: fail then success
    assert original.state_snapshot.effect_states


@pytest.mark.parametrize(
    "case",
    [
        "source",
        "activity",
        "dc",
        "magic",
        "origin",
        "duration",
        "changes",
        "child",
        "flags",
        "status",
        "lineage",
        "clock_identity",
        "expiry_clock",
        "repeat_clock",
        "applied_clock",
        "missing_clock",
        "concentration",
        "condition_source",
        "condition_duration",
        "other_condition",
        "two_effects",
        "reaction",
        "cover",
        "dying",
        "pinned_clause",
    ],
)
def test_incomplete_or_unmigrated_effect_dependencies_refuse_before_rng(monkeypatch, case):
    request, _, _, loader = effect_case()
    snapshot = request.state_snapshot
    effect = snapshot.effect_states[0]
    clock = snapshot.combat_state.effect_lifecycles[0]
    app = effect.lifecycle
    assert app is not None
    if case in {"source", "activity", "dc", "magic"}:
        changes = {
            "source": {"source_id": "missing"},
            "activity": {"activity_id": "unknown"},
            "dc": {"save_dc": -1},
            "magic": {"is_magical": True},
        }[case]
        effect = effect.model_copy(update={"lifecycle": app.model_copy(update=changes)})
    elif case == "origin":
        effect = effect.model_copy(update={"origin": "wrong"})
    elif case == "duration":
        effect = effect.model_copy(
            update={"duration": effect.duration.model_copy(update={"seconds": 61})}
        )
    elif case == "changes":
        effect = effect.model_copy(
            update={
                "changes": [
                    {
                        "key": "flags.save.next_disadvantage",
                        "mode": "override",
                        "value": True,
                        "priority": 20,
                    }
                ]
            }
        )
    elif case == "child":
        effect = effect.model_copy(update={"end_effects": (effect,)})
    elif case == "flags":
        effect = effect.model_copy(update={"flags": {"concentration": True}})
    elif case == "status":
        effect = effect.model_copy(update={"statuses": {"stunned"}})
    state = snapshot.combat_state
    if case == "lineage":
        state = state.model_copy(update={"conditions_by_effect": ()})
    elif case == "missing_clock":
        state = state.model_copy(update={"effect_lifecycles": ()})
    elif case in {"clock_identity", "expiry_clock", "repeat_clock", "applied_clock"}:
        changes = {
            "clock_identity": {"identity": ("wrong", effect.id, effect.origin)},
            "expiry_clock": {"expires_round": 3},
            "repeat_clock": {"last_repeat_turn_serial": 2},
            "applied_clock": {"applied_round": 0},
        }[case]
        state = state.model_copy(
            update={
                "effect_lifecycles": (
                    clock.model_copy(update={"state": replace(clock.state, **changes)}),
                )
            }
        )
    elif case == "reaction":
        state = state.model_copy(update={"help_grants": {HERO: [FOE]}})
    actors = list(snapshot.character_states)
    target = 1
    if case == "concentration":
        actors[target] = actors[target].model_copy(update={"concentration_effect_id": "spell"})
    elif case in {"condition_source", "condition_duration"}:
        cond = actors[target].conditions[0]
        changes = (
            {"source_effect_id": "other"} if case == "condition_source" else {"duration_rounds": 3}
        )
        actors[target] = actors[target].model_copy(
            update={"conditions": [cond.model_copy(update=changes)]}
        )
    elif case == "other_condition":
        actors[0] = actors[0].model_copy(update={"conditions": actors[target].conditions})
    elif case == "dying":
        actors[target] = actors[target].model_copy(update={"hp_current": 0})
    if case == "pinned_clause":
        definition = loader.get_condition("poisoned")
        loader = MemoryAssetLoader(
            items=[loader.get_item(WEAPON)],
            features=[loader.get_feature(FEATURE)],
            conditions=[definition.model_copy(update={"effects": []})],
        )
        request = request.model_copy(update={"ruleset_binding": ruleset_binding(loader)})
    scene = snapshot.scene_state
    if case == "cover":
        scene = scene.model_copy(
            update={"grid": scene.grid.model_copy(update={"cover_cells": {"0,0": "half"}})}
        )
    effects = (effect, effect) if case == "two_effects" else (effect,)
    request = request.model_copy(
        update={
            "state_snapshot": snapshot.model_copy(
                update={
                    "effect_states": effects,
                    "combat_state": state,
                    "character_states": tuple(actors),
                    "scene_state": scene,
                }
            )
        }
    )
    before = deepcopy(request)
    monkeypatch.setattr(
        RNGState, "restore", lambda self: pytest.fail("RNG restored before refusal")
    )
    if case == "two_effects":
        with pytest.raises(ValidationError):
            execute(request, loader)
    else:
        result = execute(request, loader)
        assert result.status == "unsupported", result.error
        assert result.state_delta is None
        assert not result.proposed_events
        assert result.rng_transition is None
    assert request == before


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("phase", ["save", "expiry", "projection"])
def test_fault_after_actual_save_or_cleanup_never_publishes(monkeypatch, failure, phase):
    request, _, live, loader = effect_case()
    before = deepcopy(request)
    rng_before = live.rng.getstate()
    calls = []
    if phase == "save":
        original = evaluation_effect_turn.resolve_snapshot_save

        def fault(*args, **kwargs):
            event = original(*args, **kwargs)
            calls.append(event)
            raise failure("after actual save")

        monkeypatch.setattr(evaluation_effect_turn, "resolve_snapshot_save", fault)
    elif phase == "expiry":
        original = evaluation_effect_turn.EffectTurnComputation.expire_effect

        def fault(self, *args):
            original(self, *args)
            calls.append(self.result())
            raise failure("after cleanup")

        monkeypatch.setattr(evaluation_effect_turn.EffectTurnComputation, "expire_effect", fault)
    else:

        def fault(*args, **kwargs):
            calls.append(True)
            raise failure("after full computation")

        monkeypatch.setattr(RNGTransition, "between", fault)
    isolate(monkeypatch)
    with pytest.raises(failure):
        execute(request, loader)
    assert calls
    assert request == before
    assert live.rng.getstate() == rng_before
    assert not live.event_log


def test_concurrent_replay_and_conflicting_application(monkeypatch):
    request, _, _, loader = effect_case()
    isolate(monkeypatch)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: execute(request, loader), range(8)))
    assert all(r == results[0] for r in results)
    after = apply_effect_delta(request.state_snapshot, results[0])
    before = deepcopy(after)
    with pytest.raises(AssertionError):
        apply_effect_delta(after, results[0])
    assert after == before
    stale = request.state_snapshot.model_copy(update={"world_version": 8})
    with pytest.raises(AssertionError):
        apply_effect_delta(stale, results[0])


@pytest.mark.parametrize("change", ["identity", "partial_remove", "origin", "clock", "lineage"])
def test_closed_effect_delta_rejects_incomplete_transitions(change):
    request, _, _, loader = effect_case(seed=1)
    result = execute(request, loader)
    op = next(o for o in result.state_delta.operations if isinstance(o, EffectLifecycleUpdate))
    value = op.model_dump(mode="python")
    if change == "identity":
        value["identity"] = ("wrong", op.identity[1], op.identity[2])
    elif change == "partial_remove":
        value["effect"] = None
    elif change == "origin":
        value["effect"]["origin"] = "wrong"
    elif change == "clock":
        value["lifecycle"]["state"]["expires_round"] = 20
    else:
        value["lineage"]["statuses"] = ["stunned"]
    with pytest.raises(ValidationError):
        EffectLifecycleUpdate.model_validate_json(to_json(value))


def test_binding_and_projector_still_fail_closed():
    request, _, _, loader = effect_case()
    with pytest.raises(RulesetBindingError):
        execute(request, synthetic_loader())
    result = execute(request, loader)
    after = apply_effect_delta(request.state_snapshot, result)
    with pytest.raises(EvaluationInvariantError):
        attack_delta(request.state_snapshot, after)
    # The dedicated projector retains all ordinary omission guards.
    changed = after.model_copy(update={"inventory_state": None})
    with pytest.raises(EvaluationInvariantError):
        evaluation_effect_turn.effect_turn_delta(request.state_snapshot, changed)


@pytest.mark.parametrize(
    "path",
    [
        ("last_repeat_turn_serial",),
        ("expires_round",),
        ("expiry_actor_id",),
        ("remaining_one_use_modifiers",),
        ("application", "is_magical"),
        ("application", "save_dc"),
        ("application", "spec", "on_end"),
        ("application", "spec", "expire_on_positive_damage"),
        ("application", "spec", "repeat_save", "phase"),
    ],
)
def test_lifecycle_wire_never_fills_missing_dependencies(path):
    request, _, _, _ = effect_case()
    wire = request.model_dump(mode="json")
    record = wire["state_snapshot"]["combat_state"]["effect_lifecycles"][0]["state"]
    for name in path[:-1]:
        record = record[name]
    del record[path[-1]]
    from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest

    with pytest.raises(ValidationError, match="explicit"):
        RuleEvaluationRequest.model_validate_json(to_json(wire))
