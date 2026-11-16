"""Single-use owned inventory units through shared item payment and healing."""

import re
from collections.abc import Callable, Iterable
from random import Random
from typing import Literal, TypedDict

from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.common import (
    ActivationBlock,
    ConsumptionBlock,
    ConsumptionTargetEntry,
    DamageScalingBlock,
    HealActivity,
    RangeBlock,
    TargetAffectsBlock,
    TargetBlock,
    VisibilityBlock,
)
from dnd5e_srd_data.schema.item import Item, ItemUses

from dnd5e_engine.action_economy_rules import action_economy_gate_failure
from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.resolver import resolve_activity
from dnd5e_engine.damage_rules import healing_balance
from dnd5e_engine.evaluation_actor import combatant
from dnd5e_engine.evaluation_class_dependencies import class_dependencies
from dnd5e_engine.evaluation_computation import CombatComputation
from dnd5e_engine.evaluation_contracts import (
    CombatIntentPayload,
    ExplorationItemUsePayload,
    ItemUsePayload,
    RuleError,
    RuleEvaluationRequest,
)
from dnd5e_engine.evaluation_delta import HPDelta, InventoryConsume, StateDelta
from dnd5e_engine.evaluation_equipment_dependencies import reviewed_healing_potion_owners
from dnd5e_engine.evaluation_preflight import snapshot_support_failure, template_support_failure
from dnd5e_engine.evaluation_projection import EvaluationInvariantError, attack_delta
from dnd5e_engine.evaluation_rng import RNGTransition
from dnd5e_engine.evaluation_ruleset import get_default_ruleset
from dnd5e_engine.evaluation_state import (
    CharacterStateV2,
    CombatSnapshot,
    InventoryEntry,
    NonCombatSnapshot,
    StateSnapshot,
)
from dnd5e_engine.events import (
    AttackFailed,
    CastFailed,
    CombatEvent,
    HealingApplied,
    IntentSubmitted,
)
from dnd5e_engine.item_rules import activity_item_use_cost, item_use_counter_key
from dnd5e_engine.lib_loader import scoped_lib_loader
from dnd5e_engine.rules.conditions import conditions_block_actions
from dnd5e_engine.turn_rules import bonus_action_payment, record_budget_changes
from dnd5e_engine.types.combat import Combatant


class ItemEvaluation(TypedDict):
    """Describe uncommitted item result fields returned to the evaluation envelope."""

    status: Literal["accepted", "rejected", "unsupported"]
    state_delta: StateDelta | None
    proposed_events: tuple[CombatEvent, ...]
    rng_transition: RNGTransition | None
    choice: None
    error: RuleError | None


def _refuse(status: Literal["rejected", "unsupported"], code: str, reason: str) -> ItemEvaluation:
    """Build a refusal with no Delta, proposed events or RNG transition."""
    return dict(
        status=status,
        state_delta=None,
        proposed_events=(),
        rng_transition=None,
        choice=None,
        error=RuleError(code=code, reason=reason),
    )


def _item_support_failure(item: Item) -> str | None:
    # Explicit reviewed identity; execution semantics still come from typed fields.
    """Return a reason unless the item fits the reviewed single-use healing profile."""
    if item.slug != "potion-of-healing" or item.item_kind != "item":
        return "only the reviewed single-use healing item is admitted"
    if (
        item.requires_attunement
        or item.attunement_constraint
        or item.passive_effects
        or (item.uses != ItemUses(max="1", spent=0, auto_destroy=True, recovery=[]))
    ):
        return "item attunement, passive effects or non-single-use pools are not migrated"
    if len(item.activities) != 1 or not isinstance(item.activities[0], HealActivity):
        return "item must contain exactly one typed healing activity"
    activity = item.activities[0]
    basic = HealActivity(
        id=activity.id,
        activation=ActivationBlock(type="bonus", value=activity.activation.value),
        consumption=ConsumptionBlock(targets=[ConsumptionTargetEntry(type="itemUses", value="1")]),
        range=RangeBlock(units="self"),
        target=TargetBlock(affects=TargetAffectsBlock(type="creature")),
        visibility=VisibilityBlock(require_magic=True),
        healing=activity.healing,
    )
    metadata = {"id", "name", "img", "sort", "description"}
    if activity.activation.value not in (None, 1) or any(
        getattr(activity, field) != getattr(basic, field)
        for field in type(activity).model_fields
        if field not in metadata
    ):
        return "activity costs, targeting, timing, effects or other sidecars are not migrated"
    healing = activity.healing
    if (
        healing.types != ["healing"]
        or healing.number is None
        or not 1 <= healing.number <= 20
        or healing.denomination not in (4, 6, 8, 10, 12)
        or healing.custom.enabled
        or healing.custom.formula
        or healing.scaling != DamageScalingBlock()
        or (healing.bonus and re.fullmatch(r"[0-9]+", healing.bonus) is None)
    ):
        return "healing must use ordinary fixed dice without scaling or custom formulas"
    return None


