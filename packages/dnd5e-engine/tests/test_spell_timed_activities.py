"""Acceptance tests for the shared spell turn-boundary scheduler."""

from copy import deepcopy
from dataclasses import replace

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader

from dnd5e_engine import orchestrator as orch
from dnd5e_engine.events import (
    CastFailed,
    ConcentrationDropped,
    DamageApplied,
    EffectExpired,
    SaveRolled,
    TurnPhase,
    TurnStarted,
)
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.specs import GridScene
from tests.c20_support import act, combatant, events, foe, pc, start

HERO = "char:hero"
FOE = "mon:foe"


@pytest.fixture(autouse=True)
def _loader():
    set_lib_loader_for_tests(BundledAssetLoader())
    yield
    set_lib_loader_for_tests(None)


def _combat(seed=7, **kwargs):
    return start(
        [
            pc(
                class_slug="wizard",
                character_level=7,
                classes={"wizard": 5, "fighter": 2},
                hp_current=500,
                hp_max=500,
                intelligence=40,
                constitution=40,
                spell_slots={1: 2, 3: 2, 4: 2, 6: 2, 9: 2},
                **kwargs,
            )
        ],
        seed=seed,
        encounter=[foe(zone_id="10,0")],
        grid_scene=GridScene(width=40, height=10),
    )


def _cast(handle, spell, **kwargs):
    act(handle, HERO, intent_type="cast_spell", spell_id=spell, target_id=FOE, **kwargs)


def _end(live):
    # Exercise the real lifecycle without introducing monster movement/attacks.
    orch._end_turn_and_advance(live, live.current_actor_id)


def _damage(live, target=FOE):
    return [e for e in events(live, DamageApplied) if e.target_id == target]


def test_weird_resolves_initial_save_once_then_recurs_at_target_turn_end():
    handle, live = _combat()
    _cast(handle, "weird")
    assert len(events(live, SaveRolled)) == 1
    assert len(_damage(live)) == 1
    assert len(live.timed_activities.pending) == 1
    assert not live.effect_lifecycles  # no competing generic repeat save
    pending = live.timed_activities.pending[0]
    assert pending.spell.slug == "weird"
    assert pending.activity.id == "nuStSySOEkwOUnXf"
    assert pending.effect_identity is not None
    assert pending.concentration_identity == pending.effect_identity
    act(handle, HERO, intent_type="pass")
    assert len(events(live, SaveRolled)) == 1  # target turn start is not its end
    _end(live)
    assert len(events(live, SaveRolled)) == 2
    assert len(_damage(live)) == 2
    assert len(live.timed_activities.pending) == 1
    act(handle, HERO, intent_type="pass")
    _end(live)
    assert len(events(live, SaveRolled)) == 3
    assert len(_damage(live)) == 3


def test_weird_success_ends_only_that_targets_effect_and_timed_work():
    handle, live = _combat()
    _cast(handle, "weird")
    identity = live.timed_activities.pending[0].effect_identity
    orch._update_combatant(live, FOE, wisdom=80)
    act(handle, HERO, intent_type="pass")
    _end(live)
    assert events(live, SaveRolled)[-1].succeeded
    assert not live.timed_activities.pending
    assert not combatant(live, FOE).conditions
    assert [(e.target_id, e.effect_id, e.origin) for e in events(live, EffectExpired)] == [identity]


@pytest.mark.parametrize("slot", [4, 6])
def test_vitriolic_sphere_lingering_damage_runs_once_and_does_not_upcast(slot):
    handle, live = _combat()
    _cast(handle, "vitriolic-sphere", slot_level=slot)
    assert len(events(live, SaveRolled)) == 1
    assert len(_damage(live)) == 1
    pending = live.timed_activities.pending[0]
    assert pending.slot_level == slot
    assert not pending.timing.scale_with_slot
    rng = deepcopy(live.rng)
    expected_damage = sum(rng.randint(1, 4) for _ in range(5))
    act(handle, HERO, intent_type="pass")
    _end(live)
    assert len(_damage(live)) == 2
    assert _damage(live)[-1].amount == expected_damage
    assert live.rng.getstate() == rng.getstate()
    assert not live.timed_activities.pending
    assert not live.active_effects.get(FOE)
    act(handle, HERO, intent_type="pass")
    _end(live)
    assert len(_damage(live)) == 2


