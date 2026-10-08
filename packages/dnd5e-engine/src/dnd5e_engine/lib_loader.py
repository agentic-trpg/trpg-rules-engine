"""The asset-loader seam — where the engine gets its rules content.

The engine ships no rules data. Every typed entity it resolves is fetched
through ``get_lib_loader``. Outside combat it returns the process default,
which defaults to the bundled SRD 5.2 corpus (``BundledAssetLoader``). Public
combat operations bind the loader captured when that combat opened.

The engine is edition-agnostic: it resolves whatever typed content it is handed.
To drive it from a different corpus, implement the ``AssetLoader`` protocol and
install it with ``configure_lib_loader``; pass ``None`` to revert to the bundled
corpus. Replacing the default applies to future combats. Loader objects must
remain immutable after installation; replace the loader rather than mutating
an existing instance's assets.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from dnd5e_srd_data.loader import AssetLoader, BundledAssetLoader

_LIB_LOADER: AssetLoader | None = None
_SCOPED_LIB_LOADER: ContextVar[AssetLoader | None] = ContextVar("combat_lib_loader", default=None)


def get_lib_loader() -> AssetLoader:
    global _LIB_LOADER
    scoped = _SCOPED_LIB_LOADER.get()
    if scoped is not None:
        return scoped
    if _LIB_LOADER is None:
        _LIB_LOADER = BundledAssetLoader()
    return _LIB_LOADER


@contextmanager
def scoped_lib_loader(loader: AssetLoader) -> Iterator[None]:
    """Bind a combat's captured ruleset for this operation and nested delivery."""
    token = _SCOPED_LIB_LOADER.set(loader)
    try:
        yield
    finally:
        _SCOPED_LIB_LOADER.reset(token)


def set_lib_loader_for_tests(loader: AssetLoader | None) -> None:
    """Inject a loader (MemoryAssetLoader in tests); None reverts to lazy default."""
    global _LIB_LOADER
    _LIB_LOADER = loader


def configure_lib_loader(loader: AssetLoader | None) -> None:
    """Public host seam: install a custom AssetLoader (e.g. a homebrew
    overlay) for future combats. ``None`` reverts to the lazy bundled
    default. Existing combats keep their original loader; installed loader
    objects and their assets must not be mutated in place."""
    global _LIB_LOADER
    _LIB_LOADER = loader


__all__ = [
    "configure_lib_loader",
    "get_lib_loader",
    "set_lib_loader_for_tests",
]
