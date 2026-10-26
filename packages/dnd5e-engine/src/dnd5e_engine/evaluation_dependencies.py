"""Reviewed dependency-specific admission; actor identities and facts stay intact."""

from dnd5e_srd_data.loader import AssetLoader

from dnd5e_engine.evaluation_ruleset import get_default_ruleset
from dnd5e_engine.evaluation_state import CharacterStateV2

# Canonical activated/rest abilities below do not fire on ordinary damage/CON saves.
_DAMAGE_SAVE_NEUTRAL = {"spellcasting", "arcane-recovery", "second-wind"}


def damage_save_identity_failure(actor: CharacterStateV2, loader: AssetLoader) -> str | None:
    """Check the damage/save source identity dependencies for the admitted snapshot."""
    if actor.granted_features is None:
        return "complete explicit feature repertoire is required"
    classes = actor.classes or (
        {actor.class_slug: actor.character_level} if actor.class_slug else {}
    )
    if classes and (
        sum(classes.values()) != actor.character_level or any(n <= 0 for n in classes.values())
    ):
        return "class levels contradict the supplied character level"
    if actor.class_slug and actor.classes and actor.class_slug not in actor.classes:
        return "primary class is absent from explicit class levels"
    reviewed = get_default_ruleset()
    for slug in classes:
        if slug not in {"wizard", "fighter", "cleric", "warlock"} or loader.get_class(
            slug
        ) != reviewed.get_class(slug):
            return "unreviewed class dependencies"
    if actor.subclass_slug or actor.species_slug:
        return "subclass/species dependent damage or saves require further review"
    for slug in actor.granted_features:
        definition = loader.get_feature(slug)
        if (
            slug not in _DAMAGE_SAVE_NEUTRAL
            or definition is None
            or definition != reviewed.get_feature(slug)
        ):
            return "unreviewed feature may change damage or Constitution saves"
        if definition.source_slug not in classes:
            return "feature owner is inconsistent with explicit class facts"
    return None
