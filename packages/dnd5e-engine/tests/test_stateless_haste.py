"""Public stateless Haste cast, paired follow-up, and exact target end boundaries."""

import json
from copy import deepcopy

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader

from dnd5e_engine.evaluation_casting import CastingStateV20, SpellMaterial, SpellSelection
from dnd5e_engine.evaluation_contracts import (
    SCHEMA_VERSION,
    CombatIntentPayload,
    RuleEvaluationRequest,
    RuleEvaluationResult,
)
from dnd5e_engine.evaluation_delta import ConcentrationUpdate, EffectLifecycleUpdate
from dnd5e_engine.evaluation_haste_delta import EffectFollowUpCreate, HasteGraphCreate
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_engine.evaluation_state import CombatSnapshot
from tests.evaluation_support import FOE, HERO
from tests.test_stateless_attack import execute
from tests.test_stateless_hold_cast import apply_existing, cast_case
from tests.test_stateless_spell_feature import apply_healing


def haste_case(*, target=FOE, willing=True, immune=False):
    """Build the reviewed caster, source and optional condition-immunity case."""
    request, _, _, loader = cast_case()
    canonical = BundledAssetLoader()
    loader = MemoryAssetLoader(
        items=[loader.get_item(s) for s in loader.list_slugs("items")],
        spells=[loader.get_spell(s) for s in loader.list_slugs("spells")]
        + [canonical.get_spell("haste")],
        classes=[loader.get_class(s) for s in loader.list_slugs("classes")],
        subclasses=[loader.get_subclass(s) for s in loader.list_slugs("subclasses")],
        features=[loader.get_feature(s) for s in loader.list_slugs("features")],
        conditions=[canonical.get_condition("incapacitated"), canonical.get_condition("paralyzed")],
    )
    snapshot = request.state_snapshot
    old = snapshot.casting_state.actors[0].sources[0]
    spells = [
        SpellSelection(
            spell_id="haste" if s == "hold-person" else s,
            prepared_order=old.prepared_spells.index(s) if s in old.prepared_spells else None,
            material=SpellMaterial(inventory_instance_id="pouch:wizard", available=True)
            if s == "hold-person"
            else None,
        )
        for s in old.known_spells
    ]
    cast = CastingStateV20.model_validate(
        {
            "actors": (
                {
                    "actor_id": FOE,
                    "verbal_available": True,
                    "sources": (
                        {
                            "source_id": "class:wizard",
                            "class_slug": "wizard",
                            "ability": "int",
                            "spells": tuple(spells),
                        },
                    ),
                },
            )
        }
    )
    wire = json.loads(request.model_dump_json())
    wire["schema_version"] = SCHEMA_VERSION
    wire["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    wire["payload"] = CombatIntentPayload(
        intent_type="cast_spell",
        spell_id="haste",
        spell_source_id="class:wizard",
        target_id=target,
        willing_target_ids=(target,) if willing else (),
        slot_level=3,
    ).model_dump(mode="json")
    wire_snapshot = wire["state_snapshot"]
    wire_snapshot["snapshot_schema_version"] = "engine-snapshot/20"
    wire_snapshot["casting_state"] = cast.model_dump(mode="json")
    for actor in wire_snapshot["character_states"]:
        actor.pop("spells_known")
        if actor["weapon_in_hands"]:
            instance = f"inventory:held:{actor['entity_id']}"
            actor["held_weapon_instance_id"] = instance
            wire_snapshot["inventory_state"]["entries"].append(
                {
                    "instance_id": instance,
                    "owner_id": actor["entity_id"],
                    "item_slug": actor["weapon_in_hands"],
                    "quantity": 1,
                    "charges_remaining_per_unit": 0,
                    "accessible": True,
                }
            )
        if immune and actor["entity_id"] == target:
            actor["condition_immunities"] = ["incapacitated"]
    return RuleEvaluationRequest.model_validate_json(json.dumps(wire)), loader


