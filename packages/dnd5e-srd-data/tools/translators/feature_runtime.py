"""Exact, audited Foundry identities for feature activity operations.

These bindings supplement typed activities; descriptions/names are never parsed.
See docs/dev/feature-runtime.md for the supported operations and deferred riders.
"""

from dnd5e_srd_data.schema.common import Activity, HealActivity, PassiveEffect
from dnd5e_srd_data.schema.feature import FeatureRuntimeOperation, FeatureTargetRule

OPERATIONS: dict[tuple[str, str], FeatureRuntimeOperation] = {
    ("adrenaline-rush", "jN5Zo6mDXll3c4Cz"): "dash_heal",
    ("action-surge", "jtpVG47zPBUI0CZ5"): "native",
    ("bardic-inspiration", "dnd5eactivity000"): "native",
    ("rage", "dnd5eactivity000"): "native",
    ("wild-shape", "FsDnynyqno8XNHgf"): "native",
    ("monks-focus", "2ghJTBhilLrFn9xT"): "flurry",
    ("monks-focus", "7xj7b6e8tDznDSrE"): "dodge_disengage",
    ("monks-focus", "EFzidO6yAapw8d60"): "disengage",
    ("lay-on-hands", "K6UeXQwTyDHWvis8"): "remove_poison",
}
TARGET_RULES: dict[tuple[str, str], FeatureTargetRule] = {
    ("bardic-inspiration", "dnd5eactivity000"): "perceives_caster",
    ("channel-divinity-cleric", "UdbUwbvrWwgDuNy9"): "other_visible_creature",
    ("channel-divinity-cleric", "OY9UrTXvlRL0JUoI"): "other_visible_creature",
}


def feature_target_rules(slug: str, activity_ids: list[str]) -> dict[str, FeatureTargetRule]:
    return {
        activity_id: TARGET_RULES[(slug, activity_id)]
        for activity_id in activity_ids
        if (slug, activity_id) in TARGET_RULES
    }


def feature_runtime_activities(slug: str, activities: list[Activity]) -> list[Activity]:
    for index, activity in enumerate(activities):
        if slug == "lay-on-hands" and activity.id in ("gXZh9aGHcywV9huC", "K6UeXQwTyDHWvis8"):
            activities[index] = activity.model_copy(
                update={"range": activity.range.model_copy(update={"units": "touch"})}
            )
        if (
            slug == "wholeness-of-body"
            and activity.id == "CoatPmjognJHbfZ6"
            and isinstance(activity, HealActivity)
        ):
            healing = activity.healing.model_copy(
                update={
                    "custom": activity.healing.custom.model_copy(
                        update={
                            "enabled": True,
                            "formula": "max(1, @scale.monk.die + @abilities.wis.mod)",
                        }
                    )
                }
            )
            activities[index] = activity.model_copy(update={"healing": healing})
    return activities


def feature_runtime_operations(
    slug: str, activity_ids: list[str]
) -> dict[str, FeatureRuntimeOperation]:
    return {
        activity_id: OPERATIONS[(slug, activity_id)]
        for activity_id in activity_ids
        if (slug, activity_id) in OPERATIONS
    }


def feature_runtime_effects(slug: str, effects: list[PassiveEffect]) -> list[PassiveEffect]:
    """Rage's typed 10-minute activity/600 seconds override its stale 10 rounds.

    Exact audited effect identity, matching the activity operation above; other
    durations keep their original round/second precedence.
    """
    return [
        effect.model_copy(update={"duration": {**effect.duration, "rounds": 100}})
        if slug == "rage" and effect.id == "G5XZTi4zYTFiHVll"
        else effect
        for effect in effects
    ]
