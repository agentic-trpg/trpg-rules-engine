"""Weighted routes and reachable cells share deterministic legal step costs."""

import random

import pytest

from dnd5e_engine.movement import step_cost
from dnd5e_engine.spatial import GridTopology, canonical_cell_id
from dnd5e_engine.specs import GridScene, WallSegment


def _route(reached: dict[str, tuple[int, str | None]], target: str) -> list[str]:
    path = [target]
    while (previous := reached[path[-1]][1]) is not None:
        path.append(previous)
    return path[::-1]


def test_lowest_cost_path_takes_a_longer_route_to_avoid_expensive_ground() -> None:
    grid = GridTopology(
        GridScene(
            width=8,
            height=3,
            difficult_terrain_cells=[f"{col},0" for col in range(1, 7)],
            blocked_cells=[f"{col},1" for col in range(1, 7)],
        )
    )
    shortest = grid.shortest_path("0,0", "7,0")
    cheapest = grid.lowest_cost_path("0,0", "7,0")
    assert len(shortest) == 8
    assert len(cheapest) == 12
    reached = grid.reachable_cells("0,0", 55)
    assert reached["7,0"][0] == 55
    assert _route(reached, "7,0") == cheapest
    assert grid.lowest_cost_path("0,0", "7,0", budget_ft=55) == cheapest
    assert grid.lowest_cost_path("0,0", "7,0", budget_ft=54) == []


def test_equal_cost_routes_prefer_fewer_steps_even_when_discovered_later() -> None:
    grid = GridTopology(GridScene(width=3, height=2))
    costs = {
        ("0,0", "1,0"): 14,
        ("1,0", "2,0"): 1,
        ("0,0", "0,1"): 5,
        ("0,1", "1,1"): 5,
        ("1,1", "2,0"): 5,
    }

    def oracle(a: str, b: str) -> int | None:
        return costs.get((a, b))

    expected = ["0,0", "1,0", "2,0"]
    assert grid.lowest_cost_path("0,0", "2,0", step_cost=oracle) == expected
    assert _route(grid.reachable_cells("0,0", 15, step_cost=oracle), "2,0") == expected


def test_additive_oracle_controls_reachability_and_routes_without_double_terrain() -> None:
    grid = GridTopology(GridScene(width=4, height=1, difficult_terrain_cells=["1,0"]))

    def oracle(a: str, b: str) -> int | None:
        if b == "3,0":
            return None
        return step_cost(
            grid.cell_size_ft,
            mode="crawl",
            difficult_terrain=grid.is_difficult_terrain(b),
            creature_space_difficult=b == "1,0",
            drag_extra=True,
        )

    reached = grid.reachable_cells("0,0", 35, step_cost=oracle)
    assert reached == {"0,0": (0, None), "1,0": (20, "0,0"), "2,0": (35, "1,0")}
    assert grid.lowest_cost_path("0,0", "2,0", step_cost=oracle) == ["0,0", "1,0", "2,0"]
    assert grid.lowest_cost_path("0,0", "3,0", step_cost=oracle) == []


def test_oracle_cannot_bypass_wall_blocked_cell_or_diagonal_corner() -> None:
    grid = GridTopology(
        GridScene(
            width=3,
            height=3,
            blocked_cells=["1,0"],
            wall_segments=[WallSegment(x1=1, y1=1, x2=1, y2=3)],
        )
    )
    observed: list[tuple[str, str]] = []

    def oracle(a: str, b: str) -> int:
        observed.append((a, b))
        return 0

    assert grid.lowest_cost_path("0,0", "2,2", step_cost=oracle) == []
    assert set(grid.reachable_cells("0,0", 30, step_cost=oracle)) == {"0,0", "0,1", "0,2"}
    assert ("0,0", "1,1") not in observed
    assert all(b != "1,0" for _, b in observed)


def test_weighted_paths_are_deterministic_draw_free_and_canonical() -> None:
    grid = GridTopology(GridScene(width=5, height=5))
    rng = random.Random(817)
    before = rng.getstate()
    expected = ["0,0", "1,0", "2,1", "3,2"]
    for _ in range(5):
        path = grid.lowest_cost_path(" 0, 0", "03,02", avoid=["01,01"])
        assert path == expected
        assert _route(grid.reachable_cells("0,0", 15, avoid=["1,1"]), "3,2") == path
    assert rng.getstate() == before
    assert canonical_cell_id(" 003, 02 ") == "3,2"
    assert grid.lowest_cost_path("bad", "1,1") == []
    assert grid.lowest_cost_path("0,0", "9,9") == []
    assert grid.lowest_cost_path("0,0", "0,0", budget_ft=0) == ["0,0"]
    assert grid.lowest_cost_path("0,0", "0,0", budget_ft=-1) == []
    assert grid.lowest_cost_path("0,0", "1,1", avoid=["1,1"]) == []


def test_negative_oracle_cost_fails_before_constructing_a_route() -> None:
    grid = GridTopology(GridScene(width=2, height=1))
    with pytest.raises(ValueError, match="nonnegative"):
        grid.lowest_cost_path("0,0", "1,0", step_cost=lambda _a, _b: -1)
