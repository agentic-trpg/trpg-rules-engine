"""Actual check resolver parity, explicit adjudication and unpublished failures."""

from copy import deepcopy
from typing import get_args

import pytest
from dnd5e_srd_data import MemoryAssetLoader
from pydantic import ValidationError

from dnd5e_engine.check import CheckSpec, resolve_check
from dnd5e_engine.effect_lifecycle import conditions_after_effect_application
from dnd5e_engine.evaluation_contracts import (
    CheckPayload,
    RuleEvaluationRequest,
    RuleEvaluationResult,
)
from dnd5e_engine.evaluation_effects import EffectChangeState, effect_state
from dnd5e_engine.evaluation_region_effect import region_condition_identity
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import (
    RulesetBindingError,
    get_default_ruleset,
    ruleset_binding,
)
from dnd5e_engine.evaluation_state import ConditionRecord, NonCombatSnapshot
from dnd5e_engine.events import CheckRolled
from dnd5e_engine.rules.skills import SKILL_ABILITIES, Skill
from dnd5e_engine.types.conditions import ActiveCondition
from dnd5e_engine.types.effects import ActiveEffect
from tests.evaluation_support import FOE, HERO, WEAPON, synthetic_loader
from tests.test_stateless_attack import execute, request_and_live


def check_case(*, seed=1, skill="athletics", ability="str", actor_updates=None, **adjudication):
    request, _, _ = request_and_live(seed=seed)
    snapshot = request.state_snapshot
    actors = list(snapshot.character_states)
    actors[0] = actors[0].model_copy(update=actor_updates or {})
    payload = dict(
        actor_id=HERO,
        ability=ability,
        skill=skill,
        tool=None,
        dc=15,
        target_id=None,
        context="generic",
        required_sense="none",
        social_interaction=False,
        advantage=(),
        disadvantage=(),
        redeem_granted_die=None,
    )
    payload.update(adjudication)
    return request.model_copy(
        update={
            "schema_version": "engine-evaluation/24",
            "operation_kind": "rules.check",
            "payload": CheckPayload(**payload),
            "state_snapshot": NonCombatSnapshot(
                combat_setup=None,
                **snapshot.model_dump(
                    exclude={"combat_state", "snapshot_kind", "character_states"}
                ),
                snapshot_kind="non_combat",
                character_states=tuple(actors),
            ),
        }
    )


@pytest.mark.parametrize("skill", [None, *get_args(Skill)])
@pytest.mark.parametrize("mode", ["normal", "advantage", "disadvantage", "both"])
@pytest.mark.parametrize("proficiency", ["none", "proficient", "expertise", "jack", "reliable"])
def test_real_resolver_parity(skill, mode, proficiency):
    names = {
        "strength": "str",
        "dexterity": "dex",
        "constitution": "con",
        "intelligence": "int",
        "wisdom": "wis",
        "charisma": "cha",
    }
    ability = names[SKILL_ABILITIES[skill]] if skill else "str"
    proficient = [skill] if skill and proficiency in ("proficient", "expertise", "reliable") else []
    expertise = [skill] if skill and proficiency == "expertise" else []
    adv, dis = mode in ("advantage", "both"), mode in ("disadvantage", "both")
    request = check_case(
        skill=skill,
        ability=ability,
        advantage=("flag",) if adv else (),
        disadvantage=("flag",) if dis else (),
        actor_updates={
            "skill_proficiencies": proficient,
            "skill_expertise": expertise,
            "proficiency_bonus_override": 3,
            "jack_of_all_trades": proficiency == "jack",
            "reliable_talent": proficiency == "reliable",
        },
    )
    before = deepcopy(request)
    result = execute(request)
    actor = request.state_snapshot.character_states[0]
    rng = request.rng_context.state.restore()
    legacy = resolve_check(
        CheckSpec(
            kind="skill" if skill else "ability",
            ability_scores={n: getattr(actor, n) for n in names},
            proficient_skills=tuple(proficient),
            proficient_saves=(),
            proficiency_bonus=3,
            skill=skill,
            ability=next(n for n, code in names.items() if code == ability),
            dc=15,
            advantage=adv,
            disadvantage=dis,
            expertise_skills=tuple(expertise),
            jack_of_all_trades=proficiency == "jack",
            reliable_talent=proficiency == "reliable",
            rng=rng,
        )
    )
    assert result.status == "accepted"
    assert result.state_delta.operations == ()
    (event,) = result.proposed_events
    assert isinstance(event, CheckRolled)
    assert (event.roll_total, event.modifier, event.natural, event.succeeded) == (
        legacy.roll_total,
        legacy.modifier,
        legacy.natural_roll,
        legacy.success,
    )
    assert result.rng_transition.next_state == RNGState.capture(rng)
    assert result.rng_transition.state_changed
    assert request == before
    assert execute(request) == result
    assert RuleEvaluationResult.model_validate_json(result.model_dump_json()) == result
    assert RuleEvaluationRequest.model_validate_json(request.model_dump_json()) == request


