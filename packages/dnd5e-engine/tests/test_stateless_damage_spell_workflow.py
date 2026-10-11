"""A complete class caster can continue through actual spell and turn operations."""

import asyncio

from dnd5e_engine import orchestrator as orch
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_snapshot import capture_combat_snapshot
from dnd5e_engine.specs import GridScene
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import execute
from tests.test_stateless_concentration import apply_candidate
from tests.test_stateless_damage_spell import damage_spell_case
from tests.test_stateless_item_closure_core import isolate


def test_full_cleric_cast_pass_reset_cast_workflow(monkeypatch):
    request, handle, live, loader = damage_spell_case(seed=2, empty_slots=True)
    steps = [
        (HERO, request.payload),
        (FOE, CombatIntentPayload(intent_type="pass")),
        (HERO, request.payload),
    ]
    for actor, payload in steps:
        asyncio.run(orch.submit_player_intent(handle, actor, payload))
    snapshot, rng = request.state_snapshot, request.rng_context
    isolate(monkeypatch)
    for index, (actor, payload) in enumerate(steps):
        invocation = request.model_copy(
            update={
                "command_id": f"cantrip-workflow:{index}",
                "actor_id": actor,
                "payload": payload,
                "state_snapshot": snapshot,
                "rng_context": rng,
            }
        )
        result = execute(invocation, loader)
        assert result.status == "accepted", result.error
        snapshot = apply_candidate(snapshot, result)
        rng = rng.model_copy(update={"state": result.rng_transition.next_state})
    assert snapshot == capture_combat_snapshot(
        live,
        grid=GridScene(width=3, height=3),
        world_version=7,
        combat_id=snapshot.combat_state.combat_id,
        inventory_state=snapshot.inventory_state,
        resource_state=snapshot.resource_state,
        casting_state=snapshot.casting_state,
    )
    assert rng.state == RNGState.capture(live.rng)
    assert snapshot.resource_state == request.state_snapshot.resource_state
