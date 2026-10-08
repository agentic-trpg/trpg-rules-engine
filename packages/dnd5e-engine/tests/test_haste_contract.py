"""B6 public Haste, real Utilize, source lifecycle and atomic replay contracts."""

from functools import partial
from itertools import permutations

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader

from dnd5e_engine import live_effect_lifecycle as lifecycle
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.action_policy import classify_intent
from dnd5e_engine.events import (
    AreaCreated,
    AttackRolled,
    CastFailed,
    CheckRolled,
    ConcentrationCheck,
    ConditionApplied,
    DamageApplied,
    Death,
    EffectApplied,
    EffectExpired,
    ReactionTriggered,
    SaveRolled,
)
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.live_movement import effective_speed
from dnd5e_engine.rules.effects import effective_effects
from dnd5e_engine.specs import GridScene
from tests.c21_support import act, combatant, events, foe, monster_turn, pc, start
from tests.test_action_policy import take_turn
from tests.test_execution_integrity import snapshot

HERO, ALLY, OTHER, FOE = "char:hero", "char:ally", "char:other", "mon:foe"


@pytest.fixture(autouse=True)
def loader():
    set_lib_loader_for_tests(BundledAssetLoader())
    yield
    set_lib_loader_for_tests(None)


def table(*, extra_foe=False, **ally):
    caster = dict(
        class_slug="wizard",
        character_level=11,
        intelligence=18,
        spells_known=["haste", "slow", "fire-bolt", "counterspell"],
        spell_slots={3: 5},
        hp_current=500,
        hp_max=500,
    )
    return start(
        [
            pc(**caster),
            pc(
                ALLY,
                **(
                    dict(
                        initiative=19,
                        zone_id="1,1",
                        class_slug="fighter",
                        character_level=11,
                        hp_current=500,
                        hp_max=500,
                        equipment=(
                            "longsword",
                            "scimitar",
                            "dagger",
                            "greataxe",
                            "ball-bearings",
                            "caltrops",
                            "wand-of-fireballs",
                        ),
                        spells_known=["fire-bolt", "haste", "sunbeam", "counterspell"],
                        spell_slots={3: 3, 6: 1},
                    )
                    | ally
                ),
            ),
            pc(OTHER, initiative=18, zone_id="2,1", **caster),
        ],
        seed=19,
        encounter=[foe(zone_id="2,2")]
        + ([foe(entity_id="mon:other", zone_id="1,2")] if extra_foe else []),
    )


def haste(handle, actor=HERO, target=ALLY, **fields):
    act(
        handle,
        actor,
        **(
            dict(
                intent_type="cast_spell",
                spell_id="haste",
                slot_level=3,
                target_id=target,
                willing_target_ids=(target,),
            )
            | fields
        ),
    )


def buff(live, target=ALLY):
    return next(
        e
        for e in effective_effects(live.active_effects.get(target, ()))
        if e.action_policy and e.action_policy.extra_action
    )


def source(live, target=ALLY):
    effect = buff(live, target)
    return effect.id, effect.origin


def lethargies(live, target=ALLY):
    return [e for e in live.active_effects.get(target, ()) if "incapacitated" in e.statuses]


def hasted(**ally):
    handle, live = table(**ally)
    haste(handle)
    take_turn(live, ALLY)
    return handle, live


def test_legal_cast_numeric_projection_and_real_dexterity_save():
    _handle, live = hasted(dexterity=18)
    effect = buff(live)
    assert effect.lifecycle.source_id == HERO
    assert len(effect.end_effects) == 1
    assert not lethargies(live)
    assert live.spell_slots_by_entity[HERO][3] == 4
    assert live.concentration_chain[HERO] == [(ALLY, effect.id, effect.origin)]
    assert effective_speed(combatant(live, ALLY), "walk", live) == 60
    assert combatant(live, ALLY).movement_remaining == 60
    payload = orch._build_hydration_payload(live, caster=combatant(live))
    assert payload["save_modifiers"][ALLY]["passive_ac_bonus"] == "2"
    assert payload["save_modifiers"][ALLY]["passive_save_adv"] == ["DEX"]
    assert payload["save_modifiers"][HERO]["passive_save_adv"] == []
    result = orch._roll_unarmed_option_save(live, combatant(live), combatant(live, ALLY))
    assert result.ability == "dex"
    assert result.advantage == "advantage"
    assert events(live, SaveRolled)[-1] == result


