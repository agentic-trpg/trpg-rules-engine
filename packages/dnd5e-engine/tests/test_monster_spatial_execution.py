"""Monster aiming, area resolution and source-action resource identity."""

from __future__ import annotations

import copy
import random

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd5e_srd_data.schema.common import (
    RangeBlock,
    SaveBlock,
    SaveDcBlock,
    TargetAffectsBlock,
    TargetBlock,
    TargetTemplateBlock,
    UsesBlock,
)
from dnd5e_srd_data.schema.monster import MonsterAction, MonsterActionKind

from dnd5e_engine import GridScene
from dnd5e_engine.activities.monster_actions import plan_monster_action
from dnd5e_engine.events import (
    AreaTargeted,
    DamageApplied,
    EffectApplied,
    SaveRolled,
    SpellCast,
)
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.orchestrator import (
    _monster_action_available,
    _monster_activity_available,
    _monster_area_placement,
    _resolve_monster_attack_activities,
    _resolve_monster_cast,
)
from tests.e2e.harness import cell, events_of
from tests.e2e.test_c26_area_targeting import _combatant, _foe, _start, _sturdy


@pytest.fixture(autouse=True)
def loader():
    bundled = BundledAssetLoader()
    set_lib_loader_for_tests(bundled)
    yield bundled
    set_lib_loader_for_tests(None)


def burst(
    loader,
    shape="cone",
    *,
    size="15",
    affects="creature",
    choice=False,
    uses=None,
    recharge="5-6",
    activity_uses="",
    distance="60",
):
    original = next(
        a for a in loader.get_monster("magma-mephit").actions if a.slug == "fire-breath"
    )
    activity = original.activities[0].model_copy(
        update={
            "range": RangeBlock(units="ft", value=distance),
            "target": TargetBlock(
                template=TargetTemplateBlock(type=shape, size=size),
                affects=TargetAffectsBlock(type=affects, choice=choice),
            ),
            "save": SaveBlock(ability=["dex"], dc=SaveDcBlock(formula="99")),
            "uses": UsesBlock(max=activity_uses),
        },
        deep=True,
    )
    return original.model_copy(
        update={
            "slug": "burst",
            "name": "Burst",
            "activities": [activity],
            "recharge": recharge,
            "uses_per_day": uses,
        },
        deep=True,
    )


def setup(loader, action, enemies, *, allies=(), at=(5, 5), grid=None, seed=19, extra_actions=()):
    monster = loader.get_monster("magma-mephit").model_copy(
        update={"actions": [action, *extra_actions]}, deep=True
    )
    set_lib_loader_for_tests(MemoryAssetLoader(monsters=[monster]))
    party = [_sturdy(f"char:{i}", cell(*position), 10 - i) for i, position in enumerate(enemies)]
    foes = [_foe("mon:actor", cell(*at), monster.slug, initiative=30, hp_current=1000, hp_max=1000)]
    foes.extend(
        _foe(
            f"mon:ally{i}",
            cell(*position),
            monster.slug,
            initiative=1 - i,
            hp_current=1000,
            hp_max=1000,
        )
        for i, position in enumerate(allies)
    )
    _, live = _start(
        party,
        foes,
        session="monster-spatial",
        seed=seed,
        grid=grid or GridScene(width=30, height=30),
    )
    actor = _combatant(live, "mon:actor")
    return live, actor, _combatant(live, "char:0"), monster


def execute(live, actor, target, monster, action):
    plan = plan_monster_action(
        monster,
        action,
        is_available=lambda a: _monster_action_available(live, actor, a),
        is_activity_available=lambda a, b: _monster_activity_available(live, actor, a, b),
    )
    _resolve_monster_attack_activities(
        live,
        actor,
        [target],
        plan.activities,
        execution_plan=plan,
    )
    return plan


