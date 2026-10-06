"""Initiative totals from structured Foundry bonuses, with old-data compatibility."""

from datetime import date
from pathlib import Path

import pytest
import yaml

from dnd5e_srd_data import BundledAssetLoader
from dnd5e_srd_data.schema.monster import Monster
from tools.translators.foundry import translate_monster_yaml

FIXTURE = Path(__file__).parent / "fixtures/foundry_pack_minimal/monsters/goblin.yml"


@pytest.mark.parametrize(
    ("bonus", "expected"),
    [
        ("", -1),
        ("@prof", 3),
        ("@prof * 2", 7),
        ("3", 2),
        ("@abilities.int.mod", 1),
        ("1d4", None),
        ("@unknown", None),
    ],
)
def test_structured_bonus_is_additional_to_ability(tmp_path, bonus, expected):
    doc = yaml.safe_load(FIXTURE.read_text())
    doc["system"]["details"]["cr"] = 10
    doc["system"]["attributes"]["prof"] = 4
    doc["system"]["abilities"]["dex"]["value"] = 9
    doc["system"]["abilities"]["int"]["value"] = 14
    doc["system"]["attributes"]["init"] = {"ability": "", "bonus": bonus}
    path = tmp_path / "monster.yml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    monster = translate_monster_yaml(path, ingest_date=date(2026, 5, 30), ingest_version="test")
    assert monster.initiative_modifier == expected


def test_missing_initiative_and_old_json_remain_compatible():
    monster = translate_monster_yaml(FIXTURE, ingest_date=date(2026, 5, 30), ingest_version="test")
    assert monster.initiative_modifier is None
    old = monster.model_dump(mode="json")
    old.pop("initiative_modifier")
    assert Monster.model_validate(old).initiative_modifier is None
    for value in (None, 0, -2, 7):
        updated = monster.model_copy(update={"initiative_modifier": value})
        assert Monster.model_validate_json(updated.model_dump_json()) == updated


@pytest.mark.parametrize(
    ("slug", "dex", "total"),
    [
        ("tough", 12, 1),
        ("wolf", 15, 2),
        ("crocodile", 10, 0),
        ("aboleth", 9, 7),
        ("adult-red-dragon", 10, 12),
    ],
)
def test_shipped_initiative_oracles(slug, dex, total):
    monster = BundledAssetLoader().get_monster(slug)
    assert monster is not None
    assert monster.ability_scores.dex == dex
    assert monster.initiative_modifier == total


def test_pinned_structured_source_matches_entire_canonical_corpus():
    root = Path(__file__).parents[1]
    raw = root / "raw_sources/foundry/packs/_source/actors24"
    if not raw.exists():
        pytest.skip("maintainer source not populated")
    loader = BundledAssetLoader()
    seen = 0
    for path in raw.rglob("*.yml"):
        monster = loader.get_monster(path.stem)
        if monster is None or not monster.provenance.source_url.endswith(
            "actors24/" + path.relative_to(raw).as_posix()
        ):
            continue
        doc = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.CSafeLoader)
        system = doc["system"]
        init = system["attributes"]["init"]
        ability = init["ability"] or "dex"
        score = system["abilities"][ability]["value"]
        bonus = init["bonus"]
        # Independent oracle for the surveyed pin's four literal source shapes.
        if bonus == "":
            extra = 0
        elif bonus == "@prof":
            extra = monster.proficiency_bonus
        elif bonus == "@prof * 2":
            extra = 2 * monster.proficiency_bonus
        else:
            assert bonus == "@abilities.int.mod", (path, bonus)
            extra = (system["abilities"]["int"]["value"] - 10) // 2
        assert monster.initiative_modifier == (score - 10) // 2 + extra, monster.slug
        seen += 1
    assert seen == 341
