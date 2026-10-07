"""Canonical species choice and live size lineage for all creature producers."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.advancement import AdvancementEntry
from dnd5e_srd_data.schema.monster import CreatureSize
from pydantic import ValidationError

from dnd5e_engine import CombatInstance, build_party_member, make_build_spec
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.activities.conjuration import StatBlockMagnitudes
from dnd5e_engine.build_spec import CharacterBuildSpec, DerivedSheet, derive_sheet
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.size import (
    one_size_larger,
    resolve_species_size,
    size_at_most,
    size_rank,
    species_size_options,
    target_at_most_one_size_larger,
    two_or_more_sizes_apart,
)
from dnd5e_engine.specs import GridScene
from dnd5e_engine.types.effects import ActiveEffect
from tests.c20_support import combatant, foe, pc, start

LOADER = BundledAssetLoader()


@pytest.fixture(autouse=True)
def _loader() -> Iterator[None]:
    set_lib_loader_for_tests(LOADER)
    yield
    set_lib_loader_for_tests(None)


def test_size_qualifiers_share_the_dataset_enum_and_all_boundaries():
    sizes = tuple(CreatureSize)
    assert [size_rank(size) for size in sizes] == list(range(6))
    assert [one_size_larger(size) for size in sizes] == [*sizes[1:], None]
    for attacker in sizes:
        for target in sizes:
            difference = size_rank(target) - size_rank(attacker)
            assert target_at_most_one_size_larger(attacker, target) is (difference <= 1)
            assert two_or_more_sizes_apart(attacker, target) is (abs(difference) >= 2)
            assert size_at_most(attacker, target) is (difference >= 0)


def test_legacy_handconstructed_sheet_defaults_size_without_weakening_build_validation():
    sheet = derive_sheet(make_build_spec(species_slug="dwarf", class_slug="fighter"), loader=LOADER)
    legacy_fields = sheet.model_dump(exclude={"creature_size"})
    assert DerivedSheet.model_validate(legacy_fields).creature_size is CreatureSize.MEDIUM


@pytest.mark.parametrize("choice", [CreatureSize.SMALL, CreatureSize.MEDIUM])
def test_multi_size_species_requires_and_preserves_explicit_choice_to_live_state(choice):
    spec = make_build_spec(species_slug="human", size_choice=choice, class_slug="fighter")
    assert CharacterBuildSpec.model_validate_json(spec.model_dump_json()).size_choice == choice
    assert derive_sheet(spec, loader=LOADER).creature_size is choice
    member = build_party_member(
        spec,
        CombatInstance(entity_id="char:hero", name="Hero", zone_id="0,0", initiative=20),
        loader=LOADER,
    )
    assert member.creature_size is choice
    _, live = start([member], seed=0)
    assert combatant(live).creature_size is choice


@pytest.mark.parametrize("slug", ["human", "tiefling-infernal", "tiefling-abyssal"])
def test_multi_size_species_refuses_missing_or_disallowed_choice(slug):
    with pytest.raises(ValueError, match="explicit size_choice"):
        derive_sheet(make_build_spec(species_slug=slug, class_slug="fighter"), loader=LOADER)
    with pytest.raises(ValueError, match="not allowed"):
        derive_sheet(
            make_build_spec(
                species_slug=slug, size_choice=CreatureSize.LARGE, class_slug="fighter"
            ),
            loader=LOADER,
        )


@pytest.mark.parametrize(
    "slug,size", [("dwarf", CreatureSize.MEDIUM), ("halfling", CreatureSize.SMALL)]
)
def test_single_size_species_needs_no_redundant_choice_and_rejects_override(slug, size):
    assert (
        derive_sheet(
            make_build_spec(species_slug=slug, class_slug="fighter"), loader=LOADER
        ).creature_size
        is size
    )
    with pytest.raises(ValueError, match="not allowed"):
        derive_sheet(
            make_build_spec(species_slug=slug, size_choice=CreatureSize.HUGE, class_slug="fighter"),
            loader=LOADER,
        )


def test_canonical_size_advancement_is_authoritative_and_never_reads_prose():
    species = LOADER.get_species("human")
    assert species is not None
    assert species_size_options(species) == (CreatureSize.SMALL, CreatureSize.MEDIUM)
    changed = species.model_copy(update={"description": "Huge only", "size": CreatureSize.HUGE})
    assert resolve_species_size(changed, CreatureSize.SMALL) is CreatureSize.SMALL
    fallback = changed.model_copy(update={"advancement": []})
    assert resolve_species_size(fallback, None) is CreatureSize.HUGE


@pytest.mark.parametrize("codes", [[], ["gigantic"], "med", [1]])
def test_malformed_canonical_size_advance_is_not_silently_defaulted(codes):
    species = LOADER.get_species("dwarf")
    entry = AdvancementEntry(id="test", type="Size", configuration={"sizes": codes})
    with pytest.raises(ValueError, match=r"size|Size"):
        resolve_species_size(species.model_copy(update={"advancement": [entry]}), None)


def test_unknown_host_size_is_rejected_at_the_typed_boundary():
    with pytest.raises(ValidationError):
        make_build_spec(species_slug="human", size_choice="giant", class_slug="fighter")
    with pytest.raises(ValidationError):
        pc(creature_size="giant")


def test_raw_specs_copy_explicit_size_and_canonicalize_cell_identity():
    member = pc(creature_size=CreatureSize.TINY, zone_id=" 0, 0 ")
    enemy = foe(creature_size=CreatureSize.GARGANTUAN, zone_id="1, 0")
    assert (member.zone_id, enemy.zone_id) == ("0,0", "1,0")
    _, live = start([member], encounter=[enemy], seed=0)
    assert combatant(live).creature_size is CreatureSize.TINY
    assert combatant(live, "mon:foe").creature_size is CreatureSize.GARGANTUAN
    assert live.actor_zone == {"char:hero": "0,0", "mon:foe": "1,0"}


def test_scene_cells_use_the_same_canonical_identity_for_every_spatial_surface():
    scene = GridScene(
        width=4,
        height=4,
        blocked_cells=["1, 1", " 1,1 "],
        difficult_terrain_cells=["2, 2"],
        cover_cells={" 3, 1": "half"},
        lighting={"2, 3": "dark"},
        obscurement_cells={" 3,3 ": "heavy"},
    )
    assert scene.blocked_cells == ["1,1"]
    assert scene.difficult_terrain_cells == ["2,2"]
    assert scene.cover_cells == {"3,1": "half"}
    assert scene.lighting == {"2,3": "dark"}
    assert scene.obscurement_cells == {"3,3": "heavy"}
    with pytest.raises(ValidationError, match="conflicting values"):
        GridScene(width=4, height=4, cover_cells={"1,1": "half", "1, 1": "total"})


@pytest.mark.parametrize("slug", ["giant-spider", "giant-ape", "giant-octopus"])
def test_monster_template_hydrates_authoritative_size_and_special_speeds(slug):
    monster = LOADER.get_monster(slug)
    _, live = start([pc()], encounter=[foe(monster_template_slug=slug)], seed=0)
    actual = combatant(live, "mon:foe")
    assert actual.creature_size is monster.creature_size
    for mode in ("climb", "swim", "fly", "burrow"):
        assert getattr(actual.movement_modes, mode) == getattr(monster.movement, mode)


@pytest.mark.parametrize("source", ["wild-shape", "polymorph"])
def test_transform_replaces_size_and_revert_restores_original_size(source):
    _, live = start([pc(creature_size=CreatureSize.SMALL)], seed=0)
    form = LOADER.get_monster("brown-bear")
    assert form is not None
    assert form.creature_size is CreatureSize.LARGE
    before_rng = live.rng.getstate()
    orch._apply_transform(
        live,
        "char:hero",
        form,
        source=source,
        effect=ActiveEffect(
            id="shape:test", origin="test:shape", name="Shape", target_id="char:hero"
        ),
        temp_hp=5,
    )
    assert combatant(live).creature_size is CreatureSize.LARGE
    assert live.transforms["char:hero"].stash["creature_size"] is CreatureSize.SMALL
    orch._end_transform(live, "char:hero", "remove_ieffect")
    assert combatant(live).creature_size is CreatureSize.SMALL
    assert live.rng.getstate() == before_rng


def test_summon_uses_its_stat_block_size_and_preserves_owners_size():
    _, live = start([pc(creature_size=CreatureSize.SMALL)], seed=0)
    owner = combatant(live)
    form = LOADER.get_monster("draconic-spirit")
    summoned = orch._summon_combatant(
        owner,
        form,
        entity_id="summon:test",
        ac=15,
        hp=50,
        magnitudes=StatBlockMagnitudes(ability_scores={}, proficiency_bonus=3),
    )
    assert summoned.creature_size is form.creature_size
    assert owner.creature_size is CreatureSize.SMALL
