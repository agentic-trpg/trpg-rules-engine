"""Evaluate creation and termination of spatial spell effects from fixed exploration facts."""

from functools import lru_cache
from random import Random
from typing import Literal

from dnd5e_srd_data.loader import AssetLoader, BundledAssetLoader
from dnd5e_srd_data.schema.environment import EnvironmentalSpec
from dnd5e_srd_data.schema.spell import Spell, SpellDurationUnits

from dnd5e_engine.activities.actor_stats import (
    _SCORE_ATTR,
    ability_modifier_of,
    proficiency_bonus_of,
)
from dnd5e_engine.activities.check_pipeline import resolve_check_request
from dnd5e_engine.activities.context import ActivityResolutionContext
from dnd5e_engine.areas import AreaTemplate, area_activity, area_cells, area_template
from dnd5e_engine.evaluation_actor import combatant
from dnd5e_engine.evaluation_casting import CastingStateV20
from dnd5e_engine.evaluation_checks import _check_actor_effects
from dnd5e_engine.evaluation_contracts import (
    DISPEL_MAGIC_SCHEMA_VERSION,
    ENVIRONMENTAL_SPELL_SCHEMA_VERSION,
    FAERIE_FIRE_SCHEMA_VERSION,
    FOG_CLOUD_SCHEMA_VERSION,
    CheckPayload,
    RuleEvaluationRequest,
    RuleEvaluationResult,
    SavePayload,
)
from dnd5e_engine.evaluation_delta import ResourceUpdate, StateDelta, StateDeltaOperation
from dnd5e_engine.evaluation_environmental_contracts import (
    EnvironmentalCreatureTarget,
    EnvironmentalItemTarget,
    EnvironmentalMagicEffectTarget,
    EnvironmentalPointTarget,
    ExplorationEnvironmentalSpellCastPayload,
    VisualTargetEffect,
    WorldEnvironmentalEffect,
    WorldEnvironmentalEffectCreate,
    WorldEnvironmentalEffectEnd,
    WorldVisualEffectCreate,
)
from dnd5e_engine.evaluation_payments import spell_payment
from dnd5e_engine.evaluation_projection import EvaluationInvariantError
from dnd5e_engine.evaluation_rng import RNGTransition
from dnd5e_engine.evaluation_saves import resolve_snapshot_save, save_actor_failure
from dnd5e_engine.evaluation_spell_termination import WorldSpellEnd
from dnd5e_engine.evaluation_state import CharacterStateV2, NonCombatSnapshot
from dnd5e_engine.evaluation_timed_spells import (
    exploration_source_failure,
    exploration_spell_reaches,
)
from dnd5e_engine.evaluation_turn import TurnEvaluation, _refuse
from dnd5e_engine.events import (
    Ability,
    CheckRolled,
    CombatEvent,
    IntentSubmitted,
    SaveRolled,
    SpellCast,
)
from dnd5e_engine.rules.conditions import conditions_block_actions
from dnd5e_engine.spatial import GridTopology
from dnd5e_engine.specs import GridScene
from dnd5e_engine.spellcasting import derive_pact_slots, spell_component_metadata
from dnd5e_engine.types.checks import CheckRequest

_CLASSES = {
    "light": frozenset({"bard", "cleric", "sorcerer", "wizard"}),
    "darkness": frozenset({"sorcerer", "warlock", "wizard"}),
    "fog-cloud": frozenset({"druid", "ranger", "sorcerer", "wizard"}),
    "silence": frozenset({"bard", "cleric", "ranger"}),
    "dispel-magic": frozenset(
        {"bard", "cleric", "druid", "paladin", "ranger", "sorcerer", "warlock", "wizard"}
    ),
    "faerie-fire": frozenset({"bard", "druid"}),
}
_LIGHT = EnvironmentalSpec(kind="light", light="bright", dim_extension_ft=20, object_anchoring=True)
_SECONDS_PER_UNIT = {SpellDurationUnits.MINUTE: 60, SpellDurationUnits.HOUR: 3600}


