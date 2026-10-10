"""Bounded Short Rest: existing resolver, typed owned pools and no world mutation."""

from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.feature import RecoveryRule

from dnd5e_engine.damage_rules import healing_balance
from dnd5e_engine.evaluation_contracts import RestPayload, RuleEvaluationRequest
from dnd5e_engine.evaluation_delta import HPDelta, ResourceUpdate, StateDelta, StateDeltaOperation
from dnd5e_engine.evaluation_resources import FeatureResource, HitDiceResource, SlotResource
from dnd5e_engine.evaluation_rng import RNGTransition
from dnd5e_engine.evaluation_state import CharacterStateV2, NonCombatSnapshot
from dnd5e_engine.evaluation_turn import TurnEvaluation, _refuse
from dnd5e_engine.events import CombatEvent, HealingApplied, RestResolved, RestSpend
from dnd5e_engine.rest import HitDicePool, recover_feature_uses, resolve_short_rest
from dnd5e_engine.rules.dice import ability_modifier


def evaluate_rest(request: RuleEvaluationRequest, loader: AssetLoader) -> TurnEvaluation:
    snapshot, payload = request.state_snapshot, request.payload
    assert isinstance(payload, RestPayload)
    if not isinstance(snapshot, NonCombatSnapshot) or snapshot.combat_setup is not None:
        return _refuse(
            "unsupported",
            "rest.snapshot",
            "rest requires a noncombat snapshot without pending creation",
        )
    if payload.rest_type != "short":
        return _refuse(
            "unsupported", "rest.type", "Long Rest recovery dependency closure is not migrated"
        )
    actor = next((a for a in snapshot.character_states if a.entity_id == request.actor_id), None)
    if actor is None or not actor.is_alive or actor.hp_current <= 0:
        return _refuse("rejected", "actor_invalid", "rest actor is absent, dead or dying")
    if snapshot.resource_state is None:
        return _refuse(
            "unsupported", "rest.resources", "explicit owned resource capacities are required"
        )
    if (
        snapshot.effect_states
        or actor.conditions
        or actor.concentration_effect_id
        or actor.trait_mechanics
        or actor.species_slug
        or actor.subclass_slug
        or actor.granted_features is None
        or set(actor.granted_features or ()) - {"second-wind"}
        or actor.fighting_styles
        or any(k.startswith("item_use:") for k in actor.custom_counters)
    ):
        return _refuse(
            "unsupported",
            "rest.dependencies",
            "feature/item/effect recovery needs its own complete lifecycle",
        )
    if not 1 <= actor.constitution <= 30 or not 1 <= actor.character_level <= 20:
        return _refuse("unsupported", "rest.constitution", "invalid Constitution fact")
    for slug in actor.carried_item_slugs:
        item = loader.get_item(slug)
        if item is None or item.passive_effects or item.requires_attunement:
            return _refuse(
                "unsupported", "rest.equipment", "equipment rest dependencies are not migrated"
            )
    pools = [p for p in snapshot.resource_state.pools if p.owner_id == actor.entity_id]
    feature_pools = [p for p in pools if isinstance(p, FeatureResource)]
    if {p.feature_slug for p in feature_pools} != set(actor.granted_features or ()):
        return _refuse(
            "unsupported", "rest.features", "owned feature recovery closure is incomplete"
        )
    feature_recovery = {}
    for feature_pool in feature_pools:
        feature = loader.get_feature(feature_pool.feature_slug)
        if feature_pool.feature_slug != "second-wind" or feature is None or feature.uses is None:
            return _refuse(
                "unsupported", "rest.features", "only reviewed Second Wind recovery is migrated"
            )
        short = [r for r in feature.uses.recovery if r.period == "sr"]
        if len(short) != 1 or short[0].type != "formula" or short[0].formula != "1":
            return _refuse(
                "unsupported",
                "rest.feature_rule",
                "feature recovery differs from the reviewed typed rule",
            )
        feature_recovery[feature_pool.feature_slug] = feature.uses.recovery
    hit_dice = {p.pool_id: p for p in pools if isinstance(p, HitDiceResource)}
    classes = actor.classes or (
        {actor.class_slug: actor.character_level} if actor.class_slug else {}
    )
    if not hit_dice or {p.class_slug: p.maximum for p in hit_dice.values()} != classes:
        return _refuse(
            "unsupported",
            "rest.hit_dice",
            "complete class Hit Dice maxima and identities are required",
        )
    for pool in hit_dice.values():
        cls = loader.get_class(pool.class_slug)
        if cls is None or str(cls.hit_die) != f"d{pool.die_size}":
            return _refuse(
                "unsupported", "rest.hit_die_binding", "Hit Die size contradicts pinned class data"
            )
    if sum(classes.values()) != actor.character_level or any(n <= 0 for n in classes.values()):
        return _refuse(
            "unsupported", "rest.classes", "class levels do not close over character level"
        )
    if feature_pools and classes.get("fighter", 0) <= 0:
        return _refuse(
            "unsupported", "rest.feature_owner", "Second Wind requires its explicit Fighter owner"
        )
    for spend in payload.hit_dice:
        requested_pool = hit_dice.get(spend.pool_id)
        if requested_pool is None or spend.amount > requested_pool.current:
            return _refuse(
                "rejected", "rest.insufficient", "requested Hit Dice are absent or exhausted"
            )
    pact = [p for p in pools if isinstance(p, SlotResource) and p.kind == "pact_slot"]
    return _compute_rest(request, actor, hit_dice, pact, feature_pools, feature_recovery)


