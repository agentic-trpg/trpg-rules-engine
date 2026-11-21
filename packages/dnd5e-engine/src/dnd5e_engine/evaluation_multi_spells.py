"""Evaluate bounded exploration utilities with independent creature willingness."""

from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.common import UtilityActivity
from dnd5e_srd_data.schema.spell import Spell, SpellRangeUnits

from dnd5e_engine.areas import has_line_of_effect
from dnd5e_engine.evaluation_contracts import (
    MAX_EXPLORATION_SPELL_TARGETS,
    ExplorationMultiSpellCastPayload,
    ExplorationMultiSpellPositions,
    MultiSpellWillingnessChoice,
    RuleEvaluationRequest,
    RuleEvaluationResult,
)
from dnd5e_engine.evaluation_delta import StateDelta, WorldEffectCreate
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_payments import spell_payment
from dnd5e_engine.evaluation_projection import EvaluationInvariantError
from dnd5e_engine.evaluation_rng import RNGTransition
from dnd5e_engine.evaluation_spells import spell_event
from dnd5e_engine.evaluation_state import CharacterStateV2, NonCombatSnapshot
from dnd5e_engine.evaluation_timed_spells import _passive_profile, exploration_source_failure
from dnd5e_engine.evaluation_turn import TurnEvaluation, _refuse
from dnd5e_engine.evaluation_world_effect import (
    EffectExpiryPolicy,
    WorldEffectApplication,
    WorldEffectApplicationRequest,
)
from dnd5e_engine.events import EffectApplied, IntentSubmitted, SpellCast
from dnd5e_engine.rules.conditions import conditions_block_actions
from dnd5e_engine.spatial import GridTopology, canonical_cell_id
from dnd5e_engine.specs import GridScene
from dnd5e_engine.spellcasting import spell_range_ft
from dnd5e_engine.types.effects import ActiveEffect


def _multi_spell_profile(
    spell: Spell, payload: ExplorationMultiSpellCastPayload, caster_id: str
) -> tuple[UtilityActivity, ActiveEffect, int]:
    """Select the shared passive profile and its bound creature capacity."""
    if spell.concentration or spell.range.units != SpellRangeUnits.TOUCH:
        raise ValueError("multi spell requires a nonconcentration touch utility")
    activity, template, capacity = _passive_profile(
        spell,
        target_id=payload.target_ids[0],
        caster_id=caster_id,
        max_target_capacity=MAX_EXPLORATION_SPELL_TARGETS,
    )
    if activity.target.affects.type != "willing":
        raise ValueError("utility must require willing creatures")
    return activity, template, capacity


def _touch_target_failure(
    snapshot: NonCombatSnapshot,
    positions: ExplorationMultiSpellPositions,
    actors: dict[str, CharacterStateV2],
    reach: int,
    effect_id: str,
) -> tuple[str, str] | None:
    """Check each selected living creature against one fixed grid and current effects."""
    topology = GridTopology(GridScene.model_validate(snapshot.scene_state.grid.model_dump()))
    try:
        source_cell = canonical_cell_id(positions.source_cell)
        target_cells = tuple(canonical_cell_id(target.cell_id) for target in positions.targets)
    except ValueError:
        return "target_invalid", "spell places must identify grid cells"
    if not topology.is_valid_cell(source_cell):
        return "target_invalid", "caster must occupy a valid grid cell"
    existing_targets = {
        effect.target_id for effect in snapshot.effect_states if effect.id == effect_id
    }
    for target_facts, target_cell in zip(positions.targets, target_cells, strict=True):
        target = actors.get(target_facts.target_id)
        if target is None or not target.is_alive:
            return "target_invalid", "cast requires living creatures"
        if not (
            topology.is_valid_cell(target_cell)
            and topology.within_range(source_cell, target_cell, reach)
            and has_line_of_effect(topology, source_cell, target_cell)
        ):
            return "target_invalid", "selected creature must be within touch"
        if target.entity_id in existing_targets:
            return "spell.effect_exists", "target already has this effect"
    return None


