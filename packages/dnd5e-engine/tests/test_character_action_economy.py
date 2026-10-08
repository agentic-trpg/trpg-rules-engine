"""Character turn continuation and payment are independent of action order."""

from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from random import Random

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader

from dnd5e_engine import PlayerIntent
from dnd5e_engine.events import (
    AttackFailed,
    AttackRolled,
    CastFailed,
    CheckRolled,
    IntentSubmitted,
    ReactionTriggered,
    SaveRolled,
    SpellCast,
    TurnEnded,
)
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.live_reactions import register_pending_reaction
from dnd5e_engine.orchestrator import IntentRejectedError
from dnd5e_engine.specs import GridScene
from tests.c20_support import act, combatant, events, monster_turn, pc, start

HERO = "char:hero"
FOE = "mon:foe"


@pytest.fixture(autouse=True)
def _bundled_loader() -> Iterator[None]:
    set_lib_loader_for_tests(BundledAssetLoader())
    yield
    set_lib_loader_for_tests(None)


def _swing(handle, weapon="longsword", **kwargs):
    act(handle, HERO, intent_type="attack", weapon_id=weapon, target_id=FOE, **kwargs)


def _surge(handle):
    act(handle, HERO, intent_type="use_feature", feature_id="action-surge")


def _flurry(handle):
    act(
        handle,
        HERO,
        intent_type="use_feature",
        feature_id="monks-focus",
        activity_id="2ghJTBhilLrFn9xT",
    )


def _magic(handle):
    act(handle, HERO, intent_type="cast_spell", spell_id="fire-bolt", target_id=FOE)


def _spent(live, feature):
    return live.custom_counters_by_entity[HERO][f"feature_use:{feature}"]["spent"]


@pytest.mark.parametrize("level", [2, 3, 4])
def test_one_attack_fighter_can_surge_after_attacking(level):
    handle, live = start([pc(class_slug="fighter", character_level=level)], seed=6)
    _swing(handle)
    assert live.current_actor_id == HERO
    assert not combatant(live).action_available
    _surge(handle)
    _swing(handle)
    assert len(events(live, AttackRolled)) == 2
    assert _spent(live, "action-surge") == 1
    assert combatant(live).extra_actions_remaining == 0
    assert live.current_actor_id == FOE


@pytest.mark.parametrize("magic_first", [False, True])
def test_surge_attack_and_magic_can_be_taken_in_either_order(magic_first):
    handle, live = start([pc(class_slug="fighter", character_level=2)], seed=6)
    _surge(handle)
    first, second = (_magic, _swing) if magic_first else (_swing, _magic)
    first(handle)
    hero = combatant(live)
    assert (hero.action_available, hero.extra_actions_remaining) == (
        (False, 1) if magic_first else (True, 0)
    )
    assert live.current_actor_id == HERO
    second(handle)
    assert len(events(live, AttackRolled)) == 2
    assert [e.spell_id for e in events(live, SpellCast)] == ["fire-bolt"]
    assert events(live, CastFailed) == []
    assert _spent(live, "action-surge") == 1
    assert (combatant(live).action_available, combatant(live).extra_actions_remaining) == (False, 0)
    assert live.current_actor_id == FOE


@pytest.mark.parametrize("level", [1, 2, 3, 4])
def test_one_attack_monk_can_take_bonus_unarmed_after_attack(level):
    handle, live = start([pc(class_slug="monk", character_level=level)], seed=4)
    _swing(handle, "quarterstaff")
    assert live.current_actor_id == HERO
    _swing(handle, "unarmed-strike", use_bonus_action=True)
    assert len(events(live, AttackRolled)) == 2
    assert not combatant(live).bonus_action_available
    # An attempted second Bonus Unarmed Strike cannot consume another budget.
    before = combatant(live)
    _swing(handle, "unarmed-strike", use_bonus_action=True)
    assert combatant(live) == before
    assert [e.reason for e in events(live, AttackFailed)] == ["no_action_economy"]
    act(handle, HERO, intent_type="pass")
    assert live.current_actor_id == FOE


