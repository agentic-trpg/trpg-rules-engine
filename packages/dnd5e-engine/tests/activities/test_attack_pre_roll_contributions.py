"""Full pre-roll provenance and canonical extra damage share one hit instance."""

from __future__ import annotations

import random
from dataclasses import replace

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.common import AttackActivity, DamageActivity

from dnd5e_engine.activities.attack import resolve_attack
from dnd5e_engine.activities.context import (
    ActivityResolutionContext,
    AttackDamageContribution,
    AttackPreRollContext,
    AttackRiderPreparation,
    AttackRollModifier,
)
from dnd5e_engine.events import AttackRolled, CombatEvent, DamageApplied
from dnd5e_engine.feature_runtime import FeaturePreflightError
from dnd5e_engine.types.combat import Combatant
from dnd5e_engine.types.effects import ActiveEffect, ActiveEffectChange


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


def _fixture(*, natural: int | None = 15):
    weapon = BundledAssetLoader().get_weapon("longsword")
    assert weapon is not None
    activity = next(a for a in weapon.activities if isinstance(a, AttackActivity))
    events: list[CombatEvent] = []
    ctx = ActivityResolutionContext(
        rng=random.Random(7),
        caster=_actor("attacker"),
        targets=[_actor("target")],
        event_emitter=events.append,
        caster_abilities={"str": 16, "dex": 18},
        attack_origin="action",
        turn_serial=12,
        variables={"force_d20": natural} if natural is not None else {},
    )
    return ctx, events, activity, weapon


def _flag(key: str, *, target="attacker", disabled=False) -> ActiveEffect:
    return ActiveEffect(
        id=key,
        name=key,
        origin="test",
        target_id=target,
        disabled=disabled,
        changes=[ActiveEffectChange(key=key, mode="override", value=True)],
    )


def _damage(formula: str, *, damage_type: str | None = None, crit=False) -> DamageActivity:
    return DamageActivity.model_validate(
        {
            "id": "canonical-extra",
            "damage": {
                "critical": {"allow": crit},
                "parts": [
                    {
                        "custom": {"enabled": True, "formula": formula},
                        "types": [damage_type] if damage_type else [],
                    }
                ],
            },
        }
    )


def test_pre_roll_observes_complete_sources_after_consuming_one_use() -> None:
    ctx, events, activity, weapon = _fixture()
    seen: list[AttackPreRollContext] = []
    order: list[str] = []

    def modifier(*_ids):
        order.append("consume")
        return AttackRollModifier(advantage_sources=("effect",), flat_bonus=5)

    def before_roll(plan):
        assert order == ["consume"]
        assert events == []
        order.append("pre_roll")
        seen.append(plan)
        return AttackRollModifier()

    ctx = replace(
        ctx,
        active_effects=(
            _flag("flags.advantage.attack"),
            _flag("flags.disadvantage.attack"),
        ),
        attacker_conditions=["restrained"],
        attacker_unseen_by={"target": True},
        target_help_advantage={"target": True},
        attacker_vex_advantage={"target": True},
        attack_roll_modifier=modifier,
        attack_pre_roll=before_roll,
    )
    resolve_attack(activity, ctx, weapon=weapon)
    plan = seen[0]
    assert plan.advantage_sources == ("flag", "unseen", "help", "trait", "effect")
    assert plan.disadvantage_sources == ("flag", "condition:attacker")
    assert (plan.governing_ability, plan.weapon_slug, plan.weapon_category) == (
        "str",
        "longsword",
        "martial_melee",
    )
    assert (plan.attacker_id, plan.target_id, plan.source_activity_id) == (
        "attacker",
        "target",
        activity.id,
    )
    assert (plan.attack_origin, plan.turn_serial, plan.unarmed) == ("action", 12, False)
    roll = next(e for e in events if isinstance(e, AttackRolled))
    assert roll.advantage == "normal"
    assert roll.modifier == 10


def test_pre_roll_refusal_precedes_even_dice_valued_attack_bonus() -> None:
    ctx, events, activity, weapon = _fixture(natural=None)
    activity = activity.model_copy(
        update={"attack": activity.attack.model_copy(update={"bonus": "1d4"})}
    )
    before = ctx.rng.getstate()

    def reject(_plan):
        raise FeaturePreflightError("disadvantage blocks this declaration")

    with pytest.raises(FeaturePreflightError, match="disadvantage"):
        resolve_attack(activity, replace(ctx, attack_pre_roll=reject), weapon=weapon)
    assert ctx.rng.getstate() == before
    assert events == []


