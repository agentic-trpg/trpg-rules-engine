"""Bound Burning across the public stateless Pass and closure evaluators."""

from copy import deepcopy
from random import Random

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader

from dnd5e_engine.activities.effects import passive_effect_to_active_effect
from dnd5e_engine.evaluation_contracts import (
    CombatClosePayload,
    CombatIntentPayload,
    RuleEvaluationResult,
)
from dnd5e_engine.evaluation_delta import CombatClose, EffectLifecycleUpdate
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_engine.events import DamageApplied, DeathSaveRolled, EffectExpired, TurnPhase
from tests.evaluation_support import FOE, HERO, WEAPON, synthetic_loader
from tests.test_stateless_attack import apply_delta, execute
from tests.test_stateless_haste import apply_haste, haste_case, step
from tests.test_stateless_turn import turn_case


def burning_case(*, hp=1, dying=False, target=HERO, source=FOE, hp_temp=0):
    request, _, _ = turn_case(npc=target == HERO, dying=dying, seed=0)
    item = BundledAssetLoader().get_item("alchemists-fire")
    loader = MemoryAssetLoader(items=[synthetic_loader().get_item(WEAPON), item])
    return with_burning(request, loader, source, target, hp, hp_temp), loader


def with_burning(request, loader, source, target, hp, hp_temp=0):
    snapshot = request.state_snapshot
    definition = loader.get_item("alchemists-fire").passive_effects[0]
    application = passive_effect_to_active_effect(definition, target_id=target, caster_id=source)
    burning = effect_state(
        application.model_copy(update={"origin": f"item:alchemists-fire:{source}"})
    )
    actors = tuple(
        actor.model_copy(update={"hp_current": hp, "temp_hp": hp_temp})
        if actor.entity_id == target
        else actor
        for actor in snapshot.character_states
    )
    return request.model_copy(
        update={
            "ruleset_binding": ruleset_binding(loader),
            "state_snapshot": snapshot.model_copy(
                update={
                    "character_states": actors,
                    "effect_states": (*snapshot.effect_states, burning),
                }
            ),
        }
    )


def phases(result):
    return [
        (e.phase, e.actor_id, e.round_number)
        for e in result.proposed_events
        if isinstance(e, TurnPhase)
    ]


def test_burning_knockout_skips_the_turn_after_ending_it_without_a_death_save():
    request, loader = burning_case()
    original = deepcopy(request)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert result == execute(request, loader)
    assert request == original
    assert result == RuleEvaluationResult.model_validate_json(result.model_dump_json())
    assert phases(result) == [
        ("turn_end", FOE, 1),
        ("round_start", None, 2),
        ("turn_start", HERO, 2),
        ("turn_end", HERO, 2),
        ("turn_start", FOE, 2),
    ]
    assert not any(isinstance(e, DeathSaveRolled) for e in result.proposed_events)
    assert [e.type for e in result.proposed_events] == [
        "intent_submitted",
        "turn_phase",
        "turn_ended",
        "round_started",
        "turn_phase",
        "turn_started",
        "turn_phase",
        "damage_applied",
        "unconscious",
        "condition_applied",
        "turn_phase",
        "turn_ended",
        "turn_started",
        "turn_phase",
    ]
    [damage] = [e for e in result.proposed_events if isinstance(e, DamageApplied)]
    assert (damage.source_id, damage.source_actor_id, damage.damage_instance_id) == (
        "item:alchemists-fire",
        FOE,
        f"damage:1:{FOE}:{HERO}",
    )
    rng = Random(0)
    assert damage.amount == rng.randint(1, 4)
    assert result.rng_transition.next_state == RNGState.capture(rng)
    after = apply_delta(request.state_snapshot, result)
    hero = next(a for a in after.character_states if a.entity_id == HERO)
    assert hero.hp_current == hero.movement_remaining == 0
    assert hero.is_alive
    assert [c.condition for c in hero.conditions] == ["unconscious"]
    assert after.combat_state.turn_serial == request.state_snapshot.combat_state.turn_serial + 2
    assert after.combat_state.last_ended_turn == (2, HERO)
    assert after.effect_states == request.state_snapshot.effect_states


def test_temporary_hp_absorbs_fire_and_handoff_remains_with_the_entrant():
    request, loader = burning_case(hp_temp=4)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert phases(result)[-1] == ("turn_start", HERO, 2)
    assert len(phases(result)) == 3
    after = apply_delta(request.state_snapshot, result)
    hero = next(a for a in after.character_states if a.entity_id == HERO)
    assert (hero.hp_current, hero.temp_hp, hero.conditions) == (1, 0, [])


