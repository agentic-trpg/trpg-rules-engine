"""Ingestion-only canonical lifecycle inventory; runtime never reads prose.

Run from the data package: python -m tools.effect_lifecycle_audit --output PATH.
Discovery deliberately includes unreviewed prose candidates and display-only
condition durations, but neither grants executable runtime semantics. Exact
typed bindings are the sole authority for executable lifecycle records.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from dnd5e_srd_data.loader import AssetLoader, BundledAssetLoader, Category
from dnd5e_srd_data.schema.common import Activity, AppliedEffectRef, PassiveEffect
from dnd5e_srd_data.schema.feature import Feature
from dnd5e_srd_data.schema.lifecycle import EffectLifecycleSpec

_REPEAT = re.compile(
    r"\brepeats?\b[^.\n]{0,100}\b(?:sav(?:e|ing throw)|it)\b"
    r"|\b(?:save|saving throw)\b[^.\n]{0,100}\brepeats?\b"
    r"|\bat the (?:end|start)\b[^.\n]{0,120}\b(?:save|saving throw)\b",
    re.IGNORECASE,
)
_DAMAGE_BREAK = re.compile(
    r"(?:ends?[^.\n]{0,90}(?:takes?|take) (?:any )?damage)"
    r"|(?:until[^.\n]{0,45}(?:takes?|take) (?:any )?damage)"
    r"|(?:(?:wakes?|awakens?)[^.\n]{0,50}(?:takes?|take) (?:any )?damage)"
    r"|(?:(?:takes?|take) (?:any )?damage[^.\n]{0,60}(?:ends?|disappears?))",
    re.IGNORECASE,
)
_ONE_USE = re.compile(r"next (?:saving throw|save)\b", re.IGNORECASE)
_REVIEWED_REASONS = {
    "befuddlement": "repeat saves use a 30-day calendar interval and the complete "
    "altered-stat contract",
    "blindness-deafness": "requires a validated choice of Blinded or Deafened; the "
    "canonical activity binds both effects",
    "burnt-othur-fumes": "requires target-turn-start damage, three successful saves and "
    "poison exposure semantics",
    "compulsion": "repeat save occurs after the prescribed movement, not at every target turn end",
    "confusion": "requires turn-start behavior rolls and the complete action/Bonus "
    "Action/Reaction restrictions",
    "contagion": "requires multiple-save progression and disease-specific condition/healing "
    "semantics",
    "crawler-mucus": "canonical condition effect is not bound to the save activity; "
    "exposure and item semantics remain deferred",
    "dominate-beast": "repeat save occurs on damage, not turn end; damage-triggered saves "
    "and command/control semantics remain deferred",
    "dominate-monster": "repeat save occurs on damage, not turn end; damage-triggered saves "
    "and command/control semantics remain deferred",
    "dominate-person": "repeat save occurs on damage, not turn end; damage-triggered saves "
    "and command/control semantics remain deferred",
    "dust-of-sneezing-and-choking": "requires complete Suffocation and Lesser Restoration "
    "removal semantics",
    "flesh-to-stone": "requires three-success/three-failure progression and transformation "
    "into Petrified",
    "hideous-laughter": "also repeats on damage with Advantage and prevents voluntary Prone "
    "removal; a turn-end fragment is insufficient",
    "hypnotic-pattern": "requires damage-break and explicit action-to-awaken lifecycle; "
    "concentration alone does not create repeat saves",
    "irresistible-dance": "repeat save requires an Action; successful initial save has a "
    "separate next-turn effect",
    "ivory-goats": "requires summoned-goat fear source, immunity-on-success and item "
    "activation semantics",
    "mace-of-terror": "requires forced fleeing, approach restrictions and the restricted "
    "action contract",
    "pale-tincture": "requires 24-hour saves, seven-success progression, recurring damage "
    "and healing restrictions",
    "pipes-of-haunting": "requires immunity-on-success and complete item "
    "activation/resource semantics",
    "ray-of-enfeeblement": "requires separate initial-success/initial-failure effects, "
    "Strength-test modifiers and damage penalties",
    "sleep": "requires staged second-save progression, Unconscious transition and "
    "action-to-awaken semantics",
    "slow": "dedicated Slow marks and their complete budget/modifier consumers require an "
    "explicit lifecycle migration",
    "sunburst": "repeat clause reviewed but not migrated to an exact typed effect binding "
    "in this batch",
    "weird": "failed repeats deal Psychic damage; a save-to-expire fragment is insufficient",
}


def _sources(loader: AssetLoader) -> Iterator[tuple[str, BaseModel]]:
    getters: tuple[tuple[str, Category, Callable[[str], BaseModel | None]], ...] = (
        ("spell", "spells", loader.get_spell),
        ("feature", "features", loader.get_feature),
        ("item", "items", loader.get_item),
    )
    for kind, category, getter in getters:
        for slug in sorted(loader.list_slugs(category)):
            if (source := getter(slug)) is not None:
                yield kind, source


def _candidate_flags(source: BaseModel) -> dict[str, bool]:
    # This is an ingestion audit, not a runtime support predicate.
    text = getattr(source, "description", "")
    return {
        "repeat_save_candidate": bool(_REPEAT.search(text)),
        "damage_break_candidate": bool(_DAMAGE_BREAK.search(text)),
        "one_use_modifier_candidate": bool(_ONE_USE.search(text)),
    }


def _row(
    kind: str,
    source: BaseModel,
    *,
    activity_id: str | None,
    effect_id: str | None,
    path: str,
    spec: EffectLifecycleSpec | None,
    consumer: str | None = None,
    display_duration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    flags = _candidate_flags(source)
    if spec is not None:
        classification = (
            "executable_typed_lifecycle"
            if spec.repeat_save or spec.expire_on_positive_damage or spec.one_use_modifiers
            else "typed_duration_only"
        )
        reason = None
    else:
        classification = (
            "repeated_save_candidate"
            if flags["repeat_save_candidate"]
            else "damage_break_candidate"
            if flags["damage_break_candidate"]
            else "one_use_modifier_candidate"
            if flags["one_use_modifier_candidate"]
            else "deferred"
        )
        reason = _REVIEWED_REASONS.get(
            source.slug,
            "no reviewed typed lifecycle binding; display duration or prose alone "
            "does not authorize execution",
        )
    return {
        "source_kind": kind,
        "source_slug": source.slug,
        "activity_id": activity_id,
        "effect_id": effect_id,
        "path": path,
        "lifecycle": spec.model_dump(mode="json") if spec is not None else None,
        "consumer": consumer,
        **flags,
        "finite_reviewed_duration": spec is not None and spec.maximum_rounds is not None,
        "display_duration": display_duration,
        "fully_executable": spec is not None,
        "classification": classification,
        "deferred_reason": reason,
    }


def _bound_refs(source: BaseModel) -> Iterator[tuple[Activity, AppliedEffectRef, str]]:
    for activity_index, activity in enumerate(getattr(source, "activities", ())):
        for ref_index, ref in enumerate(getattr(activity, "effects", ())):
            yield activity, ref, f"/activities/{activity_index}/effects/{ref_index}"


def _typed_rows(kind: str, source: BaseModel) -> tuple[list[dict[str, Any]], set[str]]:
    rows = []
    managed: set[str] = set()
    for activity, ref, path in _bound_refs(source):
        spec = ref.lifecycle
        consumer = "typed_effect_lifecycle"
        if spec is None and isinstance(source, Feature):
            rider = source.attack_riders.get(activity.id)
            binding = (
                next((e for e in rider.effects if e.effect_id == ref.id), None) if rider else None
            )
            if binding is not None and binding.expiry != "none":
                spec = EffectLifecycleSpec(expiry_boundary=binding.expiry)
                consumer = "existing_typed_rider_boundary"
        reaction = activity.reaction
        if spec is None and reaction and reaction.effect_expiry == "owner_next_turn_start":
            spec = EffectLifecycleSpec(expiry_boundary="source_next_turn_start")
            consumer = "existing_typed_reaction_boundary"
        if spec is not None:
            managed.add(ref.id)
            rows.append(
                _row(
                    kind,
                    source,
                    activity_id=activity.id,
                    effect_id=ref.id,
                    path=path,
                    spec=spec,
                    consumer=consumer,
                )
            )
    if isinstance(source, Feature) and source.attack_rider_context is not None:
        for index, binding in enumerate(source.attack_rider_context.effects):
            if binding.lifecycle is None or binding.effect_id in managed:
                continue
            managed.add(binding.effect_id)
            rows.append(
                _row(
                    kind,
                    source,
                    activity_id=None,
                    effect_id=binding.effect_id,
                    path=f"/attack_rider_context/effects/{index}",
                    spec=binding.lifecycle,
                    consumer="typed_attack_foundation_lifecycle",
                )
            )
    return rows, managed


def _display_rows(kind: str, source: BaseModel, managed: set[str]) -> list[dict[str, Any]]:
    rows = []
    refs = list(_bound_refs(source))
    for index, effect in enumerate(getattr(source, "passive_effects", ())):
        assert isinstance(effect, PassiveEffect)
        if effect.id in managed or not (effect.statuses and effect.duration):
            continue
        bindings = [(a.id, path) for a, ref, path in refs if ref.id == effect.id]
        for activity_id, path in bindings or [(None, f"/passive_effects/{index}")]:
            rows.append(
                _row(
                    kind,
                    source,
                    activity_id=activity_id,
                    effect_id=effect.id,
                    path=path,
                    spec=None,
                    display_duration=effect.duration,
                )
            )
    return rows


def audit_document(loader: AssetLoader) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for kind, source in _sources(loader):
        typed, managed = _typed_rows(kind, source)
        candidates = _display_rows(kind, source, managed)
        rows.extend([*typed, *candidates])
        if not (typed or candidates) and any(_candidate_flags(source).values()):
            rows.append(
                _row(kind, source, activity_id=None, effect_id=None, path="/description", spec=None)
            )
    return {
        "counts": dict(sorted(Counter(row["classification"] for row in rows).items())),
        "inventory_rows": len(rows),
        "source_counts": dict(sorted(Counter(row["source_kind"] for row in rows).items())),
        "producer_counts": {
            "typed_repeat_save": sum(
                bool(row["lifecycle"] and row["lifecycle"]["repeat_save"]) for row in rows
            ),
            "typed_expire_on_positive_damage": sum(
                bool(row["lifecycle"] and row["lifecycle"]["expire_on_positive_damage"])
                for row in rows
            ),
            "finite_reviewed_duration": sum(row["finite_reviewed_duration"] for row in rows),
            "typed_one_use_modifier": sum(
                bool(row["lifecycle"] and row["lifecycle"]["one_use_modifiers"]) for row in rows
            ),
            "supported_lifecycle": sum(row["fully_executable"] for row in rows),
            "deferred": sum(not row["fully_executable"] for row in rows),
        },
        "inventory_notes": {
            "authority": "only reviewed typed metadata grants lifecycle "
            "execution; concentration and failed saves do not "
            "imply repetition",
            "discovery": "ingestion-only prose candidate discovery plus exact "
            "typed refs and finite condition display durations; "
            "runtime reads no prose",
            "duration": "deadline is applied_round + maximum_rounds; target "
            "turn end checks repeat first, then duration; "
            "application round is not a full round",
            "one_use": "Staggering binds next_save_disadvantage; Sundering binds "
            "next_attack_bonus_other_creature with other-creature scope and "
            "a nonstacking bonus group; consumption removes only the used clause",
            "stacking": "Hamstring uses the typed latest_only target group "
            "across all sources; next boundaries use exact source identity",
            "scope": "canonical spells/features/items; lifecycle support is "
            "distinct from complete spell/feature/item execution",
            "stable_boundaries": "existing typed Shield and rider boundaries "
            "remain executable under their current "
            "adapters",
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
