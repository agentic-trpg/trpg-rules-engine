"""Evaluate a bound retained item casting a reviewed instant exploration heal."""

import re
from dataclasses import dataclass, replace

from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.common import (
    ActivationBlock,
    CastActivity,
    CastSpellBlock,
    ConsumptionBlock,
    ConsumptionTargetEntry,
    HealActivity,
)
from dnd5e_srd_data.schema.feature import RecoveryRule
from dnd5e_srd_data.schema.item import Item
from dnd5e_srd_data.schema.spell import Spell

from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.heal import HealingModifiers, resolve_heal
from dnd5e_engine.activities.resolver import resolve_activity
from dnd5e_engine.areas import area_activity, area_template, has_line_of_effect
from dnd5e_engine.evaluation_actor import combatant
from dnd5e_engine.evaluation_class_dependencies import class_dependencies
from dnd5e_engine.evaluation_contracts import (
    STAFF_CAST_SCHEMA_VERSION,
    ExplorationItemUsePayload,
    RuleEvaluationRequest,
    RuleEvaluationResult,
)
from dnd5e_engine.evaluation_delta import InventorySpendCharge, StateDelta
from dnd5e_engine.evaluation_equipment_dependencies import is_stowed_inventory_entry
from dnd5e_engine.evaluation_exploration_healing import (
    exploration_healing_activity,
    exploration_healing_hp_delta,
    exploration_healing_maximizes,
    exploration_healing_requires_sight,
)
from dnd5e_engine.evaluation_items import ItemEvaluation, _equipment_failure, _refuse
from dnd5e_engine.evaluation_projection import EvaluationInvariantError
from dnd5e_engine.evaluation_rng import RNGTransition
from dnd5e_engine.evaluation_ruleset import FrozenRuleset, get_default_ruleset
from dnd5e_engine.evaluation_state import InventoryEntry, NonCombatSnapshot
from dnd5e_engine.evaluation_timed_spells import exploration_spell_reaches
from dnd5e_engine.events import (
    CombatEvent,
    HealingApplied,
    IntentSubmitted,
    ItemDepletionRolled,
    SpellCast,
)
from dnd5e_engine.rules.conditions import conditions_block_actions
from dnd5e_engine.rules.dice import roll_d6, roll_d20
from dnd5e_engine.spatial import GridTopology, canonical_cell_id
from dnd5e_engine.specs import GridScene
from dnd5e_engine.spellcasting import spell_range_ft

_STAFF = "staff-of-healing"
_STAFF_CLASSES = frozenset({"bard", "cleric", "druid"})
_DAWN_RECOVER_ALL = RecoveryRule(period="dawn", type="recoverAll")


@dataclass(frozen=True)
class ExplorationChargedCastProfile:
    """Bind an immediate heal, its item activity and the retained charge policy."""

    activity: CastActivity
    spell: Spell
    cost: int
    capacity: int
    recovers_at_dawn: bool
    cast_level: int = 1
    requires_held_attunement: bool = False


def is_healing_staff(item: Item) -> bool:
    """Identify the reviewed SRD 5.2.1 Staff of Healing by its canonical asset."""
    return item.slug == _STAFF and item == get_default_ruleset().get_item(_STAFF)


def staff_attunement_ability(actor, loader: AssetLoader) -> str | None:
    """Use the canonical spellcasting ability of an eligible single-class holder."""
    levels = actor.classes or (
        {actor.class_slug: actor.character_level} if actor.class_slug else {}
    )
    if len(levels) != 1 or sum(levels.values()) != actor.character_level:
        return None
    slug = next(iter(levels))
    if actor.class_slug not in (None, slug):
        return None
    cls = loader.get_class(slug)
    if slug not in _STAFF_CLASSES or cls is None or cls != get_default_ruleset().get_class(slug):
        return None
    return cls.spellcasting.ability or None


def roll_healing_staff_dawn(item: Item, rng):
    """Roll the canonical dawn allowance using the supplied uncommitted stream."""
    if not is_healing_staff(item):
        raise ValueError("random dawn recovery requires the reviewed Staff of Healing")
    return roll_d6(modifier=4, rng=rng)


