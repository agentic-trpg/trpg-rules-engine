"""Public bound attacks execute immediate movement with an independent allowance."""

from __future__ import annotations

from copy import deepcopy
from random import Random

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.monster import CreatureSize
from pydantic import ValidationError

from dnd5e_engine import PlayerIntent
from dnd5e_engine import live_movement as movement
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.attack_riders import AttackRiderRequest
from dnd5e_engine.events import (
    ActorMoved,
    AttackFailed,
    AttackRiderTriggered,
    AttackRolled,
    CombatantMoved,
    DamageApplied,
    EffectApplied,
    IntentSubmitted,
    ReactionTriggered,
    SaveRolled,
)
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.movement import MovementChoice, MovementLedger
from dnd5e_engine.persistent_areas import register_area
from dnd5e_engine.specs import GridScene
from dnd5e_engine.types.effects import ActiveEffect, ActiveEffectChange
from tests.c20_support import act, combatant, events, foe, pc, start

HERO, FOE = "char:hero", "mon:foe"
BRUTAL = ("brutal-strike", "nN5gsB6AcSQ4uQPN")
WITHDRAW = ("cunning-strike", "m2bRZ1YeD3yf9nV7")


@pytest.fixture(autouse=True)
def _fresh_loader():
    set_lib_loader_for_tests(BundledAssetLoader())
    yield
    set_lib_loader_for_tests(None)


def _barbarian(**fields):
    return pc(
        **(
            {
                "class_slug": "barbarian",
                "character_level": 9,
                "strength": 18,
                "dexterity": 10,
                "equipment": ("longsword",),
                "attack_bonus": 99,
                "hp_current": 200,
                "hp_max": 200,
            }
            | fields
        )
    )


def _rogue(**fields):
    return pc(
        **(
            {
                "class_slug": "rogue",
                "character_level": 5,
                "dexterity": 18,
                "equipment": ("dagger",),
                "attack_bonus": 99,
                "hp_current": 200,
                "hp_max": 200,
            }
            | fields
        )
    )


def _ally():
    return pc("char:ally", initiative=10, zone_id="1,1")


def _request(identity, **fields):
    return AttackRiderRequest(feature_id=identity[0], activity_id=identity[1], **fields)


def _forceful(choice=None, *, options=("forceful-blow",)):
    return _request(BRUTAL, option_ids=options, movement_choice=choice)


def _withdraw(choice=None):
    return _request(WITHDRAW, movement_choice=choice)


def _attack(handle, request, *, rogue=False, target=FOE, **fields):
    act(
        handle,
        HERO,
        intent_type="attack",
        weapon_id="dagger" if rogue else "longsword",
        target_id=target,
        attack_riders=(request,),
        **({} if rogue else {"reckless_attack": True}),
        **fields,
    )


def _start_barbarian(*, party=None, encounter=None, grid=None, effects=(), **fields):
    return start(
        party or [_barbarian(**fields)],
        seed=7,
        encounter=encounter,
        grid_scene=grid,
        active_effects=effects,
    )


def _start_rogue(*, grid=None, encounter=None, seed=7, **fields):
    return start([_rogue(**fields), _ally()], seed=seed, encounter=encounter, grid_scene=grid)


def _snapshot(live):
    return (
        [actor.model_dump(mode="json") for actor in live.initiative],
        deepcopy(live.actor_zone),
        deepcopy(live.active_effects),
        deepcopy(live.effect_lifecycles),
        deepcopy(live.rider_uses),
        deepcopy(live.custom_counters_by_entity),
        deepcopy(live.movement_ledgers),
        live.rng.getstate(),
    )


def _assert_rejected(live, before, offset):
    assert _snapshot(live) == before
    [failure] = live.event_log[offset:]
    assert isinstance(failure, AttackFailed)
    assert failure.reason == "unsupported_rider"
    assert not any(isinstance(event, IntentSubmitted) for event in live.event_log[offset:])


