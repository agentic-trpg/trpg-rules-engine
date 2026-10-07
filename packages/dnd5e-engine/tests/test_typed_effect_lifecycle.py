"""Typed ownership, boundary clocks, shared saves and complete damage instances."""

from __future__ import annotations

import json
from dataclasses import asdict
from random import Random

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.lifecycle import EffectLifecycleSpec, RepeatSaveSpec
from dnd5e_srd_data.schema.monster import MonsterTraitMechanic

from dnd5e_engine import live_effect_lifecycle as lifecycle
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.activities.context import DamageInstanceContext
from dnd5e_engine.activities.effects import bind_effect_lifecycle
from dnd5e_engine.events import (
    ConcentrationDropped,
    ConditionRemoved,
    DamageApplied,
    Death,
    EffectApplied,
    EffectExpired,
    LegendaryResistanceUsed,
    SaveRolled,
    TurnPhase,
)
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.types.effects import ActiveEffect, ActiveEffectChange, ActiveEffectDuration
from tests.c20_support import act, combatant, events, foe, pc, start, wizard

HERO, TARGET = "char:hero", "mon:foe"


@pytest.fixture(autouse=True)
def _loader():
    set_lib_loader_for_tests(BundledAssetLoader())
    yield
    set_lib_loader_for_tests(None)


def _combat(**fields):
    return start([pc()], seed=4, encounter=[foe(**fields)])


def _effect(live, *, spec=None, origin="source:a", status="poisoned", magical=False, dc=30):
    effect = ActiveEffect(
        id="effect:shared-template",
        origin=origin,
        name="Name has no semantics",
        target_id=TARGET,
        statuses={status} if status else set(),
        duration=ActiveEffectDuration(rounds=1, turns=1, seconds=6),
    )
    if spec is not None:
        effect = bind_effect_lifecycle(
            effect,
            spec,
            source_id=HERO,
            source_kind="spell" if magical else "feature",
            source_slug="canonical-source",
            activity_id="canonical-activity",
            save_ability="con",
            save_dc=dc,
            is_magical=magical,
        )
    before = live.rng.getstate()
    orch._emit(live, EffectApplied(effect=effect))
    assert live.rng.getstate() == before
    return (effect.target_id, effect.id, effect.origin)


def _repeat_spec(**fields):
    return EffectLifecycleSpec(repeat_save=RepeatSaveSpec(), **fields)


def test_failed_save_concentration_and_condition_without_metadata_never_registers():
    _, live = _combat()
    offset = len(live.event_log)
    orch._emit(
        live, SaveRolled(target_id=TARGET, ability="con", dc=30, roll_total=1, succeeded=False)
    )
    key = _effect(live)
    effect = live.active_effects[TARGET][0]
    live.active_effects[TARGET][0] = effect.model_copy(update={"flags": {"concentration": True}})
    orch._record_effect_lifecycle_links(live, combatant(live), offset)
    before = live.rng.getstate()
    lifecycle.run_repeats(live, TARGET)
    assert key not in live.effect_lifecycles
    assert live.rng.getstate() == before


def test_dominate_person_has_no_fabricated_end_turn_save():
    handle, live = start(
        [
            wizard(
                HERO,
                initiative=20,
                character_level=9,
                spells_known=["dominate-person"],
                spell_slots={5: 2},
                zone_id="0,0",
            )
        ],
        seed=4,
    )
    orch._update_combatant(live, TARGET, wisdom=-20)
    act(
        handle,
        HERO,
        intent_type="cast_spell",
        spell_id="dominate-person",
        target_id=TARGET,
        slot_level=5,
    )
    assert events(live, SaveRolled)[0].succeeded is False
    assert live.concentration_chain[HERO]
    assert "charmed" in live.active_conditions[TARGET]
    assert not live.effect_lifecycles
    before, offset = live.rng.getstate(), len(live.event_log)
    orch._run_end_of_turn_saves(live, TARGET)
    assert live.rng.getstate() == before
    assert live.event_log[offset:] == []


