"""Public acceptance for reviewed per-target spell effect choices."""

import random

import pytest
from dnd5e_srd_data import BundledAssetLoader

from dnd5e_engine import orchestrator as orch
from dnd5e_engine.events import CastFailed, CheckRolled, EffectApplied
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from tests.c21_support import act, combatant, events, foe, pc, start
from tests.test_execution_integrity import snapshot
from tests.test_typed_check_pipeline import CountingRandom

HERO = "char:hero"
ALLY = "char:ally"
SPELLS = ("guidance", "enhance-ability", "protection-from-energy")


@pytest.fixture(autouse=True)
def loader():
    bundled = BundledAssetLoader()
    set_lib_loader_for_tests(bundled)
    yield bundled
    set_lib_loader_for_tests(None)


def table():
    return start(
        [
            pc(
                class_slug="cleric",
                character_level=7,
                wisdom=18,
                spells_known=[*SPELLS, "fire-bolt", "burning-hands"],
                spell_slots={1: 3, 2: 3, 3: 3, 4: 2},
            ),
            pc(
                ALLY,
                initiative=19,
                zone_id="1,0",
                ac=1,
                equipment=("shortbow",),
                class_slug="cleric",
                spells_known=list(SPELLS),
                spell_slots={2: 3, 3: 3},
            ),
        ],
        seed=7,
        encounter=[foe(zone_id="5,5")],
    )


def choice(loader, slug, target=HERO, index=0):
    spell = loader.get_spell(slug)
    return {
        "spell_id": slug,
        "activity_id": spell.activities[0].id,
        "target_id": target,
        "effect_id": spell.passive_effects[index].id,
    }


@pytest.mark.parametrize("slug", SPELLS)
def test_canonical_declares_one_effect_per_target(loader, slug):
    spell = loader.get_spell(slug)
    assert spell.activities[0].effect_selection == "one_per_target"


@pytest.mark.parametrize("slug", SPELLS)
def test_public_cast_applies_only_selected_canonical_effect(loader, slug):
    handle, live = table()
    spell = loader.get_spell(slug)
    before_rng = live.rng.getstate()
    act(
        handle,
        HERO,
        intent_type="cast_spell",
        spell_id=slug,
        target_id=ALLY,
        slot_level=spell.level,
        effect_selections=[choice(loader, slug, ALLY)],
        willing_target_ids=[ALLY],
    )
    assert not events(live, CastFailed)
    applied = events(live, EffectApplied)
    assert len(applied) == 1
    assert applied[0].effect.target_id == ALLY
    assert applied[0].effect.changes[0].key == spell.passive_effects[0].changes[0].key
    assert live.rng.getstate() == before_rng
    assert not combatant(live).action_available
    assert live.concentration_chain[HERO] == [
        (ALLY, applied[0].effect.id, applied[0].effect.origin)
    ]


@pytest.mark.parametrize("index", range(18))
def test_all_guidance_canonical_skill_candidates_reach_public_check_consumer(loader, index):
    from dnd5e_engine.rules.skill_bonuses import SKILL_CHECK_CHANGE_KEYS

    handle, live = table()
    effect = loader.get_spell("guidance").passive_effects[index]
    skill = SKILL_CHECK_CHANGE_KEYS[effect.changes[0].key]
    cast(handle, loader, "guidance", indices=(index,))
    live.rng = CountingRandom(13)
    act(
        handle,
        ALLY,
        intent_type="check",
        check={"actor_id": ALLY, "ability": "wis", "skill": skill},
    )
    assert [d[:2] for d in live.rng.draws] == [(1, 20), (1, 4)]
    assert events(live, CheckRolled)[-1].modifier == live.rng.draws[-1][2]