def test_forgo_clears_all_existing_and_new_advantage_and_retains_disadvantage() -> None:
    ctx, events, activity, weapon = _fixture(natural=None)
    ctx = replace(
        ctx,
        active_effects=(_flag("flags.advantage.attack.strength"),),
        attacker_unseen_by={"target": True},
        target_help_advantage={"target": True},
        attacker_vex_advantage={"target": True},
        attacker_sapped=True,
        attack_roll_modifier=lambda *_ids: AttackRollModifier(advantage_sources=("effect",)),
        attack_pre_roll=lambda _plan: AttackRollModifier(
            advantage_sources=("feature_rider",), forgo_all_advantage=True
        ),
    )
    resolve_attack(activity, ctx, weapon=weapon)
    roll = next(e for e in events if isinstance(e, AttackRolled))
    expected = random.Random(7)
    natural = min(expected.randint(1, 20), expected.randint(1, 20))
    if natural != 1:
        expected.randint(1, 8)
    assert roll.natural == natural
    assert roll.advantage == "disadvantage"
    assert roll.advantage_sources == []
    assert roll.disadvantage_sources == ["trait"]
    assert ctx.rng.getstate() == expected.getstate()


@pytest.mark.parametrize(
    "ability,expected", [("str", "advantage"), ("dex", "normal"), ("int", "normal")]
)
def test_strength_flag_is_ability_scoped(ability, expected) -> None:
    ctx, events, activity, weapon = _fixture()
    activity = activity.model_copy(
        update={"attack": activity.attack.model_copy(update={"ability": ability})}
    )
    resolve_attack(
        activity,
        replace(ctx, active_effects=(_flag("flags.advantage.attack.strength"),)),
        weapon=weapon,
    )
    assert next(e for e in events if isinstance(e, AttackRolled)).advantage == expected


def test_attack_against_flag_belongs_to_target_and_works_for_spell_attacks() -> None:
    ctx, events, activity, _weapon = _fixture()
    resolve_attack(
        activity,
        replace(ctx, active_effects=(_flag("flags.advantage.attack_against", target="target"),)),
    )
    assert next(e for e in events if isinstance(e, AttackRolled)).advantage == "advantage"


def test_disabled_or_another_actors_flags_do_not_apply() -> None:
    ctx, events, activity, weapon = _fixture()
    ctx = replace(
        ctx,
        active_effects=(
            _flag("flags.advantage.attack.strength", disabled=True),
            _flag("flags.advantage.attack_against"),
            _flag("flags.advantage.attack", target="someone-else"),
        ),
    )
    resolve_attack(activity, ctx, weapon=weapon)
    assert next(e for e in events if isinstance(e, AttackRolled)).advantage == "normal"


@pytest.mark.parametrize(
    "value,expected",
    [(True, "advantage"), ("true", "advantage"), (False, "normal"), ("false", "normal")],
)
def test_effect_flags_accept_canonical_boolean_string_without_truthiness(value, expected) -> None:
    ctx, events, activity, weapon = _fixture()
    effect = _flag("flags.advantage.attack.strength")
    effect.changes[0].value = value
    resolve_attack(activity, replace(ctx, active_effects=(effect,)), weapon=weapon)
    assert next(e for e in events if isinstance(e, AttackRolled)).advantage == expected


def test_live_projection_refreshes_chained_cleave_after_first_roll_declares_effect() -> None:
    ctx, events, activity, weapon = _fixture()
    effects: list[ActiveEffect] = []
    plans: list[AttackPreRollContext] = []

    def declare(plan):
        plans.append(plan)
        if len(plans) == 1:
            effects.append(_flag("flags.advantage.attack.strength"))
            return AttackRollModifier(advantage_sources=("feature_rider",))
        return AttackRollModifier()

    ctx = replace(
        ctx,
        cleave_available=True,
        cleave_candidate=_actor("chain-target"),
        attack_active_effects=lambda actor: tuple(e for e in effects if e.target_id == actor),
        attack_pre_roll=declare,
    )
    resolve_attack(activity, ctx, weapon=weapon)
    assert len(plans) == 2
    assert plans[0].advantage_sources == ()
    assert plans[1].advantage_sources == ("flag",)
    assert plans[1].target_id == "chain-target"
    assert [e.advantage for e in events if isinstance(e, AttackRolled)] == [
        "advantage",
        "advantage",
    ]


def test_later_roll_in_same_context_reprojects_target_effects_and_opportunity_origin() -> None:
    ctx, events, activity, weapon = _fixture()
    effects: list[ActiveEffect] = []
    plans: list[AttackPreRollContext] = []
    ctx = replace(
        ctx,
        attack_active_effects=lambda _actor: effects,
        attack_pre_roll=lambda p: plans.append(p) or AttackRollModifier(),
    )
    resolve_attack(activity, ctx, weapon=weapon)
    effects.append(_flag("flags.advantage.attack_against", target="target"))
    resolve_attack(
        activity, replace(ctx, attack_origin="default", is_opportunity_attack=True), weapon=weapon
    )
    assert [e.advantage for e in events if isinstance(e, AttackRolled)] == ["normal", "advantage"]
    assert plans[-1].attack_origin == "opportunity"


