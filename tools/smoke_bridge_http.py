"""Installed-wheel HTTP intent/retry/object execution; no repository imports."""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from nat20_bridge.app import create_app
from nat20_bridge.state import BridgeState


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="nat20-http-") as directory:
        state = BridgeState(
            homebrew_path=Path(directory) / "homebrew.json",
            host_tokens={"smoke-credential": "host:scene"},
        )
        with TestClient(create_app(state)) as client:
            for spell, level in (("darkness", 2), ("daylight", 3)):
                _object_flow(client, spell, level)
    print("BRIDGE HTTP PASSED: installed intent, retry, authority, objects and cover", flush=True)


def _object_flow(client: TestClient, spell: str, level: int) -> None:
    start = client.post(
        "/v1/combat",
        json={
            "request_id": f"opening-{spell}",
            "party": [
                {
                    "name": "Mage",
                    "build": {
                        "species_slug": "human",
                        "size_choice": "medium",
                        "class_slug": "sorcerer",
                        "level": 13,
                        "ability_scores": {"dex": 20, "con": 20, "cha": 20},
                    },
                    "spells_known": [spell],
                }
            ],
            "monsters": ["clay-golem"],
            "seed": 19,
        },
    )
    assert start.status_code == 200, start.text
    cid = start.json()["combat_id"]
    base = f"/v1/combat/{cid}"
    for _ in range(3):
        live = client.get(base).json()["state"]
        if live["initiative"][live["current_turn_index"]]["entity_id"] == "char:mage":
            break
        assert client.post(base + "/advance-monster", json={}).status_code == 200
    headers = {"Authorization": "Bearer smoke-credential"}
    registered = client.post(
        base + "/host/objects",
        headers=headers,
        json={
            "request_id": "register",
            "objects": [
                {
                    "id": "object:stone",
                    "owner_id": "host:scene",
                    "source_id": "host:initial",
                    "position": "0,0",
                }
            ],
        },
    )
    assert registered.status_code == 200, registered.text
    payload = {
        "actor_id": "char:mage",
        "intent_type": "cast_spell",
        "spell_id": spell,
        "slot_level": level,
        "target_object_id": "object:stone",
        "request_id": "cast",
    }
    cast = client.post(base + "/intent", json=payload)
    assert cast.status_code == 200, cast.text
    assert any(event["type"] == "spell_cast" for event in cast.json()["events"])
    assert client.post(base + "/intent", json=payload).json() == cast.json()
    after = client.get(base).json()["state"]
    assert len(after["environment_sources"]) == 1
    for operation, extra in (("cover", {}), ("uncover", {}), ("move", {"position": "4,4"})):
        request = {
            "request_id": operation,
            "mutation": {
                "object_id": "object:stone",
                "source_id": "host:operation",
                "operation": operation,
                **extra,
            },
        }
        changed = client.post(base + "/host/object", headers=headers, json=request)
        assert changed.status_code == 200, changed.text
        assert (
            client.post(base + "/host/object", headers=headers, json=request).json()
            == changed.json()
        )
        current = client.get(base).json()["state"]
        assert current["spell_slots_by_entity"] == after["spell_slots_by_entity"]
        assert current["environment_sources"][0]["suppressed"] == (operation == "cover")
    assert client.get(base).json()["state"]["environment_sources"][0]["origin"] == "4,4"
    close = client.post(base + "/end", json={"request_id": "close"})
    assert close.status_code == 200, close.text
    assert client.post(base + "/end", json={"request_id": "close"}).json() == close.json()


if __name__ == "__main__":
    main()
