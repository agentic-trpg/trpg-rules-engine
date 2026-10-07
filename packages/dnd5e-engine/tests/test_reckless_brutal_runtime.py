"""Public Reckless declarations and Brutal commitments follow actual rolls."""

from __future__ import annotations

import copy
from collections.abc import Iterator
from random import Random

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader

from dnd5e_engine import orchestrator as orch
from dnd5e_engine.attack_riders import AttackRiderRequest
from dnd5e_engine.events import (
    AttackFailed,
    AttackRiderTriggered,
    AttackRolled,
    ConcentrationCheck,
    ConditionApplied,
    ConditionRemoved,
    DamageApplied,
    EffectApplied,
    EffectExpired,
    ReactionTriggered,
    SaveRolled,
)
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.types.effects import ActiveEffect, ActiveEffectChange
from tests.c20_support import act, combatant, events, foe, pc, start

HERO = "char:hero"
FOE = "mon:foe"
BRUTAL = "nN5gsB6AcSQ4uQPN"


@pytest.fixture(autouse=True)
def _loader() -> Iterator[None]:
    set_lib_loader_for_tests(BundledAssetLoader())
    yield
    set_lib_loader_for_tests(None)


def _barbarian(**fields):
    return pc(
        **(
            {
                "class_slug": "barbarian",
                "character_level": 9,
                "strength": 20,
                "dexterity": 16,
                "constitution": 16,
                "hp_current": 500,
                "hp_max": 500,
                "ac": 1,
                "equipment": ("longsword", "dagger", "greataxe", "javelin"),
            }
            | fields
        )
    )


def _brutal(*options):
    return AttackRiderRequest(
        feature_id="brutal-strike",
        activity_id=BRUTAL,
        option_ids=options or ("hamstring-blow",),
    )


def _attack(handle, *, target=FOE, weapon="longsword", brutal=False, **fields):
    act(
        handle,
        HERO,
        intent_type="attack",
        weapon_id=weapon,
        target_id=target,
        attack_riders=(_brutal(),) if brutal else (),
        **fields,
    )


def _reckless(live):
    return [
        e
        for e in live.active_effects.get(HERO, [])
        if e.lifecycle and e.lifecycle.source_slug == "reckless-attack"
    ]


def _snapshot(live):
    return copy.deepcopy(
        (
            live.initiative,
            live.active_effects,
            live.custom_counters_by_entity,
            live.rider_uses,
            live.effect_lifecycles,
            live.current_actor_id,
            live.actor_zone,
            live.rng.getstate(),
        )
    )


def _reject_preserves(live, before, offset):
    assert _snapshot(live) == before
    emitted = live.event_log[offset:]
    assert len(emitted) == 1
    assert isinstance(emitted[0], AttackFailed)


def _effect(*keys, target=HERO, statuses=()) -> ActiveEffect:
    return ActiveEffect(
        id="test:attack-sources",
        name="Sources",
        origin="test:reckless",
        target_id=target,
        statuses=list(statuses),
        changes=[ActiveEffectChange(key=k, mode="override", value=True) for k in keys],
    )


def test_first_strength_roll_declares_before_roll_and_advantage_persists() -> None:
    handle, live = start([_barbarian()], seed=7)
    _attack(handle, reckless_attack=True)
    first = events(live, AttackRolled)[0]
    assert first.advantage == "advantage"
    assert combatant(live).attack_rolls_made_this_turn == 1
    [effect] = _reckless(live)
    assert {c.key for c in effect.changes} == {
        "flags.advantage.attack.strength",
        "flags.advantage.attack_against",
    }
    applied = next(e for e in events(live, EffectApplied) if e.effect.id == effect.id)
    assert live.event_log.index(applied) < live.event_log.index(first)
    _attack(handle)
    assert events(live, AttackRolled)[1].advantage == "advantage"
    assert combatant(live).attack_rolls_made_this_turn == 2
    assert len(_reckless(live)) == 1


def test_first_dexterity_attack_activates_only_later_strength_advantage() -> None:
    handle, live = start([_barbarian(strength=16, dexterity=20)], seed=7)
    _attack(handle, weapon="dagger", reckless_attack=True)
    assert events(live, AttackRolled)[0].advantage == "normal"
    assert _reckless(live)
    _attack(handle)
    assert events(live, AttackRolled)[1].advantage == "advantage"


