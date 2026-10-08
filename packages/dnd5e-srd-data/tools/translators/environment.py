"""Pinned SRD 5.2.1 point modes. Runtime consumes typed metadata only."""

from dnd5e_srd_data.schema.common import PersistentAreaSpec
from dnd5e_srd_data.schema.environment import EnvironmentalSpec
from dnd5e_srd_data.schema.spell import Spell

_REVIEWED: dict[str, tuple[int, int, bool, str, int, str, str, EnvironmentalSpec]] = {
    "fog-cloud": (
        1,
        120,
        True,
        "hour",
        1,
        "20 * @item.level",
        "20",
        EnvironmentalSpec(
            kind="fog",
            obscurement="heavy",
            radius_increase_per_slot_ft=20,
            strong_wind_dispersal=True,
        ),
    ),
    "darkness": (
        2,
        60,
        True,
        "minute",
        10,
        "15",
        "15",
        EnvironmentalSpec(kind="magical_darkness", light="dark", dispels_light_through_level=2),
    ),
    "daylight": (
        3,
        60,
        False,
        "hour",
        1,
        "60",
        "60",
        EnvironmentalSpec(
            kind="light",
            light="bright",
            sunlight=True,
            dim_extension_ft=60,
            dispels_darkness_through_level=3,
        ),
    ),
}

_CLAUSES = {
    "fog-cloud": (
        "until a strong wind",
        "radius increases by 20 feet for each spell slot level above 1",
    ),
    "darkness": (
        "Darkvision can't see through it",
        "nonmagical light can't illuminate it",
        "spell of level 2 or lower",
        "object that isn't being worn or carried",
    ),
    "daylight": (
        "sunlight spreads from a point",
        "Dim Light for an additional 60 feet",
        "spell of level 3 or lower",
        "object that isn't being worn or carried",
    ),
}


def apply_environment(spell: Spell) -> Spell:
    reviewed = _REVIEWED.get(spell.slug)
    if reviewed is None:
        return spell
    level, range_ft, concentration, units, duration, raw_size, size, spec = reviewed
    if len(spell.activities) != 1:
        raise ValueError(f"environment source drift: {spell.slug}")
    activity = spell.activities[0]
    template = activity.target.template
    if (
        (
            spell.level,
            spell.range.units,
            spell.range.value,
            spell.concentration,
            spell.duration.units,
            spell.duration.value,
            spell.casting_time.unit.value,
            spell.casting_time.value,
        )
        != (level, "ft", range_ft, concentration, units, duration, "action", 1)
        or activity.kind != "utility"
        or activity.id != "dnd5eactivity000"
        or activity.effects
        or spell.passive_effects
        or activity.roll.formula
        or (template.type, template.size, template.units) != ("sphere", raw_size, "ft")
        or activity.target.affects.type
        or activity.target.affects.choice
        or activity.target.affects.count
        or activity.activation.override
        or activity.range.override
        or not all(clause in spell.description for clause in _CLAUSES[spell.slug])
    ):
        raise ValueError(f"environment source drift: {spell.slug}")
    target = activity.target.model_copy(
        update={
            "template": template.model_copy(update={"size": size, "stationary": True}),
        }
    )
    return spell.model_copy(
        update={
            "activities": [
                activity.model_copy(
                    update={
                        "target": target,
                        "persistent_area": PersistentAreaSpec(triggers=(), environment=spec),
                    }
                )
            ]
        }
    )
