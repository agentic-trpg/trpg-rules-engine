"""Monster declarations use common delivery and canonical movement outcomes."""

from __future__ import annotations

import copy

import pytest
from dnd5e_srd_data import BundledAssetLoader
from dnd5e_srd_data.schema.common import (
    CastActivity,
    CastChallengeBlock,
    CastSpellBlock,
    TargetTemplateBlock,
)
from dnd5e_srd_data.schema.monster import MonsterAction, MonsterActionKind

from dnd5e_engine.events import (
    AreaTargeted,
    AttackRolled,
    CombatantMoved,
    DamageApplied,
    SaveRolled,
    SpellCast,
)
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.orchestrator import (
    CombatHandle,
    _monster_area_placement,
    _monster_cast_candidate,
    _resolve_monster_activities,
    _resolve_monster_cast,
    advance_monster_turn,
)
from tests.e2e.harness import cell, events_of, run_async
from tests.e2e.test_c26_area_targeting import _combatant
from tests.test_monster_spatial_execution import burst, setup


@pytest.fixture(autouse=True)
def loader():
    bundled = BundledAssetLoader()
    set_lib_loader_for_tests(bundled)
    yield bundled
    set_lib_loader_for_tests(None)


def _cast_action(spell, *, activity_id="cast:spell", count=2):
    wrapper = CastActivity(
        id=activity_id,
        spell=CastSpellBlock(
            uuid=spell.foundry_uuid,
            level=spell.level,
            ability="int",
            challenge=CastChallengeBlock(override=True, save=15),
        ),
    )
    return MonsterAction(
        slug="spellcasting",
        name="Spellcasting",
        description="A typed spellcasting test action.",
        kind=MonsterActionKind.ACTION,
        uses_per_day=count,
        activities=[wrapper],
    )


def _spell_setup(loader, slug="thunderwave", *, enemies=((6, 5), (7, 6)), seed=19):
    spell = loader.get_spell(slug)
    action = _cast_action(spell)
    live, actor, target, monster = setup(loader, action, enemies, seed=seed, spells=[spell])
    actor.spellcasting_ability = "int"
    actor.intelligence = 18
    return live, actor, target, monster, action, spell


def test_point_origin_includes_empty_cells_and_improves_safe_enemy_count(loader):
    action = burst(loader, "sphere", size="5", distance="150")
    live, actor, _, _ = setup(loader, action, [(8, 5), (9, 5), (17, 5)], at=(0, 5), allies=[(8, 6)])
    before = (live.rng.getstate(), list(live.event_log), copy.deepcopy(live.actor_zone))
    placements = [_monster_area_placement(live, actor, action.activities) for _ in range(3)]
    assert all(p.origin == cell(8, 4) for p in placements)
    assert all(p.selection.affected_ids == ("char:0", "char:1") for p in placements)
    assert placements[0].origin not in live.actor_zone.values()
    assert (live.rng.getstate(), live.event_log, live.actor_zone) == before


def test_point_origin_tie_break_uses_numeric_cells_after_distance(loader):
    action = burst(loader, "sphere", size="5", distance="150")
    live, actor, _, _ = setup(loader, action, [(11, 5)], at=(0, 5))
    placement = _monster_area_placement(live, actor, action.activities)
    assert placement.origin == cell(10, 4)
    assert live.topology.distance_ft(cell(0, 5), placement.origin) == 50


def test_monster_thunderwave_has_per_target_saves_and_typed_post_damage_push(loader):
    def run():
        live, actor, target, _, action, spell = _spell_setup(loader)
        target.constitution = -30
        _combatant(live, "char:1").constitution = 100
        before_budget = {c.entity_id: c.movement_remaining for c in live.initiative}
        pre = len(live.event_log)
        _resolve_monster_cast(live, actor, target, action, action.activities[0], spell)
        later = live.event_log[pre:]
        saves = events_of(live, SaveRolled)
        assert [(s.target_id, s.succeeded, s.dc) for s in saves] == [
            ("char:0", False, 15),
            ("char:1", True, 15),
        ]
        [area] = events_of(live, AreaTargeted)
        assert area.origin == cell(5, 5)
        assert area.affected_ids == ["char:0", "char:1"]
        damage = events_of(live, DamageApplied)
        assert damage[1].amount == damage[0].amount // 2
        assert all(d.source_id == f"spell:thunderwave:{spell.activities[0].id}" for d in damage)
        assert all(
            d.source_parent_id == f"monster:{actor.entity_id}:{action.slug}:cast:spell"
            for d in damage
        )
        [moved] = events_of(live, CombatantMoved)
        assert (moved.actor_id, moved.from_zone, moved.to_zone, moved.distance_ft) == (
            "char:0",
            cell(6, 5),
            cell(8, 5),
            10,
        )
        assert moved.forced
        assert moved.movement_cost_ft == 0
        assert not events_of(live, AttackRolled)
        assert {c.entity_id: c.movement_remaining for c in live.initiative} == before_budget
        assert next(i for i, e in enumerate(later) if e is moved) > max(
            i
            for i, e in enumerate(later)
            if isinstance(e, DamageApplied) and e.target_id == "char:0"
        )
        pool = live.monster_action_uses_by_entity[actor.entity_id][action.slug]
        assert pool.action_uses_remaining == 1
        return [e.model_dump() for e in later], live.rng.getstate()

    assert run() == run()