def test_non_owner_declaration_is_draw_free_and_atomic() -> None:
    handle, live = start([_barbarian(class_slug="fighter", character_level=9)], seed=7)
    before, offset = _snapshot(live), len(live.event_log)
    _attack(handle, reckless_attack=True)
    _reject_preserves(live, before, offset)
    assert not _reckless(live)


def test_second_actual_roll_cannot_first_declare_reckless() -> None:
    handle, live = start([_barbarian()], seed=7)
    _attack(handle)
    before, offset = _snapshot(live), len(live.event_log)
    _attack(handle, reckless_attack=True)
    _reject_preserves(live, before, offset)
    assert not _reckless(live)


def test_first_spell_attack_counts_for_later_declaration_gate() -> None:
    handle, live = start(
        [
            _barbarian(
                class_slug="wizard",
                character_level=12,
                classes={"barbarian": 9, "wizard": 1, "fighter": 2},
                intelligence=18,
                spells_known=["fire-bolt"],
            )
        ],
        seed=7,
    )
    act(handle, HERO, intent_type="use_feature", feature_id="action-surge")
    act(handle, HERO, intent_type="cast_spell", spell_id="fire-bolt", target_id=FOE)
    assert combatant(live).attack_rolls_made_this_turn == 1
    before, offset = _snapshot(live), len(live.event_log)
    _attack(handle, reckless_attack=True)
    _reject_preserves(live, before, offset)


def test_static_range_failure_never_attaches_reckless_or_draws() -> None:
    handle, live = start([_barbarian()], seed=7, encounter=[foe(zone_id="8,0")])
    before, offset = _snapshot(live), len(live.event_log)
    _attack(handle, reckless_attack=True)
    _reject_preserves(live, before, offset)
    assert not _reckless(live)


def test_reckless_remains_after_miss() -> None:
    handle, live = start([_barbarian()], seed=7, encounter=[foe(ac=100)])
    _attack(handle, reckless_attack=True)
    assert not events(live, AttackRolled)[0].is_hit
    assert _reckless(live)
    _attack(handle)
    assert events(live, AttackRolled)[1].advantage == "advantage"


@pytest.mark.parametrize("incoming", ["weapon", "spell", "save"])
def test_target_held_reckless_advantage_applies_to_incoming_attack_rolls_only(incoming) -> None:
    other = pc(
        "char:other",
        initiative=10,
        zone_id="0,1",
        class_slug="wizard",
        character_level=5,
        intelligence=18,
        spells_known=["fire-bolt", "sacred-flame"],
        equipment=("dagger",),
    )
    handle, live = start([_barbarian(), other], seed=7, encounter=[foe(zone_id="8,8")])
    # An adjacent ally is a valid attack target and leaves the distant foe idle.
    _attack(handle, target="char:other", weapon="unarmed-strike", reckless_attack=True)
    act(handle, HERO, intent_type="pass")
    offset = len(live.event_log)
    if incoming == "weapon":
        act(handle, "char:other", intent_type="attack", weapon_id="dagger", target_id=HERO)
    else:
        act(
            handle,
            "char:other",
            intent_type="cast_spell",
            spell_id="fire-bolt" if incoming == "spell" else "sacred-flame",
            target_id=HERO,
        )
    emitted = live.event_log[offset:]
    if incoming == "save":
        [save] = [e for e in emitted if isinstance(e, SaveRolled)]
        assert save.advantage == "normal"
        assert not any(isinstance(e, AttackRolled) for e in emitted)
    else:
        [roll] = [e for e in emitted if isinstance(e, AttackRolled)]
        assert roll.advantage == "advantage"


def test_off_turn_opportunity_uses_reckless_without_polluting_next_own_turn_gate() -> None:
    handle, live = start([_barbarian(opportunity_attack_weapon_id="longsword")], seed=7)
    _attack(handle, reckless_attack=True)
    act(handle, HERO, intent_type="pass")
    assert live.current_actor_id == FOE
    offset = len(live.event_log)
    act(handle, FOE, intent_type="move", target_zone_id="4,0")
    [opportunity] = [e for e in live.event_log[offset:] if isinstance(e, AttackRolled)]
    assert opportunity.is_opportunity_attack
    assert opportunity.advantage == "advantage"
    assert combatant(live).attack_rolls_made_this_turn == 1
    act(handle, FOE, intent_type="pass")
    assert live.current_actor_id == HERO
    assert combatant(live).attack_rolls_made_this_turn == 0
    assert not _reckless(live)
    assert any(e.target_id == HERO for e in events(live, EffectExpired))
    _attack(handle, weapon="javelin", reckless_attack=True)
    assert events(live, AttackRolled)[-1].advantage == "advantage"


