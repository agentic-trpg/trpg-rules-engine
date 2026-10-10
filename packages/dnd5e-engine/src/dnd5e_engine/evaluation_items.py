"""Single-use owned inventory units through shared item payment and healing."""

import random
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

from dnd5e_engine import action_policy
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.evaluation_context import execution_context
from dnd5e_engine.evaluation_contracts import (
    CombatIntentPayload,
    ItemUsePayload,
    RuleError,
    RuleEvaluationRequest,
)
from dnd5e_engine.evaluation_delta import InventoryConsume, StateDelta
from dnd5e_engine.evaluation_preflight import snapshot_support_failure, template_support_failure
from dnd5e_engine.evaluation_projection import EvaluationInvariantError, attack_delta
from dnd5e_engine.evaluation_rng import RNGState, RNGTransition
from dnd5e_engine.evaluation_snapshot import capture_evaluation_snapshot
from dnd5e_engine.evaluation_state import CharacterStateV2, InventoryCombatSnapshot
from dnd5e_engine.events import AttackFailed, CastFailed, CombatEvent, HealingApplied
from dnd5e_engine.lib_loader import scoped_lib_loader
from dnd5e_engine.specs import GridScene


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
    snapshot: InventoryCombatSnapshot, actor_id: str, item: Item, loader: AssetLoader
) -> ItemEvaluation | None:
    for actor in snapshot.character_states:
        for slug in actor.carried_item_slugs:
            equipment = loader.get_item(slug)
            if equipment is None or equipment.passive_effects or equipment.requires_attunement:
                return _refuse(
                    "unsupported", "item.equipment", "equipment passive effects require migration"
                )
    counter_key = orch._item_use_counter_key(item.slug)
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
    if not isinstance(snapshot, InventoryCombatSnapshot) or not isinstance(payload, ItemUsePayload):
        raise EvaluationInvariantError("item evaluation requires the inventory contract")
    view = snapshot.combat_view()
    entry = next(
        (e for e in snapshot.inventory_state if e.instance_id == payload.instance_id), None
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
    counter_key = orch._item_use_counter_key(item.slug)
    intent = CombatIntentPayload(
        intent_type="use_item",
        item_id=item.slug,
        activity_id=payload.activity_id,
        target_id=payload.target_id,
    )
    with scoped_lib_loader(loader):
        live = execution_context(view, loader)
        try:
            current = orch._validate_intent_preconditions(
                live, orch.CombatHandle(live.handle_id), request.actor_id, intent=intent
            )
            action_policy.validate_grant(live, current, intent)
            cost = orch._classify_action_cost(intent, None)
            funding = orch._classify_attack_funding(current, intent, None, repeats_construct=False)
            funding = action_policy.preflight_intent_policy(
                live, current, intent, cost, funding, None
            )
        except orch.IntentRejectedError as error:
            return _refuse("rejected", error.reason, str(error))
        failure = orch._intent_pre_resolution_failure(
            live, current, intent, None, None, funding, cost
        )
        if failure is not None:
            if not isinstance(failure, (AttackFailed, CastFailed)):
                raise EvaluationInvariantError("unexpected item refusal event")
            return _refuse("rejected", str(failure.reason), "shared item preflight refused payment")
        if orch._item_charge_gate(live, request.actor_id, intent):
            return _refuse("rejected", "item_exhausted", "shared item charge gate refused payment")
        grid = GridScene.model_validate(snapshot.scene_state.grid.model_dump())
        captured = capture_evaluation_snapshot(live, view, grid)
        if captured != view or RNGState.capture(live.rng) != RNGState.capture(random.Random(0)):
            raise EvaluationInvariantError("item preflight changed state or RNG")
        # A private fresh-unit pool is derived from the explicit inventory balance.
        live.rng = request.rng_context.state.restore()
        await orch._submit_live_intent(
            live, orch.CombatHandle(live.handle_id), request.actor_id, intent
        )
        action_policy.record_budget_changes(live, current)
        counters = live.custom_counters_by_entity[request.actor_id]
        if counters.get(counter_key) != {"spent": 1}:
            raise EvaluationInvariantError("item did not pay exactly one private unit charge")
        del counters[counter_key]
        after = capture_evaluation_snapshot(live, view, grid)
        proposals = after.combat_state.event_log[len(view.combat_state.event_log) :]
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
        rng_transition=RNGTransition.between(request.rng_context, live.rng),
        choice=None,
        error=None,
    )