@pytest.mark.parametrize(
    "invalid", ["willing", "blind", "range", "count", "slot", "followup", "missing"]
)
def test_illegal_cast_refuses_before_payment_rng_and_concentration(invalid):
    handle, live = table()
    haste(handle, target=OTHER)
    take_turn(live, HERO)
    fields = {}
    if invalid == "willing":
        fields["willing_target_ids"] = ()
    elif invalid == "blind":
        orch._emit(live, ConditionApplied(target_id=HERO, condition="blinded"))
    elif invalid == "range":
        live.actor_zone[ALLY] = "9,9"
    elif invalid == "count":
        fields.update(target_ids=[ALLY, OTHER], willing_target_ids=(ALLY, OTHER))
    elif invalid == "slot":
        live.spell_slots_by_entity[HERO][3] = 0
    elif invalid == "followup":
        fields["activity_id"] = "rBQrZnq7cHkSUAK9"
    else:
        fields.update(target_id="absent", willing_target_ids=("absent",))
    before = snapshot(live)
    haste(handle, **fields)
    after = snapshot(live)
    assert live.rng.getstate() == before[1]
    assert live.spell_slots_by_entity == before[0]["spell_slots_by_entity"]
    assert live.concentration_chain == before[0]["concentration_chain"]
    assert combatant(live).action_available
    assert events(live, CastFailed)
    # Only the typed refusal is observable.
    assert len(after[0]["event_log"]) == len(before[0]["event_log"]) + 1
    assert not lethargies(live, OTHER)


@pytest.mark.parametrize("order", list(permutations(("normal", "grant", "surge"))))
def test_extra_attack_and_action_surge_in_every_order(order):
    handle, live = hasted()
    grant = source(live)
    for funding in order:
        if funding == "surge":
            act(handle, ALLY, intent_type="use_feature", feature_id="action-surge")
        count = 1 if funding == "grant" else 3
        for _ in range(count):
            before = combatant(live, ALLY)
            act(
                handle,
                ALLY,
                intent_type="attack",
                weapon_id="longsword",
                target_id=FOE,
                action_grant=grant if funding == "grant" else None,
            )
            if funding == "grant":
                after = combatant(live, ALLY)
                assert (
                    after.action_available,
                    after.extra_actions_remaining,
                    after.attacks_remaining,
                ) == (
                    before.action_available,
                    before.extra_actions_remaining,
                    before.attacks_remaining,
                )
    assert len(events(live, AttackRolled)) == 7
    actor = combatant(live, ALLY)
    assert actor.action_grants_spent == (grant,)
    assert actor.action_grant_groups_spent == ("haste",)
    assert not actor.action_available
    assert actor.extra_actions_remaining == actor.attacks_remaining == 0
    before = snapshot(live)
    with pytest.raises(orch.IntentRejectedError, match="action_restricted"):
        act(
            handle,
            ALLY,
            intent_type="attack",
            weapon_id="longsword",
            target_id=FOE,
            action_grant=grant,
        )
    assert snapshot(live) == before


@pytest.mark.parametrize("operation", ["dash", "disengage", "hide", "grapple", "shove"])
def test_grant_uses_real_existing_action_handlers(operation):
    handle, live = hasted()
    if operation == "hide":
        live.topology = orch.GridTopology(
            GridScene(width=10, height=10, cover_cells={"1,1": "three_quarters"})
        )
    grant = source(live)
    kwargs = dict(target_id=FOE) if operation in ("grapple", "shove") else {}
    # A grapple/shove victim must be within reach.
    if kwargs:
        live.actor_zone[FOE] = "2,1"
    act(handle, ALLY, intent_type=operation, action_grant=grant, **kwargs)
    actor = combatant(live, ALLY)
    assert actor.action_available
    assert actor.extra_actions_remaining == 0
    assert actor.action_grants_spent == (grant,)
    if operation == "dash":
        assert actor.movement_remaining == 120
    elif operation == "disengage":
        assert actor.disengaging_this_turn
    elif operation == "hide":
        assert events(live, CheckRolled)[-1].context == "hide"
    else:
        assert events(live, SaveRolled)


@pytest.mark.parametrize("item,cell", [("ball-bearings", "3,1"), ("caltrops", "2,1")])
@pytest.mark.parametrize("payment", ["grant", "surge"])
def test_reviewed_utilize_creates_real_area_and_consumes_item(item, cell, payment):
    handle, live = hasted()
    grant = source(live)
    if payment == "surge":
        act(handle, ALLY, intent_type="use_feature", feature_id="action-surge")
    before = combatant(live, ALLY)
    act(
        handle,
        ALLY,
        intent_type="use_item",
        item_id=item,
        target_zone_id=cell,
        action_grant=grant if payment == "grant" else None,
    )
    assert events(live, AreaCreated)[-1].source_id
    assert live.persistent_areas.areas[-1].source_id == item
    assert live.custom_counters_by_entity[ALLY][f"item_use:{item}"]["spent"] == 1
    assert combatant(live, ALLY).action_available == before.action_available
    assert combatant(live, ALLY).extra_actions_remaining == (
        before.extra_actions_remaining if payment == "grant" else 0
    )
    from dnd5e_engine.persistent_areas import after_movement_step

    previous = live.actor_zone[FOE]
    live.actor_zone[FOE] = cell
    orch._update_combatant(live, FOE, dexterity=-30)
    after_movement_step(live, FOE, previous)
    assert events(live, SaveRolled)[-1].dc == (10 if item == "ball-bearings" else 15)
    assert (
        "prone" in live.active_conditions[FOE]
        if item == "ball-bearings"
        else effective_speed(combatant(live, FOE), "walk", live) == 0
    )


