"""SRD browse + homebrew import/delete + item-forge routes.

Reads (`GET /v1/srd/...`) always go through ``state.loader`` — the
``OverlayAssetLoader`` installed by ``create_app`` — so homebrew (``hb-``)
entries resolve alongside canonical SRD content. Every homebrew mutation
(import, delete, forge-then-store) calls ``state.refresh_loader()`` so the
overlay (and the engine's module-global lib loader singleton) picks up the
change without a process restart. Updates conflict while the bridge has an
open combat and are rejected before the homebrew store is changed.
"""

from __future__ import annotations

from typing import Any

from dnd5e_srd_data.loader import Category
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from nat20_bridge.forge import ForgeError, forge_item
from nat20_bridge.homebrew import HomebrewValidationError
from nat20_bridge.state import BridgeState

_CATEGORIES: tuple[Category, ...] = (
    "items",
    "monsters",
    "spells",
    "species",
    "classes",
    "subclasses",
    "backgrounds",
    "feats",
    "features",
)

# Category -> AssetLoader getter name, used to fetch a single entry for
# `GET /v1/srd/{category}/{slug}`.
_GETTERS: dict[str, str] = {
    "items": "get_item",
    "monsters": "get_monster",
    "spells": "get_spell",
    "species": "get_species",
    "classes": "get_class",
    "subclasses": "get_subclass",
    "backgrounds": "get_background",
    "feats": "get_feat",
    "features": "get_feature",
}


def _check_category(category: str) -> Category:
    """Require a supported content category or return HTTP 404."""
    if category not in _CATEGORIES:
        raise HTTPException(status_code=404, detail=f"unknown category: {category!r}")
    return category


class _ForgeItemRequest(BaseModel):
    """Validate the supported homebrew item forge request."""

    name: str
    base: str
    bonus: int = 0
    extra_damage: str | None = None


def _forge_summary(name: str, slug: str, base: str, bonus: int, extra_damage: str | None) -> str:
    """Format the forged item's base, bonus and optional damage contribution."""
    parts = [f"{base} base", f"+{bonus}"]
    if extra_damage is not None:
        dice, _, damage_type = extra_damage.partition(":")
        parts.append(f"+{dice} {damage_type}")
    return f"{name} ({slug}): {', '.join(parts)}"


def _browse(state: BridgeState, category: str, q: str | None) -> dict[str, Any]:
    """List asset slugs, optionally filtered by the supplied substring."""
    checked = _check_category(category)
    assert state.loader is not None
    slugs = state.loader.list_slugs(checked)
    if q:
        slugs = [s for s in slugs if q in s]
    return {"slugs": slugs}


def _get_entry(state: BridgeState, category: str, slug: str) -> dict[str, Any]:
    """Load and serialize one typed asset or return HTTP 404."""
    checked = _check_category(category)
    assert state.loader is not None
    getter = getattr(state.loader, _GETTERS[checked])
    entry = getter(slug)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"unknown {checked} slug: {slug!r}")
    dumped: dict[str, Any] = entry.model_dump(mode="json")
    return dumped


def _homebrew_add(state: BridgeState, category: str, raw: dict[str, Any]) -> dict[str, str]:
    """Validate/persist a homebrew entry and refresh the effective loader."""
    checked = _check_category(category)
    _require_ruleset_update_allowed(state)
    assert state.homebrew_store is not None
    assert state.refresh_loader is not None
    try:
        slug = state.homebrew_store.add(checked, raw)
    except HomebrewValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    state.refresh_loader()
    return {"slug": slug}


def _homebrew_list(state: BridgeState) -> dict[str, dict[str, str]]:
    """List persisted homebrew identities and their categories."""
    assert state.homebrew_store is not None
    entries = state.homebrew_store.entries()
    return {"entries": {slug: entry["category"] for slug, entry in entries.items()}}


def _homebrew_delete(state: BridgeState, slug: str) -> None:
    """Remove a homebrew entry and refresh the effective loader, returning 404 if absent."""
    _require_ruleset_update_allowed(state)
    assert state.homebrew_store is not None
    assert state.refresh_loader is not None
    removed = state.homebrew_store.remove(slug)
    if not removed:
        raise HTTPException(status_code=404, detail=f"unknown homebrew slug: {slug!r}")
    state.refresh_loader()


def _forge(state: BridgeState, req: _ForgeItemRequest) -> dict[str, str]:
    """Create/persist a supported forged item and refresh the effective loader."""
    _require_ruleset_update_allowed(state)
    assert state.loader is not None
    assert state.homebrew_store is not None
    assert state.refresh_loader is not None
    try:
        raw = forge_item(
            name=req.name,
            base=req.base,
            loader=state.loader,
            bonus=req.bonus,
            extra_damage=req.extra_damage,
        )
    except ForgeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    slug = state.homebrew_store.add("items", raw)
    state.refresh_loader()
    summary = _forge_summary(req.name, slug, req.base, req.bonus, req.extra_damage)
    return {"slug": slug, "summary": summary}


def _require_ruleset_update_allowed(state: BridgeState) -> None:
    """Reject content replacement while any Bridge combat remains open."""
    if state.combats:
        raise HTTPException(status_code=409, detail="end all open combats before updating homebrew")


def build_content_router(state: BridgeState) -> APIRouter:
    """Install content browse, homebrew and item-forge routes."""
    router = APIRouter()

    @router.get("/v1/srd/{category}")
    def browse(category: str, q: str | None = None) -> dict[str, Any]:
        """Delegate content listing with an optional slug filter."""
        return _browse(state, category, q)

    @router.get("/v1/srd/{category}/{slug}")
    def get_entry(category: str, slug: str) -> dict[str, Any]:
        """Delegate typed content lookup and HTTP error handling."""
        return _get_entry(state, category, slug)

    @router.post("/v1/homebrew/{category}")
    async def homebrew_add(category: str, raw: dict[str, Any]) -> dict[str, str]:
        """Add homebrew while holding the ruleset update lock."""
        async with state.ruleset_lock:
            return _homebrew_add(state, category, raw)

    @router.get("/v1/homebrew")
    def homebrew_list() -> dict[str, dict[str, str]]:
        """Return homebrew identity/category metadata."""
        return _homebrew_list(state)

    @router.delete("/v1/homebrew/{slug}", status_code=204)
    async def homebrew_delete(slug: str) -> None:
        """Delete homebrew while holding the ruleset update lock."""
        async with state.ruleset_lock:
            _homebrew_delete(state, slug)

    @router.post("/v1/forge/item")
    async def forge(req: _ForgeItemRequest) -> dict[str, str]:
        """Forge an item while holding the ruleset update lock."""
        async with state.ruleset_lock:
            return _forge(state, req)

    return router


__all__ = ["build_content_router"]