@pytest.mark.parametrize(
    ("shape", "size", "reported"),
    [
        ("cone", "15", "cone"),
        ("line", "15", "line"),
        ("sphere", "5", "sphere"),
        ("cylinder", "5", "cylinder"),
        ("cube", "15", "cube"),
        ("radius", "15", "emanation"),
    ],
)
def test_supported_shapes_resolve_every_target_and_spend_once(loader, shape, size, reported):
    action = burst(loader, shape, size=size, uses=2)
    live, actor, target, monster = setup(loader, action, [(7, 5), (8, 5), (15, 15)])
    before = copy.deepcopy(monster.model_dump())
    execute(live, actor, target, monster, action)
    [area] = events_of(live, AreaTargeted)
    assert (area.actor_id, area.source_id, area.shape, area.size_ft) == (
        actor.entity_id,
        "burst",
        reported,
        int(size),
    )
    assert area.affected_ids == ["char:0", "char:1"]
    assert [e.target_id for e in events_of(live, SaveRolled)] == area.affected_ids
    damage = events_of(live, DamageApplied)
    assert [e.target_id for e in damage] == area.affected_ids
    assert damage[0].amount == damage[1].amount > 0
    pool = live.monster_action_uses_by_entity[actor.entity_id]["burst"]
    assert pool.recharge_spent
    assert pool.action_uses_remaining == 1
    assert monster.model_dump() == before


@pytest.mark.parametrize(
    ("affects", "choice", "direction", "affected", "excluded"),
    [
        ("creature", False, (0, -1), ["char:2"], []),
        ("enemy", False, (1, 0), ["char:0", "char:1"], ["mon:ally0"]),
        ("creature", True, (1, 0), ["char:0", "char:1"], ["mon:ally0"]),
    ],
)
def test_aiming_uses_actual_affects_and_prefers_safety_over_enemy_count(
    loader, affects, choice, direction, affected, excluded
):
    action = burst(loader, affects=affects, choice=choice)
    live, actor, target, monster = setup(loader, action, [(7, 6), (8, 7), (5, 2)], allies=[(6, 6)])
    execute(live, actor, target, monster, action)
    [area] = events_of(live, AreaTargeted)
    assert (area.direction, area.affected_ids, area.excluded_ids) == (direction, affected, excluded)


def test_sphere_avoids_ally_even_when_that_reduces_enemy_count(loader):
    action = burst(loader, "sphere", size="5", distance="150")
    live, actor, target, monster = setup(
        loader, action, [(8, 5), (9, 5), (17, 5)], at=(0, 5), allies=[(8, 6)]
    )
    execute(live, actor, target, monster, action)
    [area] = events_of(live, AreaTargeted)
    assert (area.origin, area.direction, area.affected_ids) == (cell(17, 5), None, ["char:2"])


def test_unavoidable_friendly_fire_has_stable_events_and_exact_rng_draws(loader):
    def run():
        action = burst(loader, "line")
        live, actor, target, monster = setup(loader, action, [(7, 5), (8, 5)], allies=[(6, 5)])
        initial_rng = live.rng.getstate()
        initial_events = list(live.event_log)
        initial_pools = copy.deepcopy(live.monster_action_uses_by_entity)
        for _ in range(3):
            placement = _monster_area_placement(live, actor, action.activities)
            assert placement.direction == (1, 0)
        assert live.rng.getstate() == initial_rng
        assert live.event_log == initial_events
        assert live.monster_action_uses_by_entity == initial_pools
        oracle = random.Random()
        oracle.setstate(initial_rng)
        # Magma Mephit Fire Breath: one shared 2d6, then one d20 per creature.
        raw = sum(oracle.randint(1, 6) for _ in range(2))
        expected_saves = [oracle.randint(1, 20) for _ in range(3)]
        execute(live, actor, target, monster, action)
        later = live.event_log[len(initial_events) :]
        assert [e.type for e in later] == ["area_targeted"] + ["save_rolled", "damage_applied"] * 3
        [area] = events_of(live, AreaTargeted)
        assert area.affected_ids == ["char:0", "char:1", "mon:ally0"]
        assert [e.natural for e in events_of(live, SaveRolled)] == expected_saves
        # Mephit ally's fire immunity is resolved by the existing damage path.
        assert [e.amount for e in events_of(live, DamageApplied)] == [raw, raw, 0]
        assert live.rng.getstate() == oracle.getstate()
        return [e.model_dump() for e in later], live.rng.getstate()

    assert run() == run()


@pytest.mark.parametrize(
    ("shape", "size", "distance", "affects"),
    [
        ("line", "5", "60", "creature"),
        ("sphere", "5", "10", "creature"),
        ("cone", "15", "60", "ally"),
        ("wall", "15", "60", "creature"),
        ("cone", "@item.level", "60", "creature"),
    ],
)
def test_no_legal_placement_preserves_resources_events_and_rng(
    loader, shape, size, distance, affects
):
    action = burst(loader, shape, size=size, distance=distance, affects=affects, uses=2)
    live, actor, target, monster = setup(loader, action, [(8, 5)], allies=[(6, 5)])
    events, rng = list(live.event_log), live.rng.getstate()
    pools = copy.deepcopy(live.monster_action_uses_by_entity)
    execute(live, actor, target, monster, action)
    assert live.event_log == events
    assert live.rng.getstate() == rng
    assert live.monster_action_uses_by_entity == pools