def _healing_staff_cast(item: Item, loader: AssetLoader, activity_id, cast_level):
    """Select a canonical spell and its price from the staff's bound Cast activities."""
    assert item.uses is not None
    canonical = get_default_ruleset()
    for slug in ("cure-wounds", "mass-cure-wounds"):
        spell = loader.get_spell(slug)
        if spell is None or spell != canonical.get_spell(slug):
            continue
        activity = next(
            activity
            for activity in item.activities
            if isinstance(activity, CastActivity) and activity.spell.uuid == spell.foundry_uuid
        )
        if activity_id not in (None, activity.id):
            continue
        level = cast_level or spell.level
        price = int(activity.consumption.targets[0].value)
        if slug == "cure-wounds":
            if not 1 <= level <= int(activity.consumption.scaling.max):
                return None
            price *= level
        elif level != spell.level:
            return None
        return ExplorationChargedCastProfile(
            activity, spell, price, int(item.uses.max), True, level, True
        )
    return None


def _area_healing_targets(request: RuleEvaluationRequest, spell: Spell) -> tuple[str, ...]:
    """Verify the center's range and reach and each selected creature's area membership."""
    snapshot, payload, facts = request.state_snapshot, request.payload, request.item_area_facts
    assert isinstance(snapshot, NonCombatSnapshot)
    assert isinstance(payload, ExplorationItemUsePayload)
    selection = payload.area_target
    if facts is None or selection is None:
        return ()
    topology = GridTopology(GridScene.model_validate(snapshot.scene_state.grid.model_dump()))
    targets = tuple(facts.target_cells[target_id] for target_id in selection.target_ids)
    return (
        selection.target_ids
        if healing_staff_area_reaches(
            topology, spell, facts.source_cell, facts.visible_center_cell, targets
        )
        else ()
    )


def healing_staff_area_reaches(topology, spell: Spell, source: str, center: str, targets) -> bool:
    """Bound the healing sphere and its rays using the selected canonical spell."""
    child = area_activity(spell.activities)
    shape = area_template(child) if child else None
    reach = spell_range_ft(spell)
    if shape is None or reach is None:
        return False
    try:
        center = canonical_cell_id(center)
        source = canonical_cell_id(source)
        targets = tuple(map(canonical_cell_id, targets))
    except ValueError:
        return False
    return (
        topology.is_valid_cell(source)
        and topology.is_valid_cell(center)
        and topology.within_range(source, center, reach)
        and has_line_of_effect(topology, source, center)
        and all(
            topology.is_valid_cell(target)
            and topology.within_range(center, target, shape.size_ft)
            and has_line_of_effect(topology, center, target)
            for target in targets
        )
    )


def exploration_charged_cast_profile(
    item: Item,
    loader: AssetLoader,
    *,
    activity_id: str | None = None,
    cast_level: int | None = None,
) -> ExplorationChargedCastProfile | None:
    """Identify a supported charged touch heal from the bound item and activity.

    The Staff of Healing delegates to Cure Wounds at levels 1–4 or Mass Cure
    Wounds at level 5.
    A reviewed authored charm has one fixed-cost, level-one cast and may
    refill completely at dawn. The State Machine owns time and item balances.
    """
    if is_healing_staff(item):
        return _healing_staff_cast(item, loader, activity_id, cast_level)
    if cast_level is not None:
        return None
    uses = item.uses
    if (
        item.item_kind not in ("item", "magic_item")
        or item.requires_attunement
        or item.attunement_constraint
        or item.passive_effects
        or uses is None
        or uses.auto_destroy
        or uses.recovery not in ([], [_DAWN_RECOVER_ALL])
        or uses.spent
        or re.fullmatch(r"[1-9][0-9]{0,18}", uses.max) is None
        or len(item.activities) != 1
        or not isinstance(activity := item.activities[0], CastActivity)
        or not activity.id
    ):
        return None
    capacity = int(uses.max)
    costs = activity.consumption.targets
    if (
        capacity > 2**63 - 1
        or len(costs) != 1
        or re.fullmatch(r"[1-9][0-9]{0,18}", costs[0].value) is None
        or activity.spell.ability not in ("str", "dex", "con", "int", "wis", "cha")
        or not activity.spell.uuid
    ):
        return None
    cost = int(costs[0].value)
    if cost > capacity:
        return None
    basic = CastActivity(
        id=activity.id,
        activation=ActivationBlock(type="action", value=activity.activation.value),
        consumption=ConsumptionBlock(
            spell_slot=False,
            targets=[ConsumptionTargetEntry(type="itemUses", value=costs[0].value)],
        ),
        spell=CastSpellBlock(
            ability=activity.spell.ability,
            level=activity.spell.level,
            properties=[],
            spellbook=False,
            uuid=activity.spell.uuid,
        ),
    )
    metadata = {"id", "name", "img", "sort", "description"}
    if activity.activation.value not in (None, 1) or any(
        getattr(activity, field) != getattr(basic, field)
        for field in type(activity).model_fields
        if field not in metadata
    ):
        return None
    spell = loader.get_spell_by_uuid(activity.spell.uuid)
    if (
        spell is None
        or spell.foundry_uuid != activity.spell.uuid
        # A raw loader is bound by slug; its UUID accessor is not in the digest.
        or (type(loader) is not FrozenRuleset and spell != loader.get_spell(spell.slug))
        or activity.spell.level not in (None, spell.level)
        or exploration_healing_activity(loader, spell) is None
        or exploration_healing_requires_sight(spell)
    ):
        return None
    return ExplorationChargedCastProfile(activity, spell, cost, capacity, bool(uses.recovery))


