"""Live-check projection, ingress legality and Help writeback.

This adapter owns no roll algorithm. Canonical CheckActivity and live actions
both call activities.check_pipeline.resolve_check_request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Never

from dnd5e_engine.activities.actor_stats import _SCORE_ATTR, proficiency_bonus_of
from dnd5e_engine.activities.check import check_dc, check_skill_ability
from dnd5e_engine.activities.check_pipeline import resolve_check_request
from dnd5e_engine.activities.context import ActivityResolutionContext
from dnd5e_engine.areas import has_line_of_effect
from dnd5e_engine.events import CheckCostOwner, CheckRolled, EffectExpired
from dnd5e_engine.rules.conditions import (
    conditions_auto_fail_check,
    conditions_charmer_social_advantage,
)
from dnd5e_engine.rules.skills import SKILL_CODE_TO_SLUG
from dnd5e_engine.types.checks import CheckActorState, CheckRequest

if TYPE_CHECKING:
    from dnd5e_srd_data.schema.common import Activity

    from dnd5e_engine.orchestrator import PlayerIntent, _FeatureInvocation, _LiveCombat
    from dnd5e_engine.types.combat import Combatant


# Audited SRD 5.2 pp. 9, 184, 187, 189. DC and sensory/social requirements
# remain explicit DM inputs. Influence has no attitude or narrative policy here.
ACTION_CHECKS = {
    "search": ("wis", frozenset({"insight", "medicine", "perception", "survival"})),
    "study": ("int", frozenset({"arcana", "history", "investigation", "nature", "religion"})),
    "influence": ("cha", frozenset({"deception", "intimidation", "performance", "persuasion"})),
}


def project_check_states(live: _LiveCombat) -> dict[str, CheckActorState]:
    from dnd5e_engine import orchestrator as orch

    return {
        c.entity_id: CheckActorState(
            effects=tuple(live.active_effects.get(c.entity_id, ())),
            fear_source_in_sight=orch._fear_source_in_sight(live, c),
            in_sunlight=live.scene_sunlight,
            help_grants=tuple(live.help_check_grants),
            charmer_ids=tuple(
                dict.fromkeys(
                    source
                    for ac in c.conditions
                    if conditions_charmer_social_advantage([ac.condition])
                    and (
                        source := orch._condition_source_entity(
                            live, c.model_copy(update={"conditions": [ac]}), "charmed"
                        )
                    )
                )
            ),
        )
        for c in live.initiative
    }


def _reject(message: str) -> Never:
    from dnd5e_engine.orchestrator import IntentRejectedError

    raise IntentRejectedError("target_invalid", message)


def validate_check_surface(live: _LiveCombat, actor: Combatant, intent: PlayerIntent) -> None:
    from dnd5e_engine import orchestrator as orch

    request = intent.check
    if intent.intent_type == "check":
        if request is None or request.actor_id != actor.entity_id:
            _reject("check requires a typed request for the submitting actor")
        assert request is not None
        if (
            intent.use_bonus_action
            or intent.redeem_granted_die
            or intent.feature_id
            or intent.item_id
            or intent.spell_id
        ):
            _reject("generic checks spend an Action; use the nested redemption payload")
        if request.context not in ("generic", *ACTION_CHECKS):
            _reject("rules-owned/internal contexts cannot be submitted as generic actions")
        if request.context in ACTION_CHECKS:
            ability, skills = ACTION_CHECKS[request.context]
            animal = request.context == "influence" and request.skill == "animal_handling"
            valid = request.ability == ability and request.skill in skills
            if animal:
                target = (
                    orch._find_combatant(live, request.target_id) if request.target_id else None
                )
                valid = (
                    request.ability == "wis"
                    and target is not None
                    and target.creature_type in ("beast", "monstrosity")
                )
            if not valid or request.dc is None:
                _reject("action wrapper requires an audited ability/skill and an explicit DM DC")
        if request.context == "influence" and (
            not request.social_interaction or not request.target_id
        ):
            _reject("Influence requires an explicit social interaction and target")
        validate_live_request(live, request)
    elif request is not None and intent.intent_type not in (
        "use_feature",
        "use_item",
        "cast_spell",
    ):
        _reject("check payload is only valid for check actions or a canonical CheckActivity")
    if intent.help_check is not None:
        if intent.intent_type != "help":
            _reject("ability-check assistance is a Help action")
        spec = intent.help_check
        target = orch._find_combatant(live, spec.beneficiary_id)
        if (
            not spec.assistance_possible
            or spec.skill not in actor.skill_proficiencies
            or target is None
            or not target.is_alive
            or target.entity_id in live.dead_ids
            or target.entity_id == actor.entity_id
            or target.entity_id not in orch._allied_ids(live, actor.entity_id)
        ):
            _reject(
                "Help requires a possible assistance, a proficient helper and another living ally"
            )
        assert target is not None
        start, end = live.actor_zone.get(actor.entity_id), live.actor_zone.get(target.entity_id)
        if (
            start is None
            or end is None
            or not live.topology.within_range(start, end, spec.range_ft)
        ):
            _reject("Help beneficiary is outside the DM-declared assistance range")
        if spec.assistance == "physical" and not has_line_of_effect(live.topology, start, end):
            _reject("physical assistance is blocked")
        if spec.assistance == "verbal" and conditions_auto_fail_check(
            [c.condition for c in target.conditions], "hearing"
        ):
            _reject("beneficiary cannot hear verbal assistance")


def validate_live_request(live: _LiveCombat, request: CheckRequest) -> None:
    from dnd5e_engine import orchestrator as orch

    actor = orch._find_combatant(live, request.actor_id)
    if actor is None or not actor.is_alive or request.actor_id in live.dead_ids:
        _reject("rolling actor is not living in this combat")
    if request.target_id is not None:
        target = orch._find_combatant(live, request.target_id)
        if target is None or not target.is_alive or target.entity_id in live.dead_ids:
            _reject("check target is not living in this combat")
    if request.redeem_granted_die and (
        request.dc is None or orch._granted_die(live, actor) is None
    ):
        _reject("redemption requires a DC and a redeemable held Bardic Inspiration die")


def resolve_live_check(
    live: _LiveCombat, request: CheckRequest, *, cost_owner: CheckCostOwner = "action"
) -> CheckRolled:
    from dnd5e_engine import orchestrator as orch

    actor = orch._find_combatant(live, request.actor_id)
    assert actor is not None  # pre-payment ingress validation
    target = orch._find_combatant(live, request.target_id) if request.target_id else None
    ctx = ActivityResolutionContext(
        rng=live.rng,
        caster=actor,
        targets=[target] if target else [],
        caster_abilities={},
        event_emitter=lambda e: orch._emit(live, e),
        check_states=project_check_states(live),
        granted_die=orch._granted_die(live, actor) if request.redeem_granted_die else None,
    )
    return resolve_check_request(request, ctx, cost_owner=cost_owner)


def observe_check(live: _LiveCombat, event: CheckRolled) -> None:
    from dnd5e_engine import orchestrator as orch

    if "help" in event.advantage_sources:
        skill = SKILL_CODE_TO_SLUG.get(event.skill or "", event.skill)
        grant = next(
            (
                g
                for g in live.help_check_grants
                if g.beneficiary_id == event.actor_id and g.skill == skill
            ),
            None,
        )
        if grant is not None:
            live.help_check_grants.remove(grant)
    if event.inspiration_bonus:
        effect = orch._inspiration_effect(live, event.actor_id)
        if effect is not None:
            orch._emit(
                live,
                EffectExpired(
                    target_id=event.actor_id,
                    effect_id=effect.id,
                    origin=effect.origin,
                    reason="expended",
                ),
            )


def validate_activity_checks(
    live: _LiveCombat,
    actor: Combatant,
    intent: PlayerIntent,
    activities: list[Activity],
    targets: list[Combatant],
    *,
    spellcasting_ability: str | None,
) -> None:
    """Validate canonical DC/ability and any override before activation is paid."""
    checks = [a for a in activities if a.kind == "check"]
    if intent.check is not None and len(checks) != 1:
        _reject("a typed check override requires exactly one CheckActivity")
    ctx = ActivityResolutionContext(
        rng=live.rng,
        caster=actor,
        targets=targets,
        event_emitter=lambda _: None,
        caster_abilities={ab: getattr(actor, attr) for ab, attr in _SCORE_ATTR.items()},
        caster_proficiency_bonus=proficiency_bonus_of(actor),
        spellcasting_ability=spellcasting_ability,
    )
    for activity in checks:
        # Canonical check DCs are scalar formulas. Reject dice DCs on the live
        # path rather than drawing during legality or paying half an action.
        try:
            skill, ability = check_skill_ability(activity)
            dc = check_dc(activity, ctx, allow_dice=False)
            if dc is not None and dc < 0:
                _reject("live check DC cannot be negative")
        except ValueError as error:
            _reject(str(error))
        if intent.check is not None:
            request = intent.check
            rolling_actor = targets[0] if targets else actor
            if (
                request.actor_id != rolling_actor.entity_id
                or request.ability != ability
                or (request.skill or request.tool) != SKILL_CODE_TO_SLUG.get(skill or "", skill)
                or request.dc != dc
                or request.context != "activity"
            ):
                _reject("check override cannot change canonical actor, ability, proficiency or DC")
            validate_live_request(live, request)


def preflight_activity_checks(
    live: _LiveCombat, actor: Combatant, intent: PlayerIntent, invocation: _FeatureInvocation | None
) -> None:
    from dnd5e_engine import orchestrator as orch

    if intent.intent_type not in ("use_item", "use_feature", "cast_spell"):
        return
    resolved = orch._resolve_intent_activities(
        intent,
        invocation,
        actor,
        stat_block_slug=orch._current_stat_block_slug(live, actor.entity_id),
    )
    if intent.check is not None or any(a.kind == "check" for a in resolved.activities):
        targets = orch._resolve_targets(
            live, actor, intent, resolved.activities, resolved.cast_spell
        )
        validate_activity_checks(
            live,
            actor,
            intent,
            resolved.activities,
            targets,
            spellcasting_ability=resolved.spellcasting_ability,
        )