def environmental_spell_schema_version(
    spell_id: str, loader: AssetLoader, *, ending: bool = False
) -> str:
    """Select the spatial wire from typed spell mechanics, leaving admission to RE."""
    if ending:
        return DISPEL_MAGIC_SCHEMA_VERSION
    spell = loader.get_spell(spell_id)
    activity = area_activity(spell.activities) if spell is not None else None
    environment = (
        activity.persistent_area.environment
        if activity is not None and activity.persistent_area is not None
        else None
    )
    return (
        FAERIE_FIRE_SCHEMA_VERSION
        if spell_id == "faerie-fire"
        else FOG_CLOUD_SCHEMA_VERSION
        if environment is not None and environment.obscurement != "none"
        else ENVIRONMENTAL_SPELL_SCHEMA_VERSION
    )


@lru_cache(maxsize=len(_CLASSES))
def _reviewed_spell(spell_id: str) -> Spell | None:
    """Read only the supported bundled spell, kept private for exact profile checks."""
    return BundledAssetLoader().get_spell(spell_id) if spell_id in _CLASSES else None


def _effect(
    actor_id: str, payload: ExplorationEnvironmentalSpellCastPayload, spell: Spell
) -> WorldEnvironmentalEffectCreate:
    """Price the fixed region and lifecycle policy independently of World."""
    if not isinstance(payload.target, (EnvironmentalItemTarget, EnvironmentalPointTarget)):
        raise EvaluationInvariantError("spatial effect creation needs a placement target")
    recasts = spell.slug == "light"
    shape: Literal["sphere", "emanation"]
    if recasts:
        # SRD 5.2.1 p.144. The corpus activity represents a VTT summon profile.
        # The spell instead enchants an existing object and creates no creature.
        shape, radius, environment = "sphere", 20, _LIGHT
    else:
        activity = area_activity(spell.activities)
        if activity is None:
            raise EvaluationInvariantError("canonical spatial spell lacks an area activity")
        template = area_template(activity, cast_level=payload.slot_level, base_level=spell.level)
        environment = activity.persistent_area.environment
        if template is None or environment is None:
            raise EvaluationInvariantError("canonical spatial spell lacks an environmental area")
        shape = "emanation" if isinstance(payload.target, EnvironmentalItemTarget) else "sphere"
        radius = template.size_ft
    duration = spell.duration.value
    if not duration:
        raise EvaluationInvariantError("canonical spatial spell requires elapsed duration")
    return WorldEnvironmentalEffectCreate(
        kind="world.environmental_effect_create",
        value=WorldEnvironmentalEffect(
            source_id=actor_id,
            spell_source_id=payload.spell_source_id,
            spell_id=spell.slug,
            slot_level=payload.slot_level,
            duration_seconds=_SECONDS_PER_UNIT[spell.duration.units] * duration,
            concentration=spell.concentration,
            replace_same_caster_on_recast=recasts,
            anchor=payload.target,
            environment=environment,
            radius_ft=radius,
            shape=shape,
        ),
    )


def _events(
    actor_id: str, payload: ExplorationEnvironmentalSpellCastPayload, spell: Spell
) -> tuple[IntentSubmitted, SpellCast]:
    """Describe the cast and its component evidence using the existing event vocabulary."""
    components, material, consumed, cost = spell_component_metadata(spell)
    return (
        IntentSubmitted(
            actor_id=actor_id,
            intent_type="cast_spell",
            spell_id=spell.slug,
            item_id=payload.target.item_id
            if isinstance(payload.target, EnvironmentalItemTarget)
            else None,
            source_id=payload.spell_source_id,
        ),
        SpellCast(
            actor_id=actor_id,
            spell_id=spell.slug,
            slot_level=payload.slot_level or None,
            ritual=False,
            components=list(components),
            material=material,
            material_consumed=consumed,
            material_cost_gp=cost,
        ),
    )


