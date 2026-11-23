"""Bounded explicit saving throws over supplied actor facts and private RNG."""

import random
from collections.abc import Iterable
from functools import lru_cache
from hashlib import sha256
from json import dumps
from typing import Literal, get_args

from dnd5e_srd_data.loader import AssetLoader, BundledAssetLoader
from dnd5e_srd_data.schema.monster import MonsterTrait, MonsterTraitMechanic
from dnd5e_srd_data.schema.spell import Spell

from dnd5e_engine.activities.actor_stats import _SCORE_ATTR, save_modifier
from dnd5e_engine.activities.context import ActivityResolutionContext
from dnd5e_engine.activities.d20 import AdvantageSources
from dnd5e_engine.activities.effects import passive_effect_to_active_effect
from dnd5e_engine.activities.save_primitive import SaveBonusDie, SaveRoll, roll_save
from dnd5e_engine.evaluation_actor import combatant
from dnd5e_engine.evaluation_bardic_check import (
    BARDIC_INSPIRATION_DIE_SIDES,
    BARDIC_INSPIRATION_SOURCE,
)
from dnd5e_engine.evaluation_beacon import is_reviewed_beacon_effect
from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest, SavePayload
from dnd5e_engine.evaluation_delta import StateDelta
from dnd5e_engine.evaluation_effects import EffectState, effect_state
from dnd5e_engine.evaluation_rng import RNGContext, RNGTransition
from dnd5e_engine.evaluation_ruleset import verify_ruleset
from dnd5e_engine.evaluation_state import CharacterStateV2, NonCombatSnapshot
from dnd5e_engine.evaluation_turn import TurnEvaluation, _refuse
from dnd5e_engine.events import Ability, RollDieModifier, RollFixedModifier, SaveRolled
from dnd5e_engine.rules.conditions import (
    _DECLARATIVE_CONDITION_EFFECTS,
    CONDITION_IMPLIES,
    Condition,
    condition_matches_compiled,
    d20_test_penalty,
    project_passive_save_modifiers,
)

_SAVE_SPELL_DICE: dict[str, tuple[str, Literal[-1, 1]]] = {
    "effect:blessed": ("bless", 1),
    "effect:bane": ("bane", -1),
}
_MAGIC_RESISTANCE_SLUG = "magic-resistance"
type _PreparedSave = tuple[
    CharacterStateV2,
    SavePayload,
    list[EffectState],
    tuple[SaveBonusDie, ...],
]


def _held_inspiration(effect: EffectState, loader: AssetLoader) -> bool:
    """Recognize only an inert issued Inspired grant, not arbitrary save effects."""
    grant = effect.flags.get("bardic_inspiration")
    shape = bool(
        effect.id == "effect:inspired"
        and effect.origin.startswith("cast:inspired:")
        and effect.origin != "cast:inspired:"
        and not effect.disabled
        and not effect.transfer
        and not effect.changes
        and not effect.statuses
        and not effect.end_effects
        and effect.lifecycle is None
        and effect.action_policy is None
        and set(effect.flags) == {"bardic_inspiration"}
        and type(grant) is dict
        and type(grant.get("grant_id")) is str
        and bool(grant["grant_id"])
        and type(grant.get("die_sides")) is int
        and grant["die_sides"] in BARDIC_INSPIRATION_DIE_SIDES
        and set(grant) == {"grant_id", "die_sides"}
    )
    if not shape:
        return False
    feature = loader.get_feature("bardic-inspiration")
    canonical = BundledAssetLoader().get_feature("bardic-inspiration")
    if feature is None or feature != canonical or len(feature.passive_effects) != 1:
        return False
    expected = effect_state(
        passive_effect_to_active_effect(
            feature.passive_effects[0],
            target_id=effect.target_id,
            caster_id=effect.origin.removeprefix("cast:inspired:"),
            extra_flags={"bardic_inspiration": grant},
        )
    )
    return effect == expected