def test_vitriolic_sphere_success_never_schedules_lingering_damage():
    handle, live = _combat()
    orch._update_combatant(live, FOE, dexterity=80)
    _cast(handle, "vitriolic-sphere")
    assert events(live, SaveRolled)[0].succeeded
    assert len(_damage(live)) == 1
    assert not live.timed_activities.pending


def test_next_turn_end_damage_on_current_actor_waits_for_its_next_turn():
    handle, live = _combat(dexterity=-30)
    act(handle, HERO, intent_type="cast_spell", spell_id="vitriolic-sphere", target_id=HERO)
    assert len(_damage(live, HERO)) == 1
    act(handle, HERO, intent_type="pass")
    assert len(_damage(live, HERO)) == 1
    assert live.timed_activities.pending
    _end(live)
    act(handle, HERO, intent_type="pass")
    assert len(_damage(live, HERO)) == 2
    assert not live.timed_activities.pending


def test_stinking_cloud_saves_only_at_turn_start_and_poison_expires_at_turn_end():
    handle, live = _combat()
    _cast(handle, "stinking-cloud")
    assert not events(live, SaveRolled)
    assert not _damage(live)
    assert not live.timed_activities.pending
    assert len(live.persistent_areas.areas) == 1
    assert live.persistent_areas.areas[0].duration.rounds == 10
    act(handle, HERO, intent_type="pass")
    assert len(events(live, SaveRolled)) == 1
    assert "poisoned" in live.active_conditions[FOE]
    save_index = live.event_log.index(events(live, SaveRolled)[0])
    assert isinstance(live.event_log[save_index - 1], TurnPhase)
    assert live.event_log[save_index - 1].phase == "turn_start"
    _end(live)
    assert "poisoned" not in live.active_conditions[FOE]
    assert len(live.persistent_areas.areas) == 1
    act(handle, HERO, intent_type="pass")
    assert len(events(live, SaveRolled)) == 2
    _end(live)
    # Boundary membership uses current positions, not the cast-time target set.
    live.actor_zone[FOE] = "30,0"
    act(handle, HERO, intent_type="pass")
    assert len(events(live, SaveRolled)) == 2


@pytest.mark.parametrize("cleanup", ["effect", "concentration", "death", "departure"])
def test_timed_work_is_cancelled_synchronously_without_rng(cleanup):
    handle, live = _combat()
    _cast(handle, "weird")
    pending = live.timed_activities.pending[0]
    rng = live.rng.getstate()
    if cleanup == "effect":
        target, effect_id, origin = pending.effect_identity
        orch._emit(
            live,
            EffectExpired(target_id=target, effect_id=effect_id, origin=origin, reason="duration"),
        )
    elif cleanup == "concentration":
        act(handle, HERO, intent_type="drop_concentration")
    elif cleanup == "death":
        orch._emit(
            live, DamageApplied(target_id=FOE, amount=500, damage_type="force", is_overkill=False)
        )
    else:
        orch._leave_roster(live, FOE, "zero_hp")
    assert not live.timed_activities.pending
    assert live.rng.getstate() == rng


def test_area_anchor_removal_cancels_recurring_work():
    handle, live = _combat()
    _cast(handle, "stinking-cloud")
    target, effect_id, origin = live.persistent_areas.areas[0].concentration_identity
    orch._emit(
        live, EffectExpired(target_id=target, effect_id=effect_id, origin=origin, reason="duration")
    )
    assert not live.timed_activities.pending
    assert not live.persistent_areas.areas


def test_same_boundary_uses_stable_cast_order_and_seeded_events(monkeypatch):
    from dnd5e_engine import timed_activities as timed

    real_resolve = timed.resolve_activity
    order = []

    def observed(activity, ctx, **kwargs):
        order.append(ctx.caster.entity_id)
        return real_resolve(activity, ctx, **kwargs)

    monkeypatch.setattr(timed, "resolve_activity", observed)

    def run():
        handle, live = start(
            [
                pc(class_slug="wizard", intelligence=40, spell_slots={4: 1}),
                pc(
                    "char:other",
                    zone_id="0,1",
                    class_slug="wizard",
                    initiative=15,
                    intelligence=40,
                    spell_slots={4: 1},
                ),
            ],
            seed=7,
            encounter=[foe(zone_id="10,0")],
            grid_scene=GridScene(width=40, height=10),
        )
        _cast(handle, "vitriolic-sphere")
        act(
            handle,
            "char:other",
            intent_type="cast_spell",
            spell_id="vitriolic-sphere",
            target_id=FOE,
        )
        assert [p.sequence for p in live.timed_activities.pending] == [0, 1]
        order.clear()
        _end(live)
        assert order == [HERO, "char:other"]
        assert len(_damage(live)) == 4
        return (
            [e.model_dump(exclude={"uuid", "handle_id"}) for e in live.event_log],
            live.initiative,
            live.timed_activities.pending,
            live.rng.getstate(),
        )

    assert run() == run()


