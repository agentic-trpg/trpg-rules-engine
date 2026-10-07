"""Generic next-save consumption across shared and special saving throws."""

from __future__ import annotations

import random
from dataclasses import replace

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.monster import MonsterTraitMechanic

from dnd5e_engine import orchestrator as orch
from dnd5e_engine.activities.apply import apply_damage
from dnd5e_engine.activities.context import ActivityResolutionContext
from dnd5e_engine.activities.save_primitive import roll_save
from dnd5e_engine.death_saves import roll_death_save
from dnd5e_engine.events import (
    ConcentrationCheck,
    DamageApplied,
    DeathSaveRolled,
    EffectModifiersConsumed,
    SaveRolled,
)
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.live_effect_lifecycle import roll_live_save
from dnd5e_engine.live_save_modifiers import consume_next_save_modifier
from dnd5e_engine.types.combat import Combatant
from dnd5e_engine.types.effects import ActiveEffect, ActiveEffectChange
from tests.c20_support import act, combatant, events, monster_turn, pc, start
from tests.lifecycle_support import seed_repeat_save

HERO = "char:hero"


@pytest.fixture(autouse=True)
def _fresh_loader():
    set_lib_loader_for_tests(BundledAssetLoader())
    yield
    set_lib_loader_for_tests(None)


def _effect(
    *, target_id=HERO, origin="feature:source-a", disabled=False, mode="override", value=True
):
    return ActiveEffect(
        id="effect:save-grant",
        name="One use",
        target_id=target_id,
        origin=origin,
        disabled=disabled,
        changes=[
            ActiveEffectChange(key="flags.save.next_disadvantage", mode=mode, value=value),
            ActiveEffectChange(
                key="flags.cannot_make_opportunity_attacks", mode="override", value=True
            ),
            ActiveEffectChange(key="speed.reduction", mode="add", value=5),
        ],
    )


def _ctx(*, seed=7, **fields):
    target = Combatant(
        entity_id=HERO,
        entity_type="Character",
        name="Hero",
        initiative=20,
        hp_current=20,
        hp_max=20,
    )
    return ActivityResolutionContext(
        rng=random.Random(seed),
        caster=target,
        targets=[target],
        caster_abilities={ability: 10 for ability in ("str", "dex", "con", "int", "wis", "cha")},
        event_emitter=lambda event: None,
        **fields,
    ), target


@pytest.mark.parametrize("advantaged", [False, True])
@pytest.mark.parametrize("dc", [0, 99])
def test_next_save_is_consumed_even_when_canceled_or_save_succeeds(advantaged, dc):
    consumed = []
    ctx, target = _ctx(
        consume_next_save_modifier=lambda target_id: consumed.append(target_id) is None,
        passive_save_adv={HERO: ["WIS"]} if advantaged else {},
    )
    reference = random.Random(7)
    first = reference.randint(1, 20)
    natural = first if advantaged else min(first, reference.randint(1, 20))
    roll = roll_save(ctx, target, "wis", dc)
    assert consumed == [HERO]
    assert roll.mode == ("normal" if advantaged else "disadvantage")
    assert roll.sources == (("condition:target", "effect") if advantaged else ("effect",))
    assert roll.natural == natural
    assert roll.succeeded == (dc == 0)
    assert ctx.rng.getstate() == reference.getstate()


@pytest.mark.parametrize("ability", ["str", "dex"])
def test_condition_auto_failure_consumes_once_without_rng(ability):
    consumed = []
    ctx, target = _ctx(
        consume_next_save_modifier=lambda target_id: consumed.append(target_id) is None,
        passive_save_auto_fail={HERO: ["STR", "DEX"]},
    )
    before = ctx.rng.getstate()
    result = roll_save(ctx, target, ability, 10)
    assert consumed == [HERO]
    assert result.natural is None
    assert not result.succeeded
    assert result.sources == ()
    assert ctx.rng.getstate() == before


