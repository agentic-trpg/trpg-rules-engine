"""Exact lifecycle contracts, deterministic ingestion and the reviewed audit."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.common import AppliedEffectRef, UtilityActivity
from dnd5e_srd_data.schema.feature import AttackRiderSemantics, RiderEffectSpec
from dnd5e_srd_data.schema.lifecycle import EffectLifecycleSpec, RepeatSaveSpec
from tools.effect_lifecycle_audit import audit_document, main
from tools.translators.effect_lifecycle import (
    effect_lifecycle_activities,
    effect_lifecycle_effects,
    reviewed_effect_lifecycle,
)
from tools.translators.foundry import translate_feature_yaml, translate_spell_yaml

PACKAGE = Path(__file__).resolve().parents[1]
GOLDEN = PACKAGE.parents[1] / "docs/dev/effect-lifecycle-audit.json"


@pytest.mark.parametrize(
    "kind,slug,activity_id,effect_id,rounds,damage_break",
    [
        ("spell", "hold-person", "dnd5eactivity000", "PklwZi3SKQ3JU2M2", None, False),
        ("spell", "hold-monster", "dnd5eactivity000", "vC7qgyy8cjgi3t0T", None, False),
        ("feature", "cunning-strike", "n64fvJMT9fPUy7DH", "RazTM6biKVtNtjEW", 10, False),
        ("feature", "devious-strikes", "3eq7lcmpkJJBU2KO", "C2IGgt4PnRMxZVey", 10, True),
        ("feature", "intimidating-presence", "ZRHT8mOlea6T8XpP", "3LofAPFLZJMQJirN", 10, False),
    ],
)
def test_reviewed_producers_have_exact_triggering_save_bindings(
    kind, slug, activity_id, effect_id, rounds, damage_break
):
    loader = BundledAssetLoader()
    source = loader.get_spell(slug) if kind == "spell" else loader.get_feature(slug)
    activity = next(a for a in source.activities if a.id == activity_id)
    ref = next(ref for ref in activity.effects if ref.id == effect_id)
    assert ref.on_save is False
    assert ref.lifecycle.repeat_save == RepeatSaveSpec()
    assert ref.lifecycle.maximum_rounds == rounds
    assert ref.lifecycle.expire_on_positive_damage is damage_break
    assert ref.lifecycle.expiry_boundary is None
    assert ref.lifecycle == reviewed_effect_lifecycle(kind, slug, activity_id, effect_id)
    if kind == "feature" and slug != "intimidating-presence":
        rider = source.attack_riders[activity_id]
        [binding] = rider.effects
        assert binding.effect_id == effect_id and binding.outcome == "failure"
        assert binding.expiry == "none" and binding.lifecycle == ref.lifecycle
        assert rider.deferred_reason is None
        assert activity.save.ability == ["con"] and activity.save.dc.calculation == "dex"


def test_poison_uses_real_carried_item_identity_and_ip_owns_its_one_use_pool():
    loader = BundledAssetLoader()
    poison = loader.get_feature("cunning-strike").attack_riders["n64fvJMT9fPUy7DH"]
    assert poison.requires_carried_items == ("poisoners-kit",)
    assert loader.get_item("poisoners-kit") is not None
    presence = loader.get_feature("intimidating-presence")
    assert presence.uses.max == "1"
    assert presence.uses.recovery[0].period == "lr"
    initial, recharge = presence.activities
    assert initial.consumption.targets[0].target == ""
    assert initial.consumption.targets[0].value == "1"
    assert initial.save.dc.formula == "8 + @abilities.str.mod + @prof"
    assert presence.passive_effects[0].duration == {"rounds": 10}
    assert recharge.activation.type == ""
    assert recharge.consumption.targets[1].value == "-1"


@pytest.mark.parametrize("slug", ["dominate-beast", "dominate-person", "dominate-monster"])
def test_damage_repeat_dominate_spells_never_gain_end_turn_semantics(slug):
    spell = BundledAssetLoader().get_spell(slug)
    assert spell.concentration
    assert any(effect.statuses for effect in spell.passive_effects)
    assert all(ref.lifecycle is None for activity in spell.activities for ref in activity.effects)
    rows = [
        row for row in audit_document(BundledAssetLoader())["rows"] if row["source_slug"] == slug
    ]
    assert rows
    assert all(row["classification"] == "repeated_save_candidate" for row in rows)
    assert all("on damage, not turn end" in row["deferred_reason"] for row in rows)


def test_lifecycle_ingestion_requires_exact_source_activity_and_effect_identity():
    spell = BundledAssetLoader().get_spell("hold-person")
    original = spell.activities[0].model_copy(
        update={"effects": [AppliedEffectRef(id="PklwZi3SKQ3JU2M2", on_save=False)]}
    )
    before = original.model_dump(mode="json")
    assert (
        reviewed_effect_lifecycle("feature", "hold-person", original.id, "PklwZi3SKQ3JU2M2") is None
    )
    assert reviewed_effect_lifecycle("spell", "hold-person", "unknown", "PklwZi3SKQ3JU2M2") is None
    assert reviewed_effect_lifecycle("spell", "hold-person", original.id, "unknown") is None
    assert (
        effect_lifecycle_activities("spell", "unknown", [original])[0].effects[0].lifecycle is None
    )
    [mapped] = effect_lifecycle_activities("spell", "hold-person", [original])
    assert mapped.effects[0].lifecycle.repeat_save == RepeatSaveSpec()
    assert original.model_dump(mode="json") == before
    assert effect_lifecycle_activities("spell", "hold-person", [UtilityActivity(id="unknown")]) == [
        UtilityActivity(id="unknown")
    ]
    assert (
        effect_lifecycle_effects("spell", "hold-person", spell.passive_effects)
        == spell.passive_effects
    )
    first = reviewed_effect_lifecycle("spell", "hold-person", original.id, "PklwZi3SKQ3JU2M2")
    second = reviewed_effect_lifecycle("spell", "hold-person", original.id, "PklwZi3SKQ3JU2M2")
    assert first is not second and first.repeat_save is not second.repeat_save


@pytest.mark.parametrize(
    "raw",
    [
        {"repeat_save": {"phase": "damage"}},
        {"repeat_save": {"save_source": "host_dc"}},
        {"repeat_save": {"on_success": "drop_all_concentration"}},
        {"repeat_save": {"ability": "wis"}},
        {"expiry_boundary": "whenever_convenient"},
        {"maximum_rounds": 0},
        {"maximum_rounds": -1},
        {"one_use_modifiers": ["all_saves"]},
        {"duration_from_description": True},
        {"stacking": "add_all"},
        {"next_attack_scope": "anybody"},
    ],
)
def test_lifecycle_metadata_is_closed(raw):
    with pytest.raises(ValidationError):
        EffectLifecycleSpec.model_validate(raw)


def test_new_metadata_is_additive_and_omitted_from_unreviewed_bindings():
    ref = AppliedEffectRef(id="x")
    binding = RiderEffectSpec(effect_id="x")
    semantics = AttackRiderSemantics(
        trigger="final_hit", qualification="any_attack", phase="after_damage"
    )
    assert "lifecycle" not in ref.model_dump(mode="json")
    assert "lifecycle" not in binding.model_dump(mode="json")
    assert "requires_carried_items" not in semantics.model_dump(mode="json")
    spec = EffectLifecycleSpec(
        expiry_boundary="source_next_turn_start", one_use_modifiers=("next_save_disadvantage",)
    )
    ref = ref.model_copy(update={"lifecycle": spec})
    assert AppliedEffectRef.model_validate_json(ref.model_dump_json()) == ref


@pytest.mark.parametrize(
    "slug",
    [
        "hold-person",
        "hold-monster",
        "cunning-strike",
        "devious-strikes",
        "intimidating-presence",
        "reckless-attack",
        "brutal-strike",
        "improved-brutal-strike",
        "improved-brutal-strike-2",
        "frenzy",
    ],
)
def test_reviewed_canonical_documents_reproduce_the_pinned_translator(slug):
    loader = BundledAssetLoader()
    entry = loader.get_spell(slug) if slug.startswith("hold-") else loader.get_feature(slug)
    provenance = entry.provenance
    # Exact pinned inputs are checked in: this regression must run on a clean checkout.
    snapshot = PACKAGE / "tests/fixtures/foundry"
    manifest = json.loads((snapshot / "snapshot.json").read_text(encoding="utf-8"))
    pins = json.loads((PACKAGE / "raw_sources/PINS.json").read_text(encoding="utf-8"))
    assert manifest["commit"] == pins["foundry"]["commit"]
    relative = provenance.source_url.split("/packs/", 1)[1]
    raw = snapshot / "packs" / relative
    assert manifest["files"][relative]["source_url"] == provenance.source_url
    assert hashlib.sha256(raw.read_bytes()).hexdigest() == manifest["files"][relative]["sha256"]
    translator = translate_spell_yaml if slug.startswith("hold-") else translate_feature_yaml
    translated = translator(
        raw, ingest_date=provenance.ingest_date, ingest_version=provenance.ingest_version
    )
    assert translated.model_dump(mode="json") == entry.model_dump(mode="json")


def test_lifecycle_audit_is_deterministic_matches_golden_and_keeps_deferrals_explicit():
    first = audit_document(BundledAssetLoader())
    second = audit_document(BundledAssetLoader())
    assert json.dumps(first).encode() == json.dumps(second).encode()
    assert first == json.loads(GOLDEN.read_text(encoding="utf8"))
    assert first["producer_counts"] == {
        "typed_repeat_save": 5,
        "typed_expire_on_positive_damage": 1,
        "finite_reviewed_duration": 31,
        "typed_one_use_modifier": 2,
        "supported_lifecycle": 44,
        "deferred": 144,
    }
    assert {row["source_kind"] for row in first["rows"]} == {"spell", "feature", "item"}
    assert first["inventory_rows"] == 188
    beams = [row for row in first["rows"] if row["source_slug"] == "sunbeam"]
    assert len(beams) == 2
    assert all(
        row["lifecycle"]["expiry_boundary"] == "source_next_turn_start" and row["fully_executable"]
        for row in beams
    )
    for slug in ("guidance", "enhance-ability", "protection-from-energy"):
        spell = BundledAssetLoader().get_spell(slug)
        rows = [row for row in first["rows"] if row["source_slug"] == slug]
        assert {row["effect_id"] for row in rows} == {e.id for e in spell.passive_effects}
        assert all(row["lifecycle"]["stacking"] == "latest_applies" for row in rows)
        assert all(row["lifecycle"]["stacking_group"] == spell.foundry_uuid for row in rows)
    for row in first["rows"]:
        assert row["fully_executable"] is (row["lifecycle"] is not None)
        assert row["deferred_reason"] is None if row["fully_executable"] else row["deferred_reason"]


def test_lifecycle_audit_cli_writes_the_same_document(tmp_path, monkeypatch, capsys):
    output = tmp_path / "audit.json"
    monkeypatch.setattr(sys, "argv", ["effect_lifecycle_audit", "--output", str(output)])
    main()
    assert json.loads(output.read_text(encoding="utf8")) == audit_document(BundledAssetLoader())
    monkeypatch.setattr(sys, "argv", ["effect_lifecycle_audit"])
    main()
    assert json.loads(capsys.readouterr().out) == audit_document(BundledAssetLoader())
