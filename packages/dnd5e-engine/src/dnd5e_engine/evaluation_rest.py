"""Bounded Short Rest: existing resolver, typed owned pools and no world mutation."""

from dnd5e_srd_data import BundledAssetLoader
from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.feature import RecoveryRule

from dnd5e_engine.damage_rules import healing_balance
from dnd5e_engine.evaluation_class_dependencies import class_dependencies, evoker_grants
from dnd5e_engine.evaluation_contracts import RestPayload, RuleEvaluationRequest
from dnd5e_engine.evaluation_delta import HPDelta, ResourceUpdate, StateDelta, StateDeltaOperation
from dnd5e_engine.evaluation_resources import (
    FeatureResource,
    HitDiceResource,
    ResourceState,
    SlotResource,
)
from dnd5e_engine.evaluation_rng import RNGTransition
from dnd5e_engine.evaluation_state import CharacterStateV2, InventoryState, NonCombatSnapshot
from dnd5e_engine.evaluation_turn import TurnEvaluation, _refuse
from dnd5e_engine.events import CombatEvent, HealingApplied, RestResolved, RestSpend
from dnd5e_engine.rest import (
    HitDicePool,
    recover_feature_uses,
    resolve_long_rest,
    resolve_short_rest,
)
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
    actor = next((a for a in snapshot.character_states if a.entity_id == request.actor_id), None)
    if actor is None or not actor.is_alive or actor.hp_current <= 0:
        return _refuse("rejected", "actor_invalid", "rest actor is absent, dead or dying")
    if snapshot.resource_state is None:
        return _refuse(
            "unsupported", "rest.resources", "explicit owned resource capacities are required"
        )
    levels = actor.classes or (
        {actor.class_slug: actor.character_level} if actor.class_slug else {}
    )
    # Retain R16/R19's previously admitted explicit recovery-only profiles.
    # Complete R20/R21 builds use the stricter canonical grant/pool closure.
    reviewed = levels in ({"cleric": 1}, {"fighter": 1}, {"wizard": 5}) and bool(
        set(actor.granted_features or ())
        - {"second-wind", "spellcasting", "ritual-adept", "arcane-recovery"}
        or actor.subclass_slug
        or actor.fighting_styles
    )
    failure = (
        class_dependencies(actor, snapshot, loader, BundledAssetLoader()) if reviewed else None
    )
    if (
        failure
        or snapshot.effect_states
        or actor.conditions
        or actor.concentration_effect_id
        or actor.trait_mechanics
        or actor.species_slug
        or (actor.subclass_slug and not reviewed)
        or actor.granted_features is None
        or (
            not reviewed
            and set(actor.granted_features or ())
            - (
                {"second-wind", "spellcasting", "ritual-adept", "arcane-recovery"}
                if payload.rest_type == "long"
                else {"second-wind"}
            )
        )
        or (actor.fighting_styles and not reviewed)
        or any(k.startswith("item_use:") for k in actor.custom_counters)
    ):
        return _refuse(
            "unsupported",
            "rest.dependencies",
            failure or "feature/item/effect recovery needs its own complete lifecycle",
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
    finite = {
        slug
        for slug in actor.granted_features or ()
        if (f := loader.get_feature(slug)) is not None and f.uses is not None and f.uses.max
    }
    if payload.rest_type == "short" and not reviewed:
        finite = set(actor.granted_features or ())
    if {p.feature_slug for p in feature_pools} != finite:
        return _refuse(
            "unsupported", "rest.features", "owned feature recovery closure is incomplete"
        )
    feature_recovery = {}
    for feature_pool in feature_pools:
        feature = loader.get_feature(feature_pool.feature_slug)
        if (
            feature_pool.feature_slug
            not in (
                {"second-wind", "arcane-recovery"}
                if payload.rest_type == "long"
                else {"second-wind"}
            )
            or feature is None
            or feature.uses is None
        ):
            return _refuse(
                "unsupported", "rest.features", "only reviewed Second Wind recovery is migrated"
            )
        short = [
            r
            for r in feature.uses.recovery
            if r.period == ("lr" if payload.rest_type == "long" else "sr")
        ]
        rule_ok = len(short) == 1 and (
            (short[0].type == "recoverAll" and short[0].formula == "")
            if payload.rest_type == "long"
            else (short[0].type == "formula" and short[0].formula == "1")
        )
        if not rule_ok:
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
    if (
        any(p.feature_slug == "second-wind" for p in feature_pools)
        and classes.get("fighter", 0) <= 0
    ):
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
    if payload.rest_type == "long":
        if payload.hit_dice:
            return _refuse("rejected", "rest.long_spend", "Long Rest does not spend Hit Dice")
        failure = _long_rest_failure(
            actor, snapshot.resource_state, classes, loader, snapshot.inventory_state
        )
        if failure:
            return _refuse("unsupported", "rest.long_dependencies", failure)
        return _compute_long_rest(request, actor, hit_dice, pact, feature_pools, feature_recovery)
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
            hit_dice_restored={},
            spell_slots_restored={},
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


def _compute_long_rest(
    request: RuleEvaluationRequest,
    actor: CharacterStateV2,
    hit_dice: dict[str, HitDiceResource],
    pact: list[SlotResource],
    feature_pools: list[FeatureResource],
    feature_recovery: dict[str, list[RecoveryRule]],
) -> TurnEvaluation:
    snapshot = request.state_snapshot
    assert snapshot.resource_state is not None
    spell = [
        p
        for p in snapshot.resource_state.pools
        if isinstance(p, SlotResource) and p.owner_id == actor.entity_id and p.kind == "spell_slot"
    ]
    outcomes = {
        pid: resolve_long_rest(
            HitDicePool(hit_die_size=p.die_size, dice_remaining=p.current, dice_total=p.maximum),
            actor.hp_current,
            actor.hp_max,
            spell_slots={s.level: s.current for s in spell},
            spell_slot_max={s.level: s.maximum for s in spell},
            pact_slots={s.level: s.current for s in pact},
            pact_slot_max={s.level: s.maximum for s in pact},
        )
        for pid, p in hit_dice.items()
    }
    first = next(iter(outcomes.values()))
    recovered = recover_feature_uses(actor.custom_counters, "lr", feature_recovery)
    operations: list[StateDeltaOperation] = []
    for pool in snapshot.resource_state.pools:
        if pool.owner_id != actor.entity_id:
            continue
        if isinstance(pool, HitDiceResource):
            current = outcomes[pool.pool_id].dice_remaining
        elif isinstance(pool, SlotResource):
            slots = first.spell_slots if pool.kind == "spell_slot" else first.pact_slots
            assert slots is not None
            current = slots[pool.level]
        else:
            current = pool.maximum - recovered[pool.feature_slug]
        if current != pool.current:
            operations.append(
                ResourceUpdate(
                    kind="resource.update",
                    owner_id=actor.entity_id,
                    pool_id=pool.pool_id,
                    expected=pool,
                    value=pool.model_copy(update={"current": current}),
                    amount=current - pool.current,
                )
            )
    healed = first.healed
    if healed:
        assert first.hp_current is not None
        operations.append(
            HPDelta(
                kind="actor.hp_delta",
                target_id=actor.entity_id,
                expected_hp=actor.hp_current,
                amount=healed,
                resulting_hp=first.hp_current,
            )
        )
    events: list[CombatEvent] = [
        RestResolved(
            actor_id=actor.entity_id,
            rest_type="long",
            hit_dice=(),
            hit_dice_restored={
                p.pool_id: outcomes[p.pool_id].dice_remaining - p.current for p in hit_dice.values()
            },
            spell_slots_restored={p.level: p.maximum - p.current for p in spell},
            pact_slots_restored={p.level: p.maximum - p.current for p in pact},
            feature_uses_restored={
                p.feature_slug: p.maximum - recovered[p.feature_slug] - p.current
                for p in feature_pools
            },
            hp_regained=healed,
        )
    ]
    if healed:
        events.append(HealingApplied(target_id=actor.entity_id, amount=healed))
    # This reviewed recovery subset draws no dice and does not restore an execution RNG.
    context = request.rng_context
    return dict(
        status="accepted",
        state_delta=StateDelta(
            expected_world_version=snapshot.world_version, operations=tuple(operations)
        ),
        proposed_events=tuple(events),
        rng_transition=RNGTransition(
            stream_id=context.stream_id,
            input_version=context.version,
            input_state=context.state,
            next_state=context.state,
            state_changed=False,
        ),
        choice=None,
        error=None,
    )


def _long_rest_failure(
    actor: CharacterStateV2,
    resources: ResourceState,
    classes: dict[str, int],
    loader: AssetLoader,
    inventory: InventoryState,
) -> str | None:
    from dnd5e_engine.activities.actor_stats import _SCORE_ATTR
    from dnd5e_engine.activities.context import ActivityResolutionContext
    from dnd5e_engine.activities.scale import build_scale_values
    from dnd5e_engine.build_spec import derive_multiclass_pact_slots, derive_multiclass_slots
    from dnd5e_engine.evaluation_actor import combatant
    from dnd5e_engine.feature_runtime import DrawFreeRandom, scalar_formula

    for entry in inventory.entries:
        if entry.owner_id == actor.entity_id:
            item = loader.get_item(entry.item_slug)
            if (
                item is None
                or item.passive_effects
                or item.requires_attunement
                or (item.uses is not None and item.uses.recovery)
            ):
                return "owned item recovery/passive closure is not migrated"
    canonical = BundledAssetLoader()
    granted: set[str] = set()
    for slug, level in classes.items():
        definition = loader.get_class(slug)
        if definition is None or definition != canonical.get_class(slug) or not 1 <= level <= 20:
            return "unreviewed class recovery definition"
        granted.update(g.slug for g in definition.granted_features if g.level <= level)
    if actor.class_slug and actor.class_slug not in classes:
        return "primary class contradicts complete class levels"
    if any(not k.startswith("feature_use:") for k in actor.custom_counters):
        return "unreviewed rest counter dependency"
    if actor.subclass_slug:
        additional = evoker_grants(loader, canonical, actor.character_level)
        if additional is None:
            return "unreviewed subclass recovery definition"
        granted |= additional
    for slug in actor.granted_features or ():
        feature = loader.get_feature(slug)
        if feature is None or feature != canonical.get_feature(slug):
            return "unreviewed rest feature definition"
        if slug not in granted:
            return "feature is not granted by the explicit class levels"
    owned = [p for p in resources.pools if p.owner_id == actor.entity_id]
    for kind, maximum in (
        ("spell_slot", derive_multiclass_slots(classes, loader=loader)),
        ("pact_slot", derive_multiclass_pact_slots(classes, loader=loader)),
    ):
        actual = {
            p.level: p.maximum for p in owned if isinstance(p, SlotResource) and p.kind == kind
        }
        if actual != maximum:
            return "slot capacity closure differs from pinned class progression"
    context = ActivityResolutionContext(
        caster=combatant(actor),
        caster_abilities={ab: getattr(actor, field) for ab, field in _SCORE_ATTR.items()},
        targets=[],
        rng=DrawFreeRandom(0),
        event_emitter=lambda _: None,
        class_levels=classes,
        scale_values=build_scale_values(
            class_slug=actor.class_slug,
            subclass_slug=None,
            species_slug=None,
            level=actor.character_level,
            classes=classes,
            loader=loader,
        ),
    )
    for pool in owned:
        if isinstance(pool, FeatureResource):
            feature = loader.get_feature(pool.feature_slug)
            assert feature is not None
            assert feature.uses is not None
            if scalar_formula(feature.uses.max, context) != pool.maximum:
                return "feature maximum contradicts pinned class Scale/formula"
    return None
