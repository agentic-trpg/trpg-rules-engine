"""Per-activity targets and source chains survive existing lifecycle owners."""

from dataclasses import replace

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.common import Activity, ActivityTiming

from dnd5e_engine import orchestrator as orch
from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.context import ActivityResolutionContext
from dnd5e_engine.events import DamageApplied
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.specs import GridScene
from dnd5e_engine.timed_activities import resolve_spell_activities
from tests.c20_support import act, combatant, events, foe, pc, start

HERO = "char:hero"
FOE = "mon:foe"
OTHER = "mon:other"


@pytest.fixture(autouse=True)
def _loader():
    set_lib_loader_for_tests(BundledAssetLoader())
    yield
    set_lib_loader_for_tests(None)


def _combat():
    return start(
        [
            pc(
                class_slug="wizard",
                classes={"wizard": 7},
                character_level=7,
                intelligence=40,
                constitution=40,
                hp_current=500,
                hp_max=500,
                spell_slots={3: 3, 4: 3},
            )
        ],
        seed=7,
        encounter=[
            foe(zone_id="10,0"),
            foe(entity_id=OTHER, initiative=0, zone_id="11,0"),
        ],
        grid_scene=GridScene(width=30, height=10),
    )


def test_scheduler_uses_each_prepared_activitys_targets_and_captures_parent_source():
    _, live = _combat()
    spell = BundledAssetLoader().get_spell("magic-missile")
    assert spell is not None
    immediate = spell.activities[0]
    delayed = immediate.model_copy(
        update={
            "id": "test-delayed-damage",
            "timing": ActivityTiming(trigger="turn_end", subject="target", recurring=False),
        }
    )
    spell = spell.model_copy(update={"activities": [immediate, delayed]})
    caster = combatant(live, HERO)
    ctx = build_activity_context(
        caster,
        [combatant(live, FOE), combatant(live, OTHER)],
        rng=live.rng,
        event_emitter=lambda event: orch._emit(live, event),
        spellcasting_ability="int",
        base_spell_level=1,
        slot_level=1,
        concentration=False,
        source_passive_effects=[],
        spell_book={},
        passive_damage_modifiers={},
        save_modifiers={},
    )
    parent = "item:test-wand:cast-activity"
    ctx = replace(ctx, source_parent_id=parent)
    prepared: list[str] = []

    def prepare(activity: Activity, current: ActivityResolutionContext):
        prepared.append(activity.id)
        return replace(
            current,
            targets=[combatant(live, FOE if activity.id == immediate.id else OTHER)],
            activity_source_id=f"spell:{spell.slug}:{activity.id}",
        )

    returned = resolve_spell_activities(live, spell, ctx, prepare_activity=prepare)

    assert prepared == [immediate.id, delayed.id]
    assert [target.entity_id for target in returned.targets] == [FOE, OTHER]
    assert [event.target_id for event in events(live, DamageApplied)] == [FOE]
    assert len(live.timed_activities.pending) == 1
    pending = live.timed_activities.pending[0]
    assert pending.target_id == OTHER
    assert pending.activity_source_id == f"spell:magic-missile:{delayed.id}"
    assert pending.source_parent_id == parent

    orch._end_turn_and_advance(live, HERO)
    orch._end_turn_and_advance(live, FOE)
    orch._end_turn_and_advance(live, OTHER)

    damage = events(live, DamageApplied)[-1]
    assert damage.target_id == OTHER
    assert damage.source_id == pending.activity_source_id
    assert damage.source_parent_id == parent
    assert not live.timed_activities.pending


def test_public_timed_spell_reports_distinct_immediate_and_delayed_activity_identities():
    handle, live = _combat()
    act(handle, HERO, intent_type="cast_spell", spell_id="vitriolic-sphere", target_id=FOE)
    pending = live.timed_activities.pending[0]
    immediate = events(live, DamageApplied)[0]

    orch._end_turn_and_advance(live, HERO)
    orch._end_turn_and_advance(live, FOE)

    delayed = [event for event in events(live, DamageApplied) if event.target_id == FOE][-1]
    assert immediate.source_id == "spell:vitriolic-sphere:dnd5eactivity000"
    assert delayed.source_id == f"spell:vitriolic-sphere:{pending.activity.id}"
    assert immediate.source_id != delayed.source_id


def test_persistent_spell_keeps_child_activity_identity_at_later_boundary():
    handle, live = _combat()
    orch._update_combatant(live, HERO, class_slug="cleric", wisdom=40)
    live.actor_zone[FOE] = "2,0"
    act(handle, HERO, intent_type="cast_spell", spell_id="spirit-guardians")
    assert len(live.persistent_areas.areas) == 1
    area = live.persistent_areas.areas[0]
    assert area.activity_source_id == f"spell:spirit-guardians:{area.activity.id}"

    orch._end_turn_and_advance(live, HERO)
    orch._end_turn_and_advance(live, FOE)

    damage = next(event for event in events(live, DamageApplied) if event.target_id == FOE)
    assert damage.source_id == area.activity_source_id
    assert damage.source_parent_id is None