@lru_cache(maxsize=2)
def _reviewed_save_spell(slug: str, sign: Literal[-1, 1]) -> tuple[Spell, EffectState]:
    """Normalize the canonical application and verify its compiled signed d4."""
    spell = BundledAssetLoader().get_spell(slug)
    if spell is None or len(spell.passive_effects) != 1:
        raise ValueError(f"{slug} has no unique reviewed save modifier")
    application = passive_effect_to_active_effect(
        spell.passive_effects[0],
        target_id="reviewed-target",
        caster_id="reviewed-source",
        concentration=True,
    )
    save_changes = [
        change for change in application.changes if change.key == "system.bonuses.abilities.save"
    ]
    expected = "1d4" if sign == 1 else "-1d4"
    if len(save_changes) != 1 or (save_changes[0].mode, save_changes[0].value) != ("add", expected):
        raise ValueError(f"{slug} has no reviewed signed d4 save modifier")
    return spell, effect_state(application)


def project_save_bonus_dice(
    effects: Iterable[EffectState],
    loader: AssetLoader,
    *,
    allow_lifecycle_marker: bool = False,
) -> dict[str, tuple[SaveBonusDie, ...]]:
    """Project reviewed applications per target in persisted application order.

    Disabled sources contribute nothing. The latest equal-potency cast of a
    spell wins, with the preceding cast available if it ends first. Unknown
    active mechanics fail admission before any caller draws dice.
    """
    winners: dict[str, dict[str, tuple[int, SaveBonusDie]]] = {}
    bound_spells: dict[str, Spell | None] = {}
    for ordinal, effect in enumerate(effects):
        if effect.disabled:
            continue
        rule = _SAVE_SPELL_DICE.get(effect.id)
        if rule is None:
            if allow_lifecycle_marker and (
                not (
                    effect.changes or effect.statuses or effect.action_policy or effect.end_effects
                )
                and effect.lifecycle is None
                and (
                    not effect.flags
                    or (
                        effect.flags.keys() == {"concentration"}
                        and effect.flags["concentration"] is True
                    )
                )
            ):
                continue
            raise ValueError("target has unsupported save effect modifiers")
        slug, sign = rule
        spell, reviewed = _reviewed_save_spell(slug, sign)
        if slug not in bound_spells:
            bound_spells[slug] = loader.get_spell(slug)
        prefix = reviewed.origin.removesuffix("reviewed-source")
        if (
            not effect.origin.startswith(prefix)
            or not effect.origin.removeprefix(prefix).strip()
            or bound_spells[slug] != spell
            or effect
            != reviewed.model_copy(update={"target_id": effect.target_id, "origin": effect.origin})
            or effect.flags.get("concentration") is not True
        ):
            raise ValueError(f"{slug} save modifier differs from its reviewed application")
        winners.setdefault(effect.target_id, {})[slug] = (
            ordinal,
            SaveBonusDie(source=f"{effect.id}@{effect.origin}", sides=4, sign=sign),
        )
    return {
        target_id: tuple(die for _, die in sorted(ranked.values()))
        for target_id, ranked in winners.items()
    }


@lru_cache(maxsize=1)
def _reviewed_magic_resistance() -> MonsterTrait | None:
    """Read the canonical typed Magic Resistance definition from the bundled rules."""
    return BundledAssetLoader().get_trait(_MAGIC_RESISTANCE_SLUG)


def resolve_snapshot_save(
    state: CharacterStateV2,
    payload: SavePayload,
    rng: random.Random,
    *,
    bonus_dice: tuple[SaveBonusDie, ...] = (),
) -> SaveRolled:
    """Shared calculation seam; caller must prove full modifier/lifecycle admission."""
    roll = roll_snapshot_save(state, payload, rng, bonus_dice=bonus_dice)
    return SaveRolled(
        target_id=state.entity_id,
        ability=payload.ability,
        dc=payload.dc,
        roll_total=roll.total,
        succeeded=roll.succeeded,
        advantage=roll.mode,
        natural=roll.natural,
        modifier=roll.modifier,
        sources=list(roll.sources),
        roll_detail=roll.roll_detail,
    )


