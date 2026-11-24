"""Bounded explicit saving throws over supplied actor facts and private RNG."""

import random
from collections.abc import Iterable
from functools import lru_cache
from hashlib import sha256
from json import dumps
from typing import Literal, NamedTuple, get_args

from dnd5e_srd_data.loader import AssetLoader, BundledAssetLoader
from dnd5e_srd_data.schema.item import Weapon
from dnd5e_srd_data.schema.monster import MonsterTrait, MonsterTraitMechanic
from dnd5e_srd_data.schema.spell import Spell

from dnd5e_engine.activities.actor_stats import _SCORE_ATTR, save_modifier
from dnd5e_engine.activities.context import ActivityResolutionContext
from dnd5e_engine.activities.d20 import AdvantageSources
from dnd5e_engine.activities.effects import passive_effect_to_active_effect
from dnd5e_engine.activities.save_primitive import SaveBonusDie, SaveRoll, roll_save
from dnd5e_engine.effect_lifecycle import conditions_after_effect_application
from dnd5e_engine.evaluation_actor import combatant
from dnd5e_engine.evaluation_bardic_check import (
    BARDIC_INSPIRATION_DIE_SIDES,
    BARDIC_INSPIRATION_SOURCE,
)
from dnd5e_engine.evaluation_beacon import is_reviewed_beacon_effect
from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest, SavePayload
from dnd5e_engine.evaluation_delta import StateDelta
from dnd5e_engine.evaluation_effects import EffectState, effect_state
from dnd5e_engine.evaluation_region_effect import is_fixed_region_condition_effect
from dnd5e_engine.evaluation_rng import RNGContext, RNGTransition
from dnd5e_engine.evaluation_ruleset import verify_ruleset
from dnd5e_engine.evaluation_state import CharacterStateV2, NonCombatSnapshot
from dnd5e_engine.evaluation_turn import TurnEvaluation, _refuse
from dnd5e_engine.evaluation_wearables import reviewed_protective_cloak
from dnd5e_engine.events import Ability, RollDieModifier, RollFixedModifier, SaveRolled
from dnd5e_engine.rules.conditions import (
    _DECLARATIVE_CONDITION_EFFECTS,
    CONDITION_IMPLIES,
    Condition,
    condition_matches_compiled,
    d20_test_penalty,
    project_passive_save_modifiers,
)
from dnd5e_engine.spatial import GridTopology, canonical_cell_id
from dnd5e_engine.specs import GridScene

_SAVE_SPELL_DICE: dict[str, tuple[str, Literal[-1, 1]]] = {
    "effect:blessed": ("bless", 1),
    "effect:bane": ("bane", -1),
}
_MAGIC_RESISTANCE_SLUG = "magic-resistance"
_INDEPENDENT_SAVE_CONDITIONS = frozenset(
    {
        "restrained",
        "paralyzed",
        "stunned",
        "petrified",
        "unconscious",
        "incapacitated",
        "prone",
        "exhaustion",
    }
)
type _PreparedSave = tuple[
    CharacterStateV2,
    SavePayload,
    list[EffectState],
    tuple[SaveBonusDie, ...],
    tuple[tuple[str, int], ...],
    str,
]


class SaveGeometryNeed(NamedTuple):
    """Exact actor pair whose World positions a directional save requires."""

    source_id: str
    target_id: str


def save_geometry_need(
    request: RuleEvaluationRequest, loader: AssetLoader
) -> SaveGeometryNeed | None:
    """Declare bounded source/target position facts after checking the rules binding."""
    verify_ruleset(request.ruleset_binding, loader)
    payload = request.payload
    if (
        request.operation_kind == "rules.save"
        and isinstance(payload, SavePayload)
        and payload.requires_cover
        and payload.ability == "dex"
        and payload.source_id is not None
    ):
        return SaveGeometryNeed(payload.source_id, payload.target_id)
    return None


def reviewed_held_save_weapon(item: Weapon) -> bool:
    """Recognize one authored fixed save clause, without other item mechanics."""
    if (
        item.requires_attunement
        or item.uses is not None
        or item.magical_bonus
        or len(item.passive_effects) != 1
    ):
        return False
    effect = item.passive_effects[0]
    return bool(
        not effect.disabled
        and effect.transfer
        and effect.duration is None
        and effect.action_policy is None
        and not effect.statuses
        and len(effect.changes) == 1
        and effect.changes[0].key == "system.bonuses.abilities.save"
        and effect.changes[0].mode == 2
        and effect.changes[0].value in ("1", "+1")
    )