@pytest.mark.parametrize("action_cap", [True, False])
def test_limited_use_area_spends_per_invocation_not_per_victim(loader, action_cap):
    action = burst(
        loader,
        "radius",
        uses=2 if action_cap else None,
        activity_uses="3" if action_cap else "2",
        recharge=None,
    )
    live, actor, target, monster = setup(loader, action, [(7, 5), (8, 5)])
    for _ in range(3):
        execute(live, actor, target, monster, action)
    assert len(events_of(live, AreaTargeted)) == 2
    assert len(events_of(live, SaveRolled)) == 4
    pool = live.monster_action_uses_by_entity[actor.entity_id]["burst"]
    if action_cap:
        assert pool.action_uses_remaining == 0
        assert pool.uses_remaining == {}
    else:
        assert pool.action_uses_remaining is None
        assert list(pool.uses_remaining.values()) == [0]


def test_repeated_area_child_keeps_source_identity_and_rechecks_recharge(loader):
    child = burst(loader, "radius")
    root = MonsterAction(
        slug="multiattack",
        name="Multiattack",
        kind=MonsterActionKind.ACTION,
        uses_per_day=2,
        description="The creature makes two [[/item Burst]] attacks.",
    )
    live, actor, target, monster = setup(loader, root, [(7, 5), (8, 5)], extra_actions=[child])
    plan = execute(live, actor, target, monster, root)
    assert len(plan.executions) == 2
    assert all(step.source_action is child for step in plan.executions)
    assert [e.source_id for e in events_of(live, AreaTargeted)] == ["burst"]
    assert len(events_of(live, SaveRolled)) == 2
    pools = live.monster_action_uses_by_entity[actor.entity_id]
    assert pools["burst"].recharge_spent
    assert pools["multiattack"].action_uses_remaining == 1
    # Replaying a stale plan cannot spend either pool again.
    before = (copy.deepcopy(pools), live.rng.getstate(), list(live.event_log))
    _resolve_monster_attack_activities(live, actor, [target], plan.activities, execution_plan=plan)
    assert (pools, live.rng.getstate(), live.event_log) == before


def test_monster_spell_area_emits_cast_then_area_then_individual_effects(loader):
    spell = loader.get_spell("sleep")
    mage = loader.get_monster("mage")
    action = next(a for a in mage.actions if a.slug == "spellcasting")
    wrapper = action.activities[0].model_copy(update={"uses": UsesBlock(max="2")}, deep=True)
    action = action.model_copy(update={"activities": [wrapper], "uses_per_day": 2}, deep=True)
    live, actor, target, _ = setup(loader, action, [(8, 5), (9, 5)], at=(0, 5))
    actor.spellcasting_ability = "int"
    actor.intelligence = 30
    pre = len(live.event_log)
    _resolve_monster_cast(live, actor, target, action, wrapper, spell)
    later = live.event_log[pre:]
    assert isinstance(later[0], SpellCast)
    assert isinstance(later[1], AreaTargeted)
    assert later[1].source_id == "sleep"
    assert later[1].affected_ids == ["char:0", "char:1"]
    assert [e.target_id for e in events_of(live, SaveRolled)] == ["char:0", "char:1"]
    assert [e.effect.target_id for e in events_of(live, EffectApplied)] == ["char:0", "char:1"]
    assert (
        live.monster_action_uses_by_entity[actor.entity_id][action.slug].action_uses_remaining == 1
    )


@pytest.mark.parametrize("obstruction", ["wall", "total", "blocked"])
def test_line_of_effect_excludes_creatures_beyond_obstructions(loader, obstruction):
    options = {
        "wall": {"wall_segments": [{"x1": 7, "y1": 0, "x2": 7, "y2": 30}]},
        "total": {"cover_cells": {cell(7, 5): "total"}},
        "blocked": {"blocked_cells": [cell(7, 5)]},
    }[obstruction]
    action = burst(loader, "line", size="30")
    live, actor, target, monster = setup(
        loader, action, [(6, 5), (8, 5)], grid=GridScene(width=30, height=30, **options)
    )
    execute(live, actor, target, monster, action)
    [area] = events_of(live, AreaTargeted)
    assert area.direction == (1, 0)
    assert area.affected_ids == ["char:0"]
    assert [e.target_id for e in events_of(live, SaveRolled)] == ["char:0"]