def _charged_healing_modifiers(snapshot, actor_id, targets, loader, *, staff):
    """Use recipient healing effects for the Staff while keeping the authored charm fixed."""
    if any(actor.trait_mechanics for actor in snapshot.character_states):
        raise ValueError("unreviewed healing trait")
    if staff:
        return {
            target.entity_id: HealingModifiers(maximize_dice=True)
            for target in targets
            if exploration_healing_maximizes(snapshot, actor_id, target.entity_id, loader)
        }
    if any(actor.concentration_effect_id for actor in snapshot.character_states) or any(
        not effect.disabled for effect in snapshot.effect_states
    ):
        raise ValueError("unreviewed healing effect")
    return {}


def _resolve_charged_healing(activity, context, modifiers):
    """Delegate a reviewed Cast through its one Heal activity and shared dice handling."""
    if modifiers:

        def dispatch_healing(child, child_context):
            """Apply the established recipient-dependent modifier without a slot feature."""
            heal = child.activities[0]
            assert isinstance(heal, HealActivity)
            resolve_heal(
                heal,
                replace(child_context, activity_source_id=f"spell:{child.slug}:{heal.id}"),
                modifiers=modifiers,
            )

        context = replace(context, spell_dispatch=dispatch_healing)
    resolve_activity(activity, context)


