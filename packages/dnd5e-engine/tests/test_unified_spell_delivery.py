"""Public direct and delegated casts share canonical target delivery rules."""

from __future__ import annotations

import copy
import random

import pytest
from dnd5e_srd_data import BundledAssetLoader
from dnd5e_srd_data.schema.common import TargetCreatureFilter, TargetTemplateBlock

from dnd5e_engine import orchestrator as orch
from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.events import (
    AreaTargeted,
    CastFailed,
    DamageApplied,
    EffectApplied,
    IntentSubmitted,
    SaveRolled,
)
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.live_spell_delivery import execute_spell_delivery, plan_spell_delivery
from dnd5e_engine.spatial import cell_id
from dnd5e_engine.specs import GridScene
from dnd5e_engine.spell_delivery import SpellDeliverySpec
from tests.c20_support import act, combatant, events, foe, start, wizard

CASTER = "char:caster"
WAND = "wand-of-fireballs"
DUST = "dust-of-sneezing-and-choking"


@pytest.fixture(autouse=True)
def loader():
    bundled = BundledAssetLoader()
    set_lib_loader_for_tests(bundled)
    yield bundled
    set_lib_loader_for_tests(None)


def _caster(**overrides):
    defaults = dict(
        initiative=100,
        character_level=9,
        intelligence=16,
        hp_current=500,
        hp_max=500,
        zone_id=cell_id(0, 0),
        creature_type="humanoid",
        spell_slots={2: 1, 3: 1, 4: 1, 5: 1},
        spells_known=["fireball", "slow", "phantasmal-force", "wall-of-fire"],
        equipment=(WAND, DUST, "helm-of-brilliance"),
    )
    return wizard(CASTER, **(defaults | overrides))


def _target(entity_id, col, row, *, initiative=10, **overrides):
    return foe(
        **(
            dict(
                entity_id=entity_id,
                zone_id=cell_id(col, row),
                initiative=initiative,
                creature_type="humanoid",
                dexterity=10,
            )
            | overrides
        )
    )


def _fireball_setup(*, seed=19, grid=None, caster=None):
    return start(
        [caster or _caster()],
        seed=seed,
        grid_scene=grid or GridScene(width=40, height=20),
        encounter=[
            _target("mon:at-origin", 10, 10, initiative=30, dexterity=-30),
            _target("mon:east", 13, 10, initiative=20, dexterity=100),
            _target("mon:west", 6, 10, initiative=10),
            _target("mon:outside", 15, 10, initiative=1),
        ],
    )


def _cast_fireball(handle, *, source="direct", charges=1, point="10,10", **choices):
    if source == "direct":
        act(
            handle,
            CASTER,
            intent_type="cast_spell",
            spell_id="fireball",
            slot_level=charges + 2,
            target_zone_id=point,
            **choices,
        )
    else:
        act(
            handle,
            CASTER,
            intent_type="use_item",
            item_id=WAND,
            charges_to_spend=charges,
            target_zone_id=point,
            **choices,
        )


@pytest.mark.parametrize("source", ["direct", "wand"])
@pytest.mark.parametrize("occupied_origin", [False, True])
def test_fireball_expands_from_empty_or_occupied_point_in_stable_order(
    loader, source, occupied_origin
):
    handle, live = _fireball_setup()
    point = cell_id(10, 10) if occupied_origin else cell_id(10, 9)
    # The named target is a direction/selection hint, never an uncounted area override.
    _cast_fireball(handle, source=source, point=point, target_id="mon:east")
    assert not events(live, CastFailed)
    [area] = events(live, AreaTargeted)
    assert (area.shape, area.size_ft, area.origin) == ("sphere", 20, point)
    assert area.affected_ids == ["mon:at-origin", "mon:east", "mon:west"]
    assert [e.target_id for e in events(live, SaveRolled)] == area.affected_ids
    assert all(e.dc == 15 for e in events(live, SaveRolled))
    damage = events(live, DamageApplied)
    assert damage[1].amount == damage[0].amount // 2
    activity = loader.get_spell("fireball").activities[0]
    assert all(e.source_id == f"spell:fireball:{activity.id}" for e in damage)
    if source == "wand":
        assert live.custom_counters_by_entity[CASTER][f"item_use:{WAND}"] == {"spent": 1}
        assert area.source_spell_id == "fireball"
        assert area.source_parent_id == f"item:{WAND}:{loader.get_item(WAND).activities[0].id}"
        assert all(e.source_parent_id == area.source_parent_id for e in damage)


