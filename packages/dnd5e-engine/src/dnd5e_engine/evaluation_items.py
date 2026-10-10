"""Single-use owned inventory units through shared item payment and healing."""

import re
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
from dnd5e_engine.evaluation_computation import CombatComputation
from dnd5e_engine.evaluation_contracts import (
    CombatIntentPayload,
    ItemUsePayload,
    RuleError,
    RuleEvaluationRequest,
)
from dnd5e_engine.evaluation_delta import InventoryConsume, StateDelta
from dnd5e_engine.evaluation_preflight import snapshot_support_failure, template_support_failure
from dnd5e_engine.evaluation_projection import EvaluationInvariantError, attack_delta
from dnd5e_engine.evaluation_rng import RNGTransition
from dnd5e_engine.evaluation_state import CharacterStateV2, CombatSnapshot
from dnd5e_engine.events import (
    AttackFailed,
    CastFailed,
    CombatEvent,
    HealingApplied,
    IntentSubmitted,
)
from dnd5e_engine.item_rules import activity_item_use_cost, item_use_counter_key
from dnd5e_engine.lib_loader import scoped_lib_loader
from dnd5e_engine.turn_rules import bonus_action_payment, record_budget_changes


class ItemEvaluation(TypedDict):
    status: Literal["accepted", "rejected", "unsupported"]
    state_delta: StateDelta | None
    proposed_events: tuple[CombatEvent, ...]
    rng_transition: RNGTransition | None
    choice: None
    error: RuleError | None


def _refuse(status: Literal["rejected", "unsupported"], code: str, reason: str) -> ItemEvaluation:
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


def _equipment_failure(
    snapshot: CombatSnapshot, actor_id: str, item: Item, loader: AssetLoader
) -> ItemEvaluation | None:
    for actor in snapshot.character_states:
        for slug in actor.carried_item_slugs:
            equipment = loader.get_item(slug)
            if equipment is None or equipment.passive_effects or equipment.requires_attunement:
                return _refuse(
                    "unsupported", "item.equipment", "equipment passive effects require migration"
                )
    counter_key = item_use_counter_key(item.slug)
    actor_state = next(a for a in snapshot.character_states if a.entity_id == actor_id)
    if not isinstance(actor_state, CharacterStateV2):
        raise EvaluationInvariantError("inventory actor lacks explicit equipment")
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


async def evaluate_item(request: RuleEvaluationRequest, loader: AssetLoader) -> ItemEvaluation:
    snapshot, payload = request.state_snapshot, request.payload
    if not isinstance(snapshot, CombatSnapshot) or not isinstance(payload, ItemUsePayload):
        raise EvaluationInvariantError("item evaluation requires the inventory contract")
    view = snapshot
    entry = next(
        (e for e in snapshot.inventory_state.entries if e.instance_id == payload.instance_id), None
    )
    if entry is None:
        return _refuse("rejected", "item_missing", "inventory instance is absent")
    if entry.owner_id != request.actor_id or not entry.accessible:
        return _refuse("rejected", "item_unauthorized", "item must be owned and accessible")
    if entry.quantity == 0 or entry.charges_remaining_per_unit == 0:
        return _refuse("rejected", "item_exhausted", "no usable inventory unit remains")
    if entry.charges_remaining_per_unit != 1:
        return _refuse("unsupported", "item.charges", "only single-use units are migrated")
    if payload.target_id != request.actor_id:
        return _refuse("rejected", "target_invalid", "reviewed activity requires the drinker")
    support = snapshot_support_failure(view) or template_support_failure(view, loader)
    if support:
        return _refuse("unsupported", "item.snapshot", support)
    item = loader.get_item(entry.item_slug)
    if item is None:
        return _refuse("unsupported", "item.capability", "inventory asset is absent")
    support = _item_support_failure(item)
    if support:
        return _refuse("unsupported", "item.capability", support)
    if payload.activity_id != item.activities[0].id:
        return _refuse("rejected", "activity_invalid", "activity is absent from this item")
    equipment_failure = _equipment_failure(snapshot, request.actor_id, item, loader)
    if equipment_failure is not None:
        return equipment_failure
    state = snapshot.combat_state
    if state.ended:
        return _refuse("rejected", "combat_ended", "combat has ended")
    if request.actor_id != state.initiative_ids[state.current_turn_index]:
        return _refuse("rejected", "not_actor_turn", "actor does not own the current turn")
    actor_state = next(a for a in snapshot.character_states if a.entity_id == request.actor_id)
    if (
        not actor_state.is_alive
        or actor_state.hp_current <= 0
        or request.actor_id in state.dead_ids
    ):
        return _refuse("rejected", "actor_incapacitated", "drinker must be alive")
    from dnd5e_engine.evaluation_actor import combatant

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
    with scoped_lib_loader(loader):
        context = build_activity_context(
            computation.actors[current.entity_id],
            [current],
            rng=rng,
            event_emitter=computation.emit,
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
    computation.finish_turn(current.entity_id, allow_movement=True)
    computation.actors[current.entity_id] = record_budget_changes(
        current, computation.actors[current.entity_id]
    )
    after = computation.result()
    proposals = tuple(computation.events)
    if sum(isinstance(e, HealingApplied) for e in proposals) != 1:
        raise EvaluationInvariantError("item did not produce exactly one healing result")
    mechanical = attack_delta(view, after)
    consume = InventoryConsume(
        kind="inventory.consume",
        expected=entry,
        value=entry.model_copy(update={"quantity": entry.quantity - 1}),
        amount=1,
    )
    return dict(
        status="accepted",
        state_delta=StateDelta(
            expected_world_version=snapshot.world_version,
            operations=(consume, *mechanical.operations),
        ),
        proposed_events=proposals,
        rng_transition=RNGTransition.between(request.rng_context, rng),
        choice=None,
        error=None,
    )