def _carried_equipment_failure(slugs: Iterable[str], loader: AssetLoader) -> ItemEvaluation | None:
    """Refuse carried assets whose passive effects or attunement are outside the profile."""
    for slug in slugs:
        equipment = loader.get_item(slug)
        if equipment is None or equipment.passive_effects or equipment.requires_attunement:
            return _refuse(
                "unsupported", "item.equipment", "equipment passive effects require migration"
            )
    return None


def _equipment_failure(actor_state: CharacterStateV2, item: Item) -> ItemEvaluation | None:
    """Check the drinker's charge authority and declared free hand."""
    counter_key = item_use_counter_key(item.slug)
    if counter_key in actor_state.custom_counters:
        return _refuse(
            "unsupported",
            "item.charge_authority",
            "inventory units cannot share a persistent slug charge pool",
        )
    if (
        actor_state.weapon_grip == "two_handed"
        or actor_state.other_hand_occupied
        or actor_state.shield_equipped
    ):
        return _refuse(
            "rejected", "item_hand_unavailable", "drinking requires a declared free hand"
        )
    return None


def _selected_unit(
    snapshot: StateSnapshot, actor_id: str, payload: ItemUsePayload | ExplorationItemUsePayload
) -> InventoryEntry | ItemEvaluation:
    """Select one exact usable, accessible inventory entry owned by its self-target."""
    entry = next(
        (e for e in snapshot.inventory_state.entries if e.instance_id == payload.instance_id), None
    )
    if entry is None:
        return _refuse("rejected", "item_missing", "inventory instance is absent")
    if entry.owner_id != actor_id or not entry.accessible:
        return _refuse("rejected", "item_unauthorized", "item must be owned and accessible")
    if entry.quantity == 0 or entry.charges_remaining_per_unit == 0:
        return _refuse("rejected", "item_exhausted", "no usable inventory unit remains")
    if entry.charges_remaining_per_unit != 1:
        return _refuse("unsupported", "item.charges", "only single-use units are migrated")
    if payload.target_id != actor_id:
        return _refuse("rejected", "target_invalid", "reviewed activity requires the drinker")
    return entry


def _item_for_activity(
    entry: InventoryEntry, activity_id: str, loader: AssetLoader
) -> Item | ItemEvaluation:
    """Bind the selected entry's typed healing activity to the requested identity."""
    item = loader.get_item(entry.item_slug)
    if item is None:
        return _refuse("unsupported", "item.capability", "inventory asset is absent")
    support = _item_support_failure(item)
    if support:
        return _refuse("unsupported", "item.capability", support)
    if activity_id != item.activities[0].id:
        return _refuse("rejected", "activity_invalid", "activity is absent from this item")
    return item


def _resolve_drink(
    item: Item,
    actor: Combatant,
    rng: Random,
    loader: AssetLoader,
    emit: Callable[[CombatEvent], None],
) -> None:
    """Run the reviewed potion activity against one isolated self-target."""
    with scoped_lib_loader(loader):
        context = build_activity_context(
            actor,
            [actor],
            rng=rng,
            event_emitter=emit,
            slot_level=None,
            base_spell_level=None,
            spellcasting_ability=None,
            concentration=False,
            source_passive_effects=[],
            spell_book={},
            save_modifiers={},
            passive_damage_modifiers={},
        )
        resolve_activity(item.activities[0], context)


def _consume_unit(entry: InventoryEntry) -> InventoryConsume:
    """Propose a one-unit decrement guarded by the original inventory entry."""
    return InventoryConsume(
        kind="inventory.consume",
        expected=entry,
        value=entry.model_copy(update={"quantity": entry.quantity - 1}),
        amount=1,
    )


