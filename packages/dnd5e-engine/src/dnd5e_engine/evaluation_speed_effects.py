"""Admit and compute a canonical spell-hit speed effect in fixed combat input."""

import random
from collections.abc import Sequence
from typing import TYPE_CHECKING

from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.common import AppliedEffectRef, AttackActivity, PassiveEffect
from dnd5e_srd_data.schema.lifecycle import EffectLifecycleSpec
from dnd5e_srd_data.schema.spell import Spell

from dnd5e_engine.activities.effects import bind_effect_lifecycle, passive_effect_to_active_effect
from dnd5e_engine.evaluation_computation import CombatComputation
from dnd5e_engine.evaluation_delta import CombatEffectCreate, EffectLifecycleUpdate, StateDelta
from dnd5e_engine.evaluation_effects import EffectState, effect_state
from dnd5e_engine.evaluation_haste_delta import application_clock
from dnd5e_engine.evaluation_projection import EvaluationInvariantError, attack_delta
from dnd5e_engine.evaluation_ruleset import get_default_ruleset
from dnd5e_engine.evaluation_state import CharacterStateV2, CombatSnapshot, ConditionLink
from dnd5e_engine.events import (
    AttackRolled,
    CombatEvent,
    DamageApplied,
    EffectApplied,
    EffectExpired,
    TurnEnded,
)
from dnd5e_engine.movement import MovementMode, SpeedModifier, project_speeds, speed_for_mode
from dnd5e_engine.rules.conditions import active_condition_names, exhaustion_level_of
from dnd5e_engine.types.combat import Combatant

if TYPE_CHECKING:
    from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest


_LIFECYCLE = EffectLifecycleSpec(expiry_boundary="source_next_turn_start")


def attack_speed_effect_template(spell: Spell, activity: AttackActivity) -> PassiveEffect | None:
    """Select the single unconditional, flat speed reduction from an attack definition."""
    if len(activity.effects) != 1 or len(spell.passive_effects) != 1:
        return None
    (template,) = spell.passive_effects
    if (
        not template.id
        or not template.name
        or activity.effects[0] != AppliedEffectRef(id=template.id, lifecycle=_LIFECYCLE)
        or template.duration
        or template.action_policy is not None
        or template.disabled
        or template.transfer
        or template.statuses
        or len(template.changes) != 1
    ):
        return None
    (change,) = template.changes
    if (
        change.key != "speed.reduction"
        or change.mode != 2
        or not change.value.isascii()
        or not change.value.isdecimal()
        or len(change.value) > 3
        or not int(change.value)
    ):
        return None
    return template


def spell_attack_has_speed_effect(loader: AssetLoader, spell_id: str) -> bool:
    """Identify a bound canonical spell attack with the reviewed hit rider."""
    return _spell_speed_definition(loader, spell_id) is not None


def _spell_speed_definition(
    loader: AssetLoader, spell_id: str
) -> tuple[Spell, AttackActivity, PassiveEffect] | None:
    """Resolve one canonical spell and its exact supported effect declaration."""
    from dnd5e_engine.evaluation_spells import _ordinary_spell_profile

    spell = get_default_ruleset().get_spell(spell_id)
    if spell is None or spell != loader.get_spell(spell_id):
        return None
    if _ordinary_spell_profile(spell) != "attack_damage":
        return None
    (activity,) = spell.activities
    assert isinstance(activity, AttackActivity)
    template = attack_speed_effect_template(spell, activity)
    return (spell, activity, template) if template is not None else None


def _bound_effect(
    definition: tuple[Spell, AttackActivity, PassiveEffect], source_id: str, target_id: str
) -> EffectState:
    """Project the canonical activity effect as emitted by the shared hit resolver."""
    spell, activity, template = definition
    return effect_state(
        bind_effect_lifecycle(
            passive_effect_to_active_effect(template, target_id=target_id, caster_id=source_id),
            _LIFECYCLE,
            source_id=source_id,
            source_kind="spell",
            source_slug=spell.slug,
            activity_id=activity.id,
            is_magical=True,
        )
    )


def has_speed_effects(snapshot: CombatSnapshot) -> bool:
    """Route an input carrying the flat speed lifecycle to its admission checks."""
    return any(
        effect.lifecycle is not None
        and effect.lifecycle.source_kind == "spell"
        and any(change.key == "speed.reduction" for change in effect.changes)
        for effect in snapshot.effect_states
    )


def is_bound_attack_speed_effect(effect: EffectState, loader: AssetLoader) -> bool:
    """Check an effect against its exact canonical spell rider and application identity."""
    app = effect.lifecycle
    definition = _spell_speed_definition(loader, app.source_slug) if app is not None else None
    return (
        app is not None
        and definition is not None
        and effect == _bound_effect(definition, app.source_id, effect.target_id)
    )


