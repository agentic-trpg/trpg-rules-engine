"""Actual Hold Person damage/CON save/lineage, independent values and Legacy parity."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from random import Random

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from pydantic import ValidationError

from dnd5e_engine import evaluation_concentration as concentration
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.activities.context import DamageInstanceContext
from dnd5e_engine.activities.effects import bind_effect_lifecycle, passive_effect_to_active_effect
from dnd5e_engine.evaluation_concentration import (
    ConcentrationDamagePayload,
    ConcentrationDamageRequest,
    ResolvedDamageInstance,
    evaluate_concentration_candidate,
)
from dnd5e_engine.evaluation_delta import ConcentrationUpdate, EffectLifecycleUpdate
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_engine.evaluation_snapshot import capture_evaluation_snapshot
from dnd5e_engine.events import DamageApplied, EffectApplied
from dnd5e_engine.lib_loader import scoped_lib_loader
from dnd5e_engine.specs import GridScene
from tests.evaluation_support import FOE, HERO, WEAPON, synthetic_loader
from tests.test_stateless_attack import apply_delta, request_and_live
from tests.test_stateless_item_closure_core import isolate


def concentration_case(*, seed=0, amount=24, instances=1, npc=False, hp=40, temp=0):
    request, handle, live = request_and_live(seed=seed)
    canonical = BundledAssetLoader()
    spell = canonical.get_spell("hold-person")
    loader = MemoryAssetLoader(
        items=[synthetic_loader().get_item(WEAPON)],
        spells=[spell],
        conditions=[canonical.get_condition("paralyzed")],
        classes=[canonical.get_class("wizard")],
        features=[canonical.get_feature(s) for s in ("spellcasting", "arcane-recovery")],
    )
    live.ruleset_loader = loader
    caster, target = (FOE, HERO) if npc else (HERO, FOE)
    for index, actor in enumerate(live.initiative):
        facts = {"granted_features": ()}
        if actor.entity_id == caster:
            facts.update(
                hp_current=hp,
                hp_max=40,
                temp_hp=temp,
                constitution=14,
                save_proficiencies=["con"],
                proficiency_bonus_override=2,
                class_slug="wizard",
                classes={"wizard": 2},
                character_level=2,
                granted_features=("spellcasting", "arcane-recovery"),
            )
            live.tracked_hp[caster] = hp
            live.tracked_temp_hp[caster] = temp
        live.initiative[index] = actor.model_copy(update=facts)
    binding = spell.activities[0].effects[0]
    definition = spell.passive_effects[0]
    effect = bind_effect_lifecycle(
        passive_effect_to_active_effect(definition, target_id=target, caster_id=caster),
        binding.lifecycle,
        source_id=caster,
        source_kind="spell",
        source_slug=spell.slug,
        activity_id=spell.activities[0].id,
        save_ability="wis",
        save_dc=15,
        is_magical=True,
    )
    effect = effect.model_copy(update={"flags": {**effect.flags, "concentration": True}})
    with scoped_lib_loader(loader):
        orch._emit(live, EffectApplied(effect=effect))
    identity = (target, effect.id, effect.origin)
    live.concentration_chain[caster] = [identity]
    live.concentration_rounds_remaining[caster] = 10
    orch._update_combatant(live, caster, concentration_effect_id=effect.id)
    live.event_log.clear()
    while not live.event_queue.empty():
        live.event_queue.get_nowait()
    snapshot = capture_evaluation_snapshot(
        live, request.state_snapshot, GridScene(width=3, height=3)
    )
    candidate = ConcentrationDamageRequest(
        schema_version=request.schema_version,
        session_id=request.session_id,
        command_id=request.command_id,
        operation_kind="candidate.damage",
        actor_id=target,
        payload=ConcentrationDamagePayload(
            target_id=caster,
            source_id=target,
            instances=tuple(
                ResolvedDamageInstance(amount=amount, damage_type="force", is_crit=False)
                for _ in range(instances)
            ),
        ),
        state_snapshot=snapshot,
        ruleset_binding=ruleset_binding(loader),
        rng_context=request.rng_context,
    )
    return candidate, handle, live, loader


def apply_candidate(snapshot, result, *, world_version=None):
    assert result.state_delta.expected_world_version == (
        snapshot.world_version if world_version is None else world_version
    )
    assert result.read_set[0].version == snapshot.world_version
    concentration_ops = [
        op for op in result.state_delta.operations if isinstance(op, ConcentrationUpdate)
    ]
    effects = [op for op in result.state_delta.operations if isinstance(op, EffectLifecycleUpdate)]
    for op in concentration_ops:
        caster = next(a for a in snapshot.character_states if a.entity_id == op.source_id)
        assert op.combat_id == snapshot.combat_state.combat_id
        assert caster.concentration_effect_id == op.expected_effect_id
        assert tuple(snapshot.combat_state.concentration_chain[op.source_id]) == op.expected_chain
        assert (
            snapshot.combat_state.concentration_rounds_remaining[op.source_id] == op.expected_rounds
        )
    for op in effects:
        assert op.expected_effect in snapshot.effect_states
        assert op.expected_lifecycle in snapshot.combat_state.effect_lifecycles
        assert op.expected_lineage in snapshot.combat_state.conditions_by_effect
    filtered = result.model_copy(
        update={
            "state_delta": result.state_delta.model_copy(
                update={
                    "operations": tuple(
                        op
                        for op in result.state_delta.operations
                        if not isinstance(op, (ConcentrationUpdate, EffectLifecycleUpdate))
                    )
                }
            )
        }
    )
    after = apply_delta(snapshot, filtered)
    if concentration_ops:
        after = after.model_copy(
            update={
                "effect_states": (),
                "character_states": tuple(
                    a.model_copy(update={"concentration_effect_id": None})
                    if a.entity_id == concentration_ops[0].source_id
                    else a
                    for a in after.character_states
                ),
                "combat_state": after.combat_state.model_copy(
                    update={
                        "concentration_chain": {},
                        "concentration_rounds_remaining": {},
                        "conditions_by_effect": (),
                        "effect_lifecycles": (),
                    }
                ),
            }
        )
    return after


@pytest.mark.parametrize("npc", [False, True])
@pytest.mark.parametrize(
    "seed,natural,success", [(0, 13, True), (1, 5, False), (5, 20, True), (31, 1, False)]
)
def test_real_class_pc_npc_damage_save_and_full_legacy_parity(
    monkeypatch, npc, seed, natural, success
):
    request, _, live, loader = concentration_case(npc=npc, seed=seed)
    before = deepcopy(request)
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = evaluate_concentration_candidate(request, loader=loader)
        assert result.status == "accepted", result.error
    checks = [e for e in result.proposed_events if e.type == "concentration_check"]
    assert len(checks) == 1
    check = checks[0]
    assert (check.dc, check.natural, check.modifier, check.roll_total, check.succeeded) == (
        12,
        natural,
        4,
        natural + 4,
        success,
    )
    rng = Random(seed)
    rng.randint(1, 20)
    assert result.rng_transition.next_state == RNGState.capture(rng)
    assert [e.type for e in result.proposed_events] == (
        ["damage_applied", "concentration_check"]
        + ([] if success else ["concentration_dropped", "effect_expired", "condition_removed"])
    )
    with scoped_lib_loader(loader):
        live.damage_instance_sequence += 1
        identity = f"damage:{live.damage_instance_sequence}:{live.current_actor_id}:{request.payload.target_id}"
        event = DamageApplied(
            target_id=request.payload.target_id,
            amount=24,
            damage_type="force",
            is_overkill=False,
            source_actor_id=request.payload.source_id,
            source_id="candidate:resolved-damage",
            damage_instance_id=identity,
        )
        orch._emit(live, event)
        orch.live_effect_lifecycle.damage_instance_completed(
            live,
            DamageInstanceContext(
                target_id=event.target_id,
                damage_instance_id=identity,
                source_id=event.source_id,
                source_actor_id=request.payload.source_id,
                amount=24,
                damage_types=("force",),
            ),
        )
    assert result.proposed_events == tuple(live.event_log)
    assert result.rng_transition.next_state == RNGState.capture(live.rng)
    expected = capture_evaluation_snapshot(
        live, request.state_snapshot, GridScene(width=3, height=3)
    )
    assert apply_candidate(request.state_snapshot, result) == expected
    assert request == before


@pytest.mark.parametrize("amount,dc", [(1, 10), (21, 10), (24, 12), (59, 29), (100, 30)])
def test_damage_dc_cap_and_temp_hp(amount, dc):
    request, _, _, loader = concentration_case(amount=amount, hp=40, temp=200)
    result = evaluate_concentration_candidate(request, loader=loader)
    assert result.status == "accepted", result.error
    check = next(e for e in result.proposed_events if e.type == "concentration_check")
    assert check.dc == dc
    after = apply_candidate(request.state_snapshot, result)
    actor = next(a for a in after.character_states if a.entity_id == request.payload.target_id)
    assert (actor.hp_current, actor.temp_hp) == (40, 200 - amount)


def test_multiple_instances_stop_saves_after_failed_concentration():
    request, _, _, loader = concentration_case(seed=1, amount=1, instances=3)
    result = evaluate_concentration_candidate(request, loader=loader)
    assert result.status == "accepted", result.error
    assert len([e for e in result.proposed_events if e.type == "damage_applied"]) == 3
    assert len([e for e in result.proposed_events if e.type == "concentration_check"]) == 1
    assert apply_candidate(request.state_snapshot, result).character_states[0].hp_current == 37


@pytest.mark.parametrize("npc", [False, True])
def test_zero_hp_and_death_end_graph_even_when_save_succeeds(npc):
    request, _, _, loader = concentration_case(npc=npc, seed=5, amount=40)
    result = evaluate_concentration_candidate(request, loader=loader)
    assert result.status == "accepted", result.error
    after = apply_candidate(request.state_snapshot, result)
    assert not after.effect_states
    assert not after.combat_state.concentration_chain
    assert (
        next(
            a for a in after.character_states if a.entity_id == request.payload.target_id
        ).concentration_effect_id
        is None
    )
    assert len([e for e in result.proposed_events if e.type == "concentration_dropped"]) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("granted_features", ("war-caster",)),
        ("granted_features", None),
        ("species_slug", "human"),
        ("subclass_slug", "evoker"),
        ("constitution", 0),
        ("trait_mechanics", ()),
        ("class_slug", "rogue"),
    ],
)
def test_unreviewed_real_dependencies_refuse_before_rng(monkeypatch, field, value):
    request, _, _, loader = concentration_case()
    actors = list(request.state_snapshot.character_states)
    if field == "trait_mechanics":
        # An actual save-relevant feature override must not ride on a familiar slug.
        f = loader.get_feature("spellcasting")
        loader = MemoryAssetLoader(
            items=[synthetic_loader().get_item(WEAPON)],
            spells=[loader.get_spell("hold-person")],
            conditions=[BundledAssetLoader().get_condition("paralyzed")],
            classes=[BundledAssetLoader().get_class("wizard")],
            features=[
                f.model_copy(update={"name": "changed"}),
                loader.get_feature("arcane-recovery"),
            ],
        )
        request = request.model_copy(update={"ruleset_binding": ruleset_binding(loader)})
    else:
        actors[0] = actors[0].model_copy(update={field: value})
        request = request.model_copy(
            update={
                "state_snapshot": request.state_snapshot.model_copy(
                    update={"character_states": tuple(actors)}
                )
            }
        )
    monkeypatch.setattr(RNGState, "restore", lambda *a: pytest.fail("refusal restored RNG"))
    result = evaluate_concentration_candidate(request, loader=loader)
    assert result.status == "unsupported"
    assert result.state_delta is result.rng_transition is None
    assert not result.proposed_events


@pytest.mark.parametrize(
    "break_graph", ["source", "clock", "lineage", "duration", "duplicate", "effect"]
)
def test_incomplete_graph_refuses_before_rng(monkeypatch, break_graph):
    request, _, _, loader = concentration_case()
    snap, state = request.state_snapshot, request.state_snapshot.combat_state
    if break_graph == "source":
        effect = snap.effect_states[0]
        app = effect.lifecycle.model_copy(update={"source_id": FOE})
        snap = snap.model_copy(
            update={"effect_states": (effect.model_copy(update={"lifecycle": app}),)}
        )
    elif break_graph == "clock":
        state = state.model_copy(update={"effect_lifecycles": ()})
    elif break_graph == "lineage":
        state = state.model_copy(update={"conditions_by_effect": ()})
    elif break_graph == "duration":
        state = state.model_copy(update={"concentration_rounds_remaining": {HERO: 11}})
    elif break_graph == "duplicate":
        state = state.model_copy(
            update={"concentration_chain": {HERO: state.concentration_chain[HERO] * 2}}
        )
    else:
        snap = snap.model_copy(update={"effect_states": ()})
    request = request.model_copy(
        update={"state_snapshot": snap.model_copy(update={"combat_state": state})}
    )
    monkeypatch.setattr(RNGState, "restore", lambda *a: pytest.fail("refusal restored RNG"))
    result = evaluate_concentration_candidate(request, loader=loader)
    assert result.status == "unsupported"


def test_replay_concurrency_stale_commit_and_post_save_rollback(monkeypatch):
    request, _, _, loader = concentration_case(seed=1)
    before = deepcopy(request)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda _: evaluate_concentration_candidate(request, loader=loader), range(8))
        )
    assert all(r == results[0] for r in results)
    with pytest.raises(AssertionError):
        apply_candidate(request.state_snapshot, results[0], world_version=8)
    with pytest.raises(AssertionError):
        apply_candidate(apply_candidate(request.state_snapshot, results[0]), results[0])
    original = concentration.resolve_snapshot_save

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("after save")

    monkeypatch.setattr(concentration, "resolve_snapshot_save", fail)
    with pytest.raises(RuntimeError, match="after save"):
        evaluate_concentration_candidate(request, loader=loader)
    assert request == before


def test_packet_requires_explicit_positive_complete_input():
    with pytest.raises(ValidationError):
        ResolvedDamageInstance(amount=0, damage_type="force", is_crit=False)
    with pytest.raises(ValidationError):
        ResolvedDamageInstance(amount=1, damage_type="force")


@pytest.mark.parametrize("seed,expected_checks", [(0, 3), (1, 1)])
def test_ordered_multiple_instances_share_only_explicit_rng(seed, expected_checks):
    request, _, _, loader = concentration_case(seed=seed, amount=1, instances=3)
    result = evaluate_concentration_candidate(request, loader=loader)
    assert result.status == "accepted", result.error
    rng = Random(seed)
    for _ in range(expected_checks):
        rng.randint(1, 20)
    assert (
        len([e for e in result.proposed_events if e.type == "concentration_check"])
        == expected_checks
    )
    assert result.rng_transition.next_state == RNGState.capture(rng)
    assert [e.damage_instance_id for e in result.proposed_events if e.type == "damage_applied"] == [
        f"damage:{i}:{HERO}:{HERO}" for i in range(1, 4)
    ]


def test_multiple_linked_targets_are_removed_in_one_result():
    request, _, _, loader = concentration_case(seed=1)
    snap, state = request.state_snapshot, request.state_snapshot.combat_state
    old = snap.effect_states[0]
    new_id = "npc:second-target"
    app = old.lifecycle
    effect = old.model_copy(update={"target_id": new_id})
    identity = (new_id, effect.id, effect.origin)
    clock = state.effect_lifecycles[0]
    new_clock = clock.model_copy(
        update={
            "identity": identity,
            "state": concentration.OngoingEffectLifecycle.from_application(identity, app, 1, 1),
        }
    )
    new_link = state.conditions_by_effect[0].model_copy(update={"identity": identity})
    target = snap.character_states[1].model_copy(update={"entity_id": new_id})
    state = state.model_copy(
        update={
            "initiative_ids": (*state.initiative_ids, new_id),
            "encounter_ids": state.encounter_ids | {new_id},
            "xp_value_by_entity": {**state.xp_value_by_entity, new_id: 0},
            "actor_zone": {**state.actor_zone, new_id: "2,2"},
            "movement_ledgers": {**state.movement_ledgers, new_id: state.movement_ledgers[FOE]},
            "concentration_chain": {HERO: [*state.concentration_chain[HERO], identity]},
            "effect_lifecycles": (*state.effect_lifecycles, new_clock),
            "conditions_by_effect": (*state.conditions_by_effect, new_link),
        }
    )
    snap = snap.model_copy(
        update={
            "character_states": (*snap.character_states, target),
            "effect_states": (*snap.effect_states, effect),
            "combat_state": state,
        }
    )
    request = request.model_copy(update={"state_snapshot": snap})
    result = evaluate_concentration_candidate(request, loader=loader)
    assert result.status == "accepted", result.error
    assert (
        len([op for op in result.state_delta.operations if isinstance(op, EffectLifecycleUpdate)])
        == 2
    )
    assert (
        len([op for op in result.state_delta.operations if isinstance(op, ConcentrationUpdate)])
        == 1
    )
    after = apply_candidate(snap, result)
    assert not after.effect_states
    assert not after.combat_state.effect_lifecycles
    assert not after.combat_state.conditions_by_effect
    assert all(not a.conditions for a in after.character_states)
    assert [e.target_id for e in result.proposed_events if e.type == "effect_expired"] == [
        FOE,
        new_id,
    ]


@pytest.mark.parametrize("stage", ["drop", "projection", "cancel"])
def test_post_damage_cleanup_projection_and_cancellation_rollback(monkeypatch, stage):
    import asyncio

    request, _, _, loader = concentration_case(seed=1)
    before = deepcopy(request)
    if stage == "drop":
        original = concentration.ConcentrationComputation.drop

        def fail(self, *args):
            original(self, *args)
            raise RuntimeError("after cleanup")

        monkeypatch.setattr(concentration.ConcentrationComputation, "drop", fail)
    else:
        original = concentration.concentration_delta

        def fail(*args):
            original(*args)
            if stage == "cancel":
                raise asyncio.CancelledError()
            raise RuntimeError("after projection")

        monkeypatch.setattr(concentration, "concentration_delta", fail)
    with pytest.raises(asyncio.CancelledError if stage == "cancel" else RuntimeError):
        evaluate_concentration_candidate(request, loader=loader)
    assert request == before


def test_candidate_is_not_a_public_damage_source_api():
    from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest, RuleEvaluationResult

    request, _, _, loader = concentration_case()
    with pytest.raises(ValidationError):
        RuleEvaluationRequest.model_validate(request.model_dump())
    result = evaluate_concentration_candidate(request, loader=loader)
    assert RuleEvaluationResult.model_validate_json(result.model_dump_json()) == result


def test_new_execution_has_no_legacy_import_or_runtime_factory():
    import ast
    from pathlib import Path

    for module in (concentration,):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not any(
                    v in (node.module or "")
                    for v in ("orchestrator", "live_", "evaluation_context", "evaluation_snapshot")
                )
            if isinstance(node, (ast.Name, ast.Attribute)):
                assert (node.id if isinstance(node, ast.Name) else node.attr) not in {
                    "_LiveCombat",
                    "CombatHandle",
                    "_REGISTRY",
                    "_submit_live_intent",
                }


def test_terminal_damage_does_not_silently_discard_later_resolved_instances():
    request, _, _, loader = concentration_case(npc=True, seed=5, amount=40, instances=2)
    result = evaluate_concentration_candidate(request, loader=loader)
    assert result.status == "accepted", result.error
    assert len([e for e in result.proposed_events if e.type == "damage_applied"]) == 2
    assert len([e for e in result.proposed_events if e.type == "death"]) == 1
    assert len([e for e in result.proposed_events if e.type == "concentration_check"]) == 1
    assert (
        apply_candidate(request.state_snapshot, result).combat_state.damage_instance_sequence == 2
    )


def test_existing_condition_cannot_contradict_target_immunity(monkeypatch):
    request, _, _, loader = concentration_case()
    actors = list(request.state_snapshot.character_states)
    actors[1] = actors[1].model_copy(update={"condition_immunities": ["paralyzed"]})
    request = request.model_copy(
        update={
            "state_snapshot": request.state_snapshot.model_copy(
                update={"character_states": tuple(actors)}
            )
        }
    )
    monkeypatch.setattr(RNGState, "restore", lambda *a: pytest.fail("restored RNG"))
    assert evaluate_concentration_candidate(request, loader=loader).status == "unsupported"


def test_modified_condition_binding_cannot_use_compiled_projection(monkeypatch):
    request, _, _, loader = concentration_case()
    canonical = BundledAssetLoader()
    condition = canonical.get_condition("paralyzed").model_copy(update={"effects": []})
    loader = MemoryAssetLoader(
        items=[synthetic_loader().get_item(WEAPON)],
        spells=[loader.get_spell("hold-person")],
        conditions=[condition],
        classes=[canonical.get_class("wizard")],
        features=[canonical.get_feature(s) for s in ("spellcasting", "arcane-recovery")],
    )
    request = request.model_copy(update={"ruleset_binding": ruleset_binding(loader)})
    monkeypatch.setattr(RNGState, "restore", lambda *a: pytest.fail("restored RNG"))
    assert evaluate_concentration_candidate(request, loader=loader).status == "unsupported"


def test_recorded_dead_source_cannot_keep_existing_concentration_or_roll(monkeypatch):
    from dnd5e_engine.damage_rules import death_record
    from dnd5e_engine.evaluation_actor import combatant
    from dnd5e_engine.events import Death

    request, _, _, loader = concentration_case(seed=0, amount=1)
    snapshot = request.state_snapshot
    actors = list(snapshot.character_states)
    index = next(i for i, a in enumerate(actors) if a.entity_id == request.payload.target_id)
    actors[index] = actors[index].model_copy(update={"hp_current": 0, "is_alive": False})
    record = death_record(
        combatant(actors[index]),
        Death(target_id=request.payload.target_id, reason="damage"),
        location_id=snapshot.scene_state.scene_id,
        killer_id=request.payload.source_id,
    )
    state = snapshot.combat_state.model_copy(
        update={"dead_ids": {request.payload.target_id}, "deaths_recorded": [record]}
    )
    snapshot = snapshot.model_copy(
        update={"character_states": tuple(actors), "combat_state": state}
    )
    original = deepcopy(snapshot)
    monkeypatch.setattr(
        RNGState, "restore", lambda *a: pytest.fail("dead concentration source restored RNG")
    )
    result = evaluate_concentration_candidate(
        request.model_copy(update={"state_snapshot": snapshot}), loader=loader
    )
    assert result.status == "unsupported"
    assert result.state_delta is None
    assert result.rng_transition is None
    assert not result.proposed_events
    assert snapshot == original
