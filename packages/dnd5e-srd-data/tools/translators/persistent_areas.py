"""Pinned persistent-area producers, verified against audited source clauses.

Only ingestion reads descriptions. Runtime consumes PersistentAreaSpec and the
measured template, never spell names, activity labels or descriptive prose.
"""

from dnd5e_srd_data.schema.common import Activity, PersistentAreaSpec

_STRUCTURES = {
    "spirit-guardians": ("radius", "15", ["wis"], "spellcasting", ""),
    "stinking-cloud": ("sphere", "20", ["con"], "spellcasting", ""),
    "ball-bearings": ("square", "10", ["dex"], "", "10"),
    "caltrops": ("square", "5", ["dex"], "", "15"),
}

_PRODUCERS = {
    "spirit-guardians": (
        "dnd5eactivity000",
        "A creature makes this save only once per turn.",
        PersistentAreaSpec(
            placement="follow-source",
            triggers=("enter", "area-enters-creature", "turn-end-inside"),
            speed_multiplier=0.5,
        ),
    ),
    "stinking-cloud": (
        "dnd5eactivity000",
        "Each creature that starts its turn in the Sphere must succeed",
        PersistentAreaSpec(triggers=("turn-start-inside",)),
    ),
    "ball-bearings": (
        "FCoYqKd4jqXjHFNN",
        "A creature that enters this area for the first time on a turn",
        PersistentAreaSpec(triggers=("enter",)),
    ),
    "caltrops": (
        "G8upRnZw22jGWKWy",
        "until the start of its next turn.",
        PersistentAreaSpec(triggers=("enter",), effects_until_target_turn_start=True),
    ),
}


def apply_persistent_areas(
    slug: str, description: str, activities: list[Activity]
) -> list[Activity]:
    """Apply explicit mappings, failing closed when source structure changes."""
    producer = _PRODUCERS.get(slug)
    if producer is None:
        return activities
    activity_id, clause, spec = producer
    payload = next((a for a in activities if a.id == activity_id), None)
    template = next((a.target.template for a in activities if a.target.template.type), None)
    if payload is None or payload.kind != "save" or template is None or clause not in description:
        raise ValueError(f"persistent area source drift: {slug}")
    if (
        template.type,
        template.size,
        payload.save.ability,
        payload.save.dc.calculation,
        payload.save.dc.formula,
    ) != _STRUCTURES[slug]:
        raise ValueError(f"persistent area source drift: {slug}")
    template = template.model_copy(update={"stationary": spec.placement == "stationary"})
    affects = payload.target.affects.model_copy(
        update={"count": "", "choice": spec.placement == "follow-source"}
    )
    updates: dict = {
        "persistent_area": spec,
        "target": payload.target.model_copy(update={"template": template, "affects": affects}),
    }
    if spec.speed_multiplier is not None:
        # The speed rider is continuous area membership, not a save effect.
        updates.update(effects=[], applied_effects=[])
    if spec.effects_until_target_turn_start:
        updates["damage"] = payload.damage.model_copy(update={"on_save": "none"})
    return [a.model_copy(update=updates) if a.id == activity_id else a for a in activities]
