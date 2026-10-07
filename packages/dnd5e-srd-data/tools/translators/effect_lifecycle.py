"""Exact reviewed activity/effect lifecycle mappings, with no prose inference."""

from typing import Literal

from dnd5e_srd_data.schema.common import Activity, PassiveEffect
from dnd5e_srd_data.schema.lifecycle import EffectLifecycleSpec, RepeatSaveSpec

LifecycleSourceKind = Literal["spell", "feature", "item"]
_REPEAT = EffectLifecycleSpec(repeat_save=RepeatSaveSpec())
_MINUTE_REPEAT = EffectLifecycleSpec(repeat_save=RepeatSaveSpec(), maximum_rounds=10)
_MAPPINGS: dict[tuple[LifecycleSourceKind, str, str, str], EffectLifecycleSpec] = {
    ("spell", "hold-person", "dnd5eactivity000", "PklwZi3SKQ3JU2M2"): _REPEAT,
    ("spell", "hold-monster", "dnd5eactivity000", "vC7qgyy8cjgi3t0T"): _REPEAT,
    ("feature", "cunning-strike", "n64fvJMT9fPUy7DH", "RazTM6biKVtNtjEW"): _MINUTE_REPEAT,
    ("feature", "devious-strikes", "3eq7lcmpkJJBU2KO", "C2IGgt4PnRMxZVey"): EffectLifecycleSpec(
        repeat_save=RepeatSaveSpec(), maximum_rounds=10, expire_on_positive_damage=True
    ),
    ("feature", "intimidating-presence", "ZRHT8mOlea6T8XpP", "3LofAPFLZJMQJirN"): _MINUTE_REPEAT,
    ("feature", "reckless-attack", "", "XA0GhXVB54U2IuRP"): EffectLifecycleSpec(
        expiry_boundary="source_next_turn_start"
    ),
    ("feature", "brutal-strike", "nN5gsB6AcSQ4uQPN", "bg9jWpPRIJ0Q2vPY"): EffectLifecycleSpec(
        expiry_boundary="source_next_turn_start",
        stacking="latest_only",
        stacking_group="hamstring-blow",
    ),
    (
        "feature",
        "improved-brutal-strike",
        "I30qGlPDcyKwz65H",
        "L9N4evZo1jt46dap",
    ): EffectLifecycleSpec(
        expiry_boundary="source_next_turn_start",
        one_use_modifiers=("next_save_disadvantage",),
    ),
    (
        "feature",
        "improved-brutal-strike",
        "UmRlsf4QWW98I4FS",
        "tUyuyTQGmpUqMcFe",
    ): EffectLifecycleSpec(
        expiry_boundary="source_next_turn_start",
        one_use_modifiers=("next_attack_bonus_other_creature",),
        next_attack_scope="other_creature",
        next_attack_bonus_group="sundering-blow",
    ),
}


def reviewed_effect_lifecycle(
    source_kind: LifecycleSourceKind, slug: str, activity_id: str, effect_id: str
) -> EffectLifecycleSpec | None:
    spec = _MAPPINGS.get((source_kind, slug, activity_id, effect_id))
    return spec.model_copy(deep=True) if spec is not None else None


def effect_lifecycle_activities(
    source_kind: LifecycleSourceKind, slug: str, activities: list[Activity]
) -> list[Activity]:
    out: list[Activity] = []
    for activity in activities:
        if not hasattr(activity, "effects"):
            out.append(activity)
            continue
        refs = []
        for ref in activity.effects:
            lifecycle = reviewed_effect_lifecycle(source_kind, slug, activity.id, ref.id)
            refs.append(ref.model_copy(update={"lifecycle": lifecycle}) if lifecycle else ref)
        out.append(activity.model_copy(update={"effects": refs}))
    return out


def effect_lifecycle_effects(
    source_kind: LifecycleSourceKind, slug: str, effects: list[PassiveEffect]
) -> list[PassiveEffect]:
    """Correct the reviewed one-minute feature's erroneous display counter."""
    if (source_kind, slug) != ("feature", "intimidating-presence"):
        return effects
    return [
        effect.model_copy(update={"duration": {"rounds": 10}})
        if effect.id == "3LofAPFLZJMQJirN"
        else effect
        for effect in effects
    ]
