"""Real Wizard initial saves, atomic graph consumer and subsequent R28 turns."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from random import Random

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from pydantic import ValidationError

from dnd5e_engine import evaluation_hold_cast
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.evaluation_casting import HoldPersonMaterial
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, RuleEvaluationResult
from dnd5e_engine.evaluation_delta import ConcentrationUpdate, EffectLifecycleUpdate
from dnd5e_engine.evaluation_hold_delta import HoldPersonGraphCreate
from dnd5e_engine.evaluation_resources import ResourceState
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import RulesetBindingError, ruleset_binding
from dnd5e_engine.evaluation_snapshot import capture_combat_snapshot, capture_evaluation_snapshot
from dnd5e_engine.evaluation_state import CombatSnapshot, InventoryEntry, InventoryState
from dnd5e_engine.rules.character import proficiency_grants
from dnd5e_engine.specs import GridScene
from tests.evaluation_support import FOE, HERO, WEAPON
from tests.test_stateless_attack import apply_delta, execute
from tests.test_stateless_attack_concentration import ATTACKER, attack_concentration_case
from tests.test_stateless_hold_turn import isolated
from tests.test_stateless_spell_feature import apply_healing


def cast_case(*, seed=0, multi=False, pc=False, immune=False):
    request, handle, live, _ = attack_concentration_case(seed=seed, pc=pc)
    canonical = BundledAssetLoader()
    caster = next(a for a in live.initiative if a.entity_id == FOE)
    old_source = request.state_snapshot.casting_state.actors[0].sources[0]
    known = (
        *old_source.known_spells,
        "mirror-image",
        "feather-fall",
        "light",
        "mage-hand",
        "fire-bolt",
    )
    loader = MemoryAssetLoader(
        items=[canonical.get_item("component-pouch"), live.ruleset_loader.get_weapon(WEAPON)],
        spells=[canonical.get_spell(s) for s in known],
        classes=[canonical.get_class("wizard")],
        subclasses=[canonical.get_subclass("evoker")],
        features=[canonical.get_feature(s) for s in caster.granted_features],
        conditions=[canonical.get_condition("paralyzed")],
    )
    live.ruleset_loader = loader
    live.active_effects.clear()
    live.active_conditions.clear()
    live.conditions_by_effect.clear()
    live.effect_lifecycles.clear()
    live.concentration_chain.clear()
    live.concentration_rounds_remaining.clear()
    live.expended_resources.clear()
    grants = proficiency_grants([(canonical.get_class("wizard"), 5, "primary")])
    actors = []
    for a in live.initiative:
        update = {
            "conditions": [],
            "concentration_effect_id": None,
            "creature_type": "humanoid",
            "movement_remaining": 30,
        }
        if a.entity_id == FOE:
            update.update(
                weapon_in_hands=None,
                weapon_grip="none",
                carried_item_slugs=("component-pouch",),
                weapon_proficiencies=sorted(grants.weapons),
                ac=10 + (a.dexterity - 10) // 2,
                attack_bonus=None,
            )
        if a.entity_id == HERO and immune:
            update["condition_immunities"] = ["paralyzed"]
        actors.append(a.model_copy(update=update))
    live.initiative = actors
    live.current_turn_index = 2
    live.current_actor_id = FOE
    live.turn_serial = 3
    live.spell_slots_by_entity[FOE] = {1: 4, 2: 3, 3: 2}
    live.spells_known_by_entity[FOE] = list(known)
    resources = ResourceState(
        pools=tuple(
            p.model_copy(update={"current": p.maximum})
            for p in request.state_snapshot.resource_state.pools
        )
    )
    source = old_source.model_copy(
        update={
            "known_spells": known,
            "prepared_spells": ("hold-person", *tuple(s for s in known if s != "hold-person")[:8]),
        }
    )
    casting = request.state_snapshot.casting_state.model_copy(
        update={
            "actors": (
                request.state_snapshot.casting_state.actors[0].model_copy(
                    update={
                        "sources": (source,),
                        "materials": (
                            HoldPersonMaterial(
                                spell_slug="hold-person",
                                source_id="class:wizard",
                                inventory_instance_id="pouch:wizard",
                                available=True,
                            ),
                        ),
                    }
                ),
            )
        }
    )
    inventory = InventoryState(
        entries=(
            InventoryEntry(
                instance_id="pouch:wizard",
                owner_id=FOE,
                item_slug="component-pouch",
                quantity=1,
                charges_remaining_per_unit=0,
                accessible=True,
            ),
        )
    )
    live.event_log.clear()
    while not live.event_queue.empty():
        live.event_queue.get_nowait()
    snapshot = capture_combat_snapshot(
        live,
        grid=GridScene(width=3, height=3),
        world_version=7,
        combat_id=request.state_snapshot.combat_state.combat_id,
        inventory_state=inventory,
        casting_state=casting,
        resource_state=resources,
    )
    intent = CombatIntentPayload(
        intent_type="cast_spell",
        spell_id="hold-person",
        spell_source_id="class:wizard",
        target_id=HERO,
        target_ids=(HERO, ATTACKER) if multi else None,
        slot_level=3 if multi else 2,
    )
    return (
        request.model_copy(
            update={
                "actor_id": FOE,
                "payload": intent,
                "state_snapshot": snapshot,
                "ruleset_binding": ruleset_binding(loader),
            }
        ),
        handle,
        live,
        loader,
    )


def apply_cast(snapshot, result, *, world_version=None):
    assert result.state_delta.expected_world_version == (
        snapshot.world_version if world_version is None else world_version
    )
    op = next(o for o in result.state_delta.operations if isinstance(o, HoldPersonGraphCreate))
    assert op.combat_id == snapshot.combat_state.combat_id
    assert op.actor_ids == snapshot.combat_state.initiative_ids
    assert not snapshot.effect_states
    assert not snapshot.combat_state.effect_lifecycles
    assert not snapshot.combat_state.conditions_by_effect
    assert not snapshot.combat_state.concentration_chain
    assert not snapshot.combat_state.concentration_rounds_remaining
    assert all(
        not a.conditions and a.concentration_effect_id is None for a in snapshot.character_states
    )
    filtered = result.model_copy(
        update={
            "state_delta": result.state_delta.model_copy(
                update={
                    "operations": tuple(
                        o
                        for o in result.state_delta.operations
                        if not isinstance(o, HoldPersonGraphCreate)
                    )
                }
            )
        }
    )
    after = apply_healing(snapshot, filtered)
    conditions = {e.effect.target_id: ([e.condition] if e.condition else []) for e in op.entries}
    after = after.model_copy(
        update={
            "effect_states": tuple(e.effect for e in op.entries),
            "character_states": tuple(
                a.model_copy(
                    update={
                        "conditions": conditions.get(a.entity_id, []),
                        "concentration_effect_id": op.concentration_id
                        if a.entity_id == op.source_id
                        else None,
                    }
                )
                for a in after.character_states
            ),
            "combat_state": after.combat_state.model_copy(
                update={
                    "concentration_chain": {op.source_id: list(op.chain)},
                    "concentration_rounds_remaining": {op.source_id: op.rounds},
                    "effect_lifecycles": tuple(e.clock for e in op.entries if e.clock is not None),
                    "conditions_by_effect": tuple(e.lineage for e in op.entries),
                }
            ),
        }
    )
    return CombatSnapshot.model_validate_json(after.model_dump_json())


def apply_existing(snapshot, result):
    """Guard entire graph first, then build a private new value, including empty anchors."""
    ops = [
        o
        for o in result.state_delta.operations
        if isinstance(o, (ConcentrationUpdate, EffectLifecycleUpdate))
    ]
    for op in ops:
        assert op.combat_id == snapshot.combat_state.combat_id
        if isinstance(op, EffectLifecycleUpdate):
            assert op.expected_effect in snapshot.effect_states
            assert op.expected_lineage in snapshot.combat_state.conditions_by_effect
            if op.expected_lifecycle is not None:
                assert op.expected_lifecycle in snapshot.combat_state.effect_lifecycles
            else:
                assert not any(
                    c.identity == op.identity for c in snapshot.combat_state.effect_lifecycles
                )
        else:
            assert (
                tuple(snapshot.combat_state.concentration_chain[op.source_id]) == op.expected_chain
            )
            assert (
                snapshot.combat_state.concentration_rounds_remaining[op.source_id]
                == op.expected_rounds
            )
            assert (
                next(
                    a for a in snapshot.character_states if a.entity_id == op.source_id
                ).concentration_effect_id
                == op.expected_effect_id
            )
    filtered = result.model_copy(
        update={
            "state_delta": result.state_delta.model_copy(
                update={
                    "operations": tuple(
                        o
                        for o in result.state_delta.operations
                        if not isinstance(o, (ConcentrationUpdate, EffectLifecycleUpdate))
                    )
                }
            )
        }
    )
    after = apply_delta(snapshot, filtered)
    active = {(e.target_id, e.id, e.origin): e for e in after.effect_states}
    clocks = {r.identity: r for r in after.combat_state.effect_lifecycles}
    links = {r.identity: r for r in after.combat_state.conditions_by_effect}
    chains = dict(after.combat_state.concentration_chain)
    rounds = dict(after.combat_state.concentration_rounds_remaining)
    actors = list(after.character_states)
    for op in ops:
        if isinstance(op, EffectLifecycleUpdate):
            for mapping, value in (
                (active, op.effect),
                (clocks, op.lifecycle),
                (links, op.lineage),
            ):
                if value is None:
                    mapping.pop(op.identity, None)
                else:
                    mapping[op.identity] = value
        else:
            actors = [
                a.model_copy(update={"concentration_effect_id": op.effect_id})
                if a.entity_id == op.source_id
                else a
                for a in actors
            ]
            if op.chain:
                chains[op.source_id] = list(op.chain)
                rounds[op.source_id] = op.rounds
            else:
                chains.pop(op.source_id)
                rounds.pop(op.source_id)
    return CombatSnapshot.model_validate_json(
        after.model_copy(
            update={
                "character_states": tuple(actors),
                "effect_states": tuple(active.values()),
                "combat_state": after.combat_state.model_copy(
                    update={
                        "effect_lifecycles": tuple(clocks.values()),
                        "conditions_by_effect": tuple(links.values()),
                        "concentration_chain": chains,
                        "concentration_rounds_remaining": rounds,
                    }
                ),
            }
        ).model_dump_json()
    )


def step(request, handle, live, loader, monkeypatch, intent=None):
    if intent is not None:
        state = request.state_snapshot.combat_state
        request = request.model_copy(
            update={"payload": intent, "actor_id": state.initiative_ids[state.current_turn_index]}
        )
    before = deepcopy(request)
    offset = len(live.event_log)
    asyncio.run(orch.submit_player_intent(handle, request.actor_id, request.payload))
    with monkeypatch.context() as scoped:
        isolated(scoped)
        for name in (
            "_apply_concentration_anchor",
            "_fold_resolution_outcome",
            "_record_effect_lifecycle_links",
        ):
            scoped.setattr(orch, name, lambda *args, **kwargs: pytest.fail("Legacy cast fold"))
        result = execute(request, loader)
    assert result.status == "accepted", result.error
    after = (apply_cast if request.payload.intent_type == "cast_spell" else apply_existing)(
        request.state_snapshot, result
    )
    expected = capture_evaluation_snapshot(live, after, GridScene(width=3, height=3))
    assert after == expected
    assert result.proposed_events == tuple(live.event_log[offset:])
    assert result.rng_transition.next_state == RNGState.capture(live.rng)
    assert RuleEvaluationResult.model_validate_json(result.model_dump_json()) == result
    assert request == before
    return request.model_copy(
        update={
            "state_snapshot": after,
            "rng_context": request.rng_context.model_copy(
                update={"state": result.rng_transition.next_state}
            ),
        }
    ), result


@pytest.mark.parametrize("pc", [False, True])
@pytest.mark.parametrize("multi", [False, True])
@pytest.mark.parametrize("seed", [0, 1, 5, 6, 31])
def test_initial_real_cast_save_graph_and_cost_full_legacy_parity(pc, multi, seed, monkeypatch):
    request, handle, live, loader = cast_case(seed=seed, multi=multi, pc=pc)
    after, result = step(request, handle, live, loader, monkeypatch)
    saves = [e for e in result.proposed_events if e.type == "save_rolled"]
    rng = Random(seed)
    assert [e.natural for e in saves] == [rng.randint(1, 20) for _ in saves]
    assert [e.dc for e in saves] == [15] * len(saves)
    assert [e.modifier for e in saves] == [0] * len(saves)
    assert all(e.succeeded == (e.natural >= 15) for e in saves)
    failures = [e.target_id for e in saves if not e.succeeded]
    if failures:
        assert [e.target_id for e in after.state_snapshot.effect_states] == failures
        assert all(e.statuses == {"paralyzed"} for e in after.state_snapshot.effect_states)
    else:
        assert len(after.state_snapshot.effect_states) == 1
        assert after.state_snapshot.effect_states[0].flags == {
            "concentration": True,
            "concentration_anchor": True,
        }
        assert not after.state_snapshot.combat_state.effect_lifecycles
    assert after.state_snapshot.combat_state.concentration_rounds_remaining == {FOE: 9}
    caster = next(a for a in after.state_snapshot.character_states if a.entity_id == FOE)
    assert caster.spell_slots[3 if multi else 2] == (1 if multi else 2)
    assert caster.action_available is False
    assert after.state_snapshot.inventory_state == request.state_snapshot.inventory_state
    assert after.state_snapshot.casting_state == request.state_snapshot.casting_state


@pytest.mark.parametrize("seed,multi", [(0, True), (6, True), (5, False), (31, False)])
def test_cast_into_existing_lifecycle_cleanup_and_later_attack(seed, multi, monkeypatch):
    request, handle, live, loader = cast_case(seed=seed, multi=multi)
    request, _ = step(request, handle, live, loader, monkeypatch)
    partial = False
    for _ in range(32):
        count = len(request.state_snapshot.effect_states)
        request, _ = step(
            request, handle, live, loader, monkeypatch, CombatIntentPayload(intent_type="pass")
        )
        partial |= 0 < len(request.state_snapshot.effect_states) < count
        if not request.state_snapshot.effect_states:
            break
    assert not request.state_snapshot.effect_states
    assert not request.state_snapshot.combat_state.concentration_chain
    assert all(not a.conditions for a in request.state_snapshot.character_states)
    if seed == 0 and multi:
        assert partial
    while (
        request.state_snapshot.combat_state.initiative_ids[
            request.state_snapshot.combat_state.current_turn_index
        ]
        != ATTACKER
    ):
        request, _ = step(
            request, handle, live, loader, monkeypatch, CombatIntentPayload(intent_type="pass")
        )
    request, result = step(
        request,
        handle,
        live,
        loader,
        monkeypatch,
        CombatIntentPayload(intent_type="attack", weapon_id=WEAPON, target_id=FOE),
    )
    assert any(e.type == "attack_rolled" for e in result.proposed_events)


def test_condition_immunity_pays_slot_creates_only_anchor(monkeypatch):
    request, handle, live, loader = cast_case(seed=1, immune=True)
    after, result = step(request, handle, live, loader, monkeypatch)
    assert next(e for e in result.proposed_events if e.type == "save_rolled").succeeded is False
    assert not any(e.type == "condition_applied" for e in result.proposed_events)
    assert after.state_snapshot.effect_states[0].id == "effect:hold-person"


@pytest.mark.parametrize("seed", [1, 5, 6])
def test_actual_cast_then_attack_concentration_break(seed, monkeypatch):
    request, handle, live, loader = cast_case(seed=seed)
    request, _ = step(request, handle, live, loader, monkeypatch)
    request, result = step(
        request,
        handle,
        live,
        loader,
        monkeypatch,
        CombatIntentPayload(intent_type="attack", weapon_id=WEAPON, target_id=FOE),
    )
    roll = next(e for e in result.proposed_events if e.type == "attack_rolled")
    checks = [e for e in result.proposed_events if e.type == "concentration_check"]
    if roll.is_hit:
        assert len(checks) == 1
        assert checks[0].dc == 10
        if not checks[0].succeeded:
            assert not request.state_snapshot.effect_states
            assert not request.state_snapshot.combat_state.concentration_chain


def test_initial_partial_immunity_keeps_only_nonimmune_target(monkeypatch):
    request, handle, live, loader = cast_case(seed=0, multi=True, immune=True)
    request, result = step(request, handle, live, loader, monkeypatch)
    assert len([e for e in result.proposed_events if e.type == "save_rolled"]) == 2
    assert [e.target_id for e in request.state_snapshot.effect_states] == [ATTACKER]


@pytest.mark.parametrize(
    "case",
    [
        "material",
        "verbal",
        "hand",
        "prepared",
        "source",
        "slot",
        "grant",
        "humanoid",
        "range",
        "count",
    ],
)
def test_refusals_before_rng_or_payment(case, monkeypatch):
    request, _, _, loader = cast_case()
    snap = request.state_snapshot
    entry = snap.casting_state.actors[0]
    actor = next(a for a in snap.character_states if a.entity_id == FOE)
    if case == "material":
        entry = entry.model_copy(update={"materials": ()})
    elif case == "verbal":
        entry = entry.model_copy(update={"verbal_available": False})
    elif case == "prepared":
        entry = entry.model_copy(
            update={"sources": (entry.sources[0].model_copy(update={"prepared_spells": ()}),)}
        )
    elif case == "source":
        request = request.model_copy(
            update={"payload": request.payload.model_copy(update={"spell_source_id": "wrong"})}
        )
    elif case == "hand":
        actor = actor.model_copy(update={"other_hand_occupied": True})
    elif case == "grant":
        actor = actor.model_copy(update={"granted_features": ()})
    elif case == "humanoid":
        snap = snap.model_copy(
            update={
                "character_states": tuple(
                    a.model_copy(update={"creature_type": "undead"}) if a.entity_id == HERO else a
                    for a in snap.character_states
                )
            }
        )
    elif case == "range":
        snap = snap.model_copy(
            update={
                "scene_state": snap.scene_state.model_copy(
                    update={"grid": snap.scene_state.grid.model_copy(update={"width": 30})}
                ),
                "combat_state": snap.combat_state.model_copy(
                    update={"actor_zone": {**snap.combat_state.actor_zone, HERO: "29,0"}}
                ),
            }
        )
    elif case == "count":
        request = request.model_copy(
            update={"payload": request.payload.model_copy(update={"target_ids": (HERO, ATTACKER)})}
        )
    elif case == "slot":
        actor = actor.model_copy(update={"spell_slots": {1: 4, 2: 0, 3: 2}})
        snap = snap.model_copy(
            update={
                "resource_state": ResourceState(
                    pools=tuple(
                        p.model_copy(update={"current": 0}) if getattr(p, "level", None) == 2 else p
                        for p in snap.resource_state.pools
                    )
                )
            }
        )
    snap = snap.model_copy(
        update={
            "character_states": tuple(
                actor if a.entity_id == FOE else a for a in snap.character_states
            ),
            "casting_state": snap.casting_state.model_copy(update={"actors": (entry,)}),
        }
    )
    request = request.model_copy(update={"state_snapshot": snap})
    original = deepcopy(request)
    monkeypatch.setattr(RNGState, "restore", lambda *_: pytest.fail("RNG before refusal"))
    result = execute(request, loader)
    assert result.status in ("unsupported", "rejected")
    assert result.state_delta is None
    assert result.rng_transition is None
    assert result.proposed_events == ()
    assert request == original


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
def test_fault_after_real_effect_application_is_atomic(error, monkeypatch):
    request, _, live, loader = cast_case(seed=1)
    original = deepcopy(request)
    old_events = tuple(live.event_log)
    resolver = evaluation_hold_cast.resolve_activity

    def broken(*args, **kwargs):
        resolver(*args, **kwargs)
        assert any(e.type == "effect_applied" for e in args[1].event_emitter.__self__.events)
        raise error("after actual Hold Person application")

    monkeypatch.setattr(evaluation_hold_cast, "resolve_activity", broken)
    with pytest.raises(error):
        execute(request, loader)
    assert request == original
    assert tuple(live.event_log) == old_events


def test_concurrent_replay_closed_graph_stale_and_old_versions(monkeypatch):
    request, _, _, loader = cast_case(seed=1, multi=True)
    isolated(monkeypatch)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: execute(request, loader), range(8)))
    assert all(r == results[0] for r in results)
    result = results[0]
    after = apply_cast(request.state_snapshot, result)
    with pytest.raises(AssertionError):
        apply_cast(after, result)
    with pytest.raises(AssertionError):
        apply_cast(request.state_snapshot, result, world_version=8)
    op = next(o for o in result.state_delta.operations if isinstance(o, HoldPersonGraphCreate))
    for patch in (
        {"entries": (*op.entries, op.entries[0])},
        {"source_id": HERO},
        {"rounds": 10},
        {"expected_concentration_id": "old"},
        {"selected_target_ids": ("missing",)},
    ):
        with pytest.raises(ValidationError):
            HoldPersonGraphCreate.model_validate(op.model_copy(update=patch))
    wire = request.model_dump(mode="json")
    wire["schema_version"] = "engine-evaluation/20"
    with pytest.raises(ValidationError):
        type(request).model_validate_json(__import__("json").dumps(wire))
    wire = request.state_snapshot.model_dump(mode="json")
    wire["snapshot_schema_version"] = "engine-snapshot/14"
    with pytest.raises(ValidationError):
        CombatSnapshot.model_validate_json(__import__("json").dumps(wire))
    prior = request.model_copy(
        update={
            "ruleset_binding": request.ruleset_binding.model_copy(
                update={"evaluator_version": "dnd5e-evaluation/33"}
            )
        }
    )
    with pytest.raises(RulesetBindingError):
        execute(prior, loader)


def test_material_fact_is_required_and_source_referenced():
    request, _, _, _ = cast_case()
    entry = request.state_snapshot.casting_state.actors[0]
    from dnd5e_engine.evaluation_casting import ActorCastingState

    wire = entry.model_dump(mode="json")
    del wire["materials"]
    with pytest.raises(ValidationError):
        ActorCastingState.model_validate_json(__import__("json").dumps(wire))
    wrong = entry.model_copy(
        update={"materials": (entry.materials[0].model_copy(update={"source_id": "missing"}),)}
    )
    with pytest.raises(ValidationError):
        ActorCastingState.model_validate(wrong)