@pytest.mark.parametrize("charges", [1, 2, 3])
def test_wand_fireball_preserves_dc_charge_cost_upcast_and_exact_rng(loader, charges):
    handle, live = _fireball_setup()
    oracle = random.Random()
    oracle.setstate(live.rng.getstate())
    raw = sum(oracle.randint(1, 6) for _ in range(7 + charges))
    naturals = [oracle.randint(1, 20) for _ in range(3)]
    _cast_fireball(handle, source="wand", charges=charges)
    saves = events(live, SaveRolled)
    assert [e.natural for e in saves] == naturals
    assert all(e.dc == 15 for e in saves)
    assert [e.amount for e in events(live, DamageApplied)] == [
        raw // 2 if save.succeeded else raw for save in saves
    ]
    assert live.custom_counters_by_entity[CASTER][f"item_use:{WAND}"] == {"spent": charges}
    assert live.spell_slots_by_entity[CASTER] == {2: 1, 3: 1, 4: 1, 5: 1}
    assert live.rng.getstate() == oracle.getstate()


def test_direct_and_wand_fireball_have_equal_geometry_targets_and_outcomes(loader):
    outputs = []
    for source in ("direct", "wand"):
        handle, live = _fireball_setup()
        spell = loader.get_spell("fireball")
        spec = SpellDeliverySpec(
            origin_cell="10,10", source_kind="direct_spell" if source == "direct" else "item_cast"
        )
        before = (live.rng.getstate(), list(live.event_log), copy.deepcopy(live.actor_zone))
        plan = plan_spell_delivery(live, combatant(live, CASTER), spell, spec)
        assert (live.rng.getstate(), live.event_log, live.actor_zone) == before
        _cast_fireball(handle, source=source)
        [area] = events(live, AreaTargeted)
        outputs.append(
            (
                plan.activities[0].cells,
                area.affected_ids,
                [e.model_dump() for e in events(live, SaveRolled)],
                [(e.target_id, e.amount, e.damage_type) for e in events(live, DamageApplied)],
                live.rng.getstate(),
            )
        )
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize("source", ["direct", "wand"])
def test_uncounted_fireball_selected_ids_cannot_replace_the_real_area(loader, source):
    handle, live = _fireball_setup()
    _cast_fireball(handle, source=source, target_ids=("mon:east",))
    [area] = events(live, AreaTargeted)
    assert area.affected_ids == ["mon:at-origin", "mon:east", "mon:west"]
    assert [e.target_id for e in events(live, SaveRolled)] == area.affected_ids


def test_execution_revalidates_dead_and_departed_targets_after_draw_free_plan(loader):
    _, live = _fireball_setup()
    caster = combatant(live, CASTER)
    spell = loader.get_spell("fireball")
    spec = SpellDeliverySpec(origin_cell="10,10")
    plan = plan_spell_delivery(live, caster, spell, spec)
    assert plan.target_ids == ("mon:at-origin", "mon:east", "mon:west")
    # This models the live state after an intervening reaction, before delivery.
    combatant(live, "mon:at-origin").is_alive = False
    live.dead_ids.add("mon:at-origin")
    live.initiative = [c for c in live.initiative if c.entity_id != "mon:east"]
    ctx = build_activity_context(
        caster,
        [],
        rng=live.rng,
        event_emitter=lambda event: orch._emit(live, event),
        slot_level=3,
        base_spell_level=3,
        spellcasting_ability="int",
        concentration=False,
        source_passive_effects=[],
        spell_book={},
        passive_damage_modifiers={},
        save_modifiers={},
        save_dc_override=15,
    )
    execute_spell_delivery(live, spell, ctx, spec)
    [area] = events(live, AreaTargeted)
    assert area.affected_ids == ["mon:west"]
    assert [e.target_id for e in events(live, SaveRolled)] == ["mon:west"]
    assert [e.target_id for e in events(live, DamageApplied)] == ["mon:west"]


