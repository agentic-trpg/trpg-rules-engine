"""Canonical pre-arm queue contracts and deterministic initiative-order firing."""

from __future__ import annotations

import asyncio

import pytest
from dnd5e_srd_data.schema.common import ReactionTriggerKind
from pydantic import ValidationError

from dnd5e_engine import PlayerIntent
from dnd5e_engine.events import ReactionTriggered, TurnStarted
from dnd5e_engine.live_reactions import fire_reaction, register_pending_reaction
from dnd5e_engine.orchestrator import _get_live, start_combat, submit_player_intent
from dnd5e_engine.reactions import ReactionOpportunity
from dnd5e_engine.spatial import cell_id
from dnd5e_engine.specs import EncounterMemberSpec, GridScene, PartyMemberSpec


def _start(session_id: str, rng_seed: int = 1):
    async def _run():
        start = await start_combat(
            session_id=session_id,
            party=[
                PartyMemberSpec(
                    entity_id="char:alpha",
                    name="Alpha",
                    initiative=30,
                    hp_current=20,
                    hp_max=20,
                    intelligence=18,
                    class_slug="wizard",
                    character_level=5,
                    spells_known=["counterspell", "shield"],
                    spell_slots={1: 2, 3: 1},
                    zone_id=cell_id(0, 0),
                ),
                PartyMemberSpec(
                    entity_id="char:beta",
                    name="Beta",
                    initiative=20,
                    hp_current=20,
                    hp_max=20,
                    intelligence=18,
                    class_slug="wizard",
                    character_level=5,
                    spells_known=["counterspell", "fireball"],
                    spell_slots={1: 2, 3: 2},
                    zone_id=cell_id(0, 1),
                ),
            ],
            encounter=[
                EncounterMemberSpec(
                    entity_id="mon:target",
                    entity_type="Monster",
                    name="Target",
                    initiative=1,
                    hp_current=50,
                    hp_max=50,
                    ac=13,
                    zone_id=cell_id(6, 0),
                )
            ],
            grid_scene=GridScene(width=7, height=2),
            rng_seed=rng_seed,
        )
        return start.handle

    return asyncio.run(_run())


def _arm(live, owner: str, spell: str) -> None:
    register_pending_reaction(live, owner, PlayerIntent(intent_type="ready", spell_id=spell))


def _opportunity(kind, actor="mon:target", affected=None):
    return ReactionOpportunity(kind=kind, triggering_actor_id=actor, affected_target_id=affected)


def _fired(live):
    return [e for e in live.event_log if isinstance(e, ReactionTriggered)]


def test_ready_intent_registers_pending_reaction_and_spends_action():
    handle = _start("rq-register")
    live = _get_live(handle)
    before_rng = live.rng.getstate()
    asyncio.run(
        submit_player_intent(
            handle,
            actor_id="char:alpha",
            intent=PlayerIntent(intent_type="ready", spell_id="counterspell", slot_level=3),
        )
    )
    [pending] = live.pending_reactions
    assert pending.owner_id == "char:alpha"
    assert pending.spell_id == "counterspell"
    assert pending.slot_level == 3
    assert tuple(c.kind for c in pending.conditions) == (ReactionTriggerKind.SEES_SPELL_CAST,)
    alpha = next(c for c in live.initiative if c.entity_id == "char:alpha")
    assert alpha.action_available is False
    assert alpha.reaction_available is True
    assert live.spell_slots_by_entity["char:alpha"][3] == 1
    assert live.rng.getstate() == before_rng
    assert not _fired(live)
    assert [e for e in live.event_log if isinstance(e, TurnStarted)][-1].actor_id == "char:beta"


def test_register_replaces_prior_entry_for_same_owner():
    live = _get_live(_start("rq-replace"))
    _arm(live, "char:alpha", "counterspell")
    _arm(live, "char:alpha", "shield")
    [pending] = live.pending_reactions
    assert pending.spell_id == "shield"
    assert tuple(c.kind for c in pending.conditions) == (
        ReactionTriggerKind.HIT_BY_ATTACK,
        ReactionTriggerKind.TARGETED_BY_SPELL,
    )


