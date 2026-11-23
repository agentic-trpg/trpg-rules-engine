"""Build complete Stateless event expectations from bounded Legacy combat cases."""

from collections.abc import Iterable

from dnd5e_engine.events import (
    CombatEvent,
    RollD20Detail,
    RollDetail,
    RollFixedModifier,
    SaveRolled,
    TurnEnded,
    TurnPhase,
)


def expected_repeat_save_events(
    legacy_events: Iterable[CombatEvent],
    *,
    ability_modifier: int,
    proficiency: int,
) -> tuple[CombatEvent, ...]:
    """Add expected single-d20 evidence to the fixture's Legacy turn-end saves."""
    expected = []
    at_turn_end = False
    for event in legacy_events:
        if isinstance(event, TurnPhase):
            at_turn_end = event.phase == "turn_end"
        elif isinstance(event, TurnEnded):
            at_turn_end = False
        elif at_turn_end and isinstance(event, SaveRolled):
            assert event.natural is not None
            event = event.model_copy(
                update={
                    "roll_detail": RollDetail(
                        status="rolled",
                        d20=RollD20Detail(faces=[event.natural], kept_index=0, origin="rng"),
                        modifiers=[
                            RollFixedModifier(source="ability", value=ability_modifier),
                            RollFixedModifier(source="save_proficiency", value=proficiency),
                        ],
                    )
                }
            )
        expected.append(event)
    return tuple(expected)
