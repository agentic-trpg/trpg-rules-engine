"""R15 Save mechanics, independent expectations, Legacy parity and isolation."""

import asyncio
import random
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd5e_srd_data.schema.common import SaveActivity, SaveBlock, SaveDcBlock
from pydantic import ValidationError

from dnd5e_engine import evaluation_saves
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.activities.actor_stats import _SCORE_ATTR
from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.save import resolve_save
from dnd5e_engine.evaluation_contracts import (
    RuleEvaluationRequest,
    RuleEvaluationResult,
    SavePayload,
)
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_engine.evaluation_state import ConditionRecord, NonCombatSnapshot
from dnd5e_engine.events import SaveRolled
from dnd5e_engine.types.conditions import ActiveCondition
from dnd5e_engine.types.effects import ActiveEffect
from tests.evaluation_support import FOE, HERO, synthetic_loader
from tests.test_stateless_attack import execute, request_and_live
from tests.test_stateless_item_closure_core import isolate


def save_loader():
    canonical = BundledAssetLoader()
    source = synthetic_loader()
    return MemoryAssetLoader(
        items=[source.get_item(slug) for slug in source.list_slugs("items")],
        conditions=[
            canonical.get_condition(name)
            for name in (
                "restrained",
                "paralyzed",
                "stunned",
                "petrified",
                "unconscious",
                "incapacitated",
                "prone",
            )
        ],
    )


def save_case(
    *,
    ability="str",
    dc=15,
    seed=0,
    npc=False,
    proficiency=True,
    advantage=(),
    disadvantage=(),
    conditions=(),
):
    request, _, live = request_and_live(seed=seed)
    target_id = FOE if npc else HERO
    actors = []
    for a in request.state_snapshot.character_states:
        if a.entity_id == target_id:
            a = a.model_copy(
                update={
                    **{field: 18 for field in _SCORE_ATTR.values()},
                    "proficiency_bonus_override": 3,
                    "entity_type": "NPC" if npc else "Character",
                    "save_proficiencies": [ability] if proficiency else [],
                    "conditions": list(conditions),
                }
            )
            live.initiative = [
                c.model_copy(
                    update={
                        **{field: 18 for field in _SCORE_ATTR.values()},
                        "proficiency_bonus_override": 3,
                        "entity_type": "NPC" if npc else "Character",
                        "save_proficiencies": [ability] if proficiency else [],
                        "conditions": [
                            ActiveCondition.model_validate(c.model_dump()) for c in conditions
                        ],
                    }
                )
                if c.entity_id == target_id
                else c
                for c in live.initiative
            ]
        actors.append(a)
    snapshot = NonCombatSnapshot(
        combat_setup=None,
        snapshot_kind="non_combat",
        character_states=tuple(actors),
        **request.state_snapshot.model_dump(
            exclude={"snapshot_kind", "combat_state", "character_states"}
        ),
    )
    return request.model_copy(
        update={
            "schema_version": "engine-evaluation/11",
            "operation_kind": "rules.save",
            "actor_id": target_id,
            "state_snapshot": snapshot,
            "ruleset_binding": ruleset_binding(save_loader()),
            "payload": SavePayload(
                kind="rules.save",
                target_id=target_id,
                ability=ability,
                dc=dc,
                advantage=advantage,
                disadvantage=disadvantage,
                is_magical=False,
            ),
        }
    ), live


def legacy_save(request, live):
    payload = request.payload
    target = next(a for a in live.initiative if a.entity_id == request.actor_id)
    hydration = orch._build_hydration_payload(live, caster=target)
    events = []
    rng = request.rng_context.state.restore()
    ctx = build_activity_context(
        target,
        [target],
        rng=rng,
        event_emitter=events.append,
        slot_level=None,
        base_spell_level=None,
        spellcasting_ability=None,
        concentration=False,
        source_passive_effects=[],
        spell_book={},
        save_modifiers=hydration["save_modifiers"],
        passive_damage_modifiers={},
        d20_test_penalty=hydration["d20_test_penalty"],
        save_dc_override=payload.dc,
    )
    adv = dict(ctx.passive_save_adv)
    dis = dict(ctx.passive_save_dis)
    if payload.advantage:
        adv[target.entity_id] = [payload.ability.upper()]
    if payload.disadvantage:
        dis[target.entity_id] = [payload.ability.upper()]
    ctx = replace(ctx, passive_save_adv=adv, passive_save_dis=dis)
    resolve_save(
        SaveActivity(
            id="explicit-existing-save",
            save=SaveBlock(
                ability=[payload.ability],
                dc=SaveDcBlock(calculation="flat", formula=str(payload.dc)),
            ),
        ),
        ctx,
    )
    return next(e for e in events if isinstance(e, SaveRolled)), RNGState.capture(rng)