def roll_snapshot_save(
    state: CharacterStateV2,
    payload: SavePayload,
    rng: random.Random,
    *,
    bonus_dice: tuple[SaveBonusDie, ...] = (),
) -> SaveRoll:
    """Roll with admitted save facts, including Exhaustion, and supplied RNG."""
    target = combatant(state)
    projected = project_passive_save_modifiers([c.condition for c in target.conditions])
    ctx = ActivityResolutionContext(
        rng=rng,
        caster=target,
        caster_abilities={ab: getattr(target, field) for ab, field in _SCORE_ATTR.items()},
        targets=[target],
        event_emitter=lambda event: None,
        passive_save_modifiers={
            target.entity_id: {ab: save_modifier(target, ab).total for ab in get_args(Ability)}
        },
        passive_save_adv={target.entity_id: projected["passive_save_adv"]},
        passive_save_dis={target.entity_id: projected["passive_save_dis"]},
        passive_save_auto_fail={target.entity_id: projected["passive_save_auto_fail"]},
        d20_test_penalty={target.entity_id: d20_test_penalty(target.conditions)},
        save_is_magical=payload.is_magical,
    )
    profile = save_modifier(target, payload.ability)
    return roll_save(
        ctx,
        target,
        payload.ability,
        payload.dc,
        ignore_cover=True,
        additional_sources=AdvantageSources(
            advantage=payload.advantage, disadvantage=payload.disadvantage
        ),
        fixed_contributions=(
            RollFixedModifier(source="ability", value=profile.ability_mod),
            RollFixedModifier(source="save_proficiency", value=profile.proficiency),
        ),
        bonus_dice=bonus_dice,
    )


def save_condition_failure(
    state: CharacterStateV2, loader: AssetLoader, *, include_implied: bool = False
) -> str | None:
    """Check the supplied conditions against the compiled save and lifecycle clauses."""
    names = {condition.condition for condition in state.conditions}
    if include_implied:
        names.update(
            implied.value
            for condition, dependencies in CONDITION_IMPLIES.items()
            if condition.value in names
            for implied in dependencies
        )
    for name in sorted(names):
        definition = loader.get_condition(name)
        if definition is None or tuple(definition.effects) != _DECLARATIVE_CONDITION_EFFECTS.get(
            name, ()
        ):
            return "condition clauses differ from the pinned compiled rule projection"
        if include_implied and (
            name not in Condition or not condition_matches_compiled(definition)
        ):
            return "condition implications differ from the pinned compiled rule projection"
    return None


def save_actor_failure(
    state: CharacterStateV2,
    loader: AssetLoader,
    *,
    reviewed_identity: bool = False,
    reviewed_concentration: bool = False,
    reviewed_equipment: bool = False,
    magic_resistance_loader: AssetLoader | None = None,
    include_implied_conditions: bool = False,
) -> str | None:
    """Check actor prerequisites for the admitted saving throw."""
    if (
        any(not 1 <= getattr(state, field) <= 30 for field in _SCORE_ATTR.values())
        or not 1 <= state.character_level <= 20
        or (state.proficiency_bonus_override is not None and state.proficiency_bonus_override < 0)
        or set(state.save_proficiencies) - set(get_args(Ability))
    ):
        return "invalid save ability/proficiency facts"
    if any(
        condition.condition == "exhaustion" and not 1 <= condition.exhaustion_level <= 5
        for condition in state.conditions
    ):
        return "living saving actor has invalid Exhaustion level"
    if (
        ((state.worn_armor or state.shield_equipped) and not reviewed_equipment)
        or (state.concentration_effect_id and not reviewed_concentration)
        or (
            not reviewed_identity
            and (
                state.classes
                or state.class_slug
                or state.subclass_slug
                or state.species_slug
                or state.granted_features
                or state.fighting_styles
            )
        )
        or state.legendary_resistances_max
        or state.legendary_resistances_remaining
    ):
        return "feature, trait, concentration and resource-dependent saves are not migrated"
    if state.trait_mechanics:
        canonical = _reviewed_magic_resistance() if magic_resistance_loader is not None else None
        if (
            state.entity_type == "Character"
            or state.trait_mechanics != [MonsterTraitMechanic.MAGIC_RESISTANCE]
            or magic_resistance_loader is None
            or canonical is None
            or canonical.mechanic != MonsterTraitMechanic.MAGIC_RESISTANCE
            or magic_resistance_loader.get_trait(_MAGIC_RESISTANCE_SLUG) != canonical
        ):
            return "feature, trait, concentration and resource-dependent saves are not migrated"
    if failure := save_condition_failure(state, loader, include_implied=include_implied_conditions):
        return failure
    for slug in state.carried_item_slugs:
        item = loader.get_item(slug)
        if item is None or item.passive_effects or item.requires_attunement:
            return "unmigrated equipment save modifiers"
    return None