def _compute_rest(
    request: RuleEvaluationRequest,
    actor: CharacterStateV2,
    hit_dice: dict[str, HitDiceResource],
    pact: list[SlotResource],
    feature_pools: list[FeatureResource],
    feature_recovery: dict[str, list[RecoveryRule]],
) -> TurnEvaluation:
    snapshot, payload = request.state_snapshot, request.payload
    assert isinstance(payload, RestPayload)
    # All identity/capacity/cost validation completes before restoring RNG.
    rng = request.rng_context.state.restore()
    con = ability_modifier(actor.constitution)
    outcomes = []
    chosen = [(hit_dice[s.pool_id], s.amount) for s in payload.hit_dice]
    if not chosen:
        chosen = [(next(iter(hit_dice.values())), 0)]
    operations: list[StateDeltaOperation] = []
    spent = []
    for index, (pool, amount) in enumerate(chosen):
        outcome = resolve_short_rest(
            HitDicePool(
                hit_die_size=pool.die_size, dice_remaining=pool.current, dice_total=pool.maximum
            ),
            amount,
            con,
            rng=rng,
            pact_slots={p.level: p.current for p in pact} if index == 0 else None,
            pact_slot_max={p.level: p.maximum for p in pact} if index == 0 else None,
        )
        outcomes.append(outcome)
        if amount:
            value = pool.model_copy(update={"current": outcome.dice_remaining})
            operations.append(
                ResourceUpdate(
                    kind="resource.update",
                    owner_id=actor.entity_id,
                    pool_id=pool.pool_id,
                    expected=pool,
                    value=value,
                    amount=-amount,
                )
            )
            spent.append(
                RestSpend(
                    pool_id=pool.pool_id,
                    die_size=pool.die_size,
                    amount=amount,
                    healing_rolls=outcome.rolls,
                )
            )
    restored = outcomes[0].pact_slots
    assert restored is not None
    for slot in pact:
        if slot.current != restored[slot.level]:
            operations.append(
                ResourceUpdate(
                    kind="resource.update",
                    owner_id=actor.entity_id,
                    pool_id=slot.pool_id,
                    expected=slot,
                    value=slot.model_copy(update={"current": restored[slot.level]}),
                    amount=restored[slot.level] - slot.current,
                )
            )
    feature_spent = recover_feature_uses(actor.custom_counters, "sr", feature_recovery, rng=rng)
    feature_restored = {}
    for feature_pool in feature_pools:
        remaining = feature_pool.maximum - feature_spent[feature_pool.feature_slug]
        feature_restored[feature_pool.feature_slug] = remaining - feature_pool.current
        if remaining != feature_pool.current:
            operations.append(
                ResourceUpdate(
                    kind="resource.update",
                    owner_id=actor.entity_id,
                    pool_id=feature_pool.pool_id,
                    expected=feature_pool,
                    value=feature_pool.model_copy(update={"current": remaining}),
                    amount=remaining - feature_pool.current,
                )
            )
    hp = healing_balance(actor.hp_current, actor.hp_max, sum(o.healed for o in outcomes))
    healed = hp - actor.hp_current
    if healed:
        operations.append(
            HPDelta(
                kind="actor.hp_delta",
                target_id=actor.entity_id,
                expected_hp=actor.hp_current,
                amount=healed,
                resulting_hp=hp,
            )
        )
    events: list[CombatEvent] = [
        RestResolved(
            actor_id=actor.entity_id,
            rest_type="short",
            hit_dice=tuple(spent),
            pact_slots_restored={p.level: restored[p.level] - p.current for p in pact},
            hp_regained=healed,
            feature_uses_restored=feature_restored,
        )
    ]
    if healed:
        events.append(HealingApplied(target_id=actor.entity_id, amount=healed))
    return dict(
        status="accepted",
        state_delta=StateDelta(
            expected_world_version=snapshot.world_version, operations=tuple(operations)
        ),
        proposed_events=tuple(events),
        rng_transition=RNGTransition.between(request.rng_context, rng),
        choice=None,
        error=None,
    )
