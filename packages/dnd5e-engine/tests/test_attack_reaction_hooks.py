"""Live Shield adjustments remain visible to a reused attack context."""

from __future__ import annotations

import asyncio
import random
from types import SimpleNamespace

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.common import (
    AttackActivity,
    AttackBlock,
    AttackDamageBlock,
    DamagePartBlock,
    ReactionTriggerKind,
    SaveActivity,
)

from dnd5e_engine import live_reactions
from dnd5e_engine.activities.apply import apply_damage
from dnd5e_engine.activities.attack import resolve_attack
from dnd5e_engine.activities.context import ActivityResolutionContext
from dnd5e_engine.events import (
    AttackRolled,
    CastFailed,
    DamageApplied,
    EffectApplied,
    ReactionTriggered,
    SaveRolled,
)
from dnd5e_engine.live_reactions import (
    attach_reaction_hooks,
    fire_reaction,
    register_pending_reaction,
)
from dnd5e_engine.orchestrator import PlayerIntent, _emit, _get_live, start_combat
from dnd5e_engine.reactions import ReactionOpportunity
from dnd5e_engine.spatial import cell_id
from dnd5e_engine.specs import EncounterMemberSpec, GridScene, PartyMemberSpec
from dnd5e_engine.types.effects import ActiveEffect, ActiveEffectChange
from tests.c21_support import act, cleric, combatant, pc, start


@pytest.mark.parametrize("existing_ac_change, existing_ac_bonus", [(None, 0), ("1d4", 3)])
def test_shield_remains_live_for_later_hits_in_one_reused_context(
    existing_ac_change: str | None, existing_ac_bonus: int
) -> None:
    async def setup():
        result = await start_combat(
            session_id=f"shield-reused-context:{existing_ac_change}",
            party=[
                PartyMemberSpec(
                    entity_id="char:reactor",
                    name="Reactor",
                    initiative=20,
                    hp_current=30,
                    hp_max=30,
                    ac=12,
                    intelligence=16,
                    class_slug="wizard",
                    character_level=5,
                    spells_known=["shield"],
                    spell_slots={1: 2},
                    zone_id=cell_id(0, 0),
                )
            ],
            encounter=[
                EncounterMemberSpec(
                    entity_id="mon:attacker",
                    entity_type="Monster",
                    name="Attacker",
                    initiative=10,
                    hp_current=30,
                    hp_max=30,
                    zone_id=cell_id(1, 0),
                )
            ],
            grid_scene=GridScene(width=2, height=1),
            rng_seed=4,
        )
        return _get_live(result.handle)

    live = asyncio.run(setup())
    attacker = next(actor for actor in live.initiative if actor.entity_id == "mon:attacker")
    reactor = next(actor for actor in live.initiative if actor.entity_id == "char:reactor")
    if existing_ac_change is not None:
        _emit(
            live,
            EffectApplied(
                effect=ActiveEffect(
                    id="effect:existing-ac",
                    name="Existing AC",
                    origin="fixture",
                    target_id=reactor.entity_id,
                    changes=[
                        ActiveEffectChange(key="ac.bonus", mode="add", value=existing_ac_change)
                    ],
                )
            ),
        )
    register_pending_reaction(
        live,
        reactor.entity_id,
        PlayerIntent(intent_type="ready", spell_id="shield"),
    )
    # The die-valued AC bonus represents the value already rolled when the
    # attack context was built. Resolving Shield must preserve that value.
    ctx = ActivityResolutionContext(
        rng=live.rng,
        caster=attacker,
        targets=[reactor],
        event_emitter=lambda event: _emit(live, event),
        caster_abilities={"str": 10},
        passive_ac_bonus={reactor.entity_id: existing_ac_bonus},
    )
    ctx = attach_reaction_hooks(live, ctx)
    attack = AttackActivity(
        id="repeated-attack",
        name="Repeated attack",
        attack=AttackBlock(flat=True, bonus=str(5 + existing_ac_bonus)),
        damage=AttackDamageBlock(
            include_base=False,
            parts=[DamagePartBlock(bonus="4", types=["bludgeoning"])],
        ),
    )
    before = len(live.event_log)
    resolve_attack(attack, ctx)
    resolve_attack(attack, ctx)
    emitted = live.event_log[before:]
    attacks = [event for event in emitted if isinstance(event, AttackRolled)]
    assert [event.natural for event in attacks] == [8, 10]
    assert [event.is_hit for event in attacks] == [False, False]
    assert len([event for event in emitted if isinstance(event, ReactionTriggered)]) == 1
    assert not any(isinstance(event, DamageApplied) for event in emitted)
    assert live.spell_slots_by_entity[reactor.entity_id][1] == 1
    expected_rng = random.Random(4)
    expected_rng.randint(1, 20)
    expected_rng.randint(1, 20)
    assert live.rng.getstate() == expected_rng.getstate()


