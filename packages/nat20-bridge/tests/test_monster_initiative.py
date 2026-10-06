"""Bridge keeps its fixed-Initiative boundary and one draw per monster."""

import random
from pathlib import Path

from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader

from nat20_bridge.routes_combat import _build_encounter_specs
from nat20_bridge.state import BridgeState


def test_canonical_totals_preserve_bridge_draw_order():
    state = BridgeState(homebrew_path=Path("unused.json"), loader=BundledAssetLoader())
    rng = random.Random(11)
    expected = random.Random(11)
    specs, _ = _build_encounter_specs(state, ["aboleth", "tough", "adult-red-dragon"], rng)
    assert [s.initiative for s in specs] == [expected.randint(1, 20) + m for m in (7, 1, 12)]
    assert rng.getstate() == expected.getstate()
    assert all("attack_bonus" not in s.model_fields_set for s in specs)
    repeat, _ = _build_encounter_specs(
        state, ["aboleth", "tough", "adult-red-dragon"], random.Random(11)
    )
    assert repeat == specs


def test_old_canonical_falls_back_to_dex_and_zero_total_is_valid():
    monster = BundledAssetLoader().get_monster("wolf")
    for total, expected_mod in ((None, 2), (0, 0)):
        old = monster.model_copy(update={"initiative_modifier": total})
        state = BridgeState(
            homebrew_path=Path("unused.json"), loader=MemoryAssetLoader(monsters=[old])
        )
        specs, _ = _build_encounter_specs(state, ["wolf"], random.Random(11))
        assert specs[0].initiative == 15 + expected_mod
