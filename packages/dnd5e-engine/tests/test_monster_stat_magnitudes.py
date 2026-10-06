"""Ordinary templates use the same numeric resolver as forms and summons."""

import asyncio
import copy
import random

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd5e_srd_data.schema.common import AttackActivity, CheckActivity

from dnd5e_engine import EncounterMemberSpec, GridScene, PartyMemberSpec, start_combat
from dnd5e_engine.activities.actor_stats import check_modifier
from dnd5e_engine.activities.attack import _stat_block_damage_parts, resolve_attack
from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.d20 import AdvantageSources
from dnd5e_engine.activities.formula import resolve_roll_data
from dnd5e_engine.events import AttackRolled, DamageApplied
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.orchestrator import (
    _get_live,
    _initiative_dexterity,
    _initiative_modifier,
    _resolve_initiative,
    _resolve_monster_attack_activities,
    _stat_block_magnitudes_of,
)
from dnd5e_engine.types.effects import ActiveEffect


@pytest.fixture(autouse=True)
def loader():
    bundled = BundledAssetLoader()
    set_lib_loader_for_tests(bundled)
    yield bundled
    set_lib_loader_for_tests(None)


def spec(slug="tough", **fields):
    defaults = dict(
        entity_id="mon:foe",
        entity_type="Monster",
        name="Foe",
        initiative=20,
        hp_current=1000,
        hp_max=1000,
        zone_id="1,0",
        monster_template_slug=slug,
    )
    return EncounterMemberSpec(**(defaults | fields))


def start(foe, *, seed=11, effects=()):
    hero = PartyMemberSpec(
        entity_id="char:hero",
        name="Hero",
        initiative=1,
        hp_current=10000,
        hp_max=10000,
        ac=1,
        zone_id="0,0",
    )
    before = copy.deepcopy((hero, foe))
    result = asyncio.run(
        start_combat(
            session_id="monster-magnitudes",
            party=[hero],
            encounter=[foe],
            grid_scene=GridScene(width=20, height=5),
            rng_seed=seed,
            active_effects=effects,
        )
    )
    assert (hero, foe) == before
    live = _get_live(result.handle)
    actor = next(c for c in live.initiative if c.entity_id == foe.entity_id)
    target = next(c for c in live.initiative if c.entity_id == hero.entity_id)
    return live, actor, target


def attack(monster, action_slug):
    return next(
        a
        for a in next(a for a in monster.actions if a.slug == action_slug).activities
        if isinstance(a, AttackActivity)
    )


@pytest.mark.parametrize(
    ("slug", "action_slug", "bonus", "dice", "size", "mod"),
    [
        ("tough", "mace", 4, 1, 6, 2),
        ("wolf", "bite", 4, 1, 6, 2),
        ("crocodile", "bite", 4, 1, 8, 2),
        ("tough", "heavy-crossbow", 3, 1, 10, 1),
        ("adult-red-dragon", "rend", 14, 1, 10, 8),
    ],
)
def test_real_template_attack_and_base_damage(loader, slug, action_slug, bonus, dice, size, mod):
    monster = loader.get_monster(slug)
    before = monster.model_dump()
    live, actor, target = start(spec(slug))
    assert actor.attack_bonus is None
    _resolve_monster_attack_activities(live, actor, [target], [attack(monster, action_slug)])
    roll = next(e for e in live.event_log if isinstance(e, AttackRolled))
    damage = next(e for e in live.event_log if isinstance(e, DamageApplied))
    rng = random.Random(11)
    assert (roll.natural, roll.modifier, roll.roll_total) == (rng.randint(1, 20), bonus, 15 + bonus)
    assert damage.amount == sum(rng.randint(1, size) for _ in range(dice)) + mod
    if slug == "adult-red-dragon":
        fire = next(
            e for e in live.event_log if isinstance(e, DamageApplied) and e.damage_type == "fire"
        )
        assert fire.amount == rng.randint(1, 4) + rng.randint(1, 4)  # rider gets no STR.
    assert monster.model_dump() == before
    if slug == "crocodile":
        escape = next(a for a in monster.actions[0].activities if isinstance(a, CheckActivity))
        assert escape.check.dc.formula == "@skills.ath.passive"
        assert resolve_roll_data(escape.check.dc.formula, context(live, actor, target)) == "12"


@pytest.mark.parametrize("override", [0, 9, 99])
def test_host_override_only_pins_to_hit(loader, override):
    live, actor, target = start(spec(attack_bonus=override))
    _resolve_monster_attack_activities(
        live, actor, [target], [attack(loader.get_monster("tough"), "mace")]
    )
    roll = next(e for e in live.event_log if isinstance(e, AttackRolled))
    damage = next(e for e in live.event_log if isinstance(e, DamageApplied))
    assert actor.attack_bonus == roll.modifier == override
    assert damage.amount == 7  # d6 5 + canonical STR 2, independent of override.


