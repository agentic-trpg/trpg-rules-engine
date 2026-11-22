"""Evaluate a bound retained item casting a reviewed instant exploration heal."""

import re
from dataclasses import dataclass

from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.common import (
    ActivationBlock,
    CastActivity,
    CastSpellBlock,
    ConsumptionBlock,
    ConsumptionTargetEntry,
)
from dnd5e_srd_data.schema.feature import RecoveryRule
from dnd5e_srd_data.schema.item import Item
from dnd5e_srd_data.schema.spell import Spell

from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.resolver import resolve_activity
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
    """Select Cure Wounds from the bundled staff's verified activities and scaling."""
    cure_wounds = loader.get_spell("cure-wounds")
    if (
        cure_wounds is None
        or cure_wounds != get_default_ruleset().get_spell("cure-wounds")
        or exploration_healing_activity(loader, cure_wounds) is None
        or (cast_level is not None and not 1 <= cast_level <= 4)
    ):
        return None
    activity = next(
        activity
        for activity in item.activities
        if isinstance(activity, CastActivity) and activity.spell.uuid == cure_wounds.foundry_uuid
    )
    if activity_id not in (None, activity.id):
        return None
    level = cast_level or 1
    assert item.uses is not None
    return ExplorationChargedCastProfile(
        activity, cure_wounds, level, int(item.uses.max), True, level, True
    )


def exploration_charged_cast_profile(
    item: Item,
    loader: AssetLoader,
    *,
    activity_id: str | None = None,
    cast_level: int | None = None,
) -> ExplorationChargedCastProfile | None:
    """Identify a supported charged touch heal from the bound item and activity.

    A canonical Staff of Healing delegates to Cure Wounds at levels 1–4.
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
    if staff and request.schema_version != STAFF_CAST_SCHEMA_VERSION:
        return _refuse("unsupported", "item.capability", "Staff of Healing requires evaluation/41")
    profile = exploration_charged_cast_profile(
        item, loader, activity_id=payload.activity_id, cast_level=payload.cast_level
    )
    if profile is None:
        if staff and payload.activity_id not in {activity.id for activity in item.activities}:
            return _refuse("rejected", "activity_invalid", "activity is absent from this item")
        if staff and payload.cast_level is not None and payload.cast_level > 4:
            return _refuse("rejected", "cast_level_invalid", "Cure Wounds supports levels 1–4")
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
    target = actors.get(payload.target_id)
    if (
        target is None
        or not target.is_alive
        or target.hp_current <= 0
        or (
            target.entity_id != actor.entity_id
            and not exploration_spell_reaches(request, snapshot, profile.spell)
        )
    ):
        return _refuse("rejected", "target_invalid", "cast requires a living creature within touch")
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
    if any(a.trait_mechanics or a.concentration_effect_id for a in actors.values()) or any(
        not effect.disabled for effect in snapshot.effect_states
    ):
        return _refuse("unsupported", "item.snapshot", "unreviewed trait or effect")
    canonical = get_default_ruleset()
    if class_failure := class_dependencies(
        actor,
        snapshot,
        loader,
        canonical,
        equipped=True,
        stowed_consumable_ids=ignored_items,
        reviewed_lore_bard=staff,
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
            target_id=target.entity_id,
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
        [caster if actor.entity_id == target.entity_id else combatant(target)],
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
    resolve_activity(activity, context)
    if (
        len(events) != 3
        or not isinstance(healing := events[2], HealingApplied)
        or healing.target_id != target.entity_id
    ):
        raise EvaluationInvariantError("charged exploration cast must produce one healing event")
    if healing.amount < 0:
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
            operations=(payment, *exploration_healing_hp_delta(target, healing.amount)),
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
    if result.rng_transition is None or not result.rng_transition.state_changed:
        raise ValueError("charged healing requires a recorded RNG advance")
    staff = request.schema_version == STAFF_CAST_SCHEMA_VERSION
    last_charge = staff and spend.value.charges_remaining_per_unit == 0
    if len(result.proposed_events) != 3 + int(last_charge):
        raise ValueError("charged cast requires the item intent, cast and healing evidence")
    intent, cast, healing = result.proposed_events[:3]
    if last_charge:
        depletion = result.proposed_events[3]
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
        or cast.item_cast_level != ((payload.cast_level or 1) if staff else None)
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
        )
        or not isinstance(healing, HealingApplied)
        or healing.target_id != payload.target_id
        or healing.amount < 0
    ):
        raise ValueError("charged item cast events must match the request")
    target = next(
        (actor for actor in snapshot.character_states if actor.entity_id == payload.target_id),
        None,
    )
    if target is None or not target.is_alive or target.hp_current <= 0:
        raise ValueError("charged item cast requires a living target")
    if delta.operations[1:] != exploration_healing_hp_delta(target, healing.amount):
        raise ValueError("charged item cast HP delta must match the healing evidence")
