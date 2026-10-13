"""Real computation samples plus unchanged independent Legacy/Delta/RNG oracles."""

from copy import deepcopy

import pytest

from dnd5e_engine import evaluation, evaluation_attack
from dnd5e_engine.evaluation_stages import actor_facts, combat_facts
from tests.evaluation_support import FOE, HERO
from tests.test_npc_availability import npc_case
from tests.test_stateless_attack import apply_delta, execute, request_and_live
from tests.test_stateless_shield import shield_case


def capture_attack(monkeypatch, request, loader=None):
    captured = []
    original = evaluation_attack.execute_attack

    def run(*args, **kwargs):
        computed = original(*args, **kwargs)
        captured.append(computed)
        return computed

    monkeypatch.setattr(evaluation, "execute_attack", run)
    result = execute(request, loader)
    assert len(captured) == 1
    return result, captured[0]


def views(snapshot, facts):
    state = snapshot.combat_state
    actors = {
        a.entity_id: actor_facts(
            a, state.actor_zone[a.entity_id], state.movement_ledgers[a.entity_id]
        )
        for a in snapshot.character_states
    }
    combat = combat_facts(state)
    result = []
    for stage in facts.stages:
        actors = {**actors, **{a.actor_id: a for a in stage.actor_updates}}
        combat = stage.combat_update or combat
        result.append((actors, combat))
    return result


def assert_final(snapshot, computed, result):
    facts = computed.stage_facts
    facts.verify_input(snapshot, result.proposed_events)
    final_actors, final_combat = views(snapshot, facts)[-1]
    after = apply_delta(snapshot, result)
    assert after == computed.candidate_state
    assert final_combat == combat_facts(after.combat_state)
    assert final_actors == {
        a.entity_id: actor_facts(
            a,
            after.combat_state.actor_zone[a.entity_id],
            after.combat_state.movement_ledgers[a.entity_id],
        )
        for a in after.character_states
    }


@pytest.mark.parametrize(
    "seed,ac,hp,temp",
    [
        (0, 1, 100, 0),
        (1, 99, 100, 0),
        (5, 1, 100, 2),
        (31, 1, 100, 0),
        (0, 1, 1, 0),
        (0, 1, 100, 100),
    ],
)
def test_real_attack_boundaries_and_final_projection(monkeypatch, seed, ac, hp, temp):
    request, _, _ = request_and_live(seed=seed, ac=ac, hp=hp, temp_hp=temp)
    before = deepcopy(request)
    result, computed = capture_attack(monkeypatch, request)
    facts = computed.stage_facts
    assert result.status == "accepted"
    assert facts.coverage == "complete"
    samples = views(request.state_snapshot, facts)
    declaration = facts.stages[facts.event_references[0].stage_sequences[-1]]
    assert samples[declaration.sequence][0][HERO].budget.action_available
    boundary = next(s for s in facts.stages if s.kind == "attack_execution")
    assert not samples[boundary.sequence][0][HERO].budget.action_available
    roll_index = next(i for i, e in enumerate(result.proposed_events) if e.type == "attack_rolled")
    roll = result.proposed_events[roll_index]
    assert roll.is_crit == (seed == 5)
    assert roll.is_hit == (seed not in {1, 31})
    ref = facts.event_references[roll_index]
    assert ref.stage_sequences[0] == boundary.sequence
    assert samples[ref.stage_sequences[-1]][0][HERO].budget.attack_rolls_made_this_turn == 1
    for index, event in enumerate(result.proposed_events):
        point = facts.stages[facts.event_references[index].stage_sequences[-1]]
        assert point.kind == "event"
        assert point.event_cursor == index + 1
        if event.type == "damage_applied":
            assert samples[point.sequence][0][FOE].hp_current <= hp
            assert samples[point.sequence][0][FOE].temp_hp <= temp
            if hp == 1:
                # The packet is sampled before the nested Death, never from final state.
                assert samples[point.sequence][0][FOE].death.is_alive
                death = facts.event_references[index + 1]
                assert result.proposed_events[index + 1].type == "death"
                assert not samples[death.stage_sequences[-1]][0][FOE].death.is_alive
    assert_final(request.state_snapshot, computed, result)
    assert request == before


@pytest.mark.parametrize("seed", [22, 30, 0])
@pytest.mark.parametrize("immune", [False, True])
def test_statblock_dying_and_immunity_filtered_events_have_exact_references(
    monkeypatch, seed, immune
):
    request, _, live, loader = npc_case(seed=seed, hero_hp=1, hero_max=100)
    if immune:
        from tests.test_evaluation_review_findings import recapture

        live.initiative[0] = live.initiative[0].model_copy(
            update={"condition_immunities": ["unconscious"]}
        )
        request = recapture(request, live)
    result, computed = capture_attack(monkeypatch, request, loader)
    facts = computed.stage_facts
    assert result.status == "accepted"
    assert facts.coverage == "complete"
    assert len(facts.event_references) == len(result.proposed_events)
    assert any(s.kind == "damage_instance_completed" for s in facts.stages)
    assert any(e.type == "death_save_rolled" for e in result.proposed_events)
    if immune:
        assert not any(
            e.type == "condition_applied" and e.condition == "unconscious"
            for e in result.proposed_events
        )
    assert_final(request.state_snapshot, computed, result)


def test_shield_keeps_rule_result_and_explicitly_has_no_stage_coverage(monkeypatch):
    request, _, _, loader = shield_case()
    result, computed = capture_attack(monkeypatch, request, loader)
    assert result.status == "accepted"
    assert computed.stage_facts.coverage == "uncovered"
    assert computed.stage_facts.reason == "complex_effects_or_reactions"
    assert not computed.stage_facts.stages


def test_records_remain_detached_after_subsequent_budget_position_condition_changes(monkeypatch):
    request, _, _ = request_and_live(ac=1)
    result, computed = capture_attack(monkeypatch, request)
    encoded = computed.stage_facts.model_dump_json()
    computed.candidate_state.combat_state.actor_zone[HERO] = "2,2"
    computed.candidate_state.character_states[0].conditions.clear()
    assert computed.stage_facts.model_dump_json() == encoded
    assert result.proposed_events == computed.proposed_events
