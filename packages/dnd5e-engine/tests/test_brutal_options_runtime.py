"""Canonical Brutal option ownership, one-use clauses and automatic Frenzy."""

from __future__ import annotations

from copy import deepcopy
from random import Random

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader

from dnd5e_engine import AttackRiderRequest
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.activities.passive_stats import CombatantMovementModes
from dnd5e_engine.events import (
    AttackFailed,
    AttackRiderTriggered,
    AttackRolled,
    ConditionApplied,
    DamageApplied,
    EffectApplied,
    EffectExpired,
    EffectModifiersConsumed,
)
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.live_effect_lifecycle import roll_live_save
from dnd5e_engine.live_movement import effective_speed
from tests.c20_support import act, combatant, events, foe, monster_turn, pc, start
from tests.lifecycle_support import seed_repeat_save

HERO, FOE, ALLY = "char:hero", "mon:foe", "char:ally"
BRUTAL = "nN5gsB6AcSQ4uQPN"


@pytest.fixture(autouse=True)
def _loader():
    set_lib_loader_for_tests(BundledAssetLoader())
    yield
    set_lib_loader_for_tests(None)


def _barbarian(entity_id=HERO, *, level=17, berserker=False, **fields):
    return pc(
        entity_id,
        **(
            dict(
                class_slug="barbarian",
                character_level=level,
                strength=18,
                dexterity=20,
                subclass_slug="berserker" if berserker else None,
                hp_current=500,
                hp_max=500,
                equipment=("longsword", "dagger"),
            )
            | fields
        ),
    )


def _combat(*, level=17, berserker=False, seed=4, **enemy):
    return start(
        [
            _barbarian(level=level, berserker=berserker),
            pc(ALLY, initiative=10, zone_id="1,1", strength=18, attack_bonus=0),
        ],
        seed=seed,
        encounter=[foe(**enemy)],
    )


def _attack(handle, options=(), *, target=FOE, reckless=True, actor=HERO, **fields):
    requests = (
        (
            AttackRiderRequest(
                feature_id="brutal-strike",
                activity_id=BRUTAL,
                option_ids=options,
            ),
        )
        if options
        else ()
    )
    act(
        handle,
        actor,
        **(
            dict(
                intent_type="attack",
                target_id=target,
                weapon_id="longsword",
                reckless_attack=reckless,
                attack_riders=requests,
            )
            | fields
        ),
    )


def _effects(live, target=FOE):
    return [effect for effect in live.active_effects.get(target, []) if effect.lifecycle]


def _to_next_hero_turn(handle, live):
    serial = live.turn_serial
    for _ in range(6):
        if live.current_actor_id == HERO and live.turn_serial > serial:
            return
        if live.current_actor_id.startswith("mon:"):
            monster_turn(handle)
        else:
            act(handle, live.current_actor_id, intent_type="pass")
    raise AssertionError("no next hero turn")


@pytest.mark.parametrize(
    "options",
    [
        ("hamstring-blow", "staggering-blow"),
        ("staggering-blow", "sundering-blow"),
        ("sundering-blow", "hamstring-blow"),
    ],
)
def test_level17_two_distinct_options_preserve_order_and_roll_shared_damage_once(options):
    handle, live = _combat()
    expected = Random()
    expected.setstate(live.rng.getstate())
    natural = expected.randint(1, 20)
    weapon = sum(expected.randint(1, 8) for _ in range(2 if natural == 20 else 1))
    extra = sum(expected.randint(1, 10) for _ in range(2))
    _attack(handle, options)
    [damage] = events(live, DamageApplied)
    assert damage.amount == weapon + 4 + extra
    assert damage.damage_type == "slashing"
    [trigger] = events(live, AttackRiderTriggered)
    assert trigger.option_ids == options
    assert trigger.damage_activity_id == BRUTAL
    assert trigger.damage_formula == "2d10"
    applied = [e.effect for e in events(live, EffectApplied) if e.effect.target_id == FOE]
    assert [e.lifecycle.source_slug for e in applied] == [
        "brutal-strike" if option == "hamstring-blow" else "improved-brutal-strike"
        for option in options
    ]
    assert live.rng.getstate() == expected.getstate()