@pytest.mark.parametrize(
    "choice", [MovementChoice(distance_ft=10), MovementChoice(destination_cell="2,0")]
)
def test_forceful_pushes_fifteen_then_follows_actual_target_without_ordinary_payment(choice):
    handle, live = _start_barbarian()
    ordinary = live.movement_ledgers[HERO], combatant(live).movement_remaining
    _attack(handle, _forceful(choice))
    [attack] = events(live, AttackRolled)
    assert attack.is_hit
    assert attack.advantage == "normal"
    assert live.actor_zone[FOE] == "4,0"
    assert live.actor_zone[HERO] == "2,0"
    [pushed] = [event for event in events(live, CombatantMoved) if event.actor_id == FOE]
    assert (pushed.from_zone, pushed.to_zone, pushed.forced) == ("1,0", "4,0", True)
    steps = events(live, ActorMoved)
    assert [event.to_zone for event in steps] == ["1,0", "2,0"]
    damage_index = max(live.event_log.index(event) for event in events(live, DamageApplied))
    assert live.event_log.index(attack) < damage_index < live.event_log.index(pushed)
    assert live.event_log.index(pushed) < live.event_log.index(steps[0])
    [rider] = events(live, AttackRiderTriggered)
    assert rider.option_ids == ("forceful-blow",)
    assert rider.source_activity_id
    assert (live.movement_ledgers[HERO], combatant(live).movement_remaining) == ordinary
    assert not combatant(live).disengaging_this_turn


def test_forceful_partial_push_follows_actual_free_space_and_never_enters_target_cell():
    handle, live = _start_barbarian(grid=GridScene(width=10, height=1, blocked_cells=["3,0"]))
    _attack(handle, _forceful(MovementChoice(distance_ft=15)))
    assert live.actor_zone[FOE] == "2,0"
    assert live.actor_zone[HERO] == "1,0"
    assert len(events(live, ActorMoved)) == 1
    assert events(live, DamageApplied)
    assert events(live, AttackRiderTriggered)
    assert not events(live, AttackFailed)


def test_forceful_diagonal_follow_uses_complete_straight_grid_steps():
    handle, live = _start_barbarian(encounter=[foe(zone_id="1,1")])
    _attack(handle, _forceful(MovementChoice(destination_cell="2,2")))
    assert live.actor_zone[FOE] == "4,4"
    assert live.actor_zone[HERO] == "2,2"
    assert [event.to_zone for event in events(live, ActorMoved)] == ["1,1", "2,2"]


def test_forceful_follow_stops_when_real_push_is_blocked_by_another_creature():
    blocker = "mon:blocker"
    handle, live = _start_barbarian(
        encounter=[foe(), foe(entity_id=blocker, zone_id="3,0", initiative=0)]
    )
    _attack(handle, _forceful(MovementChoice(distance_ft=15)))
    assert live.actor_zone[FOE] == "2,0"
    assert live.actor_zone[HERO] == "1,0"
    assert live.actor_zone[blocker] == "3,0"
    assert not events(live, AttackFailed)


def test_forceful_completely_blocked_push_keeps_hit_and_leaves_follow_at_zero():
    handle, live = _start_barbarian(grid=GridScene(width=10, height=1, blocked_cells=["2,0"]))
    _attack(handle, _forceful(MovementChoice(distance_ft=15)))
    assert live.actor_zone[FOE] == "1,0"
    assert live.actor_zone[HERO] == "0,0"
    assert not events(live, ActorMoved)
    assert not events(live, CombatantMoved)
    assert len(events(live, DamageApplied)) == 1
    assert len(events(live, AttackRiderTriggered)) == 1
    assert not events(live, AttackFailed)


@pytest.mark.parametrize("choice", [None, MovementChoice(), MovementChoice(distance_ft=0)])
def test_forceful_zero_optional_follow_still_deals_damage_and_pushes(choice):
    handle, live = _start_barbarian()
    _attack(handle, _forceful(choice))
    assert live.actor_zone[FOE] == "4,0"
    assert live.actor_zone[HERO] == "0,0"
    assert not events(live, ActorMoved)
    assert events(live, DamageApplied)
    assert len(events(live, AttackRiderTriggered)) == 1