def test_monster_thunderwave_does_not_move_a_target_killed_by_its_damage(loader):
    live, actor, target, _, action, spell = _spell_setup(loader, enemies=((6, 5),))
    target.constitution = -30
    target.hp_current = 1
    target.hp_max = 1
    live.tracked_hp[target.entity_id] = 1
    _resolve_monster_cast(live, actor, target, action, action.activities[0], spell)
    assert events_of(live, DamageApplied)[0].amount > 0
    assert not events_of(live, CombatantMoved)


def test_public_monster_turn_uses_the_canonical_spell_movement_fold(loader):
    live, _, target, _, _, _ = _spell_setup(loader, enemies=((6, 5),))
    target.constitution = -30
    run_async(advance_monster_turn(CombatHandle(live.handle_id)))
    assert [event.spell_id for event in events_of(live, SpellCast)] == ["thunderwave"]
    assert [event.actor_id for event in events_of(live, CombatantMoved)] == ["char:0"]
    assert live.actor_zone["char:0"] == cell(8, 5)


def test_monster_skips_unsupported_cast_and_selects_next_legal_spell_without_draws(loader):
    spell = loader.get_spell("thunderwave")
    action = _cast_action(spell)
    bad_activity = spell.activities[0].model_copy(
        update={
            "target": spell.activities[0].target.model_copy(
                update={"template": TargetTemplateBlock(type="wall", size="30")}
            )
        }
    )
    bad_spell = spell.model_copy(
        update={
            "slug": "unsupported-storm",
            "foundry_uuid": "test:unsupported",
            "activities": [bad_activity],
        }
    )
    bad_wrapper = _cast_action(bad_spell, activity_id="cast:unsupported").activities[0]
    action = action.model_copy(update={"activities": [bad_wrapper, *action.activities]})
    live, actor, _, _ = setup(loader, action, ((6, 5), (7, 6)), spells=[bad_spell, spell])
    before = (
        live.rng.getstate(),
        list(live.event_log),
        copy.deepcopy(live.monster_action_uses_by_entity),
    )
    chosen, resolved_spell = _monster_cast_candidate(live, actor, action)
    assert chosen.id == "cast:spell"
    assert resolved_spell.slug == "thunderwave"
    assert (live.rng.getstate(), live.event_log, live.monster_action_uses_by_entity) == before


@pytest.mark.parametrize("shape,size", [("wall", "30"), ("sphere", "@item.level")])
def test_unsupported_monster_action_falls_through_without_spending_recharge_or_uses(
    loader, shape, size
):
    bad = burst(loader, shape, size=size, uses=2)
    fallback = next(a for a in loader.get_monster("magma-mephit").actions if a.slug == "claw")
    live, actor, target, monster = setup(loader, bad, [(6, 5)], extra_actions=[fallback])
    before = copy.deepcopy(live.monster_action_uses_by_entity[actor.entity_id][bad.slug])
    rng = live.rng.getstate()
    plan, selected_cast = _resolve_monster_activities(live, actor, monster.slug, False, target)
    assert selected_cast is None
    assert plan.source_action.slug == fallback.slug
    assert live.rng.getstate() == rng
    assert live.monster_action_uses_by_entity[actor.entity_id][bad.slug] == before
    run_async(advance_monster_turn(CombatHandle(live.handle_id)))
    assert live.monster_action_uses_by_entity[actor.entity_id][bad.slug] == before
    assert events_of(live, AttackRolled)
    assert not events_of(live, AreaTargeted)