def test_same_turn_repeat_and_once_per_boundary_failed_then_successful():
    _, live = _combat()
    live.current_actor_id = TARGET
    key = _effect(live, spec=_repeat_spec())
    lifecycle.run_repeats(live, TARGET)
    assert key in live.effect_lifecycles
    assert len(events(live, SaveRolled)) == 1
    before = live.rng.getstate()
    lifecycle.run_repeats(live, TARGET)
    assert live.rng.getstate() == before
    orch._update_combatant(live, TARGET, constitution=100)
    live.turn_serial += 1
    lifecycle.run_repeats(live, TARGET)
    assert key not in live.effect_lifecycles
    assert events(live, EffectExpired)[-1].reason == "save_succeeded"
    assert len(events(live, ConditionRemoved)) == 1


@pytest.mark.parametrize("own_turn", [False, True])
def test_ten_round_deadline_owns_duration_despite_display_counter(own_turn):
    _, live = _combat()
    live.current_actor_id = TARGET if own_turn else HERO
    key = _effect(live, spec=EffectLifecycleSpec(maximum_rounds=10))
    for number in range(1, 11):
        live.round_number = number
        live.turn_serial += 1
        orch._tick_durations_at_turn_end(live, HERO)
        orch._expire_timed_effects_at_turn_end(live, TARGET)
        lifecycle.expire_at_boundary(live, TARGET, "end")
        assert key in live.effect_lifecycles
    live.round_number = 11
    lifecycle.expire_at_boundary(live, HERO, "end")
    assert key in live.effect_lifecycles
    lifecycle.expire_at_boundary(live, TARGET, "end")
    assert key not in live.effect_lifecycles
    assert len(events(live, EffectExpired)) == 1


@pytest.mark.parametrize(
    "boundary",
    [
        "source_next_turn_start",
        "source_next_turn_end",
        "target_next_turn_start",
        "target_next_turn_end",
    ],
)
def test_exact_next_boundaries_skip_application_turn_only(boundary):
    _, live = _combat()
    key = _effect(live, spec=EffectLifecycleSpec(expiry_boundary=boundary))
    actor = HERO if boundary.startswith("source") else TARGET
    phase = "start" if boundary.endswith("start") else "end"
    lifecycle.expire_at_boundary(live, actor, phase)
    assert key in live.effect_lifecycles
    live.turn_serial += 1
    lifecycle.expire_at_boundary(live, TARGET if actor == HERO else HERO, phase)
    assert key in live.effect_lifecycles
    lifecycle.expire_at_boundary(live, actor, phase)
    assert key not in live.effect_lifecycles


@pytest.mark.parametrize("amount,temporary,expires", [(0, 0, False), (5, 5, False), (6, 5, True)])
def test_legacy_damage_uses_positive_effective_damage(amount, temporary, expires):
    _, live = _combat()
    key = _effect(live, spec=EffectLifecycleSpec(expire_on_positive_damage=True))
    live.tracked_temp_hp[TARGET] = temporary
    orch._emit(
        live, DamageApplied(target_id=TARGET, amount=amount, damage_type="fire", is_overkill=False)
    )
    assert (key not in live.effect_lifecycles) == expires


def test_multitype_damage_keeps_condition_until_instance_completes_and_expires_once():
    _, live = _combat()
    key = _effect(
        live, spec=EffectLifecycleSpec(expire_on_positive_damage=True), status="unconscious"
    )
    before = live.rng.getstate()
    for kind in ("fire", "cold"):
        orch._emit(
            live,
            DamageApplied(
                target_id=TARGET,
                amount=3,
                damage_type=kind,
                is_overkill=False,
                damage_instance_id="instance:1",
            ),
        )
        assert key in live.effect_lifecycles
        assert "unconscious" in live.active_conditions[TARGET]
    damage = DamageInstanceContext("instance:1", HERO, TARGET, None, 6, ("fire", "cold"))
    lifecycle.damage_instance_completed(live, damage)
    lifecycle.damage_instance_completed(live, damage)
    assert len(events(live, EffectExpired)) == 1
    assert events(live, EffectExpired)[0].reason == "damaged"
    assert len(events(live, ConditionRemoved)) == 1
    assert live.rng.getstate() == before