def test_two_options_resolve_in_declared_order_and_roll_shared_brutal_damage_once():
    handle, live = _start_barbarian(character_level=17)
    expected = Random()
    expected.setstate(live.rng.getstate())
    natural = expected.randint(1, 20)
    weapon = expected.randint(1, 8)
    brutal = sum(expected.randint(1, 10) for _ in range(2))
    _attack(
        handle,
        _forceful(MovementChoice(distance_ft=5), options=("forceful-blow", "hamstring-blow")),
    )
    [attack] = events(live, AttackRolled)
    assert (attack.natural, attack.is_hit) == (natural, True)
    [damage] = events(live, DamageApplied)
    assert damage.amount == weapon + 4 + brutal
    hamstring = next(
        event
        for event in events(live, EffectApplied)
        if any(change.key == "speed.reduction" for change in event.effect.changes)
    )
    [pushed] = events(live, CombatantMoved)
    [followed] = events(live, ActorMoved)
    assert live.event_log.index(damage) < live.event_log.index(pushed)
    assert live.event_log.index(pushed) < live.event_log.index(followed)
    assert live.event_log.index(followed) < live.event_log.index(hamstring)
    [rider] = events(live, AttackRiderTriggered)
    assert rider.option_ids == ("forceful-blow", "hamstring-blow")
    assert live.rng.getstate() == expected.getstate()


def test_forceful_half_speed_allowance_ignores_dash_and_retains_exhausted_ordinary_ledger():
    slow = ActiveEffect(
        id="seed:slower",
        name="Reduced Speed",
        origin="test:speed",
        target_id=HERO,
        changes=[ActiveEffectChange(key="speed.reduction", mode="add", value=10)],
    )
    handle, live = _start_barbarian(effects=[slow])
    assert movement.effective_speed(combatant(live), "walk", live) == 20
    live.movement_ledgers[HERO] = MovementLedger(spent_ft=60, distance_ft=60, dash_count=2)
    movement.project_remaining(live, HERO)
    ordinary = live.movement_ledgers[HERO], combatant(live).movement_remaining
    _attack(handle, _forceful(MovementChoice(distance_ft=10)))
    assert live.actor_zone[HERO] == "2,0"
    assert sum(event.movement_cost_ft for event in events(live, ActorMoved)) == 10
    assert (live.movement_ledgers[HERO], combatant(live).movement_remaining) == ordinary


def test_forceful_terrain_charges_grant_and_stops_at_complete_affordable_step():
    handle, live = _start_barbarian(
        grid=GridScene(width=10, height=1, difficult_terrain_cells=["1,0", "2,0"])
    )
    _attack(handle, _forceful(MovementChoice(distance_ft=15)))
    assert live.actor_zone[FOE] == "4,0"
    assert live.actor_zone[HERO] == "1,0"
    assert [event.movement_cost_ft for event in events(live, ActorMoved)] == [10]


def test_forceful_follow_uses_existing_grapple_drag_and_charges_extra_cost():
    victim = "mon:victim"
    grapple = ActiveEffect(
        id="seed:grapple",
        name="Grappled",
        origin=f"grapple:unarmed-strike:{HERO}",
        target_id=victim,
        statuses={"grappled"},
    )
    handle, live = _start_barbarian(
        creature_size=CreatureSize.MEDIUM,
        encounter=[
            foe(),
            foe(entity_id=victim, zone_id="0,1", initiative=0, creature_size=CreatureSize.MEDIUM),
        ],
        effects=[grapple],
    )
    ordinary = deepcopy(live.movement_ledgers)
    _attack(handle, _forceful(MovementChoice(distance_ft=15)))
    assert live.actor_zone[HERO] == "1,0"
    assert live.actor_zone[victim] == "0,0"
    assert [event.movement_cost_ft for event in events(live, ActorMoved)] == [10]
    dragged = [event for event in events(live, CombatantMoved) if event.actor_id == victim]
    assert len(dragged) == 1
    assert all(event.forced and event.dragged_by == HERO for event in dragged)
    assert live.movement_ledgers == ordinary
    assert not any(event.is_opportunity_attack for event in events(live, AttackRolled))