@pytest.mark.parametrize(
    "index,damage_type", list(enumerate(("acid", "cold", "fire", "lightning", "thunder")))
)
@pytest.mark.parametrize("defense", ["none", "resistance", "immunity"])
def test_energy_choice_uses_existing_damage_projection_without_stacking(
    loader, index, damage_type, defense
):
    from dnd5e_engine.activities.apply import apply_damage
    from dnd5e_engine.activities.build_context import build_activity_context
    from dnd5e_engine.events import DamageApplied

    handle, live = table()
    cast(handle, loader, "protection-from-energy", indices=(index,))
    field = "damage_resistances" if defense == "resistance" else "damage_immunities"
    if defense != "none":
        orch._update_combatant(live, ALLY, **{field: [damage_type]})
    target = combatant(live, ALLY)
    payload = orch._build_hydration_payload(live, caster=combatant(live))
    ctx = build_activity_context(
        combatant(live),
        [target],
        rng=live.rng,
        event_emitter=lambda e: orch._emit(live, e),
        source_passive_effects=[],
        slot_level=None,
        base_spell_level=None,
        spellcasting_ability=None,
        concentration=False,
        spell_book={},
        save_modifiers=payload["save_modifiers"],
        passive_damage_modifiers=payload["passive_damage_modifiers"],
    )
    before = live.rng.getstate()
    assert apply_damage(target, {damage_type: 11, "force": 11}, ctx) == (
        11 if defense == "immunity" else 16
    )
    assert [e.amount for e in events(live, DamageApplied)[-2:]] == [
        0 if defense == "immunity" else 5,
        11,
    ]
    assert live.rng.getstate() == before


def test_energy_resistance_changes_real_public_fire_bolt_damage(loader):
    from dnd5e_engine.events import AttackRolled, DamageApplied

    def run(protected):
        handle, live = table()
        if protected:
            cast(handle, loader, "protection-from-energy", indices=(2,))
            take_turn(live)
        before = live.rng.getstate()
        act(handle, HERO, intent_type="cast_spell", spell_id="fire-bolt", target_id=ALLY)
        assert not events(live, CastFailed)
        assert events(live, AttackRolled)[-1].is_hit
        damage = events(live, DamageApplied)[-1]
        assert damage.damage_type == "fire"
        assert live.rng.getstate() != before
        return damage.amount, live.rng.getstate()

    raw, rng = run(False)
    resisted, protected_rng = run(True)
    assert resisted == raw // 2
    assert rng == protected_rng


@pytest.mark.parametrize("slug,index", [("guidance", 0), ("enhance-ability", 1)])
def test_check_effects_do_not_modify_attack_or_saving_throw(loader, slug, index):
    from dnd5e_engine.events import AttackRolled, SaveRolled

    handle, live = table()
    cast(handle, loader, slug, indices=(index,))
    live.rng = CountingRandom(13)
    act(handle, ALLY, intent_type="attack", weapon_id="shortbow", target_id="mon:foe")
    attack = events(live, AttackRolled)[-1]
    assert attack.advantage == "normal"
    assert (1, 4) not in [d[:2] for d in live.rng.draws]
    take_turn(live)
    act(
        handle,
        HERO,
        intent_type="cast_spell",
        spell_id="burning-hands",
        slot_level=1,
        direction=(1, 0),
        target_id=ALLY,
    )
    save = next(e for e in events(live, SaveRolled) if e.target_id == ALLY)
    assert save.advantage == "normal"


@pytest.mark.parametrize(
    "slug,rounds", [("guidance", 10), ("enhance-ability", 600), ("protection-from-energy", 600)]
)
def test_duration_is_caster_owned_and_expires_through_public_turn(loader, slug, rounds):
    from dnd5e_engine.events import ConcentrationDropped, EffectExpired

    handle, live = table()
    cast(handle, loader, slug)
    assert live.concentration_rounds_remaining[HERO] == rounds - 1
    act(handle, ALLY, intent_type="pass")
    assert live.concentration_rounds_remaining[HERO] == rounds - 1
    # Seat the final source boundary; target turns cannot shorten this cap.
    live.concentration_rounds_remaining[HERO] = 1
    take_turn(live)
    before = live.rng.getstate()
    act(handle, HERO, intent_type="pass")
    assert not live.active_effects.get(ALLY)
    assert HERO not in live.concentration_chain
    assert events(live, ConcentrationDropped)[-1].target_id == HERO
    assert events(live, EffectExpired)[-1].reason == "duration"
    assert live.rng.getstate() == before