def test_legal_failure_and_explicit_variant_ability_and_flat_bonus():
    request = check_case(
        ability="cha",
        dc=99,
        actor_updates={
            "charisma": 18,
            "skill_proficiencies": ["athletics"],
        },
    )
    result = execute(request)
    assert result.status == "accepted"
    (event,) = result.proposed_events
    assert event.ability == "cha"
    assert event.modifier == 6
    assert event.succeeded is False
    canonical = execute(check_case(actor_updates={"skill_check_bonuses": {"athletics": 7}}))
    assert canonical.proposed_events[0].modifier == 10


@pytest.mark.parametrize("ability", ["str", "dex", "con", "int", "wis", "cha"])
def test_all_abilities_without_global_random_or_registry(ability, monkeypatch):
    import random

    from dnd5e_engine import orchestrator

    request = check_case(skill=None, ability=ability)
    monkeypatch.setattr(random, "randint", lambda *args: pytest.fail("global RNG"))
    monkeypatch.setattr(orchestrator, "_get_live", lambda *args: pytest.fail("registry"))
    monkeypatch.setattr(orchestrator, "CombatHandle", lambda *args: pytest.fail("handle"))
    result = execute(request)
    assert result.status == "accepted"
    assert result.proposed_events[0].ability == ability


def test_v2_snapshot_and_npc_share_the_check_pipeline():
    from dnd5e_srd_data import MemoryAssetLoader

    from dnd5e_engine.evaluation_ruleset import ruleset_binding
    from tests.evaluation_support import FOE, WEAPON, synthetic_loader
    from tests.test_stateless_weapons import weapon_case

    request, _, _, loader = weapon_case(npc=True)
    loader = MemoryAssetLoader(
        items=[loader.get_weapon("rapier"), synthetic_loader().get_weapon(WEAPON)]
    )
    payload = check_case().payload.model_copy(update={"actor_id": FOE})
    snapshot = NonCombatSnapshot(
        combat_setup=None,
        **request.state_snapshot.model_dump(exclude={"combat_state", "snapshot_kind"}),
        snapshot_kind="non_combat",
    )
    request = request.model_copy(
        update={
            "schema_version": "engine-evaluation/24",
            "operation_kind": "rules.check",
            "payload": payload,
            "state_snapshot": snapshot,
            "ruleset_binding": ruleset_binding(loader),
        }
    )
    result = execute(request, loader)
    assert result.status == "accepted"
    assert result.proposed_events[0].actor_id == FOE


def test_missing_adjudication_is_choice_but_omitted_fields_or_wrong_types_are_schema_errors():
    request = check_case(dc=None)
    result = execute(request)
    assert result.status == "needs_choice"
    assert result.choice.required_fields == ("dc",)
    assert result.state_delta is result.rng_transition is None
    assert result.proposed_events == ()
    for field in CheckPayload.model_fields:
        value = request.model_dump(mode="python")
        del value["payload"][field]
        with pytest.raises(ValidationError):
            RuleEvaluationRequest.model_validate(value)
    for field, value in [("dc", True), ("dc", "15"), ("social_interaction", 0), ("ability", "STR")]:
        with pytest.raises(ValidationError):
            execute(
                request.model_copy(
                    update={"payload": request.payload.model_copy(update={field: value})}
                )
            )


@pytest.mark.parametrize(
    "updates",
    [
        {"tool": "thieves-tools", "skill": None},
        {"redeem_granted_die": "feature_grant:bardic-inspiration"},
        {"context": "search"},
        {"required_sense": "sight"},
        {"advantage": ("help",)},
    ],
)
def test_unmigrated_mechanics_before_rng(updates, monkeypatch):
    request = check_case(**updates)
    monkeypatch.setattr(
        RNGState, "restore", lambda self: pytest.fail("RNG restored before admission")
    )
    result = execute(request)
    assert result.status == "unsupported"
    assert result.proposed_events == ()


