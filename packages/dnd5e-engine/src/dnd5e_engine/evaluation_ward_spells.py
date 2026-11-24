"""Evaluate the reviewed Bless and Bane creature casts from fixed world facts."""

from dataclasses import dataclass
from functools import lru_cache

from dnd5e_srd_data.loader import AssetLoader, BundledAssetLoader
from dnd5e_srd_data.schema.common import SaveActivity, UtilityActivity

from dnd5e_engine.activities.actor_stats import ability_modifier_of, proficiency_bonus_of
from dnd5e_engine.activities.effects import passive_effect_to_active_effect
from dnd5e_engine.activities.save import spellcasting_save_dc
from dnd5e_engine.areas import has_line_of_effect
from dnd5e_engine.evaluation_actor import combatant
from dnd5e_engine.evaluation_casting import CastingStateV20
from dnd5e_engine.evaluation_contracts import (
    ExplorationSpellCastPayload,
    RuleEvaluationRequest,
    RuleEvaluationResult,
    SavePayload,
)
from dnd5e_engine.evaluation_delta import StateDelta, WorldEffectCreate
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_payments import spell_payment
from dnd5e_engine.evaluation_rng import RNGTransition
from dnd5e_engine.evaluation_saves import (
    _save_equipment,
    project_save_bonus_dice,
    resolve_snapshot_save,
    save_actor_failure,
)
from dnd5e_engine.evaluation_spells import spell_event
from dnd5e_engine.evaluation_state import NonCombatSnapshot
from dnd5e_engine.evaluation_timed_spells import exploration_source_failure
from dnd5e_engine.evaluation_turn import TurnEvaluation, _refuse
from dnd5e_engine.evaluation_world_effect import EffectExpiryPolicy, WorldEffectApplication
from dnd5e_engine.events import EffectApplied, IntentSubmitted
from dnd5e_engine.rules.conditions import conditions_block_actions
from dnd5e_engine.spatial import GridTopology, canonical_cell_id
from dnd5e_engine.specs import GridScene
from dnd5e_engine.spellcasting import resolve_target_count, spell_range_ft

_CLASSES = {
    "bless": frozenset({"cleric", "paladin"}),
    "bane": frozenset({"bard", "cleric", "warlock"}),
}


@dataclass(frozen=True)
class WardSpellNeeds:
    """Identify the selected actors and bound delivery facts the Host must fix."""

    actor_ids: tuple[str, ...]
    sight_target_ids: tuple[str, ...]
    range_ft: int
    material_instance_id: str | None


def prepare_ward_spell(
    payload: ExplorationSpellCastPayload, actor_id: str, loader: AssetLoader
) -> WardSpellNeeds | None:
    """Declare draw-free Bless/Bane facts from one reviewed bound spell profile."""
    if payload.target_ids is None:
        return None
    spell = loader.get_spell(payload.spell_id)
    try:
        _profile(spell, payload.slot_level)
    except (AttributeError, ValueError):
        return None
    return WardSpellNeeds(
        actor_ids=tuple(dict.fromkeys((actor_id, *payload.target_ids))),
        sight_target_ids=payload.target_ids if spell.slug == "bane" else (),
        range_ft=spell_range_ft(spell),
        material_instance_id=payload.material_instance_id,
    )


@lru_cache(maxsize=2)
def _canonical(spell_id: str):
    """Keep the two reviewed SRD spell profiles separate from arbitrary overrides."""
    return BundledAssetLoader().get_spell(spell_id)


def _profile(spell, level):
    """Read target capacity and the actual activity from the bound spell."""
    if spell.slug not in ("bless", "bane") or spell != _canonical(spell.slug):
        raise ValueError("ward requires its reviewed canonical spell")
    if (
        level < spell.level
        or len(spell.activities) != 1
        or len(spell.passive_effects) != 1
        or not spell.concentration
        or spell.duration.units != "minute"
        or spell.duration.value != 1
        or spell_range_ft(spell) != 30
    ):
        raise ValueError("ward duration, range or activity differs")
    activity = spell.activities[0]
    capacity = resolve_target_count(activity.target.affects.count, cast_level=level)
    if (
        capacity != level + 2
        or activity.target.affects.type != "creature"
        or not isinstance(activity, UtilityActivity if spell.slug == "bless" else SaveActivity)
    ):
        raise ValueError("ward target or save activity differs")
    if spell.slug == "bane" and (
        activity.save.ability != ["cha"] or activity.save.dc.calculation != "spellcasting"
    ):
        raise ValueError("Bane requires its bound Charisma save")
    return activity, spell.passive_effects[0], capacity


