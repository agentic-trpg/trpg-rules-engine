"""Independent shared-core correctness checks required by stateless migration."""

from copy import deepcopy

import pytest
from dnd5e_srd_data import BundledAssetLoader
from dnd5e_srd_data.schema.action_policy import ActionPolicy

from dnd5e_engine import orchestrator as orch
from dnd5e_engine.events import EffectApplied
from dnd5e_engine.lib_loader import scoped_lib_loader
from dnd5e_engine.types.effects import ActiveEffect
from tests.c21_support import act, combatant, pc, start


@pytest.mark.parametrize("pact", [False, True])
def test_spell_expenditure_records_actual_payer_and_pool_not_effect_recipient(pact):
    with scoped_lib_loader(BundledAssetLoader()):
        handle, live = start(
            [
                pc(
                    class_slug="wizard",
                    character_level=5,
                    intelligence=16,
                    spells_known=["haste"],
                    spell_slots={3: 0 if pact else 2},
                    pact_slots={3: 2} if pact else {},
                ),
                pc("char:ally", initiative=19, zone_id="2,0", spell_slots={3: 2}),
            ],
            seed=1,
        )
        act(
            handle,
            "char:hero",
            intent_type="cast_spell",
            spell_id="haste",
            slot_level=3,
            target_id="char:ally",
            willing_target_ids=("char:ally",),
        )
        resource = "pact_slot:3" if pact else "spell_slot:3"
        assert live.expended_resources == {"char:hero": {resource: 1}}
        pool = live.pact_slots_by_entity if pact else live.spell_slots_by_entity
        assert pool["char:hero"][3] == 1
        assert live.spell_slots_by_entity["char:ally"][3] == 2
        # Projected history is a report of this one payment, never another debit.
        assert orch._project_outcome(live).expended_resources == live.expended_resources


@pytest.mark.parametrize(
    "training,denial,accepted",
    [
        ((), "bonus", False),
        ((), "action", True),
        (("scimitar",), "bonus", True),
    ],
)
def test_nick_action_policy_uses_actual_mastery_training(training, denial, accepted):
    with scoped_lib_loader(BundledAssetLoader()):
        handle, live = start([pc(equipment=("dagger", "scimitar"))], seed=1)
        orch._update_combatant(live, "char:hero", weapon_mastery_slugs=training)
        act(handle, "char:hero", intent_type="attack", weapon_id="dagger", target_id="mon:foe")
        orch._emit(
            live,
            EffectApplied(
                effect=ActiveEffect(
                    id="effect:restriction",
                    origin="test:policy",
                    name="Restriction",
                    target_id="char:hero",
                    action_policy=ActionPolicy(
                        deny_bonus_actions=denial == "bonus", deny_actions=denial == "action"
                    ),
                )
            ),
        )
        before = (deepcopy(live.initiative), live.rng.getstate(), deepcopy(live.event_log))
        if accepted:
            act(
                handle, "char:hero", intent_type="attack", weapon_id="scimitar", target_id="mon:foe"
            )
            assert combatant(live).bonus_action_available == bool(training)
            assert live.rng.getstate() != before[1]
        else:
            with pytest.raises(orch.IntentRejectedError, match="action_restricted"):
                act(
                    handle,
                    "char:hero",
                    intent_type="attack",
                    weapon_id="scimitar",
                    target_id="mon:foe",
                )
            assert (live.initiative, live.rng.getstate(), live.event_log) == before