def test_legacy_and_unresolved_template_magnitudes():
    live, actor, _ = start(spec(None))
    assert actor.attack_bonus == 0
    assert _stat_block_magnitudes_of(live, actor) is None
    live, actor, _ = start(spec("missing-template"))
    assert _stat_block_magnitudes_of(live, actor) is None


def context(live, actor, target, **fields):
    return build_activity_context(
        actor,
        [target],
        rng=live.rng,
        event_emitter=lambda e: live.event_log.append(e),
        slot_level=fields.pop("slot_level", None),
        base_spell_level=fields.pop("base_spell_level", None),
        spellcasting_ability=fields.pop("spellcasting_ability", None),
        concentration=False,
        source_passive_effects=[],
        spell_book={},
        passive_damage_modifiers={},
        save_modifiers={},
        stat_block_magnitudes=_stat_block_magnitudes_of(live, actor),
        **fields,
    )


def test_real_roll_data_and_expertise(loader):
    live, actor, target = start(spec("wolf", attack_bonus=99))
    ctx = context(live, actor, target)
    assert (
        resolve_roll_data("@mod + @prof + @abilities.int.mod", ctx, ability="str") == "2 + 2 + -4"
    )
    assert resolve_roll_data("@skills.prc.passive", ctx) == "15"
    assert check_modifier(actor, "wis", "perception").total == 5
    live, actor, target = start(spec("aboleth"))
    assert resolve_roll_data("@skills.his.passive", context(live, actor, target)) == "22"


@pytest.mark.parametrize(
    ("ability", "bonus", "flat", "expected"),
    [("dex", "", False, 3), ("str", "2", False, 6), ("str", "17", True, 17)],
)
def test_declared_attack_ability_bonus_and_flat_semantics(loader, ability, bonus, flat, expected):
    live, actor, target = start(spec())
    mace = attack(loader.get_monster("tough"), "mace")
    altered = mace.model_copy(
        update={
            "attack": mace.attack.model_copy(
                update={"ability": ability, "bonus": bonus, "flat": flat}
            )
        }
    )
    _resolve_monster_attack_activities(live, actor, [target], [altered])
    roll = next(e for e in live.event_log if isinstance(e, AttackRolled))
    assert roll.modifier == expected


def test_spell_numbers_stay_separate_from_rend_and_host_override(loader):
    live, actor, target = start(spec("adult-red-dragon", attack_bonus=99))
    weapon_ctx = context(live, actor, target, spellcasting_ability="cha")
    assert weapon_ctx.attack_bonus_override == 99
    assert weapon_ctx.save_dc_override is None
    ctx = context(live, actor, target, spellcasting_ability="cha", base_spell_level=0)
    assert (ctx.attack_bonus_override, ctx.save_dc_override) == (12, 20)
    assert resolve_roll_data("@mod + @prof", ctx, ability="cha") == "6 + 6"
    fire_bolt = next(
        a for a in loader.get_spell("fire-bolt").activities if isinstance(a, AttackActivity)
    )
    resolve_attack(fire_bolt, ctx)
    roll = next(e for e in live.event_log if isinstance(e, AttackRolled))
    damage = next(e for e in live.event_log if isinstance(e, DamageApplied))
    assert roll.modifier == 12
    rng = random.Random(11)
    rng.randint(1, 20)
    assert damage.amount == rng.randint(1, 10)  # no CHA damage modifier.


@pytest.mark.parametrize("override", [None, 9])
def test_unresolved_monster_spellcasting_keeps_legacy_fallback(loader, override):
    # Mummy Lord's ability remains prose-only (existing data BACKLOG).
    assert loader.get_monster("mummy-lord").spellcasting_ability is None
    live, actor, target = start(
        spec("mummy-lord", **({} if override is None else {"attack_bonus": override}))
    )
    ctx = context(live, actor, target, base_spell_level=1)
    bonus = override if override is not None else 0
    assert (ctx.attack_bonus_override, ctx.save_dc_override) == (bonus, 8 + bonus)