def test_first_reckless_hit_reprojects_for_cleave_and_counts_both_real_rolls() -> None:
    handle, live = start(
        [_barbarian()],
        seed=7,
        encounter=[foe(), foe(entity_id="mon:second", zone_id="1,1", initiative=0)],
    )
    _attack(handle, weapon="greataxe", reckless_attack=True)
    attacks = events(live, AttackRolled)
    assert [e.target_id for e in attacks] == [FOE, "mon:second"]
    assert [e.advantage for e in attacks] == ["advantage", "advantage"]
    assert combatant(live).attack_rolls_made_this_turn == 2
    assert combatant(live).cleave_spent_this_turn


def test_only_chosen_brutal_roll_loses_advantage_and_contributes_damage_before_cleave() -> None:
    handle, live = start(
        [_barbarian()],
        seed=7,
        encounter=[foe(), foe(entity_id="mon:second", zone_id="1,1", initiative=0)],
    )
    expected = Random()
    expected.setstate(live.rng.getstate())
    first_natural = expected.randint(1, 20)
    first_amount = sum(expected.randint(1, 12) for _ in range(2 if first_natural == 20 else 1)) + 5
    first_amount += expected.randint(1, 10)
    second_natural = max(expected.randint(1, 20), expected.randint(1, 20))
    second_amount = sum(expected.randint(1, 12) for _ in range(2 if second_natural == 20 else 1))
    _attack(handle, weapon="greataxe", reckless_attack=True, brutal=True)
    assert [e.advantage for e in events(live, AttackRolled)] == ["normal", "advantage"]
    damage = events(live, DamageApplied)
    assert [(e.target_id, e.amount) for e in damage] == [
        (FOE, first_amount),
        ("mon:second", second_amount),
    ]
    assert damage[0].damage_instance_id != damage[1].damage_instance_id
    assert [(e.target_id, e.feature_id) for e in events(live, AttackRiderTriggered)] == [
        (FOE, "brutal-strike")
    ]
    assert combatant(live).attack_rolls_made_this_turn == 2
    assert live.rng.getstate() == expected.getstate()


class _TwoTargetAttackLoader(BundledAssetLoader):
    """A future counted attack carrier reuses the public multi-target seam."""

    def get_spell(self, slug):
        spell = super().get_spell(slug)
        if slug != "fire-bolt" or spell is None:
            return spell
        activity = spell.activities[0]
        target = activity.target.model_copy(
            update={
                "affects": activity.target.affects.model_copy(
                    update={"count": "2 + @item.level", "type": "creature"}
                )
            }
        )
        return spell.model_copy(
            update={"activities": [activity.model_copy(update={"target": target})]}, deep=True
        )


def test_every_roll_in_one_counted_attack_activity_updates_first_roll_gate() -> None:
    set_lib_loader_for_tests(_TwoTargetAttackLoader())
    handle, live = start(
        [
            _barbarian(
                class_slug="wizard",
                character_level=12,
                classes={"barbarian": 9, "wizard": 1, "fighter": 2},
                intelligence=18,
                spells_known=["fire-bolt"],
            )
        ],
        seed=7,
        encounter=[foe(), foe(entity_id="mon:second", zone_id="1,1", initiative=0)],
    )
    act(handle, HERO, intent_type="use_feature", feature_id="action-surge")
    act(
        handle, HERO, intent_type="cast_spell", spell_id="fire-bolt", target_ids=(FOE, "mon:second")
    )
    assert [e.target_id for e in events(live, AttackRolled)] == [FOE, "mon:second"]
    assert combatant(live).attack_rolls_made_this_turn == 2
    before, offset = _snapshot(live), len(live.event_log)
    _attack(handle, reckless_attack=True)
    _reject_preserves(live, before, offset)


@pytest.mark.parametrize("case", ["no_reckless", "non_strength", "spell"])
def test_brutal_foundation_and_actual_attack_category_refuse_before_rng(case) -> None:
    handle, live = start(
        [_barbarian(strength=16, dexterity=20, spells_known=["fire-bolt"])], seed=7
    )
    before, offset = _snapshot(live), len(live.event_log)
    if case == "spell":
        act(
            handle,
            HERO,
            intent_type="cast_spell",
            spell_id="fire-bolt",
            target_id=FOE,
            attack_riders=(_brutal(),),
        )
    else:
        _attack(
            handle,
            weapon="dagger" if case == "non_strength" else "longsword",
            brutal=True,
            reckless_attack=case == "non_strength",
        )
    _reject_preserves(live, before, offset)