@pytest.mark.parametrize(
    "level,options",
    [
        (9, ("staggering-blow",)),
        (13, ("hamstring-blow", "staggering-blow")),
        (17, ("hamstring-blow", "hamstring-blow")),
        (17, ("unknown",)),
    ],
)
def test_unowned_over_capacity_duplicate_and_unknown_options_reject_before_payment(level, options):
    handle, live = _combat(level=level)
    before = deepcopy((live.initiative, live.active_effects, live.rider_uses, live.rng.getstate()))
    _attack(handle, options)
    assert events(live, AttackFailed)[-1].reason == "unsupported_rider"
    assert not events(live, AttackRolled)
    assert (live.initiative, live.active_effects, live.rider_uses, live.rng.getstate()) == before


def test_hamstring_reduces_every_speed_clamps_budget_and_expires_at_source_next_start():
    handle, live = _combat()
    orch._update_combatant(
        live,
        FOE,
        movement_modes=CombatantMovementModes(
            climb=40,
            swim=25,
            fly=60,
            burrow=20,
        ),
    )
    _attack(handle, ("hamstring-blow",))
    target = combatant(live, FOE)
    assert [
        effective_speed(target, mode, live) for mode in ("walk", "climb", "swim", "fly", "burrow")
    ] == [15, 25, 10, 45, 5]
    assert target.movement_remaining == 15
    [effect] = _effects(live)
    assert effect.lifecycle.spec.stacking == "latest_only"
    _to_next_hero_turn(handle, live)
    assert not _effects(live)
    assert events(live, EffectExpired)[-1].reason == "duration"


def test_hamstring_latest_source_replaces_previous_full_identity_instead_of_stacking():
    handle, live = start(
        [
            _barbarian(level=13),
            _barbarian(ALLY, level=13, initiative=10, zone_id="1,1"),
        ],
        seed=4,
    )
    _attack(handle, ("hamstring-blow",))
    [old] = _effects(live)
    act(handle, HERO, intent_type="pass")
    _attack(handle, ("hamstring-blow",), actor=ALLY)
    [new] = _effects(live)
    assert old.origin != new.origin
    assert new.lifecycle.source_id == ALLY
    assert effective_speed(combatant(live, FOE), "walk", live) == 15
    assert (FOE, old.id, old.origin) not in live.effect_lifecycles
    assert len([e for e in events(live, EffectExpired) if e.origin == old.origin]) == 1


@pytest.mark.parametrize("auto_fail,advantaged", [(False, False), (False, True), (True, False)])
def test_staggering_consumes_only_next_save_clause_including_cancel_and_auto_fail(
    auto_fail, advantaged
):
    handle, live = _combat()
    _attack(handle, ("staggering-blow",))
    if auto_fail:
        orch._emit(live, ConditionApplied(target_id=FOE, condition="unconscious"))
    if advantaged:
        orch._set_dodging(live, FOE)
    ability = "str" if auto_fail else "dex" if advantaged else "wis"
    result = roll_live_save(live, combatant(live, FOE), ability, 99)
    assert result.mode == ("normal" if auto_fail or advantaged else "disadvantage")
    [effect] = _effects(live)
    assert [change.key for change in effect.changes] == ["flags.cannot_make_opportunity_attacks"]
    state = live.effect_lifecycles[(FOE, effect.id, effect.origin)]
    assert not state.remaining_one_use_modifiers
    assert len(events(live, EffectModifiersConsumed)) == 1
    _to_next_hero_turn(handle, live)
    assert not _effects(live)


def test_staggering_repeat_save_consumes_next_clause_and_keeps_oa_suppression():
    handle, live = _combat()
    _attack(handle, ("staggering-blow",))
    seed_repeat_save(live, (FOE, "effect:repeat-test", "test:repeat"), source_id=HERO, dc=99)
    act(handle, HERO, intent_type="pass")
    act(handle, ALLY, intent_type="pass")
    monster_turn(handle)
    assert len(events(live, EffectModifiersConsumed)) == 1
    # At this point hero's next turn has begun, so the remainder expired exactly
    # at the source boundary rather than during consumption.
    consumed = events(live, EffectModifiersConsumed)[0]
    expired = [e for e in events(live, EffectExpired) if e.effect_id == consumed.effect_id]
    assert len(expired) == 1
    assert live.event_log.index(consumed) < live.event_log.index(expired[0])


