"""Authoritative close and commit receipts survive observer failures."""

import asyncio

import pytest

from dnd5e_engine import end_combat, get_live
from dnd5e_engine.events import CastFailed, CombatEnded
from tests.c21_support import pc, start


def test_end_listener_failure_is_after_authoritative_close():
    handle, live = start([pc()], seed=19)

    def broken(event):
        if isinstance(event, CombatEnded):
            raise RuntimeError("observer")  # noqa: TRY004 - injected observer fault

    live.event_listeners.append(broken)
    with pytest.raises(RuntimeError, match="observer"):
        asyncio.run(end_combat(handle))
    assert get_live(handle).ended
    assert get_live(handle).final_outcome is not None
    assert list(live.event_queue._queue)[-1] is None
    before = get_live(handle)
    assert asyncio.run(end_combat(handle)).events == []
    assert get_live(handle) == before
    assert len([e for e in live.event_log if isinstance(e, CombatEnded)]) == 1


def test_close_inside_failed_outer_transaction_restores_registry_and_sentinel():
    from dnd5e_engine import orchestrator

    handle, live = start([pc()], seed=21)
    ended_before = list(orchestrator._ENDED)
    before = get_live(handle)
    pending = list(live.event_queue._queue)

    def failed_outer():
        with orchestrator._execution_transaction(live):
            asyncio.run(end_combat(handle))
            raise RuntimeError("outer")

    with pytest.raises(RuntimeError, match="outer"):
        failed_outer()
    assert get_live(handle) == before
    assert list(live.event_queue._queue) == pending
    assert list(orchestrator._ENDED) == ended_before


def test_pure_refusal_listener_is_publication_without_gameplay_serial_change():
    from dnd5e_engine import PlayerIntent, submit_player_intent

    handle, live = start([pc()], seed=23)
    before = get_live(handle)

    def broken(event):
        if isinstance(event, CastFailed):
            raise RuntimeError("refusal observer")  # noqa: TRY004

    live.event_listeners.append(broken)
    with pytest.raises(RuntimeError, match="refusal observer"):
        asyncio.run(
            submit_player_intent(
                handle, "char:hero", PlayerIntent(intent_type="cast_spell", spell_id="wish")
            )
        )
    after = get_live(handle)
    assert after.execution_serial == before.execution_serial
    assert after.event_count == before.event_count + 1
    assert after.spell_slots_by_entity == before.spell_slots_by_entity
    assert after == before  # Event history is separate from gameplay equality.