@pytest.mark.parametrize("source", ["flag", "cancelled", "restrained", "unseen", "long_range"])
def test_brutal_dynamic_disadvantage_rejection_rolls_back_all_declaration_state(source) -> None:
    keys = ("flags.disadvantage.attack",) if source in ("flag", "cancelled") else ()
    if source == "cancelled":
        keys += ("flags.advantage.attack",)
    effect = _effect(
        *keys,
        target=FOE if source == "unseen" else HERO,
        statuses=("restrained",)
        if source == "restrained"
        else (("invisible",) if source == "unseen" else ()),
    )
    handle, live = start(
        [_barbarian()],
        seed=7,
        encounter=[foe(zone_id="8,0" if source == "long_range" else "1,0")],
        active_effects=[effect],
    )
    # This pending target modifier is consumed before the dynamic check and
    # must be restored along with the tentative declaration and action payment.
    orch._emit(live, EffectApplied(effect=_effect("flags.attack.next_advantage", target=FOE)))
    before, offset = _snapshot(live), len(live.event_log)
    _attack(
        handle,
        weapon="javelin" if source == "long_range" else "longsword",
        reckless_attack=True,
        brutal=True,
    )
    _reject_preserves(live, before, offset)
    assert not _reckless(live)
    assert not live.rider_uses
    assert not events(live, AttackRolled)


def test_brutal_miss_commits_choice_but_later_reckless_attack_keeps_advantage() -> None:
    handle, live = start([_barbarian()], seed=7, encounter=[foe(ac=100)])
    _attack(handle, reckless_attack=True, brutal=True)
    [first] = events(live, AttackRolled)
    assert not first.is_hit
    assert first.advantage == "normal"
    assert live.rider_uses
    assert not events(live, DamageApplied)
    assert not events(live, AttackRiderTriggered)
    before, offset = _snapshot(live), len(live.event_log)
    _attack(handle, brutal=True)
    _reject_preserves(live, before, offset)
    _attack(handle)
    assert events(live, AttackRolled)[1].advantage == "advantage"


@pytest.mark.parametrize("brutal", [False, True])
def test_shield_miss_preserves_reckless_and_pre_roll_brutal_commit(brutal) -> None:
    shielded = pc(
        "char:shielded",
        initiative=30,
        class_slug="wizard",
        character_level=5,
        zone_id="1,0",
        ac=12,
        hp_current=500,
        hp_max=500,
        spells_known=["shield"],
        spell_slots={1: 2},
    )
    # Seed 7 normal natural=11; advantage keeps 11. +4 provisionally hits
    # AC12, and Shield AC17 turns that same roll into a miss.
    handle, live = start(
        [shielded, _barbarian(attack_bonus=4)], seed=7, encounter=[foe(zone_id="8,8")]
    )
    act(handle, "char:shielded", intent_type="ready", spell_id="shield")
    before = Random()
    before.setstate(live.rng.getstate())
    first = before.randint(1, 20)
    if not brutal:
        first = max(first, before.randint(1, 20))
    _attack(handle, target="char:shielded", reckless_attack=True, brutal=brutal)
    [roll] = events(live, AttackRolled)
    assert (roll.natural, roll.is_hit) == (first, False)
    assert len(events(live, ReactionTriggered)) == 1
    assert not events(live, DamageApplied)
    assert not events(live, AttackRiderTriggered)
    assert _reckless(live)
    assert bool(live.rider_uses) == brutal
    assert live.rng.getstate() == before.getstate()
    if brutal:
        state, offset = _snapshot(live), len(live.event_log)
        _attack(handle, target="char:shielded", brutal=True)
        _reject_preserves(live, state, offset)


def test_brutal_can_choose_later_own_turn_attack_without_removing_reckless() -> None:
    handle, live = start([_barbarian()], seed=7)
    _attack(handle, reckless_attack=True)
    _attack(handle, brutal=True)
    assert [e.advantage for e in events(live, AttackRolled)] == ["advantage", "normal"]
    assert _reckless(live)
    [rider] = [e for e in events(live, AttackRiderTriggered) if e.feature_id == "brutal-strike"]
    assert rider.option_ids == ("hamstring-blow",)


