"""Public action constraints, spell attempts and independent target lifecycles."""

import asyncio
import random

import pytest
from dnd5e_srd_data import BundledAssetLoader

from dnd5e_engine import orchestrator as orch
from dnd5e_engine.events import (
    CastFailed,
    DamageApplied,
    EffectApplied,
    EffectExpired,
    SaveRolled,
    SomaticSpellRolled,
)
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.specs import GridScene
from tests.c21_support import act, combatant, events, foe, pc
from tests.test_execution_integrity import snapshot

HERO = "char:hero"
FOE = "mon:foe"


@pytest.fixture(autouse=True)
def loader():
    set_lib_loader_for_tests(BundledAssetLoader())
    yield
    set_lib_loader_for_tests(None)


def table(*, scene=None, **ally):
    result = asyncio.run(
        orch.start_combat(
            session_id="policy",
            party=[
                pc(
                    class_slug="sorcerer",
                    character_level=13,
                    charisma=20,
                    spells_known=["slow", "fire-bolt", "misty-step"],
                    spell_slots={2: 3, 3: 3},
                ),
                pc(
                    "char:ally",
                    **(
                        {
                            "initiative": 19,
                            "zone_id": "3,0",
                            "wisdom": -30,
                            "spells_known": [
                                "fire-bolt",
                                "misty-step",
                                "healing-word",
                                "burning-hands",
                                "magic-missile",
                                "counterspell",
                                "hellish-rebuke",
                                "slow",
                                "sunbeam",
                            ],
                            "spell_slots": {1: 3, 2: 3, 3: 3, 6: 2},
                        }
                        | ally
                    ),
                ),
            ],
            encounter=[foe(zone_id="4,0")],
            grid_scene=scene or GridScene(width=30, height=20),
            rng_seed=19,
        )
    )
    live = orch._get_live(result.handle)
    orch._update_combatant(live, FOE, wisdom=-30)
    return result.handle, live


def slow(handle, targets=None):
    act(
        handle,
        HERO,
        intent_type="cast_spell",
        spell_id="slow",
        slot_level=3,
        target_zone_id="1,0",
        target_ids=targets or [FOE],
    )


def test_public_slow_attaches_reviewed_action_policy():
    handle, live = table()
    slow(handle)
    applied = [e.effect for e in events(live, EffectApplied) if e.effect.target_id == FOE]
    assert applied
    assert applied[0].action_policy.action_or_bonus
    assert applied[0].action_policy.deny_reactions
    assert applied[0].action_policy.attack_count_cap == 1
    assert not combatant(live).action_available


def slowed_ally(**kwargs):
    handle, live = table(**kwargs)
    slow(handle, ["char:ally"])
    take_turn(live, "char:ally")
    return handle, live


def take_turn(live, actor):
    from dnd5e_engine.events import TurnStarted

    live.current_turn_index = next(i for i, c in enumerate(live.initiative) if c.entity_id == actor)
    orch._emit(live, TurnStarted(actor_id=actor))


@pytest.mark.parametrize("first,second", [(False, True), (True, False)])
@pytest.mark.parametrize("action", ["dash", "disengage"])
def test_action_bonus_exclusion_in_both_orders(first, second, action):
    handle, live = slowed_ally(class_slug="rogue", character_level=5)
    act(handle, "char:ally", intent_type=action, use_bonus_action=first)
    before = snapshot(live)
    with pytest.raises(orch.IntentRejectedError, match="action_restricted"):
        act(handle, "char:ally", intent_type=action, use_bonus_action=second)
    assert snapshot(live) == before


def test_slow_caps_extra_attack_and_action_surge():
    handle, live = slowed_ally(class_slug="fighter", character_level=11, equipment=("longsword",))
    act(handle, "char:ally", intent_type="attack", target_id=FOE, weapon_id="longsword")
    before = snapshot(live)
    with pytest.raises(orch.IntentRejectedError, match="action_restricted"):
        act(handle, "char:ally", intent_type="attack", target_id=FOE, weapon_id="longsword")
    assert snapshot(live) == before
    act(handle, "char:ally", intent_type="use_feature", feature_id="action-surge")
    act(handle, "char:ally", intent_type="attack", target_id=FOE, weapon_id="longsword")
    assert combatant(live, "char:ally").extra_actions_remaining == 0
    assert combatant(live, "char:ally").attacks_remaining == 0
    before = snapshot(live)
    with pytest.raises(orch.IntentRejectedError, match="action_restricted"):
        act(handle, "char:ally", intent_type="attack", target_id=FOE, weapon_id="longsword")
    assert snapshot(live) == before


