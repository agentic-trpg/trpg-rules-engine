"""Bardic Inspiration continuation on published check and RNG evidence."""

from copy import deepcopy

import pytest

from dnd5e_engine.evaluation_bardic_check import (
    BARDIC_INSPIRATION_SOURCE,
    check_bardic_fingerprint,
    resolve_bardic_check_choice,
)
from dnd5e_engine.evaluation_effects import EffectChangeState, effect_state
from dnd5e_engine.evaluation_rng import RNGContext, RNGState
from dnd5e_engine.evaluation_state import ConditionRecord
from dnd5e_engine.events import RollDieModifier
from dnd5e_engine.types.conditions import ActiveCondition
from dnd5e_engine.types.effects import ActiveEffect
from tests.evaluation_support import HERO, synthetic_loader
from tests.test_stateless_attack import execute
from tests.test_stateless_checks import check_case


@pytest.mark.parametrize("die_sides", [6, 8, 10, 12])
@pytest.mark.parametrize("dc", [9, 99])
def test_bardic_uses_current_rng_and_keeps_original_rolled_check(die_sides, dc):
    result = execute(check_case(seed=1, dc=dc, disadvantage=("flag",)))
    (original,) = result.proposed_events
    before = deepcopy(original)
    assert not original.succeeded
    rng = result.rng_transition.next_state.restore()
    rng.randint(1, 20)  # Another operation already advanced the host's stream.
    context = RNGContext(stream_id="main", version=5, state=RNGState.capture(rng))
    face = rng.randint(1, die_sides)

    event, transition = resolve_bardic_check_choice(original, die_sides, context, use=True)
    assert original == before
    assert event.roll_detail.d20 == original.roll_detail.d20
    assert event.roll_detail.modifiers[:-1] == original.roll_detail.modifiers
    assert event.roll_detail.modifiers[-1] == RollDieModifier(
        source=BARDIC_INSPIRATION_SOURCE, sides=die_sides, face=face, value=face
    )
    assert event.modifier == original.modifier
    assert event.inspiration_bonus == face
    assert event.roll_total == original.roll_total + face
    assert event.succeeded is (dc < 99)
    transition.verify_input(context)
    assert transition.next_state == RNGState.capture(rng)
    with pytest.raises(ValueError, match="unmodified failed"):
        resolve_bardic_check_choice(event, die_sides, context, use=True)


def test_bardic_decline_and_invalid_choices_never_restore_rng(monkeypatch):
    request = check_case(dc=99)
    result = execute(request)
    (original,) = result.proposed_events
    context = request.rng_context

    def fail_restore(_state):
        pytest.fail("RNG restored before validating the choice")

    monkeypatch.setattr(RNGState, "restore", fail_restore)
    event, transition = resolve_bardic_check_choice(original, 6, context, use=False)
    assert event == original
    assert event.roll_detail is not original.roll_detail
    transition.verify_input(context)
    assert not transition.state_changed
    assert transition.next_state == context.state
    with pytest.raises(ValueError, match="unsupported Bardic Inspiration die"):
        resolve_bardic_check_choice(original, 4, context, use=True)
    for changes in (
        {"dc": original.roll_total, "succeeded": True},
        {"auto_failure": "sight"},
        {"roll_detail": None},
        {"target_id": "named target"},
    ):
        with pytest.raises(ValueError, match="unmodified failed"):
            resolve_bardic_check_choice(original.model_copy(update=changes), 6, context, use=True)


def test_bardic_fingerprint_reads_check_inputs_without_rng(monkeypatch):
    request = check_case()
    loader = synthetic_loader()
    snapshot = request.state_snapshot
    actor, other = snapshot.character_states

    def fingerprint_update(*, roller=actor, effects=()):
        """Rebuild the request with current facts and incidental host changes."""
        current = snapshot.model_copy(
            update={
                "world_version": snapshot.world_version + 1,
                "character_states": (roller, other.model_copy(update={"strength": 18})),
                "effect_states": effects,
            }
        )
        return check_bardic_fingerprint(
            request.model_copy(
                update={
                    "command_id": "new-command",
                    "state_snapshot": current,
                    "rng_context": request.rng_context.model_copy(update={"version": 8}),
                }
            ),
            loader,
        )

    def fail_restore(_state):
        pytest.fail("check revalidation touched RNG")

    monkeypatch.setattr(RNGState, "restore", fail_restore)
    original = check_bardic_fingerprint(request, loader)
    assert fingerprint_update(roller=actor.model_copy(update={"dexterity": 20})) == original
    assert fingerprint_update(roller=actor.model_copy(update={"strength": 7})) != original
    poison = ConditionRecord.model_validate(
        ActiveCondition(
            condition="poisoned", source_entity_id="trap:012345abcdef", scope="session"
        ).model_dump()
    )
    assert fingerprint_update(roller=actor.model_copy(update={"conditions": [poison]})) != original
    effect = effect_state(
        ActiveEffect(
            id="training", name="Training", origin="feature:training", target_id=HERO, changes=[]
        )
    )
    for key, same in (("check.dex.bonus", True), ("check.str.bonus", False)):
        change = EffectChangeState(key=key, mode="add", value="1d4", priority=20)
        modified = effect.model_copy(update={"changes": (change,)})
        assert (fingerprint_update(effects=(modified,)) == original) is same
    with pytest.raises(ValueError, match="explicit untargeted"):
        check_bardic_fingerprint(
            request.model_copy(update={"payload": request.payload.model_copy(update={"dc": None})}),
            loader,
        )
