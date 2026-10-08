"""Magic actions against existing area-owned sources, without spell-cast gates."""

from dataclasses import replace
from typing import TYPE_CHECKING, NoReturn

from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.areas import area_template
from dnd5e_engine.events import IntentSubmitted
from dnd5e_engine.live_reactions import attach_reaction_hooks
from dnd5e_engine.live_spell_delivery import (
    execute_activity_delivery,
    plan_activity_delivery,
    spec_from_intent,
)
from dnd5e_engine.spell_delivery import DeliveryPlanningError
from dnd5e_engine.spell_execution import admission_failure

if TYPE_CHECKING:
    from dnd5e_engine.orchestrator import PlayerIntent, _LiveCombat
    from dnd5e_engine.types.combat import Combatant


def activate_spell(live: "_LiveCombat", actor: "Combatant", intent: "PlayerIntent") -> None:
    from dnd5e_engine import orchestrator as orch

    def refuse(detail: str) -> NoReturn:
        raise orch.IntentRejectedError("invalid_spell_activation", detail)

    area = next((a for a in live.persistent_areas.areas if a.id == intent.source_id), None)
    if area is None or area.source_entity_id != actor.entity_id:
        refuse("source does not exist or belongs to another actor")
    activation = area.spec.ongoing_activation
    if activation is None or intent.activity_id not in activation.activity_ids:
        refuse("activity is not declared by this ongoing source")
    if (
        not actor.is_alive
        or actor.entity_id in live.dead_ids
        or area.source_kind != "spell"
        or (area.rounds_remaining is not None and area.rounds_remaining <= 0)
        or (
            area.concentration_identity is not None
            and area.concentration_identity not in live.concentration_chain.get(actor.entity_id, ())
        )
        or orch._rage_effect(live, actor.entity_id) is not None
    ):
        refuse("source is no longer active")
    # Only delivery decisions belong on an activation. Reject misleading cast,
    # charge, resource, check or feature inputs instead of silently ignoring them.
    allowed = {
        "intent_type",
        "source_id",
        "activity_id",
        "target_id",
        "target_ids",
        "direction",
        "target_zone_id",
        "excluded_target_ids",
    }
    defaults = orch.PlayerIntent(intent_type="activate_spell")
    if any(
        getattr(intent, name) != getattr(defaults, name)
        for name in type(intent).model_fields
        if name not in allowed
    ):
        refuse("activation accepts source, activity and delivery decisions only")
    spell = live.ruleset_loader.get_spell(area.source_id)
    activity = (
        next((a for a in spell.activities if a.id == intent.activity_id), None) if spell else None
    )
    if (
        spell is None
        or activity is None
        or admission_failure(spell, [activity], ongoing_carrier=True)
    ):
        refuse("ongoing activity lacks an admitted execution contract")
    if activity.persistent_area is not None or activity.kind not in ("save", "damage", "heal"):
        refuse("ongoing activity requires an unsupported carrier")
    if actor.entity_id not in live.actor_zone:
        refuse("source has no position")
    spec = spec_from_intent(intent)
    template = area_template(activity, cast_level=area.slot_level, base_level=area.base_spell_level)
    if template and template.anchor == "actor" and intent.target_zone_id is not None:
        refuse("actor-origin activity does not accept point placement")
    try:
        plan = plan_activity_delivery(
            live,
            actor,
            [activity],
            spec,
            range_spec=spell.range,
            cast_level=area.slot_level,
            base_level=area.base_spell_level,
        )
    except DeliveryPlanningError as error:
        refuse(str(error))
    named = intent.target_ids or ([intent.target_id] if intent.target_id else [])
    if len(set(named)) != len(named) or not set(named) <= set(plan.target_ids):
        refuse("named targets must be distinct legal affected creatures")
    charmer = orch._condition_source_entity(live, actor, "charmed")
    if charmer in plan.target_ids and activity.kind in ("save", "damage"):
        refuse("a harmful activation cannot target its owner's charmer")
    orch._action_economy_gate_failure(actor, intent, is_bonus_action=False, is_reaction_cast=False)
    orch._emit(
        live,
        IntentSubmitted(
            actor_id=actor.entity_id,
            intent_type=intent.intent_type,
            source_id=area.id,
            activity_id=activity.id,
        ),
    )
    current = orch._consume_action_budget(
        live, actor.entity_id, orch._ActionCost(False, False, None), intent.intent_type
    )
    before = len(live.event_log)
    ctx = build_activity_context(
        area.caster,
        [],
        rng=live.rng,
        event_emitter=lambda event: orch._emit(live, event),
        slot_level=area.slot_level,
        base_spell_level=area.base_spell_level,
        spellcasting_ability=area.spellcasting_ability,
        save_dc_override=area.save_dc,
        concentration=spell.concentration and activity.timing.effects_concentration,
        source_passive_effects=list(area.passive_effects),
        spell_book={},
        passive_damage_modifiers={},
        save_modifiers={},
    )
    ctx = attach_reaction_hooks(
        live,
        replace(
            ctx,
            lifecycle_source_kind="spell",
            lifecycle_source_slug=spell.slug,
            source_parent_id=area.id,
        ),
    )
    ctx = execute_activity_delivery(live, ctx, activity, plan.activities[0], spec, spell.slug)
    orch._fold_resolution_outcome(live, current, spell=None, actx=ctx, pre_event_count=before)
    orch._sync_legendary_resistance(live, before)
    orch._end_action(live, actor.entity_id, intent, allow_movement=True)