@pytest.mark.parametrize("seed,failed", [(1, True), (5, False)])
def test_somatic_cast_attempt_pays_slot_and_action_once(seed, failed):
    handle, live = slowed_ally(class_slug="sorcerer", character_level=5)
    live.rng = random.Random(seed)
    expected = random.Random(seed)
    roll = expected.randint(1, 100)
    before_slot = live.spell_slots_by_entity["char:ally"][1]
    act(
        handle,
        "char:ally",
        intent_type="cast_spell",
        spell_id="burning-hands",
        slot_level=1,
        direction=(1, 0),
    )
    attempt = events(live, SomaticSpellRolled)[-1]
    assert (attempt.roll, attempt.failed) == (roll, failed)
    assert live.spell_slots_by_entity["char:ally"][1] == before_slot - 1
    assert not combatant(live, "char:ally").action_available
    if failed:
        assert events(live, CastFailed)[-1].reason == "somatic_failure"
        assert not events(live, DamageApplied)
        assert live.rng.getstate() == expected.getstate()
    else:
        assert events(live, DamageApplied)


def test_non_somatic_cast_does_not_draw_percentile():
    from tests.test_typed_check_pipeline import CountingRandom

    handle, live = slowed_ally(class_slug="sorcerer", character_level=5)
    live.rng = CountingRandom(3)
    act(
        handle,
        "char:ally",
        intent_type="cast_spell",
        spell_id="healing-word",
        slot_level=1,
        target_id="char:ally",
    )
    assert not events(live, SomaticSpellRolled)
    assert not events(live, CastFailed)
    assert live.rng.draws
    assert all(draw[:2] == (1, 4) for draw in live.rng.draws)
    assert combatant(live, "char:ally").bonus_action_taken_this_turn


def test_illegal_cast_does_not_draw_or_pay():
    handle, live = slowed_ally(class_slug="sorcerer", character_level=5)
    before = snapshot(live)
    act(handle, "char:ally", intent_type="cast_spell", spell_id="burning-hands", slot_level=1)
    assert not events(live, SomaticSpellRolled)
    assert live.rng.getstate() == before[1]
    assert combatant(live, "char:ally").action_available
    assert live.spell_slots_by_entity["char:ally"][1] == 3


def test_numeric_projection_and_real_movement_restore_spent_distance():
    from dnd5e_engine.live_movement import effective_speed

    handle, live = slowed_ally(class_slug="rogue", character_level=5)
    target = combatant(live, "char:ally")
    assert effective_speed(target, "walk", live) == 15
    payload = orch._build_hydration_payload(live, caster=target)
    assert payload["save_modifiers"]["char:ally"]["saves"]["dex"] == -2
    assert payload["save_modifiers"]["char:ally"]["passive_ac_bonus"] == "-2"
    act(handle, "char:ally", intent_type="move", target_zone_id="3,1")
    assert combatant(live, "char:ally").movement_remaining == 10
    take_turn(live, HERO)
    act(handle, HERO, intent_type="drop_concentration")
    assert effective_speed(combatant(live, "char:ally"), "walk", live) == 30
    assert combatant(live, "char:ally").movement_remaining == 25


def test_repeat_save_ends_only_successful_target():
    handle, live = table()
    slow(handle, ["char:ally", FOE])
    take_turn(live, "char:ally")
    orch._update_combatant(live, "char:ally", wisdom=100)
    act(handle, "char:ally", intent_type="pass")
    assert not live.active_effects.get("char:ally")
    assert live.active_effects.get(FOE)
    assert events(live, SaveRolled)[-1].succeeded
    assert events(live, EffectExpired)[-1].reason == "save_succeeded"


def test_reaction_gate_uses_same_policy():
    from dnd5e_engine.live_reactions import can_take_reaction

    handle, live = slowed_ally(class_slug="sorcerer", character_level=5)
    assert combatant(live, "char:ally").reaction_available
    assert not can_take_reaction(live, combatant(live, "char:ally"))
    assert not can_take_reaction(live, combatant(live, "char:ally"), spell=True)
    take_turn(live, HERO)
    act(handle, HERO, intent_type="drop_concentration")
    assert can_take_reaction(live, combatant(live, "char:ally"))


