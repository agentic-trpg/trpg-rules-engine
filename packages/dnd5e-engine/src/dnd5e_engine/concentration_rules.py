"""Shared concentration anchors, duration, and saves over completed damage."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from dnd5e_srd_data.schema.spell import Spell, SpellDurationUnits

from dnd5e_engine.damage_rules import concentration_dc
from dnd5e_engine.events import ConcentrationCheck
from dnd5e_engine.types.effects import ActiveEffect

if TYPE_CHECKING:
    from dnd5e_engine.activities.save_primitive import SaveRoll


def roll_damage_concentration(
    target_id: str, amount: int, constitution_save: Callable[[int], SaveRoll]
) -> tuple[ConcentrationCheck, int | None]:
    """Roll once for a positive, completed damage packet and return evidence and LR usage.

    The caller supplies the admitted Constitution-save mechanics. The amount includes
    damage absorbed by temporary HP and is measured after damage defenses.
    """
    dc = concentration_dc(amount)
    roll = constitution_save(dc)
    return (
        ConcentrationCheck(
            target_id=target_id,
            dc=dc,
            roll_total=roll.total,
            succeeded=roll.succeeded,
            advantage=roll.mode,
            natural=roll.natural,
            modifier=roll.modifier,
            sources=list(roll.sources),
        ),
        roll.legendary_resistance_remaining,
    )


def concentration_anchor(spell: Spell, caster_id: str) -> ActiveEffect:
    """Construct the source effect anchoring a concentration spell."""
    return ActiveEffect(
        id=f"effect:{spell.slug}",
        name=spell.name,
        origin=f"cast:{spell.slug}:{caster_id}",
        target_id=caster_id,
        flags={"concentration": True, "concentration_anchor": True},
    )


def concentration_max_rounds(spell: Spell | None) -> int | None:
    """Derive the supported concentration duration in rounds."""
    if spell is None or not spell.concentration or spell.duration.value is None:
        return None
    per_unit = {
        SpellDurationUnits.ROUND: 1,
        SpellDurationUnits.MINUTE: 10,
        SpellDurationUnits.HOUR: 600,
        SpellDurationUnits.DAY: 14400,
    }.get(spell.duration.units)
    return spell.duration.value * per_unit if per_unit is not None else None
