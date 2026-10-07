"""Authoritative attack rider phases, one-use rolls and Sneak Attack budgets."""

from __future__ import annotations

import random
from dataclasses import replace

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.common import AttackActivity, AttackDamageBlock, DamagePartBlock

from dnd5e_engine.activities.attack import resolve_attack
from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.context import (
    ActivityResolutionContext,
    AttackResolutionContext,
    AttackRollModifier,
)
from dnd5e_engine.events import AttackRolled, CombatEvent, DamageApplied
from dnd5e_engine.types.combat import Combatant


def _actor(entity_id: str) -> Combatant:
    return Combatant(
        entity_id=entity_id,
        entity_type="Character",
        name=entity_id,
        initiative=10,
        hp_current=500,
        hp_max=500,
        ac=1,
    )


def _fixture(*, seed: int = 7, natural: int = 15):
    weapon = BundledAssetLoader().get_weapon("dagger")
    assert weapon is not None
    activity = next(
        activity for activity in weapon.activities if isinstance(activity, AttackActivity)
    )
    events: list[CombatEvent] = []
    target = _actor("target")
    ctx = ActivityResolutionContext(
        rng=random.Random(seed),
        caster=_actor("attacker"),
        targets=[target],
        event_emitter=events.append,
        caster_abilities={"str": 10, "dex": 16},
        scale_values={"rogue.sneak-attack": "3d6"},
        sneak_attack_ally_adjacent={target.entity_id: True},
        attack_origin="action",
        turn_serial=12,
        variables={"force_d20": natural},
    )
    return ctx, events, activity, weapon


def test_final_hit_plan_and_damage_phases_preserve_reaction_order() -> None:
    ctx, events, activity, weapon = _fixture()
    phases: list[str] = []
    plans: list[AttackResolutionContext] = []
    rng_before = ctx.rng.getstate()

    def reaction(_hit):
        assert events == []
        phases.append("hit_reaction")
        return 1

    def prepare(plan):
        assert isinstance(events[-1], AttackRolled)
        assert ctx.rng.getstate() == rng_before
        plans.append(plan)
        phases.append("prepare")
        return 1

    def commit(actor_id, target_id):
        assert ctx.sneak_attack_spent[actor_id] is True
        assert target_id == "target"
        assert isinstance(events[-1], DamageApplied)
        phases.append("sneak_commit")

    def damage_complete(_instance):
        assert ctx.sneak_attack_spent["attacker"] is True
        phases.append("damage_reaction")

    def resolved(plan):
        plans.append(plan)
        phases.append("resolved")

    ctx = replace(
        ctx,
        attack_hit_reaction=reaction,
        attack_rider_prepare=prepare,
        attack_rider_resolved=resolved,
        sneak_attack_commit=commit,
        damage_instance_resolved=damage_complete,
    )
    resolve_attack(activity, ctx, weapon=weapon)
    assert phases == ["hit_reaction", "prepare", "sneak_commit", "damage_reaction", "resolved"]
    before, after = plans
    assert before.attacker_id == "attacker"
    assert before.target_id == "target"
    assert before.source_activity_id == activity.id
    assert before.weapon_slug == "dagger"
    assert before.weapon_category == "simple_melee"
    assert before.unarmed is False
    assert before.monk_weapon is True
    assert before.governing_ability == "dex"
    assert before.attack_origin == "action"
    assert before.turn_serial == 12
    assert before.sneak_eligible is before.sneak_will_fire is True
    assert before.sneak_dice_count == 3
    assert before.damage_types == ("piercing",)
    assert after.sneak_dice_sacrificed == 1
    assert after.remaining_sneak_dice_count == 2
    assert after.damage_dealt == sum(
        event.amount for event in events if isinstance(event, DamageApplied)
    )


@pytest.mark.parametrize("natural, refreshed_ac", [(1, 1), (15, 99)])
def test_miss_and_shield_converted_miss_never_prepare_or_resolve_riders(
    natural, refreshed_ac
) -> None:
    ctx, events, activity, weapon = _fixture(natural=natural)
    called: list[object] = []
    ctx = replace(
        ctx,
        attack_hit_reaction=lambda _hit: refreshed_ac,
        attack_rider_prepare=lambda plan: called.append(plan) or 0,
        attack_rider_resolved=called.append,
        sneak_attack_commit=lambda *_args: called.append("commit"),
    )
    before_rng = ctx.rng.getstate()
    resolve_attack(activity, ctx, weapon=weapon)
    [attack] = [event for event in events if isinstance(event, AttackRolled)]
    assert attack.is_hit is False
    assert called == []
    assert ctx.sneak_attack_spent == {}
    assert not any(isinstance(event, DamageApplied) for event in events)
    assert ctx.rng.getstate() == before_rng