@pytest.mark.parametrize(
    "disabled,mode,value",
    [(True, "override", True), (False, "add", True), (False, "override", False)],
)
def test_disabled_false_or_wrong_mode_clause_is_not_consumed(disabled, mode, value):
    effect = _effect(disabled=disabled, mode=mode, value=value)
    _, live = start([pc()], seed=7, active_effects=[effect])
    before = list(live.active_effects[HERO])
    rng = live.rng.getstate()
    assert not consume_next_save_modifier(live, HERO)
    assert live.active_effects[HERO] == before
    assert not events(live, EffectModifiersConsumed)
    assert live.rng.getstate() == rng


def test_consumer_preserves_other_clauses_and_full_source_identity():
    _, live = start([pc()], seed=7, active_effects=[_effect(), _effect(origin="feature:source-b")])
    before = live.rng.getstate()
    assert consume_next_save_modifier(live, HERO)
    consumed = events(live, EffectModifiersConsumed)
    assert [(event.effect_id, event.origin, event.keys) for event in consumed] == [
        ("effect:save-grant", "feature:source-a", ("flags.save.next_disadvantage",)),
        ("effect:save-grant", "feature:source-b", ("flags.save.next_disadvantage",)),
    ]
    for effect in live.active_effects[HERO]:
        assert [change.key for change in effect.changes] == [
            "flags.cannot_make_opportunity_attacks",
            "speed.reduction",
        ]
    assert not consume_next_save_modifier(live, HERO)
    assert len(events(live, EffectModifiersConsumed)) == 2
    assert live.rng.getstate() == before


@pytest.mark.parametrize("magical", [False, True])
def test_explicit_magic_provenance_does_not_treat_ordinary_features_as_spells(magical):
    ctx, target = _ctx(save_is_magical=magical, lifecycle_source_kind="feature")
    target.trait_mechanics = [MonsterTraitMechanic.MAGIC_RESISTANCE]
    reference = random.Random(7)
    natural = reference.randint(1, 20)
    if magical:
        natural = max(natural, reference.randint(1, 20))
    result = roll_save(ctx, target, "con", 10)
    assert result.natural == natural
    assert result.mode == ("advantage" if magical else "normal")
    assert result.sources == (("trait",) if magical else ())
    assert ctx.rng.getstate() == reference.getstate()


def test_live_adapter_consumes_exactly_one_save_then_keeps_other_changes():
    _, live = start([pc()], seed=7, active_effects=[_effect()])
    reference = random.Random()
    reference.setstate(live.rng.getstate())
    first = min(reference.randint(1, 20), reference.randint(1, 20))
    second = reference.randint(1, 20)
    rolled = roll_live_save(live, combatant(live), "wis", 99)
    later = roll_live_save(live, combatant(live), "wis", 99)
    assert (rolled.mode, rolled.natural) == ("disadvantage", first)
    assert (later.mode, later.natural) == ("normal", second)
    assert len(events(live, EffectModifiersConsumed)) == 1
    assert live.rng.getstate() == reference.getstate()


def test_actual_repeat_save_consumes_one_use_at_target_turn_end():
    handle, live = start([pc()], seed=7, active_effects=[_effect()])
    seed_repeat_save(live, (HERO, "effect:repeat", "feature:source"), dc=99)
    act(handle, HERO, intent_type="pass")
    [save] = events(live, SaveRolled)
    assert save.advantage == "disadvantage"
    assert "effect" in save.sources
    consumed = events(live, EffectModifiersConsumed)
    assert len(consumed) == 1
    assert live.event_log.index(consumed[0]) < live.event_log.index(save)


def test_death_save_consumes_next_save_modifier_and_keeps_seeded_draw_order():
    ctx, target = _ctx()
    target.hp_current = 0
    consumed = []
    reference = random.Random(7)
    kept = min(reference.randint(1, 20), reference.randint(1, 20))
    result = roll_death_save(
        target,
        ctx.rng,
        consume_next_save_modifier=lambda target_id: consumed.append(target_id) is None,
    )
    assert consumed == [HERO]
    rolled = next(event for event in result.events if event.type == "death_save_rolled")
    assert rolled.roll_total == kept
    assert ctx.rng.getstate() == reference.getstate()