def test_concentration_replacement_expires_every_selected_target(loader):
    from dnd5e_engine.events import EffectExpired

    handle, live = table()
    cast(handle, loader, "enhance-ability", targets=(HERO, ALLY), indices=(0, 1), level=3)
    old = tuple(live.concentration_chain[HERO])
    take_turn(live)
    cast(handle, loader, "guidance", targets=(HERO,))
    assert all(
        not any(
            (e.target_id, e.id, e.origin) == identity
            for effects in live.active_effects.values()
            for e in effects
        )
        for identity in old
    )
    assert {(e.target_id, e.effect_id, e.origin) for e in events(live, EffectExpired)} >= set(old)
    assert len(live.concentration_chain[HERO]) == 1


def test_guidance_draws_only_on_matching_skill_check(loader):
    handle, live = table()
    spell = loader.get_spell("guidance")
    index = next(
        i
        for i, e in enumerate(spell.passive_effects)
        if e.changes[0].key == "system.skills.prc.bonuses.check"
    )
    live.rng = CountingRandom(7)
    act(
        handle,
        HERO,
        intent_type="cast_spell",
        spell_id="guidance",
        target_id=ALLY,
        effect_selections=[choice(loader, "guidance", ALLY, index)],
        willing_target_ids=[ALLY],
    )
    assert live.rng.draws == []
    act(
        handle,
        ALLY,
        intent_type="check",
        check={"actor_id": ALLY, "ability": "wis", "skill": "perception", "dc": 15},
    )
    assert [d[:2] for d in live.rng.draws] == [(1, 20), (1, 4)]
    event = events(live, CheckRolled)[-1]
    assert event.modifier == live.rng.draws[1][2]


def take_turn(live, actor=HERO):
    """Seat the indicated actor at a fresh turn for isolated ingress scenarios."""
    live.current_turn_index = next(i for i, c in enumerate(live.initiative) if c.entity_id == actor)
    orch._update_combatant(live, actor, action_available=True, bonus_action_available=True)


def state(live):
    fields, rng, _, hooks = snapshot(live)
    fields.pop("event_log")
    return fields, rng, hooks


def cast(handle, loader, slug, *, actor=HERO, targets=(ALLY,), indices=(0,), level=None, **kwargs):
    act(
        handle,
        actor,
        intent_type="cast_spell",
        spell_id=slug,
        target_ids=targets,
        slot_level=loader.get_spell(slug).level if level is None else level,
        effect_selections=[
            choice(loader, slug, target, index)
            for target, index in zip(targets, indices, strict=True)
        ],
        willing_target_ids=targets,
        **kwargs,
    )


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "unknown",
        "duplicate",
        "two_effects",
        "foreign_spell",
        "foreign_activity",
        "foreign_target",
        "missing_target",
        "repeat_target",
        "too_many",
        "out_of_range",
        "unwilling",
        "duplicate_willing",
    ],
)
def test_invalid_choices_refuse_before_all_payment_concentration_and_rng(loader, case):
    handle, live = table()
    # An existing concentration must survive even an invalid replacement cast.
    cast(handle, loader, "guidance", targets=(HERO,))
    take_turn(live)
    selections = [choice(loader, "enhance-ability", HERO)]
    args = dict(
        spell_id="enhance-ability",
        target_ids=[HERO],
        slot_level=2,
        effect_selections=selections,
        willing_target_ids=[HERO],
    )
    if case == "missing":
        args["effect_selections"] = []
    elif case == "unknown":
        selections[0]["effect_id"] = "not-a-canonical-effect"
    elif case == "duplicate":
        selections.append(selections[0].copy())
    elif case == "two_effects":
        selections.append(choice(loader, "enhance-ability", HERO, 1))
    elif case == "foreign_spell":
        selections[0]["spell_id"] = "guidance"
    elif case == "foreign_activity":
        selections[0]["activity_id"] = "other-activity"
    elif case == "foreign_target":
        selections[0]["target_id"] = "missing-creature"
    elif case == "missing_target":
        args.update(target_ids=[HERO, ALLY], slot_level=3)
    elif case == "repeat_target":
        args.update(target_ids=[HERO, HERO], slot_level=3)
    elif case == "too_many":
        args.update(target_ids=[HERO, ALLY])
        selections.append(choice(loader, "enhance-ability", ALLY))
    elif case == "out_of_range":
        args["target_ids"] = ["mon:foe"]
        selections[0]["target_id"] = "mon:foe"
    else:
        args.update(
            spell_id="guidance",
            effect_selections=[choice(loader, "guidance", HERO)],
            slot_level=0,
            willing_target_ids=[] if case == "unwilling" else [HERO, HERO],
        )
    before = state(live)
    offset = len(live.event_log)
    act(handle, HERO, intent_type="cast_spell", **args)
    assert state(live) == before
    assert len(live.event_log[offset:]) == 1
    assert isinstance(live.event_log[-1], CastFailed)
    assert live.event_log[-1].reason == "target_invalid"


