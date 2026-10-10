"""Actual check resolver parity, explicit adjudication and unpublished failures."""

from copy import deepcopy
from typing import get_args

import pytest
from pydantic import ValidationError

from dnd5e_engine.check import CheckSpec, resolve_check
from dnd5e_engine.evaluation_contracts import (
    CheckPayload,
    RuleEvaluationRequest,
    RuleEvaluationResult,
)
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import RulesetBindingError
from dnd5e_engine.evaluation_state import NonCombatSnapshot
from dnd5e_engine.events import CheckRolled
from dnd5e_engine.rules.skills import SKILL_ABILITIES, Skill
from dnd5e_engine.types.effects import ActiveEffect
from tests.evaluation_support import HERO
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
            "schema_version": "engine-evaluation/13",
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
            "schema_version": "engine-evaluation/13",
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
