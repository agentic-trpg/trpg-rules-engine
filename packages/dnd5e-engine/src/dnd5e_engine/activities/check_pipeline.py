"""One pure ability-check resolver for live requests and canonical activities.

Legality and payment belong to the ingress. This module never imports the
orchestrator, infers semantics from a skill name, or draws during projection.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Literal

from dnd5e_srd_data.schema.monster import MonsterTraitMechanic

from dnd5e_engine.activities.actor_stats import _SCORE_ATTR, check_modifier, proficiency_bonus_of
from dnd5e_engine.activities.d20 import AdvantageSources, resolve_mode, roll_d20_test
from dnd5e_engine.activities.dice import roll_expr
from dnd5e_engine.events import AdvantageSource, CheckCostOwner, CheckRolled
from dnd5e_engine.rules.conditions import (
    conditions_auto_fail_check,
    conditions_charmer_social_advantage,
    conditions_grant_disadvantage_on_ability_checks,
    d20_test_penalty,
)
from dnd5e_engine.rules.effects import apply_changes_to_check, projected_boolean_flag
from dnd5e_engine.rules.skills import SKILL_CODE_TO_SLUG
from dnd5e_engine.types.checks import CheckActorState, CheckRequest
from dnd5e_engine.types.combat import Combatant
from dnd5e_engine.types.effects import ActiveEffect

if TYPE_CHECKING:
    from .context import ActivityResolutionContext


def activity_check_modifier(
    ctx: ActivityResolutionContext, actor: Combatant, *, skill: str | None, ability: str
) -> int:
    """Compatibility adapter for explicit standalone, resolved modifier sidecars."""
    penalty = ctx.d20_test_penalty.get(actor.entity_id, d20_test_penalty(actor.conditions))
    actor_mods = ctx.check_modifiers.get(actor.entity_id, {})
    if skill is not None:
        skills = actor_mods.get("skills", {})
        for key in (SKILL_CODE_TO_SLUG.get(skill), skill):
            if key is not None and key in skills:
                return int(skills[key]) + penalty
    return int(actor_mods.get("ability_mods", {}).get(ability, 0)) + penalty


def _sources(
    request: CheckRequest, actor: Combatant, ctx: ActivityResolutionContext, state: CheckActorState
) -> AdvantageSources:
    adv: list[AdvantageSource] = list(request.advantage)
    dis: list[AdvantageSource] = list(request.disadvantage)
    legacy = ctx.check_modifiers.get(actor.entity_id, {})
    condition_dis = (
        bool(legacy["disadvantage"])
        if actor.entity_id not in ctx.check_states and "disadvantage" in legacy
        else conditions_grant_disadvantage_on_ability_checks(
            [c.condition for c in actor.conditions], fear_source_in_sight=state.fear_source_in_sight
        )
    )
    if condition_dis:
        dis.append("condition:attacker")
    if request.skill == "stealth" and actor.stealth_disadvantage:
        dis.append("armor")
    if state.in_sunlight and MonsterTraitMechanic.SUNLIGHT_SENSITIVITY in actor.trait_mechanics:
        dis.append("trait")
    if any(
        g.beneficiary_id == actor.entity_id and g.skill == request.skill for g in state.help_grants
    ):
        adv.append("help")
    target = next((c for c in ctx.targets if c.entity_id == request.target_id), None)
    if request.social_interaction and request.target_id:
        target_state = ctx.check_states.get(request.target_id, CheckActorState())
        direct_sources = (
            tuple(
                c.source_entity_id
                for c in target.conditions
                if conditions_charmer_social_advantage([c.condition])
            )
            if target is not None
            else ()
        )
        if actor.entity_id in target_state.charmer_ids or actor.entity_id in direct_sources:
            adv.append("charmed")
    changes = [c for effect in state.effects for c in effect.changes]
    for prefix, sources in (("advantage", adv), ("disadvantage", dis)):
        keys = [
            f"flags.{prefix}.check",
            f"flags.{prefix}.check.{request.ability}",
            f"flags.{prefix}.check.{_SCORE_ATTR[request.ability]}",
        ]
        if request.skill:
            keys.append(f"flags.{prefix}.check.{request.skill}")
        if any(projected_boolean_flag(changes, key) for key in keys):
            sources.append("effect")
    return AdvantageSources(tuple(adv), tuple(dis))


def _effect_bonus(
    request: CheckRequest, state: CheckActorState, ctx: ActivityResolutionContext
) -> int:
    # Normalize audited Foundry aliases once, then use the existing effect
    # arithmetic. Projection remains draw-free; bonus dice roll AFTER the d20.
    aliases = {
        "abilities.check": "check.bonus",
        "system.bonuses.abilities.check": "check.bonus",
        "abilities.skill": "check.skill_check.bonus",
        "system.bonuses.abilities.skill": "check.skill_check.bonus",
    }
    effects: list[ActiveEffect] = [
        effect.model_copy(
            update={
                "changes": [
                    c.model_copy(update={"key": aliases.get(c.key, c.key)}) for c in effect.changes
                ]
            }
        )
        for effect in state.effects
    ]
    total = 0
    buckets = ["check.bonus", f"check.{request.ability}.bonus"]
    if request.skill or request.tool:
        buckets.append("check.skill_check.bonus")
    else:
        buckets.append("check.ability_check.bonus")
    for bucket in buckets:
        total, _ = apply_changes_to_check(total, bucket, effects, ctx.rng)
    return total


def resolve_check_request(
    request: CheckRequest,
    ctx: ActivityResolutionContext,
    *,
    cost_owner: CheckCostOwner = "internal",
    activity_id: str | None = None,
    skill_label: str | None = None,
) -> CheckRolled:
    actor = next((c for c in [ctx.caster, *ctx.targets] if c.entity_id == request.actor_id), None)
    if actor is None:
        raise ValueError("rolling actor is absent from the check context")
    state = ctx.check_states.get(
        actor.entity_id,
        CheckActorState(
            effects=tuple(ctx.active_effects) if actor.entity_id == ctx.caster.entity_id else (),
            fear_source_in_sight=ctx.attacker_fear_source_in_sight,
            in_sunlight=ctx.attacker_in_sunlight,
        ),
    )
    if actor.entity_id in ctx.check_modifiers and actor.entity_id not in ctx.check_states:
        modifier = activity_check_modifier(
            ctx, actor, skill=skill_label or request.skill, ability=request.ability
        )
    else:
        modifier = check_modifier(
            actor, request.ability, request.skill
        ).total + ctx.d20_test_penalty.get(actor.entity_id, d20_test_penalty(actor.conditions))
        if request.tool in actor.tool_proficiencies:
            modifier += proficiency_bonus_of(actor)
    sources = _sources(request, actor, ctx, state)
    failure: Literal["sight", "hearing"] | None = None
    if request.required_sense in ("sight", "hearing") and conditions_auto_fail_check(
        [c.condition for c in actor.conditions], request.required_sense
    ):
        failure = request.required_sense
    event = CheckRolled(
        actor_id=actor.entity_id,
        ability=request.ability,
        skill=skill_label or request.skill or request.tool,
        dc=request.dc,
        roll_total=0,
        succeeded=False,
        modifier=modifier,
        target_id=request.target_id,
        context=request.context,
        required_sense=request.required_sense,
        social_interaction=request.social_interaction,
        cost_owner=cost_owner,
        activity_id=activity_id,
        auto_failure=failure,
        advantage=resolve_mode(sources),
        sources=list(sources.advantage + sources.disadvantage),
        advantage_sources=list(sources.advantage),
        disadvantage_sources=list(sources.disadvantage),
    )
    if failure is None:
        forced = ctx.variables.get("force_check_d20")
        roll = roll_d20_test(
            ctx.rng, modifier, sources, forced_natural=int(forced) if forced is not None else None
        )
        eligible = (request.skill is not None and request.skill in actor.skill_proficiencies) or (
            request.tool is not None and request.tool in actor.tool_proficiencies
        )
        natural = max(10, roll.kept) if actor.reliable_talent and eligible else roll.kept
        if actor.entity_id in ctx.check_states or actor.entity_id not in ctx.check_modifiers:
            modifier += _effect_bonus(request, state, ctx)
        total = natural + modifier
        inspiration = 0
        if (
            request.redeem_granted_die
            and ctx.granted_die
            and request.dc is not None
            and total < request.dc
        ):
            inspiration = roll_expr(ctx.granted_die, ctx.rng)
            ctx.granted_die_rolls.append(inspiration)
            total += inspiration
        event = event.model_copy(
            update={
                "natural": natural,
                "rolled_natural": roll.kept,
                "roll_total": total,
                "modifier": modifier,
                "advantage": roll.mode,
                "reliable_talent_applied": natural != roll.kept,
                "inspiration_bonus": inspiration,
                "succeeded": total >= request.dc if request.dc is not None else None,
            }
        )
    ctx.event_emitter(event)
    # A canonical invocation can resolve several checks using one context.
    # Keep its draw-free snapshot in sync with the one-use live writeback so
    # later checks cannot reuse a consumed Help grant.
    if "help" in sources.advantage:
        grant = next(
            (
                g
                for g in state.help_grants
                if g.beneficiary_id == actor.entity_id and g.skill == request.skill
            ),
            None,
        )
        if grant is not None:
            for entity_id, actor_state in ctx.check_states.items():
                remaining = list(actor_state.help_grants)
                if grant in remaining:
                    remaining.remove(grant)
                    ctx.check_states[entity_id] = replace(actor_state, help_grants=tuple(remaining))
    return event