@pytest.mark.parametrize("source", ["direct", "wand"])
@pytest.mark.parametrize("failure", ["bounds", "range", "line_of_effect"])
def test_invalid_fireball_point_refuses_before_action_slots_charges_and_rng(
    loader, source, failure
):
    options = {}
    point = {"bounds": "40,10", "range": "31,10", "line_of_effect": "10,10"}[failure]
    if failure == "line_of_effect":
        options["wall_segments"] = [{"x1": 5, "y1": 0, "x2": 5, "y2": 20}]
    handle, live = _fireball_setup(grid=GridScene(width=40, height=20, **options))
    before = (
        live.rng.getstate(),
        copy.deepcopy(live.spell_slots_by_entity),
        copy.deepcopy(live.custom_counters_by_entity),
    )
    _cast_fireball(handle, source=source, point=point)
    assert len(events(live, CastFailed)) == 1
    assert events(live, CastFailed)[0].reason in ("target_invalid", "out_of_range")
    assert not events(live, IntentSubmitted)
    assert not events(live, SaveRolled)
    assert not events(live, AreaTargeted)
    assert combatant(live, CASTER).action_available
    assert (
        live.rng.getstate(),
        live.spell_slots_by_entity,
        live.custom_counters_by_entity,
    ) == before


def _slow_setup(*, seed=19):
    return start(
        [_caster()],
        seed=seed,
        grid_scene=GridScene(width=40, height=20),
        encounter=[_target(f"mon:{i}", 10 + i, 10, initiative=20 - i) for i in range(7)]
        + [_target("mon:outside", 19, 10, initiative=1)],
    )


def test_slow_selects_six_creatures_inside_point_cube_away_from_caster(loader):
    handle, live = _slow_setup()
    act(
        handle,
        CASTER,
        intent_type="cast_spell",
        spell_id="slow",
        slot_level=3,
        target_zone_id="10,10",
        target_ids=tuple(f"mon:{i}" for i in reversed(range(6))),
    )
    [area] = events(live, AreaTargeted)
    assert (area.shape, area.size_ft, area.origin) == ("cube", 40, "10,10")
    assert area.affected_ids == [f"mon:{i}" for i in range(6)]
    assert area.excluded_ids == ["mon:6"]
    assert [e.target_id for e in events(live, SaveRolled)] == area.affected_ids
    assert live.spell_slots_by_entity[CASTER][3] == 0


@pytest.mark.parametrize(
    "selection",
    [tuple(f"mon:{i}" for i in range(7)), ("mon:0", "mon:0"), ("mon:0", "mon:outside")],
)
def test_invalid_slow_selected_targets_preserve_payment_and_rng(loader, selection):
    handle, live = _slow_setup()
    rng = live.rng.getstate()
    act(
        handle,
        CASTER,
        intent_type="cast_spell",
        spell_id="slow",
        slot_level=3,
        target_zone_id="10,10",
        target_ids=selection,
    )
    assert [e.reason for e in events(live, CastFailed)] == ["target_invalid"]
    assert not events(live, SaveRolled)
    assert live.spell_slots_by_entity[CASTER][3] == 1
    assert combatant(live, CASTER).action_available
    assert live.rng.getstate() == rng


def test_slow_exclusions_only_remove_from_authoritative_counted_selection(loader):
    handle, live = _slow_setup()
    act(
        handle,
        CASTER,
        intent_type="cast_spell",
        spell_id="slow",
        slot_level=3,
        target_zone_id="10,10",
        target_ids=("mon:2", "mon:0"),
        excluded_target_ids=("mon:0",),
    )
    [area] = events(live, AreaTargeted)
    assert area.affected_ids == ["mon:2"]
    assert [e.target_id for e in events(live, SaveRolled)] == ["mon:2"]


