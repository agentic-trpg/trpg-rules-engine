"""Review regressions for lifecycle refresh, cures, departure and transactions."""

from __future__ import annotations

from copy import deepcopy

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.lifecycle import EffectLifecycleSpec, RepeatSaveSpec

from dnd5e_engine import live_effect_lifecycle as lifecycle
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.activities.effects import bind_effect_lifecycle
from dnd5e_engine.events import (
    ConcentrationDropped,
    ConditionRemoved,
    DamageApplied,
    EffectApplied,
    EffectExpired,
    EffectModifiersConsumed,
    SaveRolled,
)
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.types.effects import ActiveEffect, ActiveEffectChange
from tests.c20_support import act, combatant, events, foe, pc, start

HERO, TARGET = "char:hero", "mon:foe"


@pytest.fixture(autouse=True)
def _loader():
    set_lib_loader_for_tests(BundledAssetLoader())
    yield
    set_lib_loader_for_tests(None)


def _effect(*, typed=True, status="poisoned", spec=None):
    effect = ActiveEffect(
        id="effect:refresh-test",
        name="Refresh test",
        target_id=TARGET,
        origin=f"feature:review:{HERO}",
        statuses={status} if status else set(),
    )
    return (
        bind_effect_lifecycle(
            effect,
            spec or EffectLifecycleSpec(repeat_save=RepeatSaveSpec()),
            source_id=HERO,
            source_kind="feature",
            source_slug="review-source",
            activity_id="review-activity",
            save_ability="con",
            save_dc=30,
        )
        if typed
        else effect
    )


@pytest.mark.parametrize("typed", [True, False])
def test_repeated_identity_refreshes_typed_effect_without_changing_legacy_stack(typed):
    _, live = start([pc()], seed=4)
    effect = _effect(typed=typed)
    identity = (TARGET, effect.id, effect.origin)
    orch._emit(live, EffectApplied(effect=effect))
    live.turn_serial += 1
    orch._emit(live, EffectApplied(effect=effect))
    copies = [e for e in live.active_effects[TARGET] if (e.id, e.origin) == identity[1:]]
    assert len(copies) == (1 if typed else 2)
    if typed:
        assert live.effect_lifecycles[identity].applied_turn_serial == live.turn_serial
    orch._emit(
        live,
        EffectExpired(
            target_id=TARGET, effect_id=effect.id, origin=effect.origin, reason="dispelled"
        ),
    )
    assert ("poisoned" in live.active_conditions[TARGET]) == (not typed)
    assert identity not in live.effect_lifecycles
    if not typed:
        orch._emit(
            live,
            EffectExpired(
                target_id=TARGET, effect_id=effect.id, origin=effect.origin, reason="dispelled"
            ),
        )
        assert "poisoned" not in live.active_conditions[TARGET]


def test_explicit_cure_stops_condition_only_repeat_without_future_rng():
    _, live = start([pc()], seed=4)
    effect = _effect()
    identity = (TARGET, effect.id, effect.origin)
    orch._emit(live, EffectApplied(effect=effect))
    orch._emit(live, ConditionRemoved(target_id=TARGET, condition="poisoned", all_sources=True))
    assert identity not in live.effect_lifecycles
    before = live.rng.getstate()
    lifecycle.run_repeats(live, TARGET)
    assert live.rng.getstate() == before
    assert not events(live, SaveRolled)


def test_legacy_repeated_concentration_identity_keeps_owner_until_last_instance_expires():
    _, live = start([pc()], seed=4)
    effect = _effect(typed=False).model_copy(update={"flags": {"concentration": True}})
    identity = (TARGET, effect.id, effect.origin)
    for _ in range(2):
        orch._emit(live, EffectApplied(effect=effect))
    live.concentration_chain[HERO] = [identity]
    orch._update_combatant(live, HERO, concentration_effect_id=effect.id)
    expiry = EffectExpired(
        target_id=TARGET, effect_id=effect.id, origin=effect.origin, reason="dispelled"
    )
    orch._emit(live, expiry)
    assert live.concentration_chain[HERO] == [identity]
    assert combatant(live).concentration_effect_id == effect.id
    assert "poisoned" in live.active_conditions[TARGET]
    orch._emit(live, expiry)
    assert HERO not in live.concentration_chain
    assert combatant(live).concentration_effect_id is None
    assert "poisoned" not in live.active_conditions[TARGET]


def test_multiple_repeats_do_not_restore_one_use_state_consumed_by_first_save():
    _, live = start([pc()], seed=4)
    spec = EffectLifecycleSpec(
        repeat_save=RepeatSaveSpec(), one_use_modifiers=("next_save_disadvantage",)
    )
    first = _effect(spec=spec).model_copy(
        update={
            "changes": [
                ActiveEffectChange(key="flags.save.next_disadvantage", mode="override", value=True)
            ]
        }
    )
    second = first.model_copy(update={"origin": "feature:review:second-source"})
    for effect in (first, second):
        orch._emit(live, EffectApplied(effect=effect))
    lifecycle.run_repeats(live, TARGET)
    assert [save.advantage for save in events(live, SaveRolled)] == ["disadvantage", "normal"]
    assert len(events(live, EffectModifiersConsumed)) == 2
    assert len(live.effect_lifecycles) == 2
    assert all(not state.remaining_one_use_modifiers for state in live.effect_lifecycles.values())