def evaluate_exploration_charged_cast(
    request: RuleEvaluationRequest, entry: InventoryEntry, item: Item, loader: AssetLoader
) -> ItemEvaluation:
    """Propose retained instance payment, capped healing and the associated RNG transition.

    The item evaluator supplies the exact accessible unit from this request.
    Source, ability, activity and cost come from the bound asset. Refusals
    produce no candidate writes or RNG transition.
    """
    snapshot, payload = request.state_snapshot, request.payload
    assert isinstance(snapshot, NonCombatSnapshot)
    assert isinstance(payload, ExplorationItemUsePayload)
    staff = is_healing_staff(item)
    if staff != (request.schema_version == STAFF_CAST_SCHEMA_VERSION):
        return _refuse(
            "unsupported",
            "item.capability",
            "evaluation/41 requires the canonical Staff of Healing",
        )
    profile = exploration_charged_cast_profile(
        item, loader, activity_id=payload.activity_id, cast_level=payload.cast_level
    )
    if profile is None:
        if staff and payload.activity_id not in {activity.id for activity in item.activities}:
            return _refuse("rejected", "activity_invalid", "activity is absent from this item")
        if (
            staff
            and payload.cast_level is not None
            and exploration_charged_cast_profile(item, loader, activity_id=payload.activity_id)
            is not None
        ):
            return _refuse("rejected", "cast_level_invalid", "level differs from the activity")
        return _refuse("unsupported", "item.capability", "item lacks a reviewed charged cast")
    if profile.activity.id != payload.activity_id:
        return _refuse("rejected", "activity_invalid", "activity is absent from this item")
    if entry.quantity != 1 or entry.charges_remaining_per_unit > profile.capacity:
        return _refuse(
            "unsupported", "item.charges", "one instance with a valid charge pool is required"
        )
    if entry.charges_remaining_per_unit < profile.cost:
        return _refuse("rejected", "item_exhausted", "item has insufficient charges")
    actors = {actor.entity_id: actor for actor in snapshot.character_states}
    actor = actors[request.actor_id]
    if (
        not actor.is_alive
        or actor.hp_current <= 0
        or conditions_block_actions([condition.condition for condition in actor.conditions])
    ):
        return _refuse("rejected", "actor_incapacitated", "caster must be alive and able to act")
    if profile.spell.slug == "mass-cure-wounds":
        target_ids = _area_healing_targets(request, profile.spell)
    elif payload.area_target is None and (
        payload.target_id == actor.entity_id
        or exploration_spell_reaches(request, snapshot, profile.spell)
    ):
        target_ids = (payload.target_id,)
    else:
        target_ids = ()
    if not target_ids or any(
        target_id not in actors
        or not actors[target_id].is_alive
        or actors[target_id].hp_current <= 0
        for target_id in target_ids
    ):
        return _refuse(
            "rejected", "target_invalid", "cast requires living creatures at valid positions"
        )
    targets = tuple(actors[target_id] for target_id in target_ids)
    ability = None
    ignored_items = frozenset({entry.instance_id})
    if staff:
        if actor.held_weapon_instance_id != entry.instance_id:
            return _refuse("rejected", "item_not_held", "hold the selected staff before casting")
        if entry.attuned_actor_id != actor.entity_id:
            return _refuse(
                "rejected", "item_not_attuned", "attune to the selected staff before casting"
            )
        ability = staff_attunement_ability(actor, loader)
        if ability is None:
            return _refuse(
                "rejected", "item_attunement_class", "staff requires Bard, Cleric or Druid"
            )
        ignored_items = frozenset(
            owned.instance_id
            for owned in snapshot.inventory_state.entries
            if owned.owner_id == actor.entity_id
            and owned.item_slug == item.slug
            and (owned.instance_id == entry.instance_id or is_stowed_inventory_entry(owned, actor))
        )
    else:
        if failure := _equipment_failure(actor, item):
            return failure
        if not is_stowed_inventory_entry(entry, actor):
            return _refuse("unsupported", "item.snapshot", "unreviewed equipped charged item")
    try:
        modifiers = _charged_healing_modifiers(
            snapshot, actor.entity_id, targets, loader, staff=staff
        )
    except ValueError as exc:
        return _refuse("unsupported", "item.snapshot", str(exc))
    canonical = get_default_ruleset()
    if class_failure := class_dependencies(
        actor,
        snapshot,
        loader,
        canonical,
        equipped=True,
        stowed_consumable_ids=ignored_items,
        reviewed_lore_bard=staff,
        reviewed_life_healing=staff,
    ):
        return _refuse("unsupported", "item.snapshot", class_failure)

    rng = request.rng_context.state.restore()
    activity, spell = profile.activity, profile.spell
    events: list[CombatEvent] = [
        IntentSubmitted(
            actor_id=actor.entity_id,
            intent_type="use_item",
            item_id=item.slug,
            source_id=entry.instance_id,
            activity_id=activity.id,
            spell_id=spell.slug,
            target_id=payload.target_id,
            area_center_cell=payload.area_target.center_cell if payload.area_target else None,
            area_target_ids=payload.area_target.target_ids if payload.area_target else None,
        ),
        SpellCast(
            actor_id=actor.entity_id,
            spell_id=spell.slug,
            slot_level=None,
            ritual=False,
            components=[],
            material=None,
            material_consumed=False,
            material_cost_gp=0,
            item_cast_level=profile.cast_level if staff else None,
        ),
    ]
    caster = combatant(actor)
    context = build_activity_context(
        caster,
        [
            caster if actor.entity_id == target.entity_id else combatant(target)
            for target in targets
        ],
        rng=rng,
        event_emitter=events.append,
        slot_level=None,
        base_spell_level=None,
        spellcasting_ability=ability,
        cast_level_override=profile.cast_level if staff else None,
        concentration=False,
        source_passive_effects=[],
        spell_book={activity.spell.uuid: spell},
        passive_damage_modifiers={},
        save_modifiers={},
    )
    _resolve_charged_healing(activity, context, modifiers)
    healed = events[2:]
    if len(healed) != len(targets) or any(
        not isinstance(event, HealingApplied) or event.target_id != target.entity_id
        for target, event in zip(targets, healed, strict=True)
    ):
        raise EvaluationInvariantError("charged exploration cast must heal each selected target")
    if any(event.amount < 0 for event in healed):
        return _refuse("unsupported", "healing.amount", "negative healing is outside this profile")
    balance = entry.charges_remaining_per_unit - profile.cost
    quantity = 1
    if staff and balance == 0:
        die = roll_d20(rng=rng).total
        events.append(
            ItemDepletionRolled(actor_id=actor.entity_id, instance_id=entry.instance_id, roll=die)
        )
        quantity = int(die != 1)
    payment = InventorySpendCharge(
        kind="inventory.spend_charge",
        expected=entry,
        value=entry.model_copy(
            update={"charges_remaining_per_unit": balance, "quantity": quantity}
        ),
        amount=profile.cost,
    )
    return dict(
        status="accepted",
        state_delta=StateDelta(
            expected_world_version=snapshot.world_version,
            operations=(
                payment,
                *(
                    edit
                    for target, event in zip(targets, healed, strict=True)
                    for edit in exploration_healing_hp_delta(target, event.amount)
                ),
            ),
        ),
        proposed_events=tuple(events),
        rng_transition=RNGTransition.between(request.rng_context, rng),
        choice=None,
        error=None,
    )