def _payment(
    snapshot: NonCombatSnapshot, actor_id: str, level: int, *, allow_pact: bool = False
) -> ResourceUpdate:
    """Select one owned slot from the fixed snapshot using the cast's payment profile."""
    resources = snapshot.resource_state
    if resources is None:
        raise ValueError("no owned slot remains")
    payment = spell_payment(resources, actor_id, level)
    if payment.expected.kind == "pact_slot":
        actor = next((a for a in snapshot.character_states if a.entity_id == actor_id), None)
        if actor is None:
            raise ValueError("Pact payment requires the fixed caster")
        class_levels = actor.classes or (
            {actor.class_slug: actor.character_level} if actor.class_slug else {}
        )
        pact_level = class_levels.get("warlock", 0)
        if (
            not allow_pact
            or not 1 <= pact_level <= 20
            or derive_pact_slots(pact_level).get(level) != payment.expected.maximum
        ):
            raise ValueError("no reviewed owned Pact slot remains")
    return payment


def _operations(
    request: RuleEvaluationRequest,
    snapshot: NonCombatSnapshot,
    payload: ExplorationEnvironmentalSpellCastPayload,
    spell: Spell,
) -> tuple[StateDeltaOperation, ...]:
    """Pair payment with one exact spatial effect creation or ending."""
    if isinstance(payload.target, EnvironmentalMagicEffectTarget):
        positions = request.spell_positions
        if positions is None or positions.target_effect is None:
            raise EvaluationInvariantError("spatial effect termination needs its fixed facts")
        return (
            _payment(snapshot, request.actor_id, payload.slot_level),
            WorldEnvironmentalEffectEnd(
                kind="world.environmental_effect_end",
                expected=positions.target_effect,
                reason="dispelled",
            ),
        )
    effect = _effect(request.actor_id, payload, spell)
    if not payload.slot_level:
        return (effect,)
    return (
        _payment(
            snapshot, request.actor_id, payload.slot_level, allow_pact=spell.slug == "darkness"
        ),
        effect,
    )


def _target_failure(request: RuleEvaluationRequest, spell: Spell) -> str | None:
    """Check SRD placement restrictions or the reviewed draw-free dispel profile."""
    positions = request.spell_positions
    assert positions is not None
    item = positions.target_item
    effect = positions.target_effect
    if spell.slug == "dispel-magic":
        if request.schema_version == DISPEL_MAGIC_SCHEMA_VERSION:
            payload = request.payload
            if not isinstance(payload, ExplorationEnvironmentalSpellCastPayload):
                raise EvaluationInvariantError("Dispel requires an environmental spell payload")
            if isinstance(payload.target, EnvironmentalCreatureTarget):
                applications = positions.target_applications
                if any(
                    fact.cast_slot_level is None
                    or fact.effect not in request.state_snapshot.effect_states
                    for fact in applications
                ):
                    return (
                        "creature spell applications require actual cast levels and fixed effects"
                    )
            elif effect is None:
                return "selected spatial effect requires its actual cast level"
            return None
        if effect is None or effect.spell_id != "darkness" or not 2 <= effect.slot_level <= 3:
            return "reviewed Dispel Magic requires one fixed-point Darkness of level 2 or 3"
        return None
    if effect is not None:
        return "effect creation requires a placement target"
    if spell.slug == "light":
        if item is None or item.size not in ("tiny", "small", "medium", "large"):
            return "Light requires a Large or smaller object of known size"
        if item.worn_or_carried_by not in (None, request.actor_id):
            return "Light cannot target an object worn or carried by someone else"
    elif spell.slug in ("fog-cloud", "silence") and item is not None:
        return f"{spell.name} requires a fixed point"
    elif item is not None and item.worn_or_carried_by is not None:
        return "Darkness cannot target a worn or carried object"
    return None


def _slot_valid(payload: ExplorationEnvironmentalSpellCastPayload, spell: Spell) -> bool:
    """Recognize a slotless cantrip or a valid base-level or higher ordinary slot."""
    return payload.slot_level == 0 if spell.level == 0 else payload.slot_level >= spell.level


