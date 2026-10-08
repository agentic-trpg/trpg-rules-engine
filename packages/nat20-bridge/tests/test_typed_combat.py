"""Actual HTTP execution, authoritative discovery and retry acceptance."""

import asyncio
import random
from copy import deepcopy

import httpx
import pytest
from dnd5e_engine import PlayerIntent, orchestrator
from fastapi.testclient import TestClient

from nat20_bridge import routes_combat
from nat20_bridge.app import create_app
from nat20_bridge.combat_requests import IntentRequest
from nat20_bridge.state import BridgeState

SPELLS = [
    "guidance",
    "fog-cloud",
    "daylight",
    "sunbeam",
    "slow",
    "haste",
    "moonbeam",
    "darkness",
    "wish",
]
ACTOR = "char:mage"


def opening(seed=19):
    return {
        "party": [
            {
                "name": "Mage",
                "entity_id": ACTOR,
                "build": {
                    "species_slug": "human",
                    "size_choice": "medium",
                    "class_slug": "sorcerer",
                    "level": 20,
                    "ability_scores": {
                        "str": 10,
                        "dex": 20,
                        "con": 20,
                        "int": 16,
                        "wis": 10,
                        "cha": 20,
                    },
                    "equipment": ["dagger", "ball-bearings"],
                },
                "spells_known": SPELLS,
            }
        ],
        "monsters": ["clay-golem"],
        "seed": seed,
    }


def table(tmp_path):
    state = BridgeState(
        homebrew_path=tmp_path / "homebrew.json",
        host_tokens={"scene-token": "host:scene", "foreign-token": "host:foreign"},
    )
    return state, create_app(state)


def start(client):
    result = client.post("/v1/combat", json=opening())
    assert result.status_code == 200, result.text
    cid = result.json()["combat_id"]
    turn(client, cid)
    return cid


def view(client, cid):
    response = client.get(f"/v1/combat/{cid}")
    assert response.status_code == 200, response.text
    return response.json()["state"]


def turn(client, cid, *, next_turn=False):
    if next_turn:
        post(client, cid, {"intent_type": "pass"})
    for _ in range(20):
        state = view(client, cid)
        current = state["initiative"][state["current_turn_index"]]["entity_id"]
        if current == ACTOR:
            return
        response = client.post(f"/v1/combat/{cid}/advance-monster", json={})
        assert response.status_code == 200, response.text
    pytest.fail("caster turn did not arrive")


def post(client, cid, payload, *, status=200):
    response = client.post(f"/v1/combat/{cid}/intent", json={"actor_id": ACTOR, **payload})
    assert response.status_code == status, response.text
    return response.json()