def test_new_concentration_ends_old_before_new_resolution_but_refusals_preserve_it():
    handle, live = _combat()
    act(handle, HERO, intent_type="cast_spell", spell_id="bless", target_id=HERO)
    assert live.concentration_chain[HERO]
    _end(live)
    _end(live)
    rng = live.rng.getstate()
    _cast(handle, "weird", slot_level=8)  # unavailable slot: never starts casting
    assert events(live, CastFailed)[-1].reason == "no_slot"
    assert live.rng.getstate() == rng
    assert live.concentration_chain[HERO]
    before = len(live.event_log)
    _cast(handle, "weird")
    new = live.event_log[before:]
    dropped = next(i for i, e in enumerate(new) if isinstance(e, ConcentrationDropped))
    saved = next(i for i, e in enumerate(new) if isinstance(e, SaveRolled))
    assert dropped < saved


def test_self_applied_sleep_does_not_repeat_save_on_current_turn_end():
    """Self-inflicted Incapacitated ends Sleep's concentration immediately."""
    handle, live = _combat(wisdom=-30)
    act(
        handle,
        HERO,
        intent_type="cast_spell",
        spell_id="sleep",
        target_id=HERO,
        excluded_target_ids=(),
    )
    saves = len(events(live, SaveRolled))
    assert events(live, ConcentrationDropped)[-1].target_id == HERO
    assert not live.timed_activities.pending
    act(handle, HERO, intent_type="pass")
    assert len(events(live, SaveRolled)) == saves
    _end(live)
    _end(live)
    assert len(events(live, SaveRolled)) == saves


def test_turn_start_hook_removal_hands_off_without_stranding_initiative():
    handle, live = _combat()
    live.lifecycle.register(
        "turn_start",
        lambda combat, actor: (
            orch._leave_roster(combat, actor, "zero_hp") if actor == FOE else None
        ),
        key="test:departure",
    )
    act(handle, HERO, intent_type="pass")
    assert live.current_actor_id == HERO
    assert live.departed_actor_id is None
    assert [e.actor_id for e in events(live, TurnStarted)][-2:] == [FOE, HERO]
    assert live.round_number == 2


@pytest.mark.parametrize("spell", ["searing-smite", "ensnaring-strike"])
def test_start_of_turn_damage_is_deferred_and_stops_with_its_source(spell):
    handle, live = _combat()
    _cast(handle, spell)
    assert len(_damage(live)) == 1
    assert len(live.timed_activities.pending) == 1
    act(handle, HERO, intent_type="pass")
    assert len(_damage(live)) == 2
    if spell == "searing-smite":
        orch._update_combatant(live, FOE, constitution=80)
        _end(live)
        act(handle, HERO, intent_type="pass")
        assert events(live, SaveRolled)[-1].succeeded
        assert not live.timed_activities.pending
    else:
        orch._drop_concentration(live, HERO)
        assert not live.timed_activities.pending


def test_nonconcentration_lingering_damage_survives_its_casters_departure():
    handle, live = _combat()
    _cast(handle, "vitriolic-sphere")
    orch._leave_roster(live, HERO, "zero_hp")
    assert live.timed_activities.pending
    orch._hand_off_departed_turn(live)
    _end(live)
    assert len(_damage(live)) == 2
    assert not live.timed_activities.pending


def test_recurring_area_expires_with_concentration_duration_without_extra_draws():
    handle, live = _combat()
    _cast(handle, "stinking-cloud")
    live.concentration_rounds_remaining[HERO] = 1
    rng = live.rng.getstate()
    act(handle, HERO, intent_type="pass")
    assert not live.timed_activities.pending
    assert not live.persistent_areas.areas
    assert not events(live, SaveRolled)
    assert live.rng.getstate() == rng


