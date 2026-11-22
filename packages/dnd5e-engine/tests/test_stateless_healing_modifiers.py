"""Effect contributions drawn from a real exploration cast and bound class grants."""

import json
from random import Random

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader

from dnd5e_engine.death_saves import roll_death_save
from dnd5e_engine.evaluation_actor import combatant
from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest
from dnd5e_engine.evaluation_delta import WorldEffectCreate
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_engine.events import DeathSaveRolled, EffectApplied
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import execute
from tests.test_stateless_exploration_healing import exploration_wire

BEACON = "beacon-of-hope"


def _beacon_cast(target_id):
    """Return a fixed casting request with canonical Cleric and Life Domain grants."""
    wire, _ = exploration_wire()
    canonical = BundledAssetLoader()
    cleric = canonical.get_class("cleric")
    life = canonical.get_subclass("life")
    grants = tuple(
        grant.slug
        for asset in (cleric, life)
        for grant in asset.granted_features
        if grant.level <= 5 and grant.ref_type == "feature"
    )
    loader = MemoryAssetLoader(
        classes=[cleric],
        subclasses=[life],
        features=[canonical.get_feature(grant) for grant in grants],
        spells=[canonical.get_spell(slug) for slug in ("cure-wounds", BEACON)],
    )
    wire["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    wire["payload"].update(spell_id=BEACON, slot_level=3, target_id=target_id)
    wire["rng_context"]["state"] = RNGState.capture(Random(1)).model_dump(mode="json")
    snapshot = wire["state_snapshot"]
    actor = next(a for a in snapshot["character_states"] if a["entity_id"] == HERO)
    capacities = {1: 4, 2: 3, 3: 2}
    actor.update(
        classes={"cleric": 5},
        character_level=5,
        subclass_slug="life",
        granted_features=grants,
        spell_slots=capacities,
        custom_counters={"feature_use:channel-divinity-cleric": {"spent": 0}},
    )
    snapshot["resource_state"]["pools"] = [
        {
            "kind": "spell_slot",
            "owner_id": HERO,
            "pool_id": f"spell:{level}",
            "current": maximum,
            "maximum": maximum,
            "level": level,
        }
        for level, maximum in capacities.items()
    ] + [
        {
            "kind": "feature_uses",
            "owner_id": HERO,
            "pool_id": "feature:channel-divinity-cleric",
            "current": 2,
            "maximum": 2,
            "feature_slug": "channel-divinity-cleric",
        }
    ]
    source = snapshot["casting_state"]["actors"][0]["sources"][0]
    source["spells"].append({"spell_id": BEACON, "prepared_order": 1, "material": None})
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    return request, loader


def _cast_beacon(target_id=FOE):
    """Use the real caster, target, and ruleset to obtain the applied effect."""
    request, loader = _beacon_cast(target_id)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert not result.rng_transition.state_changed
    spend, create = result.state_delta.operations
    assert spend.expected.level == 3
    assert isinstance(create, WorldEffectCreate)
    assert create.value.effect.target_id == target_id
    assert create.value.duration_seconds == 60
    assert create.value.concentration
    applied = result.proposed_events[-1]
    assert isinstance(applied, EffectApplied)
    return request, loader, create.value.effect, applied.effect


@pytest.mark.parametrize(
    ("ability", "disadvantage", "mode", "natural"),
    [("wis", (), "advantage", 19), ("dex", (), "normal", 5), ("wis", ("flag",), "normal", 5)],
)
def test_beacon_changes_real_standalone_saves(ability, disadvantage, mode, natural):
    cast, loader, effect, _ = _cast_beacon()
    wire = json.loads(cast.model_dump_json())
    wire.pop("spell_positions")
    wire.update(
        actor_id=FOE,
        operation_kind="rules.save",
        payload={
            "kind": "rules.save",
            "target_id": FOE,
            "ability": ability,
            "dc": 10,
            "advantage": [],
            "disadvantage": disadvantage,
            "is_magical": False,
        },
    )
    wire["state_snapshot"]["effect_states"] = [effect.model_dump(mode="json")]
    request = RuleEvaluationRequest.model_validate_json(json.dumps(wire))
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    (roll,) = result.proposed_events
    assert (roll.advantage, roll.natural) == (mode, natural)
    assert ("effect" in roll.sources) == (ability == "wis")
    expected = Random(1)
    for _ in range(2 if mode == "advantage" else 1):
        expected.randint(1, 20)
    assert result.rng_transition.next_state == RNGState.capture(expected)
    assert not result.state_delta.operations

    wire["state_snapshot"]["effect_states"][0]["changes"][0]["value"] = False
    forged = execute(RuleEvaluationRequest.model_validate_json(json.dumps(wire)), loader)
    assert forged.error.code == "save.effects"
    assert forged.rng_transition is None


def test_beacon_changes_the_shared_death_save_primitive_for_its_recipient():
    cast, _, _, effect = _cast_beacon(HERO)
    actor = next(a for a in cast.state_snapshot.character_states if a.entity_id == HERO)
    dying = combatant(actor).model_copy(update={"hp_current": 0})
    assisted = roll_death_save(dying, Random(1), active_effects=[effect])
    baseline = roll_death_save(
        dying, Random(1), active_effects=[effect.model_copy(update={"target_id": FOE})]
    )
    advantaged_roll = next(e for e in assisted.events if isinstance(e, DeathSaveRolled))
    normal_roll = next(e for e in baseline.events if isinstance(e, DeathSaveRolled))
    assert assisted.outcome == "ongoing"
    assert baseline.outcome == "ongoing"
    assert (advantaged_roll.running_successes, advantaged_roll.running_failures) == (1, 0)
    assert (normal_roll.running_successes, normal_roll.running_failures) == (0, 1)