@pytest.mark.parametrize("level", [2, 3, 4])
@pytest.mark.parametrize("flurry_first", [False, True])
def test_attack_and_flurry_can_be_taken_in_either_order(level, flurry_first):
    handle, live = start([pc(class_slug="monk", character_level=level)], seed=4)
    if flurry_first:
        _flurry(handle)
    _swing(handle, "quarterstaff")
    if not flurry_first:
        _flurry(handle)
    assert live.current_actor_id == HERO
    assert combatant(live).flurry_strikes_remaining == 2
    _swing(handle, "unarmed-strike")
    assert combatant(live).flurry_strikes_remaining == 1
    _swing(handle, "unarmed-strike")
    assert len(events(live, AttackRolled)) == 3
    assert _spent(live, "monks-focus") == 1
    hero = combatant(live)
    assert (hero.action_available, hero.bonus_action_available, hero.flurry_strikes_remaining) == (
        False,
        False,
        0,
    )
    act(handle, HERO, intent_type="pass")
    assert live.current_actor_id == FOE


@pytest.mark.parametrize("action", ["dodge", "help", "grapple", "shove"])
def test_paid_flurry_survives_intervening_action(action):
    handle, live = start([pc(class_slug="monk", character_level=2)], seed=4)
    _flurry(handle)
    # Ordinary Grapple/Shove use pending Flurry funding, unlike Dodge/Help.
    act(handle, HERO, intent_type=action, target_id=FOE)
    assert live.current_actor_id == HERO
    expected = 1 if action in ("grapple", "shove") else 2
    assert combatant(live).flurry_strikes_remaining == expected
    for _ in range(expected):
        _swing(handle, "unarmed-strike")
    assert combatant(live).flurry_strikes_remaining == 0
    assert _spent(live, "monks-focus") == 1
    assert not combatant(live).bonus_action_available
    assert combatant(live).action_available == (action in ("grapple", "shove"))


@pytest.mark.parametrize("option", ["grapple", "shove"])
def test_martial_arts_bonus_can_fund_grapple_or_shove(option):
    handle, live = start([pc(class_slug="monk", character_level=1)], seed=4)
    _swing(handle, "quarterstaff")
    act(handle, HERO, intent_type=option, target_id=FOE, use_bonus_action=True)
    hero = combatant(live)
    assert (hero.action_available, hero.bonus_action_available, hero.attacks_remaining) == (
        False,
        False,
        0,
    )
    before = hero
    assert live.current_actor_id == HERO
    act(handle, HERO, intent_type=option, target_id=FOE, use_bonus_action=True)
    assert [e.reason for e in events(live, AttackFailed)] == ["no_action_economy"]
    assert combatant(live) == before
    act(handle, HERO, intent_type="pass")
    assert live.current_actor_id == FOE


@pytest.mark.parametrize("class_slug", ["fighter", "monk"])
def test_pass_abandons_windows_and_turn_start_resets_budgets(class_slug):
    handle, live = start([pc(class_slug=class_slug, character_level=2)], seed=4)
    if class_slug == "fighter":
        _surge(handle)
    else:
        _flurry(handle)
        _swing(handle, "unarmed-strike")
    act(handle, HERO, intent_type="pass")
    assert live.current_actor_id == FOE
    assert len([e for e in events(live, TurnEnded) if e.actor_id == HERO]) == 1
    monster_turn(handle)
    hero = combatant(live)
    assert live.current_actor_id == HERO
    assert (hero.action_available, hero.bonus_action_available, hero.attacks_remaining) == (
        True,
        True,
        1,
    )
    assert (hero.extra_actions_remaining, hero.flurry_strikes_remaining) == (0, 0)
    assert not hero.action_surge_used_this_turn
    assert not hero.attack_action_engaged
    assert not hero.loading_weapon_fired_this_action
    assert _spent(live, "action-surge" if class_slug == "fighter" else "monks-focus") == 1


def test_exhausted_surge_opportunity_does_not_hold_next_turn_open():
    handle, live = start([pc(class_slug="fighter", character_level=2)], seed=6)
    _surge(handle)
    act(handle, HERO, intent_type="pass")
    monster_turn(handle)
    _swing(handle)
    assert live.current_actor_id == FOE
    assert _spent(live, "action-surge") == 1