def _prepare_save(
    request: RuleEvaluationRequest, loader: AssetLoader
) -> TurnEvaluation | _PreparedSave:
    """Run the save's actual admission decisions without restoring or drawing RNG."""
    snapshot, payload = request.state_snapshot, request.payload
    assert isinstance(payload, SavePayload)
    if not isinstance(snapshot, NonCombatSnapshot) or snapshot.combat_setup is not None:
        return _refuse("unsupported", "save.snapshot", "standalone save requires noncombat facts")
    target = next((a for a in snapshot.character_states if a.entity_id == payload.target_id), None)
    if target is None or not target.is_alive or target.hp_current <= 0:
        return _refuse("rejected", "target_invalid", "saving actor is absent, dead or dying")
    if snapshot.scene_state.grid.cover_cells:
        return _refuse(
            "unsupported", "save.cover", "cover needs a complete source geometry contract"
        )
    active_effects = [
        effect
        for effect in snapshot.effect_states
        if effect.target_id == target.entity_id and not effect.disabled
    ]
    other_effects = [
        effect
        for effect in active_effects
        if not is_reviewed_beacon_effect(effect, loader) and not _held_inspiration(effect, loader)
    ]
    try:
        bonus_dice = project_save_bonus_dice(other_effects, loader).get(target.entity_id, ())
    except ValueError as exc:
        return _refuse("unsupported", "save.effects", str(exc))
    if set(payload.advantage + payload.disadvantage) - {"flag"}:
        return _refuse(
            "unsupported", "save.sources", "only explicitly adjudicated flags are admitted"
        )
    known = {
        "restrained",
        "paralyzed",
        "stunned",
        "petrified",
        "unconscious",
        "incapacitated",
        "prone",
        "exhaustion",
    }
    if any(
        c.condition not in known
        or c.scope != "session"
        or c.duration_rounds is not None
        or c.save_dc is not None
        or c.source_effect_id is not None
        for c in target.conditions
    ):
        return _refuse(
            "unsupported", "save.conditions", "condition lifecycle dependencies are not migrated"
        )
    # Only the complete canonical Wizard 1 profile has been reviewed for this
    # standalone save. Other classes and subclasses retain their dependency gate.
    reviewed_identity = False
    if target.classes or target.class_slug or target.granted_features:
        from dnd5e_engine.evaluation_class_dependencies import class_dependencies
        from dnd5e_engine.evaluation_ruleset import get_default_ruleset

        levels = target.classes or (
            {target.class_slug: target.character_level} if target.class_slug else {}
        )
        if levels != {"wizard": 1} or target.subclass_slug is not None:
            return _refuse(
                "unsupported", "save.actor", "only the Wizard 1 class profile is reviewed"
            )
        failure = class_dependencies(target, snapshot, loader, get_default_ruleset())
        if failure:
            return _refuse("unsupported", "save.actor", failure)
        reviewed_identity = True
    failure = save_actor_failure(
        target,
        loader,
        reviewed_identity=reviewed_identity,
        magic_resistance_loader=loader,
    )
    if failure:
        return _refuse("unsupported", "save.actor", failure)
    if payload.ability == "wis" and any(
        is_reviewed_beacon_effect(effect, loader) for effect in active_effects
    ):
        payload = payload.model_copy(update={"advantage": (*payload.advantage, "effect")})
    return target, payload, active_effects, bonus_dice


