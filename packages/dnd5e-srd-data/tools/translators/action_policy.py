"""Source-reviewed action policy bindings, applied only during ingestion."""

import hashlib
import json

from dnd5e_srd_data.schema.action_policy import ActionPolicy, RestrictedActionGrant
from dnd5e_srd_data.schema.common import Activity
from dnd5e_srd_data.schema.lifecycle import EffectEndFollowUp, EffectLifecycleSpec, RepeatSaveSpec
from dnd5e_srd_data.schema.spell import Spell


def apply_action_policy(spell: Spell) -> Spell:
    if spell.slug == "haste":
        return _haste(spell)
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


def _haste(spell: Spell) -> Spell:
    """SRD 5.2.1 p. 139; pinned Foundry templates supply the exact deltas."""
    if len(spell.activities) != 2 or len(spell.passive_effects) != 2:
        raise ValueError("action policy source drift: haste")
    # Bind every source field, including nonnumeric targeting and timing, to
    # the reviewed snapshot. Strip only annotations produced by this function.
    payload = spell.model_dump(mode="json", exclude={"provenance"})
    for activity in payload["activities"]:
        activity.pop("action_type", None)
        activity["target"].pop("requires_sight", None)
        activity["target"].pop("requires_willing", None)
        for ref in activity["effects"]:
            ref.pop("lifecycle", None)
    for effect in payload["passive_effects"]:
        effect.pop("action_policy", None)
    source_digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if source_digest != "1adf5f2ce0a7c02e55df28dc8765c97a67370f9847f1b71ed0aaf48413e61e0a":
        raise ValueError("action policy source drift: haste")
    cast, end = spell.activities
    buff, penalty = spell.passive_effects
    modes = ("walk", "swim", "fly", "climb", "burrow")
    if (
        spell.foundry_uuid != "Compendium.dnd5e.spells24.Item.phbsplHaste00000"
        or (
            spell.level,
            spell.casting_time.unit.value,
            spell.range.units,
            spell.range.value,
            spell.duration.units,
            spell.duration.value,
            spell.concentration,
        )
        != (3, "action", "ft", 30, "minute", 1, True)
        or {str(c) for c in spell.components} != {"V", "S", "M"}
        or (cast.id, cast.kind, cast.target.affects.type, cast.target.affects.count)
        != ("dnd5eactivity000", "utility", "willing", "1")
        or [(r.id, r.on_save) for r in cast.effects] != [("NEFWcyysYgsE6de3", None)]
        or not cast.consumption.spell_slot
        or cast.activation.override
        or cast.range.override
        or (end.id, end.kind, end.activation.type) != ("rBQrZnq7cHkSUAK9", "utility", "")
        or [r.id for r in end.effects] != ["S5XcFawnnNHO8bUr"]
        or (buff.id, buff.duration, buff.statuses, buff.disabled)
        != ("NEFWcyysYgsE6de3", {"seconds": 60}, [], False)
        or {(c.key, c.mode, c.value) for c in buff.changes}
        != {
            *((f"system.attributes.movement.{m}", 1, "2") for m in modes),
            ("system.attributes.ac.bonus", 2, "2"),
            ("system.abilities.dex.save.roll.mode", 2, "1"),
        }
        or len(buff.changes) != 7
        or (penalty.id, penalty.statuses, penalty.duration, penalty.disabled)
        != ("S5XcFawnnNHO8bUr", ["incapacitated"], {"rounds": 1}, False)
        or {(c.key, c.mode, c.value) for c in penalty.changes}
        != {(f"system.attributes.movement.{m}", 3, "0") for m in modes}
        or len(penalty.changes) != 5
        or not all(
            clause in spell.description
            for clause in (
                "willing creature that you can see",
                "Speed is doubled",
                "+2 bonus to Armor Class",
                "Advantage on Dexterity",
                "one attack only",
                "Dash, Disengage, Hide, or Utilize",
                "When the spell ends",
                "Incapacitated",
                "Speed of 0 until the end of its next turn",
            )
        )
    ):
        raise ValueError("action policy source drift: haste")
    buff = buff.model_copy(
        update={
            "action_policy": ActionPolicy(
                extra_action=RestrictedActionGrant(
                    actions=("attack", "dash", "disengage", "hide", "utilize")
                )
            )
        }
    )
    cast = cast.model_copy(
        update={
            "action_type": "magic",
            "target": cast.target.model_copy(
                update={"requires_sight": True, "requires_willing": True}
            ),
            "effects": [
                cast.effects[0].model_copy(
                    update={
                        "lifecycle": EffectLifecycleSpec(
                            stacking="latest_applies",
                            stacking_group="haste",
                            on_end=(
                                EffectEndFollowUp(
                                    effect_id=penalty.id, expiry_boundary="target_next_turn_end"
                                ),
                            ),
                        )
                    }
                )
            ],
        }
    )
    return spell.model_copy(update={"activities": [cast, end], "passive_effects": [buff, penalty]})


_UTILIZE = {
    "KbVt56CK4ud3FUk3": (
        "FCoYqKd4jqXjHFNN",
        "5b2b5fc5ff65b8597394811ff8a08a91391ae74ab0a5e7ce58fd6b833adf2232",
    ),
    "iYwrPPLmuVD9eqfa": (
        "G8upRnZw22jGWKWy",
        "1e6643fe3e734a10fc3674b592c8b95b826c136dc6747dc2eedbada232e7a98c",
    ),
}


def apply_item_action_types(source_id: str, activities: list[Activity]) -> list[Activity]:
    """Exact reviewed deployment activities; recovery and unknown operations stay unclassified."""
    binding = _UTILIZE.get(source_id)
    if binding is None:
        return activities
    result = []
    found = False
    for activity in activities:
        if activity.id == binding[0]:
            payload = activity.model_dump(mode="json", exclude={"action_type"})
            digest = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if digest != binding[1]:
                raise ValueError(f"action classification source drift: {source_id}")
            activity = activity.model_copy(update={"action_type": "utilize"})
            found = True
        result.append(activity)
    if not found:
        raise ValueError(f"action classification source drift: {source_id}")
    return result