def host(client, cid, suffix, payload, *, token="scene-token", status=200):
    response = client.post(
        f"/v1/combat/{cid}/host/{suffix}",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == status, response.text
    return response.json()


def test_wire_has_exact_engine_fields_and_strict_json_arrays():
    assert set(IntentRequest.model_fields) == set(PlayerIntent.model_fields) | {
        "actor_id",
        "request_id",
    }
    intent = IntentRequest.model_validate(
        {
            "actor_id": ACTOR,
            "intent_type": "attack",
            "direction": [1, 0],
            "action_grant": ["effect", "origin"],
        }
    ).intent()
    assert intent.direction == (1, 0) and intent.action_grant == ("effect", "origin")


@pytest.mark.parametrize(
    "extra",
    [
        {"unknown": 1},
        {"slot_level": "3"},
        {"slot_level": True},
        {"use_bonus_action": "false"},
        {"direction": [True, 0]},
        {"direction": [0, 0]},
        {"target_ids": "mage"},
        {"effect_selections": [{"spell_id": "guidance", "extra": 1}]},
    ],
)
def test_strict_http_rejections_preserve_view_events_rng(tmp_path, extra):
    state, app = table(tmp_path)
    with TestClient(app) as client:
        cid = start(client)
        before = view(client, cid)
        live = orchestrator._get_live(state.combats[cid])
        rng, events = live.rng.getstate(), list(live.event_log)
        post(client, cid, {"intent_type": "cast_spell", "spell_id": "haste", **extra}, status=422)
        assert view(client, cid) == before
        assert live.rng.getstate() == rng and live.event_log == events


@pytest.mark.parametrize(
    "spell,level,choices",
    [
        ("guidance", None, {"target_id": ACTOR, "willing_target_ids": [ACTOR]}),
        ("fog-cloud", 2, {"target_zone_id": "4,4"}),
        ("daylight", 3, {"target_zone_id": "4,4"}),
        ("sunbeam", 6, {"direction": [1, 0]}),
        ("slow", 3, {"target_zone_id": "1,0", "target_ids": ["mon:clay-golem-1"]}),
        ("haste", 3, {"target_id": ACTOR, "willing_target_ids": [ACTOR]}),
        ("moonbeam", 3, {"target_zone_id": "4,4"}),
    ],
)
def test_http_real_spell_execution_discovery_cost_and_retry(tmp_path, spell, level, choices):
    _state, app = table(tmp_path)
    with TestClient(app) as client:
        cid = start(client)
        if spell == "guidance":
            raw = client.get("/v1/srd/spells/guidance").json()
            activity = raw["activities"][0]
            choices = choices | {
                "effect_selections": [
                    {
                        "spell_id": spell,
                        "activity_id": activity["id"],
                        "target_id": ACTOR,
                        "effect_id": activity["effects"][0]["id"],
                    }
                ]
            }
        before = view(client, cid)
        payload = {"intent_type": "cast_spell", "spell_id": spell, "request_id": "paid", **choices}
        if level:
            payload["slot_level"] = level
        result = post(client, cid, payload)
        after = view(client, cid)
        assert any(e["type"] == "spell_cast" for e in result["events"])
        assert result["receipt"]["status"] == "committed"
        assert post(client, cid, payload) == result
        assert view(client, cid) == after
        if level:
            assert (
                after["spell_slots_by_entity"][ACTOR][str(level)]
                == before["spell_slots_by_entity"][ACTOR][str(level)] - 1
            )
        if spell in ("sunbeam", "moonbeam"):
            [source] = after["ongoing_spells"]
            assert source["owner_id"] == ACTOR
            turn(client, cid, next_turn=True)
            slots = view(client, cid)["spell_slots_by_entity"]
            activation = {
                "intent_type": "activate_spell",
                "source_id": source["source_id"],
                "activity_id": source["activation"]["activity_ids"][0],
                "request_id": "activation",
            }
            activation |= {"direction": [1, 0]} if spell == "sunbeam" else {"target_zone_id": "5,4"}
            repeat = post(client, cid, activation)
            assert not any(e["type"] == "spell_cast" for e in repeat["events"])
            assert view(client, cid)["spell_slots_by_entity"] == slots
            assert post(client, cid, activation) == repeat
            if spell == "moonbeam":
                assert view(client, cid)["ongoing_spells"][0]["origin"] == "5,4"
        if spell == "haste":
            [grant] = after["restricted_action_grants"]
            assert grant["available"] and grant["owner_id"] == ACTOR
            post(
                client,
                cid,
                {
                    "intent_type": "dash",
                    "action_grant": grant["action_grant"],
                    "request_id": "extra",
                },
            )
            assert not view(client, cid)["restricted_action_grants"][0]["available"]
        if spell == "guidance":
            assert len([e for e in result["events"] if e["type"] == "effect_applied"]) == 1
        if spell == "slow":
            [save] = [e for e in result["events"] if e["type"] == "save_rolled"]
            assert not save["succeeded"]
            assert any(e["type"] == "effect_applied" for e in result["events"])
        if spell == "fog-cloud":
            source = after["environment_sources"][0]
            assert source["cells"] == sorted(source["cells"])
            host(
                client,
                cid,
                "strong-wind",
                {
                    "request_id": "wind",
                    "wind": {"source_id": "host:weather", "cells": [source["cells"][0]]},
                },
            )
            assert view(client, cid)["environment_sources"] == []


@pytest.mark.parametrize("spell,level", [("darkness", 2), ("daylight", 3)])
def test_http_object_mode_authority_cover_movement_retry(tmp_path, spell, level):
    _state, app = table(tmp_path)
    with TestClient(app) as client:
        cid = start(client)
        registration = {
            "request_id": "register",
            "objects": [
                {
                    "id": "object:stone",
                    "source_id": "host:scene",
                    "owner_id": "host:scene",
                    "position": "0,0",
                }
            ],
        }
        host(client, cid, "objects", registration, token="invalid", status=403)
        registered = host(client, cid, "objects", registration)
        assert host(client, cid, "objects", registration) == registered
        payload = {
            "intent_type": "cast_spell",
            "spell_id": spell,
            "slot_level": level,
            "target_object_id": "object:stone",
            "request_id": "cast",
        }
        post(client, cid, payload)
        for i, (operation, extra) in enumerate(
            [("cover", {}), ("uncover", {}), ("move", {"position": "4,4"})]
        ):
            mutation = {
                "request_id": f"object-{i}",
                "mutation": {
                    "object_id": "object:stone",
                    "source_id": "host:operation",
                    "operation": operation,
                    **extra,
                },
            }
            host(client, cid, "object", mutation, token="foreign-token", status=409)
            mutation["request_id"] += "-correct-owner"
            host(client, cid, "object", mutation)
            [source] = view(client, cid)["environment_sources"]
            assert source["suppressed"] == (operation == "cover")
            if operation == "move":
                assert source["origin"] == "4,4"
        assert client.post(f"/v1/combat/{cid}/end", json={"request_id": "close"}).status_code == 200
        assert client.post(f"/v1/combat/{cid}/end", json={"request_id": "close"}).status_code == 200


def test_http_faults_roll_back_or_cache_committed_listener_failure(tmp_path, monkeypatch):
    state, app = table(tmp_path)
    with TestClient(app) as client:
        cid = start(client)
        payload = {
            "intent_type": "cast_spell",
            "spell_id": "sunbeam",
            "slot_level": 6,
            "direction": [1, 0],
            "request_id": "broken",
        }
        before = view(client, cid)
        live = orchestrator._get_live(state.combats[cid])
        rng = live.rng.getstate()
        original = orchestrator._consume_spell_slot

        def broken(*args, **kwargs):
            original(*args, **kwargs)
            args[0].rng.randint(1, 20)
            raise RuntimeError("injected after execution")

        monkeypatch.setattr(orchestrator, "_consume_spell_slot", broken)
        failed = post(client, cid, payload, status=500)
        assert failed["receipt"]["status"] == "rolled_back"
        assert view(client, cid) == before and live.rng.getstate() == rng
        monkeypatch.setattr(orchestrator, "_consume_spell_slot", original)
        assert post(client, cid, payload, status=500) == failed

        def observer(event):
            raise RuntimeError("Host observer")

        live.event_listeners.append(observer)
        payload["request_id"] = "observer"
        committed = post(client, cid, payload, status=503)
        assert committed["receipt"]["status"] == "committed"
        after = view(client, cid)
        assert after["execution_serial"] == before["execution_serial"] + 1
        assert after["environment_sources"]
        assert post(client, cid, payload, status=503) == committed
        assert view(client, cid) == after
        close = client.post(f"/v1/combat/{cid}/end", json={"request_id": "close"})
        assert close.status_code == 503
        assert live.ended and live.final_outcome is not None
        assert close.json()["outcome"] == live.final_outcome.model_dump(mode="json")
        assert cid not in state.combats
        assert (
            client.post(f"/v1/combat/{cid}/end", json={"request_id": "close"}).json()
            == close.json()
        )


def test_failure_mapping_missing_invalid_no_action_unreviewed(tmp_path):
    _state, app = table(tmp_path)
    with TestClient(app) as client:
        cid = start(client)
        for payload in [
            {"intent_type": "cast_spell", "spell_id": "wish", "slot_level": 9},
            {"intent_type": "activate_spell", "source_id": "missing", "activity_id": "unsupported"},
            {
                "intent_type": "cast_spell",
                "spell_id": "haste",
                "target_id": "missing",
                "slot_level": 3,
            },
        ]:
            result = post(client, cid, payload, status=409)
            assert result["receipt"]["status"] == "rule_refused"
            assert result["detail"]["reason"]
        post(client, cid, {"intent_type": "dash"})
        failed = post(client, cid, {"intent_type": "dash"}, status=409)
        assert failed["detail"]["reason"] == "no_action_economy"


@pytest.mark.asyncio
async def test_concurrent_retry_cancellation_and_different_combat_isolation(tmp_path, monkeypatch):
    _state, app = table(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bridge"
    ) as client:
        a, b = await asyncio.gather(
            client.post("/v1/combat", json=opening()), client.post("/v1/combat", json=opening())
        )
        ca, cb = a.json()["combat_id"], b.json()["combat_id"]
        for cid in (ca, cb):
            current = (await client.get(f"/v1/combat/{cid}")).json()["state"]
            if current["initiative"][current["current_turn_index"]]["entity_id"] != ACTOR:
                assert (
                    await client.post(f"/v1/combat/{cid}/advance-monster", json={})
                ).status_code == 200
        entered, proceed = asyncio.Event(), asyncio.Event()
        original = routes_combat._pump_until_stable

        async def delayed(state_arg, cid):
            if cid == ca:
                entered.set()
                await proceed.wait()
            await original(state_arg, cid)

        monkeypatch.setattr(routes_combat, "_pump_until_stable", delayed)
        payload = {
            "actor_id": ACTOR,
            "intent_type": "cast_spell",
            "spell_id": "daylight",
            "slot_level": 3,
            "target_zone_id": "4,4",
            "request_id": "paid",
        }
        first = asyncio.create_task(client.post(f"/v1/combat/{ca}/intent", json=payload))
        await asyncio.wait_for(entered.wait(), 5)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        retries = [
            asyncio.create_task(client.post(f"/v1/combat/{ca}/intent", json=payload))
            for _ in range(4)
        ]
        other = await client.post(f"/v1/combat/{cb}/intent", json=payload)
        assert other.status_code == 200, other.text
        assert all(not task.done() for task in retries)
        proceed.set()
        responses = await asyncio.gather(*retries)
        assert all(
            response.json() == responses[0].json() and response.status_code == 200
            for response in responses
        )
        assert responses[0].json()["events"] == other.json()["events"]
        assert (await client.get(f"/v1/combat/{ca}")).json()["state"] == (
            await client.get(f"/v1/combat/{cb}")
        ).json()["state"]
        conflict = await client.post(
            f"/v1/combat/{ca}/intent", json=payload | {"target_zone_id": "3,3"}
        )
        assert (
            conflict.status_code == 409
            and conflict.json()["detail"]["reason"] == "request_id_conflict"
        )


def test_standalone_rng_and_apps_do_not_share_combat_handles(tmp_path):
    global_state = random.getstate()
    a, app_a = table(tmp_path / "a")
    b, app_b = table(tmp_path / "b")
    with TestClient(app_a) as ca, TestClient(app_b) as cb:
        ida, idb = start(ca), start(cb)
        assert a.combats[ida] != b.combats[idb]
        before = deepcopy(view(cb, idb))
        ca.post("/v1/roll", json={"dice": "4d20", "seed": 7})
        ca.post("/v1/check", json={"kind": "ability", "ability": "str", "seed": 7})
        post(ca, ida, {"intent_type": "dodge"})
        assert view(cb, idb) == before
        assert random.getstate() == global_state


@pytest.mark.parametrize("fault", ["drain", "narration", "serialization"])
def test_post_commit_response_fault_receipt_prevents_reexecution(tmp_path, monkeypatch, fault):
    from nat20_bridge import combat_execution

    _state, app = table(tmp_path)
    with TestClient(app) as client:
        cid = start(client)
        before = view(client, cid)
        target = routes_combat if fault != "serialization" else combat_execution
        name = {
            "drain": "_pump_until_stable",
            "narration": "narrate",
            "serialization": "jsonable_encoder",
        }[fault]
        original = getattr(target, name)

        def broken(*args, **kwargs):
            raise RuntimeError("response publication")

        async def broken_drain(*args, **kwargs):
            raise RuntimeError("response publication")

        monkeypatch.setattr(target, name, broken_drain if fault == "drain" else broken)
        payload = {
            "intent_type": "cast_spell",
            "spell_id": "daylight",
            "slot_level": 3,
            "target_zone_id": "4,4",
            "request_id": "publication",
        }
        failed = post(client, cid, payload, status=503)
        assert failed["receipt"]["status"] == "committed"
        monkeypatch.setattr(target, name, original)
        after = view(client, cid)
        assert after["execution_serial"] == before["execution_serial"] + 1
        assert (
            after["spell_slots_by_entity"][ACTOR]["3"]
            == before["spell_slots_by_entity"][ACTOR]["3"] - 1
        )
        assert post(client, cid, payload, status=503) == failed
        assert view(client, cid) == after


def test_start_narration_failure_recovers_committed_combat_and_id(tmp_path, monkeypatch):
    state, app = table(tmp_path)
    with TestClient(app) as client:
        original = routes_combat.narrate

        def broken(*args, **kwargs):
            raise RuntimeError("opening response")

        monkeypatch.setattr(routes_combat, "narrate", broken)
        payload = opening() | {"request_id": "opening"}
        failed = client.post("/v1/combat", json=payload)
        assert failed.status_code == 503
        cid = failed.json()["combat_id"]
        assert cid in state.combats and failed.json()["receipt"]["status"] == "committed"
        monkeypatch.setattr(routes_combat, "narrate", original)
        assert client.post("/v1/combat", json=payload).json() == failed.json()
        assert len(state.combats) == 1


def test_host_default_denied_and_illegal_combinations_are_structured(tmp_path):
    state = BridgeState(homebrew_path=tmp_path / "no-policy.json")
    with TestClient(create_app(state)) as client:
        cid = start(client)
        host(
            client,
            cid,
            "strong-wind",
            {"request_id": "wind", "wind": {"source_id": "host:wind", "cells": ["0,0"]}},
            status=403,
        )
        before = view(client, cid)
        for payload in [
            {"intent_type": "activate_spell"},
            {"intent_type": "cast_spell"},
            {
                "intent_type": "cast_spell",
                "spell_id": "haste",
                "target_id": ACTOR,
                "target_object_id": "object:absent",
                "slot_level": 3,
            },
        ]:
            result = post(client, cid, payload, status=409)
            assert result["detail"]["reason"]
        after = view(client, cid)
        assert after["spell_slots_by_entity"] == before["spell_slots_by_entity"]


def test_same_seed_two_apps_with_different_rulesets_remain_pinned(tmp_path):
    a, app_a = table(tmp_path / "a")
    b, app_b = table(tmp_path / "b")
    with TestClient(app_a) as ca, TestClient(app_b) as cb:
        raw = ca.get("/v1/srd/items/dagger").json()
        raw.update(slug="hb-exclusive", name="Exclusive Weapon")
        assert ca.post("/v1/homebrew/items", json=raw).status_code == 200
        first, second = start(ca), start(cb)
        before = view(cb, second)
        post(
            ca,
            first,
            {"intent_type": "attack", "weapon_id": "hb-exclusive", "target_id": "mon:clay-golem-1"},
        )
        assert view(cb, second) == before
        assert b.loader.get_weapon("hb-exclusive") is None
        assert a.loader.get_weapon("hb-exclusive") is not None


@pytest.mark.asyncio
async def test_distinct_request_after_drain_failure_uses_authoritative_event_boundary(
    tmp_path, monkeypatch
):
    _state, app = table(tmp_path)
    release = asyncio.Event()
    delayed = False
    original_events = routes_combat.narration_events

    async def delayed_events(handle):
        async for event in original_events(handle):
            if delayed:
                await release.wait()
            yield event

    monkeypatch.setattr(routes_combat, "narration_events", delayed_events)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bridge"
    ) as client:
        started = await client.post("/v1/combat", json=opening())
        cid = started.json()["combat_id"]
        current = (await client.get(f"/v1/combat/{cid}")).json()["state"]
        if current["initiative"][current["current_turn_index"]]["entity_id"] != ACTOR:
            assert (
                await client.post(f"/v1/combat/{cid}/advance-monster", json={})
            ).status_code == 200
        original_pump = routes_combat._pump_until_stable

        async def failed_drain(*args):
            raise RuntimeError("delayed collector")

        delayed = True
        monkeypatch.setattr(routes_combat, "_pump_until_stable", failed_drain)
        first = await client.post(
            f"/v1/combat/{cid}/intent",
            json={"actor_id": ACTOR, "intent_type": "dash", "request_id": "dash"},
        )
        assert first.status_code == 503
        monkeypatch.setattr(routes_combat, "_pump_until_stable", original_pump)
        second = asyncio.create_task(
            client.post(
                f"/v1/combat/{cid}/intent",
                json={
                    "actor_id": ACTOR,
                    "intent_type": "move",
                    "target_zone_id": "0,1",
                    "request_id": "move",
                },
            )
        )
        release.set()
        result = await second
        assert result.status_code == 200, result.text
        assert any(event["type"] == "actor_moved" for event in result.json()["events"])
        assert all(event["type"] != "dash_taken" for event in result.json()["events"])