@pytest.mark.parametrize("level", [2, 5])
def test_loading_new_attack_action_can_fire_after_surge(level):
    handle, live = start([pc(class_slug="fighter", character_level=level)], seed=6)
    _swing(handle, "light-crossbow")
    before = combatant(live)
    _swing(handle, "light-crossbow")
    assert combatant(live) == before
    assert [e.reason for e in events(live, AttackFailed)] == ["weapon_already_fired"]
    _surge(handle)
    _swing(handle, "light-crossbow")
    assert len(events(live, AttackRolled)) == 2
    assert combatant(live).extra_actions_remaining == 0
    assert combatant(live).loading_weapon_fired_this_action
    assert combatant(live).attacks_remaining == (1 if level == 5 else 0)


def test_loading_surge_before_first_shot_preserves_two_attack_actions():
    handle, live = start([pc(class_slug="fighter", character_level=5)], seed=6)
    _surge(handle)
    _swing(handle, "light-crossbow")
    assert (combatant(live).action_available, combatant(live).extra_actions_remaining) == (True, 0)
    _swing(handle, "light-crossbow")
    assert not combatant(live).action_available
    assert len(events(live, AttackRolled)) == 2
    before = combatant(live)
    _swing(handle, "light-crossbow")
    assert combatant(live) == before
    assert [e.reason for e in events(live, AttackFailed)] == ["weapon_already_fired"]


def test_bonus_loading_shot_does_not_close_attack_action_allowance():
    handle, live = start([pc(class_slug="fighter", character_level=5)], seed=6)
    _swing(handle, "dagger")
    _swing(handle, "hand-crossbow", use_bonus_action=True)
    hero = combatant(live)
    assert not hero.bonus_action_available
    assert not hero.loading_weapon_fired_this_action
    assert hero.attacks_remaining == 1
    _swing(handle, "hand-crossbow")
    assert len(events(live, AttackRolled)) == 3
    assert combatant(live).loading_weapon_fired_this_action
    assert combatant(live).attacks_remaining == 0
    assert events(live, AttackFailed) == []


def test_paid_flurry_cannot_spend_focus_or_bonus_action_twice():
    handle, live = start([pc(class_slug="monk", character_level=2)], seed=4)
    _flurry(handle)
    before = combatant(live)
    _flurry(handle)
    assert combatant(live) == before
    assert _spent(live, "monks-focus") == 1
    assert [e.reason for e in events(live, CastFailed)] == ["no_action_economy"]
    _swing(handle, "unarmed-strike")
    assert combatant(live).flurry_strikes_remaining == 1


@pytest.mark.parametrize("class_slug", ["fighter", "monk"])
def test_empty_attack_preserves_an_engaged_sequence_or_paid_flurry(class_slug):
    handle, live = start([pc(class_slug=class_slug, character_level=5)], seed=4)
    _swing(handle, "quarterstaff")
    if class_slug == "fighter":
        _surge(handle)
    else:
        _flurry(handle)
    before = combatant(live)
    rng = live.rng.getstate()
    act(handle, HERO, intent_type="attack", target_id=FOE, use_bonus_action=True)
    assert combatant(live) == before
    assert live.rng.getstate() == rng
    assert [e.reason for e in events(live, AttackFailed)] == ["action_unavailable"]
    assert live.current_actor_id == HERO


@pytest.mark.parametrize("option", ["attack", "grapple", "shove"])
def test_last_flurry_strike_auto_ends_when_no_movement_or_other_window_remains(option):
    handle, live = start([pc(class_slug="monk", character_level=2, base_speed=0)], seed=4)
    assert combatant(live).movement_remaining == 0
    _swing(handle, "quarterstaff")
    _flurry(handle)
    _swing(handle, "unarmed-strike")
    assert live.current_actor_id == HERO
    kwargs = {"weapon_id": "unarmed-strike"} if option == "attack" else {}
    act(handle, HERO, intent_type=option, target_id=FOE, **kwargs)
    assert combatant(live).flurry_strikes_remaining == 0
    assert live.current_actor_id == FOE