@pytest.mark.parametrize("ac", [1, 100])
def test_sundering_excludes_source_and_other_creature_consumes_on_hit_or_miss(ac):
    handle, live = _combat()
    _attack(handle, ("sundering-blow",))
    _attack(handle, reckless=False)
    assert not events(live, EffectModifiersConsumed)
    orch._update_combatant(live, FOE, ac=ac)
    act(handle, HERO, intent_type="pass")
    _attack(handle, actor=ALLY, reckless=False)
    attack = events(live, AttackRolled)[-1]
    assert attack.modifier == 5
    assert attack.is_hit == (ac == 1)
    [consumed] = events(live, EffectModifiersConsumed)
    assert consumed.keys == ("attack.next_bonus",)
    assert _effects(live)[0].changes == []


def test_multiple_sundering_grants_select_only_oldest_applicable_without_stacking():
    handle, live = _combat()
    _attack(handle, ("sundering-blow",))
    [first] = _effects(live)
    second = first.model_copy(
        update={
            "origin": "rider:second-origin",
            "lifecycle": first.lifecycle.model_copy(update={"source_id": "char:second-source"}),
        }
    )
    orch._emit(live, EffectApplied(effect=second))
    act(handle, HERO, intent_type="pass")
    _attack(handle, actor=ALLY, reckless=False)
    assert events(live, AttackRolled)[-1].modifier == 5
    [consumed] = events(live, EffectModifiersConsumed)
    assert consumed.origin == first.origin
    assert _effects(live)[1].changes[0].key == "attack.next_bonus"


@pytest.mark.parametrize("rage,reckless", [(False, True), (True, False), (True, True)])
def test_frenzy_automatic_requires_live_rage_and_reckless_and_fires_once(rage, reckless):
    handle, live = _combat(berserker=True)
    if rage:
        act(handle, HERO, intent_type="use_feature", feature_id="rage")
    _attack(handle, reckless=reckless)
    _attack(handle, reckless=False)
    triggers = [e for e in events(live, AttackRiderTriggered) if e.feature_id == "frenzy"]
    assert len(triggers) == int(rage and reckless)
    if triggers:
        assert triggers[0].damage_formula == "4d6"
        assert triggers[0].damage_activity_id == "myPBq8xozti108Mc"


def test_non_strength_hit_and_miss_preserve_frenzy_until_first_qualifying_final_hit():
    handle, live = _combat(berserker=True)
    act(handle, HERO, intent_type="use_feature", feature_id="rage")
    _attack(handle, reckless=True, weapon_id="dagger")
    assert not [e for e in events(live, AttackRiderTriggered) if e.feature_id == "frenzy"]
    _attack(handle, reckless=False)
    assert len([e for e in events(live, AttackRiderTriggered) if e.feature_id == "frenzy"]) == 1


def test_brutal_and_frenzy_share_one_authoritative_damage_instance_and_canonical_dice():
    handle, live = _combat(berserker=True)
    act(handle, HERO, intent_type="use_feature", feature_id="rage")
    reference = Random()
    reference.setstate(live.rng.getstate())
    natural = reference.randint(1, 20)
    base = sum(reference.randint(1, 8) for _ in range(2 if natural == 20 else 1))
    brutal = sum(reference.randint(1, 10) for _ in range(2))
    frenzy = sum(reference.randint(1, 6) for _ in range(4))
    _attack(handle, ("hamstring-blow",))
    [damage] = events(live, DamageApplied)
    assert damage.amount == base + 4 + 4 + brutal + frenzy
    assert damage.damage_type == "slashing"
    assert damage.damage_instance_id is not None
    assert [e.feature_id for e in events(live, AttackRiderTriggered)] == ["brutal-strike", "frenzy"]
    assert live.rng.getstate() == reference.getstate()