@pytest.mark.parametrize("level,count", [(9, 1), (17, 2)])
@pytest.mark.parametrize("seed", [7, 5])
def test_brutal_extra_damage_uses_canonical_scale_and_one_damage_instance(
    level, count, seed
) -> None:
    handle, live = start([_barbarian(character_level=level)], seed=seed)
    expected = Random()
    expected.setstate(live.rng.getstate())
    natural = expected.randint(1, 20)
    amount = sum(expected.randint(1, 8) for _ in range(2 if natural == 20 else 1)) + 5
    amount += sum(expected.randint(1, 10) for _ in range(count))
    _attack(handle, reckless_attack=True, brutal=True)
    [damage] = events(live, DamageApplied)
    assert (damage.amount, damage.damage_type, damage.source_actor_id) == (amount, "slashing", HERO)
    assert damage.damage_instance_id
    assert live.rng.getstate() == expected.getstate()


def test_reckless_and_brutal_replay_is_deterministic() -> None:
    def run():
        handle, live = start([_barbarian(character_level=17)], seed=7)
        _attack(handle, reckless_attack=True, brutal=True)
        _attack(handle)
        return [e.model_dump(mode="json") for e in live.event_log], _snapshot(live)

    first_events, first_state = run()
    second_events, second_state = run()
    assert first_events == second_events
    assert first_state == second_state


def _frenzy(live):
    return [e for e in events(live, AttackRiderTriggered) if e.feature_id == "frenzy"]


def _frenzy_used(live):
    return any(
        entity == HERO and feature == "frenzy" for entity, feature, _serial in live.rider_uses
    )


def test_actual_strength_miss_preserves_frenzy_for_later_final_hit() -> None:
    handle, live = start([_barbarian(subclass_slug="berserker")], seed=7, encounter=[foe(ac=100)])
    act(handle, HERO, intent_type="use_feature", feature_id="rage")
    _attack(handle, reckless_attack=True)
    [miss] = events(live, AttackRolled)
    assert not miss.is_hit
    assert miss.advantage == "advantage"
    assert not _frenzy(live)
    assert not _frenzy_used(live)
    assert not events(live, DamageApplied)
    orch._update_combatant(live, FOE, ac=1)
    _attack(handle)
    assert events(live, AttackRolled)[1].is_hit
    [trigger] = _frenzy(live)
    assert trigger.damage_formula == "3d6"
    assert _frenzy_used(live)
    assert live.event_log.index(events(live, DamageApplied)[0]) < live.event_log.index(trigger)


def test_shield_miss_preserves_frenzy_for_later_hit_against_the_live_shield() -> None:
    shielded = pc(
        "char:shielded",
        initiative=30,
        class_slug="wizard",
        character_level=5,
        zone_id="1,0",
        ac=12,
        hp_current=500,
        hp_max=500,
        spells_known=["shield"],
        spell_slots={1: 2},
    )
    handle, live = start(
        [shielded, _barbarian(subclass_slug="berserker", attack_bonus=4)],
        seed=7,
        encounter=[foe(zone_id="8,8")],
    )
    act(handle, "char:shielded", intent_type="ready", spell_id="shield")
    act(handle, HERO, intent_type="use_feature", feature_id="rage")
    expected = Random()
    expected.setstate(live.rng.getstate())
    first = max(expected.randint(1, 20), expected.randint(1, 20))
    _attack(handle, target="char:shielded", reckless_attack=True)
    [miss] = events(live, AttackRolled)
    assert (miss.natural, miss.roll_total, miss.is_hit) == (first, first + 4, False)
    assert len(events(live, ReactionTriggered)) == 1
    assert not events(live, DamageApplied)
    assert not _frenzy(live)
    assert not _frenzy_used(live)
    assert live.rng.getstate() == expected.getstate()
    second = max(expected.randint(1, 20), expected.randint(1, 20))
    amount = sum(expected.randint(1, 8) for _ in range(2 if second == 20 else 1)) + 5 + 3
    amount += sum(expected.randint(1, 6) for _ in range(3))
    _attack(handle, target="char:shielded")
    assert events(live, AttackRolled)[1].is_hit
    assert len(_frenzy(live)) == 1
    [damage] = events(live, DamageApplied)
    assert damage.amount == amount
    assert len(events(live, ReactionTriggered)) == 1
    assert live.rng.getstate() == expected.getstate()


