"""Persistent area geometry, trigger gates and lifetime, separate from dispatch.

Ordering is area creation sequence, then initiative order. Each completed step
first checks creature entry, then new coverage by source-following Emanations.
Placement establishes coverage without synthesizing a movement entry. Source
magnitudes are captured; target defenses are hydrated at each execution.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal

from dnd5e_srd_data.schema.common import Activity, AreaTrigger, PassiveEffect, PersistentAreaSpec

from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.resolver import resolve_activity
from dnd5e_engine.activities.save import _resolve_dc
from dnd5e_engine.areas import AreaTemplate, area_cells, area_template
from dnd5e_engine.events import (
    AreaCreated,
    AreaExpired,
    CombatantLeft,
    CombatEvent,
    ConcentrationDropped,
    DashTaken,
    Death,
    EffectApplied,
    EffectExpired,
)
from dnd5e_engine.types.combat import Combatant
from dnd5e_engine.types.effects import ActiveEffectDuration

if TYPE_CHECKING:
    from dnd5e_srd_data.schema.spell import Spell

    from dnd5e_engine.activities.context import ActivityResolutionContext
    from dnd5e_engine.orchestrator import PlayerIntent, _LiveCombat
    from dnd5e_engine.spatial import GridTopology

EffectIdentity = tuple[str, str, str]


@dataclass(frozen=True)
class StationaryArea:
    origin: str


@dataclass(frozen=True)
class FollowSourceEmanation:
    source_entity_id: str


@dataclass
class PersistentArea:
    id: str
    sequence: int
    source_id: str
    source_kind: Literal["spell", "item"]
    caster: Combatant
    activity: Activity
    spec: PersistentAreaSpec
    geometry: StationaryArea | FollowSourceEmanation
    template: AreaTemplate
    cast_origin: str
    excluded_ids: tuple[str, ...]
    slot_level: int | None
    base_spell_level: int | None
    spellcasting_ability: str | None
    save_dc: int | None
    passive_effects: tuple[PassiveEffect, ...]
    duration: ActiveEffectDuration
    rounds_remaining: int | None
    concentration_identity: EffectIdentity | None
    not_before_turn: int
    last_trigger_turn: dict[str, int] = field(default_factory=dict)

    @property
    def source_entity_id(self) -> str:
        return self.caster.entity_id

    def origin(self, positions: Mapping[str, str]) -> str | None:
        if isinstance(self.geometry, StationaryArea):
            return self.geometry.origin
        return positions.get(self.geometry.source_entity_id)

    def cells(self, topology: GridTopology, positions: Mapping[str, str]) -> frozenset[str]:
        origin = self.origin(positions)
        return (
            area_cells(topology, self.template, origin, None) if origin is not None else frozenset()
        )

    def contains(self, live: _LiveCombat, entity_id: str) -> bool:
        return entity_id not in self.excluded_ids and live.actor_zone.get(entity_id) in self.cells(
            live.topology, live.actor_zone
        )


@dataclass
class PersistentAreaState:
    areas: list[PersistentArea] = field(default_factory=list)
    next_sequence: int = 0
    # Area riders ending at a target's next turn start live independently of
    # their producer: leaving/dying inside a stationary hazard cannot delete it.
    next_turn_start_effects: dict[EffectIdentity, int] = field(default_factory=dict)
    speed_penalties: dict[str, int] = field(default_factory=dict)
    dash_grants: dict[str, int] = field(default_factory=dict)

    def concentration_ended(self, live: _LiveCombat, caster_id: str, *, duration: bool) -> None:
        for area in tuple(self.areas):
            if area.concentration_identity is not None and area.source_entity_id == caster_id:
                self.expire(live, area, "duration" if duration else "concentration_drop")

    def expire(
        self,
        live: _LiveCombat,
        area: PersistentArea,
        reason: Literal["duration", "concentration_drop", "source_removed"],
    ) -> None:
        from dnd5e_engine import orchestrator as orch

        if area not in self.areas:
            return
        self.areas.remove(area)
        _clamp_covered(live)
        orch._emit(
            live,
            AreaExpired(
                area_id=area.id,
                actor_id=area.source_entity_id,
                source_id=area.source_id,
                reason=reason,
            ),
        )

    def observe(self, live: _LiveCombat, event: CombatEvent) -> None:
        if isinstance(event, DashTaken):
            previous_grants = self.dash_grants.get(event.actor_id, 0)
            self.dash_grants[event.actor_id] = previous_grants + 1
            # Dash has already added the current effective Speed. Record the
            # extra allowance without charging that same reduction twice.
            penalty = self.speed_penalties.get(event.actor_id, 0)
            self.speed_penalties[event.actor_id] = (
                penalty * (previous_grants + 2) // (previous_grants + 1)
            )
        for area in tuple(self.areas):
            if isinstance(event, EffectExpired) and area.concentration_identity == (
                event.target_id,
                event.effect_id,
                event.origin,
            ):
                self.expire(
                    live, area, "duration" if event.reason == "duration" else "concentration_drop"
                )
            elif (
                isinstance(event, ConcentrationDropped)
                and area.concentration_identity
                and (event.target_id == area.source_entity_id)
            ):
                self.expire(live, area, "concentration_drop")
            elif isinstance(event, (Death, CombatantLeft)) and area.source_entity_id == (
                event.target_id if isinstance(event, Death) else event.entity_id
            ):
                self.expire(live, area, "source_removed")
        if isinstance(event, EffectExpired):
            self.next_turn_start_effects.pop((event.target_id, event.effect_id, event.origin), None)
        elif isinstance(event, (Death, CombatantLeft)):
            entity_id = event.target_id if isinstance(event, Death) else event.entity_id
            for identity in tuple(self.next_turn_start_effects):
                if identity[0] == entity_id:
                    del self.next_turn_start_effects[identity]
            self.speed_penalties.pop(entity_id, None)
            self.dash_grants.pop(entity_id, None)


def register_area(
    live: _LiveCombat,
    activity: Activity,
    ctx: ActivityResolutionContext,
    *,
    source_id: str,
    spell: Spell | None = None,
    intent: PlayerIntent | None = None,
    origin: str | None = None,
) -> None:
    from dnd5e_engine import orchestrator as orch

    spec = activity.persistent_area
    template = area_template(activity)
    if spec is None or template is None or template.directional:
        return
    if origin is None:
        origin = (
            orch._area_origin(live, ctx.caster.entity_id, intent, template)
            if intent
            else (live.actor_zone.get(ctx.caster.entity_id))
        )
    if origin is None:
        return
    excluded = intent.excluded_target_ids if intent else None
    if excluded is None:
        # Same default as existing harmful choice AoE, persisted at cast time
        # for every combatant, including those currently outside the area.
        excluded = (
            tuple(
                c.entity_id
                for c in live.initiative
                if not orch._is_enemy(live, ctx.caster.entity_id, c.entity_id)
            )
            if activity.target.affects.choice
            else ()
        )
    rounds = _duration_rounds(spell)
    state = live.persistent_areas
    area = PersistentArea(
        id=f"area:{state.next_sequence}",
        sequence=state.next_sequence,
        source_id=source_id,
        source_kind="spell" if spell else "item",
        caster=ctx.caster,
        activity=activity,
        spec=spec,
        geometry=FollowSourceEmanation(ctx.caster.entity_id)
        if spec.placement == "follow-source"
        else StationaryArea(origin),
        template=template,
        cast_origin=origin,
        excluded_ids=excluded,
        slot_level=ctx.slot_level,
        base_spell_level=ctx.base_spell_level,
        spellcasting_ability=ctx.spellcasting_ability,
        save_dc=_resolve_dc(
            activity,
            replace(ctx, save_dc_override=None)
            if activity.save.dc.calculation in ("", "flat")
            else ctx,
        )
        if activity.kind == "save"
        else None,
        passive_effects=tuple(ctx.source_passive_effects),
        duration=ActiveEffectDuration(rounds=rounds),
        rounds_remaining=rounds,
        concentration_identity=orch._anchor_identity(spell.slug, ctx.caster.entity_id)
        if spell and spell.concentration
        else None,
        not_before_turn=live.turn_serial
        + int(activity.timing.next_turn and activity.timing.trigger != "immediate"),
    )
    state.next_sequence += 1
    state.areas.append(area)
    orch._emit(
        live,
        AreaCreated(
            area_id=area.id,
            actor_id=area.source_entity_id,
            source_id=source_id,
            source_kind=area.source_kind,
            activity_id=activity.id,
            placement=spec.placement,
            shape=template.shape,
            grid_shape=template.grid_shape,
            size_ft=template.size_ft,
            origin=origin,
            excluded_ids=excluded,
            duration_rounds=rounds,
            concentration=area.concentration_identity is not None,
            slot_level=area.slot_level,
            save_dc=area.save_dc,
            triggers=spec.triggers,
        ),
    )
    _clamp_covered(live)


def _duration_rounds(spell: Spell | None) -> int | None:
    if spell is None or spell.duration.value is None:
        return None
    factor = {"round": 1, "minute": 10, "hour": 600, "day": 14400}.get(spell.duration.units)
    return factor * spell.duration.value if factor is not None else None


def area_speed(live: _LiveCombat, entity_id: str, speed: int) -> int:
    if any(identity[0] == entity_id for identity in live.persistent_areas.next_turn_start_effects):
        return 0
    # Identical overlapping speed effects do not multiply repeatedly.
    multipliers = [
        a.spec.speed_multiplier
        for a in live.persistent_areas.areas
        if a.spec.speed_multiplier is not None and a.contains(live, entity_id)
    ]
    return int(speed * min(multipliers)) if multipliers else speed


def _clamp_covered(live: _LiveCombat) -> None:
    from dnd5e_engine import orchestrator as orch

    for creature in tuple(live.initiative):
        # A changing Speed changes the remaining distance, without refunding
        # distance already walked or destroying a previously paid Dash budget.
        unmodified = orch._effective_speed(creature)
        if live.slow_marks.get(creature.entity_id):
            unmodified = max(0, unmodified - 10)
        penalty = unmodified - area_speed(live, creature.entity_id, unmodified)
        penalty *= 1 + live.persistent_areas.dash_grants.get(creature.entity_id, 0)
        previous = live.persistent_areas.speed_penalties.get(creature.entity_id, 0)
        if penalty != previous:
            orch._update_combatant(
                live,
                creature.entity_id,
                movement_remaining=max(0, creature.movement_remaining + previous - penalty),
            )
        live.persistent_areas.speed_penalties[creature.entity_id] = penalty


def after_movement_step(live: _LiveCombat, mover_id: str, from_cell: str) -> None:
    """Called after position/budget writeback, for voluntary AND forced steps."""
    old_positions = dict(live.actor_zone)
    old_positions[mover_id] = from_cell
    for area in tuple(live.persistent_areas.areas):
        old_cells = area.cells(live.topology, old_positions)
        new_cells = area.cells(live.topology, live.actor_zone)
        if from_cell not in old_cells and live.actor_zone.get(mover_id) in new_cells:
            _trigger(live, area, mover_id, "enter")
        if isinstance(area.geometry, FollowSourceEmanation) and area.source_entity_id == mover_id:
            for creature in tuple(live.initiative):
                if live.actor_zone.get(creature.entity_id) in new_cells - old_cells:
                    _trigger(live, area, creature.entity_id, "area-enters-creature")
    _clamp_covered(live)


def _trigger(live: _LiveCombat, area: PersistentArea, target_id: str, trigger: AreaTrigger) -> None:
    from dnd5e_engine import orchestrator as orch

    target = orch._find_combatant(live, target_id)
    if (
        area not in live.persistent_areas.areas
        or trigger not in area.spec.triggers
        or not area.contains(live, target_id)
        or target is None
        or not target.is_alive
        or target_id in live.dead_ids
        or area.not_before_turn > live.turn_serial
        or (area.spec.once_per_turn and area.last_trigger_turn.get(target_id) == live.turn_serial)
    ):
        return
    area.last_trigger_turn[target_id] = live.turn_serial
    _execute(live, area, target)


def _execute(live: _LiveCombat, area: PersistentArea, target: Combatant) -> None:
    from dnd5e_engine import orchestrator as orch
    from dnd5e_engine.live_reactions import attach_reaction_hooks

    caster = orch._find_combatant(live, area.source_entity_id) or area.caster
    payload = orch._build_hydration_payload(live, caster=caster)
    geometry = orch._monster_context_kwargs(live, caster, [target], payload)
    geometry["target_cover"] = orch._target_cover_map(
        live, caster.entity_id, [target], origin_cell=area.origin(live.actor_zone)
    )

    def emit(event: CombatEvent) -> None:
        if isinstance(event, EffectApplied):
            effect = event.effect.model_copy(
                update={"origin": f"area:{area.id}:{area.source_entity_id}"}
            )
            if area.spec.effects_until_target_turn_start:
                identity = (effect.target_id, effect.id, effect.origin)
                if identity in live.persistent_areas.next_turn_start_effects:
                    return  # Same target-next-start rider is already attached.
                effect = effect.model_copy(update={"duration": ActiveEffectDuration()})
            event = EffectApplied(effect=effect)
        orch._emit(live, event)

    ctx = build_activity_context(
        area.caster,
        [target],
        rng=live.rng,
        event_emitter=emit,
        slot_level=area.slot_level
        if area.activity.timing.scale_with_slot
        else area.base_spell_level,
        base_spell_level=area.base_spell_level,
        spellcasting_ability=area.spellcasting_ability,
        save_dc_override=area.save_dc,
        concentration=False,
        source_passive_effects=list(area.passive_effects),
        spell_book=orch._build_cast_spell_book([area.activity]),
        **geometry,
    )
    before = len(live.event_log)
    ctx = attach_reaction_hooks(live, ctx)
    resolve_activity(area.activity, ctx)
    orch._sync_legendary_resistance(live, before)
    if area.spec.effects_until_target_turn_start:
        for event in live.event_log[before:]:
            if isinstance(event, EffectApplied):
                effect = event.effect
                identity = (effect.target_id, effect.id, effect.origin)
                live.persistent_areas.next_turn_start_effects[identity] = live.turn_serial
    _clamp_covered(live)


def before_turn_start(live: _LiveCombat, actor_id: str) -> None:
    """Expire next-start riders before TurnStarted refreshes the movement budget."""
    from dnd5e_engine import orchestrator as orch

    live.persistent_areas.dash_grants.pop(actor_id, None)
    for identity in tuple(live.persistent_areas.next_turn_start_effects):
        if identity[0] == actor_id:
            target_id, effect_id, origin = identity
            orch._emit(
                live,
                EffectExpired(
                    target_id=target_id, effect_id=effect_id, origin=origin, reason="duration"
                ),
            )
    _clamp_covered(live)


def run_area_boundary(live: _LiveCombat, actor_id: str | None, trigger: AreaTrigger) -> None:
    if actor_id is None:
        return
    for area in tuple(live.persistent_areas.areas):
        _trigger(live, area, actor_id, trigger)
    if trigger == "turn-end-inside":
        for area in tuple(live.persistent_areas.areas):
            if area.source_entity_id != actor_id or area.concentration_identity is not None:
                continue
            if area.rounds_remaining is not None:
                area.rounds_remaining -= 1
                if area.rounds_remaining <= 0:
                    live.persistent_areas.expire(live, area, "duration")


def register_area_hooks(live: _LiveCombat) -> None:
    live.lifecycle.register(
        "turn_start",
        lambda combat, actor: run_area_boundary(combat, actor, "turn-start-inside"),
        key="engine:persistent-area-start",
    )
    live.lifecycle.register(
        "turn_end",
        lambda combat, actor: run_area_boundary(combat, actor, "turn-end-inside"),
        key="engine:persistent-area-end",
    )


def register_item_areas(
    live: _LiveCombat,
    activities: Sequence[Activity],
    ctx: ActivityResolutionContext,
    intent: PlayerIntent,
) -> None:
    for activity in activities:
        if activity.persistent_area is not None:
            register_area(live, activity, ctx, source_id=intent.item_id or "", intent=intent)


__all__ = ["FollowSourceEmanation", "PersistentArea", "PersistentAreaState", "StationaryArea"]