@pytest.mark.parametrize("exclusion", ["mon:outside", "mon:unknown"])
def test_slow_rejects_exclusions_outside_actual_geometry(loader, exclusion):
    handle, live = _slow_setup()
    rng = live.rng.getstate()
    act(
        handle,
        CASTER,
        intent_type="cast_spell",
        spell_id="slow",
        slot_level=3,
        target_zone_id="10,10",
        target_ids=("mon:0",),
        excluded_target_ids=(exclusion,),
    )
    assert [e.reason for e in events(live, CastFailed)] == ["target_invalid"]
    assert live.spell_slots_by_entity[CASTER][3] == 1
    assert live.rng.getstate() == rng


def test_phantasmal_force_effect_geometry_does_not_expand_save_targets(loader):
    handle, live = start(
        [_caster()],
        seed=19,
        encounter=[_target("mon:chosen", 6, 0, initiative=20), _target("mon:nearby", 6, 1)],
    )
    act(
        handle,
        CASTER,
        intent_type="cast_spell",
        spell_id="phantasmal-force",
        slot_level=2,
        target_id="mon:chosen",
    )
    assert not events(live, CastFailed)
    assert [e.target_id for e in events(live, SaveRolled)] == ["mon:chosen"]
    assert not events(live, AreaTargeted)


@pytest.mark.parametrize("obstruction", ["range", "line_of_effect"])
def test_phantasmal_force_keeps_normal_named_target_range_and_visibility(loader, obstruction):
    target_col = 13 if obstruction == "range" else 6
    grid = GridScene(
        width=20,
        height=10,
        wall_segments=[{"x1": 3, "y1": 0, "x2": 3, "y2": 10}]
        if obstruction == "line_of_effect"
        else [],
    )
    handle, live = start(
        [_caster()],
        seed=19,
        grid_scene=grid,
        encounter=[_target("mon:chosen", target_col, 0)],
    )
    rng = live.rng.getstate()
    act(
        handle,
        CASTER,
        intent_type="cast_spell",
        spell_id="phantasmal-force",
        slot_level=2,
        target_id="mon:chosen",
    )
    assert len(events(live, CastFailed)) == 1
    assert not events(live, SaveRolled)
    assert live.spell_slots_by_entity[CASTER][2] == 1
    assert live.rng.getstate() == rng


def test_dust_includes_creator_and_auto_success_types_without_d20_rng(loader):
    types = ("construct", "elemental", "ooze", "plant", "undead")
    handle, live = start(
        [_caster(zone_id="10,10", constitution=-30)],
        seed=19,
        grid_scene=GridScene(width=30, height=30),
        encounter=[
            _target(f"mon:{kind}", 11 + i, 10, initiative=30 - i, creature_type=kind)
            for i, kind in enumerate(types)
        ]
        + [_target("mon:ordinary", 10, 11), _target("mon:outside", 18, 10)],
    )
    combatant(live, "mon:ordinary").constitution = -30
    oracle = random.Random()
    oracle.setstate(live.rng.getstate())
    expected_naturals = [oracle.randint(1, 20) for _ in range(2)]
    act(handle, CASTER, intent_type="use_item", item_id=DUST)
    [area] = events(live, AreaTargeted)
    assert area.shape == "emanation"
    assert area.origin == "10,10"
    assert area.affected_ids == [CASTER, *(f"mon:{kind}" for kind in types), "mon:ordinary"]
    saves = events(live, SaveRolled)
    assert [s.natural for s in saves if s.natural is not None] == expected_naturals
    assert all(
        s.natural is None and s.succeeded
        for s in saves
        if s.target_id.startswith("mon:") and s.target_id != "mon:ordinary"
    )
    assert {e.effect.target_id for e in events(live, EffectApplied)} <= {CASTER, "mon:ordinary"}
    assert live.rng.getstate() == oracle.getstate()