@pytest.mark.parametrize("mutation", ["uses", "save", "duration", "area", "cost"])
def test_option_carrier_drift_rejects_before_any_attack_or_rng(mutation):
    bundled = BundledAssetLoader()
    feature = bundled.get_feature("improved-brutal-strike").model_copy(deep=True)
    option = next(a for a in feature.activities if a.id == "I30qGlPDcyKwz65H")
    if mutation == "uses":
        option.uses = option.uses.model_copy(update={"max": "1"})
    elif mutation == "save":
        from dnd5e_srd_data.schema.common import SaveActivity

        replacement = SaveActivity(id=option.id, kind="save")
        feature.activities = [replacement if a.id == option.id else a for a in feature.activities]
    elif mutation == "duration":
        option.duration = option.duration.model_copy(update={"concentration": True})
    elif mutation == "area":
        option.target = option.target.model_copy(
            update={
                "template": option.target.template.model_copy(update={"type": "sphere"}),
            }
        )
    else:
        from dnd5e_srd_data.schema.common import ConsumptionTargetEntry

        option.consumption = option.consumption.model_copy(
            update={
                "targets": [ConsumptionTargetEntry(type="itemUses", value="1")],
            }
        )

    class Overlay(BundledAssetLoader):
        def get_feature(self, slug):
            return feature if slug == feature.slug else bundled.get_feature(slug)

    set_lib_loader_for_tests(Overlay())
    handle, live = _combat()
    before = deepcopy((live.initiative, live.active_effects, live.rider_uses, live.rng.getstate()))
    _attack(handle, ("staggering-blow",))
    assert events(live, AttackFailed)[-1].reason == "unsupported_rider"
    assert not events(live, AttackRolled)
    assert (live.initiative, live.active_effects, live.rider_uses, live.rng.getstate()) == before


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown-change",
        "missing-change",
        "mode",
        "value",
        "status",
        "disabled",
        "expiry",
        "empty-lifecycle",
        "one-use",
        "competing-expiry",
    ],
)
def test_declaration_carrier_drift_rejects_atomically_before_roll(mutation):
    from dnd5e_srd_data.schema.common import PassiveEffectChange
    from dnd5e_srd_data.schema.lifecycle import EffectLifecycleSpec

    bundled = BundledAssetLoader()
    feature = bundled.get_feature("reckless-attack").model_copy(deep=True)
    effect = feature.passive_effects[0]
    if mutation == "unknown-change":
        effect.changes.append(
            PassiveEffectChange(key="unsupported.new.mechanic", mode=2, value="7")
        )
    elif mutation == "missing-change":
        effect.changes.pop()
    elif mutation in ("mode", "value"):
        effect.changes[0] = effect.changes[0].model_copy(
            update={mutation: 2 if mutation == "mode" else "false"}
        )
    elif mutation in ("status", "disabled"):
        feature.passive_effects[0] = effect.model_copy(
            update={"statuses": ["prone"]} if mutation == "status" else {"disabled": True}
        )
    else:
        binding = feature.attack_rider_context.effects[0]
        updates = {
            "expiry": {"lifecycle": EffectLifecycleSpec(expiry_boundary="target_next_turn_start")},
            "empty-lifecycle": {"lifecycle": EffectLifecycleSpec()},
            "one-use": {
                "lifecycle": binding.lifecycle.model_copy(
                    update={"one_use_modifiers": ("next_save_disadvantage",)}
                )
            },
            "competing-expiry": {"expiry": "source_next_turn_start"},
        }[mutation]
        feature.attack_rider_context = feature.attack_rider_context.model_copy(
            update={"effects": (binding.model_copy(update=updates),)}
        )

    class Overlay(BundledAssetLoader):
        def get_feature(self, slug):
            return feature if slug == feature.slug else bundled.get_feature(slug)

    set_lib_loader_for_tests(Overlay())
    handle, live = _combat()
    before = deepcopy(
        (
            live.initiative,
            live.active_effects,
            live.effect_lifecycles,
            live.rider_uses,
            live.rng.getstate(),
        )
    )
    offset = len(live.event_log)
    _attack(handle)
    assert (
        live.initiative,
        live.active_effects,
        live.effect_lifecycles,
        live.rider_uses,
        live.rng.getstate(),
    ) == before
    [failure] = live.event_log[offset:]
    assert isinstance(failure, AttackFailed)
    assert failure.reason == "unsupported_rider"