def test_delegated_cast_uses_the_same_typed_scheduler():
    from dnd5e_srd_data.schema.common import CastActivity, CastSpellBlock

    from dnd5e_engine.activities.build_context import build_activity_context
    from dnd5e_engine.activities.resolver import resolve_activity
    from dnd5e_engine.timed_activities import resolve_spell_activities

    _, live = _combat()
    spell = BundledAssetLoader().get_spell("vitriolic-sphere")
    ctx = build_activity_context(
        combatant(live, HERO),
        [combatant(live, FOE)],
        rng=live.rng,
        event_emitter=lambda event: orch._emit(live, event),
        slot_level=None,
        base_spell_level=None,
        spellcasting_ability="int",
        concentration=False,
        source_passive_effects=[],
        passive_damage_modifiers={},
        save_modifiers={},
        spell_book={spell.foundry_uuid: spell},
    )
    ctx = replace(
        ctx, spell_dispatch=lambda source, child: resolve_spell_activities(live, source, child)
    )
    resolve_activity(CastActivity(spell=CastSpellBlock(uuid=spell.foundry_uuid)), ctx)
    assert len(_damage(live)) == 1
    assert len(live.timed_activities.pending) == 1
    _end(live)
    _end(live)
    assert len(_damage(live)) == 2


def test_timed_save_uses_captured_dc_without_rerolling_caster_bonus(monkeypatch):
    from dnd5e_engine.activities import build_context

    calls = []

    def bonus(caster, modifiers, rng):
        calls.append(caster.entity_id)
        return rng.randint(1, 4)

    monkeypatch.setattr(build_context, "_spell_dc_bonus", bonus)
    handle, live = _combat()
    _cast(handle, "weird")
    dc = events(live, SaveRolled)[0].dc
    act(handle, HERO, intent_type="pass")
    _end(live)
    assert calls == [HERO]
    assert [event.dc for event in events(live, SaveRolled)] == [dc, dc]


def test_countered_new_concentration_cast_ends_old_chain_but_keeps_slot():
    handle, live = start(
        [
            pc(
                class_slug="wizard",
                intelligence=40,
                constitution=-30,
                character_level=7,
                classes={"wizard": 5, "fighter": 2},
                spell_slots={1: 1, 9: 1},
            ),
            pc(
                "char:counter",
                initiative=0,
                zone_id="0,1",
                class_slug="wizard",
                character_level=5,
                intelligence=40,
                spell_slots={3: 1},
            ),
        ],
        seed=4,
    )
    act(handle, HERO, intent_type="cast_spell", spell_id="bless", target_id=HERO)
    for _ in range(3):
        _end(live)
    from dnd5e_engine.live_reactions import register_pending_reaction

    register_pending_reaction(
        live,
        "char:counter",
        orch.PlayerIntent(
            intent_type="ready",
            spell_id="counterspell",
            slot_level=3,
            reaction_trigger="cast_spell",
        ),
    )
    _cast(handle, "weird")
    assert events(live, CastFailed)[-1].reason == "countered"
    assert HERO not in live.concentration_chain
    assert not live.timed_activities.pending
    assert live.spell_slots_by_entity[HERO][9] == 1
    assert not combatant(live, HERO).action_available


def test_timed_start_damage_removes_summon_and_hands_off_to_next_actor():
    from dnd5e_engine.events import CombatantLeft, TurnEnded
    from tests.c21_support import summoner

    handle, live = start(
        [
            summoner(),
            pc(
                class_slug="wizard",
                intelligence=40,
                spell_slots={1: 1},
                initiative=15,
                zone_id="0,2",
            ),
        ],
        seed=7,
        encounter=[foe(zone_id="9,9")],
    )
    act(handle, "char:summoner", intent_type="cast_spell", spell_id="summon-dragon")
    spirit = next(iter(live.summons))
    act(handle, spirit, intent_type="pass")
    act(handle, HERO, intent_type="cast_spell", spell_id="searing-smite", target_id=spirit)
    live.tracked_hp[spirit] = 1
    orch._update_combatant(live, spirit, hp_current=1)
    act(handle, HERO, intent_type="pass")
    _end(live)  # foe -> summoner
    act(handle, "char:summoner", intent_type="pass")
    assert live.current_actor_id == HERO
    assert live.round_number == 2
    assert live.departed_actor_id is None
    assert spirit not in live.summons
    assert not live.timed_activities.pending
    assert [(e.entity_id, e.reason) for e in events(live, CombatantLeft)] == [(spirit, "zero_hp")]
    assert [e.actor_id for e in events(live, TurnEnded)].count(spirit) == 2