def test_helm_diamond_light_only_targets_canonical_undead_filter(loader):
    handle, live = start(
        [_caster(zone_id="5,5")],
        seed=19,
        encounter=[
            _target("mon:undead", 6, 5, creature_type="undead"),
            _target("mon:humanoid", 5, 6),
        ],
    )
    act(
        handle,
        CASTER,
        intent_type="use_item",
        item_id="helm-of-brilliance",
        activity_id="IFscsoX8eRdhxP5s",
    )
    [area] = events(live, AreaTargeted)
    assert area.affected_ids == ["mon:undead"]
    assert [e.target_id for e in events(live, DamageApplied)] == ["mon:undead"]


@pytest.mark.parametrize("shape,size", [("wall", "30"), ("sphere", "@item.level")])
def test_unsupported_child_geometry_rejects_wand_before_any_charge_or_rng(loader, shape, size):
    original = loader.get_spell("fireball")
    invalid_activity = original.activities[0].model_copy(
        update={
            "target": original.activities[0].target.model_copy(
                update={"template": TargetTemplateBlock(type=shape, size=size)}
            )
        }
    )
    invalid = original.model_copy(update={"activities": [invalid_activity]})

    class Overlay(BundledAssetLoader):
        def get_spell_by_uuid(self, uuid):
            return invalid if uuid == original.foundry_uuid else super().get_spell_by_uuid(uuid)

    set_lib_loader_for_tests(Overlay())
    handle, live = _fireball_setup()
    before = (live.rng.getstate(), copy.deepcopy(live.custom_counters_by_entity))
    _cast_fireball(handle, source="wand")
    assert [e.reason for e in events(live, CastFailed)] == ["unsupported_area"]
    assert not events(live, DamageApplied)
    assert combatant(live, CASTER).action_available
    assert (live.rng.getstate(), live.custom_counters_by_entity) == before


def test_item_concentration_stays_explicitly_deferred_before_payment(loader):
    original = loader.get_item(WAND)
    cloud = loader.get_spell("fog-cloud")
    wrapper = original.activities[0].model_copy(
        update={
            "spell": original.activities[0].spell.model_copy(update={"uuid": cloud.foundry_uuid})
        }
    )
    item = original.model_copy(update={"activities": [wrapper]})

    class Overlay(BundledAssetLoader):
        def get_item(self, slug):
            return item if slug == WAND else super().get_item(slug)

    set_lib_loader_for_tests(Overlay())
    handle, live = _fireball_setup()
    before = (live.rng.getstate(), copy.deepcopy(live.custom_counters_by_entity))
    _cast_fireball(handle, source="wand")
    assert [e.reason for e in events(live, CastFailed)] == ["unsupported_area"]
    assert not events(live, AreaTargeted)
    assert combatant(live, CASTER).concentration_effect_id is None
    assert (live.rng.getstate(), live.custom_counters_by_entity) == before


def test_unreviewed_creature_filter_refuses_before_item_payment(loader):
    original = loader.get_item(DUST)
    activity = original.activities[0].model_copy(
        update={
            "target": original.activities[0].target.model_copy(
                update={
                    "creature_filter": TargetCreatureFilter(
                        deferred_reason="unreviewed source restriction"
                    )
                }
            )
        }
    )
    item = original.model_copy(update={"activities": [activity]})

    class Overlay(BundledAssetLoader):
        def get_item(self, slug):
            return item if slug == DUST else super().get_item(slug)

    set_lib_loader_for_tests(Overlay())
    handle, live = _fireball_setup()
    before = (live.rng.getstate(), copy.deepcopy(live.custom_counters_by_entity))
    act(handle, CASTER, intent_type="use_item", item_id=DUST)
    assert [e.reason for e in events(live, CastFailed)] == ["unsupported_area"]
    assert not events(live, SaveRolled)
    assert (live.rng.getstate(), live.custom_counters_by_entity) == before


@pytest.mark.parametrize("source", ["direct", "wand"])
def test_seeded_delivery_replays_byte_equivalent_resolution_events(loader, source):
    def run():
        handle, live = _fireball_setup()
        pre = len(live.event_log)
        _cast_fireball(handle, source=source)
        return [e.model_dump_json() for e in live.event_log[pre:]], live.rng.getstate()

    assert run() == run()
