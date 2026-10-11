"""Shared original concentration anchor and metric duration; no runtime ownership."""

from dnd5e_srd_data.schema.spell import Spell, SpellDurationUnits

from dnd5e_engine.types.effects import ActiveEffect


def concentration_anchor(spell: Spell, caster_id: str) -> ActiveEffect:
    return ActiveEffect(
        id=f"effect:{spell.slug}",
        name=spell.name,
        origin=f"cast:{spell.slug}:{caster_id}",
        target_id=caster_id,
        flags={"concentration": True, "concentration_anchor": True},
    )


def concentration_max_rounds(spell: Spell | None) -> int | None:
    if spell is None or not spell.concentration or spell.duration.value is None:
        return None
    per_unit = {
        SpellDurationUnits.ROUND: 1,
        SpellDurationUnits.MINUTE: 10,
        SpellDurationUnits.HOUR: 600,
        SpellDurationUnits.DAY: 14400,
    }.get(spell.duration.units)
    return spell.duration.value * per_unit if per_unit is not None else None