def test_forceful_scoped_oa_immunity_does_not_protect_later_ordinary_movement():
    threat = "mon:threat"
    handle, live = _start_barbarian(
        encounter=[foe(), foe(entity_id=threat, zone_id="0,1", monster_template_slug="zombie")]
    )
    _attack(handle, _forceful(MovementChoice(distance_ft=10)))
    assert live.actor_zone[HERO] == "2,0"
    assert len(events(live, AttackRolled)) == 1
    assert combatant(live, threat).reaction_available
    assert not combatant(live).disengaging_this_turn
    act(handle, HERO, intent_type="move", target_zone_id="1,0")
    act(handle, HERO, intent_type="move", target_zone_id="2,0")
    opportunity = [event for event in events(live, AttackRolled) if event.is_opportunity_attack]
    assert len(opportunity) == 1
    assert opportunity[0].attacker_id == threat
    assert not combatant(live, threat).reaction_available


def _caltrops(live, *, origin="1,0", massive_damage=False):
    item = BundledAssetLoader().get_item("caltrops")
    assert item is not None
    activity = next(
        activity for activity in item.activities if activity.persistent_area is not None
    )
    activity = activity.model_copy(deep=True)
    if massive_damage:
        part = activity.damage.parts[0]
        part = part.model_copy(
            update={"custom": part.custom.model_copy(update={"formula": "1000"})}
        )
        activity = activity.model_copy(
            update={"damage": activity.damage.model_copy(update={"parts": [part]})}
        )
    ctx = build_activity_context(
        combatant(live),
        [],
        rng=live.rng,
        event_emitter=lambda event: orch._emit(live, event),
        spellcasting_ability=None,
        source_passive_effects=item.passive_effects,
        slot_level=None,
        base_spell_level=None,
        concentration=False,
        spell_book={},
        passive_damage_modifiers={},
        save_modifiers={},
    )
    register_area(live, activity, ctx, source_id=item.slug, origin=origin)


@pytest.mark.parametrize("massive_damage", [False, True])
def test_forceful_area_entry_interrupts_follow_after_completed_step_without_undoing_hit(
    massive_damage,
):
    handle, live = _start_barbarian(dexterity=-30)
    _caltrops(live, massive_damage=massive_damage)
    ordinary = deepcopy(live.movement_ledgers)
    _attack(handle, _forceful(MovementChoice(distance_ft=15)))
    assert live.actor_zone[FOE] == "4,0"
    assert live.actor_zone[HERO] == "1,0"
    assert len(events(live, ActorMoved)) == 1
    assert any(
        event.target_id == HERO and not event.succeeded for event in events(live, SaveRolled)
    )
    assert any(event.target_id == FOE for event in events(live, DamageApplied))
    assert any(event.target_id == HERO for event in events(live, DamageApplied))
    assert len(events(live, AttackRiderTriggered)) == 1
    assert not events(live, AttackFailed)
    assert live.movement_ledgers == ordinary
    if massive_damage:
        assert combatant(live).hp_current == 0
    else:
        assert movement.effective_speed(combatant(live), "walk", live) == 0


def test_forceful_external_relocation_stops_remaining_follow_without_undoing_attack(monkeypatch):
    handle, live = _start_barbarian()
    previous = movement.post_position_steps
    relocated = False

    def relocate_after_entry(combat, positions):
        nonlocal relocated
        previous(combat, positions)
        if not relocated and combat.actor_zone[HERO] == "1,0":
            relocated = True
            orch.push_combatant(combat, HERO, origin_cell="0,0", distance_ft=5)

    monkeypatch.setattr(movement, "post_position_steps", relocate_after_entry)
    ordinary = deepcopy(live.movement_ledgers)
    _attack(handle, _forceful(MovementChoice(distance_ft=15)))
    assert live.actor_zone[FOE] == "4,0"
    assert live.actor_zone[HERO] == "2,0"
    assert len(events(live, ActorMoved)) == 1
    assert len(events(live, DamageApplied)) == 1
    assert len(events(live, AttackRiderTriggered)) == 1
    assert not events(live, AttackFailed)
    assert live.movement_ledgers == ordinary