def _holy_symbol_failure(request, loader):
    """Require a reviewed priced symbol from one fixed accessible instance.

    RUO-62-T1: equip/wear posture is not present in the current inventory
    fact. Extend this gate when the owner exposes that typed fact and a public
    cast verifies it; until then this supports accessible canonical symbols.
    """
    instance_id = request.payload.material_instance_id
    entry = next(
        (
            item
            for item in request.state_snapshot.inventory_state.entries
            if item.instance_id == instance_id
        ),
        None,
    )
    if (
        entry is None
        or entry.owner_id != request.actor_id
        or not entry.accessible
        or entry.quantity != 1
        or entry.item_slug not in {"amulet", "emblem", "reliquary"}
    ):
        return "Bless requires an accessible Holy Symbol worth at least 5 GP"
    item = loader.get_item(entry.item_slug)
    canonical = BundledAssetLoader().get_item(entry.item_slug)
    if item is None or item != canonical or item.cost_gp is None or item.cost_gp < 5:
        return "Holy Symbol differs from the reviewed material"
    return None


def evaluate_ward_spell(  # noqa: C901 - two fixed ward outcomes share one admission path
    request: RuleEvaluationRequest, loader: AssetLoader
) -> TurnEvaluation:
    """Resolve ordered Bane saves and propose one payment plus surviving effects."""
    snapshot, payload, positions = (
        request.state_snapshot,
        request.payload,
        request.multi_spell_positions,
    )
    if (
        not isinstance(snapshot, NonCombatSnapshot)
        or not isinstance(payload, ExplorationSpellCastPayload)
        or positions is None
        or payload.target_ids is None
    ):
        raise ValueError("ward requires fixed noncombat target facts")
    spell = loader.get_spell(payload.spell_id)
    try:
        activity, passive, capacity = _profile(spell, payload.slot_level)
    except (AttributeError, ValueError) as exc:
        return _refuse("unsupported", "spell.asset", str(exc))
    if len(payload.target_ids) > capacity:
        return _refuse("rejected", "spell.target_count", "too many ward targets")
    if spell.slug == "bless":
        failure = _holy_symbol_failure(request, loader)
        if failure:
            return _refuse("rejected", "spell.material", failure)
    elif payload.material_instance_id is not None:
        return _refuse("rejected", "spell.material", "Bane uses its ordinary component source")
    actors = {actor.entity_id: actor for actor in snapshot.character_states}
    caster = actors.get(request.actor_id)
    if (
        caster is None
        or not caster.is_alive
        or caster.hp_current <= 0
        or conditions_block_actions([condition.condition for condition in caster.conditions])
    ):
        return _refuse("rejected", "actor_incapacitated", "caster cannot act")
    if failure := exploration_source_failure(
        snapshot,
        caster,
        payload.spell_source_id,
        spell,
        loader,
        allowed_classes=_CLASSES[spell.slug],
        allow_pact=spell.slug == "bane",
        has_selected_material=spell.slug == "bless",
    ):
        return _refuse("rejected", "spell.source", failure)
    if snapshot.resource_state is None:
        return _refuse("rejected", "spell.slot", "fixed slot resources are absent")
    try:
        payment = spell_payment(snapshot.resource_state, request.actor_id, payload.slot_level)
    except ValueError as exc:
        return _refuse("rejected", "spell.slot", str(exc))
    topology = GridTopology(GridScene.model_validate(snapshot.scene_state.grid.model_dump()))
    try:
        source = canonical_cell_id(positions.source_cell)
        if not topology.is_valid_cell(source):
            raise ValueError("caster cell is invalid")  # noqa: TRY301
        for target_fact in positions.targets:
            target = actors.get(target_fact.target_id)
            cell = canonical_cell_id(target_fact.cell_id)
            if (
                target is None
                or not target.is_alive
                or target.hp_current <= 0
                or not topology.is_valid_cell(cell)
                or not topology.within_range(source, cell, 30)
                or not has_line_of_effect(topology, source, cell)
                or (spell.slug == "bane" and not target_fact.visible)
            ):
                raise ValueError("selected creature is not an eligible target in range")  # noqa: TRY301
    except ValueError as exc:
        return _refuse("rejected", "spell.target", str(exc))
    existing = {(effect.target_id, effect.id, effect.origin) for effect in snapshot.effect_states}
    rng = request.rng_context.state.restore()
    saves = []
    selected = []
    bonus = {}
    equipment = {}
    if spell.slug == "bane":
        for target_id in payload.target_ids:
            try:
                equipment[target_id], allowed_items = _save_equipment(
                    actors[target_id], snapshot, loader
                )
            except ValueError as exc:
                return _refuse("unsupported", "spell.save", str(exc))
            if failure := save_actor_failure(
                actors[target_id],
                loader,
                reviewed_identity=True,
                reviewed_concentration=True,
                reviewed_equipment=True,
                reviewed_save_item_slugs=allowed_items,
                magic_resistance_loader=loader,
                include_implied_conditions=True,
            ):
                return _refuse("unsupported", "spell.save", failure)
        try:
            bonus = project_save_bonus_dice(
                (
                    effect
                    for effect in snapshot.effect_states
                    if effect.target_id in payload.target_ids
                ),
                loader,
                allow_lifecycle_marker=True,
            )
        except ValueError as exc:
            return _refuse("unsupported", "spell.save", str(exc))
        casting = snapshot.casting_state
        if not isinstance(casting, CastingStateV20):
            return _refuse("rejected", "spell.source", "fixed casting source is absent")
        source = next(
            (
                item
                for actor in casting.actors
                if actor.actor_id == request.actor_id
                for item in actor.sources
                if item.source_id == payload.spell_source_id
            ),
            None,
        )
        if source is None:
            return _refuse("rejected", "spell.source", "selected casting source is absent")
        dc = spellcasting_save_dc(
            proficiency_bonus_of(combatant(caster)),
            ability_modifier_of(combatant(caster), source.ability),
        )
    for target_id in payload.target_ids:
        if spell.slug == "bane":
            save = resolve_snapshot_save(
                actors[target_id],
                SavePayload(
                    kind="rules.save",
                    target_id=target_id,
                    ability="cha",
                    dc=dc,
                    advantage=(),
                    disadvantage=(),
                    is_magical=True,
                ),
                rng,
                bonus_dice=bonus.get(target_id, ()),
                equipment_bonus=equipment[target_id],
            )
            saves.append(save)
            if save.succeeded:
                continue
        selected.append(target_id)
    creations = []
    effects = []
    for target_id in selected:
        active = passive_effect_to_active_effect(
            passive, target_id=target_id, caster_id=request.actor_id, concentration=True
        )
        effect = effect_state(active)
        if (effect.target_id, effect.id, effect.origin) in existing:
            return _refuse("rejected", "spell.effect_exists", "target already has this ward")
        effects.append(EffectApplied(effect=active))
        creations.append(
            WorldEffectCreate(
                kind="world.effect_create",
                expected_absent=True,
                value=WorldEffectApplication(
                    session_id=request.session_id,
                    command_id=request.command_id,
                    source_id=request.actor_id,
                    effect=effect,
                    expiry=EffectExpiryPolicy(action="remove"),
                    concentration=True,
                    ruleset_binding=request.ruleset_binding,
                ),
            )
        )
    return dict(
        status="accepted",
        state_delta=StateDelta(
            expected_world_version=snapshot.world_version,
            operations=(
                payment,
                *creations,
            ),
        ),
        proposed_events=(
            IntentSubmitted(
                actor_id=request.actor_id,
                intent_type="cast_spell",
                spell_id=spell.slug,
                source_id=payload.spell_source_id,
                activity_id=activity.id,
            ),
            spell_event(request.actor_id, spell, payload.slot_level),
            *saves,
            *effects,
        ),
        rng_transition=RNGTransition.between(request.rng_context, rng),
        choice=None,
        error=None,
    )


def verify_ward_cast(
    result: RuleEvaluationResult, request: RuleEvaluationRequest, *, loader: AssetLoader | None
) -> None:
    """Bind accepted ward payment, ordered saves and effects to the fixed request."""
    if result.status != "accepted":
        return
    # The Host separately checks the bound asset. The fixed save input and RNG
    # make the RE proposal deterministic, including failed-target selection.
    if loader is None:
        raise ValueError("ward verification requires the bound ruleset loader")
    expected = evaluate_ward_spell(request, loader)
    if (
        expected["status"] != "accepted"
        or result.state_delta != expected["state_delta"]
        or result.proposed_events != expected["proposed_events"]
        or result.rng_transition != expected["rng_transition"]
    ):
        raise ValueError("ward proposal differs from the fixed cast and ordered saves")
