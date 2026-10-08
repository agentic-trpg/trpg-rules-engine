"""Paid combat casts must have an admitted, explicitly bounded payload."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader

from dnd5e_engine import orchestrator as orch
from dnd5e_engine.events import CastFailed, DamageApplied, IntentSubmitted, SpellCast
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.live_spell_delivery import (
    SpellAdmissionError,
    delivery_activities,
    preflight_delivery,
)
from dnd5e_engine.spell_capability_audit import audit_document
from dnd5e_engine.spell_delivery import SpellDeliverySpec
from dnd5e_engine.spell_execution import admission_failure, spell_reviews
from tests.c20_support import act, combatant, events, start, wizard
from tests.e2e.harness import run_async
from tests.test_delivery_hardening import _state
from tests.test_monster_spell_delivery import _cast_action, _spell_setup
from tests.test_unified_spell_delivery import CASTER, WAND


@pytest.fixture(autouse=True)
def loader():
    bundled = BundledAssetLoader()
    set_lib_loader_for_tests(bundled)
    yield bundled
    set_lib_loader_for_tests(None)


@pytest.mark.parametrize(
    "slug",
    [
        "blur",
        "misty-step",
        "animate-dead",
        "true-polymorph",
        "conjure-animals",
        "dimension-door",
        "slow",
        "haste",
        "shillelagh",
        "delayed-blast-fireball",
        "heal",
        "ensnaring-strike",
        "searing-smite",
        "dominate-person",
        "finger-of-death",
        "harm",
        "sleep",
        "protection-from-poison",
        "stinking-cloud",
        "enthrall",
    ],
)
def test_public_cast_admission_and_payment_contract(loader, slug):
    spell = loader.get_spell(slug)
    handle, live = start(
        [wizard(CASTER, initiative=100, spells_known=[slug], spell_slots={spell.level: 1})],
        seed=19,
    )
    before = (_state(live), live.rng.getstate())
    pre = len(live.event_log)
    act(
        handle,
        CASTER,
        intent_type="cast_spell",
        spell_id=slug,
        slot_level=spell.level,
        target_id="mon:foe",
        target_zone_id="1,0",
    )
    if slug == "slow":
        from dnd5e_engine.events import EffectApplied, SaveRolled

        assert not events(live, CastFailed)
        assert len(events(live, SpellCast)) == len(events(live, SaveRolled)) == 1
        assert live.spell_slots_by_entity[CASTER][spell.level] == 0
        save = events(live, SaveRolled)[0]
        assert len(events(live, EffectApplied)) == (0 if save.succeeded else 1)
        assert not combatant(live, CASTER).action_available
        assert live.rng.getstate() != before[1]
        return
    assert [e.reason for e in events(live, CastFailed)] == ["unsupported_activity"]
    assert live.event_log[pre:] == events(live, CastFailed)
    assert not events(live, IntentSubmitted)
    assert not events(live, SpellCast)
    assert (_state(live), live.rng.getstate()) == before
    assert combatant(live, CASTER).action_available
    assert not live.timed_activities.pending
    assert not live.persistent_areas.areas
    failure = events(live, CastFailed)[0].execution_failure
    assert failure.code == "missing_mechanism"
    assert failure.mechanisms


def test_delegated_unsupported_child_does_not_spend_parent_charge_or_action(loader):
    child = loader.get_spell("misty-step")
    item = loader.get_item(WAND)
    wrapper = item.activities[0]
    wrapper = wrapper.model_copy(
        update={"spell": wrapper.spell.model_copy(update={"uuid": child.foundry_uuid, "level": 3})}
    )
    set_lib_loader_for_tests(
        MemoryAssetLoader(spells=[child], items=[item.model_copy(update={"activities": [wrapper]})])
    )
    handle, live = start(
        [wizard(CASTER, initiative=100, equipment=(WAND,))],
        seed=19,
    )
    before = (_state(live), live.rng.getstate(), copy.deepcopy(live.custom_counters_by_entity))
    act(handle, CASTER, intent_type="use_item", item_id=WAND, target_id="mon:foe")
    assert [e.reason for e in events(live, CastFailed)] == ["unsupported_activity"]
    assert (_state(live), live.rng.getstate(), live.custom_counters_by_entity) == before


@pytest.mark.parametrize("slug", ["fireball", "message", "blur", "conjure-animals"])
def test_pure_shared_preflight_never_changes_combat_or_spell(loader, slug):
    spell = loader.get_spell(slug)
    _, live = start([wizard(CASTER, initiative=100)], seed=19)
    before = (_state(live), live.rng.getstate(), list(live.event_log), spell.model_dump())
    spec = SpellDeliverySpec(primary_target_id="mon:foe", origin_cell="1,0")
    if slug in ("blur", "conjure-animals"):
        with pytest.raises(SpellAdmissionError):
            preflight_delivery(live, combatant(live, CASTER), spell.activities, spec, spell=spell)
    else:
        preflight_delivery(live, combatant(live, CASTER), spell.activities, spec, spell=spell)
    assert (_state(live), live.rng.getstate(), live.event_log, spell.model_dump()) == before


def test_host_narrative_is_explicit_and_does_not_promise_a_mechanical_effect(loader):
    handle, live = start([wizard(CASTER, initiative=100, spells_known=["message"])], seed=19)
    rng = live.rng.getstate()
    act(handle, CASTER, intent_type="cast_spell", spell_id="message", target_id="mon:foe")
    assert not events(live, CastFailed)
    assert events(live, SpellCast)[0].execution_class == "host_narrative"
    assert not events(live, DamageApplied)
    assert live.rng.getstate() == rng
    assert not combatant(live, CASTER).action_available


def test_mixed_spell_cannot_bypass_required_summon_by_selecting_damage_payload(loader):
    spell = loader.get_spell("conjure-animals")
    selected = next(a for a in spell.activities if a.kind == "save")
    handle, live = start(
        [wizard(CASTER, initiative=100, spells_known=[spell.slug], spell_slots={3: 1})],
        seed=19,
    )
    before = (_state(live), live.rng.getstate())
    act(
        handle,
        CASTER,
        intent_type="cast_spell",
        spell_id=spell.slug,
        activity_id=selected.id,
        target_id="mon:foe",
        slot_level=3,
    )
    assert events(live, CastFailed)[0].execution_failure.mechanisms == ("area_lifecycle",)
    assert (_state(live), live.rng.getstate()) == before


def test_independent_deferred_alternative_does_not_poison_initial_damage(loader):
    spell = loader.get_spell("freezing-sphere")
    initial = delivery_activities(spell, SpellDeliverySpec())
    assert [a.kind for a in initial] == ["save"]
    assert admission_failure(spell, initial) is None
    held = next(a for a in spell.activities if a.kind == "utility")
    failure = admission_failure(spell, [held])
    assert failure.mechanisms == ("world_object_state",)
    assert failure.activity_id == held.id


@pytest.mark.parametrize("slug,col", [("sunbeam", 10), ("freezing-sphere", 20)])
def test_default_cast_executes_initial_payload_once_and_not_later_alternatives(loader, slug, col):
    from dnd5e_engine import GridScene
    from dnd5e_engine.events import SaveRolled
    from tests.c20_support import foe

    handle, live = start(
        [
            wizard(
                CASTER,
                initiative=100,
                zone_id="0,0",
                spell_slots={6: 1},
                hp_current=500,
                hp_max=500,
            )
        ],
        encounter=[foe(zone_id=f"{col},0", hp_current=1000, hp_max=1000)],
        grid_scene=GridScene(width=40, height=10),
        seed=19,
    )
    act(handle, CASTER, intent_type="cast_spell", spell_id=slug, target_id="mon:foe")
    assert not events(live, CastFailed)
    assert len(events(live, SaveRolled)) == 1
    assert len(events(live, DamageApplied)) == 1
    assert live.spell_slots_by_entity[CASTER][6] == 0
    spell = loader.get_spell(slug)
    for alternative in spell_reviews()[spell.foundry_uuid].activities:
        if alternative.role == "alternative":
            activity = next(a for a in spell.activities if a.id == alternative.activity_id)
            assert admission_failure(spell, [activity]) is not None


@pytest.mark.parametrize("fallback", [False, True])
def test_public_monster_unsupported_spell_preserves_daily_use_and_selects_fallback(
    loader, fallback
):
    live, actor, target, monster, action, spell = _spell_setup(loader, "true-polymorph")
    legal = loader.get_spell("fire-bolt")
    if fallback:
        action = action.model_copy(
            update={
                "activities": [
                    *action.activities,
                    _cast_action(legal, activity_id="cast:fallback").activities[0],
                ]
            }
        )
    monster = monster.model_copy(update={"actions": [action]})
    live.ruleset_loader = MemoryAssetLoader(monsters=[monster], spells=[spell, legal])
    set_lib_loader_for_tests(live.ruleset_loader)
    uses = copy.deepcopy(live.monster_action_uses_by_entity[actor.entity_id])
    rng = live.rng.getstate()
    before = (_state(live), rng, list(live.event_log))
    orch._resolve_monster_cast(live, actor, target, action, action.activities[0], spell)
    assert (_state(live), live.rng.getstate(), live.event_log) == before
    run_async(orch.advance_monster_turn(orch.CombatHandle(live.handle_id)))
    assert [e.spell_id for e in events(live, SpellCast)] == (["fire-bolt"] if fallback else [])
    if not fallback:
        assert live.monster_action_uses_by_entity[actor.entity_id] == uses
        assert actor.action_available
        assert live.rng.getstate() == rng
    assert not any(e.spell_id == "true-polymorph" for e in events(live, SpellCast))


def test_refused_cast_preserves_old_concentration_and_armed_counterspell(loader):
    from dnd5e_engine.live_reactions import register_pending_reaction
    from tests.c20_support import pc
    from tests.test_spell_timed_activities import _end

    handle, live = start(
        [
            wizard(
                CASTER,
                initiative=100,
                spells_known=["bless", "blur", "fireball"],
                spell_slots={1: 1, 2: 1, 3: 1},
                constitution=-30,
            ),
            pc(
                "char:counter",
                initiative=0,
                zone_id="0,2",
                class_slug="wizard",
                character_level=5,
                intelligence=40,
                spell_slots={3: 1},
            ),
        ],
        seed=4,
    )
    act(handle, CASTER, intent_type="cast_spell", spell_id="bless", target_id=CASTER)
    for _ in range(2):
        _end(live)
    register_pending_reaction(
        live,
        "char:counter",
        orch.PlayerIntent(
            intent_type="ready",
            spell_id="counterspell",
            slot_level=3,
            reaction_trigger="cast_spell",
        ),
    )
    before = (
        _state(live),
        live.rng.getstate(),
        copy.deepcopy(live.pending_reactions),
        copy.deepcopy(live.concentration_chain),
    )
    act(handle, CASTER, intent_type="cast_spell", spell_id="blur", target_id=CASTER)
    assert events(live, CastFailed)[-1].reason == "unsupported_activity"
    assert (
        _state(live),
        live.rng.getstate(),
        live.pending_reactions,
        live.concentration_chain,
    ) == before
    act(handle, CASTER, intent_type="cast_spell", spell_id="fireball", target_zone_id="1,0")
    assert events(live, CastFailed)[-1].reason == "countered"
    assert live.spell_slots_by_entity[CASTER][3] == 1
    assert live.spell_slots_by_entity["char:counter"][3] == 0
    assert not combatant(live, CASTER).action_available


@pytest.mark.parametrize("slug", ["fireball", "message", "blur", "conjure-animals"])
def test_public_admission_replays_exact_events_rng_and_final_state(loader, slug):
    def run():
        spell = loader.get_spell(slug)
        handle, live = start(
            [wizard(CASTER, initiative=100, spells_known=[slug], spell_slots={spell.level: 1})],
            seed=42,
        )
        pre = len(live.event_log)
        act(
            handle,
            CASTER,
            intent_type="cast_spell",
            spell_id=slug,
            target_id="mon:foe",
            target_zone_id="1,0",
        )
        return (
            [e.model_dump_json() for e in live.event_log[pre:]],
            live.rng.getstate(),
            _state(live),
        )

    assert run() == run()


def test_inventory_matches_every_canonical_spell_activity_and_is_reproducible(loader):
    document = audit_document(loader)
    assert document["spell_count"] == 339
    assert document["activity_count"] == 464
    assert len(spell_reviews()) == 339
    path = Path(__file__).resolve().parents[3] / "docs/audits/spell-execution.json"
    assert json.loads(path.read_text(encoding="utf-8")) == document
    assert document == audit_document(loader)


def test_missing_governing_formula_input_refuses_before_payment(loader):
    spell = loader.get_spell("healing-word")
    # The injected loader intentionally has no class ability carrier.
    set_lib_loader_for_tests(MemoryAssetLoader(spells=[spell]))
    handle, live = start(
        [wizard(CASTER, initiative=100, spells_known=[spell.slug], spell_slots={1: 1})], seed=19
    )
    before = (_state(live), live.rng.getstate())
    act(handle, CASTER, intent_type="cast_spell", spell_id=spell.slug, target_id=CASTER)
    failure = events(live, CastFailed)[0]
    assert failure.reason == "unsupported_activity"
    assert failure.execution_failure.code == "invalid_formula"
    assert (_state(live), live.rng.getstate()) == before
    assert not events(live, IntentSubmitted)


def test_item_child_cannot_borrow_an_ability_absent_from_its_execution_context(loader):
    child = loader.get_spell("healing-word")
    item = loader.get_item(WAND)
    wrapper = item.activities[0]
    wrapper = wrapper.model_copy(
        update={
            "spell": wrapper.spell.model_copy(update={"uuid": child.foundry_uuid, "ability": ""})
        }
    )
    set_lib_loader_for_tests(
        MemoryAssetLoader(
            spells=[child],
            items=[item.model_copy(update={"activities": [wrapper]})],
            classes=[loader.get_class("wizard")],
        )
    )
    handle, live = start([wizard(CASTER, initiative=100, equipment=(WAND,))], seed=19)
    before = (_state(live), live.rng.getstate(), copy.deepcopy(live.custom_counters_by_entity))
    act(handle, CASTER, intent_type="use_item", item_id=WAND, target_id=CASTER)
    assert events(live, CastFailed)[0].execution_failure.code == "invalid_formula"
    assert (_state(live), live.rng.getstate(), live.custom_counters_by_entity) == before
    assert not events(live, IntentSubmitted)


@pytest.mark.parametrize("mutation", ["empty", "missing_effect", "unknown_activity", "kind"])
@pytest.mark.parametrize("source", ["direct", "item"])
def test_known_identity_cannot_admit_mutated_noop_payload(loader, mutation, source):
    from dnd5e_srd_data.schema.common import AppliedEffectRef, UtilityActivity

    spell = loader.get_spell("fireball")
    activity = spell.activities[0]
    if mutation == "empty":
        activity = activity.model_copy(
            update={
                "damage": activity.damage.model_copy(update={"parts": []}),
                "effects": [],
            }
        )
    elif mutation == "missing_effect":
        activity = activity.model_copy(update={"effects": [AppliedEffectRef(id="missing")]})
    elif mutation == "unknown_activity":
        activity = activity.model_copy(update={"id": "not-reviewed"})
    else:
        activity = UtilityActivity(id=activity.id)
    spell = spell.model_copy(update={"activities": [activity]})
    set_lib_loader_for_tests(MemoryAssetLoader(spells=[spell], items=[loader.get_item(WAND)]))
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
    before = (_state(live), live.rng.getstate())
    intent = (
        dict(intent_type="cast_spell", spell_id=spell.slug, slot_level=3)
        if source == "direct"
        else dict(intent_type="use_item", item_id=WAND)
    )
    act(handle, CASTER, target_id="mon:foe", target_zone_id="1,0", **intent)
    assert events(live, CastFailed)[0].reason == "unsupported_activity"
    assert (_state(live), live.rng.getstate()) == before


def test_every_admitted_default_has_a_valid_selected_contract(loader):
    for review in spell_reviews().values():
        spell = loader.get_spell(review.slug)
        failure = admission_failure(
            spell, delivery_activities(spell, SpellDeliverySpec()), direct_carrier=True
        )
        assert (failure is not None) == (review.classification == "deferred"), review.slug
        if review.classification == "deferred":
            assert review.missing_details, review.slug


def test_inventory_rejects_stale_review_evidence(loader):
    class Stale(BundledAssetLoader):
        def get_spell(self, slug):
            spell = super().get_spell(slug)
            return (
                spell.model_copy(update={"description": "changed source"})
                if slug == "fireball"
                else spell
            )

    with pytest.raises(ValueError, match="stale: fireball"):
        audit_document(Stale())