@pytest.mark.parametrize("class_slug", ["fighter", "rogue"])
def test_each_additional_hide_requires_a_separate_payment(class_slug):
    handle, live = start(
        [pc(class_slug=class_slug, character_level=2)],
        seed=9,
        grid_scene=GridScene(width=10, height=10, cover_cells={"0,0": "three_quarters"}),
    )
    if class_slug == "fighter":
        _surge(handle)
    act(handle, HERO, intent_type="hide")
    assert len(events(live, CheckRolled)) == 1
    assert live.current_actor_id == HERO
    act(handle, HERO, intent_type="hide", use_bonus_action=class_slug == "rogue")
    assert len(events(live, CheckRolled)) == 2
    assert not combatant(live).action_available
    assert combatant(live).extra_actions_remaining == 0
    if class_slug == "rogue":
        assert not combatant(live).bonus_action_available
        before = combatant(live)
        rng = live.rng.getstate()
        with pytest.raises(IntentRejectedError, match="no_action_economy"):
            act(handle, HERO, intent_type="hide", use_bonus_action=True)
        assert combatant(live) == before
        assert live.rng.getstate() == rng
        act(handle, HERO, intent_type="pass")
    assert live.current_actor_id == FOE


@pytest.mark.parametrize("after_surge", [False, True])
@pytest.mark.parametrize(
    "intent",
    [
        {"intent_type": "attack", "target_id": FOE},
        {"intent_type": "attack", "weapon_id": "missing-weapon", "target_id": FOE},
        {"intent_type": "attack", "weapon_id": "longsword", "target_id": "missing"},
        {"intent_type": "cast_spell", "spell_id": "magic-missile", "target_id": FOE},
        {"intent_type": "cast_spell", "spell_id": "fire-bolt", "target_id": FOE, "slot_level": 1},
        {"intent_type": "cast_spell", "spell_id": "detect-magic", "as_ritual": True},
        {"intent_type": "cast_spell", "spell_id": "fire-bolt", "target_id": "missing"},
        {"intent_type": "cast_spell", "spell_id": "magic-missile", "target_ids": [FOE] * 4},
    ],
)
def test_refused_intents_preserve_all_character_budgets_and_rng(intent, after_surge):
    handle, live = start([pc(class_slug="fighter", character_level=2)], seed=6)
    if after_surge:
        _surge(handle)
    before = combatant(live)
    rng = live.rng.getstate()
    counters = deepcopy(live.custom_counters_by_entity)
    act(handle, HERO, **intent)
    assert combatant(live) == before
    assert live.rng.getstate() == rng
    assert live.custom_counters_by_entity == counters
    assert live.current_actor_id == HERO
    assert events(live, AttackFailed) or events(live, CastFailed)
    if intent["intent_type"] == "attack":
        assert not [e for e in events(live, IntentSubmitted) if e.intent_type == "attack"]
    _swing(handle)
    assert len(events(live, AttackRolled)) == 1


