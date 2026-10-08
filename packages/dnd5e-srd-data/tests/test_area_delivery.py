"""Reviewed area declarations, exact source drift gates and stable inventories."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError

from dnd5e_srd_data import (
    AreaSemantics,
    BundledAssetLoader,
    ForcedMovementSpec,
    SaveActivity,
    TargetCreatureFilter,
)
from dnd5e_srd_data.schema.common import TargetAffectsBlock, TargetBlock, TargetTemplateBlock
from tools.area_delivery_audit import area_audit_document, main, spell_audit_document
from tools.translators.area_delivery import apply_area_delivery, normalize_target_filters
from tools.translators.foundry import (
    translate_feature_yaml,
    translate_generic_item_yaml,
    translate_spell_yaml,
)

PACKAGE = Path(__file__).resolve().parents[1]
DOCS = PACKAGE.parents[1] / "docs/dev"


def test_canonical_save_policy_inventory_has_no_unknown_policy():
    policies = Counter()

    def visit(value):
        if isinstance(value, dict):
            if value.get("kind") == "save":
                policies[value["damage"]["on_save"]] += 1
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for path in sorted((PACKAGE / "src/dnd5e_srd_data/canonical").rglob("*.json")):
        visit(json.loads(path.read_text(encoding="utf8")))
    assert policies == {"half": 323, "none": 162, "full": 7}


def test_delivery_values_are_frozen_closed_and_old_documents_do_not_churn():
    area = AreaSemantics(origin_policy="actor")
    with pytest.raises(ValidationError):
        area.includes_origin = True
    for model, data in [
        (AreaSemantics, {"origin_policy": "nearest_enemy"}),
        (AreaSemantics, {"origin_policy": "actor", "radius": 60}),
        (TargetCreatureFilter, {"include_creature_types": ["Unknown"]}),
        (TargetCreatureFilter, {"arbitrary_rule": "Undead"}),
        (
            ForcedMovementSpec,
            {"trigger": "failed_save", "distance_ft": 0, "direction": "away_from_source"},
        ),
        (
            ForcedMovementSpec,
            {"trigger": "on_description", "distance_ft": 10, "direction": "away_from_source"},
        ),
        (
            ForcedMovementSpec,
            {"trigger": "hit", "distance_ft": 10, "direction": "arbitrary_vector"},
        ),
    ]:
        with pytest.raises(ValidationError):
            model.model_validate(data)
    dumped = SaveActivity().model_dump(mode="json")
    assert "forced_movement" not in dumped
    assert "area_semantics" not in dumped["target"]
    assert "creature_filter" not in dumped["target"]
    assert "deferred_reason" not in TargetCreatureFilter().model_dump(mode="json")


@pytest.mark.parametrize(
    "raw,types",
    [
        ("Undead", ("undead",)),
        ("Undead of your choice", ("undead",)),
        ("Elemental", ("elemental",)),
        ("Fiend or Undead", ("fiend", "undead")),
        (
            "Celestial, Elemental, Fey, Fiend, or Undead",
            ("celestial", "elemental", "fey", "fiend", "undead"),
        ),
        ("target is Humanoid", ("humanoid",)),
    ],
)
def test_exact_reviewed_creature_type_specials_normalize(raw, types):
    activity = SaveActivity(target=TargetBlock(affects=TargetAffectsBlock(special=raw)))
    [result] = normalize_target_filters([activity])
    assert result.target.creature_filter.include_creature_types == types
    assert activity.target.creature_filter is None
    assert result.target.affects.special == raw


def test_unknown_area_restrictions_fail_closed_without_broadening_named_legacy_rules():
    target = TargetBlock(affects=TargetAffectsBlock(special="unknown creatures"))
    named = SaveActivity(target=target)
    area = SaveActivity(
        target=target.model_copy(update={"template": TargetTemplateBlock(type="sphere", size="20")})
    )
    [plain, refused] = normalize_target_filters([named, area])
    assert plain is named
    assert refused.target.creature_filter.deferred_reason == "unreviewed_target_restriction"
    [empty] = normalize_target_filters([SaveActivity()])
    assert empty.target.creature_filter is None


@pytest.mark.parametrize(
    "kind,slug,activity_id,shape,size,role,origin,included",
    [
        (
            "spell",
            "slow",
            "dnd5eactivity000",
            "cube",
            "40",
            "target_area",
            "point_within_range",
            True,
        ),
        (
            "spell",
            "phantasmal-force",
            "5LLVXOLZf1fTstUw",
            "cube",
            "10",
            "effect_geometry",
            "point_within_range",
            False,
        ),
        ("spell", "thunderwave", "dnd5eactivity000", "cube", "15", "target_area", "actor", False),
        (
            "item",
            "dust-of-sneezing-and-choking",
            "GDzbsjaVxNM7nnBu",
            "radius",
            "30",
            "target_area",
            "actor",
            True,
        ),
        (
            "feature",
            "intimidating-presence",
            "ZRHT8mOlea6T8XpP",
            "radius",
            "30",
            "target_area",
            "actor",
            False,
        ),
        (
            "spell",
            "spirit-guardians",
            "dnd5eactivity000",
            "radius",
            "15",
            "target_area",
            "actor",
            False,
        ),
        (
            "spell",
            "stinking-cloud",
            "dnd5eactivity000",
            "sphere",
            "20",
            "target_area",
            "point_within_range",
            True,
        ),
    ],
)
def test_exact_reviewed_target_templates_reach_canonical(
    kind, slug, activity_id, shape, size, role, origin, included
):
    loader = BundledAssetLoader()
    source = getattr(loader, f"get_{kind}")(slug)
    activity = next(a for a in source.activities if a.id == activity_id)
    assert (activity.target.template.type, activity.target.template.size) == (shape, size)
    assert activity.target.area_semantics == AreaSemantics(
        template_role=role, origin_policy=origin, includes_origin=included
    )
    again = apply_area_delivery(kind, slug, source.description, source.activities)
    assert [a.model_dump(mode="json") for a in again] == [
        a.model_dump(mode="json") for a in source.activities
    ]


def test_dust_is_not_a_creature_exclusion_and_thunderwave_has_typed_movement():
    loader = BundledAssetLoader()
    dust = loader.get_item("dust-of-sneezing-and-choking").activities[0]
    assert dust.target.creature_filter == TargetCreatureFilter(
        auto_success_creature_types=("construct", "elemental", "ooze", "plant", "undead")
    )
    wave = loader.get_spell("thunderwave").activities[0]
    assert wave.forced_movement == ForcedMovementSpec(
        trigger="failed_save", distance_ft=10, direction="away_from_source"
    )
    for category, slug, aid in [
        ("feature", "sear-undead", "fW3U8L8Ca9a6O9Wx"),
        ("item", "helm-of-brilliance", "IFscsoX8eRdhxP5s"),
    ]:
        source = getattr(loader, f"get_{category}")(slug)
        activity = next(a for a in source.activities if a.id == aid)
        assert activity.target.creature_filter.include_creature_types == ("undead",)


@pytest.mark.parametrize(
    "kind,slug,translator",
    [
        ("spell", "slow", translate_spell_yaml),
        ("spell", "phantasmal-force", translate_spell_yaml),
        ("spell", "thunderwave", translate_spell_yaml),
        ("spell", "spirit-guardians", translate_spell_yaml),
        ("spell", "stinking-cloud", translate_spell_yaml),
        ("item", "dust-of-sneezing-and-choking", translate_generic_item_yaml),
        ("feature", "intimidating-presence", translate_feature_yaml),
    ],
)
def test_pinned_raw_sources_regenerate_the_reviewed_metadata(kind, slug, translator):
    source = getattr(BundledAssetLoader(), f"get_{kind}")(slug)
    pack_path = source.provenance.source_url.split("/packs/_source/", 1)[1]
    raw_path = PACKAGE / "raw_sources/foundry/packs/_source" / pack_path
    if not raw_path.is_file():
        pytest.skip("pinned upstream sources are maintainer-only inputs")
    translated = translator(
        raw_path,
        ingest_date=source.provenance.ingest_date,
        ingest_version=source.provenance.ingest_version,
    )
    assert {
        a.id: (a.target.area_semantics, a.target.creature_filter, a.forced_movement)
        for a in translated.activities
    } == {
        a.id: (a.target.area_semantics, a.target.creature_filter, a.forced_movement)
        for a in source.activities
    }


@pytest.mark.parametrize(
    "mutate", ["missing", "shape", "description", "kind", "save_ability", "push_clause"]
)
def test_exact_delivery_mappings_refuse_upstream_drift(mutate):
    wave = BundledAssetLoader().get_spell("thunderwave")
    activities = wave.activities
    description = wave.description
    if mutate == "missing":
        activities = []
    elif mutate == "shape":
        original = activities[0]
        target = original.target.model_copy(
            update={"template": TargetTemplateBlock(type="sphere", size="15")}
        )
        activities = [original.model_copy(update={"target": target})]
    elif mutate == "description":
        description = "unreviewed source"
    elif mutate == "kind":
        activities = [activities[0].model_copy(update={"kind": "damage"})]
    elif mutate == "save_ability":
        original = activities[0]
        activities = [
            original.model_copy(
                update={"save": original.save.model_copy(update={"ability": ["dex"]})}
            )
        ]
    else:
        description = description.replace("pushed 10 feet away from you", "moved in an unknown way")
    with pytest.raises(ValueError, match="source drift"):
        apply_area_delivery("spell", "thunderwave", description, activities)


def test_dust_autosuccess_mapping_requires_its_exact_reviewed_clause():
    dust = BundledAssetLoader().get_item("dust-of-sneezing-and-choking")
    with pytest.raises(ValueError, match="source drift"):
        apply_area_delivery(
            "item",
            dust.slug,
            dust.description.replace("Constructs, Elementals", "unknown"),
            dust.activities,
        )


def test_delivery_audits_are_byte_stable_checked_in_inventories():
    loader = BundledAssetLoader()
    for name, document in [
        ("area-delivery-audit.json", area_audit_document(loader)),
        ("spell-delivery-audit.json", spell_audit_document(loader)),
    ]:
        actual = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
        assert (DOCS / name).read_text(encoding="utf8") == actual
    area = area_audit_document(loader)
    assert area["line_width_counts"]["10"] >= 4
    source = next(
        row
        for row in area["rows"]
        if row["slug"] == "sunbeam" and row["activity_id"] == "o3kffaumfNGjVvg9"
    )
    assert source["runtime_status"] == "ongoing_source_with_immediate_payload"
    assert source["shape"] == "line" and source["size_ft"] == "60"
    assert source["ongoing_source"]["source_radius_ft"] == 30
    assert any(
        row["slug"] == "confusion" and row["runtime_status"] == "unsupported_formula_size"
        for row in area["rows"]
    )
    delegated = spell_audit_document(loader)
    wand = next(row for row in delegated["rows"] if row["slug"] == "wand-of-fireballs")
    assert wand["child_spell_slug"] == "fireball"
    assert wand["fixed_save_dc"] == 15
    assert wand["child_area"][0]["shape"] == "sphere"
    assert wand["child_area"][0]["size_ft"] == "20"


def test_inventory_cli_writes_both_documents(tmp_path, monkeypatch):
    area, spell = tmp_path / "area.json", tmp_path / "spell.json"
    monkeypatch.setattr(
        sys, "argv", ["area-audit", "--area-output", str(area), "--spell-output", str(spell)]
    )
    main()
    assert area.read_bytes() == (DOCS / "area-delivery-audit.json").read_bytes()
    assert spell.read_bytes() == (DOCS / "spell-delivery-audit.json").read_bytes()
