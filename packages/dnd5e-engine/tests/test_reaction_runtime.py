"""Data-driven reactions through public turns and shared live resolution boundaries."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from random import Random

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.common import CastActivity, ReactionTriggerKind
from pydantic import BaseModel

from dnd5e_engine import ActiveEffect, PlayerIntent
from dnd5e_engine.activities.apply import apply_damage
from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.events import (
    AttackRolled,
    CastFailed,
    DamageApplied,
    EffectApplied,
    LegendaryActionUsed,
    ReactionTriggered,
    SaveRolled,
    SpellCast,
)
from dnd5e_engine.live_reactions import (
    attach_reaction_hooks,
    fire_reaction,
    register_pending_reaction,
)
from dnd5e_engine.orchestrator import (
    _emit,
    _resolve_monster_cast,
    _update_combatant,
    advance_monster_turn,
)
from dnd5e_engine.reaction_audit import audit_document
from dnd5e_engine.reactions import ReactionOpportunity, matching_conditions, reaction_target_id
from dnd5e_engine.spatial import GridTopology
from dnd5e_engine.specs import GridScene
from tests.c21_support import act, cleric, combatant, events, foe, monster_turn, pc, start

LOADER = BundledAssetLoader()
REACTOR = "char:reactor"
ATTACKER = "char:attacker"


def reactor(**changes):
    entity_id = changes.pop("entity_id", REACTOR)
    return pc(
        entity_id,
        **(
            {
                "initiative": 30,
                "class_slug": "wizard",
                "character_level": 5,
                "intelligence": 18,
                "charisma": 18,
                "hp_current": 200,
                "hp_max": 200,
                "ac": 12,
                "spells_known": ["shield", "counterspell", "hellish-rebuke", "feather-fall"],
                "spell_slots": {1: 3, 3: 2},
            }
            | changes
        ),
    )


def attacker(**changes):
    return pc(
        ATTACKER,
        **(
            {
                "initiative": 20,
                "zone_id": "0,1",
                "class_slug": "wizard",
                "intelligence": 16,
                "attack_bonus": 4,
                "hp_current": 200,
                "hp_max": 200,
                "equipment": ("longsword",),
                "spells_known": ["fire-bolt", "magic-missile", "fireball"],
                "spell_slots": {1: 2, 3: 2},
            }
            | changes
        ),
    )


def arm(handle, owner, spell):
    act(handle, owner, intent_type="ready", spell_id=spell)


def _opportunity(kind, *, affected=REACTOR, actor=ATTACKER, **changes):
    return ReactionOpportunity(
        kind=kind, triggering_actor_id=actor, affected_target_id=affected, **changes
    )


def _snapshot(live):
    def normalize(value):
        if isinstance(value, BaseModel):
            return normalize(value.model_dump(mode="json"))
        if dataclasses.is_dataclass(value):
            return normalize(dataclasses.asdict(value))
        if isinstance(value, dict):
            return {str(k): normalize(v) for k, v in value.items()}
        if isinstance(value, (set, frozenset)):
            return sorted(normalize(v) for v in value)
        if isinstance(value, (list, tuple)):
            return [normalize(v) for v in value]
        return value

    state = (
        live.pending_reactions,
        live.spell_slots_by_entity,
        live.pact_slots_by_entity,
        live.initiative,
        live.active_effects,
        live.tracked_hp,
        live.tracked_temp_hp,
        live.rng.getstate(),
    )
    return json.dumps(normalize(state), sort_keys=True).encode()


@pytest.mark.parametrize(
    ("seed", "fires", "hit"), [(1, False, False), (7, True, False), (9, True, True)]
)
def test_shield_adjudicates_weapon_attack_once_with_original_roll(seed, fires, hit):
    handle, live = start([reactor(), attacker()], seed=seed)
    arm(handle, REACTOR, "shield")
    expected = Random()
    expected.setstate(live.rng.getstate())
    natural = expected.randint(1, 20)
    act(handle, ATTACKER, intent_type="attack", weapon_id="longsword", target_id=REACTOR)
    [attack] = events(live, AttackRolled)
    assert (attack.natural, attack.roll_total, attack.is_hit) == (natural, natural + 4, hit)
    assert bool(events(live, ReactionTriggered)) is fires
    assert live.spell_slots_by_entity[REACTOR][1] == 3 - int(fires)
    if not hit:
        assert not events(live, DamageApplied)
        assert live.rng.getstate() == expected.getstate()
    if fires:
        [reaction] = events(live, ReactionTriggered)
        assert reaction.trigger_kind == ReactionTriggerKind.HIT_BY_ATTACK
        assert (reaction.triggering_actor_id, reaction.affected_target_id) == (ATTACKER, REACTOR)
        assert live.event_log.index(reaction) < live.event_log.index(attack)
        assert any(e.effect.target_id == REACTOR for e in events(live, EffectApplied))
    else:
        assert len(live.pending_reactions) == 1


def test_shield_advantage_attack_draws_two_d20s_without_reroll():
    effect = ActiveEffect(
        id="effect:prone",
        name="Prone",
        origin="test:reaction",
        target_id=REACTOR,
        statuses={"prone"},
    )
    handle, live = start([reactor(), attacker()], seed=7, active_effects=[effect])
    arm(handle, REACTOR, "shield")
    expected = Random()
    expected.setstate(live.rng.getstate())
    natural = max(expected.randint(1, 20), expected.randint(1, 20))
    act(handle, ATTACKER, intent_type="attack", weapon_id="longsword", target_id=REACTOR)
    [attack] = events(live, AttackRolled)
    assert (attack.advantage, attack.natural, attack.is_hit) == ("advantage", natural, False)
    assert len(events(live, ReactionTriggered)) == 1
    assert not events(live, DamageApplied)
    assert live.rng.getstate() == expected.getstate()


def test_pc_spell_attack_uses_the_shared_shield_hit_hook():
    handle, live = start([reactor(), attacker(attack_bonus=5)], seed=7)
    arm(handle, REACTOR, "shield")
    expected = Random()
    expected.setstate(live.rng.getstate())
    natural = expected.randint(1, 20)
    act(handle, ATTACKER, intent_type="cast_spell", spell_id="fire-bolt", target_id=REACTOR)
    [attack] = events(live, AttackRolled)
    assert (attack.natural, attack.roll_total, attack.is_hit) == (natural, natural + 5, False)
    assert [e.reaction_name for e in events(live, ReactionTriggered)] == ["shield"]
    assert not events(live, DamageApplied)
    assert live.rng.getstate() == expected.getstate()


def test_monster_ordinary_attack_uses_the_shared_shield_hook():
    handle, live = start(
        [reactor()], seed=7, encounter=[foe(monster_template_slug="goblin-warrior", attack_bonus=4)]
    )
    arm(handle, REACTOR, "shield")
    monster_turn(handle)
    [attack] = events(live, AttackRolled)
    assert (attack.natural, attack.roll_total, attack.is_hit) == (11, 15, False)
    assert [e.reaction_name for e in events(live, ReactionTriggered)] == ["shield"]
    assert not events(live, DamageApplied)


def test_monster_save_activity_never_fires_hit_reaction():
    handle, live = start([reactor()], seed=7, encounter=[foe(monster_template_slug="magma-mephit")])
    arm(handle, REACTOR, "shield")
    monster_turn(handle)
    assert events(live, SaveRolled)
    assert not events(live, AttackRolled)
    assert not events(live, ReactionTriggered)
    assert live.spell_slots_by_entity[REACTOR][1] == 3
    assert len(live.pending_reactions) == 1


def test_monster_spell_attack_reaches_the_same_shield_hook():
    slug = "adult-bronze-dragon"
    handle, live = start(
        [reactor(ac=18)], seed=7, encounter=[foe(monster_template_slug=slug, zone_id="2,0")]
    )
    arm(handle, REACTOR, "shield")
    monster = LOADER.get_monster(slug)
    action = next(a for a in monster.actions if a.slug == "spellcasting")
    activity = next(
        a
        for a in action.activities
        if isinstance(a, CastActivity) and a.spell.uuid.endswith("phbsplGuidingBol")
    )
    spell = LOADER.get_spell("guiding-bolt")
    _resolve_monster_cast(
        live, combatant(live, "mon:foe"), combatant(live, REACTOR), action, activity, spell
    )
    [attack] = events(live, AttackRolled)
    assert attack.attacker_id == "mon:foe"
    assert attack.natural == 11
    assert not attack.is_hit
    assert [e.reaction_name for e in events(live, ReactionTriggered)] == ["shield"]
    assert not events(live, DamageApplied)


def test_spiritual_weapon_construct_attack_uses_one_shield_adjudication():
    handle, live = start([reactor(ac=13), cleric(initiative=20, zone_id="0,1")], seed=7)
    arm(handle, REACTOR, "shield")
    expected = Random()
    expected.setstate(live.rng.getstate())
    natural = expected.randint(1, 20)
    act(
        handle,
        "char:cleric",
        intent_type="cast_spell",
        spell_id="spiritual-weapon",
        slot_level=2,
        target_id=REACTOR,
    )
    [attack] = events(live, AttackRolled)
    assert (attack.attacker_id, attack.natural, attack.roll_total, attack.is_hit) == (
        "char:cleric",
        natural,
        natural + 6,
        False,
    )
    [reaction] = events(live, ReactionTriggered)
    assert reaction.trigger_kind == ReactionTriggerKind.HIT_BY_ATTACK
    assert not events(live, DamageApplied)
    assert live.spell_slots_by_entity[REACTOR][1] == 2
    assert not combatant(live, REACTOR).reaction_available
    assert live.rng.getstate() == expected.getstate()


def test_legendary_spell_attack_uses_one_shield_adjudication():
    handle, live = start(
        [reactor(ac=18)],
        seed=7,
        encounter=[foe(monster_template_slug="adult-bronze-dragon", zone_id="2,0")],
    )
    arm(handle, REACTOR, "shield")
    expected = Random()
    expected.setstate(live.rng.getstate())
    natural = expected.randint(1, 20)
    asyncio.run(advance_monster_turn(handle, legendary=True, actor_id="mon:foe"))
    [used] = events(live, LegendaryActionUsed)
    assert used.actor_id == "mon:foe"
    [attack] = events(live, AttackRolled)
    assert (attack.attacker_id, attack.natural, attack.roll_total, attack.is_hit) == (
        "mon:foe",
        natural,
        natural + 10,
        False,
    )
    [reaction] = events(live, ReactionTriggered)
    assert reaction.trigger_kind == ReactionTriggerKind.HIT_BY_ATTACK
    assert not events(live, DamageApplied)
    assert live.spell_slots_by_entity[REACTOR][1] == 2
    assert not combatant(live, REACTOR).reaction_available
    assert live.rng.getstate() == expected.getstate()


@pytest.mark.parametrize("compatibility", [None, "hit_by_attack", "targeted_by_magic_missile"])
def test_shield_magic_missile_uses_qualified_targeted_spell_and_no_permanent_immunity(
    compatibility,
):
    handle, live = start([reactor(), attacker()], seed=7)
    act(handle, REACTOR, intent_type="ready", spell_id="shield", reaction_trigger=compatibility)
    act(handle, ATTACKER, intent_type="cast_spell", spell_id="magic-missile", target_id=REACTOR)
    [reaction] = events(live, ReactionTriggered)
    assert reaction.trigger_kind == ReactionTriggerKind.TARGETED_BY_SPELL
    assert reaction.source_spell_id == "shield"
    assert reaction.source_activity_id
    assert (reaction.triggering_actor_id, reaction.affected_target_id) == (ATTACKER, REACTOR)
    damage = events(live, DamageApplied)
    assert damage
    assert all(e.amount == 0 for e in damage)
    assert combatant(live, REACTOR).hp_current == 200
    assert "force" not in combatant(live, REACTOR).damage_immunities


def test_shield_targeted_condition_does_not_match_another_spell():
    handle, live = start([reactor(), attacker()], seed=7)
    arm(handle, REACTOR, "shield")
    before = _snapshot(live)
    fire_reaction(
        live, _opportunity(ReactionTriggerKind.TARGETED_BY_SPELL, triggering_spell_slug="fireball")
    )
    assert _snapshot(live) == before
    assert not events(live, ReactionTriggered)


@pytest.mark.parametrize("spell,negated", [("magic-missile", True), ("eldritch-blast", False)])
def test_active_shield_response_covers_later_magic_missile_only(spell, negated):
    caster = attacker(
        initiative=10, zone_id="1,1", attack_bonus=20, spells_known=[spell]
    ).model_copy(update={"entity_id": "char:caster"})
    handle, live = start([reactor(), attacker(), caster], seed=7)
    arm(handle, REACTOR, "shield")
    act(handle, ATTACKER, intent_type="attack", weapon_id="longsword", target_id=REACTOR)
    assert not events(live, AttackRolled)[0].is_hit
    act(handle, "char:caster", intent_type="cast_spell", spell_id=spell, target_id=REACTOR)
    assert len(events(live, ReactionTriggered)) == 1
    damage = [e for e in events(live, DamageApplied) if e.target_id == REACTOR]
    assert damage
    assert all((e.amount == 0) is negated for e in damage)
    assert live.spell_slots_by_entity[REACTOR][1] == 2


@pytest.mark.parametrize("spell", ["fireball", "feather-fall", "unknown-reaction"])
def test_unsupported_prearm_refuses_before_action_slot_or_rng(spell):
    handle, live = start([reactor(spells_known=[spell])], seed=7)
    before = _snapshot(live)
    arm(handle, REACTOR, spell)
    assert _snapshot(live) == before
    assert events(live, CastFailed)
    assert not live.pending_reactions
    assert live.current_actor_id == REACTOR


def test_host_trigger_cannot_change_canonical_reaction_conditions():
    handle, live = start([reactor()], seed=7)
    before = _snapshot(live)
    act(handle, REACTOR, intent_type="ready", spell_id="shield", reaction_trigger="cast_spell")
    assert _snapshot(live) == before
    assert events(live, CastFailed)
    assert not live.pending_reactions


def test_counterspell_observes_a_real_monster_cast_before_its_effects():
    handle, live = start(
        [reactor()], seed=1, encounter=[foe(monster_template_slug="mage", zone_id="2,0")]
    )
    arm(handle, REACTOR, "counterspell")
    expected = Random()
    expected.setstate(live.rng.getstate())
    expected.randint(1, 20)
    monster_turn(handle)
    [reaction] = events(live, ReactionTriggered)
    assert (reaction.reaction_name, reaction.trigger_kind, reaction.triggering_actor_id) == (
        "counterspell",
        ReactionTriggerKind.SEES_SPELL_CAST,
        "mon:foe",
    )
    [save] = events(live, SaveRolled)
    assert (save.target_id, save.ability, save.succeeded) == ("mon:foe", "con", False)
    assert events(live, CastFailed)[-1].reason == "countered"
    assert not events(live, DamageApplied)
    assert [e.spell_id for e in events(live, SpellCast)] == ["counterspell"]
    assert live.spell_slots_by_entity[REACTOR][3] == 1
    assert live.rng.getstate() == expected.getstate()


@pytest.mark.parametrize("spell", ["counterspell", "hellish-rebuke"])
@pytest.mark.parametrize(
    "blocked", ["range", "blinded", "slot", "reaction", "incapacitated", "zero_hp", "dead"]
)
def test_ineligible_candidate_stays_armed_without_payment_or_rng(blocked, spell):
    handle, live = start([reactor(), attacker()], seed=7)
    arm(handle, REACTOR, spell)
    if blocked == "range":
        live.topology = GridTopology(GridScene(width=30, height=30))
        live.actor_zone[REACTOR] = "20,20"
        live.actor_zone[ATTACKER] = "0,0"
    elif blocked == "slot":
        live.spell_slots_by_entity[REACTOR][3 if spell == "counterspell" else 1] = 0
    elif blocked == "reaction":
        _update_combatant(live, REACTOR, reaction_available=False)
    elif blocked == "zero_hp":
        _update_combatant(live, REACTOR, hp_current=0)
    elif blocked == "dead":
        _update_combatant(live, REACTOR, is_alive=False)
    else:
        status = "blinded" if blocked == "blinded" else "incapacitated"
        _emit(
            live,
            EffectApplied(
                effect=ActiveEffect(
                    id=f"effect:{status}",
                    name=status,
                    origin="test:reaction",
                    target_id=REACTOR,
                    statuses={status},
                )
            ),
        )
    before = _snapshot(live)
    kind = (
        ReactionTriggerKind.SEES_SPELL_CAST
        if spell == "counterspell"
        else ReactionTriggerKind.TAKES_DAMAGE
    )
    fire_reaction(live, _opportunity(kind, damage_source_actor_id=ATTACKER))
    assert _snapshot(live) == before
    assert not events(live, ReactionTriggered)


def test_ineligible_first_counterspeller_is_skipped_in_initiative_order():
    second = reactor(entity_id="char:second", initiative=25, zone_id="1,1")
    _, live = start([reactor(), second, attacker()], seed=1)
    register_pending_reaction(
        live, "char:second", PlayerIntent(intent_type="ready", spell_id="counterspell")
    )
    register_pending_reaction(
        live, REACTOR, PlayerIntent(intent_type="ready", spell_id="counterspell")
    )
    live.spell_slots_by_entity[REACTOR][3] = 0
    fire_reaction(live, _opportunity(ReactionTriggerKind.SEES_SPELL_CAST))
    assert [e.actor_id for e in events(live, ReactionTriggered)] == ["char:second"]
    assert [p.owner_id for p in live.pending_reactions] == [REACTOR]
    assert live.spell_slots_by_entity["char:second"][3] == 1


def test_first_eligible_counterspeller_fires_in_initiative_not_registration_order():
    second = reactor(entity_id="char:second", initiative=25, zone_id="1,1")
    _, live = start([reactor(), second, attacker()], seed=1)
    for owner_id in ("char:second", REACTOR):
        register_pending_reaction(
            live, owner_id, PlayerIntent(intent_type="ready", spell_id="counterspell")
        )
    fire_reaction(live, _opportunity(ReactionTriggerKind.SEES_SPELL_CAST))
    assert [e.actor_id for e in events(live, ReactionTriggered)] == [REACTOR]
    assert [p.owner_id for p in live.pending_reactions] == ["char:second"]
    assert live.spell_slots_by_entity["char:second"][3] == 2


def test_reaction_and_shield_effect_refresh_at_owner_next_turn_start():
    handle, live = start([reactor(), attacker()], seed=7)
    arm(handle, REACTOR, "shield")
    act(handle, ATTACKER, intent_type="attack", weapon_id="longsword", target_id=REACTOR)
    assert not combatant(live, REACTOR).reaction_available
    assert any(e.name == "Imperceptible Barrier" for e in live.active_effects[REACTOR])
    act(handle, "mon:foe", intent_type="pass")
    assert live.current_actor_id == REACTOR
    assert combatant(live, REACTOR).reaction_available
    assert not any(e.name == "Imperceptible Barrier" for e in live.active_effects.get(REACTOR, []))


def test_hellish_rebuke_fires_after_positive_damage_on_its_actual_source():
    handle, live = start([reactor(), attacker(attack_bonus=20)], seed=7)
    arm(handle, REACTOR, "hellish-rebuke")
    act(handle, ATTACKER, intent_type="attack", weapon_id="longsword", target_id=REACTOR)
    [reaction] = events(live, ReactionTriggered)
    incoming = next(e for e in events(live, DamageApplied) if e.target_id == REACTOR)
    returned = [e for e in events(live, DamageApplied) if e.target_id == ATTACKER]
    spell = LOADER.get_spell("hellish-rebuke")
    assert spell is not None
    assert returned[0].source_id == f"spell:hellish-rebuke:{spell.activities[0].id}"
    assert incoming.amount > 0
    assert incoming.source_actor_id == ATTACKER
    assert returned
    assert all(e.source_actor_id == REACTOR for e in returned)
    assert reaction.trigger_kind == ReactionTriggerKind.TAKES_DAMAGE
    assert reaction.triggering_actor_id == ATTACKER
    assert reaction.damage_instance_id == incoming.damage_instance_id
    assert live.event_log.index(incoming) < live.event_log.index(reaction)
    assert {e.target_id for e in events(live, SaveRolled)} == {ATTACKER}
    assert live.spell_slots_by_entity[REACTOR][1] == 2
    assert not combatant(live, REACTOR).reaction_available


def test_hellish_rebuke_does_not_fire_for_damage_immunity_zero_damage():
    handle, live = start(
        [reactor(damage_immunities=["slashing"]), attacker(attack_bonus=20)], seed=7
    )
    arm(handle, REACTOR, "hellish-rebuke")
    act(handle, ATTACKER, intent_type="attack", weapon_id="longsword", target_id=REACTOR)
    assert events(live, DamageApplied)
    assert all(e.amount == 0 for e in events(live, DamageApplied))
    assert not events(live, ReactionTriggered)
    assert len(live.pending_reactions) == 1
    assert live.spell_slots_by_entity[REACTOR][1] == 3


def _apply_instance(live, target_id, amounts, *, source_id=ATTACKER, crit=False):
    target, source = combatant(live, target_id), combatant(live, source_id)
    context = build_activity_context(
        source,
        [target],
        rng=live.rng,
        event_emitter=lambda event: _emit(live, event),
        slot_level=None,
        base_spell_level=None,
        spellcasting_ability=None,
        concentration=False,
        source_passive_effects=[],
        spell_book={},
        passive_damage_modifiers={},
        save_modifiers={},
    )
    context = attach_reaction_hooks(live, context)
    return apply_damage(target, amounts, context, source_id="test:multi-part", is_crit=crit)


def test_multi_type_damage_folds_all_parts_before_one_hellish_rebuke():
    handle, live = start([reactor(), attacker()], seed=7)
    arm(handle, REACTOR, "hellish-rebuke")
    _apply_instance(live, REACTOR, {"slashing": 3, "fire": 4})
    incoming = [e for e in events(live, DamageApplied) if e.target_id == REACTOR]
    [reaction] = events(live, ReactionTriggered)
    assert len(incoming) == 2
    assert incoming[0].damage_instance_id == incoming[1].damage_instance_id
    assert incoming[0].damage_instance_id == reaction.damage_instance_id
    assert all(e.source_actor_id == ATTACKER for e in incoming)
    assert all(live.event_log.index(e) < live.event_log.index(reaction) for e in incoming)
    assert combatant(live, REACTOR).hp_current == 193
    assert live.spell_slots_by_entity[REACTOR][1] == 2


def test_multi_type_damage_dropping_reactor_prevents_premature_hellish_rebuke():
    handle, live = start([reactor(hp_current=5), attacker()], seed=7)
    arm(handle, REACTOR, "hellish-rebuke")
    before_rng = live.rng.getstate()
    _apply_instance(live, REACTOR, {"slashing": 3, "fire": 4})
    assert combatant(live, REACTOR).hp_current == 0
    assert not events(live, ReactionTriggered)
    assert live.spell_slots_by_entity[REACTOR][1] == 3
    assert live.rng.getstate() == before_rng


@pytest.mark.parametrize("crit,failures", [(False, 1), (True, 2)])
def test_multi_type_damage_at_zero_hp_charges_one_damage_instance(crit, failures):
    _, live = start([reactor(hp_current=0), attacker()], seed=7)
    _apply_instance(live, REACTOR, {"slashing": 2, "fire": 3}, crit=crit)
    assert combatant(live, REACTOR).death_saves["failures"] == failures


def test_hellish_rebuke_targets_the_off_turn_opportunity_attacker():
    handle, live = start(
        [reactor(), attacker(initiative=1, zone_id="9,9")],
        seed=9,
        encounter=[foe(monster_template_slug="goblin-warrior", attack_bonus=20)],
    )
    arm(handle, REACTOR, "hellish-rebuke")
    act(handle, "mon:foe", intent_type="pass")
    act(handle, ATTACKER, intent_type="pass")
    act(handle, REACTOR, intent_type="move", target_zone_id="0,2")
    [attack] = [e for e in events(live, AttackRolled) if e.is_opportunity_attack]
    assert attack.attacker_id == "mon:foe"
    incoming = next(e for e in events(live, DamageApplied) if e.target_id == REACTOR)
    [reaction] = events(live, ReactionTriggered)
    assert incoming.source_actor_id == "mon:foe"
    assert reaction.triggering_actor_id == "mon:foe"
    assert {e.target_id for e in events(live, SaveRolled)} == {"mon:foe"}
    assert all(
        e.source_actor_id == REACTOR
        for e in events(live, DamageApplied)
        if e.target_id == "mon:foe"
    )


def test_dead_pending_owner_is_cleaned_up_after_real_damage():
    handle, live = start([reactor(hp_current=5, hp_max=5), attacker()], seed=7)
    arm(handle, REACTOR, "shield")
    _apply_instance(live, REACTOR, {"fire": 20})
    assert not combatant(live, REACTOR).is_alive
    assert not live.pending_reactions


@pytest.mark.parametrize(
    "spell,kind,expected",
    [
        ("shield", ReactionTriggerKind.HIT_BY_ATTACK, REACTOR),
        ("counterspell", ReactionTriggerKind.SEES_SPELL_CAST, ATTACKER),
        ("hellish-rebuke", ReactionTriggerKind.TAKES_DAMAGE, "mon:foe"),
    ],
)
def test_reaction_target_derivation_uses_typed_role(spell, kind, expected):
    _, live = start([reactor(), attacker()], seed=7)
    register_pending_reaction(live, REACTOR, PlayerIntent(intent_type="ready", spell_id=spell))
    [pending] = live.pending_reactions
    opportunity = _opportunity(kind, damage_source_actor_id="mon:foe")
    assert matching_conditions(pending, opportunity)
    assert reaction_target_id(pending, opportunity) == expected


def test_reaction_audit_is_deterministic_and_matches_reviewed_artifact():
    actual = audit_document(LOADER)
    assert audit_document(LOADER) == actual
    path = Path(__file__).resolve().parents[3] / "docs/dev/reaction-runtime-audit.json"
    assert json.loads(path.read_text(encoding="utf8")) == actual
    assert sum(actual["counts"].values()) == len(actual["rows"])
    for row in actual["rows"]:
        assert row["classification"] in {
            "executable",
            "typed_but_no_opportunity_producer",
            "unsupported_deferred",
        }
        if row["fully_executable"]:
            assert row["producer_exists"]
            assert row["target_derivation_exists"]
            assert row["deferred_reason"] is None
        else:
            assert row["deferred_reason"]


@pytest.mark.parametrize("spell", ["shield", "counterspell", "hellish-rebuke"])
def test_reaction_replay_is_byte_equivalent_across_events_queue_and_rules_state(spell):
    def run():
        handle, live = start(
            [reactor(), attacker(attack_bonus=20 if spell == "hellish-rebuke" else 4)], seed=7
        )
        arm(handle, REACTOR, spell)
        if spell == "counterspell":
            act(
                handle,
                ATTACKER,
                intent_type="cast_spell",
                spell_id="magic-missile",
                target_id=REACTOR,
            )
        else:
            act(handle, ATTACKER, intent_type="attack", weapon_id="longsword", target_id=REACTOR)
        return json.dumps(
            [e.model_dump(mode="json") for e in live.event_log], sort_keys=True
        ).encode(), _snapshot(live)

    assert run() == run()
