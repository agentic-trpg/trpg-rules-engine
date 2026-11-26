"""Noncombat adjudicated checks through the shared Activity Resolver pipeline."""

from typing import Literal, TypedDict, get_args

from dnd5e_srd_data.loader import AssetLoader
from pydantic import ValidationError

from dnd5e_engine.activities.actor_stats import _SCORE_ATTR, skill_ability
from dnd5e_engine.activities.check_pipeline import (
    resolve_check_request,
    validate_check_effect_change,
)
from dnd5e_engine.activities.context import ActivityResolutionContext
from dnd5e_engine.effect_lifecycle import conditions_after_effect_application
from dnd5e_engine.evaluation_actor import combatant
from dnd5e_engine.evaluation_check_facts import CreatureSightFact, CreatureSightNeed
from dnd5e_engine.evaluation_contracts import (
    CheckAdjudicationChoice,
    CheckPayload,
    RuleError,
    RuleEvaluationRequest,
)
from dnd5e_engine.evaluation_delta import StateDelta
from dnd5e_engine.evaluation_effects import EffectState, explicit_effect_fields
from dnd5e_engine.evaluation_projection import EvaluationInvariantError
from dnd5e_engine.evaluation_region_effect import (
    claims_region_condition_identity,
    is_fixed_region_effect,
)
from dnd5e_engine.evaluation_rng import RNGTransition
from dnd5e_engine.evaluation_ruleset import verify_ruleset
from dnd5e_engine.evaluation_state import CharacterStateV2, NonCombatSnapshot
from dnd5e_engine.events import CheckRolled
from dnd5e_engine.rules.conditions import (
    Condition,
    condition_matches_compiled,
    conditions_charmer_social_advantage,
    conditions_grant_disadvantage_on_ability_checks,
)
from dnd5e_engine.rules.effects import effective_effects
from dnd5e_engine.rules.skills import Skill
from dnd5e_engine.types.checks import CheckActorState
from dnd5e_engine.types.conditions import ActiveCondition
from dnd5e_engine.types.effects import ActiveEffect


class CheckEvaluation(TypedDict):
    """Describe uncommitted check result fields returned to the evaluation envelope."""

    status: Literal["accepted", "rejected", "unsupported", "needs_choice"]
    state_delta: StateDelta | None
    proposed_events: tuple[CheckRolled, ...]
    rng_transition: RNGTransition | None
    choice: CheckAdjudicationChoice | None
    error: RuleError | None


class _InvalidCheckFactsError(ValueError):
    """Identify unsupported or inconsistent mechanical inputs before RNG use."""


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
    """Resolve a noncombat check with the admitted snapshot conditions and effects.

    Return uncommitted check evidence and RNG transition through the shared resolver.
    """
    admission = _admit_check(request, loader)
    if isinstance(admission, dict):
        return admission
    actor, target, check_states = admission
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
        check_states=check_states,
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


def check_fact_needs(
    request: RuleEvaluationRequest, loader: AssetLoader
) -> tuple[CreatureSightNeed, ...]:
    """Declare the exact missing World sight relationships without touching RNG.

    Requests rejected by ordinary check admission and checks still awaiting a
    DC need no World read. Formal evaluation reports any unsupported input.
    """
    verify_ruleset(request.ruleset_binding, loader)
    if (
        request.operation_kind != "rules.check"
        or not isinstance(request.payload, CheckPayload)
        or request.payload.dc is None
    ):
        return ()
    required: list[CreatureSightNeed] = []
    admission = _admit_check(request, loader, sight_needs=required)
    if isinstance(admission, dict):
        return ()
    supplied = {fact.need for fact in request.creature_sight_facts}
    return tuple(need for need in required if need not in supplied)


def _linked_condition_effect(
    condition: ActiveCondition, pending_links: dict[tuple[str, str], EffectState]
) -> EffectState | None:
    """Match an effect-supplied condition to its unique projected status and scope."""
    if condition.source_effect_id is None:
        if condition.source_entity_id == "implied:effect":
            raise _InvalidCheckFactsError("effect-sourced condition is missing its lineage")
        return None
    effect = pending_links.pop((condition.source_effect_id, condition.condition), None)
    if effect is None:
        raise _InvalidCheckFactsError("active condition has missing or duplicate effect lineage")
    if claims_region_condition_identity(effect.id, effect.origin) and condition.scope != "session":
        raise _InvalidCheckFactsError("regional condition lineage requires session scope")
    expected, _ = conditions_after_effect_application(
        (), effect.id, (condition.condition,), scope=condition.scope
    )
    if condition != expected[0]:
        raise _InvalidCheckFactsError(
            "active condition differs from its effect's projected lineage"
        )
    return effect


def _condition_source_id(
    condition: ActiveCondition, effect: EffectState | None, source_role: str
) -> str:
    """Resolve an exact source from a direct condition or its linked actor effect."""
    source = condition.source_entity_id
    if effect is not None:
        origin_type, _, origin_detail = effect.origin.partition(":")
        origin_name, _, origin_actor = origin_detail.partition(":")
        if origin_type == "cast" and origin_name and origin_actor:
            source = origin_actor
            if effect.lifecycle is not None and effect.lifecycle.source_id != source:
                raise _InvalidCheckFactsError(
                    f"{source_role}'s effect origin differs from its captured source"
                )
        elif effect.lifecycle is not None:
            source = effect.lifecycle.source_id
    if not source.strip() or source.startswith("implied:"):
        raise _InvalidCheckFactsError(f"check requires the active {source_role}'s source identity")
    return source


