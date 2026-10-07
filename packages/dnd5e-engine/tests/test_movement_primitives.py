"""Pure occupancy, additive movement costs and shared speed accounting."""

from fractions import Fraction

import pytest
from dnd5e_srd_data.schema.monster import CreatureSize
from pydantic import ValidationError

from dnd5e_engine.activities.passive_stats import CombatantMovementModes
from dnd5e_engine.movement import (
    EffectiveSpeeds,
    MovementGrant,
    MovementLedger,
    MovementMode,
    SpeedModifier,
    can_enter_creature_space,
    creature_space_is_difficult,
    grapple_drag_extra_cost,
    has_special_speed,
    project_speeds,
    speed_for_mode,
    step_cost,
)


@pytest.mark.parametrize("mover", list(CreatureSize))
@pytest.mark.parametrize("occupant", list(CreatureSize))
def test_pass_through_uses_exact_size_categories(
    mover: CreatureSize, occupant: CreatureSize
) -> None:
    sizes = list(CreatureSize)
    expected = occupant == CreatureSize.TINY or abs(sizes.index(mover) - sizes.index(occupant)) >= 2
    assert can_enter_creature_space(mover, occupant) == expected
    assert can_enter_creature_space(mover, occupant, allied=True)
    assert can_enter_creature_space(mover, occupant, incapacitated=True)
    assert creature_space_is_difficult(occupant) == (occupant != CreatureSize.TINY)
    assert not creature_space_is_difficult(occupant, allied=True)


@pytest.mark.parametrize(
    ("mode", "special", "ordinary", "difficult"),
    [
        ("walk", False, 5, 10),
        ("crawl", False, 10, 15),
        ("crawl", True, 10, 15),
        ("climb", False, 10, 15),
        ("climb", True, 5, 10),
        ("swim", False, 10, 15),
        ("swim", True, 5, 10),
    ],
)
def test_movement_extra_feet_are_additive(
    mode: MovementMode, special: bool, ordinary: int, difficult: int
) -> None:
    assert step_cost(5, mode=mode, has_special_speed=special) == ordinary
    assert step_cost(5, mode=mode, has_special_speed=special, difficult_terrain=True) == difficult
    assert (
        step_cost(
            5,
            mode=mode,
            has_special_speed=special,
            difficult_terrain=True,
            creature_space_difficult=True,
            drag_extra=True,
        )
        == difficult + 5
    )


def test_drag_exemptions_are_directional_and_do_not_stack_per_victim() -> None:
    assert not grapple_drag_extra_cost(CreatureSize.LARGE, [])
    assert not grapple_drag_extra_cost(CreatureSize.SMALL, [CreatureSize.TINY])
    assert not grapple_drag_extra_cost(CreatureSize.HUGE, [CreatureSize.MEDIUM])
    assert grapple_drag_extra_cost(CreatureSize.MEDIUM, [CreatureSize.HUGE])
    targets = [CreatureSize.TINY, CreatureSize.MEDIUM, CreatureSize.LARGE]
    assert grapple_drag_extra_cost(CreatureSize.LARGE, targets)
    assert step_cost(5, drag_extra=grapple_drag_extra_cost(CreatureSize.LARGE, targets)) == 10


def test_ledger_deducts_all_consumed_movement_when_switching_speeds() -> None:
    initial = MovementLedger()
    walked = initial.spend(cost_ft=20, distance_ft=20, mode="walk", effective_speed=30)
    assert initial.spent_ft == 0
    assert walked.remaining(30) == 10
    assert walked.remaining(20) == 0
    assert walked.remaining(40) == 20
    climbed = walked.spend(cost_ft=10, distance_ft=5, mode="climb", effective_speed=40)
    assert climbed.spent_ft == 30
    assert climbed.distance_ft == 25
    assert climbed.active_mode == "climb"
    assert climbed.remaining(30) == 0
    assert climbed.remaining(40) == 10


