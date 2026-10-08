"""Combat lifecycle routes: start / intent / advance-monster / view / end.

Event-drain protocol
---------------------

``start_combat`` returns its opening events directly (``StartCombatResult
.events``), but ``submit_player_intent`` / ``advance_monster_turn`` return
``None`` — every event they emit goes exclusively onto the live combat's
internal ``asyncio.Queue``, drainable only through the public
``narration_events(handle)`` async iterator (it terminates only at
``end_combat``, when the engine pushes a ``None`` sentinel).

Each combat gets one persistent background collector task after ``start_combat``:

    async for event in narration_events(handle):
        state.events_log[cid].append(event)

It runs for the combat's lifetime. The adapter holds one combat lock across
execution, draining and receipt publication. The public authoritative
``event_count`` defines both the request's starting index and completion target;
the collector must reach it before a response can contain that request's delta.
This remains correct after an earlier response/drain failure. Bounded pumping
fails explicitly rather than reporting an incomplete delta. End awaits the
collector's sentinel shutdown. Cancellation cannot interrupt the lock-owned task.
"""

from __future__ import annotations

import asyncio
import random
import re
import secrets
from typing import Any

from dnd5e_engine import (
    CombatEvent,
    CombatHandle,
    EncounterMemberSpec,
    GridScene,
    PlayerIntent,
    advance_monster_turn,
    apply_strong_wind,
    cell_id,
    drain_pending_events,
    end_combat,
    get_live,
    make_build_spec,
    mutate_combat_object,
    narration_events,
    register_combat_objects,
    start_combat,
    submit_player_intent,
)
from dnd5e_engine.lib_loader import scoped_lib_loader
from dnd5e_engine.orchestrator import UnknownHandleError
from fastapi import APIRouter, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from nat20_bridge.combat_execution import execute
from nat20_bridge.combat_requests import (
    IntentRequest,
    MutateObjectRequest,
    MutationRequest,
    RegisterObjectsRequest,
    WindRequest,
)
from nat20_bridge.models import PartyValidateRequest, resolve_seed, slugify
from nat20_bridge.narrate import narrate
from nat20_bridge.sheet import derive_sheet
from nat20_bridge.state import BridgeState

_PUMP_MAX_ITERATIONS = 100
_PUMP_STABLE_CHECKS = 2

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")
_WHITESPACE_RE = re.compile(r"\s+")
_MAX_NAME_LEN = 80


def _sanitize_name(name: str, max_len: int = _MAX_NAME_LEN) -> str:
    """Neutralize prompt-injection vectors in combatant names.

    Homebrew monster/forge names flow verbatim into ``narrate()`` and end up
    in narration text handed to the host's LLM; a name carrying newlines,
    control characters, or an unbounded length is a prompt-injection /
    resource-exhaustion vector. Strip control characters, collapse all
    whitespace (including newlines) to single spaces, and cap length.
    """
    stripped = _CONTROL_CHARS_RE.sub(" ", name)
    collapsed = _WHITESPACE_RE.sub(" ", stripped).strip()
    return collapsed[:max_len]


def _ability_mod(score: int) -> int:
    return (score - 10) // 2


class _CombatStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    party: list[PartyValidateRequest]
    monsters: list[str]
    seed: int | None = None
    request_id: str | None = Field(default=None, strict=True, min_length=1, max_length=128)


_IntentRequest = IntentRequest


def _get_handle(state: BridgeState, cid: str) -> CombatHandle:
    handle = state.combats.get(cid)
    if handle is None:
        raise HTTPException(
            status_code=404, detail={"reason": "unknown_combat", "status": "not_executed"}
        )
    return handle


async def _collect_events(handle: CombatHandle, cid: str, state: BridgeState) -> None:
    """Background task: drain ``narration_events`` into ``events_log`` for good."""
    async for event in narration_events(handle):
        state.events_log.setdefault(cid, []).append(event)