@pytest.mark.parametrize(
    "fields",
    [
        dict(intent_type="cast_spell", spell_id="fire-bolt", target_id=FOE),
        dict(intent_type="activate_spell", spell_id="sunbeam"),
        dict(intent_type="use_item", item_id="wand-of-fireballs", target_zone_id="2,2"),
        dict(intent_type="use_item", item_id="potion-of-speed"),
        dict(intent_type="use_item", item_id="ball-bearings", activity_id="unknown"),
        dict(intent_type="use_feature", feature_id="second-wind"),
        dict(intent_type="dodge"),
        dict(intent_type="attack", weapon_id="dagger", target_id=FOE, use_bonus_action=True),
    ],
)
def test_grant_never_admits_magic_unknown_or_bonus_operations(fields):
    handle, live = hasted()
    before = snapshot(live)
    with pytest.raises(orch.IntentRejectedError, match="action_restricted"):
        act(handle, ALLY, action_grant=source(live), **fields)
    assert snapshot(live) == before


def test_granted_attack_opens_only_bonus_light_and_never_free_nick():
    handle, live = hasted()
    grant = source(live)
    act(handle, ALLY, intent_type="attack", weapon_id="scimitar", target_id=FOE, action_grant=grant)
    actor = combatant(live, ALLY)
    assert actor.light_weapon_swing_slug is None
    assert not actor.attack_action_engaged
    assert not actor.offhand_attack_spent
    assert actor.restricted_light_weapon_swing_slug == "scimitar"
    assert len(events(live, AttackRolled)) == 1
    act(
        handle, ALLY, intent_type="attack", weapon_id="dagger", target_id=FOE, use_bonus_action=True
    )
    actor = combatant(live, ALLY)
    assert not actor.bonus_action_available
    assert actor.action_available
    assert actor.offhand_attack_spent
    assert actor.attack_action_attacks_made == 0
    assert len(events(live, AttackRolled)) == 2


@pytest.mark.parametrize("bonus", ["martial", "flurry"])
def test_monk_bonus_strikes_survive_granted_attack(bonus):
    handle, live = hasted(class_slug="monk", character_level=11, equipment=("unarmed-strike",))
    if bonus == "flurry":
        act(
            handle,
            ALLY,
            intent_type="use_feature",
            feature_id="monks-focus",
            activity_id="2ghJTBhilLrFn9xT",
        )
    act(
        handle,
        ALLY,
        intent_type="attack",
        weapon_id="unarmed-strike",
        target_id=FOE,
        action_grant=source(live),
    )
    assert combatant(live, ALLY).action_available
    for _ in range(3 if bonus == "flurry" else 1):
        act(
            handle,
            ALLY,
            intent_type="attack",
            weapon_id="unarmed-strike",
            target_id=FOE,
            use_bonus_action=bonus == "martial",
        )
    assert len(events(live, AttackRolled)) == (4 if bonus == "flurry" else 2)


@pytest.mark.parametrize("current", [HERO, ALLY, OTHER])
def test_lethargy_lasts_through_exact_next_target_turn(current):
    handle, live = table()
    haste(handle)
    take_turn(live, current)
    # Public voluntary drop is only available on the caster's turn; the same
    # lifecycle fold is also exercised on arbitrary off-turn legal expiry.
    if current == HERO:
        act(handle, HERO, intent_type="drop_concentration")
    else:
        with orch._execution_transaction(live):
            orch._drop_concentration(live, HERO)
    assert len(lethargies(live)) == 1
    assert effective_speed(combatant(live, ALLY), "walk", live) == 0
    assert combatant(live, ALLY).movement_remaining == 0
    if current == ALLY:
        act(handle, ALLY, intent_type="pass")
        assert lethargies(live)
    take_turn(live, ALLY)
    with pytest.raises(orch.IntentRejectedError):
        act(handle, ALLY, intent_type="dash")
    assert combatant(live, ALLY).movement_remaining == 0
    act(handle, ALLY, intent_type="pass")
    assert not lethargies(live)
    assert "incapacitated" not in live.active_conditions.get(ALLY, set())


