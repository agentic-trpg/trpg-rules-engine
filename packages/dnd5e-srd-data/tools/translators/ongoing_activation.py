"""Exact SRD 5.2.1 ongoing-source mapping; never executed from spell prose."""

from dnd5e_srd_data.schema.common import (
    ActivityTiming,
    AreaRelocationSpec,
    OngoingActivationSpec,
    PersistentAreaSpec,
)
from dnd5e_srd_data.schema.environment import EnvironmentalSpec
from dnd5e_srd_data.schema.lifecycle import EffectLifecycleSpec
from dnd5e_srd_data.schema.spell import Spell

INITIAL = "o3kffaumfNGjVvg9"
REPEAT = "Y0cJvZuD7EfwGqkf"
EFFECT = "RZmnRyi6pyjIRN1i"


def apply_ongoing_activation(spell: Spell) -> Spell:
    if spell.slug == "moonbeam":
        return _moonbeam(spell)
    if spell.slug != "sunbeam":
        return spell
    if (
        (
            spell.level,
            spell.range.units,
            spell.concentration,
            spell.duration.units,
            spell.duration.value,
            spell.casting_time.unit.value,
        )
        != (6, "self", True, "minute", 1, "action")
        or [a.id for a in spell.activities] != [INITIAL, REPEAT]
        or len(spell.passive_effects) != 1
        or spell.passive_effects[0].id != EFFECT
        or spell.passive_effects[0].statuses != ["blinded"]
        or spell.passive_effects[0].changes
        or not all(
            clause in spell.description
            for clause in (
                "start of your next turn",
                "Magic action to create a new Line",
                "30-foot radius",
                "additional 30 feet",
                "This light is sunlight",
            )
        )
    ):
        raise ValueError("ongoing source drift: sunbeam")
    activities = []
    for index, activity in enumerate(spell.activities):
        template = activity.target.template
        if (
            activity.kind != "save"
            or (template.type, template.size, template.width, template.units)
            != ("line", "60", "5", "ft")
            or activity.target.affects.type != "creature"
            or activity.target.affects.choice
            or activity.target.affects.count
            or activity.save.ability != ["con"]
            or activity.save.dc.calculation != "spellcasting"
            or activity.damage.on_save != "half"
            or len(activity.damage.parts) != 1
            or (
                activity.damage.parts[0].number,
                activity.damage.parts[0].denomination,
                activity.damage.parts[0].types,
                activity.damage.parts[0].scaling.mode,
                activity.damage.parts[0].scaling.number,
                activity.damage.parts[0].scaling.formula,
                activity.damage.parts[0].bonus,
                activity.damage.parts[0].custom.enabled,
            )
            != (6, 8, ["radiant"], "", 1, "", "", False)
            or [ref.id for ref in activity.effects] != [EFFECT]
            or activity.effects[0].on_save is True
            or activity.consumption.spell_slot != (index == 0)
        ):
            raise ValueError("ongoing source drift: sunbeam activity")
        refs = [
            ref.model_copy(
                update={"lifecycle": EffectLifecycleSpec(expiry_boundary="source_next_turn_start")}
            )
            for ref in activity.effects
        ]
        # Foundry's inactive scaling block carries a display default of 1.
        # The engine's dice contract treats that as an upcast increment, so
        # encode the reviewed absence of an upcast clause explicitly as zero.
        part = activity.damage.parts[0]
        damage = activity.damage.model_copy(
            update={
                "parts": [
                    part.model_copy(
                        update={"scaling": part.scaling.model_copy(update={"number": 0})}
                    )
                ]
            }
        )
        source = (
            PersistentAreaSpec(
                placement="follow-source",
                triggers=(),
                source_radius_ft=30,
                environment=EnvironmentalSpec(
                    kind="light",
                    light="bright",
                    sunlight=True,
                    sunlight_in_dim=True,
                    dim_extension_ft=30,
                ),
                ongoing_activation=OngoingActivationSpec(activity_ids=(REPEAT,)),
            )
            if index == 0
            else None
        )
        activities.append(
            activity.model_copy(
                update={
                    "effects": refs,
                    "damage": damage,
                    "persistent_area": source,
                    "timing": ActivityTiming(effects_concentration=False),
                }
            )
        )
    return spell.model_copy(update={"activities": activities})


def _moonbeam(spell: Spell) -> Spell:
    """SRD 5.2.1 pp150-151: reviewed missing Foundry height and active area."""
    if len(spell.activities) != 1:
        raise ValueError("ongoing source drift: moonbeam")
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
        != (2, "ft", 120, True, "minute", 1, "action", 1)
        or activity.id != "dnd5eactivity000"
        or activity.kind != "save"
        or (template.type, template.size, template.height, template.units)
        != ("cylinder", "5", "", "ft")
        or activity.save.ability != ["con"]
        or activity.save.dc.calculation != "spellcasting"
        or activity.save.dc.formula
        or activity.damage.on_save != "half"
        or len(activity.damage.parts) != 1
        or activity.effects
        or spell.passive_effects
        or activity.target.affects.type
        or activity.target.affects.choice
        or activity.target.affects.count
        or activity.range.override
        or activity.activation.override
        or not activity.consumption.spell_slot
        or not all(
            clause in spell.description
            for clause in (
                "5-foot-radius, 40-foot-high Cylinder",
                "Dim Light fills the Cylinder",
                "Magic action on later turns to move the Cylinder up to 60 feet",
                "When the Cylinder appears",
                "On a failed save, a creature takes 2d10 Radiant damage, "
                "and if the creature is shape-shifted",
                "On a successful save, a creature takes half as much damage only.",
                "reverts to its true form",
                "can't shape-shift until it leaves the Cylinder",
                "area moves into its space",
                "enters the spell's area or ends its turn there",
                "only once per turn",
                "damage increases by 1d10 for each spell slot level above 2",
            )
        )
    ):
        raise ValueError("ongoing source drift: moonbeam")
    part = activity.damage.parts[0]
    if (
        part.number,
        part.denomination,
        part.types,
        part.scaling.mode,
        part.scaling.number,
        part.scaling.formula,
        part.bonus,
        part.custom.enabled,
    ) != (2, 10, ["radiant"], "whole", 1, "", "", False):
        raise ValueError("ongoing source drift: moonbeam damage")
    return spell.model_copy(
        update={
            "activities": [
                activity.model_copy(
                    update={
                        "target": activity.target.model_copy(
                            update={
                                "template": template.model_copy(
                                    update={"height": "40", "stationary": True}
                                )
                            }
                        ),
                        "timing": ActivityTiming(effects_concentration=False),
                        "persistent_area": PersistentAreaSpec(
                            triggers=(
                                "appearance",
                                "area-enters-creature",
                                "enter",
                                "turn-end-inside",
                            ),
                            revert_shape_on_failed_save=True,
                            nonstacking_same_spell=True,
                            environment=EnvironmentalSpec(kind="light", light="dim"),
                            ongoing_activation=OngoingActivationSpec(
                                activity_ids=(activity.id,),
                                relocation=AreaRelocationSpec(max_distance_ft=60),
                            ),
                        ),
                    }
                )
            ]
        }
    )
