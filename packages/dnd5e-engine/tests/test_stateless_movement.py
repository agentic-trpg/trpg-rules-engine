"""R12 route/terrain/occupancy expectations, Legacy differential and isolation."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from pydantic import ValidationError

from dnd5e_engine import evaluation_delta as delta
from dnd5e_engine import evaluation_movement
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, RuleEvaluationResult
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_snapshot import capture_evaluation_snapshot
from dnd5e_engine.evaluation_state import MovementLedgerState
from dnd5e_engine.spatial import GridTopology
from dnd5e_engine.specs import GridScene
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import apply_delta, execute, request_and_live
from tests.test_stateless_item_closure_core import isolate


def move_case(*, npc=False, destination="2,0", kind="move", blocked=(), terrain=(), walls=()):
    request, handle, live = request_and_live()
    scene = GridScene(
        width=12,
        height=4,
        blocked_cells=list(blocked),
        difficult_terrain_cells=list(terrain),
        wall_segments=list(walls),
    )
    live.topology = GridTopology(scene)
    live.actor_zone = {HERO: "0,0", FOE: "10,3"}
    if npc:
        live.current_turn_index = 1
        live.current_actor_id = FOE
    snapshot = capture_evaluation_snapshot(live, request.state_snapshot, scene)
    intent = CombatIntentPayload(
        intent_type=kind, target_zone_id=destination if kind == "move" else None
    )
    return (
        request.model_copy(
            update={"state_snapshot": snapshot, "actor_id": FOE if npc else HERO, "payload": intent}
        ),
        handle,
        live,
    )


def parity(request, handle, live, result):
    offset = len(live.event_log)
    asyncio.run(orch.submit_player_intent(handle, request.actor_id, request.payload))
    expected = capture_evaluation_snapshot(
        live,
        request.state_snapshot,
        GridScene.model_validate(request.state_snapshot.scene_state.grid.model_dump()),
    )
    assert result.proposed_events == tuple(live.event_log[offset:])
    assert result.rng_transition.next_state == RNGState.capture(live.rng)
    assert apply_delta(request.state_snapshot, result) == expected
    return expected


@pytest.mark.parametrize(
    "npc,destination,terrain",
    [(False, "2,0", ()), (True, "8,3", ()), (False, "2,0", ("1,0", "1,1", "2,0", "2,1"))],
)
def test_position_cost_budget_event_rng_parity_and_isolation(
    monkeypatch, npc, destination, terrain
):
    request, handle, live = move_case(npc=npc, destination=destination, terrain=terrain)
    before = deepcopy(request)
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request)
        assert result.status == "accepted", result.error
        with ThreadPoolExecutor(max_workers=3) as pool:
            assert list(pool.map(lambda _: execute(request), range(6))) == [result] * 6
    after = parity(request, handle, live, result)
    position = next(
        op for op in result.state_delta.operations if isinstance(op, delta.PositionUpdate)
    )
    assert position.actor_id == request.actor_id
    assert position.expected == request.state_snapshot.combat_state.actor_zone[request.actor_id]
    assert position.value == destination
    assert position.path[0] == position.expected
    assert (
        after.combat_state.current_turn_index
        == request.state_snapshot.combat_state.current_turn_index
    )
    assert after.combat_state.movement_ledgers[request.actor_id].spent_ft >= 10
    assert not result.rng_transition.state_changed
    assert request == before
    assert RuleEvaluationResult.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(AssertionError):
        apply_delta(after, result)
    stale = request.state_snapshot.model_copy(update={"world_version": 8})
    with pytest.raises(AssertionError):
        apply_delta(stale, result)


@pytest.mark.parametrize("kind", ["dash", "disengage"])
@pytest.mark.parametrize("npc", [False, True])
def test_dash_disengage_independent_budget_and_no_turn_advance(monkeypatch, kind, npc):
    request, handle, live = move_case(kind=kind, npc=npc)
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request)
    assert result.status == "accepted", result.error
    after = parity(request, handle, live, result)
    actor = next(a for a in after.character_states if a.entity_id == request.actor_id)
    assert not actor.action_available
    assert actor.action_taken_this_turn
    assert actor.bonus_action_available
    assert actor.disengaging_this_turn == (kind == "disengage")
    assert actor.movement_remaining == (60 if kind == "dash" else 30)
    assert after.combat_state.movement_ledgers[actor.entity_id].dash_count == int(kind == "dash")
    assert after.combat_state.turn_serial == request.state_snapshot.combat_state.turn_serial


@pytest.mark.parametrize(
    "destination,blocked,code",
    [
        (None, (), "movement.destination"),
        ("0,0", (), "movement.destination"),
        ("99,0", (), "movement.destination"),
        ("10,3", (), "movement.occupied"),
        ("7,0", (), "movement.insufficient"),
        ("1,0", ("1,0",), "movement.destination"),
        ("2,0", ("1,0", "1,1", "1,2", "1,3"), "movement.unreachable"),
    ],
)
def test_invalid_routes_refuse_before_rng(monkeypatch, destination, blocked, code):
    request, _, _ = move_case(destination=destination, blocked=blocked)
    before = deepcopy(request)
    monkeypatch.setattr(RNGState, "restore", lambda _: pytest.fail("RNG restored after refusal"))
    result = execute(request)
    assert result.status == "rejected"
    assert result.error.code == code
    assert result.state_delta is None
    assert result.rng_transition is None
    assert not result.proposed_events
    assert request == before


def test_blocked_detour_and_terrain_use_shared_weighted_path(monkeypatch):
    request, handle, live = move_case(destination="3,0", blocked=("1,0",), terrain=("1,1",))
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request)
    assert result.status == "accepted", result.error
    after = parity(request, handle, live, result)
    op = next(op for op in result.state_delta.operations if isinstance(op, delta.PositionUpdate))
    assert "1,0" not in op.path
    assert after.combat_state.movement_ledgers[HERO].spent_ft == 20


def test_oa_guard_entire_route_then_disengage_sequence(monkeypatch):
    request, handle, live = move_case(destination="3,0")
    live.actor_zone[FOE] = "1,0"
    scene = GridScene(width=12, height=4)
    request = request.model_copy(
        update={"state_snapshot": capture_evaluation_snapshot(live, request.state_snapshot, scene)}
    )
    before = deepcopy(request)
    with monkeypatch.context() as scoped:
        isolate(scoped)
        scoped.setattr(RNGState, "restore", lambda _: pytest.fail("OA refusal restored RNG"))
        result = execute(request)
    assert result.status == "unsupported"
    assert result.error.code == "movement.opportunity_attack"
    assert request == before
    assert result.state_delta is None
    disengage = request.model_copy(update={"payload": CombatIntentPayload(intent_type="disengage")})
    with monkeypatch.context() as scoped:
        isolate(scoped)
        paid = execute(disengage)
    after = parity(disengage, handle, live, paid)
    move = request.model_copy(update={"state_snapshot": after})
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(move)
    assert result.status == "accepted", result.error
    parity(move, handle, live, result)


@pytest.mark.parametrize(
    "mutation", ["bonus", "forced", "effects", "spent", "mode", "ledger", "occupancy"]
)
def test_unmigrated_dependencies_and_spent_budget_no_payment(monkeypatch, mutation):
    request, _, _ = move_case(kind="dash" if mutation in {"spent", "bonus"} else "move")
    state = request.state_snapshot.combat_state
    actor = request.state_snapshot.character_states[0]
    if mutation == "bonus":
        request.payload.use_bonus_action = True
    elif mutation == "forced":
        request.payload.shove_push = True
    elif mutation == "effects":
        state.__dict__["concentration_chain"] = {HERO: [(HERO, "e", "o")]}
    elif mutation == "spent":
        actor.__dict__["action_available"] = False
    elif mutation == "mode":
        request.payload.movement_mode = "swim"
    elif mutation == "ledger":
        state.movement_ledgers[HERO] = MovementLedgerState(
            spent_ft=5, distance_ft=5, active_mode="walk", dash_count=0
        )
    else:
        state.actor_zone[FOE] = state.actor_zone[HERO]
    monkeypatch.setattr(RNGState, "restore", lambda _: pytest.fail("refusal restored RNG"))
    result = execute(request)
    assert result.status == ("rejected" if mutation == "spent" else "unsupported")
    assert result.state_delta is None
    assert not result.proposed_events


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_fault_after_local_move_has_no_external_residue(monkeypatch, failure):
    request, _, live = move_case()
    before = deepcopy(request)
    registry = orch._REGISTRY
    before_registry = dict(registry)
    original = evaluation_movement.attack_delta

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise failure("after local move")

    isolate(monkeypatch)
    monkeypatch.setattr(evaluation_movement, "attack_delta", fail)
    with pytest.raises(failure, match="after local move"):
        execute(request)
    assert request == before
    assert dict(registry) == before_registry
    assert live.actor_zone[HERO] == "0,0"


def test_position_delta_closed_and_canonical():
    with pytest.raises(ValidationError):
        delta.PositionUpdate(
            kind="combat.position_update",
            combat_id="c",
            scene_id="s",
            actor_id="a",
            expected="0,0",
            value="1,0",
            path=("0,0", "2,0"),
        )


def test_dash_then_long_move_preserves_paid_budget_and_turn(monkeypatch):
    request, handle, live = move_case(kind="dash")
    with monkeypatch.context() as scoped:
        isolate(scoped)
        paid = execute(request)
    after = parity(request, handle, live, paid)
    move = request.model_copy(
        update={
            "state_snapshot": after,
            "payload": CombatIntentPayload(intent_type="move", target_zone_id="9,0"),
        }
    )
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(move)
    assert result.status == "accepted", result.error
    after = parity(move, handle, live, result)
    assert after.character_states[0].movement_remaining == 15
    assert not after.character_states[0].action_available
    assert after.combat_state.movement_ledgers[HERO].spent_ft == 45
    assert after.combat_state.current_turn_index == 0


def test_wall_blocked_adjacent_step_refuses_before_rng(monkeypatch):
    from dnd5e_engine.specs import WallSegment

    request, _, _ = move_case(
        destination="1,0", walls=(WallSegment(x1=1.0, y1=0.0, x2=1.0, y2=1.0),)
    )
    monkeypatch.setattr(RNGState, "restore", lambda _: pytest.fail("wall refusal restored RNG"))
    result = execute(request)
    assert result.status == "rejected"
    assert result.error.code == "movement.blocked"
    assert result.state_delta is None


def test_actual_weapon_reach_boundary_is_not_hidden_by_longer_body_reach(monkeypatch):
    from tests.evaluation_support import WEAPON

    request, _, live = move_case(npc=True, destination="2,0")
    live.actor_zone = {HERO: "0,0", FOE: "1,0"}
    live.initiative[0] = live.initiative[0].model_copy(update={"melee_reach_ft": 10})
    live.opportunity_attack_weapons[HERO] = WEAPON
    snapshot = capture_evaluation_snapshot(
        live, request.state_snapshot, GridScene(width=12, height=4)
    )
    request = request.model_copy(update={"state_snapshot": snapshot})
    monkeypatch.setattr(
        RNGState, "restore", lambda _: pytest.fail("weapon OA refusal restored RNG")
    )
    result = execute(request)
    assert result.status == "unsupported"
    assert result.error.code == "movement.opportunity_attack"


def test_allied_pass_through_uses_same_occupancy_and_cost_as_legacy(monkeypatch):
    from dnd5e_engine.movement import MovementLedger

    request, handle, live = move_case(destination="2,0")
    ally = live.initiative[0].model_copy(
        update={"entity_id": "char:ally", "weapon_in_hands": None, "weapon_grip": "none"}
    )
    live.initiative.append(ally)
    live.party_ids.add(ally.entity_id)
    live.actor_zone[ally.entity_id] = "1,0"
    live.movement_ledgers[ally.entity_id] = MovementLedger()
    live.tracked_hp[ally.entity_id] = 40
    from dnd5e_engine.evaluation_snapshot import capture_combat_snapshot

    live.actor_zone[FOE] = "10,0"
    snapshot = capture_combat_snapshot(
        live,
        grid=GridScene(width=12, height=1),
        inventory_state=request.state_snapshot.inventory_state,
        world_version=request.state_snapshot.world_version,
        combat_id=request.state_snapshot.combat_state.combat_id,
    )
    live.topology = GridTopology(GridScene(width=12, height=1))
    request = request.model_copy(update={"state_snapshot": snapshot})
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request)
    assert result.status == "accepted", result.error
    after = parity(request, handle, live, result)
    assert after.combat_state.movement_ledgers[HERO].spent_ft == 10
    position = next(
        op for op in result.state_delta.operations if isinstance(op, delta.PositionUpdate)
    )
    assert position.path == ("0,0", "1,0", "2,0")


def test_terrain_move_then_explicit_pass_completes_same_lifecycle(monkeypatch):
    request, handle, live = move_case(destination="2,0", terrain=("2,0",))
    with monkeypatch.context() as scoped:
        isolate(scoped)
        moved = execute(request)
    assert moved.status == "accepted", moved.error
    after = parity(request, handle, live, moved)
    end = request.model_copy(
        update={"state_snapshot": after, "payload": CombatIntentPayload(intent_type="pass")}
    )
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(end)
    assert result.status == "accepted", result.error
    after = parity(end, handle, live, result)
    assert after.combat_state.current_turn_index == 1
    assert after.character_states[1].movement_remaining == 30