@pytest.mark.parametrize(
    "reason", ["duration", "dispelled", "remove_ieffect", "concentration_drop", "source_dead"]
)
def test_every_actual_effect_end_produces_once_and_preserves_event_order(reason):
    _handle, live = hasted()
    effect = buff(live)
    pre = len(live.event_log)
    with orch._execution_transaction(live):
        orch._emit(
            live,
            EffectExpired(target_id=ALLY, effect_id=effect.id, origin=effect.origin, reason=reason),
        )
    trace = live.event_log[pre:]
    assert [type(e) for e in trace[:3]] == [EffectExpired, EffectApplied, ConditionApplied]
    assert trace[1].effect == lethargies(live)[0]
    assert effect not in live.active_effects[ALLY]
    assert not live.concentration_chain.get(HERO)
    assert effective_speed(combatant(live, ALLY), "walk", live) == 0
    orch._emit(live, trace[0])
    assert len(lethargies(live)) == 1


def test_multiple_casters_suppression_restoration_and_independent_lethargy():
    handle, live = table()
    haste(handle)
    older = buff(live)
    take_turn(live, OTHER)
    haste(handle, OTHER)
    newer = buff(live)
    assert older.origin != newer.origin
    assert len(live.active_effects[ALLY]) == 2
    assert effective_speed(combatant(live, ALLY), "walk", live) == 60
    take_turn(live, ALLY)
    before = snapshot(live)
    with pytest.raises(orch.IntentRejectedError, match="action_restricted"):
        act(handle, ALLY, intent_type="dash", action_grant=(older.id, older.origin))
    assert snapshot(live) == before
    act(handle, ALLY, intent_type="dash", action_grant=(newer.id, newer.origin))
    take_turn(live, OTHER)
    act(handle, OTHER, intent_type="drop_concentration")
    assert buff(live) == older
    assert len(lethargies(live)) == 1
    assert live.concentration_chain[HERO] == [(ALLY, older.id, older.origin)]
    take_turn(live, HERO)
    act(handle, HERO, intent_type="drop_concentration")
    assert len(lethargies(live)) == 2
    take_turn(live, ALLY)
    act(handle, ALLY, intent_type="pass")
    assert not lethargies(live)
    assert "incapacitated" not in live.active_conditions[ALLY]
    assert effective_speed(combatant(live, ALLY), "walk", live) == 30


def test_repeat_cast_ends_prior_application_and_uses_distinct_identity():
    handle, live = table()
    haste(handle)
    old = buff(live)
    take_turn(live, HERO)
    haste(handle)
    new = buff(live)
    assert old.origin != new.origin
    assert len(lethargies(live)) == 1
    assert [e.effect_id for e in events(live, EffectExpired)] == [old.id]
    assert live.concentration_chain[HERO] == [(ALLY, new.id, new.origin)]


def test_slow_speed_prone_dash_and_movement_preserve_spent_distance():
    handle, live = hasted(class_slug="rogue", character_level=5)
    act(handle, ALLY, intent_type="move", target_zone_id="1,2")
    assert combatant(live, ALLY).movement_remaining == 55
    take_turn(live, OTHER)
    orch._update_combatant(live, ALLY, wisdom=-30)
    act(
        handle,
        OTHER,
        intent_type="cast_spell",
        spell_id="slow",
        slot_level=3,
        target_zone_id="1,1",
        target_ids=[ALLY],
    )
    assert effective_speed(combatant(live, ALLY), "walk", live) == 30
    assert combatant(live, ALLY).movement_remaining == 25
    take_turn(live, ALLY)
    act(handle, ALLY, intent_type="dash", use_bonus_action=True)
    before = snapshot(live)
    with pytest.raises(orch.IntentRejectedError, match="action_restricted"):
        act(handle, ALLY, intent_type="dash", action_grant=source(live))
    assert snapshot(live) == before
    orch._emit(live, ConditionApplied(target_id=ALLY, condition="prone"))
    assert effective_speed(combatant(live, ALLY), "crawl", live) == 30
    act(handle, ALLY, intent_type="stand_up")
    assert combatant(live, ALLY).movement_remaining == 45


def test_countered_haste_pays_action_preserves_slot_and_unused_surge():
    handle, live = table()
    take_turn(live, OTHER)
    act(handle, OTHER, intent_type="ready", spell_id="counterspell", slot_level=3)
    take_turn(live, HERO)
    orch._update_combatant(live, HERO, constitution=-30, extra_actions_remaining=1)
    haste(handle)
    assert events(live, ReactionTriggered)
    assert events(live, CastFailed)[-1].reason == "countered"
    assert live.spell_slots_by_entity[HERO][3] == 5
    assert not combatant(live).action_available
    assert combatant(live).extra_actions_remaining == 1
    assert not live.active_effects


