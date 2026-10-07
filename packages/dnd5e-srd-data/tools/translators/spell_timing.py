"""Pinned spell/activity timing mappings. Only the translator reads source text.

Each mapping requires the exact activity id, kind, name and activation condition
plus an exact verified clause. A changed source fails loudly instead of applying
a guessed timing. Complex sequences/hazards remain explicitly manual.
"""

from dnd5e_srd_data.schema.common import Activity, ActivityTiming

# slug, activity id -> kind, name, activation condition, verified clause, timing
_PATTERNS: dict[tuple[str, str], tuple[str, str, str, str, ActivityTiming]] = {
    ("weird", "nuStSySOEkwOUnXf"): (
        "save",
        "End of Turn Save",
        "at the end of each affected creature's turn",
        "A Frightened target makes a Wisdom saving throw at the end of each of its turns.",
        ActivityTiming(
            trigger="turn_end",
            recurring=True,
            effect_id="6bCIQhmzYmSR5Ck1",
            requires_condition="frightened",
            ends_effect_on_success=True,
        ),
    ),
    ("vitriolic-sphere", "dnd5eactivity200"): (
        "damage",
        "End of Turn Damage",
        "",
        "damage at the end of its next turn.",
        ActivityTiming(trigger="turn_end", effect_id="WHhf4PFHQBDkCAyb", scale_with_slot=False),
    ),
    ("stinking-cloud", "dnd5eactivity000"): (
        "save",
        "Start of Turn Save",
        "",
        "Each creature that starts its turn in the Sphere must succeed "
        "on a Constitution saving throw",
        ActivityTiming(
            trigger="turn_start", recurring=True, subject="area", effects_concentration=False
        ),
    ),
    ("acid-arrow", "uAyE5DrJp0YpElcw"): (
        "damage",
        "Lingering Acid Damage",
        "",
        "2d4 Acid damage at the end of its next turn.",
        ActivityTiming(trigger="turn_end", effect_id="NgglkyjzTbDh5Loa"),
    ),
    ("ensnaring-strike", "ZWId9mbOE9zFnP6f"): (
        "damage",
        "Start of Turn Damage",
        "",
        "While Restrained, the target takes 1d6 Piercing damage at the start of each of its turns.",
        ActivityTiming(
            trigger="turn_start",
            recurring=True,
            effect_id="tFGMG3cjQTEeAhv2",
            requires_condition="restrained",
        ),
    ),
    ("searing-smite", "dnd5eactivity000"): (
        "save",
        "Start of Turn Save",
        "",
        "At the start of each of its turns until the spell ends, the target takes 1d6 Fire damage",
        ActivityTiming(
            trigger="turn_start",
            recurring=True,
            effect_id="A0tTvLeRetrC708K",
            ends_effect_on_success=True,
        ),
    ),
}

# These need state beyond this task's three boundary lifecycles: area entry,
# moving walls/clouds, numbered rounds, bead growth or spell-end detonation.
_MANUAL: dict[tuple[str, str], str] = {
    ("incendiary-cloud", "lLAC6qfRLaes7956"): "Per Turn Save",
    ("tsunami", "o7MOuS6uL1ZV3dhe"): "Start of Turn Effects",
    ("earthquake", "2CCthCZliyjEoavy"): "End of Turn Fissures",
    ("earthquake", "qQtXMvVJZDL3NtNF"): "Collapsing Structure Save",
    ("delayed-blast-fireball", "iWd7zKpQIgWofJAp"): "Touch Bead",
    ("delayed-blast-fireball", "9i14Jmun9em69EnX"): "Turn End Damage Increase",
    ("delayed-blast-fireball", "M15GlfjeWy7Cdiqn"): "Trigger Explosion",
    ("storm-of-vengeance", "WOo8u0FLAzNMpMJN"): "Turn 2: Acid Rain",
    ("storm-of-vengeance", "Fd8ZGgmDyNmJJuj3"): "Turn 3: Lightning",
    ("storm-of-vengeance", "H4uCtCkANBkttnov"): "Turn 4: Hailstones",
    ("storm-of-vengeance", "yt2oWWelZl1zV4CB"): "Turn 5+: Gusts and Rain",
    ("forbiddance", "9IBiOeIf2PC1wcLp"): "Damage Forbidden Creature",
    ("wall-of-ice", "D8Q85bjeZgiFky5j"): "Frigid Air",
    ("wall-of-thorns", "dMiM7Qec4keU3w7B"): "Traversal Save",
}


def apply_spell_timing(slug: str, description: str, activities: list[Activity]) -> list[Activity]:
    """Apply only verified mappings; leave unclassified activities immediate."""
    expected_ids = {key[1] for key in (*_PATTERNS, *_MANUAL) if key[0] == slug}
    if not expected_ids.issubset({activity.id for activity in activities}):
        raise ValueError(f"spell timing source drift: missing audited activity in {slug}")
    result: list[Activity] = []
    for activity in activities:
        key = (slug, activity.id)
        if key in _PATTERNS:
            kind, name, condition, clause, timing = _PATTERNS[key]
            if (activity.kind, activity.name, activity.activation.condition) != (
                kind,
                name,
                condition,
            ) or clause not in description:
                raise ValueError(f"spell timing source drift: {slug}/{activity.id}")
            result.append(activity.model_copy(update={"timing": timing}))
        elif key in _MANUAL:
            if activity.name != _MANUAL[key]:
                raise ValueError(f"manual timing source drift: {slug}/{activity.id}")
            result.append(activity.model_copy(update={"timing": ActivityTiming(trigger="manual")}))
        else:
            result.append(activity)
    return result
