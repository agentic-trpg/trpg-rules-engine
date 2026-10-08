"""Source-reviewed action policy bindings, applied only during ingestion."""

from dnd5e_srd_data.schema.action_policy import ActionPolicy
from dnd5e_srd_data.schema.lifecycle import EffectLifecycleSpec, RepeatSaveSpec
from dnd5e_srd_data.schema.spell import Spell


def apply_action_policy(spell: Spell) -> Spell:
    if spell.slug != "slow":
        return spell
    if len(spell.activities) != 1 or len(spell.passive_effects) != 1:
        raise ValueError("action policy source drift: slow")
    activity = spell.activities[0]
    effect = spell.passive_effects[0]
    expected = {
        ("system.attributes.ac.bonus", 2, "-2"),
        ("system.abilities.dex.bonuses.save", 2, "-2"),
        *(
            (f"system.attributes.movement.{mode}", 1, "0.5")
            for mode in ("walk", "swim", "fly", "climb", "burrow")
        ),
    }
    if (
        spell.foundry_uuid != "Compendium.dnd5e.spells24.Item.phbsplSlow000000"
        or len(spell.activities) != 1
        or len(spell.passive_effects) != 1
        or (
            spell.level,
            spell.range.value,
            spell.concentration,
            spell.duration.units,
            spell.duration.value,
        )
        != (3, 120, True, "minute", 1)
        or (
            activity.id,
            activity.kind,
            activity.save.ability,
            activity.target.template.type,
            activity.target.template.size,
            activity.target.affects.count,
            activity.target.affects.choice,
        )
        != ("dnd5eactivity000", "save", ["wis"], "cube", "40", "6", True)
        or activity.damage.parts
        or spell.casting_time.unit.value != "action"
        or spell.range.units != "ft"
        or {str(c) for c in spell.components} != {"V", "S", "M"}
        or activity.save.dc.calculation != "spellcasting"
        or activity.save.dc.formula
        or not activity.consumption.spell_slot
        or [(r.id, r.on_save) for r in activity.effects] != [("z1lkguBohjqXRSHm", False)]
        or effect.duration != {"seconds": 60}
        or effect.id != "z1lkguBohjqXRSHm"
        or effect.statuses
        or {(c.key, c.mode, c.value) for c in effect.changes} != expected
        or len(effect.changes) != len(expected)
        or not all(
            c in spell.description
            for c in (
                "can't take Reactions",
                "either an action or a Bonus Action",
                "only one attack",
                "Somatic component",
                "25 percent",
                "repeats the save at the end",
            )
        )
    ):
        raise ValueError("action policy source drift: slow")
    effect = effect.model_copy(
        update={
            "action_policy": ActionPolicy(
                action_or_bonus=True,
                deny_reactions=True,
                attack_count_cap=1,
                somatic_failure_percent=25,
            )
        }
    )
    refs = [
        r.model_copy(
            update={
                "lifecycle": EffectLifecycleSpec(
                    repeat_save=RepeatSaveSpec(), stacking="latest_applies", stacking_group="slow"
                )
            }
        )
        for r in activity.effects
    ]
    activity = activity.model_copy(update={"effects": refs})
    return spell.model_copy(update={"activities": [activity], "passive_effects": [effect]})
