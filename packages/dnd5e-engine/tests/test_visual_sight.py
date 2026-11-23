"""Review the exploration sight kernel and the bound See Invisibility grant."""

from dnd5e_srd_data.loader import BundledAssetLoader

from dnd5e_engine.activities.passive_stats import CombatantSenses
from dnd5e_engine.evaluation_timed_spells import _passive_profile
from dnd5e_engine.spatial import GridTopology
from dnd5e_engine.specs import GridScene
from dnd5e_engine.visual_sight import SEE_INVISIBLE_KEY, can_see_creature


def test_visual_kernel_composes_blind_invisible_light_and_cover():
    grid = GridTopology(GridScene(width=5, height=5))
    origin, target = "0,0", "2,0"
    ordinary = CombatantSenses()
    assert can_see_creature(grid, origin, target, ordinary)
    assert not can_see_creature(grid, origin, target, ordinary, invisible=True)
    assert can_see_creature(grid, origin, target, ordinary, invisible=True, can_see_invisible=True)
    assert not can_see_creature(
        grid, origin, target, ordinary, invisible=True, blinded=True, can_see_invisible=True
    )
    assert can_see_creature(
        grid, origin, target, CombatantSenses(blindsight=10), invisible=True, blinded=True
    )
    assert not can_see_creature(
        grid, origin, target, CombatantSenses(blindsight=5), invisible=True, blinded=True
    )
    assert can_see_creature(grid, origin, target, CombatantSenses(truesight=10), invisible=True)
    assert not can_see_creature(grid, origin, target, CombatantSenses(truesight=5), invisible=True)
    blocked = GridTopology(GridScene(width=5, height=5, blocked_cells=["1,0"]))
    assert not can_see_creature(
        blocked,
        origin,
        target,
        CombatantSenses(blindsight=30, truesight=30),
        invisible=True,
        can_see_invisible=True,
    )


def test_bound_see_invisibility_issues_one_structured_grant():
    spell = BundledAssetLoader().get_spell("see-invisibility")
    _, effect, _ = _passive_profile(spell, target_id="char:seer", caster_id="char:seer")
    assert effect.duration.seconds == 3600
    assert [(change.key, change.mode, change.value) for change in effect.changes] == [
        (SEE_INVISIBLE_KEY, "override", True)
    ]