def _save_equipment(
    state: CharacterStateV2, snapshot: NonCombatSnapshot, loader: AssetLoader
) -> tuple[tuple[tuple[str, int], ...], frozenset[str]]:
    """Admit exact held and worn source bonuses; stowed copies contribute zero."""
    allowed: set[str] = set()
    for slug in state.carried_item_slugs:
        item = loader.get_item(slug)
        if item is not None and item.passive_effects:
            if not reviewed_protective_cloak(item) and (
                not isinstance(item, Weapon) or not reviewed_held_save_weapon(item)
            ):
                raise ValueError("unmigrated equipment save modifiers")
            allowed.add(slug)
    if not allowed:
        return (), frozenset()
    if snapshot.snapshot_schema_version not in ("engine-snapshot/19", "engine-snapshot/20"):
        raise ValueError("save equipment needs an exact instance")
    bonuses: list[tuple[str, int]] = []
    worn = state.worn_item_instance_ids
    if len(worn) > 1:
        raise ValueError("unreviewed multiple worn item combinations")
    for iid in worn:
        matches = [
            entry
            for entry in snapshot.inventory_state.entries
            if entry.instance_id == iid
            and entry.owner_id == state.entity_id
            and entry.quantity == 1
            and entry.accessible
        ]
        if len(matches) != 1 or not reviewed_protective_cloak(
            loader.get_item(matches[0].item_slug)
        ):
            raise ValueError("worn save item differs from the owned inventory instance")
        if matches[0].attuned_actor_id == state.entity_id:
            bonuses.append((f"equipment:{iid}:{matches[0].item_slug}", 1))
    held = state.held_weapon_instance_id
    if held is None or state.weapon_in_hands not in allowed:
        return tuple(bonuses), frozenset(allowed)
    matches = [
        entry
        for entry in snapshot.inventory_state.entries
        if entry.instance_id == held
        and entry.owner_id == state.entity_id
        and entry.item_slug == state.weapon_in_hands
        and entry.quantity == 1
        and entry.accessible
    ]
    if len(matches) != 1:
        raise ValueError("held save weapon differs from the owned inventory instance")
    bonuses.append((f"equipment:{held}:{state.weapon_in_hands}", 1))
    return tuple(bonuses), frozenset(allowed)


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
    equipment_bonus: tuple[tuple[str, int], ...] = (),
    cover: str = "none",
) -> SaveRolled:
    """Shared calculation seam; caller must prove full modifier/lifecycle admission."""
    roll = roll_snapshot_save(
        state, payload, rng, bonus_dice=bonus_dice, equipment_bonus=equipment_bonus, cover=cover
    )
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
    equipment_bonus: tuple[tuple[str, int], ...] = (),
    cover: str = "none",
) -> SaveRoll:
    """Roll with admitted actor, held item and directional cover using supplied RNG."""
    target = combatant(state)
    projected = project_passive_save_modifiers([c.condition for c in target.conditions])
    ctx = ActivityResolutionContext(
        rng=rng,
        caster=target,
        caster_abilities={ab: getattr(target, field) for ab, field in _SCORE_ATTR.items()},
        targets=[target],
        event_emitter=lambda event: None,
        passive_save_modifiers={
            target.entity_id: {
                ab: save_modifier(target, ab).total + sum(value for _, value in equipment_bonus)
                for ab in get_args(Ability)
            }
        },
        passive_save_adv={target.entity_id: projected["passive_save_adv"]},
        passive_save_dis={target.entity_id: projected["passive_save_dis"]},
        passive_save_auto_fail={target.entity_id: projected["passive_save_auto_fail"]},
        d20_test_penalty={target.entity_id: d20_test_penalty(target.conditions)},
        save_is_magical=payload.is_magical,
        target_cover={target.entity_id: cover},
    )
    profile = save_modifier(target, payload.ability)
    return roll_save(
        ctx,
        target,
        payload.ability,
        payload.dc,
        ignore_cover=not payload.requires_cover,
        additional_sources=AdvantageSources(
            advantage=payload.advantage, disadvantage=payload.disadvantage
        ),
        fixed_contributions=(
            RollFixedModifier(source="ability", value=profile.ability_mod),
            RollFixedModifier(source="save_proficiency", value=profile.proficiency),
            *(RollFixedModifier(source=source, value=value) for source, value in equipment_bonus),
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
    reviewed_save_item_slugs: frozenset[str] = frozenset(),
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
        if (
            item is None
            or (item.passive_effects and slug not in reviewed_save_item_slugs)
            or (item.requires_attunement and slug not in reviewed_save_item_slugs)
        ):
            return "unmigrated equipment save modifiers"
    return None


def _save_condition_effects(
    target: CharacterStateV2, active_effects: list[EffectState], loader: AssetLoader
) -> set[tuple[str, str]]:
    """Verify current fixed-area condition records and return their effect identities.

    Only status-only fixed coverage is handled here. Independent round/turn
    clocks and repeat-save lifecycles need their own supported action scope.
    """
    regional = set()
    pending = {}
    for effect in active_effects:
        if not is_fixed_region_condition_effect(effect):
            continue
        regional.add((effect.id, effect.origin))
        projected, _ = conditions_after_effect_application(
            (), effect.id, effect.statuses, scope="session"
        )
        for condition in projected:
            key = (effect.id, condition.condition)
            if key in pending:
                raise ValueError("condition has ambiguous active effect lineage")
            pending[key] = condition.model_dump()
    for condition in target.conditions:
        if condition.source_effect_id is None:
            if (
                condition.condition not in _INDEPENDENT_SAVE_CONDITIONS
                or condition.scope != "session"
                or condition.duration_rounds is not None
                or condition.save_dc is not None
                or condition.source_entity_id == "implied:effect"
            ):
                raise ValueError("condition requires unsupported save or round lifecycle facts")
        elif pending.pop((condition.source_effect_id, condition.condition), None) != (
            condition.model_dump()
        ):
            raise ValueError("condition lacks its exact active fixed-area effect lineage")
        definition = loader.get_condition(condition.condition)
        if (
            definition is None
            or definition.slug != condition.condition
            or not condition_matches_compiled(definition)
        ):
            raise ValueError("condition clauses differ from the pinned compiled rule projection")
    if pending:
        raise ValueError("active fixed-area effect is missing its projected condition")
    return regional


def _prepare_save(
    request: RuleEvaluationRequest, loader: AssetLoader
) -> TurnEvaluation | _PreparedSave:
    """Check bound rules and the host's fixed current facts before restoring RNG.

    The host already owns the projected active conditions and target effects.
    Directional cover can require extra geometry. A target modifier uses its
    exact active application, independent of the roller's concentration slot.
    """
    snapshot, payload = request.state_snapshot, request.payload
    assert isinstance(payload, SavePayload)
    if not isinstance(snapshot, NonCombatSnapshot) or snapshot.combat_setup is not None:
        return _refuse("unsupported", "save.snapshot", "standalone save requires noncombat facts")
    target = next((a for a in snapshot.character_states if a.entity_id == payload.target_id), None)
    if target is None or not target.is_alive or target.hp_current <= 0:
        return _refuse("rejected", "target_invalid", "saving actor is absent, dead or dying")
    if payload.requires_cover and (payload.ability != "dex" or payload.source_id is None):
        return _refuse("unsupported", "save.cover", "Dexterity cover needs a named source")
    if not payload.requires_cover and request.save_geometry is not None:
        return _refuse("rejected", "save.cover", "unrequested save geometry was supplied")
    cover = _save_cover(request) if payload.requires_cover else "none"
    if cover is None:
        return _refuse("unsupported", "save.cover", "source or target geometry is missing")
    if cover == "total":
        return _refuse("unsupported", "save.cover", "total cover needs effect applicability")
    active_effects = [
        effect
        for effect in snapshot.effect_states
        if effect.target_id == target.entity_id and not effect.disabled
    ]
    try:
        condition_effects = _save_condition_effects(target, active_effects, loader)
    except ValueError as exc:
        return _refuse("unsupported", "save.conditions", str(exc))
    other_effects = [
        effect
        for effect in active_effects
        if (effect.id, effect.origin) not in condition_effects
        and not is_reviewed_beacon_effect(effect, loader)
        and not _held_inspiration(effect, loader)
    ]
    try:
        bonus_dice = project_save_bonus_dice(other_effects, loader).get(target.entity_id, ())
    except ValueError as exc:
        return _refuse("unsupported", "save.effects", str(exc))
    if set(payload.advantage + payload.disadvantage) - {"flag"}:
        return _refuse(
            "unsupported", "save.sources", "only explicitly adjudicated flags are admitted"
        )
    # Only these complete canonical level-one profiles are admitted here;
    # class_dependencies still checks their exact grants, pools and binding.
    reviewed_identity = False
    if target.classes or target.class_slug or target.granted_features:
        from dnd5e_engine.evaluation_class_dependencies import class_dependencies
        from dnd5e_engine.evaluation_ruleset import get_default_ruleset

        levels = target.classes or (
            {target.class_slug: target.character_level} if target.class_slug else {}
        )
        if levels not in ({"wizard": 1}, {"cleric": 1}) or target.subclass_slug is not None:
            return _refuse(
                "unsupported",
                "save.actor",
                "only Wizard 1 and Cleric 1 class profiles are reviewed",
            )
        failure = class_dependencies(
            target,
            snapshot,
            loader,
            get_default_ruleset(),
            allow_reviewed_cloak=True,
            allow_reviewed_holy_symbol=True,
        )
        if failure:
            return _refuse("unsupported", "save.actor", failure)
        reviewed_identity = True
    try:
        equipment_bonus, allowed_save_items = _save_equipment(target, snapshot, loader)
    except ValueError as exc:
        return _refuse("unsupported", "save.actor", str(exc))
    failure = save_actor_failure(
        target,
        loader,
        reviewed_identity=reviewed_identity,
        # This ordinary save neither tests nor changes existing concentration.
        reviewed_concentration=True,
        magic_resistance_loader=loader,
        reviewed_save_item_slugs=allowed_save_items,
    )
    if failure:
        return _refuse("unsupported", "save.actor", failure)
    if payload.ability == "wis" and any(
        is_reviewed_beacon_effect(effect, loader) for effect in active_effects
    ):
        payload = payload.model_copy(update={"advantage": (*payload.advantage, "effect")})
    return target, payload, active_effects, bonus_dice, equipment_bonus, cover


def _save_cover(request: RuleEvaluationRequest) -> str | None:
    """Resolve the required directional cover from fixed cells and scene geometry."""
    fact = request.save_geometry
    payload = request.payload
    if not isinstance(payload, SavePayload) or fact is None:
        return None
    scene = request.state_snapshot.scene_state
    if (
        fact.source_id != payload.source_id
        or fact.source_id == fact.target_id
        or fact.target_id != payload.target_id
        or fact.scene_id != scene.scene_id
    ):
        return None
    grid = GridTopology(GridScene.model_validate(scene.grid.model_dump()))
    try:
        if any(
            canonical_cell_id(cell) != cell or grid.distance_ft(cell, cell) is None
            for cell in (fact.source_cell, fact.target_cell)
        ):
            return None
        return grid.cover_between(fact.source_cell, fact.target_cell)
    except ValueError:
        return None


def save_bardic_fingerprint(request: RuleEvaluationRequest, loader: AssetLoader) -> str:
    """Hash the admitted save's selected rule facts, omitting RNG and world version."""
    if request.operation_kind != "rules.save" or not isinstance(request.payload, SavePayload):
        raise ValueError("Bardic Inspiration requires a standalone save")
    verify_ruleset(request.ruleset_binding, loader)
    prepared = _prepare_save(request, loader)
    if not isinstance(prepared, tuple):
        raise ValueError("saving throw was rejected")  # noqa: TRY004
    target, payload, effects, bonus_dice, equipment_bonus, cover = prepared
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
        "equipment_bonus": equipment_bonus,
        "cover": cover if payload.requires_cover else None,
        "save_geometry": (
            request.save_geometry.model_dump(mode="json")
            if request.save_geometry is not None
            else None
        ),
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
    target, payload, _, bonus_dice, equipment_bonus, cover = prepared
    snapshot = request.state_snapshot
    rng = request.rng_context.state.restore()
    event = resolve_snapshot_save(
        target,
        payload,
        rng,
        bonus_dice=bonus_dice,
        equipment_bonus=equipment_bonus,
        cover=cover,
    )
    return dict(
        status="accepted",
        state_delta=StateDelta(expected_world_version=snapshot.world_version, operations=()),
        proposed_events=(event,),
        rng_transition=RNGTransition.between(request.rng_context, rng),
        choice=None,
        error=None,
    )