@pytest.mark.parametrize("seed", [7, 5])
def test_withdraw_sacrifices_one_sneak_die_then_moves_after_damage_using_own_allowance(seed):
    handle, live = _start_rogue(seed=seed)
    live.movement_ledgers[HERO] = MovementLedger(spent_ft=30, distance_ft=30)
    movement.project_remaining(live, HERO)
    ordinary = live.movement_ledgers[HERO], combatant(live).movement_remaining
    expected = Random()
    expected.setstate(live.rng.getstate())
    natural = expected.randint(1, 20)
    critical = natural == 20
    assert critical is (seed == 5)
    weapon = sum(expected.randint(1, 4) for _ in range(2 if critical else 1))
    sneak = sum(expected.randint(1, 6) for _ in range(4 if critical else 2))
    _attack(handle, _withdraw(MovementChoice(destination_cell="0,2")), rogue=True)
    [attack] = events(live, AttackRolled)
    assert (attack.natural, attack.is_hit) == (natural, True)
    [damage] = events(live, DamageApplied)
    assert (damage.amount, damage.damage_type, damage.is_crit) == (
        weapon + 4 + sneak,
        "piercing",
        critical,
    )
    withdraw = next(
        event for event in events(live, AttackRiderTriggered) if event.feature_id == WITHDRAW[0]
    )
    assert withdraw.sacrificed_sneak_dice == 1
    assert withdraw.source_activity_id
    assert combatant(live).sneak_attack_spent_this_turn
    steps = events(live, ActorMoved)
    assert live.event_log.index(damage) < live.event_log.index(steps[0])
    assert live.actor_zone[HERO] == "0,2"
    assert (live.movement_ledgers[HERO], combatant(live).movement_remaining) == ordinary
    assert not combatant(live).disengaging_this_turn
    assert live.rng.getstate() == expected.getstate()


def test_withdraw_terrain_cost_is_paid_by_grant_and_later_ordinary_move_still_provokes():
    handle, live = _start_rogue(
        grid=GridScene(width=10, height=10, difficult_terrain_cells=["0,1", "1,1"]),
        encounter=[foe(monster_template_slug="zombie")],
    )
    ordinary = live.movement_ledgers[HERO], combatant(live).movement_remaining
    _attack(handle, _withdraw(MovementChoice(destination_cell="0,2")), rogue=True)
    assert live.actor_zone[HERO] == "0,2"
    assert [event.movement_cost_ft for event in events(live, ActorMoved)] == [10, 5]
    assert (live.movement_ledgers[HERO], combatant(live).movement_remaining) == ordinary
    assert len(events(live, AttackRolled)) == 1
    assert combatant(live, FOE).reaction_available
    assert not combatant(live).disengaging_this_turn
    act(handle, HERO, intent_type="move", target_zone_id="0,1")
    act(handle, HERO, intent_type="move", target_zone_id="0,2")
    opportunity = [event for event in events(live, AttackRolled) if event.is_opportunity_attack]
    assert len(opportunity) == 1
    assert opportunity[0].attacker_id == FOE


def test_withdraw_drags_victim_and_preserves_ordinary_movement_ledger():
    victim = "mon:victim"
    grapple = ActiveEffect(
        id="seed:grapple",
        name="Grappled",
        origin=f"grapple:unarmed-strike:{HERO}",
        target_id=victim,
        statuses={"grappled"},
    )
    handle, live = start(
        [
            _rogue(zone_id="1,0", creature_size=CreatureSize.MEDIUM),
            pc("char:ally", initiative=10, zone_id="2,1"),
        ],
        seed=7,
        encounter=[
            foe(zone_id="2,0"),
            foe(entity_id=victim, zone_id="0,0", initiative=0, creature_size=CreatureSize.MEDIUM),
        ],
        active_effects=[grapple],
    )
    ordinary = deepcopy(live.movement_ledgers)
    _attack(handle, _withdraw(MovementChoice(destination_cell="1,1")), rogue=True)
    assert live.actor_zone[HERO] == "1,1"
    assert live.actor_zone[victim] == "1,0"
    [step] = events(live, ActorMoved)
    assert step.movement_cost_ft == 10
    [dragged] = events(live, CombatantMoved)
    assert dragged.actor_id == victim
    assert dragged.dragged_by == HERO
    assert dragged.forced
    assert not events(live, AttackFailed)
    assert not any(event.is_opportunity_attack for event in events(live, AttackRolled))
    assert live.movement_ledgers == ordinary