@pytest.mark.parametrize(
    "point", ["producer", "effect", "condition", "movement", "grant", "cast", "reaction"]
)
def test_fault_injection_rolls_back_every_field_and_retry_matches_replay(monkeypatch, point):
    def setup():
        handle, live = hasted()
        if point in ("cast", "reaction"):
            if point == "reaction":
                take_turn(live, OTHER)
                act(handle, OTHER, intent_type="ready", spell_id="counterspell", slot_level=3)
            take_turn(live, HERO)
            request = partial(haste, handle, target=OTHER)
        elif point == "grant":
            request = partial(act, handle, ALLY, intent_type="hide", action_grant=source(live))
            live.topology = orch.GridTopology(
                GridScene(width=10, height=10, cover_cells={"1,1": "three_quarters"})
            )
        else:
            take_turn(live, HERO)
            request = partial(act, handle, HERO, intent_type="drop_concentration")
        return live, request

    live, request = setup()
    before = snapshot(live)
    observed = []
    live.event_listeners.append(observed.append)
    owner, name = {
        "producer": (lifecycle, "produce_end_effects"),
        "effect": (orch, "_emit_apply_effect_applied"),
        "condition": (orch, "_attach_effect_statuses"),
        "movement": (orch, "_clamp_movement_budget"),
        "grant": (orch, "resolve_live_check"),
        "cast": (orch, "_consume_spell_slot"),
        "reaction": (orch, "spell_cast_opportunity"),
    }[point]
    original = getattr(owner, name)

    def broken(*args, **kwargs):
        original(*args, **kwargs)
        live.rng.randint(1, 20)
        raise RuntimeError("injected B6 failure")

    with monkeypatch.context() as patch:
        patch.setattr(owner, name, broken)
        with pytest.raises(RuntimeError, match="injected B6 failure"):
            request()
    assert snapshot(live) == before
    assert observed == []
    request()
    expected, clean_request = setup()
    clean_request()
    assert snapshot(live) == snapshot(expected)
    if point == "reaction":
        assert events(live, ReactionTriggered)


def test_potion_of_speed_stays_deferred_without_consumption_or_partial_buff():
    handle, live = table(equipment=("potion-of-speed",))
    take_turn(live, ALLY)
    before = snapshot(live)
    act(
        handle,
        ALLY,
        intent_type="use_item",
        item_id="potion-of-speed",
        target_id=ALLY,
        willing_target_ids=(ALLY,),
    )
    assert events(live, CastFailed)[-1].reason == "unsupported_area"
    assert live.rng.getstate() == before[1]
    assert live.custom_counters_by_entity == before[0]["custom_counters_by_entity"]
    assert combatant(live, ALLY).action_available
    assert not live.active_effects


def test_classification_is_data_driven_and_renaming_is_inert():
    bundled = BundledAssetLoader()
    item = bundled.get_item("ball-bearings").model_copy(
        update={"slug": "renamed", "name": "Magic Words", "description": "cast a spell"}
    )
    set_lib_loader_for_tests(MemoryAssetLoader(items=[item]))
    assert (
        classify_intent(orch.PlayerIntent(intent_type="use_item", item_id="renamed")) == "utilize"
    )


@pytest.mark.parametrize("normal_first", [False, True])
def test_normal_nick_window_survives_interleaved_restricted_light(normal_first):
    handle, live = hasted()
    grant = source(live)
    if normal_first:
        act(handle, ALLY, intent_type="attack", weapon_id="scimitar", target_id=FOE)
    act(handle, ALLY, intent_type="attack", weapon_id="scimitar", target_id=FOE, action_grant=grant)
    if not normal_first:
        act(handle, ALLY, intent_type="attack", weapon_id="scimitar", target_id=FOE)
    actor = combatant(live, ALLY)
    assert actor.light_weapon_swing_slug == actor.restricted_light_weapon_swing_slug == "scimitar"
    attacks = actor.attacks_remaining
    act(
        handle, ALLY, intent_type="attack", weapon_id="dagger", target_id=FOE, use_bonus_action=True
    )
    actor = combatant(live, ALLY)
    assert actor.bonus_action_available
    assert actor.attacks_remaining == attacks
    assert actor.attack_action_attacks_made == 2
    assert actor.offhand_attack_spent


def test_haste_duration_produces_penalty_and_renews_grant_on_each_target_turn():
    handle, live = hasted()
    grant = source(live)
    for _ in range(2):
        assert combatant(live, ALLY).action_grants_spent == ()
        act(handle, ALLY, intent_type="dash", action_grant=grant)
        take_turn(live, ALLY)
    for remaining in range(live.concentration_rounds_remaining[HERO], 0, -1):
        assert live.concentration_rounds_remaining[HERO] == remaining
        take_turn(live, HERO)
        act(handle, HERO, intent_type="pass")
    assert not live.concentration_chain.get(HERO)
    assert len(lethargies(live)) == 1
    assert events(live, EffectExpired)[-1].reason == "duration"
    assert effective_speed(combatant(live, ALLY), "walk", live) == 0