def _dispel_plan(
    request: RuleEvaluationRequest,
    snapshot: NonCombatSnapshot,
    payload: ExplorationEnvironmentalSpellCastPayload,
    loader: AssetLoader | None,
) -> tuple[tuple[StateDeltaOperation, ...], tuple[CombatEvent, ...], RNGTransition]:
    """Replay payment, eligible levels and checks from fixed typed facts."""
    payment = _payment(snapshot, request.actor_id, payload.slot_level)
    actor = next(a for a in snapshot.character_states if a.entity_id == request.actor_id)
    casting = snapshot.casting_state
    positions = request.spell_positions
    if not isinstance(casting, CastingStateV20) or positions is None:
        raise EvaluationInvariantError("Dispel requires fixed casting and target facts")
    source = next(
        s
        for c in casting.actors
        if c.actor_id == actor.entity_id
        for s in c.sources
        if s.source_id == payload.spell_source_id
    )
    ability = source.ability
    rng = request.rng_context.state.restore()
    events: list[CheckRolled] = []
    endings: list[StateDeltaOperation] = []
    check_context: ActivityResolutionContext | None = None
    if isinstance(payload.target, EnvironmentalCreatureTarget):
        targets = sorted(positions.target_applications, key=lambda f: f.identity)
        for fact in targets:
            level = fact.cast_slot_level
            if level is None:
                raise ValueError("target spell lacks an actual cast level")
            eligible = level <= payload.slot_level
            if not eligible:
                if check_context is None:
                    check_context = _dispel_check_context(snapshot, actor, ability, loader, rng)
                eligible = _dispel_check(check_context, actor.entity_id, ability, level, events)
            if eligible:
                endings.append(WorldSpellEnd(expected=fact, reason="dispelled"))
    else:
        effect = positions.target_effect
        if effect is None:
            raise EvaluationInvariantError("Dispel requires the selected spatial effect")
        level = effect.slot_level
        eligible = level <= payload.slot_level
        if not eligible:
            check_context = _dispel_check_context(snapshot, actor, ability, loader, rng)
            eligible = _dispel_check(check_context, actor.entity_id, ability, level, events)
        if eligible:
            endings.append(
                WorldEnvironmentalEffectEnd(
                    kind="world.environmental_effect_end", expected=effect, reason="dispelled"
                )
            )
    return (
        (payment, *endings),
        (*_events(actor.entity_id, payload, _dispel_spell()), *events),
        RNGTransition.between(request.rng_context, rng),
    )


def _admit_dispel_targets(request: RuleEvaluationRequest, loader: AssetLoader) -> None:
    """Bind every recorded effective level to its actual target spell asset once."""
    positions = request.spell_positions
    if positions is None:
        raise EvaluationInvariantError("Dispel requires fixed target facts")
    payload = request.payload
    if not isinstance(payload, ExplorationEnvironmentalSpellCastPayload):
        raise EvaluationInvariantError("Dispel requires an environmental spell payload")
    if isinstance(payload.target, EnvironmentalCreatureTarget):
        for fact in positions.target_applications:
            spell = loader.get_spell(fact.spell_id)
            if spell is None or fact.cast_slot_level is None or fact.cast_slot_level < spell.level:
                raise ValueError("target spell lacks a bound cast level")
    else:
        effect = positions.target_effect
        if effect is None:
            raise EvaluationInvariantError("Dispel requires the selected spatial effect")
        spell = loader.get_spell(effect.spell_id)
        if spell is None or effect.slot_level < spell.level:
            raise ValueError("spatial spell lacks a bound actual cast level")


def _dispel_spell() -> Spell:
    """Use the canonical Dispel event facts already checked on native admission."""
    spell = _reviewed_spell("dispel-magic")
    if spell is None:
        raise EvaluationInvariantError("Dispel definition is missing")
    return spell