def _fear_sight_needs(
    actor_id: str,
    names: list[str],
    conditions: list[tuple[ActiveCondition, EffectState | None]],
) -> tuple[CreatureSightNeed, ...]:
    """Find distinct fear sources when the bound sight gate can affect this roll."""
    if (
        "frightened" not in names
        or not conditions_grant_disadvantage_on_ability_checks(
            ["frightened"], fear_source_in_sight=True
        )
        or conditions_grant_disadvantage_on_ability_checks(
            ["frightened"], fear_source_in_sight=False
        )
    ):
        return ()
    return tuple(
        CreatureSightNeed(observer_id=actor_id, source_id=source_id)
        for source_id in dict.fromkeys(
            _condition_source_id(condition, effect, "fear source")
            for condition, effect in conditions
            if condition.condition == Condition.FRIGHTENED
        )
    )


def _check_actor_effects(
    actor: CharacterStateV2,
    effects: list[EffectState],
    payload: CheckPayload,
    loader: AssetLoader | None,
    *,
    sight_facts: tuple[CreatureSightFact, ...] = (),
    sight_needs: list[CreatureSightNeed] | None = None,
) -> CheckActorState:
    """Project fixed effects and validate the roller's conditional sight inputs."""
    pending_links: dict[tuple[str, str], EffectState] = {}
    active: list[ActiveEffect] = []
    for effect in effects:
        if not effect.id.strip() or not effect.origin.strip():
            raise _InvalidCheckFactsError("active effect identity or origin is empty")
        regional = claims_region_condition_identity(effect.id, effect.origin)
        if regional and not is_fixed_region_effect(effect):
            raise _InvalidCheckFactsError("regional effect identity or definition is invalid")
        if effect.disabled:
            continue
        for status in effect.statuses:
            key = (effect.id, status)
            if key in pending_links:
                raise _InvalidCheckFactsError("active condition has ambiguous effect lineage")
            pending_links[key] = effect
        active.append(ActiveEffect.model_validate(explicit_effect_fields(effect)))

    charmer_ids: list[str] = []
    statuses = {condition.condition for condition in actor.conditions}
    for status in sorted(statuses):
        if status not in Condition:
            raise _InvalidCheckFactsError("active condition identity is unknown")
        # Compiled rules are covered by the evaluator binding when reduced
        # loaders omit the corresponding assets.
        definition = loader.get_condition(status) if loader is not None else None
        if definition is not None and (
            definition.slug != status or not condition_matches_compiled(definition)
        ):
            raise _InvalidCheckFactsError(
                f"{status} clauses differ from the pinned compiled rule projection"
            )
    condition_sources = []
    for record in actor.conditions:
        condition = ActiveCondition.model_validate(record.model_dump())
        if condition.condition == Condition.EXHAUSTION and condition.exhaustion_level < 1:
            raise _InvalidCheckFactsError("active Exhaustion requires a positive level")
        linked_effect = _linked_condition_effect(condition, pending_links)
        condition_sources.append((condition, linked_effect))

        if (
            payload.social_interaction
            and actor.entity_id == payload.target_id
            and conditions_charmer_social_advantage([condition.condition])
        ):
            charmer_ids.append(_condition_source_id(condition, linked_effect, "charmer"))
    if pending_links:
        raise _InvalidCheckFactsError("active effect is missing its projected condition")

    fear_source_in_sight = True
    if actor.entity_id == payload.actor_id:
        required = _fear_sight_needs(actor.entity_id, sorted(statuses), condition_sources)
        if sight_needs is not None:
            sight_needs.extend(required)
        else:
            known = {fact.need: fact.visible for fact in sight_facts}
            if set(required) != known.keys():
                raise _InvalidCheckFactsError(
                    "ability check requires visibility for its exact fear sources"
                )
            if required:
                fear_source_in_sight = any(known.values())
        for active_effect in effective_effects(active):
            for change in active_effect.changes:
                try:
                    validate_check_effect_change(change, payload)
                except ValueError as exc:
                    raise _InvalidCheckFactsError(f"invalid {change.key} change: {exc}") from exc
    return CheckActorState(
        effects=tuple(active),
        charmer_ids=tuple(charmer_ids),
        fear_source_in_sight=fear_source_in_sight,
    )


def _admit_check(
    request: RuleEvaluationRequest,
    loader: AssetLoader,
    *,
    sight_needs: list[CreatureSightNeed] | None = None,
) -> CheckEvaluation | tuple[CharacterStateV2, CharacterStateV2 | None, dict[str, CheckActorState]]:
    """Admit the roller and named target with their relevant fixed check inputs."""
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
    effects_by_actor: dict[str, list[EffectState]] = {actor_id: [] for actor_id in actors}
    for effect in snapshot.effect_states:
        if effect.target_id in effects_by_actor:
            effects_by_actor[effect.target_id].append(effect)
    try:
        check_states = {
            actor_id: _check_actor_effects(
                state,
                effects_by_actor[actor_id],
                payload,
                loader,
                sight_facts=request.creature_sight_facts,
                sight_needs=sight_needs,
            )
            for actor_id, state in actors.items()
        }
    except ValidationError:
        return _refuse(
            "unsupported", "check.effects", "active condition or effect facts are invalid"
        )
    except _InvalidCheckFactsError as exc:
        return _refuse("unsupported", "check.effects", str(exc))
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
    return actor, target, check_states