def test_sacrificed_sneak_dice_are_removed_before_critical_doubling() -> None:
    ctx, events, activity, weapon = _fixture(natural=20)
    plans: list[AttackResolutionContext] = []
    ctx = replace(ctx, attack_rider_prepare=lambda _plan: 1, attack_rider_resolved=plans.append)
    resolve_attack(activity, ctx, weapon=weapon)
    expected = random.Random(7)
    total = sum(expected.randint(1, 4) for _ in range(2)) + 3
    total += sum(expected.randint(1, 6) for _ in range(4))
    [damage] = [event for event in events if isinstance(event, DamageApplied)]
    assert damage.amount == total
    assert ctx.rng.getstate() == expected.getstate()
    [plan] = plans
    assert plan.is_crit is True
    assert plan.sneak_dice_count == 3
    assert plan.remaining_sneak_dice_count == 2


def test_all_sneak_dice_can_pay_options_and_still_consume_the_turn_cap() -> None:
    ctx, events, activity, weapon = _fixture()
    plans: list[AttackResolutionContext] = []
    commits: list[tuple[str, str]] = []
    ctx = replace(
        ctx,
        attack_rider_prepare=lambda plan: plan.sneak_dice_count if plan.sneak_will_fire else 0,
        attack_rider_resolved=plans.append,
        sneak_attack_commit=lambda actor, target: commits.append((actor, target)),
    )
    resolve_attack(activity, ctx, weapon=weapon)
    resolve_attack(activity, ctx, weapon=weapon)
    expected = random.Random(7)
    totals = [expected.randint(1, 4) + 3 for _ in range(2)]
    assert [event.amount for event in events if isinstance(event, DamageApplied)] == totals
    assert ctx.rng.getstate() == expected.getstate()
    assert plans[0].sneak_will_fire is True
    assert plans[0].remaining_sneak_dice_count == 0
    assert plans[1].sneak_eligible is True
    assert plans[1].sneak_will_fire is False
    assert commits == [("attacker", "target")]


def test_main_hit_commits_sneak_attack_before_a_chained_cleave_roll() -> None:
    ctx, events, activity, weapon = _fixture()
    candidate = _actor("cleave-target")
    plans: list[AttackResolutionContext] = []
    ctx = replace(
        ctx,
        cleave_available=True,
        cleave_candidate=candidate,
        sneak_attack_ally_adjacent={"target": True, "cleave-target": True},
        attack_rider_resolved=plans.append,
    )
    resolve_attack(activity, ctx, weapon=weapon)
    expected = random.Random(7)
    main_damage = expected.randint(1, 4) + 3 + sum(expected.randint(1, 6) for _ in range(3))
    cleave_natural = expected.randint(1, 20)
    cleave_damage = expected.randint(1, 4)
    damage = [event for event in events if isinstance(event, DamageApplied)]
    assert [(event.target_id, event.amount) for event in damage] == [
        ("target", main_damage),
        ("cleave-target", cleave_damage),
    ]
    assert [event.natural for event in events if isinstance(event, AttackRolled)] == [
        15,
        cleave_natural,
    ]
    assert [plan.sneak_will_fire for plan in plans] == [True, False]
    assert ctx.rng.getstate() == expected.getstate()


def test_extra_attack_shares_the_cap_and_another_turn_opportunity_attack_can_reset_it() -> None:
    ctx, _events, activity, weapon = _fixture()
    plans: list[AttackResolutionContext] = []
    ctx = replace(ctx, attack_rider_resolved=plans.append)
    resolve_attack(activity, ctx, weapon=weapon)
    resolve_attack(activity, replace(ctx, attack_origin="nick"), weapon=weapon)
    other_turn = replace(
        ctx,
        sneak_attack_spent={},
        turn_serial=13,
        attack_origin="default",
        is_opportunity_attack=True,
    )
    resolve_attack(activity, other_turn, weapon=weapon)
    assert [plan.sneak_will_fire for plan in plans] == [True, False, True]
    assert [plan.attack_origin for plan in plans] == ["action", "nick", "opportunity"]
    assert [plan.turn_serial for plan in plans] == [12, 12, 13]


