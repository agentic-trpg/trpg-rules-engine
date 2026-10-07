"""Ordered, validated feature ownership at the build/live boundary."""

from __future__ import annotations

from dataclasses import dataclass

from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.class_ import Class, Subclass

from dnd5e_engine.activities.scale import feature_owners
from dnd5e_engine.rules.character import leveled_feature_slugs, validate_feature_picks
from dnd5e_engine.specs import PartyMemberSpec
from dnd5e_engine.types.combat import Combatant


@dataclass(frozen=True)
class FeatureOwner:
    slug: str
    owner_slug: str
    level: int
    class_slug: str | None
    spellcasting_ability: str | None


def feature_repertoire(
    actor: Combatant | PartyMemberSpec, loader: AssetLoader
) -> tuple[FeatureOwner, ...]:
    """None is the legacy fixed-grants sentinel; an explicit carrier is checked.

    The carrier is the complete DerivedSheet feature projection. It may omit
    fixed grants (including explicitly empty), but cannot invent a grant or pick
    outside a reached choice pool. Ownership comes from granting documents,
    never from the primary class or a feature's source directory/slug.
    """
    classes = dict(actor.classes) or (
        {actor.class_slug: actor.character_level} if actor.class_slug else {}
    )
    owners = feature_owners(
        classes=classes,
        subclass_slug=actor.subclass_slug,
        species_slug=actor.species_slug,
        level=actor.character_level,
        loader=loader,
    )
    fixed: dict[str, FeatureOwner] = {}
    choices: dict[str, FeatureOwner] = {}
    for slug, doc, level in owners:
        class_slug = (
            doc.slug
            if isinstance(doc, Class)
            else (doc.class_identifier if isinstance(doc, Subclass) else None)
        )
        # A subclass outside the actor's classes cannot confer ownership.
        if isinstance(doc, Subclass) and doc.class_identifier not in classes:
            continue
        cls = loader.get_class(class_slug) if class_slug else None
        ability = cls.spellcasting.ability or None if cls and cls.spellcasting else None
        for feature_slug in leveled_feature_slugs([(doc, level)]):
            fixed.setdefault(
                feature_slug, FeatureOwner(feature_slug, slug, level, class_slug, ability)
            )
        for choice in doc.feature_choices:
            if any(step.level <= level and step.count > 0 for step in choice.schedule):
                for option in choice.pool:
                    if option.ref_type == "feature" and option.level <= level:
                        choices.setdefault(
                            option.slug, FeatureOwner(option.slug, slug, level, class_slug, ability)
                        )
    if actor.granted_features is None:
        return tuple(fixed.values())
    allowed = {**choices, **fixed}
    illegal = [slug for slug in actor.granted_features if slug not in allowed]
    if illegal:
        raise ValueError(f"feature repertoire contains illegal grants: {illegal!r}")
    validate_feature_picks(
        [(doc, level) for _, doc, level in owners],
        [slug for slug in actor.granted_features if slug not in fixed],
    )
    selected = set(actor.granted_features)
    ordered = [slug for slug in fixed if slug in selected]
    ordered.extend(slug for slug in actor.granted_features if slug not in fixed)
    return tuple(allowed[slug] for slug in dict.fromkeys(ordered))
