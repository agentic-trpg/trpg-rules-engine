"""Reproduce the corpus admission inventory without combat or random draws.

python -m dnd5e_engine.spell_capability_audit --output docs/audits/spell-execution.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from dnd5e_srd_data.loader import AssetLoader, BundledAssetLoader

from dnd5e_engine.live_spell_delivery import delivery_activities
from dnd5e_engine.spell_delivery import SpellDeliverySpec
from dnd5e_engine.spell_execution import (
    admission_failure,
    semantic_digest,
    spell_review,
    spell_reviews,
)


def audit_document(loader: AssetLoader) -> dict[str, object]:
    rows = []
    seen = set()
    legacy_inert = legacy_inert_concentration = 0
    for slug in sorted(loader.list_slugs("spells")):
        spell = loader.get_spell(slug)
        assert spell is not None
        review = spell_review(spell)
        if review is None or review.canonical_sha256 != semantic_digest(spell):
            raise ValueError(f"spell execution review missing or stale: {slug}")
        seen.add(review.uuid)
        selected = delivery_activities(spell, SpellDeliverySpec())
        direct_failure = admission_failure(spell, selected, direct_carrier=True)
        no_carrier_failure = admission_failure(spell, selected)
        inert = (
            not any(
                a.kind in ("attack", "damage", "save", "heal", "check", "cast")
                or (a.kind == "utility" and bool(a.effects))
                for a in spell.activities
            )
            and not review.requires_direct_carrier
        )
        legacy_inert += inert
        legacy_inert_concentration += inert and spell.concentration
        by_id = {a.activity_id: a for a in review.activities}
        activities = []
        for activity in spell.activities:
            activity_review = by_id[activity.id]
            failure = admission_failure(spell, [activity], direct_carrier=True)
            ongoing = (
                activity.persistent_area.ongoing_activation if activity.persistent_area else None
            )
            activities.append(
                {
                    **(
                        {
                            "action_policies": {
                                effect.id: effect.action_policy.model_dump(mode="json")
                                for effect in spell.passive_effects
                                if effect.action_policy is not None
                                and any(
                                    ref.id == effect.id for ref in getattr(activity, "effects", ())
                                )
                            }
                        }
                        if any(effect.action_policy is not None for effect in spell.passive_effects)
                        else {}
                    ),
                    **activity_review.model_dump(mode="json"),
                    "selected_by_default": activity in selected,
                    "timing": activity.timing.trigger,
                    "persistent": activity.persistent_area is not None,
                    "admission_failure": failure.model_dump(mode="json") if failure else None,
                    **({"action_type": activity.action_type} if activity.action_type else {}),
                    **(
                        {
                            "target_requirements": {
                                "sight": activity.target.requires_sight,
                                "willing_attestation": activity.target.requires_willing,
                            }
                        }
                        if activity.target.requires_sight or activity.target.requires_willing
                        else {}
                    ),
                    **(
                        {
                            "effect_end": {
                                ref.id: [
                                    entry.model_dump(mode="json") for entry in ref.lifecycle.on_end
                                ]
                                for ref in getattr(activity, "effects", ())
                                if ref.lifecycle and ref.lifecycle.on_end
                            }
                        }
                        if any(
                            ref.lifecycle and ref.lifecycle.on_end
                            for ref in getattr(activity, "effects", ())
                        )
                        else {}
                    ),
                    **({"ongoing_activation": ongoing.model_dump(mode="json")} if ongoing else {}),
                    **(
                        {
                            "ongoing_admission_failure": (
                                ongoing_failure.model_dump(mode="json")
                                if (
                                    ongoing_failure := admission_failure(
                                        spell, [activity], ongoing_carrier=True
                                    )
                                )
                                else None
                            )
                        }
                        if any(
                            a.persistent_area
                            and a.persistent_area.ongoing_activation
                            and activity.id in a.persistent_area.ongoing_activation.activity_ids
                            for a in spell.activities
                        )
                        else {}
                    ),
                    **(
                        {
                            "effect_selection": {
                                "mode": activity.effect_selection,
                                "candidate_ids": [ref.id for ref in activity.effects],
                                "requires_willing_attestation": activity.target.affects.type
                                == "willing",
                            }
                        }
                        if activity.kind != "cast" and activity.effect_selection
                        else {}
                    ),
                    **(
                        {
                            "environment": activity.persistent_area.environment.model_dump(
                                mode="json"
                            )
                        }
                        if activity.persistent_area and activity.persistent_area.environment
                        else {}
                    ),
                }
            )
        rows.append(
            {
                "slug": slug,
                "uuid": review.uuid,
                "classification": review.classification,
                "canonical_sha256": review.canonical_sha256,
                "source_url": review.source_url,
                "missing_mechanisms": list(review.required_mechanisms),
                "missing_details": list(review.missing_details),
                "limitations": list(review.limitations),
                "entrypoints": {
                    "pc_direct": direct_failure.model_dump(mode="json") if direct_failure else None,
                    "monster_cast": no_carrier_failure.model_dump(mode="json")
                    if no_carrier_failure
                    else "Monster AI effect selection declaration unavailable"
                    if any(getattr(a, "effect_selection", None) for a in selected)
                    else "Monster AI willing-target declaration unavailable"
                    if any(a.target.requires_willing for a in selected)
                    else "Monster AI point-origin declaration unavailable"
                    if any(
                        a.persistent_area
                        and a.persistent_area.environment
                        and a.persistent_area.placement == "stationary"
                        for a in selected
                    )
                    else "Monster AI ongoing activation declaration unavailable"
                    if any(
                        a.persistent_area and a.persistent_area.ongoing_activation for a in selected
                    )
                    else None,
                    "item_delegated": (
                        no_carrier_failure.model_dump(mode="json")
                        if no_carrier_failure
                        else "item concentration ownership deferred"
                        if spell.concentration
                        else None
                    ),
                },
                "activities": activities,
            }
        )
    if seen != set(spell_reviews()):
        raise ValueError("spell execution review contains orphaned entries")
    return {
        "schema_version": 1,
        "evidence": (
            "Static reviewed contracts and typed admission; not whole-corpus runtime proof."
        ),
        "scope": (
            "Combat creature payloads; material inventory, narrative "
            "and world objects remain host-owned."
        ),
        "counts": dict(sorted(Counter(row["classification"] for row in rows).items())),
        "spell_count": len(rows),
        "activity_count": sum(len(row["activities"]) for row in rows),
        "legacy_structural_counts": {
            "at_least_one_mechanical_kind": len(rows) - legacy_inert,
            "no_mechanical_kind": legacy_inert,
            "inert_concentration": legacy_inert_concentration,
        },
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    text = json.dumps(audit_document(BundledAssetLoader()), indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