def test_dash_expands_the_same_ledger_and_cannot_erase_terrain_costs() -> None:
    ledger = MovementLedger().add_dash()
    cost = step_cost(10, mode="crawl", difficult_terrain=True, drag_extra=True)
    ledger = ledger.spend(cost_ft=cost, distance_ft=10, mode="crawl", effective_speed=30)
    assert ledger.spent_ft == 40
    assert ledger.remaining(30) == 20
    assert ledger.remaining(20) == 0
    assert ledger.add_dash().remaining(30) == 50
    assert ledger.remaining(0) == 0


def test_ledger_and_step_reject_invalid_costs_without_mutation() -> None:
    ledger = MovementLedger()
    for cost, distance in [(-1, 5), (5, -1), (35, 5)]:
        with pytest.raises(ValueError):
            ledger.spend(cost_ft=cost, distance_ft=distance, mode="walk", effective_speed=30)
    with pytest.raises(ValueError):
        step_cost(-5)
    with pytest.raises(ValidationError):
        MovementLedger(spent_ft=-1)
    assert ledger == MovementLedger()


def test_scoped_grant_preserves_direction_source_and_oa_policy() -> None:
    grant = MovementGrant(
        max_distance_ft=15,
        source_id="feature:withdraw",
        direction="away_from_target",
        target_id="foe",
        lifespan="current_turn",
    )
    assert not grant.provokes_opportunity_attacks
    assert grant.target_id == "foe"
    with pytest.raises(ValidationError):
        MovementGrant(max_distance_ft=10, source_id="feature", direction="toward_target")
    with pytest.raises(ValidationError):
        MovementGrant(max_distance_ft=-1, source_id="feature")
    with pytest.raises(ValidationError):
        MovementGrant(max_distance_ft=10, source_id="")
    with pytest.raises(ValidationError):
        grant.max_distance_ft = 30


def test_all_speeds_share_exhaustion_flat_and_fractional_modifiers() -> None:
    speeds = project_speeds(
        40,
        CombatantMovementModes(climb=30, swim=50, fly=60, burrow=20),
        exhaustion_level=1,
        modifiers=[SpeedModifier("reduce", 10), SpeedModifier("multiply", Fraction(1, 2))],
    )
    assert speeds == EffectiveSpeeds(walk=12, climb=7, swim=17, fly=22, burrow=2)
    reverse = project_speeds(
        40,
        CombatantMovementModes(),
        exhaustion_level=1,
        modifiers=[SpeedModifier("multiply", Fraction(1, 2)), SpeedModifier("reduce", 10)],
    )
    assert reverse.walk == 7
    assert reverse.climb is None


@pytest.mark.parametrize("condition", ["grappled", "restrained", "petrified", "paralyzed"])
def test_condition_speed_zero_overrides_bonuses_for_every_speed(condition: str) -> None:
    assert project_speeds(
        30,
        CombatantMovementModes(climb=20, swim=40, fly=60, burrow=10),
        condition_names=[condition],
        modifiers=[SpeedModifier("add", 100)],
    ) == EffectiveSpeeds(walk=0, climb=0, swim=0, fly=0, burrow=0)


def test_mode_speed_uses_matching_special_speed_or_walk_fallback() -> None:
    speeds = EffectiveSpeeds(walk=30, climb=20, swim=0, fly=60)
    assert speed_for_mode(speeds, "climb") == 20
    assert has_special_speed(speeds, "climb")
    assert speed_for_mode(speeds, "swim") == 0
    assert not has_special_speed(speeds, "swim")
    assert speed_for_mode(EffectiveSpeeds(walk=30), "swim") == 30
    for mode in ("walk", "crawl"):
        assert speed_for_mode(speeds, mode) == 30
        assert not has_special_speed(speeds, mode)
    with pytest.raises(ValueError):
        SpeedModifier("reduce", -1)
    with pytest.raises(ValueError):
        SpeedModifier("multiply", Fraction(-1, 2))
