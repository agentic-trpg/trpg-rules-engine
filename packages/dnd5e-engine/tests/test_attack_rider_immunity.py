"""Known damage immunity cannot pay Sneak Attack dice for Obscure."""

from __future__ import annotations

from random import Random

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader

from dnd5e_engine import AttackRiderRequest
from dnd5e_engine.events import AttackRiderTriggered, ConditionApplied, DamageApplied, SaveRolled
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from tests.c20_support import act, combatant, events, foe, pc, start


@pytest.mark.parametrize("immunities", [["piercing"], ["all"]])
def test_obscure_never_sacrifices_sneak_dice_when_its_weapon_damage_is_immune(immunities) -> None:
    set_lib_loader_for_tests(BundledAssetLoader())
    try:
        rogue = pc(
            class_slug="rogue",
            character_level=14,
            dexterity=18,
            hp_current=100,
            hp_max=100,
            equipment=("dagger",),
        )
        ally = pc("char:ally", initiative=10, zone_id="1,1")
        handle, live = start(
            [rogue, ally], seed=4, encounter=[foe(ac=1, damage_immunities=immunities)]
        )
        expected = Random()
        expected.setstate(live.rng.getstate())
        expected.randint(1, 20)
        expected.randint(1, 4)
        act(
            handle,
            rogue.entity_id,
            intent_type="attack",
            weapon_id="dagger",
            target_id="mon:foe",
            attack_riders=(
                AttackRiderRequest(feature_id="devious-strikes", activity_id="ki4lIPVGNA0HjEzH"),
            ),
        )
        assert [event.amount for event in events(live, DamageApplied)] == [0]
        assert not events(live, SaveRolled)
        assert not events(live, AttackRiderTriggered)
        assert not events(live, ConditionApplied)
        assert combatant(live, rogue.entity_id).sneak_attack_spent_this_turn is False
        assert live.rng.getstate() == expected.getstate()
    finally:
        set_lib_loader_for_tests(None)
