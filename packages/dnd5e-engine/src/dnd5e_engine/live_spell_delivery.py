"""Shared live boundary for canonical spell and activity delivery.

Planning has no events, costs or random draws. Execution repeats planning
against current positions and hydrates each activity's own targets.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import fields, replace
from typing import TYPE_CHECKING, Any

from dnd5e_engine.activities.arithmetic import parse_expression
from dnd5e_engine.activities.build_context import _caster_mod, _save_dc, build_activity_context
from dnd5e_engine.activities.resolver import resolve_activity
from dnd5e_engine.events import AreaTargeted
from dnd5e_engine.lib_loader import get_lib_loader
from dnd5e_engine.spell_delivery import (
    ActivityDeliveryPlan,
    DeliveryPlan,
    DeliveryPlanningError,
    SpellDeliverySpec,
    plan_delivery,
)
from dnd5e_engine.spell_execution import ExecutionFailure, admission_failure, spell_review

if TYPE_CHECKING:
    from dnd5e_srd_data.schema.common import Activity
    from dnd5e_srd_data.schema.spell import Spell

    from dnd5e_engine.activities.context import ActivityResolutionContext
    from dnd5e_engine.orchestrator import PlayerIntent, _LiveCombat
    from dnd5e_engine.types.combat import Combatant


class SpellAdmissionError(DeliveryPlanningError):
    def __init__(self, failure: ExecutionFailure) -> None:
        super().__init__(failure.code, "unsupported_activity")
        self.failure = failure


def _validate_spell_inputs(
    actor: Combatant,
    spell: Spell,
    activities: Sequence[Activity],
    *,
    cast_level: int | None,
    ability: str | None,
    fixed_dc: int | None,
    fixed_attack: int | None,
) -> None:
    """Reuse formula validation on a disposable, draw-prohibited context."""
    from dnd5e_engine.activities.context import ActivityResolutionContext
    from dnd5e_engine.feature_runtime import DrawFreeRandom, validate_feature_formulas

    review = spell_review(spell)
    if review is not None and review.classification == "host_narrative":
        return
    ctx = ActivityResolutionContext(
        caster=actor,
        targets=[],
        rng=DrawFreeRandom(0),
        event_emitter=lambda _: None,
        caster_abilities={
            "str": actor.strength,
            "dex": actor.dexterity,
            "con": actor.constitution,
            "int": actor.intelligence,
            "wis": actor.wisdom,
            "cha": actor.charisma,
        },
        caster_level=actor.character_level,
        slot_level=cast_level if cast_level is not None else spell.level,
        base_spell_level=spell.level,
        spellcasting_ability=ability,
        concentration=spell.concentration,
        source_passive_effects=list(spell.passive_effects),
        spell_book={},
        save_dc_override=fixed_dc,
    )
    if ability is None and fixed_dc is None:
        # Preserve the existing draw-free DC fallback for classless actors.
        # This does not fabricate a governing ability for @mod expressions.
        ctx = replace(
            ctx,
            save_dc_override=_save_dc(
                actor,
                _caster_mod(actor),
                caster_abilities=ctx.caster_abilities,
                caster_proficiency_bonus=ctx.caster_proficiency_bonus,
                spellcasting_ability=None,
            ),
        )
    if fixed_attack is not None:
        ctx = replace(ctx, attack_bonus_override=fixed_attack)
    for activity in activities:
        try:
            validate_feature_formulas(spell, activity, ctx)
        except ValueError as exc:
            raise SpellAdmissionError(
                ExecutionFailure(
                    code="invalid_formula",
                    spell_id=spell.slug,
                    activity_id=activity.id,
                    mechanisms=("formula_inputs",),
                )
            ) from exc


def spec_from_intent(intent: PlayerIntent) -> SpellDeliverySpec:
    return SpellDeliverySpec(
        primary_target_id=intent.target_id,
        selected_target_ids=tuple(intent.target_ids) if intent.target_ids is not None else None,
        origin_cell=intent.target_zone_id,
        direction=intent.direction,
        excluded_target_ids=intent.excluded_target_ids,
        source_kind="item_cast" if intent.item_id else "direct_spell",
        source_item_id=intent.item_id,
        source_activity_id=intent.activity_id,
        effect_selections=intent.effect_selections,
        willing_target_ids=intent.willing_target_ids,
    )


def _range_ft(value: Any) -> int | None:
    if value is None:
        return None
    if value.units == "touch":
        return 5
    if value.units == "ft" and value.value is not None:
        try:
            return int(value.value)
        except (TypeError, ValueError):
            return None
    return None


def plan_activity_delivery(
    live: _LiveCombat,
    actor: Combatant,
    activities: Sequence[Activity],
    spec: SpellDeliverySpec,
    *,
    range_spec: Any = None,
    execution: bool = False,
    cast_level: int | None = None,
    base_level: int | None = None,
    default_target_ids: tuple[str, ...] = (),
    expand_areas: bool = True,
) -> DeliveryPlan:
    from dnd5e_engine import orchestrator as orch

    creatures = [c for c in live.initiative if c.is_alive and c.entity_id not in live.dead_ids]
    plans: list[ActivityDeliveryPlan] = []
    for activity in activities:
        metric = range_spec if range_spec is not None else activity.range
        if (
            activity.range.override
            and activity.timing.trigger == "immediate"
            and activity.persistent_area is None
        ):
            metric = activity.range
        if activity.timing.trigger != "immediate" and activity.persistent_area is None:
            metric = range_spec
        if activity.kind == "summon":
            # A construct's named attack victim is measured from its placed
            # force by the existing typed conjuration gate, not its caster.
            metric = None
        defaults = default_target_ids
        if not defaults and (
            activity.target.affects.type == "self"
            or (
                not spec.primary_target_id
                and spec.selected_target_ids is None
                and getattr(activity, "effects", None)
            )
        ):
            defaults = (actor.entity_id,)
        plans.extend(
            plan_delivery(
                [activity],
                spec,
                actor_id=actor.entity_id,
                topology=live.topology,
                positions=live.actor_zone,
                creatures=creatures,
                enemy_ids=frozenset(
                    c.entity_id
                    for c in creatures
                    if orch._is_enemy(live, actor.entity_id, c.entity_id)
                ),
                ally_ids=frozenset(orch._allied_ids(live, actor.entity_id)),
                range_ft=_range_ft(metric),
                default_target_ids=defaults,
                execution=execution,
                expand_areas=expand_areas,
                cast_level=cast_level,
                base_level=base_level,
            ).activities
        )
    return DeliveryPlan(tuple(plans))


def plan_spell_delivery(
    live: _LiveCombat,
    actor: Combatant,
    spell: Spell,
    spec: SpellDeliverySpec,
    *,
    execution: bool = False,
) -> DeliveryPlan:
    return plan_activity_delivery(
        live,
        actor,
        delivery_activities(spell, spec),
        spec,
        range_spec=spell.range,
        execution=execution,
    )


def delivery_activities(spell: Spell, spec: SpellDeliverySpec) -> list[Activity]:
    """Choose a canonical casting activity, retaining its lifecycle payloads.

    Foundry activation overrides declare separately invoked alternatives.
    Their conditions are never interpreted as permission to execute a sibling.
    """
    chosen = next((a for a in spell.activities if a.id == spec.source_activity_id), None)
    review = spell_review(spell)
    if review is not None:
        roles = {a.activity_id: a.role for a in review.activities}
        return [
            a
            for a in spell.activities
            if a is chosen
            or roles.get(a.id) in ("delayed", "persistent")
            or (chosen is None and roles.get(a.id) == "cast")
            or a.id not in roles
        ]
    if chosen is not None:
        if chosen.timing.trigger == "manual":
            raise DeliveryPlanningError("manual activity delivery is deferred", "unsupported_area")
        return [
            a
            for a in spell.activities
            if a is chosen
            or a.timing.trigger in ("turn_start", "turn_end")
            or a.persistent_area is not None
        ]
    first = next((a for a in spell.activities if a.timing.trigger == "immediate"), None)
    primary = [
        a
        for a in spell.activities
        if a is first
        or (not a.activation.override and not a.range.override and a.timing.trigger == "immediate")
        or a.timing.trigger in ("turn_start", "turn_end")
        or a.persistent_area is not None
    ]
    return primary or list(spell.activities[:1])


def preflight_delivery(
    live: _LiveCombat,
    actor: Combatant,
    activities: Sequence[Activity],
    spec: SpellDeliverySpec,
    *,
    spell: Spell | None = None,
    chain: tuple[str, ...] = (),
    expand_areas: bool = True,
    cast_level: int | None = None,
    delegated_level_override: int | None = None,
    spellcasting_ability: str | None = None,
    fixed_dc: int | None = None,
    fixed_attack: int | None = None,
    _validated_selections: set[tuple[str, str, str]] | None = None,
) -> DeliveryPlan:
    """Resolve delegated geometry recursively before the outer caller pays."""
    from dnd5e_engine import orchestrator as orch

    root = _validated_selections is None
    validated = set() if root else _validated_selections
    assert validated is not None
    spellcasting_ability = spellcasting_ability or (
        actor.spellcasting_ability
        if spec.source_kind == "monster_cast"
        else orch._resolve_caster_spellcasting_ability(actor)
        if spec.source_kind == "direct_spell"
        else None
    )
    if spell is not None:
        activities = delivery_activities(spell, spec)
        _validate_cast_count(
            activities, spec, cast_level if cast_level is not None else spell.level
        )
    plan = plan_activity_delivery(
        live,
        actor,
        activities,
        spec,
        range_spec=spell.range if spell else None,
        expand_areas=expand_areas,
        cast_level=cast_level,
        base_level=spell.level if spell else None,
    )
    if spell is not None:
        failure = admission_failure(
            spell, activities, direct_carrier=spec.source_kind == "direct_spell" and not chain
        )
        if failure is not None:
            raise SpellAdmissionError(failure)
        _validate_spell_inputs(
            actor,
            spell,
            activities,
            cast_level=cast_level,
            ability=spellcasting_ability,
            fixed_dc=fixed_dc,
            fixed_attack=fixed_attack,
        )
        _validate_effect_selections(spell, activities, plan, spec, cast_level, validated)
    for activity in activities:
        if activity.kind != "cast":
            continue
        uuid = activity.spell.uuid
        child = get_lib_loader().get_spell_by_uuid(uuid)
        if uuid in chain or child is None:
            raise DeliveryPlanningError(
                "delegated spell is unresolved or cyclic", "unsupported_area"
            )
        if activity.spell.level is not None and not child.level <= activity.spell.level <= 9:
            raise DeliveryPlanningError("delegated spell level is invalid", "unsupported_area")
        level = (
            delegated_level_override
            if delegated_level_override is not None
            else activity.spell.level
        )
        preflight_delivery(
            live,
            actor,
            child.activities,
            spec,
            spell=child,
            chain=(*chain, uuid),
            cast_level=level,
            spellcasting_ability=activity.spell.ability or spellcasting_ability,
            fixed_dc=activity.spell.challenge.save
            if activity.spell.challenge.override
            else (fixed_dc if not chain else None),
            fixed_attack=activity.spell.challenge.attack
            if activity.spell.challenge.override
            else (fixed_attack if not chain else None),
            _validated_selections=validated,
        )
        # Support is shared even for concentration children; outer item
        # ownership is a separate, still-deferred delivery capability.
        if spec.source_kind == "item_cast" and child.concentration:
            raise DeliveryPlanningError("item concentration is deferred", "unsupported_area")
    willing: set[str] = set()
    by_activity = {a.id: a for a in activities}
    for planned in plan.activities:
        activity = by_activity[planned.activity_id]
        if activity.target.requires_willing:
            willing.update(planned.target_ids)
            if not planned.target_ids or not set(planned.target_ids) <= set(
                spec.willing_target_ids
            ):
                raise DeliveryPlanningError("host must attest the selected targets are willing")
        if activity.target.requires_sight:
            from dnd5e_engine import orchestrator as orch

            if any(
                (selected_target := orch._find_combatant(live, target)) is None
                or not orch._combatant_can_see(live, actor, selected_target)
                for target in planned.target_ids
            ):
                raise DeliveryPlanningError("source must see the selected creature")
    if root:
        keys = [(s.spell_id, s.activity_id, s.target_id) for s in spec.effect_selections]
        if len(keys) != len(set(keys)) or set(keys) != validated:
            raise DeliveryPlanningError("effect selections repeat or name an unselected payload")
        if len(spec.willing_target_ids) != len(set(spec.willing_target_ids)):
            raise DeliveryPlanningError("willing targets repeat")
        if not set(spec.willing_target_ids) <= ({key[2] for key in validated} | willing):
            raise DeliveryPlanningError("willing attestations must name selected effect targets")
    return plan


def _validate_effect_selections(
    spell: Spell,
    activities: Sequence[Activity],
    plan: DeliveryPlan,
    spec: SpellDeliverySpec,
    cast_level: int | None,
    validated: set[tuple[str, str, str]],
) -> None:
    """Validate exact, complete choices against the draw-free target plan."""
    level = spell.level if cast_level is None else cast_level
    by_id = {p.activity_id: p for p in plan.activities}
    for activity in activities:
        if getattr(activity, "effect_selection", None) is None:
            continue
        targets = by_id[activity.id].target_ids
        selections = [
            s
            for s in spec.effect_selections
            if s.spell_id == spell.slug and s.activity_id == activity.id
        ]
        if not targets or len(targets) != len(set(targets)):
            raise DeliveryPlanningError("effect selection requires distinct live targets")
        if len(selections) != len(targets) or {s.target_id for s in selections} != set(targets):
            raise DeliveryPlanningError("each target requires exactly one effect selection")
        candidates = {
            ref.id
            for ref in getattr(activity, "effects", ())
            if (ref.level.min is None or level >= ref.level.min)
            and (ref.level.max is None or level <= ref.level.max)
        }
        if any(s.effect_id not in candidates for s in selections):
            raise DeliveryPlanningError("selected effect is not a legal canonical candidate")
        if activity.target.affects.type == "willing" and not set(targets) <= set(
            spec.willing_target_ids
        ):
            raise DeliveryPlanningError("host must attest the selected targets are willing")
        validated.update((s.spell_id, s.activity_id, s.target_id) for s in selections)


def _validate_cast_count(
    activities: Sequence[Activity], spec: SpellDeliverySpec, level: int
) -> None:
    from dnd5e_engine.spellcasting import resolve_target_count

    ids = spec.selected_target_ids
    if ids is None:
        return
    for activity in activities:
        if activity.target.affects.type in ("object", "space", "self"):
            continue
        count = resolve_target_count(activity.target.affects.count, cast_level=level)
        if count is not None and (
            len(ids) > count or (activity.kind != "damage" and len(set(ids)) != len(ids))
        ):
            raise DeliveryPlanningError("selected target count exceeds canonical cast count")


def emit_area_delivery(
    live: _LiveCombat,
    actor_id: str,
    source_id: str,
    plan: ActivityDeliveryPlan,
    spec: SpellDeliverySpec,
    *,
    source_spell_id: str | None = None,
    source_parent_id: str | None = None,
) -> None:
    from dnd5e_engine import orchestrator as orch

    if plan.template is None or plan.origin is None:
        return
    orch._emit(
        live,
        AreaTargeted(
            actor_id=actor_id,
            source_id=source_id,
            shape=plan.template.shape,
            size_ft=plan.template.size_ft,
            origin=plan.origin,
            direction=plan.direction,
            affected_ids=list(plan.target_ids),
            excluded_ids=list(plan.spared_ids),
            source_spell_id=source_spell_id,
            source_activity_id=plan.activity_id,
            source_parent_id=source_parent_id,
        ),
    )


def prepare_activity_delivery(
    live: _LiveCombat,
    ctx: ActivityResolutionContext,
    activity: Activity,
    plan: ActivityDeliveryPlan,
    spec: SpellDeliverySpec,
    source_id: str,
) -> ActivityResolutionContext:
    from dnd5e_engine import orchestrator as orch

    actor = orch._find_combatant(live, ctx.caster.entity_id) or ctx.caster
    by_id = {
        c.entity_id: c for c in live.initiative if c.is_alive and c.entity_id not in live.dead_ids
    }
    targets = [by_id[i] for i in plan.target_ids if i in by_id]
    payload = orch._build_hydration_payload(live, caster=actor)
    payload["passive_damage_modifiers"] = {
        entity_id: {key: value for key, value in entry.items() if key != "passive_spell_dc_bonus"}
        for entity_id, entry in payload["passive_damage_modifiers"].items()
    }
    saves = {}
    for entity_id, entry in payload["save_modifiers"].items():
        entry = dict(entry)
        ac_bonus = entry.get("passive_ac_bonus")
        if activity.kind != "attack":
            entry.pop("passive_ac_bonus", None)
        elif (
            isinstance(ac_bonus, str)
            and parse_expression(ac_bonus).has_dice
            and entity_id in ctx.passive_ac_bonus
        ):
            entry["passive_ac_bonus"] = str(ctx.passive_ac_bonus[entity_id])
        saves[entity_id] = entry
    payload["save_modifiers"] = saves
    fresh = build_activity_context(
        actor,
        targets,
        rng=ctx.rng,
        event_emitter=ctx.event_emitter,
        slot_level=ctx.slot_level,
        base_spell_level=ctx.base_spell_level,
        spellcasting_ability=ctx.spellcasting_ability,
        concentration=ctx.concentration,
        source_passive_effects=ctx.source_passive_effects,
        spell_book=ctx.spell_book,
        save_dc_override=ctx.save_dc_override,
        **orch._monster_context_kwargs(live, actor, targets, payload),
    )
    # Keep source magnitudes, hooks and shared output lists from the committed
    # cast; refresh defenses/perception/conditions for the new target roster.
    names = [f.name for f in fields(fresh) if f.name.startswith(("target_", "passive_save_"))]
    names += [
        "targets",
        "passive_damage_modifiers",
        "passive_ac_bonus",
        "check_modifiers",
        "check_states",
        "attacker_unseen_by",
        "attacker_invisibility_pierced_by",
        "legendary_resistance_armed",
        "legendary_resistances_remaining_by_entity",
    ]
    values = {name: getattr(fresh, name) for name in names}
    values["target_cover"] = (
        orch._target_cover_map(live, actor.entity_id, targets, origin_cell=plan.origin)
        if plan.origin
        else fresh.target_cover
    )
    rule = activity.target.creature_filter
    values.update(
        spell_delivery=spec,
        activity_source_id=f"{ctx.lifecycle_source_kind}:{source_id}:{activity.id}",
        target_auto_success_ids=frozenset(
            t.entity_id
            for t in targets
            if rule and t.creature_type in rule.auto_success_creature_types
        ),
    )
    if (
        ctx.lifecycle_source_kind != "spell"
        and activity.kind == "save"
        and activity.save.dc.calculation != "spellcasting"
    ):
        values["save_dc_override"] = None
    if activity.timing.trigger == "immediate" or activity.persistent_area is not None:
        emit_area_delivery(
            live,
            actor.entity_id,
            source_id,
            plan,
            spec,
            source_spell_id=source_id if ctx.lifecycle_source_kind == "spell" else None,
            source_parent_id=ctx.source_parent_id,
        )
    return replace(ctx, **values)


def execute_activity_delivery(
    live: _LiveCombat,
    ctx: ActivityResolutionContext,
    activity: Activity,
    activity_plan: ActivityDeliveryPlan,
    spec: SpellDeliverySpec,
    source_id: str,
) -> ActivityResolutionContext:
    ctx = prepare_activity_delivery(live, ctx, activity, activity_plan, spec, source_id)
    resolve_activity(activity, ctx)
    fold_forced_movement_requests(live, ctx)
    return ctx


def execute_spell_delivery(
    live: _LiveCombat,
    spell: Spell,
    ctx: ActivityResolutionContext,
    spec: SpellDeliverySpec | None = None,
) -> ActivityResolutionContext:
    from dnd5e_engine import orchestrator as orch
    from dnd5e_engine.timed_activities import resolve_spell_activities

    spec = (
        spec
        or ctx.spell_delivery
        or SpellDeliverySpec(selected_target_ids=tuple(t.entity_id for t in ctx.targets))
    )
    selected = delivery_activities(spell, spec)
    failure = admission_failure(spell, selected, direct_carrier=ctx.conjuration is not None)
    if failure is not None:
        raise SpellAdmissionError(failure)
    # Admission binds the complete source. Plan the reviewed selection directly;
    # a derived activity subset is not a new canonical spell to review again.
    plan = plan_activity_delivery(
        live,
        ctx.caster,
        selected,
        spec,
        range_spec=spell.range,
        execution=True,
        cast_level=ctx.slot_level,
        base_level=spell.level,
    )
    spell = spell.model_copy(update={"activities": selected})
    by_id = {c.entity_id: c for c in live.initiative}
    ctx = replace(ctx, targets=[by_id[i] for i in plan.target_ids if i in by_id])

    def prepare(
        activity: Activity, current: ActivityResolutionContext
    ) -> ActivityResolutionContext:
        from dnd5e_engine.spellcasting import resolve_target_count

        activity_spec = spec
        if (
            activity.kind == "damage"
            and spec.selected_target_ids is None
            and spec.primary_target_id
        ):
            count = resolve_target_count(
                activity.target.affects.count,
                cast_level=current.slot_level if current.slot_level is not None else spell.level,
            )
            if count is not None:
                activity_spec = spec.model_copy(
                    update={"selected_target_ids": (spec.primary_target_id,) * count}
                )
        plan = plan_activity_delivery(
            live,
            current.caster,
            [activity],
            activity_spec,
            range_spec=spell.range,
            execution=True,
            cast_level=current.slot_level,
            base_level=spell.level,
        )
        return prepare_activity_delivery(
            live, current, activity, plan.activities[0], activity_spec, spell.slug
        )

    ctx = replace(
        ctx,
        spell_delivery=spec,
        spell_book={**ctx.spell_book, **orch._build_cast_spell_book(spell.activities)},
        lifecycle_source_kind="spell",
        lifecycle_source_slug=spell.slug,
        effect_application_id=f"application-{len(live.event_log)}",
        spell_dispatch=lambda child, child_ctx: execute_spell_delivery(live, child, child_ctx),
    )
    ctx = resolve_spell_activities(live, spell, ctx, prepare_activity=prepare)
    fold_forced_movement_requests(live, ctx)
    return ctx


def fold_forced_movement_requests(live: _LiveCombat, ctx: ActivityResolutionContext) -> None:
    from dnd5e_engine import orchestrator as orch

    requests = tuple(ctx.forced_movement_requests)
    ctx.forced_movement_requests.clear()
    for request in requests:
        target = orch._find_combatant(live, request.target_id)
        origin = live.actor_zone.get(request.source_actor_id)
        if (
            target is None
            or not target.is_alive
            or target.entity_id in live.dead_ids
            or origin is None
        ):
            continue
        if request.direction == "away_from_source":
            orch.push_combatant(live, request.target_id, origin, request.distance_ft)
        else:
            from dnd5e_engine.live_movement import push

            push(live, request.target_id, origin, request.distance_ft, toward=True)
