"""Correctness regressions at shared delivery and public combat boundaries."""

from __future__ import annotations

import copy

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd5e_srd_data.schema.common import TargetBlock, TargetCreatureFilter

from dnd5e_engine import orchestrator as orch
from dnd5e_engine.activities.save import _scale_on_save, resolve_save
from dnd5e_engine.areas import area_cells, area_template, has_line_of_effect
from dnd5e_engine.events import AttackRolled, CastFailed, DamageApplied, IntentSubmitted, SaveRolled
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.live_spell_delivery import plan_spell_delivery
from dnd5e_engine.spatial import GridTopology
from dnd5e_engine.specs import GridScene
from dnd5e_engine.spell_delivery import DeliveryPlanningError, SpellDeliverySpec
from tests.activities.test_save_dc_and_damage_exprs import _ctx
from tests.c20_support import act, events, start
from tests.e2e.harness import run_async
from tests.test_monster_spatial_execution import burst, execute, setup
from tests.test_monster_spell_delivery import _spell_setup
from tests.test_unified_spell_delivery import (
    CASTER,
    DUST,
    WAND,
    _cast_fireball,
    _caster,
    _fireball_setup,
    _target,
)


@pytest.fixture(autouse=True)
def loader():
    bundled = BundledAssetLoader()
    set_lib_loader_for_tests(bundled)
    yield bundled
    set_lib_loader_for_tests(None)


def _state(live):
    """Public combat state plus lifecycle owners, without random handle IDs."""
    return copy.deepcopy(
        (
            orch.get_live(orch.CombatHandle(live.handle_id)),
            live.timed_activities,
            live.persistent_areas,
        )
    )


def _restricted_action(loader, *, uses=2, activity_uses=""):
    action = burst(loader, uses=uses, activity_uses=activity_uses)
    activity = action.activities[0].model_copy(
        update={
            "target": TargetBlock(
                creature_filter=TargetCreatureFilter(include_creature_types=("undead",))
            )
        }
    )
    return action.model_copy(update={"activities": [activity]})


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("uses,activity_uses", [(2, ""), (None, "2")])
def test_public_monster_rejects_nonarea_type_mismatch_without_spending(
    loader, fallback, uses, activity_uses
):
    action = _restricted_action(loader, uses=uses, activity_uses=activity_uses)
    claw = next(a for a in loader.get_monster("magma-mephit").actions if a.slug == "claw")
    live, actor, _, _ = setup(loader, action, [(6, 5)], extra_actions=[claw] if fallback else [])
    pool = copy.deepcopy(live.monster_action_uses_by_entity[actor.entity_id][action.slug])
    before_rng = live.rng.getstate()
    run_async(orch.advance_monster_turn(orch.CombatHandle(live.handle_id)))
    assert not events(live, SaveRolled)
    assert live.monster_action_uses_by_entity[actor.entity_id][action.slug] == pool
    assert bool(events(live, AttackRolled)) == fallback
    if not fallback:
        assert not events(live, DamageApplied)
        assert live.rng.getstate() == before_rng
        assert actor.action_available
        assert events(live, IntentSubmitted)[-1].intent_type == "pass"


@pytest.mark.parametrize("departure", ["dead", "departed"])
def test_monster_stale_nonarea_target_does_not_resolve_or_spend(loader, departure):
    action = _restricted_action(loader)
    live, actor, target, monster = setup(loader, action, [(6, 5), (7, 5)])
    target.creature_type = "undead"
    if departure == "dead":
        live.dead_ids.add(target.entity_id)
    else:
        live.initiative.remove(target)
    before = (_state(live), live.rng.getstate(), list(live.event_log))
    ctx = _ctx(
        caster=actor, targets=[target], rng=live.rng, event_emitter=lambda e: orch._emit(live, e)
    )
    assert orch._resolve_monster_execution(live, actor, ctx, action.activities, action.slug) == ()
    execute(live, actor, target, monster, action)
    assert (_state(live), live.rng.getstate(), live.event_log) == before


