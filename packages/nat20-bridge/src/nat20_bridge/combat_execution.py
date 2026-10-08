"""Serialized, cancellation-safe HTTP receipts around public Engine mutations."""

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from dnd5e_engine import get_live
from dnd5e_engine.orchestrator import IntentRejectedError, UnknownHandleError
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from nat20_bridge.state import BridgeState

_LOGGER = logging.getLogger(__name__)


async def execute(
    state: BridgeState,
    cid: str,
    operation: str,
    payload: dict[str, Any],
    request_id: str | None,
    action: Callable[[], Awaitable[dict[str, Any]]],
    drain: Callable[[], Awaitable[None]],
) -> JSONResponse:
    """Hold execution, event draining and receipt publication in one owned task.

    Receipts last for this Bridge process, including closed combats. Retrying
    without an ID remains legacy best effort. A cached failure is terminal for
    that ID; corrected/reconsidered commands require a new ID.
    """
    task = asyncio.create_task(_execute(state, cid, operation, payload, request_id, action, drain))
    state.mutation_tasks.add(task)
    task.add_done_callback(state.mutation_tasks.discard)
    return await asyncio.shield(task)


async def _execute(
    state: BridgeState,
    cid: str,
    operation: str,
    payload: dict[str, Any],
    request_id: str | None,
    action: Callable[[], Awaitable[dict[str, Any]]],
    drain: Callable[[], Awaitable[None]],
) -> JSONResponse:
    fingerprint = hashlib.sha256(
        json.dumps([operation, payload], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    async with state.combat_locks.setdefault(cid, asyncio.Lock()):
        key = (cid, request_id or "")
        if request_id and key in state.receipts:
            previous, status, body = state.receipts[key]
            if previous != fingerprint:
                return JSONResponse(
                    status_code=409,
                    content={"detail": {"reason": "request_id_conflict", "status": "not_executed"}},
                )
            return JSONResponse(status_code=status, content=body)
        handle = state.combats.get(cid)
        initial_handles = set(state.combats)
        before = get_live(handle).execution_serial if handle else 0
        start = get_live(handle).event_count if handle else 0
        status, outcome, reason, body = await _invoke(
            action,
            lambda: (
                get_live(handle).execution_serial > before or get_live(handle).event_count > start
                if handle
                else bool(set(state.combats) - initial_handles)
            ),
        )
        actual_cid = (
            next(iter(set(state.combats) - initial_handles), cid) if cid == "__start__" else cid
        )
        handle = handle or state.combats.get(actual_cid)
        serial = get_live(handle).execution_serial if handle else 0
        receipt = {"request_id": request_id, "status": outcome, "execution_serial": serial}
        # Publish a primitive-only recovery receipt BEFORE fallible drain,
        # narration or serialization. The owned lock prevents intermediate reads.
        fallback = {
            "combat_id": actual_cid,
            "events": [],
            "receipt": receipt,
            "detail": {"reason": reason or "committed_response_failed", "status": outcome},
        }
        fallback_status = status if reason else 503
        if request_id:
            state.receipts[key] = (fingerprint, fallback_status, fallback)
        try:
            await drain()
            events = state.events_log.get(actual_cid, [])[start:]
            body = body or {
                "combat_id": actual_cid,
                "events": [e.model_dump(mode="json") for e in events],
            }
            if operation == "end" and handle is not None:
                final = get_live(handle).final_outcome
                if final is not None:
                    body["outcome"] = final.model_dump(mode="json")
            failure = _primary_failure(body.get("events", []), payload.get("actor_id"))
            if status == 200 and failure:
                status, outcome, reason = 409, "rule_refused", failure["reason"]
            body["receipt"] = receipt | {"status": outcome}
            if reason:
                body["detail"] = {"reason": reason, "status": outcome}
            body = jsonable_encoder(body)
            response = JSONResponse(status_code=status, content=body)
        except Exception:
            _LOGGER.exception("combat response publication failed")
            status, body = fallback_status, fallback
            response = JSONResponse(status_code=status, content=body)
        if request_id:
            state.receipts[key] = (fingerprint, status, body)
        return response


def _primary_failure(events: list[dict[str, Any]], actor_id: str | None) -> dict[str, Any] | None:
    # A pre-execution refusal is the actor's leading result, before its
    # IntentSubmitted. Later reaction/child failures remain committed events.
    first = next((event for event in events if event.get("actor_id") == actor_id), None)
    return first if first and first["type"].endswith("_failed") else None


async def _invoke(
    action: Callable[[], Awaitable[dict[str, Any]]], committed: Callable[[], bool]
) -> tuple[int, str, Any, dict[str, Any]]:
    try:
        return 200, "committed", None, await action()
    except IntentRejectedError as error:
        return 409, "rule_refused", error.reason, {}
    except UnknownHandleError:
        return 404, "not_executed", "unknown_combat", {}
    except HTTPException as error:
        reason = error.detail.get("reason") if isinstance(error.detail, dict) else error.detail
        return error.status_code, "not_executed", reason, {}
    except Exception:
        _LOGGER.exception("combat operation failed")
        if committed():
            return 503, "committed", "committed_response_failed", {}
        return 500, "rolled_back", "engine_unexpected_error", {}