def test_implicit_modifier_guards_and_critical(loader):
    live, actor, target = start(spec())
    activity = attack(loader.get_monster("tough"), "mace")
    ctx = context(live, actor, target)
    part = activity.damage.parts[0]
    for changed in (
        part.model_copy(update={"bonus": "@mod"}),
        part.model_copy(update={"number": 0, "denomination": None, "bonus": "5"}),
        part.model_copy(
            update={
                "custom": part.custom.model_copy(update={"enabled": True, "formula": "1d6 + @mod"})
            }
        ),
    ):
        altered = activity.model_copy(
            update={"damage": activity.damage.model_copy(update={"parts": [changed]})}
        )
        assert _stat_block_damage_parts(altered, ctx, None) == [changed]
    rider = activity.model_copy(
        update={"damage": activity.damage.model_copy(update={"include_base": False})}
    )
    assert _stat_block_damage_parts(rider, ctx, None) == [part]
    weapon = loader.get_item("mace")
    assert _stat_block_damage_parts(activity, ctx, weapon) == [part]
    assert _stat_block_damage_parts(
        activity, context(live, actor, target, base_spell_level=1), None
    ) == [part]
    live, actor, target = start(spec(), seed=5)
    _resolve_monster_attack_activities(live, actor, [target], [activity])
    rng = random.Random(5)
    assert rng.randint(1, 20) == 20
    damage = next(e for e in live.event_log if isinstance(e, DamageApplied))
    assert damage.is_crit
    assert damage.amount == rng.randint(1, 6) + rng.randint(1, 6) + 2


@pytest.mark.parametrize("dexterity", [None, 10, 18])
def test_dexterity_presence_controls_live_score_and_fallback(loader, dexterity):
    foe = spec("wolf", **({} if dexterity is None else {"dexterity": dexterity}))
    live, actor, _ = start(foe)
    expected = 15 if dexterity is None else dexterity
    assert actor.dexterity == _initiative_dexterity(foe) == expected
    assert _stat_block_magnitudes_of(live, actor).ability_scores["dex"] == expected
    old = loader.get_monster("wolf").model_copy(update={"initiative_modifier": None})
    set_lib_loader_for_tests(MemoryAssetLoader(monsters=[old]))
    assert _initiative_modifier(foe) == (expected - 10) // 2


@pytest.mark.parametrize(
    ("slug", "modifier"), [("tough", 1), ("aboleth", 7), ("adult-red-dragon", 12)]
)
def test_real_rolled_initiative(loader, slug, modifier):
    live, actor, _ = start(spec(slug, initiative=None))
    assert actor.initiative == random.Random(11).randint(1, 20) + modifier
    rng = random.Random(11)
    rng.randint(1, 20)
    assert live.rng.getstate() == rng.getstate()


@pytest.mark.parametrize(
    ("adv", "dis", "surprised", "count"),
    [
        (False, False, False, 1),
        (True, False, False, 2),
        (False, True, False, 2),
        (False, False, True, 2),
        (True, True, False, 1),
        (True, False, True, 1),
    ],
)
def test_modifier_preserves_draw_count_order_and_cancellation(adv, dis, surprised, count):
    foe = spec("aboleth", initiative=None, is_surprised=surprised)
    sources = {
        foe.entity_id: AdvantageSources(
            advantage=("condition:attacker",) if adv else (),
            disadvantage=("condition:attacker",) if dis else (),
        )
    }
    rng = random.Random(13)
    expected = random.Random(13)
    dice = [expected.randint(1, 20) for _ in range(count)]
    natural = (max(dice) if adv else min(dice)) if count == 2 else dice[0]
    assert _resolve_initiative(foe, rng, sources) == natural + 7
    assert rng.getstate() == expected.getstate()
    assert (
        _resolve_initiative(spec("tough", initiative=None), rng, {}) == expected.randint(1, 20) + 1
    )
    assert rng.getstate() == expected.getstate()


@pytest.mark.parametrize("modifier", [0, 9])
def test_explicit_modifier_and_fixed_initiative(modifier):
    foe = spec("aboleth", initiative=None, initiative_modifier=modifier, dexterity=18)
    assert _resolve_initiative(foe, random.Random(11), {}) == 15 + modifier
    fixed = spec("aboleth", initiative=0, initiative_modifier=99, is_surprised=True)
    rng = random.Random(11)
    before = rng.getstate()
    assert (
        _resolve_initiative(
            fixed, rng, {fixed.entity_id: AdvantageSources(advantage=("condition:attacker",))}
        )
        == 0
    )
    assert rng.getstate() == before


@pytest.mark.parametrize(
    ("statuses", "surprised", "count", "pick"),
    [
        ({"invisible"}, False, 2, max),
        ({"incapacitated"}, False, 2, min),
        ({"invisible", "incapacitated"}, False, 1, max),
        ({"invisible"}, True, 1, max),
    ],
)
def test_seeded_canonical_conditions_keep_shared_initiative_primitive(
    statuses, surprised, count, pick
):
    effect = ActiveEffect(
        id="effect:initiative",
        name="Initiative",
        origin="test:init",
        target_id="mon:foe",
        statuses=statuses,
    )
    live, actor, _ = start(
        spec("aboleth", initiative=None, is_surprised=surprised), effects=(effect,)
    )
    rng = random.Random(11)
    assert actor.initiative == pick([rng.randint(1, 20) for _ in range(count)]) + 7
    assert live.rng.getstate() == rng.getstate()