def _combat_drink_dependencies(snapshot: CombatSnapshot, loader: AssetLoader) -> str | None:
    """Admit reviewed class grants for healing and the existing shared turn path."""
    if snapshot.snapshot_schema_version != "engine-snapshot/20" or not any(
        actor.class_slug or actor.classes or actor.granted_features
        for actor in snapshot.character_states
    ):
        return snapshot_support_failure(snapshot) or template_support_failure(snapshot, loader)

    canonical = get_default_ruleset()
    potion_owners = reviewed_healing_potion_owners(snapshot, loader, canonical)

    def actor_dependencies(actor: CharacterStateV2) -> str | None:
        if actor.weapon_mastery_slugs:
            return "trained weapon mastery execution is not migrated"
        if (
            actor.trait_mechanics
            or actor.subclass_slug
            or actor.species_slug
            or actor.fighting_styles
        ):
            return "actor effects, features or extended attack budgets are not migrated"
        if actor.class_slug or actor.classes or actor.granted_features:
            return class_dependencies(
                actor,
                snapshot,
                loader,
                canonical,
                equipped=True,
                allow_stowed_healing_potions=actor.entity_id in potion_owners,
            )
        return None

    return snapshot_support_failure(
        snapshot, actor_dependencies=actor_dependencies
    ) or template_support_failure(snapshot, loader)


async def evaluate_item(request: RuleEvaluationRequest, loader: AssetLoader) -> ItemEvaluation:
    """Propose combat self-healing, exact item consumption and turn consequences.

    Check instance identity, ownership/access, quantity/charges, target, free hand,
    bound asset and supported snapshot/action prerequisites before drawing RNG.
    Refusal leaves all balances unchanged.
    """
    snapshot, payload = request.state_snapshot, request.payload
    if not isinstance(snapshot, CombatSnapshot) or not isinstance(payload, ItemUsePayload):
        raise EvaluationInvariantError("item evaluation requires the inventory contract")
    entry = _selected_unit(snapshot, request.actor_id, payload)
    if not isinstance(entry, InventoryEntry):
        return entry
    support = _combat_drink_dependencies(snapshot, loader)
    if support:
        return _refuse("unsupported", "item.snapshot", support)
    item = _item_for_activity(entry, payload.activity_id, loader)
    if not isinstance(item, Item):
        return item
    actor_state = next(a for a in snapshot.character_states if a.entity_id == request.actor_id)
    carried_slugs = (
        slug for actor in snapshot.character_states for slug in actor.carried_item_slugs
    )
    equipment_failure = _carried_equipment_failure(carried_slugs, loader) or _equipment_failure(
        actor_state, item
    )
    if equipment_failure is not None:
        return equipment_failure
    state = snapshot.combat_state
    if state.ended:
        return _refuse("rejected", "combat_ended", "combat has ended")
    if request.actor_id != state.initiative_ids[state.current_turn_index]:
        return _refuse("rejected", "not_actor_turn", "actor does not own the current turn")
    if (
        not actor_state.is_alive
        or actor_state.hp_current <= 0
        or request.actor_id in state.dead_ids
    ):
        return _refuse("rejected", "actor_incapacitated", "drinker must be alive")
    current = combatant(actor_state)
    intent = CombatIntentPayload(
        intent_type="use_item",
        item_id=item.slug,
        activity_id=payload.activity_id,
        target_id=payload.target_id,
    )
    failure = action_economy_gate_failure(
        current, intent, is_bonus_action=True, is_reaction_cast=False
    )
    if failure is not None:
        if not isinstance(failure, (AttackFailed, CastFailed)):
            raise EvaluationInvariantError("unexpected action gate event")
        return _refuse("rejected", str(failure.reason), "shared item preflight refused payment")
    charge = activity_item_use_cost(item.slug, item.activities[0])
    if charge != entry.charges_remaining_per_unit:
        raise EvaluationInvariantError("admitted item must pay exactly one inventory unit charge")
    rng = request.rng_context.state.restore()
    computation = CombatComputation(snapshot, rng)
    computation.emit(
        IntentSubmitted(
            actor_id=current.entity_id,
            intent_type="use_item",
            target_id=current.entity_id,
            item_id=item.slug,
        )
    )
    computation.actors[current.entity_id] = current.model_copy(update=bonus_action_payment())
    _resolve_drink(item, computation.actors[current.entity_id], rng, loader, computation.emit)
    computation.finish_turn(current.entity_id, allow_movement=True)
    computation.actors[current.entity_id] = record_budget_changes(
        current, computation.actors[current.entity_id]
    )
    computed = computation.result()
    after = computed.candidate_state
    proposals = computed.proposed_events
    if sum(isinstance(e, HealingApplied) for e in proposals) != 1:
        raise EvaluationInvariantError("item did not produce exactly one healing result")
    mechanical = attack_delta(snapshot, after)
    return dict(
        status="accepted",
        state_delta=StateDelta(
            expected_world_version=snapshot.world_version,
            operations=(_consume_unit(entry), *mechanical.operations),
        ),
        proposed_events=proposals,
        rng_transition=RNGTransition.between(request.rng_context, rng),
        choice=None,
        error=None,
    )