def test_full_identity_cleanup_retains_other_origin_and_direct_condition():
    _, live = _combat()
    key = _effect(live, spec=_repeat_spec(), origin="source:a")
    other = _effect(live, spec=_repeat_spec(), origin="source:b")
    lifecycle.expire_effect(live, key, "dispelled")
    assert other in live.effect_lifecycles
    assert "poisoned" in live.active_conditions[TARGET]
    assert not events(live, ConditionRemoved)
    before = live.rng.getstate()
    orch._emit(live, events(live, EffectExpired)[0])
    assert other in live.effect_lifecycles
    assert live.rng.getstate() == before
    lifecycle.expire_effect(live, other, "dispelled")
    assert len(events(live, ConditionRemoved)) == 1


def test_condition_immunity_suppresses_registration():
    _, live = _combat(condition_immunities=["poisoned"])
    key = _effect(live, spec=_repeat_spec())
    assert key not in live.effect_lifecycles


@pytest.mark.parametrize("source_bound", [False, True])
def test_source_departure_only_ends_explicit_source_dependencies(source_bound):
    _, live = _combat()
    key = _effect(
        live, spec=_repeat_spec(expiry_boundary="source_next_turn_end" if source_bound else None)
    )
    lifecycle.actor_departed(live, HERO)
    assert (key not in live.effect_lifecycles) == source_bound
    lifecycle.actor_departed(live, TARGET)
    assert key not in live.effect_lifecycles


def test_target_death_cleans_typed_effect_and_condition():
    _, live = _combat()
    key = _effect(live, spec=_repeat_spec())
    orch._emit(live, Death(target_id=TARGET, reason="damage"))
    assert key not in live.effect_lifecycles
    assert not live.conditions_by_effect


@pytest.mark.parametrize("spell_slug,level", [("hold-person", 2), ("hold-monster", 5)])
def test_canonical_hold_repeat_magic_resistance_and_captured_dc(spell_slug, level):
    handle, live = start(
        [
            wizard(
                HERO,
                initiative=20,
                character_level=9,
                spells_known=[spell_slug],
                spell_slots={level: 3},
                zone_id="0,0",
            ),
        ],
        seed=4,
        encounter=[foe(creature_type="humanoid")],
    )
    orch._update_combatant(live, TARGET, wisdom=-20)
    act(
        handle,
        HERO,
        intent_type="cast_spell",
        spell_id=spell_slug,
        target_id=TARGET,
        slot_level=level,
    )
    [state] = live.effect_lifecycles.values()
    assert state.application.source_slug == spell_slug
    assert state.application.is_magical
    assert state.application.save_ability == "wis"
    dc = state.application.save_dc
    orch._update_combatant(live, HERO, intelligence=1, character_level=1)
    orch._update_combatant(live, TARGET, trait_mechanics=[MonsterTraitMechanic.MAGIC_RESISTANCE])
    expected = Random()
    expected.setstate(live.rng.getstate())
    expected.randint(1, 20)
    expected.randint(1, 20)
    live.turn_serial += 1
    offset = len(live.event_log)
    orch._run_end_of_turn_saves(live, TARGET)
    repeat = live.event_log[offset]
    assert isinstance(repeat, SaveRolled)
    assert repeat.dc == dc
    assert repeat.advantage == "advantage"
    assert repeat.sources == ["trait"]
    assert live.rng.getstate() == expected.getstate()
    assert live.effect_lifecycles
    orch._update_combatant(live, TARGET, wisdom=100)
    live.turn_serial += 1
    orch._run_end_of_turn_saves(live, TARGET)
    assert not live.effect_lifecycles
    assert not events(live, ConcentrationDropped)


def test_repeat_legendary_resistance_converts_failure_and_expires_in_event_order():
    _, live = _combat()
    _effect(live, spec=_repeat_spec())
    orch._update_combatant(
        live,
        TARGET,
        legendary_resistances_remaining=1,
        trait_mechanics=[MonsterTraitMechanic.LEGENDARY_RESISTANCE],
    )
    live.legendary_resistance_armed[TARGET] = 1
    offset = len(live.event_log)
    lifecycle.run_repeats(live, TARGET)
    assert [type(event) for event in live.event_log[offset:]] == [
        SaveRolled,
        LegendaryResistanceUsed,
        EffectExpired,
        ConditionRemoved,
    ]
    assert live.event_log[offset].succeeded
    assert combatant(live, TARGET).legendary_resistances_remaining == 0
    assert not live.legendary_resistance_armed.get(TARGET)
    assert not live.effect_lifecycles