@pytest.mark.asyncio
async def test_advance_retry_and_distinct_concurrent_paid_commands(tmp_path):
    _state, app = table(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bridge"
    ) as client:
        request = opening() | {"request_id": "opening"}
        started = await client.post("/v1/combat", json=request)
        assert (await client.post("/v1/combat", json=request)).json() == started.json()
        cid = started.json()["combat_id"]
        base = f"/v1/combat/{cid}"
        advance = await client.post(base + "/advance-monster", json={"request_id": "monster"})
        assert advance.status_code == 200, advance.text
        before = (await client.get(base)).json()
        assert (
            await client.post(base + "/advance-monster", json={"request_id": "monster"})
        ).json() == advance.json()
        assert (await client.get(base)).json() == before
        commands = await asyncio.gather(
            *[
                client.post(
                    base + "/intent",
                    json={"actor_id": ACTOR, "intent_type": "dash", "request_id": str(i)},
                )
                for i in range(2)
            ]
        )
        assert sorted(response.status_code for response in commands) == [200, 409]
        [paid] = [response for response in commands if response.status_code == 200]
        [refused] = [response for response in commands if response.status_code == 409]
        assert sum(e["type"] == "dash_taken" for e in paid.json()["events"]) == 1
        assert refused.json()["events"] == []
        assert refused.json()["detail"]["reason"] == "no_action_economy"


