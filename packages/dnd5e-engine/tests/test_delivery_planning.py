"""Geometry, legality and replay contracts independent of live resource payment."""

from __future__ import annotations

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.common import SaveActivity, TargetBlock, TargetTemplateBlock
from pydantic import ValidationError

from dnd5e_engine.areas import area_cells, area_template
from dnd5e_engine.spatial import GridTopology
from dnd5e_engine.specs import GridScene
from dnd5e_engine.spell_delivery import DeliveryPlanningError, SpellDeliverySpec, plan_delivery
from dnd5e_engine.types.combat import Combatant


def _creature(entity_id: str) -> Combatant:
    return Combatant(
        entity_id=entity_id,
        entity_type="Monster",
        name=entity_id,
        initiative=10,
        hp_current=100,
        hp_max=100,
    )


def _plan(activity, spec, *, execution=False):
    return plan_delivery(
        [activity],
        spec,
        actor_id="caster",
        topology=GridTopology(GridScene(width=40, height=30)),
        positions={"caster": "1,1", "a": "10,10", "b": "11,10", "outside": "30,20"},
        creatures=[_creature(i) for i in ("caster", "a", "b", "outside")],
        enemy_ids=frozenset({"a", "b", "outside"}),
        ally_ids=frozenset({"caster"}),
        range_ft=120,
        execution=execution,
    )


def test_spec_is_immutable_and_rejects_client_mechanics_values():
    spec = SpellDeliverySpec(origin_cell="10,10")
    with pytest.raises(ValidationError):
        spec.origin_cell = "11,10"
    with pytest.raises(ValidationError):
        SpellDeliverySpec(radius=999)


def test_slow_point_cube_has_exact_8_by_8_footprint_and_includes_placement_cell():
    slow = BundledAssetLoader().get_spell("slow").activities[0]
    template = area_template(slow)
    assert template is not None
    grid = GridTopology(GridScene(width=40, height=30))
    cells = area_cells(grid, template, "10,10", None)
    assert len(cells) == 64
    assert {"10,10", "17,17"} <= cells
    assert not {"9,10", "18,10", "10,18"} & cells


def test_counted_selection_respects_geometry_and_does_not_fill_explicit_exclusions():
    slow = BundledAssetLoader().get_spell("slow").activities[0]
    spec = SpellDeliverySpec(
        origin_cell="10,10", selected_target_ids=("b", "a"), excluded_target_ids=("b",)
    )
    assert _plan(slow, spec).target_ids == ("a",)
    with pytest.raises(DeliveryPlanningError):
        _plan(slow, spec.model_copy(update={"selected_target_ids": ("outside",)}))
    # Live revalidation drops a selected creature that moved out of the area.
    assert (
        _plan(
            slow, spec.model_copy(update={"selected_target_ids": ("outside",)}), execution=True
        ).target_ids
        == ()
    )


@pytest.mark.parametrize(
    "shape,size", [("wall", "30"), ("sphere", "@item.level * 5"), ("sphere", "0")]
)
def test_unsupported_area_never_falls_back_to_named_target(shape, size):
    activity = SaveActivity(target=TargetBlock(template=TargetTemplateBlock(type=shape, size=size)))
    with pytest.raises(DeliveryPlanningError) as error:
        _plan(activity, SpellDeliverySpec(primary_target_id="a"))
    assert error.value.reason == "unsupported_area"


@pytest.mark.parametrize("width,lanes", [("1", 1), ("5", 1), ("10", 2), ("20", 4)])
def test_line_width_has_distinct_lanes_without_sideways_origin_row(width, lanes):
    activity = SaveActivity(
        target=TargetBlock(template=TargetTemplateBlock(type="line", size="30", width=width))
    )
    template = area_template(activity)
    grid = GridTopology(GridScene(width=40, height=30))
    cells = area_cells(grid, template, "10,10", (1, 0))
    assert len(cells) == 6 * lanes
    assert not any(cell.startswith("10,") for cell in cells)
    assert "16,10" in cells


def test_pull_reuses_forced_path_obstacle_and_occupancy_rules():
    grid = GridTopology(GridScene(width=20, height=10, blocked_cells=["5,2"]))
    assert grid.push_path("1,1", "6,1", 15, toward=True, occupied_cells={"4,1"}) == ["5,1"]
    assert grid.push_path("1,2", "6,2", 15, toward=True) == []