async def _pump_until_stable(state: BridgeState, cid: str) -> None:
    """Yield to the loop until the collector task's appends settle.

    The engine's ``_emit`` pushes events onto the live queue synchronously
    during an awaited engine call, but the collector task only sees them
    once the loop schedules it — bounded ``asyncio.sleep(0)`` pump, per the
    module docstring's drain protocol.
    """
    prev_len = -1
    stable = 0
    for _ in range(_PUMP_MAX_ITERATIONS):
        await asyncio.sleep(0)
        cur_len = len(state.events_log.get(cid, []))
        handle = state.combats.get(cid)
        collector = state.collectors.get(cid)
        if handle is not None and collector is not None and collector.done():
            # A short-lived ASGI host can close its event loop between requests.
            # Only after that collector has stopped, use the Engine's public
            # synchronous drain under the same combat lock; never two consumers.
            state.events_log[cid].extend(drain_pending_events(handle))
            cur_len = len(state.events_log[cid])
        if handle is not None and cur_len == get_live(handle).event_count:
            return
        if cur_len == prev_len:
            stable += 1
            if stable >= _PUMP_STABLE_CHECKS and handle is None:
                return
        else:
            stable = 0
        prev_len = cur_len
    raise RuntimeError("combat event collector did not reach the authoritative event count")


def _envelope(
    cid: str, events: list[CombatEvent], names: dict[str, str], over: bool
) -> dict[str, Any]:
    return {
        "combat_id": cid,
        "events": [e.model_dump() for e in events],
        "narration": narrate(events, names),
        "over": over,
    }