def test_refusal_listener_failure_is_committed_event_and_cached_without_payment(tmp_path):
    state, app = table(tmp_path)
    with TestClient(app) as client:
        cid = start(client)
        live = orchestrator._get_live(state.combats[cid])
        before = view(client, cid)
        rng = live.rng.getstate()

        def broken(event):
            raise RuntimeError("refusal observer")

        live.event_listeners.append(broken)
        payload = {
            "intent_type": "cast_spell",
            "spell_id": "wish",
            "slot_level": 9,
            "request_id": "refused",
        }
        result = post(client, cid, payload, status=503)
        assert result["receipt"]["status"] == "committed"
        assert [e["type"] for e in result["events"]] == ["cast_failed"]
        after = view(client, cid)
        assert after["execution_serial"] == before["execution_serial"]
        assert after["event_count"] == before["event_count"] + 1
        assert after["spell_slots_by_entity"] == before["spell_slots_by_entity"]
        assert live.rng.getstate() == rng
        assert post(client, cid, payload, status=503) == result
        assert view(client, cid) == after


def test_refusal_serialization_failure_uses_committed_recovery_receipt(tmp_path, monkeypatch):
    from nat20_bridge import combat_execution

    state, app = table(tmp_path)
    with TestClient(app) as client:
        cid = start(client)
        before = view(client, cid)
        live = orchestrator._get_live(state.combats[cid])
        rng = live.rng.getstate()
        payload = {
            "intent_type": "cast_spell",
            "spell_id": "wish",
            "slot_level": 9,
            "request_id": "refusal-response",
        }

        def broken(*args, **kwargs):
            raise RuntimeError("refusal serialization")

        with monkeypatch.context() as patch:
            patch.setattr(combat_execution, "jsonable_encoder", broken)
            result = post(client, cid, payload, status=503)
        assert result["detail"] == {
            "reason": "committed_response_failed",
            "status": "committed",
        }
        assert result["receipt"]["status"] == "committed"
        after = view(client, cid)
        assert after["event_count"] == before["event_count"] + 1
        assert after["execution_serial"] == before["execution_serial"]
        assert after["spell_slots_by_entity"] == before["spell_slots_by_entity"]
        assert live.rng.getstate() == rng
        assert post(client, cid, payload, status=503) == result
        assert view(client, cid) == after
