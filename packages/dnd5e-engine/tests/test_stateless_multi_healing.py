"""Verify the canonical multi-creature heal through the public stateless evaluator."""

import json
from copy import deepcopy

import pytest
from dnd5e_srd_data import MemoryAssetLoader

from dnd5e_engine.build_spec import derive_multiclass_slots, proficiency_grants
from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest
from dnd5e_engine.evaluation_delta import (
    ActionBudgetUpdate,
    HPDelta,
    ResourceUpdate,
    SlotSpendLedgerUpdate,
)
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import get_default_ruleset, ruleset_binding
from dnd5e_engine.evaluation_spells import ordinary_spell_mechanism
from dnd5e_engine.events import HealingApplied
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import execute
from tests.test_stateless_casting_v20 import canonical_healing_wire

SPELL = "mass-healing-word"


def mass_healing_wire():
    """Build the bound Lore Bard 5 profile and two injured creatures."""
    canonical = get_default_ruleset()
    bard, lore = canonical.get_class("bard"), canonical.get_subclass("lore")
    grants = sorted(
        {
            g.slug
            for source in (bard, lore)
            for g in source.granted_features
            if g.ref_type == "feature" and g.level <= 5
        }
    )
    loader = MemoryAssetLoader(
        classes=[bard],
        subclasses=[lore],
        features=[canonical.get_feature(grant) for grant in grants],
        spells=[canonical.get_spell(SPELL)],
    )
    wire, _ = canonical_healing_wire()
    wire["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    wire["payload"].update(
        spell_id=SPELL,
        target_id=FOE,
        target_ids=[FOE, HERO],
        slot_level=3,
        spell_source_id="class:bard",
    )
    snapshot = wire["state_snapshot"]
    hero, foe = snapshot["character_states"]
    slots = derive_multiclass_slots({"bard": 5}, loader=loader)
    proficiency = proficiency_grants([(bard, 5, "primary")])
    hero.update(
        class_slug="bard",
        classes={"bard": 5},
        subclass_slug="lore",
        character_level=5,
        granted_features=grants,
        charisma=16,
        spellcasting_ability="cha",
        proficiency_bonus_override=None,
        save_proficiencies=sorted(proficiency.saves),
        weapon_proficiencies=sorted(proficiency.weapons),
        spell_slots=slots,
        custom_counters={"feature_use:bardic-inspiration": {"spent": 0}},
        hp_current=2,
        hp_max=30,
    )
    foe.update(hp_current=9, hp_max=50)
    source = snapshot["casting_state"]["actors"][0]["sources"][0]
    source.update(
        source_id="class:bard",
        class_slug="bard",
        ability="cha",
        spells=[{"spell_id": SPELL, "prepared_order": 0, "material": None}],
    )
    snapshot["resource_state"]["pools"] = [
        dict(
            kind="spell_slot",
            owner_id=HERO,
            pool_id=f"spell:{level}",
            level=level,
            current=value,
            maximum=value,
        )
        for level, value in slots.items()
    ] + [
        dict(
            kind="feature_uses",
            owner_id=HERO,
            pool_id="bardic-inspiration",
            feature_slug="bardic-inspiration",
            current=3,
            maximum=3,
        )
    ]
    return wire, loader


def test_multi_heal_resolves_in_selection_order_with_one_payment():
    wire, loader = mass_healing_wire()
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert result == execute(request, loader)
    assert [
        (event.target_id, event.amount)
        for event in result.proposed_events
        if isinstance(event, HealingApplied)
    ] == [(FOE, 11), (HERO, 7)]
    ops = result.state_delta.operations
    assert {op.target_id: op.resulting_hp for op in ops if type(op) is HPDelta} == {
        HERO: 9,
        FOE: 20,
    }
    (resource,) = (op for op in ops if type(op) is ResourceUpdate)
    (ledger,) = (op for op in ops if type(op) is SlotSpendLedgerUpdate)
    (budget,) = (op for op in ops if type(op) is ActionBudgetUpdate)
    assert (resource.owner_id, resource.pool_id, resource.amount) == (HERO, "spell:3", -1)
    assert ledger.value == {"spell_slot:3": 1}
    assert budget.actor_id == HERO
    assert budget.value.bonus_action_taken_this_turn
    assert budget.value.action_available
    assert not budget.value.bonus_action_available
    rng = request.rng_context.state.restore()
    for _ in range(4):
        rng.randint(1, 4)
    assert result.rng_transition.next_state == RNGState.capture(rng)


@pytest.mark.parametrize("count", [6, 7])
def test_delivery_count_uses_the_canonical_limit(count):
    wire, loader = mass_healing_wire()
    snapshot = wire["state_snapshot"]
    state = snapshot["combat_state"]
    for i in range(count - 2):
        extra = f"mon:extra-{i}"
        snapshot["character_states"].append(
            {**deepcopy(snapshot["character_states"][1]), "entity_id": extra}
        )
        state["initiative_ids"].append(extra)
        state["encounter_ids"].append(extra)
        state["actor_zone"][extra] = state["actor_zone"][FOE]
        state["movement_ledgers"][extra] = state["movement_ledgers"][FOE]
        state["xp_value_by_entity"][extra] = state["xp_value_by_entity"][FOE]
        wire["payload"]["target_ids"].append(extra)
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    result = execute(request, loader)
    if count == 6:
        assert result.status == "accepted", result.error
        assert [e.target_id for e in result.proposed_events if isinstance(e, HealingApplied)] == (
            wire["payload"]["target_ids"]
        )
    else:
        assert result.status == "rejected", result.error
        assert result.error.code == "target_invalid"
        assert "count" in result.error.reason
        assert result.state_delta is None
        assert not result.proposed_events
        assert result.rng_transition is None


def test_unrelated_higher_rank_or_effect_healing_stays_outside_this_profile():
    canonical = get_default_ruleset()
    assert ordinary_spell_mechanism(canonical, SPELL) == "healing"
    for slug in ("mass-cure-wounds", "prayer-of-healing", "heal", "aid"):
        assert ordinary_spell_mechanism(canonical, slug) is None
