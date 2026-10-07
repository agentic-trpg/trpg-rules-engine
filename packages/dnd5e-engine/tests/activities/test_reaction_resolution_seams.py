"""Shared attack adjudication and damage-instance boundaries for reactions."""

from __future__ import annotations

import random
from dataclasses import replace

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.common import (
    AttackActivity,
    AttackBlock,
    AttackDamageBlock,
    DamageActivity,
    DamageActivityDamageBlock,
    DamagePartBlock,
    SaveActivity,
    SaveBlock,
    SaveDamageBlock,
    SaveDamageCriticalBlock,
    SaveDcBlock,
)
from dnd5e_srd_data.schema.monster import MonsterTraitMechanic

from dnd5e_engine.activities.apply import apply_damage
from dnd5e_engine.activities.attack import resolve_attack
from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.context import (
    ActivityResolutionContext,
    AttackHitContext,
    DamageInstanceContext,
)
from dnd5e_engine.activities.resolver import resolve_activity
from dnd5e_engine.events import AttackRolled, CombatEvent, DamageApplied
from dnd5e_engine.types.combat import Combatant


def _actor(entity_id: str) -> Combatant:
    return Combatant(
        entity_id=entity_id,
        entity_type="Character",
        name=entity_id,
        initiative=10,
        hp_current=50,
        hp_max=50,
        ac=12,
    )


def _context(seed: int = 3) -> tuple[ActivityResolutionContext, list[CombatEvent]]:
    events: list[CombatEvent] = []
    ctx = ActivityResolutionContext(
        rng=random.Random(seed),
        caster=_actor("attacker"),
        targets=[_actor("target")],
        event_emitter=events.append,
        caster_abilities={"str": 10, "dex": 10, "con": 10, "int": 10, "wis": 10, "cha": 10},
    )
    return ctx, events


def _parts() -> list[DamagePartBlock]:
    return [
        DamagePartBlock(bonus="7", types=["bludgeoning"]),
        DamagePartBlock(bonus="3", types=["fire"]),
    ]


def _attack() -> AttackActivity:
    return AttackActivity(
        id="attack-activity",
        name="Attack",
        attack=AttackBlock(flat=True, bonus="5"),
        damage=AttackDamageBlock(include_base=False, parts=_parts()),
    )


@pytest.mark.parametrize(
    ("seed", "refreshed_ac", "should_fire", "final_hit"),
    [(1, 17, False, False), (3, 17, True, False), (0, 17, True, True), (5, 999, True, True)],
)
def test_only_a_provisional_hit_requests_reaction_and_retains_the_roll(
    seed: int, refreshed_ac: int, should_fire: bool, final_hit: bool
) -> None:
    ctx, events = _context(seed)
    calls: list[AttackHitContext] = []

    def react(hit: AttackHitContext) -> int:
        assert events == []  # No provisional authoritative attack event.
        calls.append(hit)
        return refreshed_ac

    ctx = replace(ctx, attack_hit_reaction=react)
    expected_rng = random.Random(seed)
    natural = expected_rng.randint(1, 20)
    resolve_attack(_attack(), ctx)

    attacks = [event for event in events if isinstance(event, AttackRolled)]
    assert len(attacks) == 1
    assert attacks[0].natural == natural
    assert attacks[0].roll_total == natural + 5
    assert attacks[0].is_hit is final_hit
    assert bool(calls) is should_fire
    assert ctx.rng.getstate() == expected_rng.getstate()
    if should_fire:
        assert calls == [
            AttackHitContext(
                "attacker",
                "target",
                "attack-activity",
                natural,
                natural + 5,
                12,
                natural == 20,
                False,
            )
        ]
    if not final_hit:
        assert not any(isinstance(event, DamageApplied) for event in events)


def test_advantage_reaction_keeps_both_original_draws() -> None:
    ctx, events = _context(3)
    calls: list[AttackHitContext] = []
    ctx.targets[0].ac = 21

    def react(hit: AttackHitContext) -> int:
        calls.append(hit)
        return 26

    ctx = replace(ctx, attacker_conditions=["invisible"], attack_hit_reaction=react)
    expected_rng = random.Random(3)
    draws = [expected_rng.randint(1, 20), expected_rng.randint(1, 20)]
    resolve_attack(_attack(), ctx)
    attack = next(event for event in events if isinstance(event, AttackRolled))
    assert attack.advantage == "advantage"
    assert attack.natural == max(draws)
    assert attack.is_hit is False
    assert len(calls) == 1
    assert ctx.rng.getstate() == expected_rng.getstate()