def test_monster_matching_named_target_still_resolves_and_replays(loader):
    def run():
        action = _restricted_action(loader)
        live, actor, target, _ = setup(loader, action, [(6, 5)])
        target.creature_type = "undead"
        pre = len(live.event_log)
        run_async(orch.advance_monster_turn(orch.CombatHandle(live.handle_id)))
        assert [e.target_id for e in events(live, SaveRolled)] == [target.entity_id]
        assert (
            live.monster_action_uses_by_entity[actor.entity_id][action.slug].action_uses_remaining
            == 1
        )
        return (
            [e.model_dump_json() for e in live.event_log[pre:]],
            live.rng.getstate(),
            _state(live),
        )

    assert run() == run()


def test_legendary_named_type_mismatch_preserves_legendary_pool(loader):
    action = _restricted_action(loader).model_copy(update={"legendary_cost": 1})
    live, actor, _, monster = setup(loader, action, [(6, 5)])
    monster = monster.model_copy(update={"legendary_actions": [action]})
    set_lib_loader_for_tests(MemoryAssetLoader(monsters=[monster]))
    actor.legendary_actions_remaining = 1
    before = (_state(live), live.rng.getstate(), list(live.event_log))
    with pytest.raises(orch.IntentRejectedError, match="no usable legendary action"):
        orch._take_legendary_action(live, actor)
    assert (_state(live), live.rng.getstate(), live.event_log) == before


@pytest.mark.parametrize("succeeded", [False, True])
def test_unknown_on_save_never_defaults_to_full_damage(succeeded):
    with pytest.raises(ValueError, match="on_save"):
        _scale_on_save(11, "unknown", succeeded=succeeded)


def test_invalid_save_policy_rejects_before_any_resolver_draw_or_event(loader):
    activity = loader.get_spell("fireball").activities[0]
    activity = activity.model_copy(
        update={"damage": activity.damage.model_copy(update={"on_save": "unknown"})}
    )
    emitted = []
    ctx = _ctx(event_emitter=emitted.append)
    before = ctx.rng.getstate()
    with pytest.raises(ValueError, match="on_save"):
        resolve_save(activity, ctx)
    assert not emitted
    assert ctx.rng.getstate() == before


@pytest.mark.parametrize("source", ["direct", "wand"])
def test_invalid_child_save_policy_refuses_before_public_payment(loader, source):
    spell = loader.get_spell("fireball")
    activity = spell.activities[0]
    bad = activity.model_copy(
        update={"damage": activity.damage.model_copy(update={"on_save": "unknown"})}
    )
    set_lib_loader_for_tests(
        MemoryAssetLoader(
            spells=[spell.model_copy(update={"activities": [bad]})], items=[loader.get_item(WAND)]
        )
    )
    handle, live = _fireball_setup()
    before = (_state(live), live.rng.getstate())
    _cast_fireball(handle, source=source)
    assert events(live, CastFailed)
    assert not events(live, IntentSubmitted)
    assert not events(live, SaveRolled)
    assert (_state(live), live.rng.getstate()) == before


def test_invalid_monster_child_save_policy_skips_before_uses_or_rng(loader):
    live, actor, _, monster, action, spell = _spell_setup(loader)
    activity = spell.activities[0]
    bad = activity.model_copy(
        update={"damage": activity.damage.model_copy(update={"on_save": "unknown"})}
    )
    set_lib_loader_for_tests(
        MemoryAssetLoader(
            monsters=[monster], spells=[spell.model_copy(update={"activities": [bad]})]
        )
    )
    before = (_state(live), live.rng.getstate(), list(live.event_log))
    assert orch._monster_cast_candidate(live, actor, action) is None
    assert (_state(live), live.rng.getstate(), live.event_log) == before
    run_async(orch.advance_monster_turn(orch.CombatHandle(live.handle_id)))
    assert not events(live, SaveRolled)
    assert not events(live, DamageApplied)
    assert live.rng.getstate() == before[1]
    assert (
        live.monster_action_uses_by_entity[actor.entity_id][action.slug].action_uses_remaining == 2
    )