def test_post_payment_failure_restores_policy_ledgers_rng_and_effects(monkeypatch):
    from dnd5e_engine import timed_activities

    handle, live = table()
    before = snapshot(live)
    original = timed_activities.resolve_activity

    def broken(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected")

    monkeypatch.setattr(timed_activities, "resolve_activity", broken)
    with pytest.raises(RuntimeError, match="injected"):
        slow(handle, ["char:ally", FOE])
    assert snapshot(live) == before


def grant(live, actor=HERO):
    from dnd5e_srd_data.schema.action_policy import ActionPolicy, RestrictedActionGrant

    from dnd5e_engine.types.effects import ActiveEffect

    effect = ActiveEffect(
        id="effect:grant",
        name="Reviewed grant",
        origin="test:source",
        target_id=actor,
        action_policy=ActionPolicy(
            extra_action=RestrictedActionGrant(
                actions=("attack", "dash", "disengage", "hide", "utilize")
            )
        ),
    )
    orch._emit(live, EffectApplied(effect=effect))
    return (effect.id, effect.origin)


@pytest.mark.parametrize("operation", ["dash", "disengage", "attack"])
def test_restricted_grant_is_separate_from_base_and_surge(operation):
    handle, live = table()
    source = grant(live)
    orch._update_combatant(live, HERO, extra_actions_remaining=1)
    request = {"target_id": FOE, "weapon_id": "shortbow"} if operation == "attack" else {}
    act(handle, HERO, intent_type=operation, action_grant=source, **request)
    actor = combatant(live)
    assert actor.action_available
    assert actor.extra_actions_remaining == 1
    assert actor.action_grants_spent == (source,)
    before = snapshot(live)
    with pytest.raises(orch.IntentRejectedError, match="action_restricted"):
        act(handle, HERO, intent_type=operation, action_grant=source, **request)
    assert snapshot(live) == before


@pytest.mark.parametrize(
    "kind", ["cast_spell", "activate_spell", "use_item", "use_feature", "dodge", "pass", "move"]
)
def test_restricted_grant_never_funds_incompatible_operation(kind):
    handle, live = table()
    source = grant(live)
    before = snapshot(live)
    with pytest.raises(orch.IntentRejectedError, match="action_restricted"):
        act(handle, HERO, intent_type=kind, action_grant=source)
    assert snapshot(live) == before


def test_restricted_attack_preserves_pending_normal_extra_attacks():
    handle, live = table(class_slug="fighter", character_level=11, equipment=("longsword",))
    take_turn(live, "char:ally")
    source = grant(live, "char:ally")
    act(handle, "char:ally", intent_type="attack", target_id=FOE, weapon_id="longsword")
    remaining = combatant(live, "char:ally").attacks_remaining
    act(
        handle,
        "char:ally",
        intent_type="attack",
        target_id=FOE,
        weapon_id="longsword",
        action_grant=source,
    )
    assert combatant(live, "char:ally").attacks_remaining == remaining == 2
    act(handle, "char:ally", intent_type="attack", target_id=FOE, weapon_id="longsword")
    assert combatant(live, "char:ally").attacks_remaining == 1


def test_slow_constraints_still_apply_to_restricted_grant():
    handle, live = slowed_ally(class_slug="rogue", character_level=5)
    source = grant(live, "char:ally")
    act(handle, "char:ally", intent_type="dash", use_bonus_action=True)
    before = snapshot(live)
    with pytest.raises(orch.IntentRejectedError, match="action_restricted"):
        act(handle, "char:ally", intent_type="dash", action_grant=source)
    assert snapshot(live) == before


def test_grant_resets_each_owner_turn_and_expires_with_source():
    handle, live = table()
    source = grant(live)
    act(handle, HERO, intent_type="dash", action_grant=source)
    take_turn(live, HERO)
    assert not combatant(live).action_grants_spent
    act(handle, HERO, intent_type="dash", action_grant=source)
    orch._emit(
        live,
        EffectExpired(target_id=HERO, effect_id=source[0], origin=source[1], reason="duration"),
    )
    take_turn(live, HERO)
    before = snapshot(live)
    with pytest.raises(orch.IntentRejectedError, match="action_restricted"):
        act(handle, HERO, intent_type="dash", action_grant=source)
    assert snapshot(live) == before


@pytest.mark.parametrize("first_bonus", [False, True])
def test_martial_arts_uses_exclusion_in_both_orders(first_bonus):
    handle, live = slowed_ally(class_slug="monk", character_level=5)
    if first_bonus:
        act(
            handle,
            "char:ally",
            intent_type="attack",
            weapon_id="unarmed-strike",
            target_id=FOE,
            use_bonus_action=True,
        )
        request = {"intent_type": "dash"}
    else:
        act(handle, "char:ally", intent_type="dash")
        request = {
            "intent_type": "attack",
            "weapon_id": "unarmed-strike",
            "target_id": FOE,
            "use_bonus_action": True,
        }
    before = snapshot(live)
    with pytest.raises(orch.IntentRejectedError, match="action_restricted"):
        act(handle, "char:ally", **request)
    assert snapshot(live) == before


def test_flurry_bonus_can_fund_strikes_but_cannot_unlock_action():
    handle, live = slowed_ally(class_slug="monk", character_level=5)
    act(
        handle,
        "char:ally",
        intent_type="use_feature",
        feature_id="monks-focus",
        activity_id="2ghJTBhilLrFn9xT",
    )
    assert combatant(live, "char:ally").flurry_strikes_remaining == 2
    for _ in range(2):
        act(handle, "char:ally", intent_type="attack", weapon_id="unarmed-strike", target_id=FOE)
    assert combatant(live, "char:ally").flurry_strikes_remaining == 0
    before = snapshot(live)
    with pytest.raises(orch.IntentRejectedError, match="action_restricted"):
        act(handle, "char:ally", intent_type="dash")
    assert snapshot(live) == before


def test_action_then_flurry_refuses_without_focus_payment():
    handle, live = slowed_ally(class_slug="monk", character_level=5)
    act(handle, "char:ally", intent_type="dash")
    before = snapshot(live)
    with pytest.raises(orch.IntentRejectedError, match="action_restricted"):
        act(
            handle,
            "char:ally",
            intent_type="use_feature",
            feature_id="monks-focus",
            activity_id="2ghJTBhilLrFn9xT",
        )
    assert snapshot(live) == before


@pytest.mark.parametrize("reaction", ["counterspell", "hellish-rebuke"])
def test_slow_prevents_actual_readied_spell_release(reaction):
    from dnd5e_engine.events import ReactionTriggered

    handle, live = slowed_ally(class_slug="sorcerer", character_level=5)
    act(
        handle,
        "char:ally",
        intent_type="ready",
        spell_id=reaction,
        slot_level=3 if reaction == "counterspell" else 1,
    )
    take_turn(live, HERO)
    before = dict(live.spell_slots_by_entity["char:ally"])
    act(handle, HERO, intent_type="cast_spell", spell_id="fire-bolt", target_id="char:ally")
    assert not events(live, ReactionTriggered)
    assert live.spell_slots_by_entity["char:ally"] == before
    assert live.pending_reactions
    assert combatant(live, "char:ally").reaction_available


def test_slow_prevents_opportunity_attack_without_consuming_reaction():
    from dnd5e_engine.events import ReactionTriggered

    handle, live = slowed_ally(class_slug="fighter", character_level=5)
    # The slowed ally is hostile to this moving monster.
    take_turn(live, FOE)
    act(handle, FOE, intent_type="move", target_zone_id="5,0")
    assert not events(live, ReactionTriggered)
    assert combatant(live, "char:ally").reaction_available


def test_slow_blocks_item_and_ongoing_magic_after_bonus():
    handle, live = table(class_slug="sorcerer", character_level=13, charisma=20)
    take_turn(live, "char:ally")
    act(
        handle,
        "char:ally",
        intent_type="cast_spell",
        spell_id="sunbeam",
        slot_level=6,
        direction=(1, 0),
    )
    source = next(a for a in live.persistent_areas.areas if a.source_entity_id == "char:ally")
    take_turn(live, HERO)
    slow(handle, ["char:ally"])
    take_turn(live, "char:ally")
    act(
        handle,
        "char:ally",
        intent_type="cast_spell",
        spell_id="healing-word",
        slot_level=1,
        target_id="char:ally",
    )
    before = snapshot(live)
    with pytest.raises(orch.IntentRejectedError, match="action_restricted"):
        act(
            handle,
            "char:ally",
            intent_type="activate_spell",
            source_id=source.id,
            activity_id="Y0cJvZuD7EfwGqkf",
            direction=(1, 0),
        )
    assert snapshot(live) == before
    with pytest.raises(orch.IntentRejectedError, match="action_restricted"):
        act(handle, "char:ally", intent_type="use_item", item_id="wand-of-fireballs")
    assert snapshot(live) == before


def test_two_casters_do_not_stack_numbers_and_older_source_resumes():
    from dnd5e_engine.live_movement import effective_speed

    handle, live = table(class_slug="sorcerer", character_level=5, charisma=20)
    slow(handle)
    take_turn(live, "char:ally")
    act(
        handle,
        "char:ally",
        intent_type="cast_spell",
        spell_id="slow",
        slot_level=3,
        target_zone_id="1,0",
        target_ids=[FOE],
    )
    assert len(live.active_effects[FOE]) == 2
    assert effective_speed(combatant(live, FOE), "walk", live) == 15
    payload = orch._build_hydration_payload(live, caster=combatant(live))
    assert payload["save_modifiers"][FOE]["passive_ac_bonus"] == "-2"
    take_turn(live, "char:ally")
    act(handle, "char:ally", intent_type="drop_concentration")
    assert len(live.active_effects[FOE]) == 1
    assert effective_speed(combatant(live, FOE), "walk", live) == 15
    take_turn(live, HERO)
    act(handle, HERO, intent_type="drop_concentration")
    assert effective_speed(combatant(live, FOE), "walk", live) == 30


def test_replay_includes_percentile_repeat_save_and_grant_ledgers():
    def run():
        handle, live = slowed_ally(class_slug="sorcerer", character_level=5)
        source = grant(live, "char:ally")
        live.rng = random.Random(1)
        act(
            handle,
            "char:ally",
            intent_type="cast_spell",
            spell_id="burning-hands",
            slot_level=1,
            direction=(1, 0),
        )
        act(handle, "char:ally", intent_type="dash", action_grant=source)
        assert combatant(live, "char:ally").action_grants_spent == (source,)
        act(handle, "char:ally", intent_type="pass")
        return snapshot(live)

    assert run() == run()


def test_somatic_roll_failure_injection_restores_paid_cost_and_rng(monkeypatch):
    handle, live = slowed_ally(class_slug="sorcerer", character_level=5)
    before = snapshot(live)
    original = orch._emit

    def broken(current, event):
        original(current, event)
        if isinstance(event, SomaticSpellRolled):
            raise RuntimeError("injected percentile")  # noqa: TRY004 - execution fault injection

    monkeypatch.setattr(orch, "_emit", broken)
    with pytest.raises(RuntimeError, match="injected percentile"):
        act(
            handle,
            "char:ally",
            intent_type="cast_spell",
            spell_id="burning-hands",
            slot_level=1,
            direction=(1, 0),
        )
    assert snapshot(live) == before


def test_haste_remains_fail_closed_until_all_mandatory_clauses_exist():
    handle, live = table()
    before = snapshot(live)
    act(
        handle,
        HERO,
        intent_type="cast_spell",
        spell_id="haste",
        slot_level=3,
        target_id="char:ally",
        willing_target_ids=["char:ally"],
    )
    assert events(live, CastFailed)[-1].reason == "unsupported_activity"
    assert live.rng.getstate() == before[1]
    assert combatant(live).action_available
    assert live.spell_slots_by_entity[HERO][3] == 3


def test_slow_duration_cleanup_and_repeat_event_order():
    handle, live = table()
    slow(handle)
    # Like Hold Person, concentration owns the ten-round cap; the target
    # lifecycle owns repeats, not a second competing duration countdown.
    # The public cast completed the caster's first turn, consuming round one.
    assert live.concentration_rounds_remaining[HERO] == 9
    take_turn(live, FOE)
    act(handle, FOE, intent_type="pass")
    assert live.active_effects[FOE]
    assert events(live, SaveRolled)[-1].target_id == FOE
    for remaining in range(9, 1, -1):
        assert live.concentration_rounds_remaining[HERO] == remaining
        take_turn(live, HERO)
        act(handle, HERO, intent_type="pass")
        assert live.active_effects[FOE]
    take_turn(live, HERO)
    act(handle, HERO, intent_type="pass")
    assert not live.effect_lifecycles
    assert not live.active_effects.get(FOE)
    expiry = events(live, EffectExpired)[-1]
    assert expiry.reason == "duration"
    assert live.event_log.index(events(live, SaveRolled)[-1]) < live.event_log.index(expiry)
    assert combatant(live).concentration_effect_id is None


def test_out_of_turn_slow_drop_restores_unspent_movement():
    handle, live = table()
    # A late-joined/pre-ledger target must capture expenditure before speed changes.
    live.movement_ledgers.pop(FOE)
    slow(handle)
    assert combatant(live, FOE).movement_remaining == 15
    take_turn(live, HERO)
    act(handle, HERO, intent_type="drop_concentration")
    assert combatant(live, FOE).movement_remaining == 30


def test_slow_nick_cannot_borrow_unspent_action_surge():
    handle, live = slowed_ally(
        class_slug="fighter", character_level=5, equipment=("scimitar", "dagger")
    )
    act(handle, "char:ally", intent_type="attack", target_id=FOE, weapon_id="scimitar")
    act(handle, "char:ally", intent_type="use_feature", feature_id="action-surge")
    before = snapshot(live)
    with pytest.raises(orch.IntentRejectedError, match="action_restricted"):
        act(
            handle,
            "char:ally",
            intent_type="attack",
            target_id=FOE,
            weapon_id="dagger",
            use_bonus_action=True,
        )
    assert snapshot(live) == before
    assert combatant(live, "char:ally").extra_actions_remaining == 1


def test_slow_new_surge_attack_can_change_to_a_nick_weapon():
    handle, live = slowed_ally(
        class_slug="fighter", character_level=5, equipment=("scimitar", "dagger")
    )
    act(handle, "char:ally", intent_type="attack", target_id=FOE, weapon_id="scimitar")
    act(handle, "char:ally", intent_type="use_feature", feature_id="action-surge")
    # This is an ordinary new Attack Action, not an explicit Light extra attack.
    act(handle, "char:ally", intent_type="attack", target_id=FOE, weapon_id="dagger")
    assert combatant(live, "char:ally").extra_actions_remaining == 0
    assert combatant(live, "char:ally").attacks_remaining == 0
    assert combatant(live, "char:ally").attack_action_attacks_made == 1


def test_grant_after_base_action_and_resolver_fault_rolls_back(monkeypatch):
    handle, live = table()
    source = grant(live)
    act(handle, HERO, intent_type="disengage")
    assert live.current_actor_id == HERO
    assert not combatant(live).action_available
    before = snapshot(live)
    original = orch.resolve_activity

    def broken(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected grant resolution")

    monkeypatch.setattr(orch, "resolve_activity", broken)
    with pytest.raises(RuntimeError, match="injected grant resolution"):
        act(
            handle,
            HERO,
            intent_type="attack",
            target_id=FOE,
            weapon_id="shortbow",
            action_grant=source,
        )
    assert snapshot(live) == before
    monkeypatch.setattr(orch, "resolve_activity", original)
    act(handle, HERO, intent_type="dash", action_grant=source)
    assert combatant(live).action_grants_spent == (source,)


def test_mutated_action_policy_cannot_inherit_admission():
    class Tampered(BundledAssetLoader):
        def get_spell(self, slug):
            spell = super().get_spell(slug)
            if slug == "slow":
                effect = spell.passive_effects[0]
                policy = effect.action_policy.model_copy(update={"somatic_failure_percent": 0})
                return spell.model_copy(
                    update={
                        "passive_effects": [effect.model_copy(update={"action_policy": policy})]
                    }
                )
            return spell

    set_lib_loader_for_tests(Tampered())
    handle, live = table()
    before = snapshot(live)
    slow(handle)
    assert events(live, CastFailed)[-1].reason == "unsupported_activity"
    assert live.rng.getstate() == before[1]
    assert combatant(live).action_available
    assert live.spell_slots_by_entity[HERO][3] == 3
    assert not live.active_effects


def test_slow_cast_still_has_legal_counterspell_window():
    from dnd5e_engine.events import ReactionTriggered

    handle, live = table(class_slug="sorcerer", character_level=5)
    take_turn(live, "char:ally")
    act(handle, "char:ally", intent_type="ready", spell_id="counterspell", slot_level=3)
    take_turn(live, HERO)
    orch._update_combatant(live, HERO, constitution=-30)
    slow(handle)
    assert events(live, ReactionTriggered)
    assert events(live, CastFailed)[-1].reason == "countered"
    assert live.spell_slots_by_entity[HERO][3] == 3
    assert not combatant(live).action_available
    assert not live.active_effects


def test_public_monster_somatic_failure_pays_limited_use_and_action():
    from dnd5e_srd_data import MemoryAssetLoader

    from tests.c21_support import start
    from tests.test_monster_spell_delivery import _cast_action

    bundled = BundledAssetLoader()
    spell = bundled.get_spell("thunderwave")
    action = _cast_action(spell)
    monster = bundled.get_monster("magma-mephit").model_copy(update={"actions": [action]})
    set_lib_loader_for_tests(
        MemoryAssetLoader(monsters=[monster], spells=[spell, bundled.get_spell("slow")])
    )
    handle, live = start(
        [pc(class_slug="sorcerer", charisma=20, spells_known=["slow"], spell_slots={3: 1})],
        seed=19,
        encounter=[foe(monster_template_slug=monster.slug, zone_id="1,0")],
    )
    orch._update_combatant(live, FOE, wisdom=-30)
    slow(handle)
    take_turn(live, FOE)
    live.rng = random.Random(1)
    asyncio.run(orch.advance_monster_turn(handle))
    assert events(live, SomaticSpellRolled)[-1].roll == 18
    assert events(live, CastFailed)[-1].reason == "somatic_failure"
    assert not events(live, DamageApplied)
    assert live.monster_action_uses_by_entity[FOE][action.slug].action_uses_remaining == 1
    assert combatant(live, FOE).action_taken_this_turn


def test_target_death_removes_policy_and_repeat_registration():
    from dnd5e_engine.events import Death

    handle, live = table()
    slow(handle)
    orch._update_combatant(live, FOE, hp_current=1, hp_max=1)
    live.tracked_hp[FOE] = 1
    take_turn(live, HERO)
    live.rng = random.Random(5)
    act(handle, HERO, intent_type="cast_spell", spell_id="fire-bolt", target_id=FOE)
    assert events(live, Death)[-1].target_id == FOE
    assert not live.effect_lifecycles
    assert not live.active_effects.get(FOE)


def test_slow_preserves_prone_and_other_speed_modifiers():
    from dnd5e_engine.events import ConditionApplied
    from dnd5e_engine.live_movement import effective_speed
    from dnd5e_engine.types.effects import ActiveEffect, ActiveEffectChange

    handle, live = slowed_ally(class_slug="rogue", character_level=5)
    orch._emit(
        live,
        EffectApplied(
            effect=ActiveEffect(
                id="effect:speed",
                origin="test:independent",
                name="Speed rider",
                target_id="char:ally",
                changes=[ActiveEffectChange(key="speed.reduction", mode="add", value=10)],
            )
        ),
    )
    orch._emit(live, ConditionApplied(target_id="char:ally", condition="prone", source="test"))
    assert effective_speed(combatant(live, "char:ally"), "crawl", live) == 10
    act(handle, "char:ally", intent_type="move", target_zone_id="3,1", movement_mode="crawl")
    assert combatant(live, "char:ally").movement_remaining == 0
    take_turn(live, HERO)
    act(handle, HERO, intent_type="drop_concentration")
    assert effective_speed(combatant(live, "char:ally"), "crawl", live) == 20
    assert combatant(live, "char:ally").movement_remaining == 10
    assert "prone" in orch._condition_names(combatant(live, "char:ally"))


def test_restricted_hide_runs_real_check_and_preserves_base_action():
    from dnd5e_engine.events import CheckRolled

    handle, live = table(
        scene=GridScene(width=30, height=20, cover_cells={"0,0": "three_quarters"})
    )
    source = grant(live)
    act(handle, HERO, intent_type="hide", action_grant=source)
    [check] = events(live, CheckRolled)
    assert check.actor_id == HERO
    assert check.skill == "stealth"
    assert combatant(live).action_available
    assert combatant(live).action_grants_spent == (source,)


@pytest.mark.parametrize("source", [("effect:missing", "test:source"), ("effect:grant", "wrong")])
def test_grant_requires_exact_owner_effect_identity(source):
    handle, live = table()
    grant(live, "char:ally")
    before = snapshot(live)
    with pytest.raises(orch.IntentRejectedError, match="action_restricted"):
        act(handle, HERO, intent_type="dash", action_grant=source)
    assert snapshot(live) == before