def test_lethargy_cascades_another_casters_haste_concentration_deterministically():
    handle, live = table()
    haste(handle, target=OTHER)
    take_turn(live, OTHER)
    haste(handle, actor=OTHER, target=ALLY)
    take_turn(live, HERO)
    pre = len(live.event_log)
    act(handle, HERO, intent_type="drop_concentration")
    assert len(lethargies(live, OTHER)) == len(lethargies(live, ALLY)) == 1
    assert not live.concentration_chain
    ends = [event for event in live.event_log[pre:] if isinstance(event, EffectExpired)]
    assert [event.target_id for event in ends] == [OTHER, ALLY]
    assert (
        len({effect.origin for target in (ALLY, OTHER) for effect in lethargies(live, target)}) == 2
    )


def test_ending_one_penalty_preserves_other_source_and_speed_zero():
    handle, live = table()
    haste(handle)
    take_turn(live, OTHER)
    haste(handle, actor=OTHER)
    take_turn(live, HERO)
    act(handle, HERO, intent_type="drop_concentration")
    older_penalty = lethargies(live)[0]
    take_turn(live, ALLY)
    with orch._execution_transaction(live):
        orch._drop_concentration(live, OTHER)
    act(handle, ALLY, intent_type="pass")
    assert len(lethargies(live)) == 1
    assert lethargies(live)[0].origin != older_penalty.origin
    assert "incapacitated" in live.active_conditions[ALLY]
    assert effective_speed(combatant(live, ALLY), "walk", live) == 0
    take_turn(live, ALLY)
    act(handle, ALLY, intent_type="pass")
    assert not lethargies(live)


def test_same_spell_group_cannot_refresh_spent_allowance_midturn():
    handle, live = hasted()
    act(handle, ALLY, intent_type="dash", action_grant=source(live))
    # A second legal public caster introduces a new same-spell source while
    # retaining the actor's already-paid turn ledger (off-turn host scheduling).
    previous = combatant(live, ALLY)
    take_turn(live, OTHER)
    haste(handle, actor=OTHER)
    take_turn(live, ALLY)
    orch._update_combatant(
        live,
        ALLY,
        action_grants_spent=previous.action_grants_spent,
        action_grant_groups_spent=previous.action_grant_groups_spent,
    )
    before = snapshot(live)
    with pytest.raises(orch.IntentRejectedError, match="action_restricted"):
        act(handle, ALLY, intent_type="dash", action_grant=source(live))
    assert snapshot(live) == before


@pytest.mark.parametrize("enabled", [False, True])
def test_haste_ac_changes_actual_attack_hit_resolution(enabled):
    handle, live = table(ac=13)
    if enabled:
        haste(handle)
    take_turn(live, HERO)
    live.rng.seed(13)
    act(handle, HERO, intent_type="attack", weapon_id="unarmed-strike", target_id=ALLY)
    roll = events(live, AttackRolled)[-1]
    assert roll.roll_total == 13
    assert roll.is_hit is (not enabled)


def test_granted_cleave_attack_never_rolls_the_real_adjacent_candidate():
    handle, live = hasted(extra_foe=True)
    act(
        handle,
        ALLY,
        intent_type="attack",
        weapon_id="greataxe",
        target_id=FOE,
        action_grant=source(live),
    )
    assert len(events(live, AttackRolled)) == 1
    assert not combatant(live, ALLY).cleave_spent_this_turn
    act(handle, ALLY, intent_type="attack", weapon_id="greataxe", target_id=FOE)
    assert len(events(live, AttackRolled)) == 3
    assert combatant(live, ALLY).cleave_spent_this_turn


def test_speed_multipliers_round_once_and_zero_overrides_all_modes():
    from dnd5e_engine import ActiveEffect, ActiveEffectChange

    _handle, live = hasted(base_speed=31)
    other = ActiveEffect(
        id="effect:other",
        name="Other",
        origin="feature:other",
        target_id=ALLY,
        changes=[
            ActiveEffectChange(key="system.attributes.movement.walk", mode="multiply", value="0.5")
        ],
    )
    orch._emit(live, EffectApplied(effect=other))
    assert effective_speed(combatant(live, ALLY), "walk", live) == 31
    live.active_effects[ALLY].reverse()
    assert effective_speed(combatant(live, ALLY), "walk", live) == 31
    zero = ActiveEffect(
        id="effect:immobile",
        name="Immobile",
        origin="feature:other",
        target_id=ALLY,
        changes=[ActiveEffectChange(key="speed.override", mode="override", value=0)],
    )
    orch._emit(live, EffectApplied(effect=zero))
    assert all(
        effective_speed(combatant(live, ALLY), mode, live) == 0
        for mode in ("walk", "crawl", "swim", "climb", "fly", "burrow")
    )