@pytest.mark.parametrize("source", ["direct", "wand"])
def test_blocked_point_origin_refuses_before_payment(loader, source):
    handle, live = _fireball_setup(grid=GridScene(width=40, height=20, blocked_cells=["10,9"]))
    before = (_state(live), live.rng.getstate())
    _cast_fireball(handle, source=source, point="10,9")
    assert events(live, CastFailed)
    assert not events(live, IntentSubmitted)
    assert (_state(live), live.rng.getstate()) == before


def test_blocked_target_cell_is_excluded_and_cannot_host_a_combatant(loader):
    scene = GridScene(width=40, height=20, blocked_cells=["13,10"])
    cells = area_cells(
        GridTopology(scene),
        area_template(loader.get_spell("fireball").activities[0]),
        "10,10",
        None,
    )
    assert "13,10" not in cells
    assert "10,10" in cells
    with pytest.raises(ValueError, match=r"start cell.*blocked"):
        _fireball_setup(grid=scene)


def test_line_of_effect_endpoints_follow_cover_not_movement_occupancy():
    grid = GridTopology(
        GridScene(
            width=10,
            height=10,
            blocked_cells=["5,5"],
            cover_cells={"3,3": "half", "4,4": "three_quarters", "6,6": "total"},
            wall_segments=[{"x1": 8, "y1": 0, "x2": 8, "y2": 10}],
        )
    )
    assert not has_line_of_effect(grid, "4,5", "5,5")
    # Existing source-cell contract is distinct from reaching an endpoint.
    assert has_line_of_effect(grid, "5,5", "5,5")
    assert has_line_of_effect(grid, "6,6", "6,6")
    assert not has_line_of_effect(grid, "7,5", "8,5")
    assert has_line_of_effect(grid, "3,3", "3,3")
    assert has_line_of_effect(grid, "4,4", "4,4")
    assert has_line_of_effect(grid, "1,1", "2,1")


@pytest.mark.parametrize("source", ["direct", "wand"])
def test_caster_own_covered_cell_remains_a_legal_origin(loader, source):
    handle, live = _fireball_setup(
        caster=_caster(zone_id="10,9"),
        grid=GridScene(width=40, height=20, cover_cells={"10,9": "total"}),
    )
    _cast_fireball(handle, source=source, point="10,9")
    assert not events(live, CastFailed)
    assert CASTER in [e.target_id for e in events(live, SaveRolled)]


def test_repeated_pure_preflight_preserves_full_state_events_and_rng(loader):
    _, live = _fireball_setup()
    caster = next(c for c in live.initiative if c.entity_id == CASTER)
    before = (_state(live), live.rng.getstate(), list(live.event_log))
    spec = SpellDeliverySpec(origin_cell="10,10")
    plans = [
        plan_spell_delivery(live, caster, loader.get_spell("fireball"), spec) for _ in range(2)
    ]
    assert plans[0] == plans[1]
    with pytest.raises(DeliveryPlanningError):
        plan_spell_delivery(
            live,
            caster,
            loader.get_spell("fireball"),
            spec.model_copy(update={"origin_cell": "40,10"}),
        )
    assert (_state(live), live.rng.getstate(), live.event_log) == before


@pytest.mark.parametrize("origin", ["", "010,10", "40,10"])
def test_explicit_invalid_literal_origin_never_falls_back_to_caster(loader, origin):
    _, live = _fireball_setup()
    caster = next(c for c in live.initiative if c.entity_id == CASTER)
    before = (_state(live), live.rng.getstate(), list(live.event_log))
    with pytest.raises(DeliveryPlanningError):
        plan_spell_delivery(
            live, caster, loader.get_spell("fireball"), SpellDeliverySpec(origin_cell=origin)
        )
    assert (_state(live), live.rng.getstate(), live.event_log) == before