@pytest.mark.parametrize("natural,shield", [(1, False), (15, True)])
def test_extra_damage_is_never_prepared_or_rolled_on_final_miss(natural, shield) -> None:
    ctx, events, activity, weapon = _fixture(natural=natural)
    preparations: list[object] = []
    before = ctx.rng.getstate()
    ctx = replace(
        ctx,
        attack_hit_reaction=(lambda _hit: 99) if shield else None,
        attack_rider_prepare=lambda plan: (
            preparations.append(plan)
            or AttackRiderPreparation(
                damage_contributions=(AttackDamageContribution(_damage("3d10"), "extra", True),)
            )
        ),
    )
    resolve_attack(activity, ctx, weapon=weapon)
    assert preparations == []
    assert ctx.rng.getstate() == before
    assert not any(isinstance(e, DamageApplied) for e in events)
    assert next(e for e in events if isinstance(e, AttackRolled)).is_hit is False


@pytest.mark.parametrize("allow_crit", [False, True])
def test_scale_formula_inherits_type_and_obeys_canonical_critical_policy(allow_crit) -> None:
    ctx, events, activity, weapon = _fixture(natural=20)
    instances = []
    plans = []
    contribution = AttackDamageContribution(
        _damage("@scale.barbarian.brutal-strike", crit=allow_crit), "feature:extra", True
    )
    ctx = replace(
        ctx,
        scale_values={"barbarian.brutal-strike": "2d10"},
        attack_rider_prepare=lambda _plan: AttackRiderPreparation(
            damage_contributions=(contribution,)
        ),
        attack_rider_resolved=plans.append,
        damage_instance_resolved=instances.append,
    )
    resolve_attack(activity, ctx, weapon=weapon)
    expected = random.Random(7)
    total = sum(expected.randint(1, 8) for _ in range(2)) + 3
    total += sum(expected.randint(1, 10) for _ in range(4 if allow_crit else 2))
    damage = [e for e in events if isinstance(e, DamageApplied)]
    assert [(e.damage_type, e.amount) for e in damage] == [("slashing", total)]
    assert len(instances) == 1
    assert instances[0].amount == total
    assert plans[0].damage_contributions == (contribution,)
    assert plans[0].damage_dealt == total
    assert ctx.rng.getstate() == expected.getstate()


def test_two_options_share_damage_group_while_another_canonical_extra_stacks() -> None:
    ctx, events, activity, weapon = _fixture()
    instances = []
    brutal = AttackDamageContribution(
        _damage("@scale.barbarian.brutal-strike"), "brutal", True, "shared-brutal"
    )
    frenzy = AttackDamageContribution(_damage("(@scale.barbarian.rage-damage)d6"), "frenzy", True)
    ctx = replace(
        ctx,
        scale_values={"barbarian.brutal-strike": "2d10", "barbarian.rage-damage": 3},
        attack_rider_prepare=lambda _plan: AttackRiderPreparation(
            damage_contributions=(brutal, brutal, frenzy)
        ),
        damage_instance_resolved=instances.append,
    )
    resolve_attack(activity, ctx, weapon=weapon)
    expected = random.Random(7)
    total = expected.randint(1, 8) + 3
    total += sum(expected.randint(1, 10) for _ in range(2))
    total += sum(expected.randint(1, 6) for _ in range(3))
    assert [e.amount for e in events if isinstance(e, DamageApplied)] == [total]
    assert len(instances) == 1
    assert ctx.rng.getstate() == expected.getstate()


def test_different_damage_types_emit_events_in_one_complete_damage_instance() -> None:
    ctx, events, activity, weapon = _fixture()
    instances = []
    contribution = AttackDamageContribution(_damage("1d6", damage_type="fire"), "fire-rider")
    ctx = replace(
        ctx,
        attack_rider_prepare=lambda _plan: AttackRiderPreparation(
            damage_contributions=(contribution,)
        ),
        damage_instance_resolved=instances.append,
    )
    resolve_attack(activity, ctx, weapon=weapon)
    damage = [e for e in events if isinstance(e, DamageApplied)]
    assert [e.damage_type for e in damage] == ["slashing", "fire"]
    assert len({e.damage_instance_id for e in damage}) == 1
    assert {e.source_actor_id for e in damage} == {"attacker"}
    assert len(instances) == 1
    assert instances[0].amount == sum(e.amount for e in damage)
    assert instances[0].damage_types == ("slashing", "fire")