@pytest.mark.parametrize("index,ability", list(enumerate(("str", "dex", "int", "wis", "cha"))))
def test_enhance_ability_advantage_matches_only_selected_ability_checks(loader, index, ability):
    handle, live = table()
    cast(handle, loader, "enhance-ability", indices=(index,))
    live.rng = CountingRandom(9)
    for tested in ("str", "dex", "con", "int", "wis", "cha"):
        take_turn(live, ALLY)
        act(
            handle, ALLY, intent_type="check", check={"actor_id": ALLY, "ability": tested, "dc": 10}
        )
        event = events(live, CheckRolled)[-1]
        assert event.advantage == ("advantage" if tested == ability else "normal")
    assert len(live.rng.draws) == 7


def test_upcast_targets_choose_independent_abilities_and_share_concentration(loader):
    handle, live = table()
    cast(handle, loader, "enhance-ability", targets=(HERO, ALLY), indices=(0, 3), level=3)
    assert not events(live, CastFailed)
    effects = events(live, EffectApplied)
    assert [(e.effect.target_id, e.effect.changes[0].key) for e in effects] == [
        (HERO, "system.abilities.str.check.roll.mode"),
        (ALLY, "system.abilities.wis.check.roll.mode"),
    ]
    assert len(live.concentration_chain[HERO]) == 2
    assert live.spell_slots_by_entity[HERO][3] == 2
    for target, ability in ((HERO, "str"), (HERO, "wis"), (ALLY, "str"), (ALLY, "wis")):
        take_turn(live, target)
        act(handle, target, intent_type="check", check={"actor_id": target, "ability": ability})
        assert events(live, CheckRolled)[-1].advantage == (
            "advantage" if (target, ability) in ((HERO, "str"), (ALLY, "wis")) else "normal"
        )


@pytest.mark.parametrize("skill", [None, "insight", "perception"])
def test_guidance_only_matching_skill_rolls_a_bonus_and_replays(loader, skill):
    def run():
        handle, live = table()
        index = next(
            i
            for i, e in enumerate(loader.get_spell("guidance").passive_effects)
            if e.changes[0].key == "system.skills.prc.bonuses.check"
        )
        cast(handle, loader, "guidance", indices=(index,))
        oracle = random.Random()
        oracle.setstate(live.rng.getstate())
        natural = oracle.randint(1, 20)
        bonus = oracle.randint(1, 4) if skill == "perception" else 0
        act(
            handle,
            ALLY,
            intent_type="check",
            check={"actor_id": ALLY, "ability": "wis", "skill": skill},
        )
        event = events(live, CheckRolled)[-1]
        assert (event.natural, event.modifier, event.roll_total) == (
            natural,
            bonus,
            natural + bonus,
        )
        assert live.rng.getstate() == oracle.getstate()
        return snapshot(live)

    assert run() == run()


@pytest.mark.parametrize("slug", SPELLS)
def test_choice_spell_exception_after_application_restores_complete_state(
    loader, monkeypatch, slug
):
    from dnd5e_engine import live_spell_delivery as delivery
    from dnd5e_engine import timed_activities as timed

    handle, live = table()
    cast(handle, loader, "guidance", targets=(HERO,))
    take_turn(live)
    before = snapshot(live)
    observed = []
    live.event_listeners.append(observed.append)
    original = delivery.resolve_activity

    def broken(activity, ctx):
        original(activity, ctx)
        assert not combatant(live).action_available
        ctx.rng.randint(1, 20)
        raise RuntimeError("choice effect fault after payment and attachment")

    with monkeypatch.context() as patch:
        patch.setattr(delivery, "resolve_activity", broken)
        patch.setattr(timed, "resolve_activity", broken)
        with pytest.raises(RuntimeError, match="choice effect fault"):
            cast(handle, loader, slug)
    assert snapshot(live) == before
    assert observed == []
    cast(handle, loader, slug)
    assert not events(live, CastFailed)