def test_opportunity_attack_uses_the_same_provisional_hit_boundary() -> None:
    ctx, events = _context()
    calls: list[AttackHitContext] = []

    def react(hit: AttackHitContext) -> int:
        calls.append(hit)
        return 17

    resolve_attack(_attack(), replace(ctx, is_opportunity_attack=True, attack_hit_reaction=react))
    assert calls[0].is_opportunity_attack is True
    attack = next(event for event in events if isinstance(event, AttackRolled))
    assert attack.is_opportunity_attack is True
    assert attack.is_hit is False


def test_cleave_uses_the_same_hit_callback_without_repeating_the_main_roll() -> None:
    ctx, events = _context(0)
    ctx.caster.strength = 13
    candidate = _actor("cleave-target")
    calls: list[AttackHitContext] = []
    weapon = BundledAssetLoader().get_weapon("greataxe")
    assert weapon is not None

    def react(hit: AttackHitContext) -> int:
        calls.append(hit)
        return 100 if hit.target_id == candidate.entity_id else hit.effective_ac

    ctx = replace(ctx, cleave_available=True, cleave_candidate=candidate, attack_hit_reaction=react)
    resolve_attack(_attack(), ctx, weapon=weapon)
    attacks = [event for event in events if isinstance(event, AttackRolled)]
    assert [hit.target_id for hit in calls] == ["target", "cleave-target"]
    assert [attack.is_hit for attack in attacks] == [True, False]
    assert not any(
        isinstance(event, DamageApplied) and event.target_id == "cleave-target" for event in events
    )
    expected_rng = random.Random(0)
    expected_rng.randint(1, 20)
    expected_rng.randint(1, 20)
    assert ctx.rng.getstate() == expected_rng.getstate()


@pytest.mark.parametrize("kind", ["attack", "save", "damage"])
def test_all_damage_activity_paths_attribute_and_group_types(kind: str) -> None:
    ctx, events = _context(0)
    completed: list[DamageInstanceContext] = []
    ctx = replace(ctx, damage_instance_resolved=completed.append)
    if kind == "attack":
        activity = _attack()
    elif kind == "save":
        activity = SaveActivity(
            save=SaveBlock(ability=["dex"], dc=SaveDcBlock(calculation="flat", formula="99")),
            damage=SaveDamageBlock(parts=_parts()),
        )
    else:
        activity = DamageActivity(damage=DamageActivityDamageBlock(parts=_parts()))
    resolve_activity(activity, ctx)
    damage = [event for event in events if isinstance(event, DamageApplied)]
    assert len(damage) == 2
    assert {event.source_actor_id for event in damage} == {"attacker"}
    assert {event.damage_instance_id for event in damage} == {"damage:attacker:target:1"}
    assert completed == [
        DamageInstanceContext(
            "damage:attacker:target:1", "attacker", "target", None, 10, ("bludgeoning", "fire")
        )
    ]


@pytest.mark.parametrize("kind", ["save", "damage"])
def test_non_attack_activities_never_request_a_hit_reaction(kind: str) -> None:
    ctx, _events = _context()

    def unexpected(_hit: AttackHitContext) -> int:
        pytest.fail("A non-attack activity requested HIT_BY_ATTACK")

    ctx = replace(ctx, attack_hit_reaction=unexpected)
    if kind == "save":
        activity = SaveActivity(
            save=SaveBlock(ability=["dex"], dc=SaveDcBlock(calculation="flat", formula="99")),
            damage=SaveDamageBlock(parts=_parts()),
        )
    else:
        activity = DamageActivity(damage=DamageActivityDamageBlock(parts=_parts()))
    resolve_activity(activity, ctx)


def test_a_nested_critical_damage_activity_keeps_its_critical_provenance() -> None:
    ctx, events = _context()
    ctx.variables["in_crit"] = 1
    activity = DamageActivity(
        damage=DamageActivityDamageBlock(
            critical=SaveDamageCriticalBlock(allow=True), parts=_parts()
        )
    )
    resolve_activity(activity, ctx)
    damage = [event for event in events if isinstance(event, DamageApplied)]
    assert len(damage) == 2
    assert all(event.is_crit for event in damage)
    assert len({event.damage_instance_id for event in damage}) == 1


def test_damage_completion_happens_after_every_type_has_folded() -> None:
    ctx, events = _context()
    ordering: list[str] = []

    def emit(event: CombatEvent) -> None:
        events.append(event)
        if isinstance(event, DamageApplied):
            ctx.targets[0].hp_current -= event.amount
            ordering.append(event.damage_type)

    def completed(instance: DamageInstanceContext) -> None:
        assert ctx.targets[0].hp_current == 40
        assert instance.amount == 10
        ordering.append("completed")

    ctx = replace(ctx, event_emitter=emit, damage_instance_resolved=completed)
    apply_damage(ctx.targets[0], {"bludgeoning": 7, "fire": 3}, ctx)
    assert ordering == ["bludgeoning", "fire", "completed"]