def _dispel_check_context(
    snapshot: NonCombatSnapshot,
    actor: CharacterStateV2,
    ability: Ability,
    loader: AssetLoader | None,
    rng: Random,
) -> ActivityResolutionContext:
    """Project the caster's check dependencies once before high-level target rolls."""
    check = CheckPayload(
        actor_id=actor.entity_id,
        ability=ability,
        skill=None,
        tool=None,
        dc=10,
        target_id=None,
        context="generic",
        required_sense="none",
        social_interaction=False,
        advantage=(),
        disadvantage=(),
        redeem_granted_die=None,
    )
    caster = combatant(actor)
    effects = [effect for effect in snapshot.effect_states if effect.target_id == actor.entity_id]
    state = _check_actor_effects(actor, effects, check, loader)
    return ActivityResolutionContext(
        rng=rng,
        caster=caster,
        targets=[],
        event_emitter=lambda event: None,
        caster_abilities={name: getattr(caster, field) for name, field in _SCORE_ATTR.items()},
        check_states={actor.entity_id: state},
    )


def _dispel_check(
    context: ActivityResolutionContext,
    actor_id: str,
    ability: Ability,
    level: int,
    events: list[CheckRolled],
) -> bool:
    """Resolve one DC 10 + target-level check on the shared mutable RNG context."""
    check = CheckRequest(actor_id=actor_id, ability=ability, dc=10 + level)
    event = resolve_check_request(check, context)
    events.append(event)
    return event.succeeded is True


def _faerie_plan(
    request: RuleEvaluationRequest, spell: Spell
) -> tuple[tuple[StateDeltaOperation, ...], tuple[CombatEvent, ...], RNGTransition]:
    """Select fixed cube occupants once and roll each creature's Dexterity save.

    The returned visual target product is shared with later light and sight
    consumers. World owns occupant completeness and all eventual writes.
    """
    snapshot, payload, positions = request.state_snapshot, request.payload, request.spell_positions
    if not isinstance(snapshot, NonCombatSnapshot) or not isinstance(
        payload, ExplorationEnvironmentalSpellCastPayload
    ):
        raise TypeError("Faerie Fire requires an exploration spell request")
    reviewed = BundledAssetLoader()
    if positions is None or not isinstance(payload.target, EnvironmentalPointTarget):
        raise ValueError("Faerie Fire requires a placed point and fixed occupants")
    if spell.slug != "faerie-fire" or payload.slot_level < 1:
        raise ValueError("Faerie Fire requires its reviewed level-one spell")
    grid = GridTopology(GridScene.model_validate(snapshot.scene_state.grid.model_dump()))
    template = AreaTemplate("cube", "square", 20, "target", True)
    cells = area_cells(grid, template, positions.target_cell, None)
    if any(occupant.cell_id not in cells for occupant in positions.visual_occupants):
        raise ValueError("visual target is outside the cube")
    if len({(o.target_kind, o.target_id) for o in positions.visual_occupants}) != len(
        positions.visual_occupants
    ):
        raise ValueError("duplicate visual target")
    casting = snapshot.casting_state
    if not isinstance(casting, CastingStateV20):
        raise TypeError("Faerie Fire requires a fixed casting source")
    caster = next((a for a in snapshot.character_states if a.entity_id == request.actor_id), None)
    if caster is None:
        raise ValueError("Faerie Fire caster is missing")
    source = next(
        (
            source
            for actor in casting.actors
            if actor.actor_id == caster.entity_id
            for source in actor.sources
            if source.source_id == payload.spell_source_id
        ),
        None,
    )
    if source is None:
        raise ValueError("Faerie Fire source is missing")
    avatar = combatant(caster)
    ability = source.ability
    dc = 8 + proficiency_bonus_of(avatar) + ability_modifier_of(avatar, ability)
    actors = {actor.entity_id: actor for actor in snapshot.character_states}
    rng = request.rng_context.state.restore()
    saves: list[SaveRolled] = []
    selected: list[VisualTargetEffect] = []
    for occupant in sorted(
        positions.visual_occupants, key=lambda value: (value.target_kind, value.target_id)
    ):
        if occupant.target_kind == "creature":
            target = actors.get(occupant.target_id)
            if target is None or not target.is_alive:
                raise ValueError("Faerie Fire creature lacks fixed living facts")
            if any(
                effect.target_id == target.entity_id
                and not effect.disabled
                and (effect.changes or effect.flags)
                for effect in snapshot.effect_states
            ):
                # RUO-54-F1: the standalone save primitive projects conditions,
                # not arbitrary active effect modifiers. Revisit with a shared
                # admitted target save profile; status-only Invisible is valid.
                raise ValueError("RUO-54-F1: target has unsupported save effect modifiers")
            if failure := save_actor_failure(
                target,
                reviewed,
                reviewed_identity=True,
                reviewed_concentration=True,
                reviewed_equipment=True,
                include_implied_conditions=True,
            ):
                # RUO-54-F1: special trait/resource save modifiers need a reviewed
                # shared resolver input before this area save can admit them.
                raise ValueError(f"RUO-54-F1: {failure}")
            save = resolve_snapshot_save(
                target,
                SavePayload(
                    kind="rules.save",
                    target_id=target.entity_id,
                    ability="dex",
                    dc=dc,
                    advantage=(),
                    disadvantage=(),
                    is_magical=True,
                ),
                rng,
            )
            saves.append(save)
            if save.succeeded:
                continue
        selected.append(
            VisualTargetEffect(
                target_kind=occupant.target_kind,
                target_id=occupant.target_id,
                dim_radius_ft=10,
                suppresses_invisible=True,
                visible_attack_advantage=True,
            )
        )
    visual = WorldVisualEffectCreate(
        kind="world.visual_effect_create",
        source_id=caster.entity_id,
        spell_source_id=payload.spell_source_id,
        spell_id=spell.slug,
        slot_level=payload.slot_level,
        duration_seconds=60,
        concentration=True,
        targets=tuple(selected),
    )
    return (
        (_payment(snapshot, caster.entity_id, payload.slot_level), visual),
        (*_events(caster.entity_id, payload, spell), *saves),
        RNGTransition.between(request.rng_context, rng),
    )


