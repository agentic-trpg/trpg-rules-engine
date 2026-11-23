"""Tremorsense uses explicit contact and does not turn a clue into sight."""

import pytest

from dnd5e_engine.spatial import GridTopology
from dnd5e_engine.specs import GridScene
from dnd5e_engine.tremorsense import (
    Surface,
    SurfaceConnection,
    SurfaceTopology,
    reachable_surfaces,
    tremorsense_location,
)


def test_shared_contact_and_connected_stone_are_distinct():
    grid = GridTopology(GridScene(width=30, height=1))
    surfaces = SurfaceTopology(
        4,
        (
            Surface("a", "floor", "stone"),
            Surface("b", "wall", "stone"),
            Surface("c", "floor", "stone"),
            Surface("d", "floor", "other"),
        ),
        (SurfaceConnection("a", "b"), SurfaceConnection("b", "c")),
    )
    args = dict(
        topology=grid,
        surfaces=surfaces,
        observer_cell="0,0",
        candidate_cell="2,0",
        observer_contacts=frozenset({"a"}),
        candidate_contacts=frozenset({"c"}),
        range_ft=60,
        candidate_kind="creature",
    )
    assert tremorsense_location(**args) == "2,0"
    assert tremorsense_location(**args, stonecunning=True) == "2,0"
    cut = SurfaceTopology(5, surfaces.surfaces, (SurfaceConnection("a", "b"),))
    assert tremorsense_location(**{**args, "surfaces": cut}, stonecunning=True) is None
    assert tremorsense_location(**{**args, "surfaces": cut}) is None
    assert tremorsense_location(**{**args, "candidate_contacts": frozenset({"a"})}) == "2,0"


def test_range_airborne_and_object_motion_require_explicit_facts():
    grid = GridTopology(GridScene(width=30, height=1))
    surfaces = SurfaceTopology(1, (Surface("ground", "floor"),))
    args = dict(
        topology=grid,
        surfaces=surfaces,
        observer_cell="0,0",
        candidate_cell="2,0",
        observer_contacts=frozenset({"ground"}),
        candidate_contacts=frozenset({"ground"}),
        range_ft=60,
        candidate_kind="creature",
    )
    assert tremorsense_location(**args) == "2,0"
    assert tremorsense_location(**args, observer_airborne=True) is None
    assert tremorsense_location(**args, candidate_airborne=True) is None
    assert tremorsense_location(**{**args, "range_ft": 5}) is None
    assert tremorsense_location(**{**args, "candidate_kind": "object"}) is None
    assert (
        tremorsense_location(**{**args, "candidate_kind": "object"}, candidate_moving=True) == "2,0"
    )


def test_canonical_edges_are_required():
    with pytest.raises(ValueError, match="material"):
        Surface("a", "floor", "lava")
    with pytest.raises(ValueError, match="sorted"):
        SurfaceConnection("z", "a")
    with pytest.raises(ValueError, match="unique"):
        SurfaceTopology(
            1,
            (Surface("a", "floor"), Surface("b", "wall")),
            (SurfaceConnection("a", "b"), SurfaceConnection("a", "b")),
        )


def test_bounded_reachability_uses_material_only_for_stonecunning():
    material = {"stone": "stone", "wood": "other", "far": "stone"}
    adjacency = {"stone": ("wood",), "wood": ("stone",), "far": ()}
    visits = []

    def neighbors(surface_id):
        visits.append(surface_id)
        return adjacency[surface_id]

    assert reachable_surfaces(
        frozenset({"stone"}), material=material.get, neighbors=neighbors
    ) == frozenset({"stone", "wood"})
    assert visits == ["stone", "wood"]
    visits.clear()
    assert reachable_surfaces(
        frozenset({"stone"}),
        material=material.get,
        neighbors=neighbors,
        stonecunning=True,
    ) == frozenset({"stone"})
    assert visits == ["stone"]