@pytest.mark.parametrize("incapacitated", [False, True])
def test_concentration_finalize_cannot_create_anchor_for_disabled_caster(incapacitated):
    import random

    from dnd5e_engine import orchestrator as orch
    from dnd5e_engine.activities.context import ActivityResolutionContext
    from dnd5e_engine.events import EffectApplied
    from dnd5e_engine.lib_loader import set_lib_loader_for_tests
    from tests.c20_support import combatant, pc, start

    set_lib_loader_for_tests(BundledAssetLoader())
    try:
        _, live = start([pc()], seed=19)
        caster = combatant(live)
        if incapacitated:
            from dnd5e_engine.types.conditions import ActiveCondition

            caster.conditions.append(
                ActiveCondition(
                    condition="incapacitated", source_entity_id="implied:stunned", scope="combat"
                )
            )
        else:
            caster.hp_current = 0
        ctx = ActivityResolutionContext(
            rng=random.Random(19),
            caster=caster,
            targets=[],
            event_emitter=lambda event: orch._emit(live, event),
            caster_abilities={},
        )
        spell = BundledAssetLoader().get_spell("slow")
        before = len(live.event_log)
        orch._fold_resolution_outcome(
            live, caster, spell=spell, actx=ctx, pre_event_count=before, concentration_max_rounds=10
        )
        assert not live.concentration_chain.get(caster.entity_id)
        assert not caster.concentration_effect_id
        assert not any(
            isinstance(event, EffectApplied) and event.effect.flags.get("concentration")
            for event in live.event_log[before:]
        )
    finally:
        set_lib_loader_for_tests(None)


def test_nested_delegation_refreshes_child_spell_book_and_parent_identity(monkeypatch):
    from dnd5e_srd_data.schema.common import CastActivity, CastSpellBlock

    from dnd5e_engine import spell_execution
    from dnd5e_engine.events import DamageApplied
    from dnd5e_engine.lib_loader import set_lib_loader_for_tests
    from dnd5e_engine.spell_execution import ActivityReview
    from tests.c20_support import act, events, pc, start

    bundled = BundledAssetLoader()
    fireball = bundled.get_spell("fireball")
    missile = bundled.get_spell("magic-missile")
    nested = fireball.model_copy(
        update={
            "activities": [
                CastActivity(
                    id="nested-cast", spell=CastSpellBlock(uuid=missile.foundry_uuid, level=1)
                )
            ]
        }
    )
    # This synthetic wrapper has an explicit reviewed contract; production
    # unknown activities still fail closed (covered by admission regressions).
    reviews = dict(spell_execution.spell_reviews())
    reviews[fireball.foundry_uuid] = reviews[fireball.foundry_uuid].model_copy(
        update={
            "activities": (ActivityReview(activity_id="nested-cast", kind="cast", role="cast"),)
        }
    )
    monkeypatch.setattr(spell_execution, "spell_reviews", lambda: reviews)

    class Overlay(BundledAssetLoader):
        def get_spell_by_uuid(self, uuid):
            return nested if uuid == fireball.foundry_uuid else super().get_spell_by_uuid(uuid)

    set_lib_loader_for_tests(Overlay())
    try:
        handle, live = start([pc(equipment=("wand-of-fireballs",))], seed=19)
        act(
            handle,
            "char:hero",
            intent_type="use_item",
            item_id="wand-of-fireballs",
            target_id="mon:foe",
        )
        damage = events(live, DamageApplied)
        assert len(damage) == 3
        assert all(event.source_id.startswith("spell:magic-missile:") for event in damage)
        assert all(event.source_parent_id == "spell:fireball:nested-cast" for event in damage)
    finally:
        set_lib_loader_for_tests(None)


@pytest.mark.parametrize("charges,accepted", [(1, False), (2, True)])
def test_delegated_count_uses_child_cast_level_before_item_payment(charges, accepted):
    from dnd5e_engine.events import CastFailed, DamageApplied
    from dnd5e_engine.lib_loader import set_lib_loader_for_tests
    from tests.c20_support import act, combatant, events, pc, start

    set_lib_loader_for_tests(BundledAssetLoader())
    try:
        handle, live = start([pc(equipment=("wand-of-magic-missiles",))], seed=19)
        before = live.rng.getstate()
        act(
            handle,
            "char:hero",
            intent_type="use_item",
            item_id="wand-of-magic-missiles",
            target_ids=("mon:foe",) * 4,
            charges_to_spend=charges,
        )
        assert bool(events(live, DamageApplied)) is accepted
        if not accepted:
            assert [event.reason for event in events(live, CastFailed)] == ["target_invalid"]
            assert live.rng.getstate() == before
            assert combatant(live).action_available
            assert not live.custom_counters_by_entity.get("char:hero")
    finally:
        set_lib_loader_for_tests(None)