def world_walk_bonus(effect: EffectState) -> int:
    """Read an admitted elapsed passive effect's flat walking-speed change."""
    from dnd5e_engine.evaluation_timed_spells import attack_world_spell_walk_bonus

    bonus = attack_world_spell_walk_bonus(effect)
    if not any(change.key == "system.attributes.movement.walk" for change in effect.changes):
        raise ValueError("timed walking requires a flat walking-speed modifier")
    return bonus


def _speed_world_effect_failure(
    effects: Sequence[EffectState], target_id: str, mode: MovementMode
) -> str | None:
    """Admit an optional single elapsed walk modifier on the attack rider target."""
    if not effects:
        return None
    if len(effects) != 1 or effects[0].target_id != target_id:
        return "speed rider admits one elapsed walk modifier on its target"
    if mode != "walk":
        return "elapsed walk modifier and speed rider require a walking ledger"
    try:
        world_walk_bonus(effects[0])
    except ValueError as error:
        return str(error)
    return None


def speed_cast_support_failure(snapshot: CombatSnapshot, target_id: str) -> str | None:
    """Admit a fresh speed rider with optional same-target elapsed walking support."""
    state = snapshot.combat_state
    if (
        state.effect_lifecycles
        or state.conditions_by_effect
        or state.concentration_chain
        or state.concentration_rounds_remaining
    ):
        return "initial speed rider requires an empty turn effect graph"
    if not snapshot.effect_states:
        return None
    ledger = state.movement_ledgers.get(target_id)
    if ledger is None:
        return "elapsed walk modifier requires a target movement ledger"
    return _speed_world_effect_failure(snapshot.effect_states, target_id, ledger.active_mode)


def speed_with_effects(
    actor: CharacterStateV2 | Combatant,
    effects: Sequence[EffectState],
    mode: MovementMode = "walk",
) -> int:
    """Project speed with an admitted elapsed walk modifier and attack reduction."""
    bonus = sum(
        world_walk_bonus(effect)
        for effect in effects
        if mode == "walk" and effect.target_id == actor.entity_id and effect.lifecycle is None
    )
    return _speed_with_reductions(actor, effects, mode, bonus)


def _speed_with_reductions(
    actor: CharacterStateV2 | Combatant,
    effects: Sequence[EffectState],
    mode: MovementMode,
    walk_bonus: int,
) -> int:
    """Apply reductions to the current speed with any already-interpreted walk bonus."""
    modifiers = [
        SpeedModifier("reduce", int(change.value))
        for effect in effects
        if effect.target_id == actor.entity_id
        if not effect.disabled
        for change in effect.changes
        if change.key == "speed.reduction"
        and change.mode == "add"
        and isinstance(change.value, (str, int))
    ]
    return speed_for_mode(
        project_speeds(
            actor.base_speed + (walk_bonus if mode == "walk" else 0),
            actor.movement_modes,
            condition_names=active_condition_names(actor.conditions),
            exhaustion_level=exhaustion_level_of(actor.conditions),
            modifiers=modifiers,
        ),
        mode,
    )