def evaluate_environmental_spell(
    request: RuleEvaluationRequest, loader: AssetLoader
) -> TurnEvaluation:
    """Propose an exact spatial transition and payment after draw-free admission."""
    snapshot, payload = request.state_snapshot, request.payload
    if not isinstance(snapshot, NonCombatSnapshot) or not isinstance(
        payload, ExplorationEnvironmentalSpellCastPayload
    ):
        raise EvaluationInvariantError("environmental spell requires a spatial noncombat request")
    if (payload.spell_id == "faerie-fire") != (
        request.schema_version == FAERIE_FIRE_SCHEMA_VERSION
    ):
        return _refuse("unsupported", "spell.asset", "spell requires its reviewed visual schema")
    if (payload.spell_id == "fog-cloud") != (request.schema_version == FOG_CLOUD_SCHEMA_VERSION):
        return _refuse("unsupported", "spell.asset", "spell requires its reviewed spatial schema")
    classes = _CLASSES.get(payload.spell_id)
    spell = loader.get_spell(payload.spell_id) if classes is not None else None
    if spell is None or classes is None or spell != _reviewed_spell(payload.spell_id):
        return _refuse(
            "unsupported", "spell.asset", "requires a reviewed canonical environmental spell"
        )
    selects_effect = isinstance(
        payload.target, (EnvironmentalMagicEffectTarget, EnvironmentalCreatureTarget)
    )
    if selects_effect and (
        _target_failure(request, spell) or not exploration_spell_reaches(request, snapshot, spell)
    ):
        return _refuse("rejected", "target_invalid", "spatial effect is unavailable")
    if not _slot_valid(payload, spell):
        return _refuse("rejected", "spell.slot", "slot level is invalid for this spell")

    actor = next(
        (item for item in snapshot.character_states if item.entity_id == request.actor_id), None
    )
    if actor is None:
        return _refuse("rejected", "actor_missing", "caster is absent from the snapshot")
    if (
        not actor.is_alive
        or actor.hp_current <= 0
        or conditions_block_actions([condition.condition for condition in actor.conditions])
    ):
        return _refuse("rejected", "actor_incapacitated", "caster must be alive and able to act")
    if any(
        effect.target_id == actor.entity_id and not effect.disabled and effect.action_policy
        for effect in snapshot.effect_states
    ):
        return _refuse("unsupported", "spell.action_policy", "caster has an active action policy")
    if not selects_effect:
        if failure := _target_failure(request, spell):
            return _refuse("rejected", "target_invalid", failure)
        if not exploration_spell_reaches(request, snapshot, spell):
            return _refuse("rejected", "target_invalid", "target requires range and a clear path")
    if failure := exploration_source_failure(
        snapshot,
        actor,
        payload.spell_source_id,
        spell,
        loader,
        allowed_classes=classes,
        allow_pact=spell.slug == "darkness",
    ):
        return _refuse("rejected", "spell.source", failure)
    if request.schema_version == FAERIE_FIRE_SCHEMA_VERSION:
        try:
            operations, events, transition = _faerie_plan(request, spell)
        except ValueError as exc:
            return _refuse("unsupported", "spell.visual_targets", str(exc))
        return dict(
            status="accepted",
            state_delta=StateDelta(
                expected_world_version=snapshot.world_version, operations=operations
            ),
            proposed_events=events,
            rng_transition=transition,
            choice=None,
            error=None,
        )
    try:
        if request.schema_version == DISPEL_MAGIC_SCHEMA_VERSION:
            _admit_dispel_targets(request, loader)
            operations, events, transition = _dispel_plan(request, snapshot, payload, loader)
        else:
            operations = _operations(request, snapshot, payload, spell)
            events = _events(request.actor_id, payload, spell)
            transition = RNGTransition(
                stream_id=request.rng_context.stream_id,
                input_version=request.rng_context.version,
                input_state=request.rng_context.state,
                next_state=request.rng_context.state,
                state_changed=False,
            )
    except ValueError as exc:
        return _refuse("rejected", "spell.slot", str(exc))
    return dict(
        status="accepted",
        state_delta=StateDelta(
            expected_world_version=snapshot.world_version, operations=operations
        ),
        proposed_events=events,
        rng_transition=transition,
        choice=None,
        error=None,
    )