def test_turn_start_death_can_use_normal_terminal_closure_without_another_handoff():
    request, loader = burning_case(target=FOE, source=HERO)
    result = execute(request, loader)
    assert result.status == "accepted", result.error
    assert phases(result) == [("turn_end", HERO, 1), ("turn_start", FOE, 1)]
    after = apply_delta(request.state_snapshot, result)
    assert after.combat_state.dead_ids == {FOE}
    close = request.model_copy(
        update={
            "operation_kind": "combat.close",
            "payload": CombatClosePayload(kind="combat.close"),
            "state_snapshot": after,
            "rng_context": request.rng_context.model_copy(
                update={"state": result.rng_transition.next_state}
            ),
        }
    )
    terminal = execute(close, loader)
    assert terminal.status == "accepted", terminal.error
    [outcome] = [op for op in terminal.state_delta.operations if isinstance(op, CombatClose)]
    assert outcome.reason == "victory"
    assert [(d.target_id, d.killer_id) for d in outcome.historical.deaths] == [(FOE, HERO)]


@pytest.mark.parametrize(
    "mutation", ["conflicting_save", "bad_origin", "altered_effect", "multiple"]
)
def test_burning_refuses_unsettled_order_or_unverified_facts_without_results(mutation):
    dying = mutation == "conflicting_save"
    request, loader = burning_case(dying=dying, hp=0 if dying else 1)
    snapshot = request.state_snapshot
    [effect] = snapshot.effect_states
    effects = {
        "conflicting_save": (effect,),
        "bad_origin": (effect.model_copy(update={"origin": "environment"}),),
        "altered_effect": (effect.model_copy(update={"disabled": True}),),
        "multiple": (effect, effect.model_copy(update={"origin": f"item:alchemists-fire:{HERO}"})),
    }[mutation]
    request = request.model_copy(
        update={"state_snapshot": snapshot.model_copy(update={"effect_states": effects})}
    )
    result = execute(request, loader)
    assert result.status == "unsupported", result.error
    assert result.error.code == ("turn.start_order" if dying else "turn.burning")
    assert result.proposed_events == ()
    assert result.state_delta is result.rng_transition is None


@pytest.mark.parametrize("target", [HERO, FOE])
def test_burning_with_lethargy_uses_turn_end_or_refuses_unrepresentable_closure(target):
    request, base_loader = haste_case(target=target)
    loader = MemoryAssetLoader(
        items=[base_loader.get_item(s) for s in base_loader.list_slugs("items")]
        + [BundledAssetLoader().get_item("alchemists-fire")],
        spells=[base_loader.get_spell(s) for s in base_loader.list_slugs("spells")],
        classes=[base_loader.get_class(s) for s in base_loader.list_slugs("classes")],
        subclasses=[base_loader.get_subclass(s) for s in base_loader.list_slugs("subclasses")],
        features=[base_loader.get_feature(s) for s in base_loader.list_slugs("features")],
        conditions=[base_loader.get_condition(s) for s in base_loader.list_slugs("conditions")],
    )
    request = request.model_copy(update={"ruleset_binding": ruleset_binding(loader)})
    request, _ = step(request, loader)
    request, _ = step(request, loader, actor_id=FOE, drop=True)
    state = request.state_snapshot.combat_state
    index = state.initiative_ids.index(target)
    outgoing = state.initiative_ids[index - 1]
    while state.initiative_ids[state.current_turn_index] != outgoing:
        request, _ = step(request, loader, actor_id=state.initiative_ids[state.current_turn_index])
        state = request.state_snapshot.combat_state
    request = with_burning(request, loader, outgoing, target, hp=1).model_copy(
        update={"actor_id": outgoing, "payload": CombatIntentPayload(intent_type="pass")}
    )
    result = execute(request, loader)
    if target == FOE:
        assert result.status == "unsupported", result.error
        assert result.error.code == "turn.burning_closure"
        assert result.proposed_events == ()
        assert result.state_delta is result.rng_transition is None
        return
    assert result.status == "accepted", result.error
    events = result.proposed_events
    [expired] = [e for e in events if isinstance(e, EffectExpired)]
    index = events.index(expired)
    assert events[index - 1] == TurnPhase(actor_id=HERO, phase="turn_end", round_number=2)
    assert [e.type for e in events[index + 1 : index + 4]] == [
        "condition_removed",
        "turn_ended",
        "turn_started",
    ]
    [removal] = [
        op for op in result.state_delta.operations if isinstance(op, EffectLifecycleUpdate)
    ]
    assert removal.expected_effect.id == expired.effect_id
    assert removal.effect is removal.lifecycle is removal.lineage is None
    after = apply_haste(request.state_snapshot, result)
    [retained] = after.effect_states
    assert retained.origin == f"item:alchemists-fire:{outgoing}"
    hero = next(a for a in after.character_states if a.entity_id == HERO)
    assert [c.condition for c in hero.conditions] == ["unconscious"]