@pytest.mark.parametrize("slug", SPELLS)
def test_multiple_casters_keep_independent_ownership_and_older_effect_resumes(loader, slug):
    from dnd5e_engine.rules.effects import effective_effects

    handle, live = table()
    cast(handle, loader, slug, targets=(HERO,))
    cast(handle, loader, slug, actor=ALLY, targets=(HERO,), indices=(1,))
    assert not events(live, CastFailed)
    older, newer = live.active_effects[HERO]
    assert len(live.concentration_chain[HERO]) == len(live.concentration_chain[ALLY]) == 1
    assert effective_effects(live.active_effects[HERO]) == (newer,)

    def consume(expected_index):
        if slug == "protection-from-energy":
            projection = orch._build_hydration_payload(live, caster=combatant(live))
            assert projection["passive_damage_modifiers"][HERO]["resistances"] == [
                "acid" if expected_index == 0 else "cold"
            ]
        else:
            take_turn(live)
            live.rng = CountingRandom(11)
            request = {"actor_id": HERO, "ability": "str" if slug == "enhance-ability" else "dex"}
            if slug == "guidance":
                request["skill"] = "acrobatics"
            act(handle, HERO, intent_type="check", check=request)
            assert len(live.rng.draws) == (2 if expected_index == 0 else 1)

    consume(1)
    take_turn(live, ALLY)
    replacement = "enhance-ability" if slug == "guidance" else "guidance"
    cast(handle, loader, replacement, actor=ALLY, targets=(ALLY,))
    assert effective_effects(live.active_effects[HERO]) == (older,)
    assert HERO in live.concentration_chain
    consume(0)


@pytest.mark.parametrize("slug", SPELLS)
def test_counterspell_interrupts_valid_choice_with_existing_cost_semantics_and_replay(loader, slug):
    from dnd5e_engine.events import SaveRolled, SpellCast
    from tests.test_reaction_runtime import REACTOR, arm, reactor

    def run():
        handle, live = start(
            [
                reactor(),
                pc(
                    class_slug="cleric",
                    character_level=7,
                    wisdom=18,
                    constitution=-30,
                    zone_id="0,1",
                    spells_known=list(SPELLS),
                    spell_slots={2: 2, 3: 2},
                ),
                pc(ALLY, initiative=19, zone_id="1,1"),
            ],
            seed=7,
            encounter=[foe(zone_id="5,5")],
        )
        arm(handle, REACTOR, "counterspell")
        slots = dict(live.spell_slots_by_entity[HERO])
        cast(handle, loader, slug)
        assert events(live, CastFailed)[-1].reason == "countered"
        assert not events(live, EffectApplied)
        assert HERO not in live.concentration_chain
        assert live.spell_slots_by_entity[HERO] == slots
        assert not combatant(live).action_available
        assert not combatant(live, REACTOR).reaction_available
        assert live.spell_slots_by_entity[REACTOR][3] == 1
        assert not events(live, SaveRolled)[-1].succeeded
        assert [e.spell_id for e in events(live, SpellCast)] == ["counterspell"]
        return snapshot(live)

    assert run() == run()


@pytest.mark.parametrize("valid", [False, True])
def test_delegated_choice_cannot_bypass_selection_or_item_concentration_boundary(loader, valid):
    from dnd5e_srd_data import MemoryAssetLoader

    item = loader.get_item("wand-of-fireballs")
    child = loader.get_spell("guidance")
    wrapper = item.activities[0]
    wrapper = wrapper.model_copy(
        update={"spell": wrapper.spell.model_copy(update={"uuid": child.foundry_uuid, "level": 0})}
    )
    set_lib_loader_for_tests(
        MemoryAssetLoader(spells=[child], items=[item.model_copy(update={"activities": [wrapper]})])
    )
    handle, live = start([pc(equipment=(item.slug,))], seed=4)
    before = state(live)
    act(
        handle,
        HERO,
        intent_type="use_item",
        item_id=item.slug,
        target_id=HERO,
        effect_selections=[choice(loader, "guidance", HERO)] if valid else [],
        willing_target_ids=[HERO],
    )
    assert state(live) == before
    assert events(live, CastFailed)[-1].reason == (
        "unsupported_area" if valid else "target_invalid"
    )
    assert not events(live, EffectApplied)