@pytest.mark.parametrize("spell_slug,level", [("hold-person", 3), ("hold-monster", 6)])
def test_canonical_hold_multitarget_success_preserves_other_then_drop_cleans_all(spell_slug, level):
    second = "mon:second"
    handle, live = start(
        [
            wizard(
                HERO,
                initiative=20,
                character_level=11,
                spells_known=[spell_slug],
                spell_slots={level: 2},
                zone_id="0,0",
            )
        ],
        seed=4,
        encounter=[
            foe(creature_type="humanoid"),
            foe(entity_id=second, zone_id="1,1", creature_type="humanoid"),
        ],
    )
    for target_id in (TARGET, second):
        orch._update_combatant(live, target_id, wisdom=-20)
    act(
        handle,
        HERO,
        intent_type="cast_spell",
        spell_id=spell_slug,
        target_ids=(TARGET, second),
        slot_level=level,
    )
    assert len(live.effect_lifecycles) == 2
    orch._update_combatant(live, TARGET, wisdom=100)
    offset = len(live.event_log)
    orch._emit(live, TurnPhase(actor_id=TARGET, phase="turn_end", round_number=live.round_number))
    orch._run_end_of_turn_saves(live, TARGET)
    assert [type(event) for event in live.event_log[offset:]] == [
        TurnPhase,
        SaveRolled,
        EffectExpired,
        ConditionRemoved,
    ]
    assert len(live.effect_lifecycles) == 1
    assert len(live.concentration_chain[HERO]) == 1
    assert combatant(live).concentration_effect_id is not None
    assert "paralyzed" in live.active_conditions[second]
    assert not events(live, ConcentrationDropped)
    orch._drop_concentration(live, HERO)
    assert not live.effect_lifecycles
    assert not live.concentration_chain
    assert combatant(live).concentration_effect_id is None
    assert "paralyzed" not in live.active_conditions[second]
    before = live.rng.getstate()
    lifecycle.run_repeats(live, second)
    assert live.rng.getstate() == before


def test_one_use_lifecycle_tracks_consumption_without_expiring_other_changes():
    _, live = _combat()
    key = _effect(
        live,
        spec=EffectLifecycleSpec(
            one_use_modifiers=("next_save_disadvantage",), expiry_boundary="source_next_turn_start"
        ),
        status=None,
    )
    effect = live.active_effects[TARGET][0]
    live.active_effects[TARGET][0] = effect.model_copy(
        update={
            "changes": [
                ActiveEffectChange(key="flags.save.next_disadvantage", mode="override", value=True),
                ActiveEffectChange(
                    key="flags.cannot_make_opportunity_attacks", mode="override", value=True
                ),
            ]
        }
    )
    assert live.effect_lifecycles[key].remaining_one_use_modifiers == ("next_save_disadvantage",)
    roll = lifecycle.roll_live_save(live, combatant(live, TARGET), "wis", 10)
    assert roll.mode == "disadvantage"
    assert live.effect_lifecycles[key].remaining_one_use_modifiers == ()
    assert [c.key for c in live.active_effects[TARGET][0].changes] == [
        "flags.cannot_make_opportunity_attacks"
    ]


def test_same_seed_replays_events_state_and_rng_byte_equivalently():
    def replay():
        _, live = _combat()
        _effect(live, spec=_repeat_spec(maximum_rounds=10))
        lifecycle.run_repeats(live, TARGET)
        return json.dumps(
            {
                "events": [e.model_dump(mode="json") for e in live.event_log],
                "lifecycle": [
                    asdict(s) | {"application": s.application.model_dump(mode="json")}
                    for s in live.effect_lifecycles.values()
                ],
                "effects": {
                    k: [e.model_dump(mode="json") for e in v]
                    for k, v in live.active_effects.items()
                },
                "actors": [c.model_dump(mode="json") for c in live.initiative],
                "concentration": live.concentration_chain,
                "resources": live.custom_counters_by_entity,
                "rng": live.rng.getstate(),
            },
            sort_keys=True,
        )

    assert replay() == replay()