def test_withdraw_real_persistent_area_entry_stops_movement_and_retains_hit_and_sacrifice():
    handle, live = _start_rogue()
    orch._emit(
        live,
        EffectApplied(
            effect=ActiveEffect(
                id="seed:save-penalty",
                name="Saving throw penalty",
                origin="test:save-penalty",
                target_id=HERO,
                changes=[ActiveEffectChange(key="save.bonus", mode="add", value=-100)],
            )
        ),
    )
    _caltrops(live, origin="0,1")
    ordinary = deepcopy(live.movement_ledgers)
    _attack(handle, _withdraw(MovementChoice(destination_cell="0,2")), rogue=True)
    assert live.actor_zone[HERO] == "0,1"
    assert len(events(live, ActorMoved)) == 1
    assert movement.effective_speed(combatant(live), "walk", live) == 0
    assert any(
        event.target_id == HERO and not event.succeeded for event in events(live, SaveRolled)
    )
    assert any(event.target_id == FOE and event.amount > 0 for event in events(live, DamageApplied))
    assert any(event.target_id == HERO for event in events(live, DamageApplied))
    withdraw = next(
        event for event in events(live, AttackRiderTriggered) if event.feature_id == WITHDRAW[0]
    )
    assert withdraw.sacrificed_sneak_dice == 1
    assert combatant(live).sneak_attack_spent_this_turn
    assert not events(live, AttackFailed)
    assert live.movement_ledgers == ordinary


@pytest.mark.parametrize(
    "choice",
    [None, MovementChoice(), MovementChoice(distance_ft=0), MovementChoice(destination_cell="0,0")],
)
def test_withdraw_zero_movement_still_pays_one_sneak_die(choice):
    handle, live = _start_rogue()
    expected = Random()
    expected.setstate(live.rng.getstate())
    expected.randint(1, 20)
    weapon = expected.randint(1, 4)
    sneak = sum(expected.randint(1, 6) for _ in range(2))
    _attack(handle, _withdraw(choice), rogue=True)
    withdraw = next(
        event for event in events(live, AttackRiderTriggered) if event.feature_id == WITHDRAW[0]
    )
    assert withdraw.sacrificed_sneak_dice == 1
    assert combatant(live).sneak_attack_spent_this_turn
    [damage] = events(live, DamageApplied)
    assert damage.amount == weapon + 4 + sneak
    assert live.rng.getstate() == expected.getstate()
    assert live.actor_zone[HERO] == "0,0"
    assert not events(live, ActorMoved)


def test_withdraw_miss_consumes_no_sneak_dice_and_does_not_move():
    handle, live = _start_rogue(encounter=[foe(ac=999)])
    expected = Random()
    expected.setstate(live.rng.getstate())
    expected.randint(1, 20)
    _attack(handle, _withdraw(MovementChoice(destination_cell="0,2")), rogue=True)
    [attack] = events(live, AttackRolled)
    assert not attack.is_hit
    assert not events(live, DamageApplied)
    assert not events(live, AttackRiderTriggered)
    assert not events(live, ActorMoved)
    assert not combatant(live).sneak_attack_spent_this_turn
    assert live.rng.getstate() == expected.getstate()


def test_withdraw_shield_converted_miss_consumes_no_sneak_dice_and_does_not_move():
    shielded = pc(
        "char:shielded",
        initiative=30,
        class_slug="wizard",
        character_level=5,
        zone_id="1,0",
        ac=12,
        spells_known=["shield"],
        spell_slots={1: 2},
    )
    handle, live = start(
        [shielded, _rogue(attack_bonus=4), _ally()], seed=7, encounter=[foe(zone_id="4,4")]
    )
    act(handle, "char:shielded", intent_type="ready", spell_id="shield")
    expected = Random()
    expected.setstate(live.rng.getstate())
    natural = expected.randint(1, 20)
    _attack(
        handle,
        _withdraw(MovementChoice(destination_cell="0,2")),
        rogue=True,
        target="char:shielded",
    )
    [attack] = events(live, AttackRolled)
    assert (attack.natural, attack.is_hit) == (natural, False)
    assert len(events(live, ReactionTriggered)) == 1
    assert not events(live, DamageApplied)
    assert not events(live, AttackRiderTriggered)
    assert not events(live, ActorMoved)
    assert not combatant(live).sneak_attack_spent_this_turn
    assert live.rng.getstate() == expected.getstate()