def test_a_lethal_rebuke_during_the_initial_construct_attack_ends_new_concentration() -> None:
    caster = cleric("char:caster", initiative=30, hp_current=1, hp_max=30)
    reactor = pc(
        "char:reactor",
        initiative=20,
        hp_current=200,
        hp_max=200,
        ac=1,
        zone_id=cell_id(0, 1),
        class_slug="warlock",
        character_level=5,
        charisma=18,
        spells_known=["hellish-rebuke"],
        spell_slots={1: 2},
    )
    handle, live = start([caster, reactor], seed=0)
    register_pending_reaction(
        live,
        reactor.entity_id,
        PlayerIntent(intent_type="ready", spell_id="hellish-rebuke"),
    )
    act(
        handle,
        caster.entity_id,
        intent_type="cast_spell",
        spell_id="spiritual-weapon",
        slot_level=2,
        target_id=reactor.entity_id,
        target_zone_id=cell_id(0, 1),
    )
    assert combatant(live, caster.entity_id).hp_current == 0
    assert caster.entity_id not in live.concentration_chain
    assert combatant(live, caster.entity_id).concentration_effect_id is None
    assert not live.constructs


def _dc_bonus_fixture(bonus: str):
    reactor = pc(
        "char:dc-reactor",
        initiative=30,
        class_slug="wizard",
        character_level=5,
        intelligence=16,
        spells_known=["counterspell", "shield"],
        spell_slots={1: 2, 3: 2},
    )
    handle, live = start([reactor], seed=17)
    effect = ActiveEffect(
        id="effect:spell-dc-bonus",
        name="Spell DC bonus",
        origin="fixture",
        target_id=reactor.entity_id,
        changes=[ActiveEffectChange(key="system.bonuses.spell.dc", mode="add", value=bonus)],
    )
    _emit(live, EffectApplied(effect=effect))
    opportunity = ReactionOpportunity(
        ReactionTriggerKind.SEES_SPELL_CAST,
        triggering_actor_id="mon:foe",
        triggering_spell_slug="fireball",
    )
    return handle, live, reactor, opportunity


def test_dice_spell_dc_bonus_is_draw_free_until_a_reaction_actually_fires() -> None:
    _handle, live, reactor, opportunity = _dc_bonus_fixture("1d4")
    before_rng = live.rng.getstate()
    register_pending_reaction(
        live, reactor.entity_id, PlayerIntent(intent_type="ready", spell_id="counterspell")
    )
    [pending] = live.pending_reactions
    before_slots = live.spell_slots_by_entity[reactor.entity_id].copy()
    assert (
        live_reactions._eligible(live, combatant(live, reactor.entity_id), pending, opportunity)
        is not None
    )
    assert live.rng.getstate() == before_rng
    assert live.spell_slots_by_entity[reactor.entity_id] == before_slots
    assert combatant(live, reactor.entity_id).reaction_available is True
    assert live.pending_reactions == [pending]

    before_events = len(live.event_log)
    fire_reaction(live, opportunity)
    [save] = [event for event in live.event_log[before_events:] if isinstance(event, SaveRolled)]
    expected_rng = random.Random(17)
    expected_bonus = expected_rng.randint(1, 4)
    expected_natural = expected_rng.randint(1, 20)
    assert save.dc == 14 + expected_bonus
    assert save.natural == expected_natural
    assert live.rng.getstate() == expected_rng.getstate()
    assert live.spell_slots_by_entity[reactor.entity_id][3] == before_slots[3] - 1
    assert combatant(live, reactor.entity_id).reaction_available is False


def test_shield_does_not_roll_an_unused_spell_dc_bonus() -> None:
    _handle, live, reactor, _opportunity = _dc_bonus_fixture("1d4")
    before_rng = live.rng.getstate()
    register_pending_reaction(
        live, reactor.entity_id, PlayerIntent(intent_type="ready", spell_id="shield")
    )
    fire_reaction(
        live,
        ReactionOpportunity(
            ReactionTriggerKind.HIT_BY_ATTACK,
            triggering_actor_id="mon:foe",
            affected_target_id=reactor.entity_id,
        ),
    )
    assert len([event for event in live.event_log if isinstance(event, ReactionTriggered)]) == 1
    assert live.rng.getstate() == before_rng


@pytest.mark.parametrize("bonus", ["1d4 +", "mystery(1)", "1d0"])
def test_invalid_spell_dc_bonus_refuses_prearm_without_payment(bonus: str) -> None:
    handle, live, reactor, _opportunity = _dc_bonus_fixture(bonus)
    before_rng = live.rng.getstate()
    before_slots = live.spell_slots_by_entity[reactor.entity_id].copy()
    act(handle, reactor.entity_id, intent_type="ready", spell_id="counterspell")
    [failed] = [event for event in live.event_log if isinstance(event, CastFailed)]
    assert failed.reason == "unsupported_reaction"
    assert live.pending_reactions == []
    assert combatant(live, reactor.entity_id).action_available is True
    assert combatant(live, reactor.entity_id).reaction_available is True
    assert live.spell_slots_by_entity[reactor.entity_id] == before_slots
    assert live.rng.getstate() == before_rng