@pytest.mark.parametrize("ally_adjacent", [False, True])
def test_cancelled_advantage_uses_the_final_mode_for_sneak_attack(ally_adjacent: bool) -> None:
    ctx, events, activity, weapon = _fixture()
    plans: list[AttackResolutionContext] = []
    ctx = replace(
        ctx,
        attacker_sapped=True,
        sneak_attack_ally_adjacent={"target": ally_adjacent},
        attack_roll_modifier=lambda *_args: AttackRollModifier(advantage_sources=("effect",)),
        attack_rider_resolved=plans.append,
    )
    resolve_attack(activity, ctx, weapon=weapon)
    [plan] = plans
    [attack] = [event for event in events if isinstance(event, AttackRolled)]
    assert plan.advantage_mode == attack.advantage == "normal"
    assert plan.advantage_sources == ("effect",)
    assert plan.disadvantage_sources == ("trait",)
    assert plan.sneak_will_fire is ally_adjacent


@pytest.mark.parametrize("source", ["condition", "help", "one_use"])
def test_every_final_advantage_source_can_qualify_sneak_attack(source: str) -> None:
    ctx, _events, activity, weapon = _fixture()
    plans: list[AttackResolutionContext] = []
    ctx = replace(ctx, sneak_attack_ally_adjacent={}, attack_rider_resolved=plans.append)
    if source == "condition":
        ctx = replace(ctx, target_conditions={"target": ["blinded"]})
    elif source == "help":
        ctx = replace(ctx, target_help_advantage={"target": True})
    else:
        ctx = replace(
            ctx,
            attack_roll_modifier=lambda *_args: AttackRollModifier(advantage_sources=("effect",)),
        )
    resolve_attack(activity, ctx, weapon=weapon)
    [plan] = plans
    assert plan.advantage_mode == "advantage"
    assert plan.sneak_will_fire is True


def test_one_use_modifier_is_consumed_on_the_matching_cancelled_miss_before_the_d20() -> None:
    ctx, events, activity, weapon = _fixture()
    consumed: list[tuple[str, str]] = []
    grant = [AttackRollModifier(advantage_sources=("effect",), flat_bonus=5)]

    def modifier(actor_id, target_id):
        assert len([event for event in events if isinstance(event, AttackRolled)]) == len(consumed)
        consumed.append((actor_id, target_id))
        return grant.pop() if grant else AttackRollModifier()

    ctx = replace(
        ctx,
        targets=[ctx.targets[0].model_copy(update={"ac": 999})],
        variables={},
        attacker_sapped=True,
        attack_roll_modifier=modifier,
    )
    resolve_attack(activity, ctx, weapon=weapon)
    resolve_attack(activity, ctx, weapon=weapon)
    rolls = [event for event in events if isinstance(event, AttackRolled)]
    assert [roll.advantage for roll in rolls] == ["normal", "disadvantage"]
    assert [roll.modifier for roll in rolls] == [10, 5]
    assert [roll.is_hit for roll in rolls] == [False, False]
    assert consumed == [("attacker", "target"), ("attacker", "target")]
    assert grant == []
    expected = random.Random(7)
    for _ in range(3):
        expected.randint(1, 20)
    assert ctx.rng.getstate() == expected.getstate()


def test_flurry_unarmed_provenance_comes_from_the_shared_attack() -> None:
    ctx, _events, _activity, _weapon = _fixture()
    weapon = BundledAssetLoader().get_weapon("unarmed-strike")
    assert weapon is not None
    activity = next(
        activity for activity in weapon.activities if isinstance(activity, AttackActivity)
    )
    plans: list[AttackResolutionContext] = []
    ctx = replace(
        ctx, attack_origin="flurry", martial_arts=True, attack_rider_resolved=plans.append
    )
    resolve_attack(activity, ctx, weapon=weapon)
    [plan] = plans
    assert plan.attack_origin == "flurry"
    assert plan.unarmed is plan.monk_weapon is True
    assert plan.governing_ability == "dex"
    assert plan.damage_types == ("bludgeoning",)
    assert plan.sneak_eligible is False