def test_burst_checks_range_to_origin_and_cover_from_origin(loader):
    action = burst(loader, "sphere", size="20", distance="40")
    live, actor, target, monster = setup(
        loader,
        action,
        [(8, 5), (12, 5)],
        at=(0, 5),
        grid=GridScene(width=30, height=30, cover_cells={cell(4, 5): "half"}),
    )
    execute(live, actor, target, monster, action)
    [area] = events_of(live, AreaTargeted)
    assert area.origin == cell(8, 5)
    assert area.affected_ids == ["char:0", "char:1"]
    # The second victim lies beyond the casting range but inside the burst.
    # The obstruction between caster and burst grants neither victim cover
    # from the burst point (the first victim occupies the origin itself).
    assert [e.modifier for e in events_of(live, SaveRolled)] == [0, 0]


def test_target_anchored_area_cannot_place_through_total_cover(loader):
    action = burst(loader, "sphere", size="5")
    live, actor, target, monster = setup(
        loader,
        action,
        [(8, 5)],
        grid=GridScene(width=30, height=30, cover_cells={cell(7, 5): "total"}),
    )
    before = (
        list(live.event_log),
        live.rng.getstate(),
        copy.deepcopy(live.monster_action_uses_by_entity),
    )
    execute(live, actor, target, monster, action)
    assert (live.event_log, live.rng.getstate(), live.monster_action_uses_by_entity) == before


def test_monster_spell_without_legal_origin_never_casts_or_spends(loader):
    spell = loader.get_spell("fireball")
    mage = loader.get_monster("mage")
    action = next(a for a in mage.actions if a.slug == "spellcasting")
    wrapper = action.activities[0]
    live, actor, target, _ = setup(
        loader,
        action,
        [(8, 5)],
        at=(0, 5),
        grid=GridScene(width=30, height=30, wall_segments=[{"x1": 4, "y1": 0, "x2": 4, "y2": 30}]),
    )
    before = (
        list(live.event_log),
        live.rng.getstate(),
        copy.deepcopy(live.monster_action_uses_by_entity),
    )
    _resolve_monster_cast(live, actor, target, action, wrapper, spell)
    assert (live.event_log, live.rng.getstate(), live.monster_action_uses_by_entity) == before


@pytest.mark.parametrize("lowest", [0, 1])
def test_equal_enemy_counts_use_lowest_hp_then_stable_initiative_priority(loader, lowest):
    action = burst(loader, "line")
    live, actor, _, _ = setup(loader, action, [(7, 5), (5, 3)])
    # With equal HP, char:0 wins over the earlier north direction. Lowering
    # char:1's HP must change the aim without changing any RNG state.
    if lowest == 1:
        _combatant(live, "char:1").hp_current = 100
    before = live.rng.getstate()
    placement = _monster_area_placement(live, actor, action.activities)
    assert placement.direction == ((1, 0) if lowest == 0 else (0, -1))
    assert placement.selection.affected_ids == (f"char:{lowest}",)
    assert live.rng.getstate() == before


def test_no_legal_area_child_preserves_multiattack_wrapper_pool(loader):
    child = burst(loader, "line", size="5", uses=2)
    root = MonsterAction(
        slug="multiattack",
        name="Multiattack",
        kind=MonsterActionKind.ACTION,
        uses_per_day=2,
        description="The creature makes two [[/item Burst]] attacks.",
    )
    live, actor, target, monster = setup(loader, root, [(8, 5)], extra_actions=[child])
    before = (
        list(live.event_log),
        live.rng.getstate(),
        copy.deepcopy(live.monster_action_uses_by_entity),
    )
    plan = execute(live, actor, target, monster, root)
    assert len(plan.executions) == 2
    assert (live.event_log, live.rng.getstate(), live.monster_action_uses_by_entity) == before


def test_spell_area_activity_range_override_is_authoritative(loader):
    spell = loader.get_spell("fireball")
    activity = spell.activities[0].model_copy(
        update={"range": RangeBlock(units="ft", value="5", override=True)}, deep=True
    )
    action = burst(loader)
    live, actor, _, _ = setup(loader, action, [(8, 5)], at=(0, 5))
    assert _monster_area_placement(live, actor, spell.activities, spell=spell) is not None
    assert _monster_area_placement(live, actor, [activity], spell=spell) is None