@pytest.mark.parametrize(
    "updates",
    [
        {"class_slug": "rogue"},
        {"skill_expertise": ["athletics"]},
        {"skill_proficiencies": ["made-up"]},
        {"strength": 0},
    ],
)
def test_unrepresented_actor_mechanics(updates):
    assert execute(check_case(actor_updates=updates)).status == "unsupported"


def test_unrelated_actor_profiles_do_not_block_or_change_the_check():
    request = check_case(target_id=FOE, social_interaction=True)
    expected = execute(request)
    snapshot = request.state_snapshot
    bystanders = tuple(
        snapshot.character_states[1].model_copy(
            update={
                "entity_id": f"bystander:{index}",
                "class_slug": "fighter",
                "classes": {"fighter": 1},
            }
        )
        for index in range(24)
    )
    populated = request.model_copy(
        update={
            "state_snapshot": snapshot.model_copy(
                update={"character_states": (*snapshot.character_states, *bystanders)}
            )
        }
    )
    assert expected.status == "accepted"
    assert execute(populated) == expected


def test_unreviewed_social_target_still_refuses_before_rng(monkeypatch):
    request = check_case(target_id=FOE, social_interaction=True)
    snapshot = request.state_snapshot
    roller, target = snapshot.character_states
    target = target.model_copy(update={"class_slug": "fighter", "classes": {"fighter": 1}})
    request = request.model_copy(
        update={
            "state_snapshot": snapshot.model_copy(update={"character_states": (roller, target)})
        }
    )
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("RNG before unsupported"))
    result = execute(request)
    assert result.status == "unsupported"
    assert result.error.code == "check.effects"
    assert result.state_delta is result.rng_transition is None
    assert result.proposed_events == ()


def test_actor_target_effect_binding_and_fault_boundaries(monkeypatch):
    request = check_case()
    missing = request.model_copy(
        update={
            "actor_id": "missing",
            "payload": request.payload.model_copy(update={"actor_id": "missing"}),
        }
    )
    assert execute(missing).status == "rejected"
    assert execute(check_case(actor_updates={"hp_current": 0})).status == "rejected"
    assert execute(check_case(target_id="missing")).status == "rejected"
    effect = effect_state(
        ActiveEffect(id="guidance", name="Guidance", target_id=HERO, origin="synthetic")
    )
    affected = request.model_copy(
        update={
            "state_snapshot": request.state_snapshot.model_copy(update={"effect_states": (effect,)})
        }
    )
    assert execute(affected).status == "unsupported"
    stale = request.model_copy(
        update={
            "ruleset_binding": request.ruleset_binding.model_copy(
                update={"evaluator_version": "dnd5e-evaluation/7"}
            )
        }
    )
    with pytest.raises(RulesetBindingError):
        execute(stale)
    from dnd5e_engine import evaluation_checks

    original = evaluation_checks.resolve_check_request
    before = deepcopy(request)

    def fail_after_draw(payload, ctx):
        original(payload, ctx)
        raise RuntimeError("injected after check draw")

    monkeypatch.setattr(evaluation_checks, "resolve_check_request", fail_after_draw)
    with pytest.raises(RuntimeError, match="after check draw"):
        execute(request)
    assert request == before


def test_variant_ability_cannot_silently_drop_a_supplied_skill_bonus(monkeypatch):
    request = check_case(
        ability="cha",
        actor_updates={
            "charisma": 18,
            "skill_proficiencies": ["athletics"],
            "skill_check_bonuses": {"athletics": 7},
        },
    )
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("RNG before unsupported"))
    result = execute(request)
    assert result.status == "unsupported"
    assert result.error.code == "check.skill_bonus"
    assert result.state_delta is result.rng_transition is None
    assert result.proposed_events == ()


def _poison_effect(target=HERO, *, origin="trap:needle"):
    return effect_state(
        ActiveEffect(
            id="condition:trap-poison",
            name="Needle Poison",
            origin=origin,
            target_id=target,
            statuses={"poisoned"},
        )
    )


def _linked_poison(effect, *, scope="session"):
    (condition,), _ = conditions_after_effect_application(
        (), effect.id, effect.statuses, scope=scope
    )
    return ConditionRecord.model_validate(condition.model_dump())