def speed_effect_support_failure(snapshot: CombatSnapshot, loader: AssetLoader) -> str | None:
    """Check the one combat speed rider, any elapsed walk modifier, and its target budget."""
    state = snapshot.combat_state
    if (
        snapshot.snapshot_schema_version != "engine-snapshot/20"
        or len(state.effect_lifecycles) != 1
        or len(state.conditions_by_effect) != 1
        or state.concentration_chain
        or state.concentration_rounds_remaining
        or state.pending_reactions
        or state.active_reaction_responses
        or state.reaction_effects_pending_expiry
    ):
        return "one reviewed speed effect requires a plain /20 combat graph"
    combat_effects = tuple(effect for effect in snapshot.effect_states if effect.lifecycle)
    if len(combat_effects) != 1:
        return "speed effect requires exactly one attack rider"
    (effect,) = combat_effects
    app = effect.lifecycle
    if app is None or not is_bound_attack_speed_effect(effect, loader):
        return "speed effect differs from the bound canonical attack rider"
    if not (
        (app.source_id in state.party_ids and effect.target_id in state.encounter_ids)
        or (app.source_id in state.encounter_ids and effect.target_id in state.party_ids)
    ):
        return "speed effect requires present opposing source and target"
    if app.source_id in state.dead_ids:
        return "speed effect source has no future initiative turn"
    identity = (effect.target_id, effect.id, effect.origin)
    (record,) = state.effect_lifecycles
    (lineage,) = state.conditions_by_effect
    clock = record.state
    serial, round_number = clock.applied_turn_serial, clock.applied_round
    if (
        not 0 <= serial <= state.turn_serial
        or not 1 <= round_number <= state.round_number
        or record != application_clock(effect, serial, round_number)
        or lineage != ConditionLink(identity=identity, statuses=[])
    ):
        return "speed effect has a contradictory application clock or lineage"
    source_index = state.initiative_ids.index(app.source_id)
    current_index = state.current_turn_index
    if (
        (serial == state.turn_serial)
        != (round_number == state.round_number and current_index == source_index)
        or (round_number == state.round_number and current_index < source_index)
        or (round_number == state.round_number - 1 and current_index >= source_index)
        or round_number < state.round_number - 1
    ):
        return "speed effect is outside its source-turn application interval"
    initiative = state.initiative_ids
    traversed = (current_index - source_index) % len(initiative)
    # Every still-live slot on this arc must have started a turn. A currently
    # dead actor may have acted before dying, so that slot only raises the upper bound.
    minimum_starts = sum(
        initiative[(source_index + offset) % len(initiative)] not in state.dead_ids
        for offset in range(1, traversed + 1)
    )
    if not minimum_starts <= state.turn_serial - serial <= traversed:
        return "speed effect serial contradicts the reachable initiative slots"
    target = next(a for a in snapshot.character_states if a.entity_id == effect.target_id)
    ledger = state.movement_ledgers[effect.target_id]
    if failure := _speed_world_effect_failure(
        tuple(other for other in snapshot.effect_states if other.lifecycle is None),
        effect.target_id,
        ledger.active_mode,
    ):
        return failure
    if target.movement_remaining != ledger.remaining(
        speed_with_effects(target, snapshot.effect_states, ledger.active_mode)
    ):
        return "speed effect disagrees with the target movement budget"
    return None


def without_speed_effect(snapshot: CombatSnapshot) -> CombatSnapshot:
    """Expose ordinary dependencies after independently admitting the speed modifiers."""
    return snapshot.model_copy(
        update={
            "effect_states": (),
            "combat_state": snapshot.combat_state.model_copy(
                update={"effect_lifecycles": (), "conditions_by_effect": ()}
            ),
        }
    )


class SpeedEffectComputation(CombatComputation):
    """Retain elapsed speed modifiers and fold one turn-bound hit rider."""

    def __init__(self, snapshot: CombatSnapshot, rng: random.Random) -> None:
        """Keep the active effect local to this evaluation."""
        super().__init__(snapshot, rng)
        self.effects = snapshot.effect_states
        self.walk_bonuses = {
            effect.target_id: world_walk_bonus(effect)
            for effect in self.effects
            if effect.lifecycle is None
        }

    def emit(self, event: CombatEvent) -> None:
        """Capture the resolver's hit effect with the applying turn's clock."""
        super().emit(event)
        if isinstance(event, EffectApplied):
            if self.state.effect_lifecycles:
                raise EvaluationInvariantError("speed computation cannot create a second effect")
            effect = effect_state(event.effect)
            record = application_clock(effect, self.state.turn_serial, self.state.round_number)
            self.effects = (*self.effects, effect)
            self.state = self.state.model_copy(
                update={
                    "effect_lifecycles": (record,),
                    "conditions_by_effect": (ConditionLink(identity=record.identity, statuses=[]),),
                }
            )
            self.refresh_movement_remaining(effect.target_id)

    def speed(self, actor: Combatant, mode: MovementMode = "walk") -> int:
        """Project speed against the currently active candidate effects."""
        return _speed_with_reductions(
            actor, self.effects, mode, self.walk_bonuses.get(actor.entity_id, 0)
        )

    def on_turn_start(self, actor_id: str) -> EffectExpired | None:
        """Expire the effect when due and return its event for the start phase."""
        if not self.state.effect_lifecycles:
            return None
        (record,) = self.state.effect_lifecycles
        if not record.state.expires_at(
            actor_id, "start", self.state.turn_serial, self.state.round_number
        ):
            return None
        target_id, effect_id, origin = record.identity
        self.effects = tuple(
            effect
            for effect in self.effects
            if (effect.target_id, effect.id, effect.origin) != record.identity
        )
        self.state = self.state.model_copy(
            update={"effect_lifecycles": (), "conditions_by_effect": ()}
        )
        self.refresh_movement_remaining(target_id)
        return EffectExpired(
            target_id=target_id, effect_id=effect_id, origin=origin, reason="duration"
        )

    def candidate_state(self) -> CombatSnapshot:
        """Include the working effect graph in the candidate used by Delta projection."""
        return super().candidate_state().model_copy(update={"effect_states": self.effects})