def verify_charged_item_cast(result: RuleEvaluationResult, request: RuleEvaluationRequest) -> None:
    """Bind the paid instance, target, proposed events and HP to the fixed request."""
    snapshot, payload, delta = request.state_snapshot, request.payload, result.state_delta
    assert isinstance(snapshot, NonCombatSnapshot)
    assert isinstance(payload, ExplorationItemUsePayload)
    assert delta is not None
    if not delta.operations or not isinstance(spend := delta.operations[0], InventorySpendCharge):
        raise ValueError("charged item cast requires a retained charge payment")
    entry = next(
        (
            entry
            for entry in snapshot.inventory_state.entries
            if entry.instance_id == payload.instance_id
        ),
        None,
    )
    if entry is None or entry != spend.expected or entry.owner_id != request.actor_id:
        raise ValueError("charged item payment must match the exact owned request instance")
    staff = request.schema_version == STAFF_CAST_SCHEMA_VERSION
    area = payload.area_target
    target_ids = area.target_ids if area else (payload.target_id,)
    last_charge = staff and spend.value.charges_remaining_per_unit == 0
    draws = (
        not staff
        or last_charge
        or any(
            not exploration_healing_maximizes(
                snapshot, request.actor_id, target, get_default_ruleset()
            )
            for target in target_ids
        )
    )
    if result.rng_transition is None or result.rng_transition.state_changed != draws:
        raise ValueError("charged cast RNG transition must match healing and depletion")
    events = result.proposed_events
    if len(events) != 2 + len(target_ids) + int(last_charge):
        raise ValueError("charged cast requires the item intent, cast and healing evidence")
    intent, cast = events[:2]
    healed = events[2 : 2 + len(target_ids)]
    if last_charge:
        depletion = events[-1]
        if (
            not isinstance(depletion, ItemDepletionRolled)
            or (depletion.actor_id, depletion.instance_id) != (request.actor_id, entry.instance_id)
            or spend.value.quantity != int(depletion.roll != 1)
        ):
            raise ValueError("charge depletion evidence must match the selected staff")
    elif spend.value.quantity != 1:
        raise ValueError("vanishing requires the reviewed last-charge roll")
    if (
        not isinstance(cast, SpellCast)
        or cast.actor_id != request.actor_id
        or cast.slot_level is not None
        or cast.item_cast_level != ((payload.cast_level or (5 if area else 1)) if staff else None)
        or cast.ritual
        or cast.components
        or cast.material is not None
        or cast.material_consumed
        or cast.execution_class is not None
        or cast.material_cost_gp
        or intent
        != IntentSubmitted(
            actor_id=request.actor_id,
            intent_type="use_item",
            item_id=entry.item_slug,
            source_id=entry.instance_id,
            activity_id=payload.activity_id,
            spell_id=cast.spell_id,
            target_id=payload.target_id,
            area_center_cell=area.center_cell if area else None,
            area_target_ids=area.target_ids if area else None,
        )
    ):
        raise ValueError("charged item cast events must match the request")
    actors = {actor.entity_id: actor for actor in snapshot.character_states}
    edits = []
    for target_id, healing in zip(target_ids, healed, strict=True):
        target = actors.get(target_id)
        if target is None or not target.is_alive or target.hp_current <= 0:
            raise ValueError("charged item cast requires living targets")
        if (
            not isinstance(healing, HealingApplied)
            or healing.target_id != target_id
            or healing.amount < 0
        ):
            raise ValueError("charged healing must match each selected target")
        edits.extend(exploration_healing_hp_delta(target, healing.amount))
    if delta.operations[1:] != tuple(edits):
        raise ValueError("charged item cast HP delta must match the healing evidence")