@pytest.mark.parametrize("ability", list(_SCORE_ATTR))
@pytest.mark.parametrize("npc", [False, True])
@pytest.mark.parametrize("proficiency", [False, True])
@pytest.mark.parametrize("mode", ["normal", "advantage", "disadvantage", "both"])
def test_six_abilities_pc_npc_shared_resolver_parity(monkeypatch, ability, npc, proficiency, mode):
    adv, dis = mode in {"advantage", "both"}, mode in {"disadvantage", "both"}
    request, live = save_case(
        ability=ability,
        npc=npc,
        proficiency=proficiency,
        advantage=("flag",) if adv else (),
        disadvantage=("flag",) if dis else (),
    )
    old, old_rng = legacy_save(request, live)
    before = deepcopy(request)
    with monkeypatch.context() as scoped:
        isolate(scoped)
        result = execute(request, save_loader())
        assert result == execute(request, save_loader())
    assert result.status == "accepted"
    assert result.state_delta.operations == ()
    (event,) = result.proposed_events
    # Random(0): first=13, second=14. Saves have no natural-1/20 override.
    natural = 14 if mode == "advantage" else 13
    modifier = 7 if proficiency else 4
    assert event.natural == natural
    assert event.modifier == modifier
    assert event.roll_total == natural + modifier
    assert event.succeeded == (natural + modifier >= 15)
    assert event.sources == (["flag"] * (int(adv) + int(dis)))
    assert event.model_dump(exclude={"sources"}) == old.model_dump(exclude={"sources"})
    assert result.rng_transition.next_state == old_rng
    independent_rng = random.Random(0)
    for _ in range(2 if mode in {"advantage", "disadvantage"} else 1):
        independent_rng.randint(1, 20)
    assert result.rng_transition.next_state == RNGState.capture(independent_rng)
    assert request == before
    assert RuleEvaluationRequest.model_validate_json(request.model_dump_json()) == request
    assert RuleEvaluationResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    ("seed", "dc", "natural", "success"),
    [(31, 0, 1, True), (5, 100, 20, False), (0, 20, 13, True), (0, 21, 13, False)],
)
def test_save_failure_is_accepted_and_no_attack_natural_override(seed, dc, natural, success):
    request, live = save_case(seed=seed, dc=dc)
    result = execute(request, save_loader())
    old, state = legacy_save(request, live)
    assert result.status == "accepted"
    (event,) = result.proposed_events
    assert event.natural == natural
    assert event.succeeded is success
    assert event == old
    assert result.rng_transition.next_state == state


def condition(name):
    return ConditionRecord(
        condition=name,
        source_entity_id="implied:fixture",
        scope="session",
        duration_rounds=None,
        save_dc=None,
        applied_round=0,
        exhaustion_level=1,
        source_effect_id=None,
    )


@pytest.mark.parametrize("name", ["paralyzed", "stunned", "petrified", "unconscious"])
@pytest.mark.parametrize("ability", ["str", "dex"])
def test_auto_fail_consumes_no_rng_and_matches_existing_resolver(name, ability):
    request, live = save_case(ability=ability, conditions=(condition(name),))
    result = execute(request, save_loader())
    old, state = legacy_save(request, live)
    assert result.status == "accepted", result.error
    assert result.proposed_events == (old,)
    assert old.natural is None
    assert old.succeeded is False
    assert result.rng_transition.next_state == state
    assert state == request.rng_context.state
    assert not result.rng_transition.state_changed


def test_restrained_disadvantage_and_flag_cancel_preserve_provenance():
    request, live = save_case(
        ability="dex", conditions=(condition("restrained"),), advantage=("flag",)
    )
    result = execute(request, save_loader())
    (event,) = result.proposed_events
    assert event.advantage == "normal"
    assert event.natural == 13
    assert set(event.sources) == {"condition:target", "flag"}
    old, state = legacy_save(request, live)
    assert event.roll_total == old.roll_total
    assert result.rng_transition.next_state == state