def test_generic_end_producer_never_reads_the_spell_library(monkeypatch):
    handle, live = hasted()
    take_turn(live, HERO)

    def forbidden(*args):
        raise AssertionError("effect-end consulted mutable spell data")

    monkeypatch.setattr(live.ruleset_loader, "get_spell", forbidden)
    act(handle, HERO, intent_type="drop_concentration")
    assert len(lethargies(live)) == 1
    assert lethargies(live)[0].end_effects == ()


def test_condition_immunity_does_not_leave_lethargy_speed_zero_without_an_expiry():
    handle, live = hasted(condition_immunities=["incapacitated"])
    take_turn(live, HERO)
    act(handle, HERO, intent_type="drop_concentration")
    assert "incapacitated" not in live.active_conditions.get(ALLY, set())
    assert effective_speed(combatant(live, ALLY), "walk", live) == 0
    take_turn(live, ALLY)
    act(handle, ALLY, intent_type="pass")
    assert not lethargies(live)
    assert effective_speed(combatant(live, ALLY), "walk", live) == 30


def natural_table(*, victim=None, other_caster=False, constitution=-30):
    """All transitions below use public intents in the actual initiative order."""
    return start(
        [
            pc(
                HERO,
                constitution=constitution,
                hp_current=1 if victim == HERO else 500,
                hp_max=1 if victim == HERO else 500,
                spells_known=["haste", "slow"],
                spell_slots={3: 2},
            ),
            pc(
                OTHER,
                initiative=19,
                zone_id="1,0",
                class_slug="wizard" if other_caster else "fighter",
                character_level=1,
                attack_bonus=100,
                strength=20,
                equipment=("longsword",),
                spells_known=["haste"] if other_caster else [],
                spell_slots={3: 1} if other_caster else {},
            ),
            pc(
                ALLY,
                initiative=18,
                zone_id="0,1",
                hp_current=1 if victim == ALLY else 500,
                hp_max=1 if victim == ALLY else 500,
            ),
        ],
        seed=19,
        encounter=[foe(zone_id="9,9", attack_bonus=-100)],
    )


@pytest.mark.parametrize("ending", ["damage", "source_death", "target_death"])
def test_public_damage_death_and_next_target_end_follow_natural_initiative(ending):
    def execute():
        victim = HERO if ending == "source_death" else ALLY if ending == "target_death" else None
        handle, live = natural_table(victim=victim)
        haste(handle)
        original = buff(live)
        assert orch._current_actor(live).entity_id == OTHER
        act(
            handle,
            OTHER,
            intent_type="attack",
            weapon_id="longsword",
            target_id=ALLY if ending == "target_death" else HERO,
        )
        assert events(live, DamageApplied)
        assert not live.concentration_chain
        assert not live.effect_lifecycles.get((ALLY, original.id, original.origin))
        if ending == "target_death":
            assert ALLY in live.dead_ids
            assert events(live, Death)
            assert not live.active_effects.get(ALLY)
            assert not lethargies(live)
            assert not live.conditions_by_effect
        else:
            if ending == "damage":
                assert not events(live, ConcentrationCheck)[-1].succeeded
                assert HERO not in live.dead_ids
            else:
                assert HERO in live.dead_ids
                assert events(live, Death)
            [penalty] = lethargies(live)
            assert penalty.lifecycle.source_id == HERO
            assert penalty.lifecycle.source_slug == "haste"
            assert effective_speed(combatant(live, ALLY), "walk", live) == 0
            assert orch._current_actor(live).entity_id == ALLY
            assert combatant(live, ALLY).movement_remaining == 0
            before = snapshot(live)
            with pytest.raises(orch.IntentRejectedError):
                act(handle, ALLY, intent_type="dash")
            assert snapshot(live) == before
            act(handle, ALLY, intent_type="pass")
            assert not lethargies(live)
            assert "incapacitated" not in live.active_conditions[ALLY]
            assert effective_speed(combatant(live, ALLY), "walk", live) == 30
        return snapshot(live)

    assert execute() == execute()


def test_public_self_haste_ending_midturn_persists_through_next_natural_turn():
    handle, live = natural_table()
    haste(handle, target=HERO)
    act(handle, HERO, intent_type="drop_concentration")
    assert lethargies(live, HERO)
    act(handle, HERO, intent_type="pass")
    assert lethargies(live, HERO)
    act(handle, OTHER, intent_type="pass")
    act(handle, ALLY, intent_type="pass")
    monster_turn(handle)
    assert orch._current_actor(live).entity_id == HERO
    assert lethargies(live, HERO)
    assert combatant(live, HERO).movement_remaining == 0
    act(handle, HERO, intent_type="pass")
    assert not lethargies(live, HERO)
    assert "incapacitated" not in live.active_conditions[HERO]