def test_rage_ending_after_a_miss_prevents_frenzy_on_later_reckless_hit() -> None:
    handle, live = start([_barbarian(subclass_slug="berserker")], seed=7, encounter=[foe(ac=100)])
    act(handle, HERO, intent_type="use_feature", feature_id="rage")
    _attack(handle, reckless_attack=True)
    assert not events(live, AttackRolled)[0].is_hit
    assert orch._rage_effect(live, HERO) is not None
    orch._emit(live, ConditionApplied(target_id=HERO, condition="incapacitated"))
    assert orch._rage_effect(live, HERO) is None
    assert any(
        e.effect_id == "effect:rage" and e.reason == "incapacitated"
        for e in events(live, EffectExpired)
    )
    orch._emit(live, ConditionRemoved(target_id=HERO, condition="incapacitated", all_sources=True))
    orch._update_combatant(live, FOE, ac=1)
    expected = Random()
    expected.setstate(live.rng.getstate())
    natural = max(expected.randint(1, 20), expected.randint(1, 20))
    amount = sum(expected.randint(1, 8) for _ in range(2 if natural == 20 else 1)) + 5
    _attack(handle)
    assert events(live, AttackRolled)[1].is_hit
    assert _reckless(live)
    assert not _frenzy(live)
    assert not _frenzy_used(live)
    assert events(live, DamageApplied)[0].amount == amount
    assert live.rng.getstate() == expected.getstate()


def test_available_frenzy_does_not_fire_on_off_turn_reckless_opportunity_hit() -> None:
    handle, live = start(
        [_barbarian(subclass_slug="berserker", opportunity_attack_weapon_id="longsword")],
        seed=7,
        encounter=[foe(ac=100)],
    )
    act(handle, HERO, intent_type="use_feature", feature_id="rage")
    _attack(handle, reckless_attack=True)
    assert not events(live, AttackRolled)[0].is_hit
    assert not _frenzy_used(live)
    act(handle, HERO, intent_type="pass")
    orch._update_combatant(live, FOE, ac=1)
    assert orch._rage_effect(live, HERO) is not None
    expected = Random()
    expected.setstate(live.rng.getstate())
    natural = max(expected.randint(1, 20), expected.randint(1, 20))
    amount = sum(expected.randint(1, 8) for _ in range(2 if natural == 20 else 1)) + 5 + 3
    offset = len(live.event_log)
    act(handle, FOE, intent_type="move", target_zone_id="4,0")
    [opportunity] = [e for e in live.event_log[offset:] if isinstance(e, AttackRolled)]
    assert opportunity.is_opportunity_attack
    assert opportunity.is_hit
    assert opportunity.advantage == "advantage"
    assert not _frenzy(live)
    assert not _frenzy_used(live)
    assert events(live, DamageApplied)[0].amount == amount
    assert live.rng.getstate() == expected.getstate()


def test_brutal_and_frenzy_same_hit_trigger_one_concentration_check_on_total_damage() -> None:
    handle, live = start([_barbarian(subclass_slug="berserker")], seed=7)
    act(handle, HERO, intent_type="use_feature", feature_id="rage")
    anchor = ActiveEffect(
        id="effect:combined-hit-concentration",
        name="Concentration",
        target_id=HERO,
        origin=f"cast:test:{FOE}",
    )
    orch._emit(live, EffectApplied(effect=anchor))
    live.concentration_chain[FOE] = [(HERO, anchor.id, anchor.origin)]
    orch._update_combatant(live, FOE, constitution=40, concentration_effect_id=anchor.id)
    expected = Random()
    expected.setstate(live.rng.getstate())
    natural = expected.randint(1, 20)
    amount = sum(expected.randint(1, 8) for _ in range(2 if natural == 20 else 1)) + 5 + 3
    amount += expected.randint(1, 10) + sum(expected.randint(1, 6) for _ in range(3))
    concentration_natural = expected.randint(1, 20)
    _attack(handle, reckless_attack=True, brutal=True)
    [damage] = events(live, DamageApplied)
    assert damage.amount == amount
    assert damage.damage_instance_id
    [check] = events(live, ConcentrationCheck)
    assert (check.target_id, check.dc, check.natural) == (
        FOE,
        max(10, amount // 2),
        concentration_natural,
    )
    assert live.event_log.index(damage) < live.event_log.index(check)
    assert [e.feature_id for e in events(live, AttackRiderTriggered)] == ["brutal-strike", "frenzy"]
    assert live.rng.getstate() == expected.getstate()