def _counterspell_combat(**caster_kwargs):
    # A real ability carrier is required when Healing Word evaluates @mod.
    caster_kwargs.setdefault("class_slug", "wizard")
    handle, live = start(
        [
            pc(**caster_kwargs),
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
    register_pending_reaction(
        live,
        "char:counter",
        PlayerIntent(
            intent_type="ready",
            spell_id="counterspell",
            slot_level=3,
            reaction_trigger="cast_spell",
        ),
    )
    return handle, live


@pytest.mark.parametrize("pool", ["spell_slots", "pact_slots"])
@pytest.mark.parametrize(
    ("spell", "target", "budget"),
    [
        ("magic-missile", FOE, "action_available"),
        ("fire-bolt", FOE, "action_available"),
        ("healing-word", HERO, "bonus_action_available"),
        ("shield", HERO, "reaction_available"),
    ],
)
def test_countered_cast_spends_casting_time_but_preserves_slot(spell, target, budget, pool):
    handle, live = _counterspell_combat(**{pool: {1: 1}})
    before = combatant(live)
    slots = deepcopy(
        (live.spell_slots_by_entity.get(HERO, {}), live.pact_slots_by_entity.get(HERO, {}))
    )
    expected_rng = Random()
    expected_rng.setstate(live.rng.getstate())
    natural = expected_rng.randint(1, 20)

    act(handle, HERO, intent_type="cast_spell", spell_id=spell, target_id=target)

    assert [e.reason for e in events(live, CastFailed)] == ["countered"]
    assert combatant(live) == before.model_copy(update={budget: False})
    assert (
        live.spell_slots_by_entity.get(HERO, {}),
        live.pact_slots_by_entity.get(HERO, {}),
    ) == slots
    assert live.spell_slots_by_entity["char:counter"][3] == 0
    assert not combatant(live, "char:counter").reaction_available
    assert not live.pending_reactions
    assert [e.spell_id for e in events(live, SpellCast)] == ["counterspell"]
    saves = events(live, SaveRolled)
    assert len(saves) == 1
    assert (saves[0].target_id, saves[0].roll_total, saves[0].succeeded) == (HERO, natural, False)
    assert live.rng.getstate() == expected_rng.getstate()
    assert [e.actor_id for e in events(live, ReactionTriggered)] == (
        [HERO, "char:counter"] if budget == "reaction_available" else ["char:counter"]
    )
    assert live.current_actor_id == (FOE if budget == "action_available" else HERO)
    if budget != "action_available":
        after_counter = combatant(live)
        act(handle, HERO, intent_type="cast_spell", spell_id=spell, target_id=target)
        assert events(live, CastFailed)[-1].reason == "no_action_economy"
        assert combatant(live) == after_counter
        assert live.rng.getstate() == expected_rng.getstate()
        assert len(events(live, ReactionTriggered)) == (2 if budget == "reaction_available" else 1)
        # The countered Bonus Action/Reaction leaves the base Action usable.
        _swing(handle)
        assert len(events(live, AttackRolled)) == 1
        assert live.current_actor_id == FOE


@pytest.mark.parametrize("order", ["surge-cast-attack", "surge-attack-cast", "cast-surge-attack"])
def test_countered_magic_spends_only_base_action_in_surge_orders(order):
    handle, live = _counterspell_combat(class_slug="fighter", character_level=2, spell_slots={1: 1})
    if order.startswith("surge"):
        _surge(handle)
    if order == "surge-attack-cast":
        _swing(handle)

    act(handle, HERO, intent_type="cast_spell", spell_id="magic-missile", target_id=FOE)

    assert [e.reason for e in events(live, CastFailed)] == ["countered"]
    assert live.spell_slots_by_entity[HERO][1] == 1
    assert not combatant(live).action_available
    assert combatant(live).bonus_action_available
    assert combatant(live).reaction_available
    assert combatant(live).extra_actions_remaining == (1 if order == "surge-cast-attack" else 0)
    if order == "surge-attack-cast":
        assert live.current_actor_id == FOE
    else:
        assert live.current_actor_id == HERO
        if order == "cast-surge-attack":
            _surge(handle)
        before = combatant(live)
        rng = live.rng.getstate()
        _magic(handle)
        assert events(live, CastFailed)[-1].reason == "no_action_economy"
        assert combatant(live) == before
        assert live.rng.getstate() == rng
        _swing(handle)
        assert live.current_actor_id == FOE
    assert len(events(live, AttackRolled)) == 1
    assert _spent(live, "action-surge") == 1
    assert combatant(live).extra_actions_remaining == 0


@pytest.mark.parametrize("after_surge", [False, True])
@pytest.mark.parametrize(
    ("intent", "reason"),
    [
        ({"spell_id": "magic-missile", "target_id": FOE, "slot_level": 2}, "no_slot"),
        ({"spell_id": "shield-of-faith", "target_id": HERO, "slot_level": 2}, "no_slot"),
        ({"spell_id": "shield", "slot_level": 2}, "no_slot"),
        ({"spell_id": "fire-bolt", "target_id": FOE, "slot_level": 1}, "no_slot"),
        ({"spell_id": "detect-magic", "as_ritual": True}, "ritual_in_combat"),
        ({"spell_id": "fire-bolt", "target_id": "missing"}, "target_invalid"),
        ({"spell_id": "magic-missile", "target_ids": [FOE] * 4}, "target_invalid"),
    ],
)
def test_refused_cast_does_not_trigger_armed_counterspell(intent, reason, after_surge):
    handle, live = _counterspell_combat(class_slug="fighter", character_level=2, spell_slots={1: 1})
    if after_surge:
        _surge(handle)
    before = deepcopy(
        (
            live.initiative,
            live.spell_slots_by_entity,
            live.pact_slots_by_entity,
            live.custom_counters_by_entity,
            live.pending_reactions,
        )
    )
    rng = live.rng.getstate()

    act(handle, HERO, intent_type="cast_spell", **intent)

    assert [e.reason for e in events(live, CastFailed)] == [reason]
    assert (
        live.initiative,
        live.spell_slots_by_entity,
        live.pact_slots_by_entity,
        live.custom_counters_by_entity,
        live.pending_reactions,
    ) == before
    assert live.rng.getstate() == rng
    assert live.current_actor_id == HERO
    assert not events(live, ReactionTriggered)
    assert not events(live, SaveRolled)
    assert not events(live, SpellCast)


def test_attack_then_surge_cannot_pay_magic_or_trigger_counterspell_with_extra_action():
    handle, live = _counterspell_combat(class_slug="fighter", character_level=2, spell_slots={1: 1})
    _swing(handle)
    _surge(handle)
    before = deepcopy((live.initiative, live.spell_slots_by_entity, live.pending_reactions))
    rng = live.rng.getstate()
    act(handle, HERO, intent_type="cast_spell", spell_id="magic-missile", target_id=FOE)
    assert [e.reason for e in events(live, CastFailed)] == ["no_action_economy"]
    assert (live.initiative, live.spell_slots_by_entity, live.pending_reactions) == before
    assert live.rng.getstate() == rng
    assert not events(live, ReactionTriggered)
    _swing(handle)
    assert len(events(live, AttackRolled)) == 2
    assert live.current_actor_id == FOE


def test_countered_cast_sequence_is_deterministic():
    def run():
        handle, live = _counterspell_combat(
            class_slug="fighter", character_level=2, spell_slots={1: 1}
        )
        _surge(handle)
        act(handle, HERO, intent_type="cast_spell", spell_id="magic-missile", target_id=FOE)
        _swing(handle)
        return (
            [e.model_dump(exclude={"uuid", "handle_id"}) for e in live.event_log],
            live.initiative,
            live.spell_slots_by_entity,
            live.custom_counters_by_entity,
            live.rng.getstate(),
        )

    assert run() == run()


def test_rogue_attack_then_cunning_hide_spends_bonus_only():
    handle, live = start(
        [pc(class_slug="rogue", character_level=2)],
        seed=9,
        grid_scene=GridScene(width=10, height=10, cover_cells={"0,0": "three_quarters"}),
    )
    _swing(handle)
    assert live.current_actor_id == HERO
    act(handle, HERO, intent_type="hide", use_bonus_action=True)
    assert len(events(live, CheckRolled)) == 1
    assert not combatant(live).action_available
    assert not combatant(live).bonus_action_available
    assert live.current_actor_id == HERO
    act(handle, HERO, intent_type="pass")
    assert live.current_actor_id == FOE


@pytest.mark.parametrize("dexterity", [-10, 40])
def test_plain_hide_costs_action_on_success_or_failure(dexterity):
    handle, live = start(
        [pc(dexterity=dexterity)],
        seed=9,
        grid_scene=GridScene(width=10, height=10, cover_cells={"0,0": "three_quarters"}),
    )
    act(handle, HERO, intent_type="hide")
    assert events(live, CheckRolled)[0].succeeded == (dexterity == 40)
    assert not combatant(live).action_available
    assert combatant(live).bonus_action_available
    assert live.current_actor_id == FOE


@pytest.mark.parametrize("bonus", [False, True])
def test_invalid_hide_preserves_budgets_state_and_rng(bonus):
    handle, live = start(
        [pc()],
        seed=9,
        grid_scene=GridScene(
            width=10, height=10, cover_cells={"0,0": "three_quarters"} if bonus else {}
        ),
    )
    before = combatant(live)
    rng = live.rng.getstate()
    with pytest.raises(IntentRejectedError) as rejected:
        act(handle, HERO, intent_type="hide", use_bonus_action=bonus)
    assert rejected.value.reason == ("no_action_economy" if bonus else "target_invalid")
    assert combatant(live) == before
    assert live.rng.getstate() == rng
    assert not events(live, CheckRolled)
    assert not live.hidden_entities
    assert live.current_actor_id == HERO


def test_same_seed_and_sequence_produce_same_events_and_resources():
    def run():
        handle, live = start([pc(class_slug="monk", character_level=2, dexterity=16)], seed=4)
        _swing(handle, "quarterstaff")
        _flurry(handle)
        _swing(handle, "unarmed-strike")
        act(handle, HERO, intent_type="shove", target_id=FOE)
        act(handle, HERO, intent_type="pass")
        assert live.current_actor_id == FOE
        # UUIDs/handles are transport identities, independent of the seeded rules stream.
        return (
            [e.model_dump(exclude={"uuid", "handle_id"}) for e in live.event_log],
            combatant(live),
            live.custom_counters_by_entity,
        )

    assert run() == run()