def test_undead_fortitude_uses_save_modifiers_bonus_and_lr_without_incoming_magic_advantage():
    ctx, target = _ctx(
        base_spell_level=2,
        save_is_magical=True,
        passive_save_modifiers={HERO: {"con": 3}},
        passive_save_bonus={HERO: "+1d4"},
        d20_test_penalty={HERO: -2},
        legendary_resistance_armed={HERO: 1},
        legendary_resistances_remaining_by_entity={HERO: 2},
        consume_next_save_modifier=lambda _: True,
    )
    target.hp_current = 1
    target.trait_mechanics = [
        MonsterTraitMechanic.UNDEAD_FORTITUDE,
        MonsterTraitMechanic.MAGIC_RESISTANCE,
    ]
    emitted = []
    ctx = replace(ctx, event_emitter=emitted.append)
    reference = random.Random(7)
    natural = min(reference.randint(1, 20), reference.randint(1, 20))
    total = natural + 1 + reference.randint(1, 4)
    apply_damage(target, {"fire": 30}, ctx)
    save = next(event for event in emitted if event.type == "save_rolled")
    assert save.roll_total == total
    assert save.modifier == 1
    assert save.advantage == "disadvantage"
    assert save.sources == ["effect"]
    assert save.succeeded
    assert [event.type for event in emitted] == [
        "save_rolled",
        "legendary_resistance_used",
        "damage_applied",
    ]
    assert ctx.legendary_resistances_remaining_by_entity[HERO] == 1
    assert ctx.rng.getstate() == reference.getstate()
    assert target.hp_current == 1


def test_actual_spell_save_and_monster_repeat_share_one_use_consumption():
    handle, live = start(
        [
            pc(
                class_slug="wizard",
                character_level=5,
                intelligence=40,
                spells_known=["hold-person"],
                spell_slots={2: 1},
            )
        ],
        seed=7,
        active_effects=[_effect(target_id="mon:foe")],
    )
    act(
        handle,
        HERO,
        intent_type="cast_spell",
        spell_id="hold-person",
        target_id="mon:foe",
        slot_level=2,
    )
    [initial] = events(live, SaveRolled)
    assert initial.advantage == "disadvantage"
    assert initial.sources == ["effect"]
    assert not initial.succeeded
    assert len(live.effect_lifecycles) == 1
    monster_turn(handle)
    initial, repeated = events(live, SaveRolled)
    assert repeated.advantage == "normal"
    assert repeated.dc == initial.dc
    assert len(events(live, EffectModifiersConsumed)) == 1


@pytest.mark.parametrize("option", ["grapple", "shove"])
def test_actual_unarmed_option_save_consumes_one_use(option):
    handle, live = start([pc(strength=18)], seed=7, active_effects=[_effect(target_id="mon:foe")])
    act(handle, HERO, intent_type=option, target_id="mon:foe")
    [save] = events(live, SaveRolled)
    assert save.advantage == "disadvantage"
    assert "effect" in save.sources
    assert len(events(live, EffectModifiersConsumed)) == 1


def test_actual_concentration_check_consumes_one_use_and_retains_other_clauses():
    _, live = start([pc()], seed=7, active_effects=[_effect()])
    live.concentration_chain[HERO] = [(HERO, "effect:focus", "cast:focus:hero")]
    orch._emit(
        live,
        DamageApplied(
            target_id=HERO,
            amount=1,
            damage_type="fire",
            is_overkill=False,
            source_actor_id="mon:foe",
        ),
    )
    [check] = events(live, ConcentrationCheck)
    assert check.advantage == "disadvantage"
    assert "effect" in check.sources
    assert len(events(live, EffectModifiersConsumed)) == 1
    assert any(change.key == "speed.reduction" for change in live.active_effects[HERO][0].changes)


def test_actual_dying_turn_save_consumes_one_use():
    _, live = start([pc()], seed=7, active_effects=[_effect()])
    live.tracked_hp[HERO] = 0
    orch._update_combatant(live, HERO, hp_current=0)
    reference = random.Random()
    reference.setstate(live.rng.getstate())
    kept = min(reference.randint(1, 20), reference.randint(1, 20))
    orch._maybe_roll_death_save(live)
    [save] = events(live, DeathSaveRolled)
    assert save.roll_total == kept
    assert len(events(live, EffectModifiersConsumed)) == 1
    assert live.rng.getstate() == reference.getstate()
