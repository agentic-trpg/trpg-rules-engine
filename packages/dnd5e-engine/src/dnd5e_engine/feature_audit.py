"""Reproducible canonical feature audit: python -m dnd5e_engine.feature_audit.

One row per reachable activity at its granting owner's level 20. Formula and
resource diagnostics are independent of semantic support; numeric success is
never used as evidence that an unmodeled trigger has been implemented.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from dnd5e_srd_data.loader import AssetLoader, BundledAssetLoader

from dnd5e_engine.activities.context import ActivityResolutionContext, SourceUses
from dnd5e_engine.activities.scale import build_scale_values
from dnd5e_engine.feature_repertoire import feature_repertoire
from dnd5e_engine.feature_runtime import (
    AuditClassification,
    DrawFreeRandom,
    FeatureInvocation,
    FeaturePreflightError,
    preflight_feature,
    resource_payments,
    scalar_formula,
    validate_feature_formulas,
)
from dnd5e_engine.types.combat import Combatant


@dataclass(frozen=True)
class FeatureAuditRow:
    slug: str
    activity_id: str
    owner: str
    classification: AuditClassification
    reason: str
    formula_error: str | None
    resource_error: str | None
    execution_path: Literal[
        "standalone_activity", "attack_triggered_rider", "other_event_triggered"
    ]
    standalone_executable: bool
    rider_executable: bool


def reachable_actors(loader: AssetLoader) -> dict[str, Combatant]:
    """Choose a deterministic actual grant/choice owner for each reachable feature."""
    actors: dict[str, Combatant] = {}
    for category in ("classes", "subclasses", "species"):
        for slug in sorted(loader.list_slugs(category)):
            if category == "classes":
                doc = loader.get_class(slug)
                class_slug, subclass_slug, species_slug = slug, None, None
            elif category == "subclasses":
                doc = loader.get_subclass(slug)  # type: ignore[assignment]
                assert doc is not None
                class_slug, subclass_slug, species_slug = doc.class_identifier, slug, None  # type: ignore[attr-defined]
            else:
                doc = loader.get_species(slug)  # type: ignore[assignment]
                class_slug, subclass_slug, species_slug = None, None, slug
            assert doc is not None
            actor = Combatant(
                entity_id="audit:actor",
                entity_type="Character",
                name="Audit",
                initiative=20,
                hp_current=100,
                hp_max=200,
                character_level=20,
                class_slug=class_slug,
                subclass_slug=subclass_slug,
                species_slug=species_slug,
                strength=16,
                dexterity=16,
                constitution=16,
                intelligence=16,
                wisdom=16,
                charisma=16,
            )
            for owner in feature_repertoire(actor, loader):
                actors.setdefault(owner.slug, actor)
            fixed = tuple(o.slug for o in feature_repertoire(actor, loader))
            for choice in doc.feature_choices:
                if any(s.level <= 20 and s.count > 0 for s in choice.schedule):
                    for option in choice.pool:
                        if option.ref_type == "feature":
                            picked = actor.model_copy(
                                update={"granted_features": (*fixed, option.slug)}
                            )
                            actors.setdefault(option.slug, picked)
    return dict(sorted(actors.items()))


def audit_context(actor: Combatant, slug: str, loader: AssetLoader) -> ActivityResolutionContext:
    owners = feature_repertoire(actor, loader)
    owner = next(o for o in owners if o.slug == slug)
    classes = dict(actor.classes) or (
        {actor.class_slug: actor.character_level} if actor.class_slug else {}
    )
    scales = build_scale_values(
        class_slug=actor.class_slug,
        subclass_slug=actor.subclass_slug,
        species_slug=actor.species_slug,
        level=actor.character_level,
        loader=loader,
        classes=classes,
        granted_feature_levels={o.slug: o.level for o in owners},
    )
    return ActivityResolutionContext(
        rng=DrawFreeRandom(0),
        caster=actor,
        targets=[actor],
        event_emitter=lambda _: None,
        caster_abilities={a: 16 for a in ("str", "dex", "con", "int", "wis", "cha")},
        caster_proficiency_bonus=6,
        caster_level=20,
        spellcasting_ability=owner.spellcasting_ability or None,
        class_levels=classes,
        scale_values=scales,
    )


def audit_features(loader: AssetLoader) -> tuple[FeatureAuditRow, ...]:
    from dataclasses import replace

    rows = []
    for slug, actor in reachable_actors(loader).items():
        feature = loader.get_feature(slug)
        if feature is None:
            continue
        for activity in feature.activities:
            ctx = audit_context(actor, slug, loader)
            scaling = (
                1
                if activity.consumption.scaling.allowed
                and any(
                    t.scaling.mode == "amount" and not t.target
                    for t in activity.consumption.targets
                )
                else None
            )
            ctx = replace(ctx, scaling_value=scaling)
            if feature.uses and feature.uses.max:
                with contextlib.suppress(ValueError):
                    ctx = replace(
                        ctx, source_uses=SourceUses(0, scalar_formula(feature.uses.max, ctx))
                    )
            formula_error = resource_error = None
            try:
                validate_feature_formulas(feature, activity, ctx)
            except ValueError as error:
                formula_error = str(error)
            repertoire = tuple(o.slug for o in feature_repertoire(actor, loader))
            try:
                resource_payments(feature, activity, ctx, loader, repertoire)
            except ValueError as error:
                resource_error = str(error)
            invocation = FeatureInvocation(
                activities=[activity],
                passive_effects=feature.passive_effects,
                is_bonus_action=activity.activation.type == "bonus",
                scaling_value=scaling,
            )
            classification: AuditClassification = "fully_resolvable"
            reason = "typed activity and required carriers validated"
            try:
                preflight_feature(
                    feature, invocation, ctx, loader=loader, repertoire=repertoire, spent={}
                )
            except FeaturePreflightError as error:
                classification, reason = error.classification, str(error)
            owner = next(o for o in feature_repertoire(actor, loader) if o.slug == slug)
            rows.append(
                FeatureAuditRow(
                    slug,
                    activity.id,
                    owner.owner_slug,
                    classification,
                    reason,
                    formula_error,
                    resource_error,
                    (
                        "attack_triggered_rider"
                        if activity.id in feature.attack_riders
                        else "other_event_triggered"
                        if classification == "semantic_special_case"
                        else "standalone_activity"
                    ),
                    classification == "fully_resolvable",
                    classification == "attack_rider_executable",
                )
            )
    return tuple(rows)


def audit_document(loader: AssetLoader) -> dict[str, object]:
    rows = audit_features(loader)
    return {
        "counts": dict(sorted(Counter(row.classification for row in rows).items())),
        "reachable_activities": len(rows),
        "rows": [asdict(row) for row in rows],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    document = audit_document(BundledAssetLoader())
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    if args.output is not None:
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