@pytest.mark.parametrize("bad", ["target", "effect", "condition", "feature", "source", "snapshot"])
def test_refusals_before_rng_or_modifiers(monkeypatch, bad):
    request, _ = save_case()
    snapshot = request.state_snapshot
    actors = list(snapshot.character_states)
    if bad == "target":
        request = request.model_copy(
            update={
                "actor_id": "absent",
                "payload": request.payload.model_copy(update={"target_id": "absent"}),
            }
        )
    elif bad == "effect":
        snapshot = snapshot.model_copy(
            update={
                "effect_states": (
                    effect_state(
                        ActiveEffect(
                            id="next",
                            name="Explicit next modifier",
                            origin="approved:source",
                            target_id=HERO,
                            changes=[],
                        )
                    ),
                )
            }
        )
    elif bad == "condition":
        actors[0] = actors[0].model_copy(update={"conditions": [condition("exhaustion")]})
    elif bad == "feature":
        actors[0] = actors[0].model_copy(update={"granted_features": ("indomitable",)})
    elif bad == "source":
        request = request.model_copy(
            update={"payload": request.payload.model_copy(update={"advantage": ("effect",)})}
        )
    else:
        combat, _, _ = request_and_live()
        snapshot = combat.state_snapshot
    if bad != "snapshot":
        snapshot = snapshot.model_copy(update={"character_states": tuple(actors)})
    request = request.model_copy(update={"state_snapshot": snapshot})
    monkeypatch.setattr(RNGState, "restore", lambda *a: pytest.fail("preflight drew RNG"))
    result = execute(request, save_loader())
    assert result.status == ("rejected" if bad == "target" else "unsupported")
    assert result.state_delta is None
    assert result.proposed_events == ()
    assert result.rng_transition is None


@pytest.mark.parametrize("fault", [RuntimeError, asyncio.CancelledError])
def test_post_draw_fault_is_unpublished_and_inputs_unchanged(monkeypatch, fault):
    request, _ = save_case()
    before = deepcopy(request)

    original = evaluation_saves.resolve_snapshot_save

    def fail(state, payload, rng):
        event = original(state, payload, rng)
        assert event.natural == 13
        assert event.roll_total == 20
        raise fault("post-draw save fault")

    monkeypatch.setattr(evaluation_saves, "resolve_snapshot_save", fail)
    with pytest.raises(fault):
        execute(request, save_loader())
    assert request == before


@pytest.mark.parametrize(
    "updates", [{"dc": -1}, {"ability": "luck"}, {"target_id": FOE}, {"dc": True}]
)
def test_strict_save_input_and_actor_identity(updates):
    request, _ = save_case()
    value = request.model_dump()
    value["payload"].update(updates)
    with pytest.raises(ValidationError):
        RuleEvaluationRequest.model_validate(value)


@pytest.mark.parametrize("bad", ["cover", "armor", "ability", "proficiency", "clause", "once"])
def test_complete_dependency_refusal_before_rng(monkeypatch, bad):
    from dnd5e_engine.types.effects import ActiveEffectChange

    request, _ = save_case(conditions=(condition("restrained"),) if bad == "clause" else ())
    loader = save_loader()
    snapshot = request.state_snapshot
    actor = snapshot.character_states[0]
    if bad == "cover":
        grid = snapshot.scene_state.grid.model_copy(update={"cover_cells": {"0,0": "half"}})
        snapshot = snapshot.model_copy(
            update={"scene_state": snapshot.scene_state.model_copy(update={"grid": grid})}
        )
    elif bad == "armor":
        actor = actor.model_copy(update={"shield_equipped": True})
    elif bad == "ability":
        actor = actor.model_copy(update={"constitution": 0})
    elif bad == "proficiency":
        actor = actor.model_copy(update={"save_proficiencies": ["luck"]})
    elif bad == "clause":
        altered = loader.get_condition("restrained").model_copy(update={"effects": []})
        loader = MemoryAssetLoader(
            items=[loader.get_item(s) for s in loader.list_slugs("items")],
            conditions=[
                altered if s == "restrained" else loader.get_condition(s)
                for s in loader.list_slugs("conditions")
            ],
        )
        request = request.model_copy(update={"ruleset_binding": ruleset_binding(loader)})
    else:
        effect = ActiveEffect(
            id="one-use",
            name="Once",
            origin="approved:once",
            target_id=HERO,
            changes=[
                ActiveEffectChange(key="flags.save.next_disadvantage", mode="override", value=True)
            ],
        )
        snapshot = snapshot.model_copy(update={"effect_states": (effect_state(effect),)})
    snapshot = snapshot.model_copy(
        update={"character_states": (actor, *snapshot.character_states[1:])}
    )
    request = request.model_copy(update={"state_snapshot": snapshot})
    monkeypatch.setattr(
        RNGState, "restore", lambda *a: pytest.fail("unsupported dependency drew RNG")
    )
    result = execute(request, loader)
    assert result.status == "unsupported"
    assert result.state_delta is None
    assert result.rng_transition is None
    assert result.proposed_events == ()


def test_concurrent_private_rng_and_request_replay(monkeypatch):
    request, _ = save_case()
    loader = save_loader()
    isolate(monkeypatch)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: execute(request, loader), range(8)))
    assert all(r == results[0] for r in results)
    assert results[0].proposed_events[0].roll_total == 20
