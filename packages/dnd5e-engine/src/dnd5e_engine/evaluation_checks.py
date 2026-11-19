"""Noncombat adjudicated checks through the shared Activity Resolver pipeline."""

from typing import Literal, TypedDict, get_args

from dnd5e_srd_data.loader import AssetLoader
from pydantic import ValidationError

from dnd5e_engine.activities.actor_stats import _SCORE_ATTR, skill_ability
from dnd5e_engine.activities.check_pipeline import resolve_check_request
from dnd5e_engine.activities.context import ActivityResolutionContext
from dnd5e_engine.effect_lifecycle import conditions_after_effect_application
from dnd5e_engine.evaluation_actor import combatant
from dnd5e_engine.evaluation_contracts import (
    CheckAdjudicationChoice,
    CheckPayload,
    RuleError,
    RuleEvaluationRequest,
)
from dnd5e_engine.evaluation_delta import StateDelta
from dnd5e_engine.evaluation_effects import EffectDurationState, EffectState
from dnd5e_engine.evaluation_projection import EvaluationInvariantError
from dnd5e_engine.evaluation_region_effect import (
    claims_region_condition_identity,
    region_condition_status,
)
from dnd5e_engine.evaluation_rng import RNGTransition
from dnd5e_engine.evaluation_state import CharacterStateV2, NonCombatSnapshot
from dnd5e_engine.events import CheckRolled
from dnd5e_engine.rules.conditions import Condition, standalone_condition_matches_compiled
from dnd5e_engine.rules.skills import Skill
from dnd5e_engine.types.checks import CheckActorState
from dnd5e_engine.types.conditions import ActiveCondition

_POISONED = Condition.POISONED.value
_UNTIMED = EffectDurationState(
    rounds=None, turns=None, seconds=None, start_round=None, start_turn=None
)


class CheckEvaluation(TypedDict):
    """Describe uncommitted check result fields returned to the evaluation envelope."""

    status: Literal["accepted", "rejected", "unsupported", "needs_choice"]
    state_delta: StateDelta | None
    proposed_events: tuple[CheckRolled, ...]
    rng_transition: RNGTransition | None
    choice: CheckAdjudicationChoice | None
    error: RuleError | None


def _refuse(status: Literal["rejected", "unsupported"], code: str, reason: str) -> CheckEvaluation:
    """Build a refusal with no Delta, proposed events or RNG transition."""
    return dict(
        status=status,
        state_delta=None,
        proposed_events=(),
        rng_transition=None,
        choice=None,
        error=RuleError(code=code, reason=reason),
    )


def evaluate_check(
    request: RuleEvaluationRequest,
    loader: AssetLoader,
    *,
    tool_slug: str | None = None,
    tool_advantage: bool = False,
) -> CheckEvaluation:
    """Resolve an admitted noncombat check with supplied active Poisoned facts.

    Return uncommitted check evidence and RNG transition through the shared resolver.
    """
    admission = _admit_check(request, loader)
    if isinstance(admission, dict):
        return admission
    actor, target = admission
    payload, snapshot = request.payload, request.state_snapshot
    if not isinstance(payload, CheckPayload) or not isinstance(snapshot, NonCombatSnapshot):
        raise EvaluationInvariantError("unreachable admitted check types")
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
    caster = combatant(actor)
    # The shared resolver reads another actor only for a social target's conditions.
    targets = (
        [combatant(target)]
        if payload.social_interaction and target is not None and target is not actor
        else []
    )
    rng = request.rng_context.state.restore()
    ctx = ActivityResolutionContext(
        rng=rng,
        caster=caster,
        targets=targets,
        event_emitter=lambda event: None,
        caster_abilities={
            ability: getattr(caster, field) for ability, field in _SCORE_ATTR.items()
        },
        check_states={c.entity_id: CheckActorState() for c in (caster, *targets)},
    )
    event = (
        resolve_check_request(payload, ctx, tool_slug=tool_slug, tool_advantage=tool_advantage)
        if tool_slug is not None or tool_advantage
        else resolve_check_request(payload, ctx)
    )
    return dict(
        status="accepted",
        state_delta=StateDelta(expected_world_version=snapshot.world_version, operations=()),
        proposed_events=(event,),
        rng_transition=RNGTransition.between(request.rng_context, rng),
        choice=None,
        error=None,
    )