def evaluate_exploration_item(
    request: RuleEvaluationRequest, loader: AssetLoader
) -> ItemEvaluation:
    """Propose an exact potion use, capped HP gain and RNG transition during exploration.

    Admission completes before RNG restore. The shared activity runs on a local actor
    copy, and no action or turn accounting enters the proposal.
    """
    snapshot, payload = request.state_snapshot, request.payload
    if not isinstance(snapshot, NonCombatSnapshot) or not isinstance(
        payload, ExplorationItemUsePayload
    ):
        raise EvaluationInvariantError("exploration item evaluation requires a noncombat request")
    if snapshot.combat_setup is not None:
        return _refuse("unsupported", "item.snapshot", "exploration requires no pending combat")
    entry = _selected_unit(snapshot, request.actor_id, payload)
    if not isinstance(entry, InventoryEntry):
        return entry
    if entry.quantity != 1:
        return _refuse("unsupported", "item.quantity", "exploration requires one exact bottle")
    actor = next(a for a in snapshot.character_states if a.entity_id == request.actor_id)
    if (
        not actor.is_alive
        or actor.hp_current <= 0
        or conditions_block_actions([condition.condition for condition in actor.conditions])
    ):
        return _refuse("rejected", "actor_incapacitated", "drinker must be alive and able to act")
    item = _item_for_activity(entry, payload.activity_id, loader)
    if not isinstance(item, Item):
        return item
    if actor.hp_current >= actor.hp_max:
        return _refuse("rejected", "hp_full", "drinker is already at maximum HP")
    if (
        actor.trait_mechanics
        or actor.concentration_effect_id
        or any(
            effect.target_id == actor.entity_id and not effect.disabled
            for effect in snapshot.effect_states
        )
    ):
        return _refuse(
            "unsupported", "item.snapshot", "drinker has unreviewed healing dependencies"
        )
    if entry.instance_id == actor.held_weapon_instance_id:
        return _refuse("unsupported", "item.equipment", "selected potion is bound as a weapon")
    # Access controls selecting the bottle. Other owned units still supply dependencies.
    owned_slugs = dict.fromkeys(
        owned.item_slug
        for owned in snapshot.inventory_state.entries
        if owned.owner_id == actor.entity_id and owned.quantity > 0
    )
    if failure := _carried_equipment_failure(owned_slugs, loader) or _equipment_failure(
        actor, item
    ):
        return failure
    if activity_item_use_cost(item.slug, item.activities[0]) != entry.charges_remaining_per_unit:
        raise EvaluationInvariantError("admitted potion must pay exactly one inventory unit charge")

    rng = request.rng_context.state.restore()
    events: list[CombatEvent] = [
        IntentSubmitted(
            actor_id=actor.entity_id,
            intent_type="use_item",
            target_id=actor.entity_id,
            item_id=item.slug,
        )
    ]
    _resolve_drink(item, combatant(actor), rng, loader, events.append)
    if len(events) != 2 or not isinstance(healing := events[1], HealingApplied):
        raise EvaluationInvariantError("potion did not produce exactly one healing result")
    if healing.target_id != actor.entity_id or healing.amount <= 0:
        raise EvaluationInvariantError("potion must heal its own drinker")
    hp = healing_balance(actor.hp_current, actor.hp_max, healing.amount)
    return dict(
        status="accepted",
        state_delta=StateDelta(
            expected_world_version=snapshot.world_version,
            operations=(
                _consume_unit(entry),
                HPDelta(
                    kind="actor.hp_delta",
                    target_id=actor.entity_id,
                    expected_hp=actor.hp_current,
                    amount=hp - actor.hp_current,
                    resulting_hp=hp,
                ),
            ),
        ),
        proposed_events=tuple(events),
        rng_transition=RNGTransition.between(request.rng_context, rng),
        choice=None,
        error=None,
    )