def evaluate_multi_spell(request: RuleEvaluationRequest, loader: AssetLoader) -> TurnEvaluation:
    """Propose one ordinary slot spend and a finite effect for each verified target.

    This profile admits a nonconcentration base-level touch utility within its
    bound target count. Unsupported delivery and invalid dependencies fail
    before the result can request any consent or propose payment.
    """
    snapshot, payload, positions = (
        request.state_snapshot,
        request.payload,
        request.multi_spell_positions,
    )
    if (
        not isinstance(snapshot, NonCombatSnapshot)
        or not isinstance(payload, ExplorationMultiSpellCastPayload)
        or positions is None
    ):
        raise EvaluationInvariantError("multi spell requires a fixed exploration request")
    spell = loader.get_spell(payload.spell_id)
    if spell is None or spell.slug != payload.spell_id:
        return _refuse("unsupported", "spell.asset", "selected spell is absent from the ruleset")
    if spell.level != payload.slot_level:
        return _refuse("unsupported", "spell.slot", "multi spell requires an ordinary base slot")
    reach = spell_range_ft(spell)
    if reach is None:
        return _refuse(
            "unsupported", "spell.mechanism", "multi spell requires a defined touch range"
        )
    try:
        activity, template, target_capacity = _multi_spell_profile(spell, payload, request.actor_id)
    except ValueError as exc:
        return _refuse("unsupported", "spell.mechanism", str(exc))
    if len(payload.target_ids) > target_capacity:
        return _refuse(
            "rejected", "spell.target_count", "selected too many creatures for this spell"
        )

    actors = {actor.entity_id: actor for actor in snapshot.character_states}
    caster = actors.get(request.actor_id)
    if caster is None:
        return _refuse("rejected", "actor_missing", "caster is absent from the snapshot")
    if (
        not caster.is_alive
        or caster.hp_current <= 0
        or conditions_block_actions([condition.condition for condition in caster.conditions])
    ):
        return _refuse("rejected", "actor_incapacitated", "caster must be alive and able to act")
    if any(
        effect.target_id == caster.entity_id and not effect.disabled and effect.action_policy
        for effect in snapshot.effect_states
    ):
        return _refuse("unsupported", "spell.action_policy", "caster has an active action policy")
    if failure := exploration_source_failure(
        snapshot, caster, payload.spell_source_id, spell, loader
    ):
        return _refuse("rejected", "spell.source", failure)
    resources = snapshot.resource_state
    if resources is None or caster.spell_slots.get(payload.slot_level, 0) <= 0:
        return _refuse("rejected", "spell.slot", "no owned ordinary slot remains")

    if invalid_target := _touch_target_failure(snapshot, positions, actors, reach, template.id):
        return _refuse("rejected", *invalid_target)

    unresolved = tuple(target.target_id for target in positions.targets if not target.willing)
    if unresolved:
        return dict(
            status="needs_choice",
            state_delta=None,
            proposed_events=(),
            rng_transition=None,
            choice=MultiSpellWillingnessChoice(
                kind="spell.targets_willingness", actor_id=request.actor_id, target_ids=unresolved
            ),
            error=None,
        )

    effects = tuple(
        template.model_copy(update={"target_id": target_id}) for target_id in payload.target_ids
    )
    creations = tuple(
        WorldEffectCreate(
            kind="world.effect_create",
            expected_absent=True,
            value=WorldEffectApplication(
                session_id=request.session_id,
                command_id=request.command_id,
                source_id=request.actor_id,
                effect=effect_state(effect),
                expiry=EffectExpiryPolicy(action="remove"),
                ruleset_binding=request.ruleset_binding,
            ),
        )
        for effect in effects
    )
    payment = spell_payment(resources, request.actor_id, payload.slot_level)
    return dict(
        status="accepted",
        state_delta=StateDelta(
            expected_world_version=snapshot.world_version, operations=(payment, *creations)
        ),
        proposed_events=(
            IntentSubmitted(
                actor_id=request.actor_id,
                intent_type="cast_spell",
                spell_id=payload.spell_id,
                source_id=payload.spell_source_id,
                activity_id=activity.id,
            ),
            spell_event(request.actor_id, spell, payload.slot_level),
            *(EffectApplied(effect=effect) for effect in effects),
        ),
        rng_transition=RNGTransition(
            stream_id=request.rng_context.stream_id,
            input_version=request.rng_context.version,
            input_state=request.rng_context.state,
            next_state=request.rng_context.state,
            state_changed=False,
        ),
        choice=None,
        error=None,
    )