def test_expiring_concentration_targets_preserves_other_then_clears_last_caster_token():
    _, live = start(
        [pc()],
        seed=4,
        encounter=[foe(), foe(entity_id="mon:other", initiative=0, zone_id="2,0")],
    )
    first = _effect().model_copy(update={"flags": {"concentration": True}})
    second = first.model_copy(update={"target_id": "mon:other"})
    identities = [(e.target_id, e.id, e.origin) for e in (first, second)]
    for effect in (first, second):
        orch._emit(live, EffectApplied(effect=effect))
    live.concentration_chain[HERO] = identities[:]
    live.concentration_rounds_remaining[HERO] = 10
    orch._update_combatant(live, HERO, concentration_effect_id=first.id)
    lifecycle.expire_effect(live, identities[0], "save_succeeded")
    assert live.concentration_chain[HERO] == identities[1:]
    assert combatant(live).concentration_effect_id == first.id
    assert live.concentration_rounds_remaining[HERO] == 10
    lifecycle.expire_effect(live, identities[1], "save_succeeded")
    assert HERO not in live.concentration_chain
    assert HERO not in live.concentration_rounds_remaining
    assert combatant(live).concentration_effect_id is None
    assert not events(live, ConcentrationDropped)


def test_late_concentration_fold_does_not_restore_identity_expired_in_same_resolution():
    _, live = start([pc()], seed=4)
    effect = _effect().model_copy(update={"flags": {"concentration": True}})
    offset = len(live.event_log)
    orch._emit(live, EffectApplied(effect=effect))
    lifecycle.expire_effect(live, (TARGET, effect.id, effect.origin), "dispelled")
    orch._record_effect_lifecycle_links(live, combatant(live), offset)
    assert HERO not in live.concentration_chain
    assert combatant(live).concentration_effect_id is None
    assert not live.effect_lifecycles


def test_actual_source_departure_retains_nonconcentration_repeat_and_target_departure_cleans():
    _, live = start([pc()], seed=4)
    effect = _effect()
    identity = (TARGET, effect.id, effect.origin)
    orch._emit(live, EffectApplied(effect=effect))
    before = live.rng.getstate()
    orch._leave_roster(live, HERO, "spell_ended")
    assert identity in live.effect_lifecycles
    assert live.rng.getstate() == before
    lifecycle.run_repeats(live, TARGET)
    assert len(events(live, SaveRolled)) == 1
    lifecycle.record_damage(live, TARGET, "pending:departure", 0)
    orch._leave_roster(live, TARGET, "spell_ended")
    assert not live.effect_lifecycles
    assert not live.lifecycle_damage
    assert not live.conditions_by_effect


def test_public_feature_rollback_restores_registration_consumption_and_damage_cache(monkeypatch):
    import dnd5e_engine.live_spell_delivery as delivery

    handle, live = start(
        [pc(class_slug="barbarian", subclass_slug="berserker", character_level=14)], seed=4
    )
    orch._update_combatant(live, TARGET, wisdom=-20)
    grant = _effect(
        status="",
        spec=EffectLifecycleSpec(one_use_modifiers=("next_save_disadvantage",), maximum_rounds=10),
    ).model_copy(
        update={
            "changes": [
                ActiveEffectChange(key="flags.save.next_disadvantage", mode="override", value=True)
            ]
        }
    )
    orch._emit(live, EffectApplied(effect=grant))
    original = delivery.resolve_activity

    def broken(activity, ctx, **kwargs):
        original(activity, ctx, **kwargs)
        assert len(live.effect_lifecycles) == 2
        ctx.event_emitter(
            DamageApplied(
                target_id=TARGET,
                amount=0,
                damage_type="fire",
                is_overkill=False,
                damage_instance_id="pending:rollback",
            )
        )
        raise ValueError("injected lifecycle failure")

    monkeypatch.setattr(delivery, "resolve_activity", broken)
    before = deepcopy(
        (
            live.initiative,
            live.active_effects,
            live.effect_lifecycles,
            live.lifecycle_damage,
            live.custom_counters_by_entity,
            live.event_log,
            live.rng.getstate(),
        )
    )
    queue_size = live.event_queue.qsize()
    observed = []
    live.event_listeners.append(observed.append)
    with pytest.raises(ValueError, match="injected lifecycle failure"):
        act(
            handle,
            HERO,
            intent_type="use_feature",
            feature_id="intimidating-presence",
            activity_id="ZRHT8mOlea6T8XpP",
            excluded_target_ids=(HERO,),
        )
    assert (
        live.initiative,
        live.active_effects,
        live.effect_lifecycles,
        live.lifecycle_damage,
        live.custom_counters_by_entity,
        live.event_log,
        live.rng.getstate(),
    ) == before
    assert live.event_queue.qsize() == queue_size
    assert observed == []
    assert combatant(live).bonus_action_available