def save_bardic_fingerprint(request: RuleEvaluationRequest, loader: AssetLoader) -> str:
    """Hash the admitted save's selected rule facts, omitting RNG and world version."""
    if request.operation_kind != "rules.save" or not isinstance(request.payload, SavePayload):
        raise ValueError("Bardic Inspiration requires a standalone save")
    verify_ruleset(request.ruleset_binding, loader)
    prepared = _prepare_save(request, loader)
    if not isinstance(prepared, tuple):
        raise ValueError("saving throw was rejected")  # noqa: TRY004
    target, payload, effects, bonus_dice = prepared
    roller = combatant(target)
    selected = getattr(roller, _SCORE_ATTR[payload.ability])
    condition_projection = project_passive_save_modifiers(
        [condition.condition for condition in roller.conditions]
    )
    ability = payload.ability.upper()
    facts = {
        "binding": request.ruleset_binding.model_dump(mode="json"),
        "payload": payload.model_dump(mode="json"),
        "ability_score": selected,
        "modifier": save_modifier(roller, payload.ability).total,
        "exhaustion": d20_test_penalty(roller.conditions),
        "condition_advantage": ability in condition_projection["passive_save_adv"],
        "condition_disadvantage": ability in condition_projection["passive_save_dis"],
        "condition_auto_fail": ability in condition_projection["passive_save_auto_fail"],
        "magic_resistance": payload.is_magical
        and MonsterTraitMechanic.MAGIC_RESISTANCE in target.trait_mechanics,
        "beacon_sources": [
            (effect.id, effect.origin)
            for effect in effects
            if payload.ability == "wis" and is_reviewed_beacon_effect(effect, loader)
        ],
        "bonus_dice": [(die.source, die.sides, die.sign) for die in bonus_dice],
    }
    return sha256(dumps(facts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def resolve_bardic_save_choice(
    original: SaveRolled, die_sides: int, rng_context: RNGContext, *, use: bool
) -> tuple[SaveRolled, RNGTransition]:
    """Apply one optional Bardic die to fixed failed save evidence and compare to DC."""
    detail = original.roll_detail
    d20 = detail.d20 if detail is not None else None
    if (
        type(die_sides) is not int
        or die_sides not in BARDIC_INSPIRATION_DIE_SIDES
        or original.succeeded is not False
        or original.roll_total >= original.dc
        or detail is None
        or detail.status != "rolled"
        or d20 is None
        or d20.origin != "rng"
        or len(d20.faces) != (1 if original.advantage == "normal" else 2)
        or not 0 <= d20.kept_index < len(d20.faces)
        or any(not 1 <= face <= 20 for face in d20.faces)
        or d20.faces[d20.kept_index] != original.natural
        or original.modifier is None
        or original.natural is None
        or original.natural + sum(item.value for item in detail.modifiers) != original.roll_total
        or any(item.source == BARDIC_INSPIRATION_SOURCE for item in detail.modifiers)
    ):
        raise ValueError("Bardic Inspiration requires an original failed rolled save")
    event = original.model_copy(deep=True)
    if not use:
        return event, RNGTransition(
            stream_id=rng_context.stream_id,
            input_version=rng_context.version,
            input_state=rng_context.state,
            next_state=rng_context.state,
            state_changed=False,
        )
    rng = rng_context.state.restore()
    face = rng.randint(1, die_sides)
    assert event.roll_detail is not None
    event.roll_detail.modifiers.append(
        RollDieModifier(source=BARDIC_INSPIRATION_SOURCE, sides=die_sides, face=face, value=face)
    )
    return event.model_copy(
        update={
            "roll_total": original.roll_total + face,
            "succeeded": original.roll_total + face >= original.dc,
        }
    ), RNGTransition.between(rng_context, rng)


def evaluate_save(request: RuleEvaluationRequest, loader: AssetLoader) -> TurnEvaluation:
    """Evaluate one preflighted standalone save with explicit private RNG."""
    prepared = _prepare_save(request, loader)
    if not isinstance(prepared, tuple):
        return prepared
    target, payload, _, bonus_dice = prepared
    snapshot = request.state_snapshot
    rng = request.rng_context.state.restore()
    event = resolve_snapshot_save(target, payload, rng, bonus_dice=bonus_dice)
    return dict(
        status="accepted",
        state_delta=StateDelta(expected_world_version=snapshot.world_version, operations=()),
        proposed_events=(event,),
        rng_transition=RNGTransition.between(request.rng_context, rng),
        choice=None,
        error=None,
    )
