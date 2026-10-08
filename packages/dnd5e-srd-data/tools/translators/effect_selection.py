"""Exact reviewed choice carriers. Runtime never dispatches on spell slugs.

Pinned canonical IDs, Change Keys, counts, target kinds and durations gate
regeneration. Any Foundry mechanical drift requires a new review.
"""

from dnd5e_srd_data.schema.lifecycle import EffectLifecycleSpec
from dnd5e_srd_data.schema.spell import Spell

_REVIEWED: dict[
    str, tuple[str, str, str, tuple[tuple[str, tuple[tuple[str, int, str], ...], int], ...]]
] = {
    "guidance": (
        "R6Fs3rsgXlgHPOFn",
        "1",
        "willing",
        (
            ("GezMMzvZhWXEYyGm", (("system.skills.acr.bonuses.check", 2, "1d4"),), 60),
            ("y6aODSkiubtFt6Ap", (("system.skills.ani.bonuses.check", 2, "1d4"),), 60),
            ("Vlfh8IFOezrSpeBZ", (("system.skills.arc.bonuses.check", 2, "1d4"),), 60),
            ("sPkLWVpkJ7NqDQT0", (("system.skills.ath.bonuses.check", 2, "1d4"),), 60),
            ("Je4xWElmaiSDsOjU", (("system.skills.dec.bonuses.check", 2, "1d4"),), 60),
            ("cw02ZbuiRn6GyoEv", (("system.skills.his.bonuses.check", 2, "1d4"),), 60),
            ("VsU5vUhMioT9pCwG", (("system.skills.ins.bonuses.check", 2, "1d4"),), 60),
            ("UQixxmOdHUDjVYNh", (("system.skills.inv.bonuses.check", 2, "1d4"),), 60),
            ("Cpte6tynFFJbraX2", (("system.skills.itm.bonuses.check", 2, "1d4"),), 60),
            ("Vl1YSG1DlZvx9nSI", (("system.skills.med.bonuses.check", 2, "1d4"),), 60),
            ("IqxGXju1tYDWoda6", (("system.skills.nat.bonuses.check", 2, "1d4"),), 60),
            ("fCdLUO5DoSIT4wVK", (("system.skills.per.bonuses.check", 2, "1d4"),), 60),
            ("qPowjrduxnzKpH8x", (("system.skills.prc.bonuses.check", 2, "1d4"),), 60),
            ("E4BqoCkyoKLJbGmb", (("system.skills.prf.bonuses.check", 2, "1d4"),), 60),
            ("jVKWknHHFuMwvTct", (("system.skills.rel.bonuses.check", 2, "1d4"),), 60),
            ("pwFahx7uqM4DOgnu", (("system.skills.slt.bonuses.check", 2, "1d4"),), 60),
            ("Vb4iRJ73Nw78UUMW", (("system.skills.ste.bonuses.check", 2, "1d4"),), 60),
            ("A1T0X0DUFIxx3qhE", (("system.skills.sur.bonuses.check", 2, "1d4"),), 60),
        ),
    ),
    "enhance-ability": (
        "dnd5eactivity000",
        "@item.level - 1",
        "creature",
        (
            ("gg9dU0SnvU03C803", (("system.abilities.str.check.roll.mode", 2, "1"),), 3600),
            ("GGEj4zNginSIp6VV", (("system.abilities.dex.check.roll.mode", 2, "1"),), 3600),
            ("NTaFTBr1uam93jHS", (("system.abilities.int.check.roll.mode", 2, "1"),), 3600),
            ("EGnngHgGBzTyThW6", (("system.abilities.wis.check.roll.mode", 2, "1"),), 3600),
            ("e02u2XLDltCT5D5x", (("system.abilities.cha.check.roll.mode", 2, "1"),), 3600),
        ),
    ),
    "protection-from-energy": (
        "rIHvDrlENxFLbUQw",
        "1",
        "willing",
        (
            ("RrJ5b1A9TghFe5QK", (("system.traits.dr.value", 2, "acid"),), 3600),
            ("xDm8bqlMGiPq7nlt", (("system.traits.dr.value", 2, "cold"),), 3600),
            ("DsflUNYKqETaLgxG", (("system.traits.dr.value", 2, "fire"),), 3600),
            ("0ODEqmz3M45lwd8q", (("system.traits.dr.value", 2, "lightning"),), 3600),
            ("M13rFIPjAIMwKzQp", (("system.traits.dr.value", 2, "thunder"),), 3600),
        ),
    ),
}


def apply_effect_selection(spell: Spell) -> Spell:
    contract = _REVIEWED.get(spell.slug)
    if contract is None:
        return spell
    activity_id, count, target_kind, effects = contract
    actual = tuple(
        (
            e.id,
            tuple((c.key, c.mode, c.value) for c in e.changes),
            (e.duration or {}).get("seconds"),
        )
        for e in spell.passive_effects
    )
    if len(spell.activities) != 1:
        raise ValueError(f"effect selection source drift: {spell.slug}")
    activity = spell.activities[0]
    refs = getattr(activity, "effects", ())
    if (
        activity.id != activity_id
        or activity.kind != "utility"
        or activity.target.affects.count != count
        or activity.target.affects.type != target_kind
        or tuple(r.id for r in refs) != tuple(e[0] for e in effects)
        or actual != effects
        or not spell.concentration
        or spell.range.units != "touch"
        or spell.casting_time.unit != "action"
        or spell.casting_time.value != 1
        or (spell.duration.value or 0)
        * {"round": 6, "minute": 60, "hour": 3600}.get(spell.duration.units, 0)
        != effects[0][2]
        or any(e.disabled or e.transfer or e.statuses for e in spell.passive_effects)
    ):
        raise ValueError(f"effect selection source drift: {spell.slug}")
    lifecycle = EffectLifecycleSpec(
        maximum_rounds=effects[0][2] // 6,
        stacking="latest_applies",
        stacking_group=spell.foundry_uuid,
    )
    return spell.model_copy(
        update={
            "activities": [
                activity.model_copy(
                    update={
                        "effect_selection": "one_per_target",
                        "effects": [
                            ref.model_copy(update={"lifecycle": lifecycle}) for ref in refs
                        ],
                    }
                )
            ]
        }
    )