def test_public_duration_ending_on_target_turn_end_uses_following_natural_turn():
    handle, live = natural_table()
    haste(handle, target=HERO)
    for _ in range(10):
        assert orch._current_actor(live).entity_id == HERO
        assert not lethargies(live, HERO)
        act(handle, HERO, intent_type="pass")
        act(handle, OTHER, intent_type="pass")
        act(handle, ALLY, intent_type="pass")
        monster_turn(handle)
    assert not live.concentration_chain
    assert len(lethargies(live, HERO)) == 1
    assert effective_speed(combatant(live), "walk", live) == 0
    act(handle, HERO, intent_type="pass")
    assert not lethargies(live, HERO)


@pytest.mark.parametrize("replacement", ["self", "other", "slow"])
def test_public_self_haste_replacement_cannot_leave_new_concentration_or_payload(replacement):
    handle, live = natural_table()
    haste(handle, target=HERO)
    act(handle, HERO, intent_type="pass")
    act(handle, OTHER, intent_type="pass")
    act(handle, ALLY, intent_type="pass")
    monster_turn(handle)
    offset = len(live.event_log)
    if replacement == "slow":
        act(
            handle,
            HERO,
            intent_type="cast_spell",
            spell_id="slow",
            slot_level=3,
            target_zone_id="1,0",
            target_ids=[OTHER],
        )
    else:
        haste(handle, target=HERO if replacement == "self" else ALLY)
    assert len(lethargies(live, HERO)) == 1
    assert not live.concentration_chain
    assert not live.concentration_rounds_remaining
    assert combatant(live).concentration_effect_id is None
    assert live.spell_slots_by_entity[HERO][3] == 0
    assert not [e for e in live.active_effects.get(ALLY, ()) if e.action_policy]
    assert not [e for e in live.active_effects.get(OTHER, ()) if e.action_policy]
    assert not [
        event
        for event in live.event_log[offset:]
        if isinstance(event, EffectApplied) and event.effect.flags.get("concentration")
    ]
    assert not [event for event in live.event_log[offset:] if isinstance(event, SaveRolled)]


def test_haste_save_flags_preserve_activated_rage_damage_and_resistance_projection():
    handle, live = hasted(class_slug="barbarian", character_level=11)
    act(handle, ALLY, intent_type="use_feature", feature_id="rage")
    payload = orch._build_hydration_payload(live, caster=combatant(live, ALLY))
    assert payload["save_modifiers"][ALLY]["passive_save_adv"] == ["DEX"]
    damage = payload["passive_damage_modifiers"][ALLY]
    assert set(damage["resistances"]) == {"bludgeoning", "piercing", "slashing"}
    assert damage["passive_melee_damage_bonus"] == "3"


def test_public_overlapping_casters_grants_and_penalties_follow_natural_initiative():
    def execute():
        handle, live = natural_table(other_caster=True, constitution=40)
        haste(handle)
        older = buff(live)
        assert orch._current_actor(live).entity_id == OTHER
        haste(handle, actor=OTHER)
        newer = buff(live)
        assert orch._current_actor(live).entity_id == ALLY
        assert older.origin != newer.origin
        assert effective_speed(combatant(live, ALLY), "walk", live) == 60
        before = snapshot(live)
        with pytest.raises(orch.IntentRejectedError, match="action_restricted"):
            act(handle, ALLY, intent_type="dash", action_grant=(older.id, older.origin))
        assert snapshot(live) == before
        act(handle, ALLY, intent_type="dash", action_grant=(newer.id, newer.origin))
        assert combatant(live, ALLY).action_available
        act(handle, ALLY, intent_type="pass")
        monster_turn(handle)
        assert orch._current_actor(live).entity_id == HERO
        act(handle, HERO, intent_type="drop_concentration")
        assert len(lethargies(live)) == 1
        assert buff(live).origin == newer.origin
        assert live.concentration_chain[OTHER] == [(ALLY, newer.id, newer.origin)]
        act(handle, HERO, intent_type="pass")
        act(handle, OTHER, intent_type="drop_concentration")
        assert len(lethargies(live)) == 2
        assert not live.concentration_chain
        act(handle, OTHER, intent_type="pass")
        assert orch._current_actor(live).entity_id == ALLY
        assert effective_speed(combatant(live, ALLY), "walk", live) == 0
        act(handle, ALLY, intent_type="pass")
        assert not lethargies(live)
        assert not live.active_effects[ALLY]
        assert "incapacitated" not in live.active_conditions[ALLY]
        return snapshot(live)

    assert execute() == execute()