def test_a_dc_bonus_that_becomes_invalid_leaves_the_pending_reaction_armed() -> None:
    _handle, live, reactor, opportunity = _dc_bonus_fixture("1d4")
    register_pending_reaction(
        live, reactor.entity_id, PlayerIntent(intent_type="ready", spell_id="counterspell")
    )
    [effect] = live.active_effects[reactor.entity_id]
    live.active_effects[reactor.entity_id] = [
        effect.model_copy(
            update={
                "changes": [
                    ActiveEffectChange(key="system.bonuses.spell.dc", mode="add", value="1d4 +")
                ]
            }
        )
    ]
    before_rng = live.rng.getstate()
    before_pending = list(live.pending_reactions)
    before_slots = live.spell_slots_by_entity[reactor.entity_id].copy()
    fire_reaction(live, opportunity)
    assert live.pending_reactions == before_pending
    assert combatant(live, reactor.entity_id).reaction_available is True
    assert live.spell_slots_by_entity[reactor.entity_id] == before_slots
    assert live.rng.getstate() == before_rng
    assert not any(isinstance(event, ReactionTriggered) for event in live.event_log)


def test_invalid_save_ability_refuses_prearm_before_any_payment(monkeypatch) -> None:
    handle, live, reactor, _opportunity = _dc_bonus_fixture("1d4")
    spell = BundledAssetLoader().get_spell("counterspell")
    assert spell is not None
    [activity] = spell.activities
    assert isinstance(activity, SaveActivity)
    invalid = activity.model_copy(
        update={"save": activity.save.model_copy(update={"ability": ["mystery"]})}
    )
    broken_spell = spell.model_copy(update={"activities": [invalid]})
    monkeypatch.setattr(
        live_reactions,
        "get_lib_loader",
        lambda: SimpleNamespace(get_spell=lambda _slug: broken_spell),
    )
    before_rng = live.rng.getstate()
    before_slots = live.spell_slots_by_entity[reactor.entity_id].copy()
    act(handle, reactor.entity_id, intent_type="ready", spell_id="counterspell")
    [failed] = [event for event in live.event_log if isinstance(event, CastFailed)]
    assert failed.reason == "unsupported_reaction"
    assert live.pending_reactions == []
    assert combatant(live, reactor.entity_id).action_available is True
    assert combatant(live, reactor.entity_id).reaction_available is True
    assert live.spell_slots_by_entity[reactor.entity_id] == before_slots
    assert live.rng.getstate() == before_rng


def test_self_inflicted_damage_has_a_real_damage_source_for_rebuke() -> None:
    reactor = pc(
        "char:reactor",
        initiative=30,
        hp_current=100,
        hp_max=100,
        class_slug="warlock",
        character_level=5,
        charisma=16,
        spells_known=["hellish-rebuke"],
        spell_slots={1: 2},
    )
    _handle, live = start([reactor], seed=17)
    register_pending_reaction(
        live, reactor.entity_id, PlayerIntent(intent_type="ready", spell_id="hellish-rebuke")
    )
    actor = combatant(live, reactor.entity_id)
    ctx = attach_reaction_hooks(
        live,
        ActivityResolutionContext(
            rng=live.rng,
            caster=actor,
            targets=[actor],
            caster_abilities={"cha": 16},
            event_emitter=lambda event: _emit(live, event),
        ),
    )
    before = len(live.event_log)
    apply_damage(actor, {"force": 1}, ctx, source_id="self-damage")
    emitted = live.event_log[before:]
    [trigger] = [event for event in emitted if isinstance(event, ReactionTriggered)]
    assert trigger.trigger_kind == ReactionTriggerKind.TAKES_DAMAGE
    assert trigger.triggering_actor_id == reactor.entity_id
    assert trigger.affected_target_id == reactor.entity_id
    initial_damage = next(event for event in emitted if isinstance(event, DamageApplied))
    assert trigger.damage_instance_id == initial_damage.damage_instance_id
    [save] = [event for event in emitted if isinstance(event, SaveRolled)]
    assert save.target_id == reactor.entity_id
    assert combatant(live, reactor.entity_id).hp_current < 99
    assert live.spell_slots_by_entity[reactor.entity_id][1] == 1
    assert combatant(live, reactor.entity_id).reaction_available is False


def test_pending_reaction_owns_an_independent_canonical_snapshot(monkeypatch) -> None:
    _handle, live, reactor, _opportunity = _dc_bonus_fixture("1d4")
    spell = BundledAssetLoader().get_spell("shield")
    assert spell is not None
    [activity] = spell.activities
    assert activity.reaction is not None
    monkeypatch.setattr(
        live_reactions,
        "get_lib_loader",
        lambda: SimpleNamespace(get_spell=lambda _slug: spell),
    )
    register_pending_reaction(
        live, reactor.entity_id, PlayerIntent(intent_type="ready", spell_id="shield")
    )
    [pending] = live.pending_reactions
    assert pending.semantics is not activity.reaction
    assert pending.semantics.responses[0] is not activity.reaction.responses[0]
    assert all(
        snapshot is not original
        for snapshot, original in zip(
            pending.conditions, activity.activation.reaction_conditions, strict=True
        )
    )
    activity.reaction.responses.clear()
    activity.activation.reaction_conditions.clear()
    assert len(pending.semantics.responses) == 1
    assert len(pending.conditions) == 2