@pytest.mark.parametrize("linked", [False, True])
def test_active_trap_poison_disadvantages_an_ability_check(linked):
    effect = _poison_effect()
    direct = ActiveCondition(
        condition="poisoned", source_entity_id="trap:012345abcdef", scope="session"
    )
    condition = (
        _linked_poison(effect, scope="combat")
        if linked
        else ConditionRecord.model_validate(direct.model_dump())
    )
    request = check_case(seed=5, skill=None, actor_updates={"conditions": [condition]})
    if linked:
        request = request.model_copy(
            update={
                "state_snapshot": request.state_snapshot.model_copy(
                    update={"effect_states": (effect,)}
                )
            }
        )
    result = execute(request)
    assert result.status == "accepted", result.error
    (event,) = result.proposed_events
    rng = request.rng_context.state.restore()
    rolls = (rng.randint(1, 20), rng.randint(1, 20))
    assert rolls[0] > rolls[1]
    assert event.advantage == "disadvantage"
    assert event.rolled_natural == min(rolls)
    assert event.disadvantage_sources == ["condition:attacker"]
    assert result.rng_transition.next_state == RNGState.capture(rng)
    assert result.state_delta.operations == ()


@pytest.mark.parametrize("named_target", [False, True])
def test_poison_on_another_actor_does_not_change_the_rollers_check(named_target):
    request = check_case(target_id=FOE if named_target else None, social_interaction=named_target)
    expected = execute(request)
    snapshot = request.state_snapshot
    roller, target = snapshot.character_states
    effect = _poison_effect(FOE)
    target = target.model_copy(update={"conditions": [_linked_poison(effect)]})
    affected = request.model_copy(
        update={
            "state_snapshot": snapshot.model_copy(
                update={"character_states": (roller, target), "effect_states": (effect,)}
            )
        }
    )
    assert expected.status == "accepted"
    assert execute(affected) == expected


@pytest.mark.parametrize(
    "fault",
    [
        "missing_condition",
        "missing_effect",
        "ambiguous",
        "wrong_source",
        "modifier",
        "other_region_status",
        "malformed_region_id",
        "malformed_region_origin",
        "combat_region",
    ],
)
def test_unreviewed_poison_profiles_fail_before_rng(fault, monkeypatch):
    effect = _poison_effect()
    if fault in ("other_region_status", "combat_region"):
        rule_ref = "condition:deafened" if fault == "other_region_status" else "condition:poisoned"
        _, effect_id, origin = region_condition_identity(
            "scene.trap", "source.trap", HERO, rule_ref=rule_ref
        )
        effect = effect.model_copy(update={"id": effect_id, "origin": origin})
    elif fault == "malformed_region_id":
        effect = effect.model_copy(update={"id": "effect:region-poisoned:forged"})
    elif fault == "malformed_region_origin":
        effect = effect.model_copy(update={"origin": "region:poisoned:forged"})
    effects = (effect,)
    condition = _linked_poison(effect)
    records = [condition]
    if fault == "missing_condition":
        records.clear()
    elif fault == "missing_effect":
        effects = ()
    elif fault == "ambiguous":
        effects = (effect, _poison_effect(origin="trap:other"))
    elif fault == "wrong_source":
        records = [condition.model_copy(update={"source_entity_id": "trap:012345abcdef"})]
    elif fault == "combat_region":
        records = [condition.model_copy(update={"scope": "combat"})]
    elif fault == "modifier":
        effects = (
            effect.model_copy(
                update={
                    "changes": [
                        EffectChangeState(key="check.bonus", mode="add", value="1d4", priority=20)
                    ]
                }
            ),
        )
    request = check_case(actor_updates={"conditions": records})
    request = request.model_copy(
        update={
            "state_snapshot": request.state_snapshot.model_copy(update={"effect_states": effects})
        }
    )
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("RNG before unsupported"))
    result = execute(request)
    assert result.status == "unsupported"
    assert result.error.code == "check.effects"
    assert result.state_delta is result.rng_transition is None
    assert result.proposed_events == ()


def test_supplied_poison_clause_must_agree_with_the_bound_evaluator(monkeypatch):
    condition = get_default_ruleset().get_condition("poisoned")
    loader = MemoryAssetLoader(
        items=[synthetic_loader().get_item(WEAPON)],
        conditions=[condition.model_copy(update={"effects": []})],
    )
    direct = ActiveCondition(
        condition="poisoned", source_entity_id="trap:012345abcdef", scope="session"
    )
    request = check_case(
        actor_updates={"conditions": [ConditionRecord.model_validate(direct.model_dump())]}
    )
    request = request.model_copy(update={"ruleset_binding": ruleset_binding(loader)})
    monkeypatch.setattr(RNGState, "restore", lambda self: pytest.fail("RNG before unsupported"))
    result = execute(request, loader)
    assert result.status == "unsupported"
    assert result.error.code == "check.effects"
    assert "compiled rule" in result.error.reason
    assert result.rng_transition is None
    assert result.proposed_events == ()