def test_replaced_contexts_share_the_fallback_sequence_without_rng() -> None:
    ctx, events = _context()
    original_rng = ctx.rng.getstate()
    apply_damage(ctx.targets[0], {"fire": 1}, ctx)
    apply_damage(ctx.targets[0], {"cold": 1}, replace(ctx))
    damage = [event for event in events if isinstance(event, DamageApplied)]
    assert [event.damage_instance_id for event in damage] == [
        "damage:attacker:target:1",
        "damage:attacker:target:2",
    ]
    assert ctx.rng.getstate() == original_rng


def test_live_sequence_provider_replaces_local_sequence() -> None:
    ctx, events = _context()
    calls: list[tuple[str, str | None]] = []

    def identify(target_id: str, source_id: str | None) -> str:
        calls.append((target_id, source_id))
        return f"live-damage:{len(calls)}"

    ctx = replace(ctx, damage_instance_id_provider=identify)
    apply_damage(ctx.targets[0], {"fire": 2, "cold": 3}, ctx, source_id="weapon")
    assert calls == [("target", "weapon")]
    assert {event.damage_instance_id for event in events if isinstance(event, DamageApplied)} == {
        "live-damage:1"
    }


def test_damage_negation_is_scoped_to_this_resolution_and_target() -> None:
    ctx, events = _context()
    other = _actor("other")
    negated = replace(ctx, negated_spell_damage_targets=frozenset({"target"}))
    apply_damage(ctx.targets[0], {"force": 10}, negated, magical=True)
    apply_damage(other, {"force": 10}, negated, magical=True)
    apply_damage(ctx.targets[0], {"force": 10}, ctx, magical=True)
    damage = [event for event in events if isinstance(event, DamageApplied)]
    assert [event.amount for event in damage] == [0, 10, 10]
    assert ctx.targets[0].damage_immunities == []


def test_zero_damage_does_not_request_undead_fortitude_at_zero_hp() -> None:
    ctx, events = _context()
    target = ctx.targets[0]
    target.hp_current = 0
    target.trait_mechanics = [MonsterTraitMechanic.UNDEAD_FORTITUDE]
    original_rng = ctx.rng.getstate()
    apply_damage(
        target, {"force": 10}, replace(ctx, negated_spell_damage_targets=frozenset({"target"}))
    )
    assert len(events) == 1
    assert isinstance(events[0], DamageApplied)
    assert events[0].amount == 0
    assert ctx.rng.getstate() == original_rng


def test_context_builder_forwards_reaction_and_damage_boundaries() -> None:
    ctx, _events = _context()
    calls: list[AttackHitContext] = []
    completed: list[DamageInstanceContext] = []

    def react(hit: AttackHitContext) -> int:
        calls.append(hit)
        return hit.effective_ac + 5

    def identify(_target_id: str, _source_id: str | None) -> str:
        return "live:1"

    def continuing() -> bool:
        return True

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
        attack_hit_reaction=react,
        attack_continuation_allowed=continuing,
        damage_instance_id_provider=identify,
        damage_instance_resolved=completed.append,
        negated_spell_damage_targets=frozenset({"target"}),
    )
    assert built.attack_hit_reaction is react
    assert built.attack_continuation_allowed is continuing
    assert built.damage_instance_id_provider is identify
    assert built.damage_instance_resolved == completed.append
    assert built.negated_spell_damage_targets == frozenset({"target"})


@pytest.mark.parametrize("cleave", [False, True])
def test_a_damage_reaction_that_stops_the_attacker_prevents_another_roll(cleave: bool) -> None:
    ctx, events = _context(0)
    ctx.caster.strength = 13
    candidate = _actor("second-target")
    allowed = True

    def completed(_damage: DamageInstanceContext) -> None:
        nonlocal allowed
        # The live callback observes a lethal damage reaction after the
        # first completed hit, before another target or Cleave can roll.
        allowed = False

    ctx = replace(
        ctx,
        targets=ctx.targets if cleave else [*ctx.targets, candidate],
        cleave_available=cleave,
        cleave_candidate=candidate if cleave else None,
        damage_instance_resolved=completed,
        attack_continuation_allowed=lambda: allowed,
    )
    weapon = BundledAssetLoader().get_weapon("greataxe") if cleave else None
    resolve_attack(_attack(), ctx, weapon=weapon)
    attacks = [event for event in events if isinstance(event, AttackRolled)]
    assert [event.target_id for event in attacks] == ["target"]
    assert not any(
        isinstance(event, DamageApplied) and event.target_id == candidate.entity_id
        for event in events
    )
    assert ctx.mastery_procs == []
    expected_rng = random.Random(0)
    expected_rng.randint(1, 20)
    assert ctx.rng.getstate() == expected_rng.getstate()
