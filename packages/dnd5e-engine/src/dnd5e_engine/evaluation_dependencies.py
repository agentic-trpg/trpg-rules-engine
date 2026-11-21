"""Reviewed dependency-specific admission; actor identities and facts stay intact."""

from dnd5e_srd_data.loader import AssetLoader

from dnd5e_engine.evaluation_class_dependencies import evoker_grants
from dnd5e_engine.evaluation_ruleset import get_default_ruleset
from dnd5e_engine.evaluation_state import CharacterStateV2

# Canonical activated/rest abilities below do not fire on ordinary damage/CON saves.
_DAMAGE_SAVE_NEUTRAL = {"spellcasting", "arcane-recovery", "second-wind"}
_EVOKER_CONCENTRATION_SAVE_NEUTRAL = {
    "spellcasting",
    "arcane-recovery",
    "ritual-adept",
    "scholar",
    "memorize-spell",
    "evocation-savant",
    "potent-cantrip",
}


def concentration_save_identity_failure(actor: CharacterStateV2, loader: AssetLoader) -> str | None:
    """Check reviewed identities for one settled packet's Constitution save.

    The complete canonical Evoker 5 has only save-neutral grants for this test.
    Slots and feature-use counts play no role after the damage has been resolved.
    """
    if actor.subclass_slug != "evoker":
        return damage_save_identity_failure(actor, loader)
    levels = actor.classes or ({"wizard": 5} if actor.class_slug == "wizard" else {})
    if (
        actor.species_slug
        or actor.character_level != 5
        or actor.class_slug not in (None, "wizard")
        or levels != {"wizard": 5}
    ):
        return "only the reviewed Evoker 5 profile is supported"
    canonical = get_default_ruleset()
    wizard = loader.get_class("wizard")
    subclass_grants = evoker_grants(loader, canonical, 5)
    if wizard is None or wizard != canonical.get_class("wizard") or subclass_grants is None:
        return "Evoker class or subclass differs from its reviewed definition"
    grants = subclass_grants | {
        g.slug for g in wizard.granted_features if g.ref_type == "feature" and g.level <= 5
    }
    if (
        grants != _EVOKER_CONCENTRATION_SAVE_NEUTRAL
        or actor.granted_features is None
        or set(actor.granted_features) != grants
        or any(loader.get_feature(g) != canonical.get_feature(g) for g in grants)
    ):
        return "complete reviewed Evoker grants are required for the concentration save"
    return None


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
