"""Deterministic typed attack inventory: python -m dnd5e_engine.attack_rider_audit.

Discovery is ingested from reviewed feature/activity identities. This command
never parses descriptions or activation prose, and does not invoke a resolver.
Concrete rider support is separate from funding producers, passive contexts,
incoming-hit defenses and foundations without an activity.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from dnd5e_srd_data.loader import AssetLoader, BundledAssetLoader
from dnd5e_srd_data.schema.common import Activity, SaveActivity
from dnd5e_srd_data.schema.feature import AttackRiderSemantics, Feature

from dnd5e_engine.feature_runtime import FeaturePreflightError, feature_operation


def _row(
    feature: Feature,
    activity: Activity | None,
    semantics: AttackRiderSemantics,
    *,
    option_id: str | None = None,
    deferred_reason: str | None = None,
) -> dict[str, Any]:
    reason = deferred_reason or semantics.deferred_reason
    executable = semantics.inventory_role == "rider" and reason is None and activity is not None
    classification = (
        "executable_rider"
        if executable
        else "deferred_rider"
        if semantics.inventory_role == "rider"
        else "supporting_context"
    )
    standalone_rejected = True
    if activity is not None:
        try:
            feature_operation(feature, activity)
        except FeaturePreflightError:
            pass
        else:
            standalone_rejected = False
    return {
        "feature_slug": feature.slug,
        "activity_id": activity.id if activity is not None else None,
        "option_id": option_id,
        "inventory_role": semantics.inventory_role,
        "trigger": semantics.trigger,
        "qualification": semantics.qualification,
        "execution_phase": semantics.phase,
        "target_role": semantics.target_role,
        "once_per_turn": semantics.once_per_turn,
        "choice_group": semantics.choice_group,
        "automatic": semantics.automatic,
        "cost_model": {
            "resource_targets": [
                target.model_dump(mode="json") for target in activity.consumption.targets
            ]
            if activity is not None
            else [],
            "feature_uses": feature.uses.model_dump(mode="json") if feature.uses else None,
            "sneak_dice_cost": semantics.sneak_dice_cost,
            "inherit_damage_type": semantics.inherit_damage_type,
            "commit": "qualifying_final_hit" if executable else None,
        },
        "save": activity.save.model_dump(mode="json")
        if isinstance(activity, SaveActivity)
        else None,
        "effects": [binding.model_dump(mode="json") for binding in semantics.effects],
        "forced_movement": (
            semantics.forced_movement.model_dump(mode="json") if semantics.forced_movement else None
        ),
        "fully_executable": executable,
        "standalone_use_feature_rejected": standalone_rejected,
        "classification": classification,
        "deferred_reason": reason,
    }


def audit_document(loader: AssetLoader) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    activity_count = 0
    source_count = 0
    for slug in sorted(loader.list_slugs("features")):
        feature = loader.get_feature(slug)
        if feature is None or not (feature.attack_riders or feature.attack_rider_context):
            continue
        source_count += 1
        for activity in feature.activities:
            semantics = feature.attack_riders.get(activity.id)
            if semantics is None:
                continue
            activity_count += 1
            if semantics.deferred_options:
                for option_id, reason in semantics.deferred_options.items():
                    rows.append(
                        _row(
                            feature,
                            activity,
                            semantics,
                            option_id=option_id,
                            deferred_reason=reason,
                        )
                    )
            else:
                rows.append(_row(feature, activity, semantics))
        context = feature.attack_rider_context
        if context is not None:
            if context.related_activity_ids:
                by_id = {activity.id: activity for activity in feature.activities}
                for activity_id in context.related_activity_ids:
                    rows.append(_row(feature, by_id[activity_id], context))
            else:
                rows.append(_row(feature, None, context))
    return {
        "counts": dict(sorted(Counter(row["classification"] for row in rows).items())),
        "role_counts": dict(sorted(Counter(row["inventory_role"] for row in rows).items())),
        "feature_sources": source_count,
        "concrete_rider_activities": activity_count,
        "inventory_rows": len(rows),
        "inventory_notes": {
            "scope": "entire canonical feature corpus reviewed; item/feat riders remain excluded",
            "discovery": "ingestion-time exact feature/activity mappings; no runtime prose parsing",
            "support": (
                "standalone refusal is independent from authoritative attack-rider execution"
            ),
            "obscure": "canonical Dexterity save; Blinded until end of target's next turn",
            "incoming_hit": (
                "defensive reaction contexts are inventoried but not executed as outgoing riders"
            ),
        },
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    text = json.dumps(audit_document(BundledAssetLoader()), indent=2, ensure_ascii=False) + "\n"
    if args.output is not None:
        args.output.write_text(text, encoding="utf8")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