def _build_party_specs(
    state: BridgeState, party: list[PartyValidateRequest], rng: random.Random
) -> tuple[list[Any], dict[str, str]]:
    assert state.loader is not None
    loader = state.loader
    party_specs = []
    names: dict[str, str] = {}
    for i, member_req in enumerate(party):
        entity_id = member_req.entity_id or f"char:{slugify(member_req.name)}"
        try:
            build_spec = make_build_spec(
                species_slug=member_req.build.species_slug,
                size_choice=member_req.build.size_choice,
                class_slug=member_req.build.class_slug,
                level=member_req.build.level,
                subclass_slug=member_req.build.subclass_slug,
                ability_scores=member_req.build.ability_scores.model_dump(by_alias=True),
                equipment=member_req.build.equipment,
            )
            dex_mod = _ability_mod(member_req.build.ability_scores.dex)
            member = derive_sheet(
                build_spec,
                name=_sanitize_name(member_req.name),
                entity_id=entity_id,
                loader=loader,
                hp_current=member_req.hp_current,
                spells_known=member_req.spells_known,
                zone_id=cell_id(0, i),
                initiative=rng.randint(1, 20) + dex_mod,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        party_specs.append(member)
        names[member.entity_id] = member.name
    return party_specs, names


def _build_encounter_specs(
    state: BridgeState, monster_slugs: list[str], rng: random.Random
) -> tuple[list[EncounterMemberSpec], dict[str, str]]:
    assert state.loader is not None
    loader = state.loader
    encounter_specs = []
    names: dict[str, str] = {}
    for i, slug in enumerate(monster_slugs):
        monster = loader.get_monster(slug)
        if monster is None:
            raise HTTPException(status_code=404, detail=f"unknown monster: {slug!r}")
        n = i + 1
        entity_id = f"mon:{slug}-{n}"
        modifier = (
            monster.initiative_modifier
            if monster.initiative_modifier is not None
            else _ability_mod(monster.ability_scores.dex)
        )
        enc = EncounterMemberSpec(
            entity_id=entity_id,
            entity_type="Monster",
            name=_sanitize_name(f"{monster.name} {n}"),
            initiative=rng.randint(1, 20) + modifier,
            hp_current=monster.hp,
            hp_max=monster.hp,
            ac=monster.ac or 10,
            dexterity=monster.ability_scores.dex,
            zone_id=cell_id(1, i),
            monster_template_slug=slug,
            creature_type=str(monster.creature_type),
            damage_resistances=list(monster.damage_resistances),
            damage_immunities=list(monster.damage_immunities),
            damage_vulnerabilities=list(monster.damage_vulnerabilities),
            condition_immunities=list(monster.condition_immunities),
        )
        encounter_specs.append(enc)
        names[entity_id] = enc.name
    return encounter_specs, names


async def _start_route(state: BridgeState, req: _CombatStartRequest) -> dict[str, Any]:
    seed = resolve_seed(req.seed)
    rng = random.Random(seed)

    party_specs, party_names = _build_party_specs(state, req.party, rng)
    encounter_specs, monster_names = _build_encounter_specs(state, req.monsters, rng)
    names = {**party_names, **monster_names}

    # Monotonic counter, not `len(state.combats) + 1` — the latter
    # collides once any combat has ended and been popped from `combats`
    # (see BridgeState.next_combat_id's docstring).
    cid = f"c{state.next_combat_id}"
    state.next_combat_id += 1
    grid_scene = GridScene(width=12, height=12)
    result = await start_combat(
        session_id=f"bridge:{state.instance_id}:{cid}",
        party=party_specs,
        encounter=encounter_specs,
        grid_scene=grid_scene,
        rng_seed=seed,
    )

    state.grids[cid] = grid_scene
    state.combats[cid] = result.handle
    state.events_log[cid] = []
    state.names[cid] = names
    state.seeds[cid] = seed
    state.collectors[cid] = asyncio.create_task(_collect_events(result.handle, cid, state))

    await _pump_until_stable(state, cid)
    events = state.events_log[cid]
    return _envelope(cid, events, names, over=False)


def _player_intent_from_request(req: _IntentRequest) -> PlayerIntent:
    return req.intent()


async def _intent_route(state: BridgeState, cid: str, req: _IntentRequest) -> dict[str, Any]:
    handle = _get_handle(state, cid)
    names = state.names.get(cid, {})
    start_idx = get_live(handle).event_count
    player_intent = _player_intent_from_request(req)

    await submit_player_intent(handle, req.actor_id, player_intent)

    await _pump_until_stable(state, cid)
    events = state.events_log.get(cid, [])[start_idx:]
    over = get_live(handle).ended
    return _envelope(cid, events, names, over=over)


async def _advance_monster_route(state: BridgeState, cid: str) -> dict[str, Any]:
    handle = _get_handle(state, cid)
    names = state.names.get(cid, {})
    start_idx = get_live(handle).event_count

    await advance_monster_turn(handle)

    await _pump_until_stable(state, cid)
    events = state.events_log.get(cid, [])[start_idx:]
    over = get_live(handle).ended
    return _envelope(cid, events, names, over=over)


def _order_row(combatant: Any, names: dict[str, str], live_view: Any) -> dict[str, Any]:
    eid = combatant.entity_id
    return {
        "entity_id": eid,
        "name": names.get(eid, combatant.name),
        "hp": live_view.tracked_hp.get(eid, combatant.hp_current),
        "max_hp": combatant.hp_max,
        "dead": eid in live_view.dead_ids,
        "conditions": sorted(live_view.active_conditions.get(eid, set())),
        "zone": live_view.actor_zone.get(eid),
    }


async def _view_route(state: BridgeState, cid: str) -> dict[str, Any]:
    handle = _get_handle(state, cid)
    names = state.names.get(cid, {})
    try:
        live_view = get_live(handle)
    except UnknownHandleError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    order = [_order_row(combatant, names, live_view) for combatant in live_view.initiative]

    current_actor = ""
    if 0 <= live_view.current_turn_index < len(live_view.initiative):
        current = live_view.initiative[live_view.current_turn_index]
        current_actor = f"{current.entity_id} ({names.get(current.entity_id, current.name)})"

    grid = state.grids.get(cid)
    return {
        "round_number": live_view.round_number,
        "current_actor": current_actor,
        "order": order,
        "ended": live_view.ended,
        "grid": grid.model_dump() if grid is not None else None,
        "state": jsonable_encoder(live_view, custom_encoder={set: sorted, frozenset: sorted}),
    }


async def _stop_collector(state: BridgeState, cid: str) -> None:
    task = state.collectors.pop(cid, None)
    if task is None:
        return
    try:
        await asyncio.wait_for(task, timeout=1)
    except (TimeoutError, asyncio.CancelledError):
        task.cancel()


async def _end_route(state: BridgeState, cid: str) -> dict[str, Any]:
    handle = _get_handle(state, cid)
    names = state.names.get(cid, {})
    try:
        result = await end_combat(handle)
    finally:
        if get_live(handle).final_outcome is not None:
            try:
                await _pump_until_stable(state, cid)
            finally:
                await _stop_collector(state, cid)
                state.combats.pop(cid, None)
                state.grids.pop(cid, None)

    return {
        "outcome": result.outcome.model_dump(),
        "narration": narrate(result.events, names),
    }


def _host_owner(state: BridgeState, authorization: str | None) -> str:
    supplied = (authorization or "").removeprefix("Bearer ")
    for token, owner in state.host_tokens.items():
        if (
            token
            and owner
            and authorization
            and authorization.startswith("Bearer ")
            and secrets.compare_digest(token.encode(), supplied.encode())
        ):
            return owner
    raise HTTPException(
        status_code=403, detail={"reason": "host_forbidden", "status": "not_executed"}
    )


def build_combat_router(state: BridgeState) -> APIRouter:
    router = APIRouter()

    @router.post("/v1/combat")
    async def start(req: _CombatStartRequest) -> JSONResponse:
        async def opening() -> dict[str, Any]:
            async with state.ruleset_lock:
                assert state.loader is not None
                with scoped_lib_loader(state.loader):
                    return await _start_route(state, req)

        return await execute(
            state,
            "__start__",
            "start",
            req.model_dump(mode="json"),
            req.request_id,
            opening,
            lambda: asyncio.sleep(0),
        )

    @router.post("/v1/combat/{cid}/intent")
    async def intent(cid: str, req: _IntentRequest) -> JSONResponse:
        return await execute(
            state,
            cid,
            "intent",
            req.model_dump(mode="json"),
            req.request_id,
            lambda: _intent_route(state, cid, req),
            lambda: _pump_until_stable(state, cid),
        )

    @router.post("/v1/combat/{cid}/advance-monster")
    async def advance_monster(cid: str, req: MutationRequest | None = None) -> JSONResponse:
        req = req or MutationRequest()
        return await execute(
            state,
            cid,
            "advance",
            req.model_dump(mode="json"),
            req.request_id,
            lambda: _advance_monster_route(state, cid),
            lambda: _pump_until_stable(state, cid),
        )

    @router.get("/v1/combat/{cid}")
    async def view(cid: str) -> dict[str, Any]:
        async with state.combat_locks.setdefault(cid, asyncio.Lock()):
            return await _view_route(state, cid)

    @router.post("/v1/combat/{cid}/end")
    async def end(cid: str, req: MutationRequest | None = None) -> JSONResponse:
        req = req or MutationRequest()
        return await execute(
            state,
            cid,
            "end",
            req.model_dump(mode="json"),
            req.request_id,
            lambda: _end_route(state, cid),
            lambda: _pump_until_stable(state, cid),
        )

    _add_host_routes(state, router)
    return router


def _add_host_routes(state: BridgeState, router: APIRouter) -> None:
    @router.post("/v1/combat/{cid}/host/objects")
    async def objects(
        cid: str, req: RegisterObjectsRequest, authorization: str | None = Header(default=None)
    ) -> JSONResponse:
        owner = _host_owner(state, authorization)

        async def register() -> dict[str, Any]:
            await register_combat_objects(
                _get_handle(state, cid), owner_id=owner, objects=tuple(req.objects)
            )
            return {}

        return await execute(
            state,
            cid,
            "register_objects",
            req.model_dump(mode="json") | {"owner": owner},
            req.request_id,
            register,
            lambda: _pump_until_stable(state, cid),
        )

    @router.post("/v1/combat/{cid}/host/object")
    async def object_mutation(
        cid: str, req: MutateObjectRequest, authorization: str | None = Header(default=None)
    ) -> JSONResponse:
        owner = _host_owner(state, authorization)

        async def mutate() -> dict[str, Any]:
            await mutate_combat_object(
                _get_handle(state, cid), owner_id=owner, mutation=req.mutation
            )
            return {}

        return await execute(
            state,
            cid,
            "mutate_object",
            req.model_dump(mode="json") | {"owner": owner},
            req.request_id,
            mutate,
            lambda: _pump_until_stable(state, cid),
        )

    @router.post("/v1/combat/{cid}/host/strong-wind")
    async def wind(
        cid: str, req: WindRequest, authorization: str | None = Header(default=None)
    ) -> JSONResponse:
        owner = _host_owner(state, authorization)

        async def apply() -> dict[str, Any]:
            await apply_strong_wind(_get_handle(state, cid), req.wind)
            return {}

        return await execute(
            state,
            cid,
            "strong_wind",
            req.model_dump(mode="json") | {"owner": owner},
            req.request_id,
            apply,
            lambda: _pump_until_stable(state, cid),
        )


__all__ = ["build_combat_router"]