@pytest.mark.parametrize("rogue", [False, True])
@pytest.mark.parametrize("destination", ["-1,0", "20,0"])
def test_out_of_bounds_choice_is_refused_before_attack_rng_payment_or_reckless_activation(
    rogue, destination
):
    handle, live = _start_rogue() if rogue else _start_barbarian()
    choice = MovementChoice(destination_cell=destination)
    request = _withdraw(choice) if rogue else _forceful(choice)
    before, offset = _snapshot(live), len(live.event_log)
    _attack(handle, request, rogue=rogue)
    _assert_rejected(live, before, offset)


@pytest.mark.parametrize("rogue", [False, True])
def test_route_exceeding_half_current_speed_is_refused_even_with_dash(rogue):
    handle, live = _start_rogue() if rogue else _start_barbarian()
    live.movement_ledgers[HERO] = MovementLedger(dash_count=2)
    movement.project_remaining(live, HERO)
    cap = movement.effective_speed(combatant(live), "walk", live) // 2
    choice = (
        MovementChoice(destination_cell="0,4") if rogue else MovementChoice(distance_ft=cap + 5)
    )
    request = _withdraw(choice) if rogue else _forceful(choice)
    before, offset = _snapshot(live), len(live.event_log)
    _attack(handle, request, rogue=rogue)
    _assert_rejected(live, before, offset)


def test_withdraw_preflight_accounts_for_terrain_route_cost_before_attack():
    handle, live = _start_rogue(
        grid=GridScene(width=2, height=5, difficult_terrain_cells=["0,1", "1,1", "0,2"])
    )
    before, offset = _snapshot(live), len(live.event_log)
    _attack(handle, _withdraw(MovementChoice(destination_cell="0,2")), rogue=True)
    _assert_rejected(live, before, offset)


@pytest.mark.parametrize(
    "choice",
    [
        {"distance_ft": True},
        {"distance_ft": "5"},
        {"distance_ft": -1},
        {"destination_cell": "broken"},
        {"destination_cell": "0,2", "distance_ft": 10},
        {"destination_cell": "0,2", "provokes_opportunity_attacks": False},
    ],
)
def test_malformed_nested_movement_choice_cannot_construct_public_intent_or_mutate_state(choice):
    _, live = _start_rogue()
    before, log = _snapshot(live), list(live.event_log)
    with pytest.raises(ValidationError):
        PlayerIntent(
            intent_type="attack",
            weapon_id="dagger",
            target_id=FOE,
            attack_riders=[
                {"feature_id": WITHDRAW[0], "activity_id": WITHDRAW[1], "movement_choice": choice}
            ],
        )
    assert _snapshot(live) == before
    assert live.event_log == log


@pytest.mark.parametrize("rogue", [False, True])
def test_public_post_hit_movement_replay_is_byte_equivalent(rogue):
    runs = []
    for _ in range(2):
        handle, live = _start_rogue() if rogue else _start_barbarian(character_level=17)
        request = (
            _withdraw(MovementChoice(destination_cell="0,2"))
            if rogue
            else _forceful(
                MovementChoice(distance_ft=10), options=("forceful-blow", "hamstring-blow")
            )
        )
        _attack(handle, request, rogue=rogue)
        assert not events(live, AttackFailed)
        assert events(live, AttackRiderTriggered)
        assert live.actor_zone[HERO] == ("0,2" if rogue else "2,0")
        runs.append(([event.model_dump_json() for event in live.event_log], _snapshot(live)))
    assert runs[0] == runs[1]