def verify_environmental_cast(
    result: RuleEvaluationResult, request: RuleEvaluationRequest, loader: AssetLoader | None = None
) -> None:
    """Bind profile, target identity, exact transition, payment and evidence to the input."""
    payload, snapshot = request.payload, request.state_snapshot
    if not isinstance(payload, ExplorationEnvironmentalSpellCastPayload) or not isinstance(
        snapshot, NonCombatSnapshot
    ):
        raise TypeError("environmental effect requires its original spatial world request")
    spell = _reviewed_spell(payload.spell_id)
    if request.schema_version == FAERIE_FIRE_SCHEMA_VERSION:
        if spell is None or result.state_delta is None:
            raise ValueError("Faerie Fire result lacks its reviewed spell or delta")
        operations, events, transition = _faerie_plan(request, spell)
        if (
            result.state_delta.operations != operations
            or result.proposed_events != events
            or result.rng_transition != transition
        ):
            raise ValueError("Faerie Fire result differs from fixed occupants and RNG")
        return
    if request.schema_version == DISPEL_MAGIC_SCHEMA_VERSION:
        operations, events, transition = _dispel_plan(request, snapshot, payload, loader)
        if (
            result.state_delta is None
            or result.state_delta.operations != operations
            or result.proposed_events != events
            or result.rng_transition != transition
        ):
            raise ValueError("Dispel result differs from its fixed target and RNG")
        return
    if (
        spell is None
        or (payload.spell_id == "fog-cloud") != (request.schema_version == FOG_CLOUD_SCHEMA_VERSION)
        or not _slot_valid(payload, spell)
        or _target_failure(request, spell)
        or not exploration_spell_reaches(request, snapshot, spell)
        or result.state_delta is None
        or result.state_delta.operations != _operations(request, snapshot, payload, spell)
        or result.proposed_events != _events(request.actor_id, payload, spell)
        or result.rng_transition is None
        or result.rng_transition.state_changed
    ):
        raise ValueError("environmental cast result differs from its fixed spell input")