def speed_effect_delta(before: CombatSnapshot, after: CombatSnapshot) -> StateDelta:
    """Project normal combat changes and the selected guarded creation or removal."""
    mechanical = attack_delta(without_speed_effect(before), without_speed_effect(after))
    if tuple(e for e in before.effect_states if e.lifecycle is None) != tuple(
        e for e in after.effect_states if e.lifecycle is None
    ):
        raise EvaluationInvariantError("combat speed transition changed an elapsed world effect")
    old = (
        tuple(e for e in before.effect_states if e.lifecycle is not None),
        before.combat_state.effect_lifecycles,
        before.combat_state.conditions_by_effect,
    )
    new = (
        tuple(e for e in after.effect_states if e.lifecycle is not None),
        after.combat_state.effect_lifecycles,
        after.combat_state.conditions_by_effect,
    )
    if old == new:
        return mechanical
    operation: CombatEffectCreate | EffectLifecycleUpdate
    if old == ((), (), ()) and len(new[0]) == len(new[1]) == len(new[2]) == 1:
        operation = CombatEffectCreate(
            kind="combat.effect_create",
            combat_id=before.combat_state.combat_id,
            identity=new[1][0].identity,
            expected_absent=True,
            effect=new[0][0],
            lifecycle=new[1][0],
            lineage=new[2][0],
        )
    elif new == ((), (), ()) and len(old[0]) == len(old[1]) == len(old[2]) == 1:
        operation = EffectLifecycleUpdate(
            kind="combat.effect_lifecycle_update",
            combat_id=before.combat_state.combat_id,
            identity=old[1][0].identity,
            expected_effect=old[0][0],
            expected_lifecycle=old[1][0],
            expected_lineage=old[2][0],
            effect=None,
            lifecycle=None,
            lineage=None,
        )
    else:
        raise EvaluationInvariantError("unrepresented speed effect transition")
    return mechanical.model_copy(update={"operations": (*mechanical.operations, operation)})


def verify_speed_effect_creation(
    operation: CombatEffectCreate,
    request: "RuleEvaluationRequest",
    proposed_events: Sequence[CombatEvent],
) -> None:
    """Bind a creation to its hit, damage and effect evidence and the applying turn."""
    from dnd5e_engine.evaluation_contracts import CombatIntentPayload

    snapshot, intent = request.state_snapshot, request.payload
    if (
        request.schema_version != "engine-evaluation/30"
        or not isinstance(snapshot, CombatSnapshot)
        or not isinstance(intent, CombatIntentPayload)
        or intent.intent_type != "cast_spell"
        or not intent.spell_id
        or not intent.target_id
        or speed_cast_support_failure(snapshot, intent.target_id) is not None
    ):
        raise ValueError("combat effect creation requires a current initial spell attack")
    canonical = get_default_ruleset()
    definition = _spell_speed_definition(canonical, intent.spell_id)
    state = snapshot.combat_state
    if (
        definition is None
        or operation.combat_id != state.combat_id
        or request.actor_id != state.initiative_ids[state.current_turn_index]
        or operation.expected_absent is not True
        or operation.identity != (intent.target_id, operation.effect.id, operation.effect.origin)
        or operation.lineage != ConditionLink(identity=operation.identity, statuses=[])
        or operation.effect != _bound_effect(definition, request.actor_id, intent.target_id)
        or operation.lifecycle
        != application_clock(operation.effect, state.turn_serial, state.round_number)
    ):
        raise ValueError("combat effect creation differs from the requested rider, combat or clock")
    rolls = [
        (i, event) for i, event in enumerate(proposed_events) if isinstance(event, AttackRolled)
    ]
    damages = [
        (i, event) for i, event in enumerate(proposed_events) if isinstance(event, DamageApplied)
    ]
    effects = [
        (i, event) for i, event in enumerate(proposed_events) if isinstance(event, EffectApplied)
    ]
    ended = [i for i, event in enumerate(proposed_events) if isinstance(event, TurnEnded)]
    spell, activity, _ = definition
    if (
        len(rolls) != 1
        or len(damages) != 1
        or len(effects) != 1
        or not rolls[0][0]
        < damages[0][0]
        < effects[0][0]
        < min(ended, default=len(proposed_events))
        or not rolls[0][1].is_hit
        or rolls[0][1].is_opportunity_attack
        or rolls[0][1].attacker_id != request.actor_id
        or rolls[0][1].target_id != intent.target_id
        or damages[0][1].source_actor_id != request.actor_id
        or damages[0][1].target_id != intent.target_id
        or damages[0][1].source_id != f"spell:{spell.slug}:{activity.id}"
        or damages[0][1].damage_type != activity.damage.parts[0].types[0]
        or damages[0][1].amount < 0
        or effect_state(effects[0][1].effect) != operation.effect
    ):
        raise ValueError(
            "combat effect creation requires its matching attack hit, damage and effect"
        )