def test_same_identity_modified_selection_asset_still_fails_admission(loader):
    from dnd5e_srd_data import MemoryAssetLoader

    spell = loader.get_spell("guidance")
    activity = spell.activities[0].model_copy(update={"effect_selection": None})
    set_lib_loader_for_tests(
        MemoryAssetLoader(spells=[spell.model_copy(update={"activities": [activity]})])
    )
    handle, live = table()
    before = state(live)
    cast(handle, loader, "guidance")
    assert events(live, CastFailed)[-1].execution_failure.code == "unreviewed_spell"
    assert state(live) == before


@pytest.mark.parametrize("level", [2, 3, 9])
def test_enhance_ability_canonical_slot_target_cap_through_public_cast(loader, level):
    targets = tuple(f"char:target-{i}" for i in range(level - 1))
    cells = [(x, y) for x in range(3, 6) for y in range(3, 6) if (x, y) != (4, 4)]
    handle, live = start(
        [
            pc(
                zone_id="4,4",
                initiative=100,
                class_slug="cleric",
                character_level=20,
                spells_known=["enhance-ability"],
                spell_slots={level: 1},
            ),
            *(
                pc(target, zone_id=f"{x},{y}")
                for target, (x, y) in zip(targets, cells, strict=False)
            ),
        ],
        seed=7,
        encounter=[foe(zone_id="9,9")],
    )
    indices = tuple(i % 5 for i in range(len(targets)))
    cast(handle, loader, "enhance-ability", targets=targets, indices=indices, level=level)
    assert not events(live, CastFailed)
    assert len(events(live, EffectApplied)) == len(targets)
    assert len(live.concentration_chain[HERO]) == len(targets)
    assert live.spell_slots_by_entity[HERO][level] == 0


def test_same_guidance_from_two_casters_never_adds_two_bonus_dice(loader):
    handle, live = table()
    cast(handle, loader, "guidance", targets=(HERO,))
    cast(handle, loader, "guidance", actor=ALLY, targets=(HERO,))
    assert len(live.active_effects[HERO]) == 2
    live.rng = CountingRandom(13)
    for _ in range(2):
        take_turn(live)
        act(
            handle,
            HERO,
            intent_type="check",
            check={"actor_id": HERO, "ability": "dex", "skill": "acrobatics"},
        )
    assert [d[:2] for d in live.rng.draws] == [(1, 20), (1, 4), (1, 20), (1, 4)]


def test_guidance_check_consumer_fault_restores_bonus_rng_action_and_events(loader, monkeypatch):
    from dnd5e_engine import live_checks

    handle, live = table()
    cast(handle, loader, "guidance")
    before = snapshot(live)
    original = live_checks.resolve_check_request

    def broken(*args, **kwargs):
        original(*args, **kwargs)
        assert live.rng.getstate() != before[1]
        raise RuntimeError("fault after guidance check resolution")

    monkeypatch.setattr(live_checks, "resolve_check_request", broken)
    with pytest.raises(RuntimeError, match="fault after guidance"):
        act(
            handle,
            ALLY,
            intent_type="check",
            check={"actor_id": ALLY, "ability": "dex", "skill": "acrobatics"},
        )
    assert snapshot(live) == before


@pytest.mark.parametrize("slug", SPELLS)
def test_monster_ai_never_synthesizes_missing_spell_effect_choices(loader, slug):
    import asyncio

    from dnd5e_srd_data import MemoryAssetLoader

    from dnd5e_engine.events import SpellCast
    from tests.test_monster_spell_delivery import _spell_setup

    live, actor, _, monster, _, spell = _spell_setup(loader, slug, enemies=((6, 5),))
    live.ruleset_loader = MemoryAssetLoader(monsters=[monster], spells=[spell])
    before = dict(live.custom_counters_by_entity.get(actor.entity_id, {}))
    asyncio.run(orch.advance_monster_turn(orch.CombatHandle(live.handle_id)))
    assert not events(live, SpellCast)
    assert not events(live, EffectApplied)
    assert live.custom_counters_by_entity.get(actor.entity_id, {}) == before
