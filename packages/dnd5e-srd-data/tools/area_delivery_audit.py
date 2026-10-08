"""Deterministic canonical area and delegated-spell inventories.

This inventory measures delivery support, not complete item/spell mechanics.
Run ``python -m tools.area_delivery_audit --area-output PATH --spell-output PATH``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from dnd5e_srd_data import AssetLoader, BundledAssetLoader, CastActivity
from dnd5e_srd_data.loader import Category
from dnd5e_srd_data.schema.common import Activity

_DEFAULTS = {
    "sphere": ("point_within_range", True),
    "circle": ("point_within_range", True),
    "cylinder": ("point_within_range", True),
    "square": ("point_within_range", True),
    "radius": ("actor", False),
    "cube": ("actor", False),
    "cone": ("actor", False),
    "line": ("actor", False),
}
_RESOLVED_KINDS = frozenset({"save", "damage", "heal"})
_BUCKETS = ("actions", "legendary_actions", "lair_actions", "special_abilities")


def _activities(loader: AssetLoader) -> Iterator[tuple[str, BaseModel, str | None, Activity]]:
    getters: tuple[tuple[str, Category, Callable[[str], BaseModel | None]], ...] = (
        ("spell", "spells", loader.get_spell),
        ("item", "items", loader.get_item),
        ("feature", "features", loader.get_feature),
        ("monster", "monsters", loader.get_monster),
    )
    for kind, category, getter in getters:
        for slug in sorted(loader.list_slugs(category)):
            if (source := getter(slug)) is None:
                continue
            if kind != "monster":
                for activity in getattr(source, "activities", ()):
                    yield kind, source, None, activity
                continue
            for bucket in _BUCKETS:
                for action in getattr(source, bucket):
                    for activity in action.activities:
                        yield kind, source, action.slug, activity


def _positive_integer(value: str) -> int | None:
    return int(value) if value.isdigit() and int(value) > 0 else None


def _area_row(
    kind: str, source: BaseModel, action_slug: str | None, activity: Activity
) -> dict[str, Any]:
    template = activity.target.template
    semantics = activity.target.area_semantics
    predicate = activity.target.creature_filter
    default = _DEFAULTS.get(template.type)
    role = semantics.template_role if semantics else "target_area"
    origin = semantics.origin_policy if semantics else default[0] if default else None
    includes = semantics.includes_origin if semantics else default[1] if default else None
    reason = None
    if role == "effect_geometry":
        status = "effect_geometry_only"
    elif template.type == "wall":
        status, reason = "unsupported_wall", "wall_geometry"
    elif _positive_integer(template.size) is None:
        status, reason = "unsupported_formula_size", "nonconstant_positive_area_size"
    elif default is None:
        status, reason = "missing_origin_semantics", "unsupported_template_shape"
    elif template.units not in ("ft", ""):
        status, reason = "unsupported_units", "area_units_not_feet"
    elif predicate is not None and predicate.deferred_reason:
        status, reason = "creature_filter_deferred", predicate.deferred_reason
    elif activity.persistent_area is not None and activity.persistent_area.environment is not None:
        status = "persistent_environment"
    elif activity.kind not in _RESOLVED_KINDS:
        status = "non_resolving_template"
    elif activity.persistent_area is not None:
        status = "persistent_area"
    elif activity.timing.trigger == "manual":
        status, reason = "manual_activity", "activity_timing_requires_manual_resolution"
    else:
        status = "executable_target_area"
    count = _positive_integer(activity.target.affects.count)
    if activity.target.affects.type in {"self", "object", "space"}:
        count = None
    return {
        "source_kind": kind,
        "slug": source.slug,
        "action_slug": action_slug,
        "activity_id": activity.id,
        "activity_kind": activity.kind,
        "shape": template.type,
        "size_ft": template.size,
        "width_ft": template.width,
        "role": role,
        "origin_policy": origin,
        "includes_origin": includes,
        "explicit_area_semantics": semantics is not None,
        "counted_target": count,
        "creature_filter": predicate.model_dump(mode="json") if predicate else None,
        "forced_movement": activity.forced_movement.model_dump(mode="json")
        if activity.forced_movement
        else None,
        "runtime_status": status,
        "deferred_reason": reason,
    }


def area_audit_document(loader: AssetLoader) -> dict[str, Any]:
    rows = [
        _area_row(kind, source, action, activity)
        for kind, source, action, activity in _activities(loader)
        if activity.target.template.type
    ]
    lines = [row for row in rows if row["shape"] == "line"]
    return {
        "inventory_rows": len(rows),
        "counts": dict(sorted(Counter(row["runtime_status"] for row in rows).items())),
        "source_counts": dict(sorted(Counter(row["source_kind"] for row in rows).items())),
        "line_width_counts": dict(
            sorted(Counter(row["width_ft"] or "default_5" for row in lines).items())
        ),
        "line_rows": len(lines),
        "inventory_notes": {
            "authority": (
                "typed activity metadata overrides conservative shape "
                "defaults; runtime reads no prose"
            ),
            "scope": (
                "delivery targeting support only; complete ongoing "
                "spell, feature and item mechanics are separate"
            ),
            "dust": (
                "creator inclusion and typed automatic saves resolved; "
                "suffocation and Lesser Restoration removal deferred"
            ),
            "preserve_life": "distributed healing pool and per-target half-max cap remain deferred",
            "line_width": (
                "canonical ancient dragon breath has real 10-ft-wide "
                "Lines; width must reach rasterization"
            ),
            "unsupported": "walls, formula-sized areas, 3D and elevation remain deferred",
        },
        "rows": rows,
    }


def spell_audit_document(loader: AssetLoader) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for kind, source, action, activity in _activities(loader):
        if not isinstance(activity, CastActivity):
            continue
        child = loader.get_spell_by_uuid(activity.spell.uuid)
        child_areas = (
            [
                _area_row("spell", child, None, candidate)
                for candidate in child.activities
                if candidate.target.template.type
            ]
            if child
            else []
        )
        unsupported = [
            row["runtime_status"] for row in child_areas if row["deferred_reason"] is not None
        ]
        reason = (
            "unresolved_child_spell"
            if child is None
            else "unsupported_child_geometry"
            if unsupported
            else None
        )
        if reason is None and kind == "item" and child is not None and child.concentration:
            reason = "item_concentration_deferred"
        rows.append(
            {
                "source_kind": kind,
                "slug": source.slug,
                "action_slug": action,
                "activity_id": activity.id,
                "referenced_uuid": activity.spell.uuid,
                "referenced_spell_resolvable": child is not None,
                "child_spell_slug": child.slug if child else None,
                "child_area": child_areas,
                "cast_level": activity.spell.level,
                "challenge_override": activity.spell.challenge.override,
                "fixed_save_dc": activity.spell.challenge.save,
                "fixed_attack_bonus": activity.spell.challenge.attack,
                "concentration": child.concentration if child else None,
                "persistent_area": any(
                    candidate.persistent_area is not None for candidate in child.activities
                )
                if child
                else False,
                "timed_activity": any(
                    candidate.timing.trigger != "immediate" for candidate in child.activities
                )
                if child
                else False,
                "unsupported_child_geometry": sorted(set(unsupported)),
                "runtime_status": "deferred_delivery" if reason else "resolvable_child_delivery",
                "deferred_reason": reason,
            }
        )
    return {
        "inventory_rows": len(rows),
        "counts": dict(sorted(Counter(row["runtime_status"] for row in rows).items())),
        "source_counts": dict(sorted(Counter(row["source_kind"] for row in rows).items())),
        "producer_counts": {
            "referenced_spell_resolvable": sum(row["referenced_spell_resolvable"] for row in rows),
            "child_area": sum(bool(row["child_area"]) for row in rows),
            "fixed_save_dc": sum(row["fixed_save_dc"] is not None for row in rows),
            "fixed_attack_bonus": sum(row["fixed_attack_bonus"] is not None for row in rows),
            "concentration": sum(bool(row["concentration"]) for row in rows),
            "persistent_area": sum(row["persistent_area"] for row in rows),
            "timed_activity": sum(row["timed_activity"] for row in rows),
            "unsupported_child_geometry": sum(
                bool(row["unsupported_child_geometry"]) for row in rows
            ),
        },
        "inventory_notes": {
            "authority": (
                "CastActivity child joins use exact canonical Foundry "
                "UUID; unresolved children fail closed"
            ),
            "scope": (
                "resolvable_child_delivery records structural delivery "
                "capability, not complete child spell or item support"
            ),
            "source_chain": (
                "item or monster invocation, child spell, and child "
                "activity retain separate deterministic identities"
            ),
        },
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--area-output", type=Path)
    parser.add_argument("--spell-output", type=Path)
    args = parser.parse_args()
    loader = BundledAssetLoader()
    for path, document in (
        (args.area_output, area_audit_document(loader)),
        (args.spell_output, spell_audit_document(loader)),
    ):
        rendered = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
        if path is None:
            sys.stdout.write(rendered)
        else:
            path.write_text(rendered, encoding="utf8")


if __name__ == "__main__":
    main()
