import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from nat20_bridge import routes_combat
from nat20_bridge.app import create_app
from nat20_bridge.state import BridgeState

PARTY = [
    {
        "name": "Brom",
        "build": {
            "species_slug": "human",
            "size_choice": "medium",
            "class_slug": "fighter",
            "level": 3,
        },
    }
]


def _combat_request() -> dict:
    return {"party": PARTY, "monsters": ["goblin-warrior"], "seed": 42}


@pytest.mark.parametrize("mutation", ["import", "overwrite", "delete", "forge"])
def test_homebrew_changes_conflict_before_persistence_during_combat(
    tmp_path: Path, mutation: str
) -> None:
    path = tmp_path / "homebrew.json"
    state = BridgeState(homebrew_path=path)
    with TestClient(create_app(state)) as client:
        raw = client.get("/v1/srd/items/longsword").json()
        raw.update(slug="hb-sword", name="Homebrew Sword")
        assert client.post("/v1/homebrew/items", json=raw).status_code == 200
        original_loader = state.loader
        original_store = path.read_bytes()
        started = client.post("/v1/combat", json=_combat_request())
        assert started.status_code == 200, started.text
        cid = started.json()["combat_id"]
        before = client.get(f"/v1/combat/{cid}").json()
        if mutation == "delete":
            response = client.delete("/v1/homebrew/hb-sword")
        elif mutation == "forge":
            response = client.post(
                "/v1/forge/item", json={"name": "Fresh Sword", "base": "longsword", "bonus": 1}
            )
        else:
            raw.update(slug="hb-new-sword" if mutation == "import" else "hb-sword", name="Changed")
            response = client.post("/v1/homebrew/items", json=raw)
        assert response.status_code == 409, response.text
        assert "combat" in response.json()["detail"].lower()
        assert path.read_bytes() == original_store
        assert state.loader is original_loader
        assert client.get("/v1/srd/items/hb-sword").json()["name"] == "Homebrew Sword"
        assert client.get(f"/v1/combat/{cid}").json() == before
        assert client.post(f"/v1/combat/{cid}/end").status_code == 200
        # Once the last combat has explicitly ended, the same host can update its ruleset.
        assert client.post("/v1/homebrew/items", json=raw).status_code == 200


@pytest.mark.asyncio
async def test_ruleset_update_cannot_race_combat_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = BridgeState(homebrew_path=tmp_path / "homebrew.json")
    app = create_app(state)
    opening = asyncio.Event()
    proceed = asyncio.Event()
    original = routes_combat._start_route

    async def delayed_start(*args, **kwargs):
        opening.set()
        await proceed.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(routes_combat, "_start_route", delayed_start)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bridge"
    ) as client:
        start_task = asyncio.create_task(client.post("/v1/combat", json=_combat_request()))
        await opening.wait()
        update_task = asyncio.create_task(
            client.post("/v1/forge/item", json={"name": "Racing Sword", "base": "longsword"})
        )
        await asyncio.sleep(0)
        assert not update_task.done()
        proceed.set()
        started = await start_task
        assert started.status_code == 200, started.text
        rejected = await update_task
        assert rejected.status_code == 409, rejected.text
        assert not state.homebrew_path.exists()
        cid = started.json()["combat_id"]
        assert (await client.post(f"/v1/combat/{cid}/end")).status_code == 200


def test_another_bridge_default_does_not_replace_combat_opening_ruleset(tmp_path: Path) -> None:
    first = BridgeState(homebrew_path=tmp_path / "first.json")
    with TestClient(create_app(first)) as client:
        raw = client.get("/v1/srd/items/longsword").json()
        raw.update(slug="hb-first-sword", name="First Sword")
        assert client.post("/v1/homebrew/items", json=raw).status_code == 200
        create_app(BridgeState(homebrew_path=tmp_path / "second.json"))
        started = client.post("/v1/combat", json=_combat_request())
        assert started.status_code == 200, started.text
        cid = started.json()["combat_id"]
        attacked = client.post(
            f"/v1/combat/{cid}/intent",
            json={
                "actor_id": "char:brom",
                "intent_type": "attack",
                "weapon_id": "hb-first-sword",
                "target_id": "mon:goblin-warrior-1",
            },
        )
        assert attacked.status_code == 200, attacked.text
        assert any(event["type"] == "attack_rolled" for event in attacked.json()["events"])
        assert client.post(f"/v1/combat/{cid}/end").status_code == 200
