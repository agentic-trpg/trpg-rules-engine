"""Judge tremorsense location clues from explicit World contact and surface facts.

The host selects nearby candidates and supplies current contacts. This module
does not infer contact from proximity, grant sight, or identify a detected target.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from dnd5e_engine.spatial import GridTopology


@dataclass(frozen=True, slots=True)
class Surface:
    """Identify an authored floor or wall and its known material."""

    surface_id: str
    kind: Literal["floor", "wall"]
    material: Literal["stone", "other", "unknown"] = "unknown"

    def __post_init__(self) -> None:
        """Reject malformed authored geometry and unknown material vocabulary."""
        if (
            type(self.surface_id) is not str
            or not self.surface_id
            or self.kind not in ("floor", "wall")
            or self.material not in ("stone", "other", "unknown")
        ):
            raise ValueError("invalid surface identity, kind, or material")


@dataclass(frozen=True, slots=True)
class SurfaceConnection:
    """One canonical undirected connection between distinct surfaces."""

    first: str
    second: str

    def __post_init__(self) -> None:
        """Reject duplicate orientations at the value boundary."""
        if not self.first or not self.second or self.first >= self.second:
            raise ValueError("surface connection must have sorted distinct IDs")


@dataclass(frozen=True, slots=True)
class SurfaceTopology:
    """Read one version of authored surfaces through a bounded adjacency list."""

    version: int
    surfaces: tuple[Surface, ...]
    connections: tuple[SurfaceConnection, ...] = ()
    _adjacency: dict[str, tuple[str, ...]] = field(init=False, repr=False, compare=False)
    _stone: frozenset[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Check unique IDs and one canonical edge for every connection."""
        known = {surface.surface_id for surface in self.surfaces}
        if len(known) != len(self.surfaces) or "" in known:
            raise ValueError("surface IDs must be unique and nonempty")
        edges = {(edge.first, edge.second) for edge in self.connections}
        if len(edges) != len(self.connections) or any(
            edge.first not in known or edge.second not in known for edge in self.connections
        ):
            raise ValueError("surface connections must be unique and reference known IDs")
        neighbors: dict[str, list[str]] = {surface_id: [] for surface_id in known}
        for edge in self.connections:
            neighbors[edge.first].append(edge.second)
            neighbors[edge.second].append(edge.first)
        object.__setattr__(
            self,
            "_adjacency",
            {surface_id: tuple(sorted(ids)) for surface_id, ids in neighbors.items()},
        )
        object.__setattr__(
            self, "_stone", frozenset(s.surface_id for s in self.surfaces if s.material == "stone")
        )

    def adjacency(self) -> dict[str, tuple[str, ...]]:
        """Build a detached adjacency list for this immutable topology version."""
        return dict(self._adjacency)

    def connected(self, contacts: frozenset[str], *, stone_only: bool = False) -> frozenset[str]:
        """Traverse connected surfaces from contacts, restricting stone when requested."""
        allowed = self._stone if stone_only else self._adjacency.keys()
        reached = set(contacts & allowed)
        if not reached:
            return frozenset()
        pending = deque(reached)
        while pending:
            for adjacent in self._adjacency[pending.popleft()]:
                if adjacent in allowed and adjacent not in reached:
                    reached.add(adjacent)
                    pending.append(adjacent)
        return frozenset(reached)


def reachable_surfaces(
    contacts: frozenset[str],
    *,
    material: Callable[[str], str | None],
    neighbors: Callable[[str], tuple[str, ...]],
    stonecunning: bool = False,
) -> frozenset[str]:
    """Traverse current surface neighbors once from the observer's contact set.

    The host resolves anchored edges from its own current facts. RE owns the
    connected-surface and Stonecunning judgment; the host may supply an indexed
    adjacency reader so unrelated components are never enumerated.
    """

    def eligible(surface_id: str) -> bool:
        """Apply the optional stone restriction to one known surface."""
        value = material(surface_id)
        return value == "stone" if stonecunning else value is not None

    reached = {surface_id for surface_id in contacts if eligible(surface_id)}
    pending = deque(reached)
    while pending:
        for adjacent in neighbors(pending.popleft()):
            if adjacent not in reached and eligible(adjacent):
                reached.add(adjacent)
                pending.append(adjacent)
    return frozenset(reached)


def tremorsense_location(
    *,
    topology: GridTopology,
    surfaces: SurfaceTopology | None,
    observer_cell: str,
    candidate_cell: str,
    observer_contacts: frozenset[str],
    candidate_contacts: frozenset[str],
    range_ft: int | None,
    candidate_kind: Literal["creature", "object"],
    candidate_moving: bool = False,
    observer_airborne: bool = False,
    candidate_airborne: bool = False,
    stonecunning: bool = False,
    reachable_surfaces: frozenset[str] | None = None,
) -> str | None:
    """Return only the candidate cell when current facts prove a vibration clue.

    Generic tremorsense accepts connected surfaces of any known material.
    Stonecunning restricts the connected component to stone. A moving object
    needs a host motion fact. Callers may reuse a reached set for many nearby
    candidates in one version.
    """
    if (
        not range_ft
        or observer_airborne
        or candidate_airborne
        or (candidate_kind == "object" and not candidate_moving)
        or not topology.within_range(observer_cell, candidate_cell, range_ft)
    ):
        return None
    if reachable_surfaces is None:
        if surfaces is None:
            raise ValueError("surface topology or precomputed reachability is required")
        reached = surfaces.connected(observer_contacts, stone_only=stonecunning)
    else:
        reached = reachable_surfaces
    return candidate_cell if reached & candidate_contacts else None
