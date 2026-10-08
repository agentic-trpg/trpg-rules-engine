"""Reviewed delivery metadata; only ingestion examines source restrictions.

Exact source/activity bindings distinguish targeting templates from display
geometry. Closed special-field normalizations deliberately do not parse prose.
"""

from dnd5e_srd_data.schema.common import (
    Activity,
    AreaSemantics,
    ForcedMovementSpec,
    TargetCreatureFilter,
)

_FILTERS: dict[str, TargetCreatureFilter] = {
    "Undead": TargetCreatureFilter(include_creature_types=("undead",)),
    "Undead of your choice": TargetCreatureFilter(include_creature_types=("undead",)),
    "Elemental": TargetCreatureFilter(include_creature_types=("elemental",)),
    "Fiend or Undead": TargetCreatureFilter(include_creature_types=("fiend", "undead")),
    "Celestial, Elemental, Fey, Fiend, or Undead": TargetCreatureFilter(
        include_creature_types=("celestial", "elemental", "fey", "fiend", "undead")
    ),
    "target is Humanoid": TargetCreatureFilter(include_creature_types=("humanoid",)),
}

_AREAS: dict[tuple[str, str, str], tuple[str, str, str, AreaSemantics]] = {
    ("spell", "slow", "dnd5eactivity000"): (
        "cube",
        "40",
        "40-foot Cube within range",
        AreaSemantics(origin_policy="point_within_range", includes_origin=True),
    ),
    ("spell", "phantasmal-force", "5LLVXOLZf1fTstUw"): (
        "cube",
        "10",
        "no larger than a 10-foot Cube",
        AreaSemantics(template_role="effect_geometry", origin_policy="point_within_range"),
    ),
    ("spell", "thunderwave", "dnd5eactivity000"): (
        "cube",
        "15",
        "15-foot Cube originating from you",
        AreaSemantics(origin_policy="actor"),
    ),
    ("item", "dust-of-sneezing-and-choking", "GDzbsjaVxNM7nnBu"): (
        "radius",
        "30",
        "forcing yourself and every creature in a 30-foot Emanation",
        AreaSemantics(origin_policy="actor", includes_origin=True),
    ),
    ("feature", "intimidating-presence", "ZRHT8mOlea6T8XpP"): (
        "radius",
        "30",
        "30-foot Emanation originating from you",
        AreaSemantics(origin_policy="actor"),
    ),
    ("spell", "spirit-guardians", "dnd5eactivity000"): (
        "radius",
        "15",
        "15-foot Emanation",
        AreaSemantics(origin_policy="actor"),
    ),
    ("spell", "stinking-cloud", "dnd5eactivity000"): (
        "sphere",
        "20",
        "gas centered on a point within range",
        AreaSemantics(origin_policy="point_within_range", includes_origin=True),
    ),
}


def normalize_target_filters(activities: list[Activity]) -> list[Activity]:
    """Normalize exact reviewed strings and refuse unreviewed area restrictions.

    Non-area legacy restrictions (size, grapples, visibility, objects) retain
    their existing contract. They are not creature-type mechanics and must not
    accidentally become type predicates during this migration.
    """
    result: list[Activity] = []
    for activity in activities:
        special = activity.target.affects.special
        predicate = _FILTERS.get(special)
        if predicate is None and special and activity.target.template.type:
            predicate = TargetCreatureFilter(deferred_reason="unreviewed_target_restriction")
        if predicate is not None:
            activity = activity.model_copy(
                update={"target": activity.target.model_copy(update={"creature_filter": predicate})}
            )
        result.append(activity)
    return result


def apply_area_delivery(
    kind: str, slug: str, description: str, activities: list[Activity]
) -> list[Activity]:
    """Attach exact reviewed semantics and fail loudly on upstream source drift."""
    expected = {key[2] for key in _AREAS if key[:2] == (kind, slug)}
    if not expected.issubset({activity.id for activity in activities}):
        raise ValueError(f"area delivery source drift: {kind}:{slug}")
    result: list[Activity] = []
    for activity in activities:
        binding = _AREAS.get((kind, slug, activity.id))
        if binding is not None:
            shape, size, clause, semantics = binding
            if (activity.target.template.type, activity.target.template.size) != (shape, size):
                raise ValueError(f"area delivery source drift: {kind}:{slug}:{activity.id}")
            if clause not in description:
                raise ValueError(f"area delivery source drift: {kind}:{slug}:{activity.id}")
            if activity.kind != "save":
                raise ValueError(f"area delivery source drift: {kind}:{slug}:{activity.id}")
            if (kind, slug) == ("spell", "thunderwave") and (
                activity.save.ability != ["con"]
                or activity.save.dc.calculation != "spellcasting"
                or activity.save.dc.formula
                or activity.damage.on_save != "half"
            ):
                raise ValueError(f"area delivery source drift: {kind}:{slug}:{activity.id}")
            target = activity.target.model_copy(update={"area_semantics": semantics})
            if (kind, slug) == ("item", "dust-of-sneezing-and-choking"):
                clause = "Constructs, Elementals, Oozes, Plants, and Undead succeed"
                if (
                    clause not in description
                    or activity.save.ability != ["con"]
                    or activity.save.dc.calculation
                    or activity.save.dc.formula != "15"
                ):
                    raise ValueError(f"area delivery source drift: {kind}:{slug}:{activity.id}")
                target = target.model_copy(
                    update={
                        "creature_filter": TargetCreatureFilter(
                            auto_success_creature_types=(
                                "construct",
                                "elemental",
                                "ooze",
                                "plant",
                                "undead",
                            )
                        )
                    }
                )
            activity = activity.model_copy(update={"target": target})
            if (kind, slug) == ("spell", "thunderwave"):
                if "pushed 10 feet away from you" not in description:
                    raise ValueError(f"area delivery source drift: {kind}:{slug}:{activity.id}")
                activity = activity.model_copy(
                    update={
                        "forced_movement": ForcedMovementSpec(
                            trigger="failed_save", distance_ft=10, direction="away_from_source"
                        )
                    }
                )
        result.append(activity)
    return result