def test_nonweapon_attack_has_no_fabricated_weapon_qualification() -> None:
    ctx, _events, activity, _weapon = _fixture()
    plans: list[AttackResolutionContext] = []
    activity = activity.model_copy(
        update={
            "damage": AttackDamageBlock(
                include_base=False, parts=[DamagePartBlock(bonus="2", types=["fire"])]
            )
        }
    )
    ctx = replace(ctx, attack_origin="spell", attack_rider_resolved=plans.append)
    resolve_attack(activity, ctx)
    [plan] = plans
    assert plan.weapon_slug is plan.weapon_category is None
    assert plan.unarmed is plan.monk_weapon is False
    assert plan.sneak_eligible is plan.sneak_will_fire is False
    assert plan.damage_types == ("fire",)


@pytest.mark.parametrize("sacrificed", [0, 3])
@pytest.mark.parametrize("policy", ["static", "all", "sidecar", "negated"])
def test_known_damage_immunity_prevents_sneak_and_dice_sacrifice(
    sacrificed: int, policy: str
) -> None:
    ctx, events, activity, weapon = _fixture()
    commits: list[tuple[str, str]] = []
    plans: list[AttackResolutionContext] = []
    if policy in ("static", "all"):
        ctx = replace(
            ctx,
            targets=[
                ctx.targets[0].model_copy(
                    update={"damage_immunities": ["all" if policy == "all" else "piercing"]}
                )
            ],
        )
    elif policy == "sidecar":
        ctx = replace(ctx, passive_damage_modifiers={"target": {"immunities": ["piercing"]}})
    else:
        ctx = replace(ctx, negated_spell_damage_targets=frozenset({"target"}))
    ctx = replace(
        ctx,
        attack_rider_prepare=lambda plan: sacrificed if plan.sneak_will_fire else 0,
        attack_rider_resolved=plans.append,
        sneak_attack_commit=lambda actor, target: commits.append((actor, target)),
    )
    resolve_attack(activity, ctx, weapon=weapon)
    [damage] = [event for event in events if isinstance(event, DamageApplied)]
    assert damage.amount == 0
    assert commits == []
    assert ctx.sneak_attack_spent.get("attacker", False) is False
    [plan] = plans
    assert plan.sneak_eligible is True
    assert plan.sneak_will_fire is False
    assert plan.sneak_dice_sacrificed == 0
    expected = random.Random(7)
    expected.randint(1, 4)
    assert ctx.rng.getstate() == expected.getstate()


def test_unrelated_damage_type_cannot_make_immune_sneak_damage_eligible() -> None:
    ctx, events, activity, weapon = _fixture()
    plans: list[AttackResolutionContext] = []
    activity = activity.model_copy(
        update={
            "damage": AttackDamageBlock(
                include_base=True, parts=[DamagePartBlock(bonus="2", types=["fire"])]
            )
        }
    )
    ctx = replace(
        ctx,
        targets=[ctx.targets[0].model_copy(update={"damage_immunities": ["piercing"]})],
        attack_rider_resolved=plans.append,
    )
    resolve_attack(activity, ctx, weapon=weapon)
    assert [
        (event.damage_type, event.amount) for event in events if isinstance(event, DamageApplied)
    ] == [("piercing", 0), ("fire", 2)]
    [plan] = plans
    assert plan.damage_dealt == 2
    assert plan.sneak_eligible is True
    assert plan.sneak_will_fire is False
    assert ctx.sneak_attack_spent == {}


def test_builder_forwards_typed_hooks_and_preserves_an_empty_shared_sneak_cap() -> None:
    ctx, _events, _activity, _weapon = _fixture()

    def modifier(*_args):
        return AttackRollModifier()

    def prepare(_plan):
        return 0

    def resolved(_plan):
        return None

    def commit(*_args):
        return None

    spent: dict[str, bool] = {}
    built = build_activity_context(
        ctx.caster,
        ctx.targets,
        rng=ctx.rng,
        event_emitter=ctx.event_emitter,
        slot_level=None,
        base_spell_level=None,
        spellcasting_ability=None,
        concentration=False,
        source_passive_effects=[],
        spell_book={},
        passive_damage_modifiers={},
        save_modifiers={},
        sneak_attack_spent=spent,
        attack_origin="flurry",
        turn_serial=13,
        attack_roll_modifier=modifier,
        attack_rider_prepare=prepare,
        attack_rider_resolved=resolved,
        sneak_attack_commit=commit,
    )
    assert built.attack_origin == "flurry"
    assert built.turn_serial == 13
    assert built.attack_roll_modifier is modifier
    assert built.attack_rider_prepare is prepare
    assert built.attack_rider_resolved is resolved
    assert built.sneak_attack_commit is commit
    assert built.sneak_attack_spent is spent