@pytest.mark.parametrize("source_kind", ["direct_spell", "item_cast", "monster_cast"])
def test_shared_area_type_filter_is_identical_for_each_delivery_source(loader, source_kind):
    _, live = _fireball_setup()
    caster = next(c for c in live.initiative if c.entity_id == CASTER)
    next(c for c in live.initiative if c.entity_id == "mon:east").creature_type = "undead"
    spell = loader.get_spell("fireball")
    activity = spell.activities[0]
    filtered = activity.model_copy(
        update={
            "target": activity.target.model_copy(
                update={"creature_filter": TargetCreatureFilter(include_creature_types=("undead",))}
            )
        }
    )
    before = (_state(live), live.rng.getstate(), list(live.event_log))
    plan = plan_spell_delivery(
        live,
        caster,
        spell.model_copy(update={"activities": [filtered]}),
        SpellDeliverySpec(origin_cell="10,10", source_kind=source_kind),
    )
    assert plan.target_ids == ("mon:east",)
    assert (_state(live), live.rng.getstate(), live.event_log) == before


@pytest.mark.parametrize("source", ["direct", "wand"])
def test_seeded_public_delivery_replays_events_rng_and_final_combat_state(loader, source):
    def run():
        handle, live = _fireball_setup()
        pre = len(live.event_log)
        _cast_fireball(handle, source=source)
        return (
            [e.model_dump_json() for e in live.event_log[pre:]],
            live.rng.getstate(),
            _state(live),
        )

    assert run() == run()


@pytest.mark.parametrize("slug", ["thunderwave", "slow", "phantasmal-force", DUST])
def test_primary_acceptances_replay_events_rng_and_final_state(loader, slug):
    def run():
        handle, live = start(
            [_caster(spells_known=[slug] if slug != DUST else [], spell_slots={1: 1, 2: 1, 3: 1})],
            seed=19,
            encounter=[
                _target("mon:chosen", 2, 0),
                _target("mon:other", 3, 1, creature_type="undead"),
            ],
            grid_scene=GridScene(width=20, height=20),
        )
        chosen = next(c for c in live.initiative if c.entity_id == "mon:chosen")
        chosen.intelligence = -30
        chosen.constitution = -30
        pre = len(live.event_log)
        if slug == DUST:
            act(handle, CASTER, intent_type="use_item", item_id=DUST)
        else:
            choices = (
                {"direction": (1, 0)}
                if slug == "thunderwave"
                else (
                    {"target_zone_id": "1,0", "target_ids": ["mon:chosen", "mon:other"]}
                    if slug == "slow"
                    else {"target_id": "mon:chosen"}
                )
            )
            act(
                handle,
                CASTER,
                intent_type="cast_spell",
                spell_id=slug,
                slot_level=loader.get_spell(slug).level,
                **choices,
            )
        assert not events(live, CastFailed)
        assert events(live, SaveRolled)
        return (
            [e.model_dump_json() for e in live.event_log[pre:]],
            live.rng.getstate(),
            _state(live),
        )

    assert run() == run()


@pytest.mark.parametrize("slug", ["fireball", "thunderwave"])
def test_public_monster_spell_replays_events_rng_and_final_state(loader, slug):
    def run():
        live, _, target, _, _, _ = _spell_setup(loader, slug)
        target.constitution = -30
        pre = len(live.event_log)
        run_async(orch.advance_monster_turn(orch.CombatHandle(live.handle_id)))
        assert events(live, DamageApplied)
        return (
            [e.model_dump_json() for e in live.event_log[pre:]],
            live.rng.getstate(),
            _state(live),
        )

    assert run() == run()


def test_monster_origin_search_skips_total_cover_but_keeps_empty_cells(loader):
    action = burst(loader, "sphere", size="5", distance="150")
    live, actor, _, _ = setup(
        loader,
        action,
        [(8, 5), (9, 5), (17, 5)],
        at=(0, 5),
        allies=[(8, 6)],
        grid=GridScene(width=30, height=30, blocked_cells=["8,4"]),
    )
    before = (_state(live), live.rng.getstate(), list(live.event_log))
    placement = orch._monster_area_placement(live, actor, action.activities)
    assert placement is not None
    assert placement.origin != "8,4"
    assert placement.origin not in live.actor_zone.values()
    assert (_state(live), live.rng.getstate(), live.event_log) == before
