"""Reviewed identities are bound to the complete runtime spell payload."""

from __future__ import annotations

import copy

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd5e_srd_data.schema.common import AppliedEffectRef, PassiveEffect

from dnd5e_engine import orchestrator as orch
from dnd5e_engine.events import CastFailed, IntentSubmitted, ReactionTriggered, SpellCast
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.spell_execution import spell_review
from tests.c20_support import act, combatant, events, start, wizard
from tests.e2e.harness import run_async
from tests.test_delivery_hardening import _state
from tests.test_monster_spell_delivery import _spell_setup
from tests.test_unified_spell_delivery import CASTER, WAND


@pytest.fixture(autouse=True)
def loader():
    bundled = BundledAssetLoader()
    set_lib_loader_for_tests(bundled)
    yield bundled
    set_lib_loader_for_tests(None)


def _tampered_spell(loader, mutation):
    spell = loader.get_spell("fireball").model_copy(deep=True)
    activity = spell.activities[0]
    if mutation == "activity":
        activity.save = activity.save.model_copy(update={"ability": ["wis"]})
    elif mutation == "formula":
        part = activity.damage.parts[0]
        activity.damage = activity.damage.model_copy(
            update={
                "parts": [
                    part.model_copy(
                        update={
                            "custom": part.custom.model_copy(
                                update={"enabled": True, "formula": "20d6"}
                            )
                        }
                    )
                ]
            }
        )
    else:
        spell.passive_effects = [
            PassiveEffect(id="injected", name="Injected", statuses=["paralyzed"])
        ]
        activity.effects = [AppliedEffectRef(id="injected")]
    return spell


@pytest.mark.parametrize("source", ["direct", "item"])
@pytest.mark.parametrize("mutation", ["activity", "effect", "formula"])
def test_public_cast_refuses_same_identity_with_changed_mechanics(loader, source, mutation):
    spell = _tampered_spell(loader, mutation)
    canonical = loader.get_spell(spell.slug)
    assert (spell.slug, spell.foundry_uuid) == (canonical.slug, canonical.foundry_uuid)
    set_lib_loader_for_tests(
        MemoryAssetLoader(
            spells=[spell], items=[loader.get_item(WAND)], classes=[loader.get_class("wizard")]
        )
    )
    handle, live = start(
        [
            wizard(
                CASTER,
                initiative=100,
                spells_known=[spell.slug],
                spell_slots={3: 1},
                equipment=(WAND,),
            )
        ],
        seed=19,
    )
    before = (_state(live), live.rng.getstate(), copy.deepcopy(live.custom_counters_by_entity))
    event_count = len(live.event_log)
    intent = (
        {"intent_type": "cast_spell", "spell_id": spell.slug, "slot_level": 3}
        if source == "direct"
        else {"intent_type": "use_item", "item_id": WAND}
    )
    act(handle, CASTER, target_zone_id="1,0", **intent)
    failures = events(live, CastFailed)
    assert len(failures) == 1
    assert failures[0].reason == "unsupported_activity"
    assert failures[0].execution_failure.code == "unreviewed_spell"
    assert live.event_log[event_count:] == failures
    assert not events(live, IntentSubmitted)
    assert not events(live, SpellCast)
    assert (_state(live), live.rng.getstate(), live.custom_counters_by_entity) == before


def test_review_rechecks_mutable_spell_content_instead_of_caching_identity(loader):
    spell = loader.get_spell("fireball").model_copy(deep=True)
    assert spell_review(spell) is not None
    spell.activities[0].damage = spell.activities[0].damage.model_copy(update={"parts": []})
    assert spell_review(spell) is None


def test_public_monster_refuses_changed_spell_without_daily_use_or_rng(loader):
    live, actor, _, monster, _, _ = _spell_setup(loader, "fireball")
    spell = _tampered_spell(loader, "formula")
    live.ruleset_loader = MemoryAssetLoader(monsters=[monster], spells=[spell])
    set_lib_loader_for_tests(live.ruleset_loader)
    uses = copy.deepcopy(live.monster_action_uses_by_entity[actor.entity_id])
    rng = live.rng.getstate()
    run_async(orch.advance_monster_turn(orch.CombatHandle(live.handle_id)))
    assert not events(live, SpellCast)
    assert live.monster_action_uses_by_entity[actor.entity_id] == uses
    assert actor.action_available
    assert live.rng.getstate() == rng
    assert events(live, IntentSubmitted)[-1].intent_type == "pass"


def test_public_reaction_declaration_refuses_changed_same_identity_payload(loader):
    spell = loader.get_spell("counterspell").model_copy(deep=True)
    activity = spell.activities[0]
    activity.save = activity.save.model_copy(update={"ability": ["wis"]})
    set_lib_loader_for_tests(
        MemoryAssetLoader(spells=[spell], classes=[loader.get_class("wizard")])
    )
    handle, live = start(
        [wizard(CASTER, initiative=100, spells_known=[spell.slug], spell_slots={3: 1})],
        seed=19,
    )
    before = (_state(live), live.rng.getstate())
    offset = len(live.event_log)
    act(handle, CASTER, intent_type="ready", spell_id=spell.slug)
    [failure] = events(live, CastFailed)
    assert failure.reason == "unsupported_reaction"
    assert live.event_log[offset:] == [failure]
    assert not live.pending_reactions
    assert not events(live, ReactionTriggered)
    assert combatant(live, CASTER).reaction_available
    assert (_state(live), live.rng.getstate()) == before