def test_register_automatically_reads_conditions_and_ignores_other_intents():
    live = _get_live(_start("rq-no-host-trigger"))
    register_pending_reaction(
        live,
        "char:alpha",
        PlayerIntent(intent_type="cast_spell", spell_id="shield"),
    )
    assert live.pending_reactions == []
    _arm(live, "char:alpha", "shield")
    assert len(live.pending_reactions) == 1
    assert len(live.pending_reactions[0].conditions) == 2


def test_firing_scans_initiative_order_and_excludes_triggering_actor():
    live = _get_live(_start("rq-order"))
    _arm(live, "char:alpha", "counterspell")
    _arm(live, "char:beta", "counterspell")
    fire_reaction(live, _opportunity(ReactionTriggerKind.SEES_SPELL_CAST, "char:alpha"))
    assert [e.actor_id for e in _fired(live)] == ["char:beta"]
    assert [p.owner_id for p in live.pending_reactions] == ["char:alpha"]
    fire_reaction(live, _opportunity(ReactionTriggerKind.SEES_SPELL_CAST))
    assert [e.actor_id for e in _fired(live)] == ["char:beta", "char:alpha"]
    assert live.pending_reactions == []
    before = live.rng.getstate()
    fire_reaction(live, _opportunity(ReactionTriggerKind.SEES_SPELL_CAST))
    assert len(_fired(live)) == 2
    assert live.rng.getstate() == before


def test_firing_respects_reaction_economy_and_affected_owner_scope():
    live = _get_live(_start("rq-economy"))
    _arm(live, "char:alpha", "shield")
    before_rng = live.rng.getstate()
    before_slots = live.spell_slots_by_entity["char:alpha"].copy()
    fire_reaction(live, _opportunity(ReactionTriggerKind.HIT_BY_ATTACK, affected="char:beta"))
    fire_reaction(live, _opportunity(ReactionTriggerKind.SEES_SPELL_CAST))
    idx = next(i for i, c in enumerate(live.initiative) if c.entity_id == "char:alpha")
    live.initiative[idx] = live.initiative[idx].model_copy(update={"reaction_available": False})
    fire_reaction(live, _opportunity(ReactionTriggerKind.HIT_BY_ATTACK, affected="char:alpha"))
    assert len(live.pending_reactions) == 1
    assert not _fired(live)
    assert live.rng.getstate() == before_rng
    assert live.spell_slots_by_entity["char:alpha"] == before_slots
    live.initiative[idx] = live.initiative[idx].model_copy(update={"reaction_available": True})
    fire_reaction(live, _opportunity(ReactionTriggerKind.HIT_BY_ATTACK, affected="char:alpha"))
    assert [e.actor_id for e in _fired(live)] == ["char:alpha"]
    assert not live.pending_reactions
    assert live.spell_slots_by_entity["char:alpha"][1] == 1


def test_reaction_trigger_rejects_unknown_values():
    with pytest.raises(ValidationError):
        PlayerIntent(intent_type="ready", spell_id="shield", reaction_trigger="interpretive_dance")


def test_counterspell_reactor_slot_spent_regardless_of_outcome():
    handle = _start("rq-reactor-slot", rng_seed=9)
    live = _get_live(handle)

    async def _script():
        await submit_player_intent(
            handle,
            actor_id="char:alpha",
            intent=PlayerIntent(intent_type="ready", spell_id="counterspell", slot_level=3),
        )
        await submit_player_intent(
            handle,
            actor_id="char:beta",
            intent=PlayerIntent(
                intent_type="cast_spell", spell_id="fireball", slot_level=3, target_id="mon:target"
            ),
        )

    asyncio.run(_script())
    assert live.spell_slots_by_entity["char:alpha"][3] == 0
    assert live.spell_slots_by_entity["char:beta"][3] == 1
    alpha = next(c for c in live.initiative if c.entity_id == "char:alpha")
    assert alpha.reaction_available is False
    assert live.pending_reactions == []
