"""Deterministic canonical reaction inventory: python -m dnd5e_engine.reaction_audit.

The inventory includes inherited typed conditions independently of activation
type, unsupported reaction prose, and reaction spells without any activities.
Its support decisions reuse the executable pre-arm contract; no prose is
interpreted as runtime semantics and no random draws occur.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

from dnd5e_srd_data.loader import AssetLoader, BundledAssetLoader, Category
from dnd5e_srd_data.schema.common import Activity
from dnd5e_srd_data.schema.spell import Spell
from pydantic import BaseModel

from dnd5e_engine.reactions import SUPPORTED_OPPORTUNITY_KINDS, reaction_support


def _source_documents(loader: AssetLoader) -> Iterator[tuple[str, str, BaseModel]]:
    getters: tuple[tuple[str, Category, Callable[[str], BaseModel | None]], ...] = (
        ("feature", "features", loader.get_feature),
        ("item", "items", loader.get_item),
        ("monster", "monsters", loader.get_monster),
        ("spell", "spells", loader.get_spell),
    )
    for kind, category, getter in getters:
        for slug in sorted(loader.list_slugs(category)):
            source = getter(slug)
            if source is not None:
                yield kind, slug, source


def _activities(value: object, path: str = "") -> Iterator[tuple[Activity, str]]:
    """Walk typed source fields, retaining nested monster-action identities."""
    if isinstance(value, BaseModel):
        fields = type(value).model_fields
        if "activation" in fields and "kind" in fields:
            yield cast(Activity, value), path
            return
        for name in fields:
            yield from _activities(getattr(value, name), f"{path}/{name}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            yield from _activities(child, f"{path}/{index}")


def _audit_activity(source_kind: str, slug: str, activity: Activity, path: str) -> dict[str, Any]:
    conditions = activity.activation.reaction_conditions
    kinds = list(dict.fromkeys(condition.kind.value for condition in conditions))
    producer_exists = bool(conditions) and all(
        condition.kind in SUPPORTED_OPPORTUNITY_KINDS for condition in conditions
    )
    reason = reaction_support(activity, source_kind=source_kind)
    if reason is None:
        classification = "executable"
    elif conditions and not producer_exists:
        classification = "typed_but_no_opportunity_producer"
    else:
        classification = "unsupported_deferred"
    return {
        "source_kind": source_kind,
        "source_slug": slug,
        "activity_id": activity.id,
        "path": path,
        "activation_type": activity.activation.type,
        "activation_condition": activity.activation.condition,
        "reaction_conditions": [condition.model_dump(mode="json") for condition in conditions],
        "trigger_kinds": kinds,
        "producer_exists": producer_exists,
        "target_derivation_exists": activity.reaction is not None,
        "reaction": activity.reaction.model_dump(mode="json") if activity.reaction else None,
        "fully_executable": reason is None,
        "classification": classification,
        "deferred_reason": reason,
    }


def audit_document(loader: AssetLoader) -> dict[str, Any]:
    """Return a stable, complete inventory using the runtime support contract."""
    rows: list[dict[str, Any]] = []
    for source_kind, slug, source in _source_documents(loader):
        activities = list(_activities(source))
        reaction_spell = isinstance(source, Spell) and source.casting_time.unit == "reaction"
        for activity, path in activities:
            if (
                reaction_spell
                or activity.activation.reaction_conditions
                or activity.activation.type == "reaction"
            ):
                rows.append(_audit_activity(source_kind, slug, activity, path))
        if reaction_spell and not activities:
            assert isinstance(source, Spell)
            rows.append(
                {
                    "source_kind": source_kind,
                    "source_slug": slug,
                    "activity_id": None,
                    "path": "/activities",
                    "activation_type": "reaction",
                    "activation_condition": source.casting_time.condition,
                    "reaction_conditions": [],
                    "trigger_kinds": [],
                    "producer_exists": False,
                    "target_derivation_exists": False,
                    "reaction": None,
                    "fully_executable": False,
                    "classification": "unsupported_deferred",
                    "deferred_reason": "reaction spell has no canonical activity",
                }
            )
    return {
        "counts": dict(sorted(Counter(row["classification"] for row in rows).items())),
        "source_counts": dict(sorted(Counter(row["source_kind"] for row in rows).items())),
        "reaction_activities": len(rows),
        "typed_activities": sum(bool(row["reaction_conditions"]) for row in rows),
        "typed_conditions": sum(len(row["reaction_conditions"]) for row in rows),
        "unrecognized_reaction_prose_activities": sum(
            bool(row["activation_condition"]) and not row["reaction_conditions"] for row in rows
        ),
        "empty_reaction_condition_activities": sum(
            not row["activation_condition"] and not row["reaction_conditions"] for row in rows
        ),
        "inventory_notes": {
            "absorb_elements": {
                "present_in_canonical": "absorb-elements" in loader.list_slugs("spells"),
                "scope": "inventory presence only; no damage-type reaction trigger is inferred",
            },
            "unrecognized_prose": (
                "Unmatched or empty activation prose has no typed trigger contract. Compound "
                "weapon, damage-type, save-result, and movement qualifiers remain deferred; "
                "runtime does not reduce them to an unqualified trigger."
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