def verify_multi_spell_cast(result: RuleEvaluationResult, request: RuleEvaluationRequest) -> None:
    """Bind a choice or paid cast to the fixed selection and per-target associations."""
    payload, snapshot, positions = (
        request.payload,
        request.state_snapshot,
        request.multi_spell_positions,
    )
    if (
        not isinstance(payload, ExplorationMultiSpellCastPayload)
        or not isinstance(snapshot, NonCombatSnapshot)
        or positions is None
    ):
        raise ValueError("multi spell result requires its original cast request")
    unresolved = tuple(target.target_id for target in positions.targets if not target.willing)
    if result.status == "needs_choice":
        choice = result.choice
        if (
            not isinstance(choice, MultiSpellWillingnessChoice)
            or choice.actor_id != request.actor_id
            or choice.target_ids != unresolved
        ):
            raise ValueError("spell choice differs from its unverified targets")
        return
    if result.status != "accepted":
        return
    if (
        unresolved
        or result.state_delta is None
        or result.rng_transition is None
        or result.rng_transition.state_changed
        or snapshot.resource_state is None
    ):
        raise ValueError("multi spell cast requires fixed consent, payment and unchanged RNG")
    payment, *creations = result.state_delta.operations
    events = result.proposed_events
    if (
        payment != spell_payment(snapshot.resource_state, request.actor_id, payload.slot_level)
        or len(creations) != len(payload.target_ids)
        or len(events) != 2 + len(creations)
    ):
        raise ValueError("multi spell writes do not match one slot and the selected targets")
    _verify_cast_evidence(request, payload, events[0], events[1])
    preexisting = {
        (effect.target_id, effect.id, effect.origin) for effect in snapshot.effect_states
    }
    for target_id, creation, applied in zip(payload.target_ids, creations, events[2:], strict=True):
        if (
            not isinstance(creation, WorldEffectCreate)
            or not creation.expected_absent
            or creation.value.concentration
            or creation.value.identity in preexisting
            or not isinstance(applied, EffectApplied)
            or effect_state(applied.effect) != creation.value.effect
        ):
            raise ValueError("multi spell effect evidence differs from its proposed write")
        creation.value.verify_request(
            WorldEffectApplicationRequest(
                schema_version="engine-world-effect/1",
                session_id=request.session_id,
                command_id=request.command_id,
                source_id=request.actor_id,
                target_id=target_id,
                ruleset_binding=request.ruleset_binding,
            )
        )


def verify_multi_spell_definition(
    result: RuleEvaluationResult, request: RuleEvaluationRequest, loader: AssetLoader
) -> None:
    """Match a verified accepted result to the bound activity and passive template.

    The host calls this after verifying the result envelope and associations.
    This only projects deterministic asset metadata. It does not reevaluate
    range, resources, authorization or RNG.
    """
    payload = request.payload
    if not isinstance(payload, ExplorationMultiSpellCastPayload) or result.state_delta is None:
        raise ValueError("multi spell definition requires its accepted cast")
    spell = loader.get_spell(payload.spell_id)
    if spell is None or spell.slug != payload.spell_id or spell.level != payload.slot_level:
        raise ValueError("multi spell does not match its bound definition and slot level")
    activity, template, capacity = _multi_spell_profile(spell, payload, request.actor_id)
    intent, cast, *_ = result.proposed_events
    if (
        len(payload.target_ids) > capacity
        or not isinstance(intent, IntentSubmitted)
        or intent.activity_id != activity.id
        or cast != spell_event(request.actor_id, spell, payload.slot_level)
    ):
        raise ValueError("multi spell evidence does not match the bound activity")
    for target_id, creation in zip(
        payload.target_ids, result.state_delta.operations[1:], strict=True
    ):
        expected = effect_state(template.model_copy(update={"target_id": target_id}))
        if not isinstance(creation, WorldEffectCreate) or creation.value.effect != expected:
            raise ValueError("multi spell effect differs from the bound passive effect")


def _verify_cast_evidence(
    request: RuleEvaluationRequest,
    payload: ExplorationMultiSpellCastPayload,
    intent: object,
    cast: object,
) -> None:
    """Bind the single submission and cast evidence to its caster, source and slot."""
    if (
        not isinstance(intent, IntentSubmitted)
        or intent.intent_type != "cast_spell"
        or not intent.activity_id
        or intent.actor_id != request.actor_id
        or intent.source_id != payload.spell_source_id
        or intent.spell_id != payload.spell_id
        or intent.target_id is not None
        or intent.item_id is not None
        or not isinstance(cast, SpellCast)
        or cast.actor_id != request.actor_id
        or cast.spell_id != payload.spell_id
        or cast.slot_level != payload.slot_level
        or cast.ritual
        or cast.material_consumed
    ):
        raise ValueError("multi spell intent and cast evidence do not match")
