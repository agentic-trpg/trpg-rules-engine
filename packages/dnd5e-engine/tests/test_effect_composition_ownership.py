"""R38 full graph closure before read-only family/projector views."""

from copy import deepcopy

import pytest

from dnd5e_engine.evaluation_effect_ownership import (
    effect_family,
    effect_graph_index,
    effect_ownership,
    without_owned_shield,
)
from dnd5e_engine.evaluation_shield import plain_shield
from tests.test_stateless_attack import execute
from tests.test_stateless_hold_turn import hold_case
from tests.test_stateless_shield import apply_shield, shield_case


def mixed_graph():
    hold, _, _, _ = hold_case(source_turn=False)
    request, _, _, loader = shield_case(seed=7)
    shield = apply_shield(request.state_snapshot, execute(request, loader))
    original = hold.state_snapshot
    return original.model_copy(
        update={
            "effect_states": (*original.effect_states, *shield.effect_states),
            "combat_state": original.combat_state.model_copy(
                update={
                    "conditions_by_effect": (
                        *original.combat_state.conditions_by_effect,
                        *shield.combat_state.conditions_by_effect,
                    ),
                    "active_reaction_responses": shield.combat_state.active_reaction_responses,
                    "reaction_effects_pending_expiry": shield.combat_state.reaction_effects_pending_expiry,
                }
            ),
        }
    )


def test_owned_subset_preserves_all_hold_facts_and_input():
    snapshot = mixed_graph()
    original = deepcopy(snapshot)
    ownership = effect_ownership(snapshot)
    assert ownership.hold_source_id is not None
    assert len(ownership.hold_identities) == 1
    assert ownership.shield is not None
    view = without_owned_shield(snapshot, ownership)
    assert view.effect_states == snapshot.effect_states[:-1]
    assert view.combat_state.effect_lifecycles == snapshot.combat_state.effect_lifecycles
    assert view.combat_state.concentration_chain == snapshot.combat_state.concentration_chain
    assert (
        view.combat_state.concentration_rounds_remaining
        == snapshot.combat_state.concentration_rounds_remaining
    )
    assert view.character_states == snapshot.character_states
    assert view.combat_state.conditions_by_effect == snapshot.combat_state.conditions_by_effect[:-1]
    assert snapshot == original
    with pytest.raises(TypeError):
        ownership.index.effects[ownership.hold_identities[0]] = None
    with pytest.raises(ValueError, match="standalone"):
        plain_shield(snapshot)


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate_effect",
        "duplicate_clock",
        "duplicate_link",
        "missing_link",
        "missing_clock",
        "unknown",
        "known_out_of_scope",
        "wrong_chain",
        "missing_rounds",
        "wrong_source",
        "dangling_response",
        "dangling_condition",
        "foreign_owner",
    ],
)
def test_full_ownership_refuses_before_any_subset_view(fault):
    snapshot = mixed_graph()
    state = snapshot.combat_state
    effect = snapshot.effect_states[0]
    if fault == "duplicate_effect":
        snapshot = snapshot.model_copy(update={"effect_states": (*snapshot.effect_states, effect)})
    elif fault in {"unknown", "known_out_of_scope"}:
        app = effect.lifecycle.model_copy(
            update={
                "source_kind": "feature",
                "source_slug": "cunning-strike" if fault == "known_out_of_scope" else "unknown",
            }
        )
        changed = effect.model_copy(update={"lifecycle": app})
        assert effect_family(changed) == fault
        snapshot = snapshot.model_copy(
            update={"effect_states": (changed, *snapshot.effect_states[1:])}
        )
    elif fault == "dangling_condition":
        actors = tuple(
            a.model_copy(
                update={
                    "conditions": [
                        c.model_copy(update={"source_effect_id": "effect:absent"})
                        for c in a.conditions
                    ]
                }
            )
            for a in snapshot.character_states
        )
        snapshot = snapshot.model_copy(update={"character_states": actors})
    else:
        updates = {
            "duplicate_clock": {
                "effect_lifecycles": (*state.effect_lifecycles, state.effect_lifecycles[0])
            },
            "duplicate_link": {
                "conditions_by_effect": (*state.conditions_by_effect, state.conditions_by_effect[0])
            },
            "missing_link": {"conditions_by_effect": state.conditions_by_effect[1:]},
            "missing_clock": {"effect_lifecycles": ()},
            "wrong_chain": {
                "concentration_chain": {
                    "actor:absent": list(effect_ownership(snapshot).hold_identities)
                }
            },
            "missing_rounds": {"concentration_rounds_remaining": {}},
            "wrong_source": {"concentration_chain": {}},
            "dangling_response": {"reaction_effects_pending_expiry": {}},
            "foreign_owner": {
                "active_reaction_responses": [
                    state.active_reaction_responses[0].__class__(
                        "actor:absent",
                        state.active_reaction_responses[0].effect_identity,
                        state.active_reaction_responses[0].conditions,
                        state.active_reaction_responses[0].responses,
                    )
                ]
            },
        }
        snapshot = snapshot.model_copy(
            update={"combat_state": state.model_copy(update=updates[fault])}
        )
    with pytest.raises(ValueError):
        effect_ownership(snapshot)


def test_empty_index_does_not_claim_other_mechanics_are_supported():
    request, _, _, _ = shield_case()
    graph = effect_graph_index(request.state_snapshot)
    assert not graph.effects
    assert not graph.clocks
    assert not graph.links
    # A pending declaration is still present and must undergo canonical policy,
    # source, resource and operation-boundary admission by the actual evaluator.
    assert request.state_snapshot.combat_state.pending_reactions
