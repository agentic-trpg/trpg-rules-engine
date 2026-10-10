"""Noncombat adjudicated checks through the shared Activity Resolver pipeline."""

from typing import Literal, TypedDict, get_args

from dnd5e_srd_data.loader import AssetLoader

from dnd5e_engine.activities.actor_stats import _SCORE_ATTR, skill_ability
from dnd5e_engine.activities.check_pipeline import resolve_check_request
from dnd5e_engine.activities.context import ActivityResolutionContext
from dnd5e_engine.evaluation_contracts import (
    CheckAdjudicationChoice,
    CheckPayload,
    RuleError,
    RuleEvaluationRequest,
)
from dnd5e_engine.evaluation_delta import StateDelta
from dnd5e_engine.evaluation_projection import EvaluationInvariantError
from dnd5e_engine.evaluation_rng import RNGTransition
from dnd5e_engine.evaluation_state import NonCombatSnapshot
from dnd5e_engine.events import CheckRolled
from dnd5e_engine.rules.skills import Skill
from dnd5e_engine.types.checks import CheckActorState
from dnd5e_engine.types.combat import Combatant


class CheckEvaluation(TypedDict):
    status: Literal["accepted", "rejected", "unsupported", "needs_choice"]
    state_delta: StateDelta | None
    proposed_events: tuple[CheckRolled, ...]
    rng_transition: RNGTransition | None
    choice: CheckAdjudicationChoice | None
    error: RuleError | None


def _refuse(status: Literal["rejected", "unsupported"], code: str, reason: str) -> CheckEvaluation:
    return dict(
        status=status,
        state_delta=None,
        proposed_events=(),
        rng_transition=None,
        choice=None,
        error=RuleError(code=code, reason=reason),
    )


def evaluate_check(request: RuleEvaluationRequest, loader: AssetLoader) -> CheckEvaluation:
    snapshot, payload = request.state_snapshot, request.payload
    if not isinstance(snapshot, NonCombatSnapshot):
        return _refuse("unsupported", "check.snapshot", "checks require a noncombat snapshot")
    if not isinstance(payload, CheckPayload):
        raise EvaluationInvariantError("check payload lacks explicit adjudication")
    actors = {actor.entity_id: actor for actor in snapshot.character_states}
    actor = actors.get(request.actor_id)
    if actor is None or not actor.is_alive or actor.hp_current <= 0:
        return _refuse("rejected", "actor_invalid", "check actor is absent, dead or dying")
    if payload.target_id is not None and payload.target_id not in actors:
        return _refuse("rejected", "target_invalid", "check target is absent")
    if (
        payload.skill is not None
        and skill_ability(payload.skill) != payload.ability
        and actor.skill_check_bonuses.get(payload.skill, 0)
    ):
        return _refuse(
            "unsupported",
            "check.skill_bonus",
            "alternate governing ability with a projected skill bonus requires review",
        )
    if (
        payload.tool is not None
        or payload.redeem_granted_die is not None
        or payload.context != "generic"
        or payload.required_sense != "none"
        or set(payload.advantage + payload.disadvantage) - {"flag"}
    ):
        return _refuse(
            "unsupported",
            "check.capability",
            "tools, resources, senses, derived sources and action checks are not migrated",
        )
    if snapshot.effect_states or any(
        a.conditions
        or a.concentration_effect_id
        or a.trait_mechanics
        or a.classes
        or a.class_slug
        or a.subclass_slug
        or a.species_slug
        or a.granted_features
        or a.fighting_styles
        for a in actors.values()
    ):
        return _refuse(
            "unsupported", "check.effects", "actor effects and feature sources are not migrated"
        )
    for a in actors.values():
        if any(not 1 <= getattr(a, field) <= 30 for field in _SCORE_ATTR.values()) or (
            not 1 <= a.character_level <= 20
            or (a.proficiency_bonus_override is not None and a.proficiency_bonus_override < 0)
            or set(a.skill_proficiencies + a.skill_expertise) - set(get_args(Skill))
            or not set(a.skill_expertise) <= set(a.skill_proficiencies)
            or set(a.skill_check_bonuses) - set(get_args(Skill))
        ):
            return _refuse(
                "unsupported",
                "check.actor_state",
                "actor ability/proficiency facts are outside the reviewed contract",
            )
        for slug in a.carried_item_slugs:
            item = loader.get_item(slug)
            if item is None or item.passive_effects or item.requires_attunement:
                return _refuse(
                    "unsupported", "check.equipment", "equipment passive effects require migration"
                )
    if payload.dc is None:
        return dict(
            status="needs_choice",
            state_delta=None,
            proposed_events=(),
            rng_transition=None,
            choice=CheckAdjudicationChoice(
                kind="check.adjudication", actor_id=actor.entity_id, required_fields=("dc",)
            ),
            error=None,
        )
    combatants = []
    for state in actors.values():
        value = state.model_dump(mode="python")
        for field in ("spell_slots", "pact_slots", "spells_known", "custom_counters"):
            del value[field]
        value["death_saves"] = value["death_saves"] or {}
        combatants.append(Combatant.model_validate(value))
    caster = next(c for c in combatants if c.entity_id == actor.entity_id)
    rng = request.rng_context.state.restore()
    ctx = ActivityResolutionContext(
        rng=rng,
        caster=caster,
        targets=[c for c in combatants if c is not caster],
        event_emitter=lambda event: None,
        caster_abilities={
            ability: getattr(caster, field) for ability, field in _SCORE_ATTR.items()
        },
        check_states={c.entity_id: CheckActorState() for c in combatants},
    )
    event = resolve_check_request(payload, ctx)
    return dict(
        status="accepted",
        state_delta=StateDelta(expected_world_version=snapshot.world_version, operations=()),
        proposed_events=(event,),
        rng_transition=RNGTransition.between(request.rng_context, rng),
        choice=None,
        error=None,
    )