def check_support_failure(
    request: RuleEvaluationRequest, loader: AssetLoader
) -> CheckEvaluation | None:
    """Return a refusal for unsupported check dependencies, or None when admitted."""
    admission = _admit_check(request, loader)
    return admission if isinstance(admission, dict) else None


def _check_condition_failure(
    actors: dict[str, CharacterStateV2], effects: tuple[EffectState, ...], loader: AssetLoader
) -> str | None:
    """Validate active Poisoned and exact links to the admitted condition-only effects."""
    pending_links: dict[str, dict[str, bool]] = {actor_id: {} for actor_id in actors}
    for effect in effects:
        links = pending_links.get(effect.target_id)
        if links is None:
            continue
        if (
            not effect.id.strip()
            or not effect.origin.strip()
            or effect.id in links
            or effect.statuses != {_POISONED}
            or effect.duration != _UNTIMED
            or effect.disabled
            or effect.transfer
            or effect.changes
            or effect.action_policy is not None
            or effect.flags
            or effect.lifecycle is not None
            or effect.end_effects
        ):
            return "only unambiguous, untimed Poisoned status effects are migrated"
        regional = claims_region_condition_identity(effect.id, effect.origin)
        if regional and region_condition_status(effect.id, effect.origin) != _POISONED:
            return "regional effect identity does not match its Poisoned status"
        links[effect.id] = regional

    if any(actor.conditions for actor in actors.values()):
        # The compiled rule is part of the verified evaluator binding. Reduced
        # loaders may omit it, while supplied overrides must still agree.
        definition = loader.get_condition(_POISONED)
        if definition is not None and (
            definition.slug != _POISONED or not standalone_condition_matches_compiled(definition)
        ):
            return "Poisoned clauses differ from the pinned compiled rule projection"

    for actor_id, actor in actors.items():
        links = pending_links[actor_id]
        for condition in actor.conditions:
            if condition.condition != _POISONED:
                return "only active Poisoned is migrated for noncombat checks"
            effect_id = condition.source_effect_id
            if effect_id is None:
                if condition.source_entity_id == "implied:effect":
                    return "effect-sourced condition is missing its lineage"
                try:
                    ActiveCondition.model_validate(condition.model_dump())
                except ValidationError:
                    return "active condition source is invalid"
                continue
            regional_link = links.pop(effect_id, None)
            if regional_link is None:
                return "active condition has missing or duplicate effect lineage"
            if regional_link and condition.scope != "session":
                return "regional condition lineage requires session scope"
            expected, _ = conditions_after_effect_application(
                (), effect_id, (_POISONED,), scope=condition.scope
            )
            if condition.model_dump() != expected[0].model_dump():
                return "active condition differs from its effect's projected lineage"
        if links:
            return "active effect is missing its projected condition"
    return None


def _admit_check(
    request: RuleEvaluationRequest, loader: AssetLoader
) -> CheckEvaluation | tuple[CharacterStateV2, CharacterStateV2 | None]:
    """Admit the roller and named target, including reviewed Poisoned facts."""
    snapshot, payload = request.state_snapshot, request.payload
    if not isinstance(snapshot, NonCombatSnapshot):
        return _refuse("unsupported", "check.snapshot", "checks require a noncombat snapshot")
    if snapshot.combat_setup is not None:
        return _refuse(
            "unsupported", "check.setup", "pending combat setup requires its candidate evaluator"
        )
    if not isinstance(payload, CheckPayload):
        raise EvaluationInvariantError("check payload lacks explicit adjudication")
    actor_ids = (request.actor_id, payload.target_id)
    actors = {
        actor.entity_id: actor
        for actor in snapshot.character_states
        if actor.entity_id in actor_ids
    }
    actor = actors.get(request.actor_id)
    if actor is None or not actor.is_alive or actor.hp_current <= 0:
        return _refuse("rejected", "actor_invalid", "check actor is absent, dead or dying")
    target = actors.get(payload.target_id) if payload.target_id is not None else None
    if payload.target_id is not None and target is None:
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
    if any(
        a.concentration_effect_id
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
            "unsupported",
            "check.effects",
            "actor concentration and feature sources are not migrated",
        )
    if failure := _check_condition_failure(actors, snapshot.effect_states, loader):
        return _refuse("unsupported", "check.effects", failure)
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
    return actor, target