def apply_haste(snapshot, result):
    """Apply the typed graph change in the test consumer before the next public call."""
    graph = next(
        (
            o
            for o in result.state_delta.operations
            if isinstance(o, (HasteGraphCreate, EffectFollowUpCreate))
        ),
        None,
    )
    if graph is None:
        return apply_existing(snapshot, result)
    filtered = result.model_copy(
        update={
            "state_delta": result.state_delta.model_copy(
                update={
                    "operations": tuple(o for o in result.state_delta.operations if o is not graph)
                }
            )
        }
    )
    initial = isinstance(graph, HasteGraphCreate)
    after = (apply_healing if initial else apply_existing)(snapshot, filtered)
    state = after.combat_state
    updates = {
        "effect_lifecycles": (*state.effect_lifecycles, graph.lifecycle),
        "conditions_by_effect": (*state.conditions_by_effect, graph.lineage),
    }
    actors = after.character_states
    if initial:
        updates.update(
            concentration_chain={graph.source_id: list(graph.chain)},
            concentration_rounds_remaining={graph.source_id: graph.rounds},
        )
        actors = tuple(
            a.model_copy(update={"concentration_effect_id": graph.concentration_id})
            if a.entity_id == graph.source_id
            else a
            for a in actors
        )
    return CombatSnapshot.model_validate_json(
        after.model_copy(
            update={
                "effect_states": (*after.effect_states, graph.effect),
                "character_states": actors,
                "combat_state": state.model_copy(update=updates),
            }
        ).model_dump_json()
    )


def step(request, loader, *, actor_id=None, drop=False):
    """Exercise public evaluation and round-trip the accepted result, leaving inputs fixed."""
    if actor_id:
        request = request.model_copy(
            update={
                "actor_id": actor_id,
                "payload": CombatIntentPayload(
                    intent_type="drop_concentration" if drop else "pass"
                ),
            }
        )
    original = deepcopy(request)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert request == original
    result = RuleEvaluationResult.model_validate_json(result.model_dump_json())
    assert result.rng_transition.state_changed is False
    return request.model_copy(
        update={"state_snapshot": apply_haste(request.state_snapshot, result)}
    ), result


def current(request):
    state = request.state_snapshot.combat_state
    return state.initiative_ids[state.current_turn_index]


@pytest.mark.parametrize("ending", ["duration", "drop"])
def test_self_haste_follow_up_survives_current_turn_and_expires_at_next_target_end(ending):
    request, loader = haste_case()
    request, cast = step(request, loader)
    [created] = [o for o in cast.state_delta.operations if isinstance(o, HasteGraphCreate)]
    assert created.rounds == 10
    child = created.effect.end_effects[0]
    while current(request) != FOE:
        request, _ = step(request, loader, actor_id=current(request))
    if ending == "duration":
        while request.state_snapshot.effect_states == (created.effect,):
            request, ended = step(request, loader, actor_id=current(request))
    else:
        request, ended = step(request, loader, actor_id=FOE, drop=True)
        request, same_turn = step(request, loader, actor_id=FOE)
        assert not any(
            isinstance(o, EffectLifecycleUpdate) for o in same_turn.state_delta.operations
        )
    removal = next(o for o in ended.state_delta.operations if isinstance(o, EffectLifecycleUpdate))
    follow_up = next(o for o in ended.state_delta.operations if isinstance(o, EffectFollowUpCreate))
    assert removal.effect is None
    assert (
        next(
            o for o in ended.state_delta.operations if isinstance(o, ConcentrationUpdate)
        ).effect_id
        is None
    )
    assert child == follow_up.effect
    assert follow_up.ended_identity == removal.identity
    events = [e.type for e in ended.proposed_events]
    i = events.index("effect_expired")
    assert events[i : i + 3] == ["effect_expired", "effect_applied", "condition_applied"]
    assert request.state_snapshot.effect_states == (child,)
    while current(request) != FOE:
        request, _ = step(request, loader, actor_id=current(request))
        assert request.state_snapshot.effect_states == (child,)
    request, expired = step(request, loader, actor_id=FOE)
    assert not request.state_snapshot.effect_states
    assert not any(isinstance(o, EffectFollowUpCreate) for o in expired.state_delta.operations)
    assert [e.type for e in expired.proposed_events].count("effect_expired") == 1


def test_willing_target_and_immunity_are_resolved_before_and_after_haste():
    rejected, loader = haste_case(willing=False)
    result = execute(rejected, loader)
    assert result.status == "rejected"
    assert result.state_delta is None
    request, loader = haste_case(target=HERO, immune=True)
    request, _ = step(request, loader)
    request, ended = step(request, loader, actor_id=FOE, drop=True)
    follow_up = next(o for o in ended.state_delta.operations if isinstance(o, EffectFollowUpCreate))
    assert follow_up.lineage.statuses == []
    assert follow_up.effect.statuses == {"incapacitated"}
    assert not any(e.type == "condition_applied" for e in ended.proposed_events)
    target = next(a for a in request.state_snapshot.character_states if a.entity_id == HERO)
    assert target.movement_remaining == 0
