"""D&D 5e conditions — effects and application logic."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.condition import ConditionEffect, ConditionEffectKind

from dnd5e_engine.rules.effects import (
    attack_distance_flag_applies,
    condition_immunity_slugs,
    project_condition_effects,
    projected_boolean_flag,
    projected_scalar_value,
    save_flag_abilities,
)
from dnd5e_engine.types.effects import ActiveEffectChange

if TYPE_CHECKING:
    from dnd5e_engine.types.conditions import ActiveCondition, ConditionScope


class Condition(StrEnum):
    BLINDED = "blinded"
    CHARMED = "charmed"
    DEAFENED = "deafened"
    EXHAUSTION = "exhaustion"
    FRIGHTENED = "frightened"
    GRAPPLED = "grappled"
    INCAPACITATED = "incapacitated"
    INVISIBLE = "invisible"
    PARALYZED = "paralyzed"
    PETRIFIED = "petrified"
    POISONED = "poisoned"
    PRONE = "prone"
    RESTRAINED = "restrained"
    STUNNED = "stunned"
    UNCONSCIOUS = "unconscious"


# Migration selection only; mechanical meaning comes from the canonical data.
_DECLARATIVE_CONDITION_MIGRATIONS: dict[str, frozenset[ConditionEffectKind]] = {
    "charmed": frozenset(
        {ConditionEffectKind.CANT_ATTACK_CHARMER, ConditionEffectKind.CHARMER_SOCIAL_ADVANTAGE}
    ),
    "deafened": frozenset({ConditionEffectKind.AUTO_FAIL_HEARING_CHECKS}),
    "incapacitated": frozenset(
        {
            ConditionEffectKind.CANNOT_TAKE_ACTIONS,
            ConditionEffectKind.BREAKS_CONCENTRATION,
            ConditionEffectKind.DISADVANTAGE_INITIATIVE,
        }
    ),
    "exhaustion": frozenset(
        {
            ConditionEffectKind.D20_TEST_PENALTY_PER_LEVEL,
            ConditionEffectKind.SPEED_PENALTY_PER_LEVEL,
        }
    ),
    "grappled": frozenset(
        {ConditionEffectKind.SPEED_ZERO, ConditionEffectKind.DISADVANTAGE_ATTACKS_EXCEPT_GRAPPLER}
    ),
    "invisible": frozenset(
        {
            ConditionEffectKind.ADVANTAGE_OWN_ATTACKS,
            ConditionEffectKind.DISADVANTAGE_ATTACKS_AGAINST,
            ConditionEffectKind.UNSEEN,
            ConditionEffectKind.ADVANTAGE_INITIATIVE,
        }
    ),
    "frightened": frozenset(
        {
            ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS,
            ConditionEffectKind.DISADVANTAGE_ABILITY_CHECKS,
            ConditionEffectKind.CANT_MOVE_TOWARD_FEAR_SOURCE,
        }
    ),
    "poisoned": frozenset(
        {
            ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS,
            ConditionEffectKind.DISADVANTAGE_ABILITY_CHECKS,
        }
    ),
    "restrained": frozenset(
        {
            ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS,
            ConditionEffectKind.DISADVANTAGE_SAVE,
            ConditionEffectKind.ADVANTAGE_ATTACKS_AGAINST,
            ConditionEffectKind.SPEED_ZERO,
        }
    ),
    "blinded": frozenset(
        {
            ConditionEffectKind.AUTO_FAIL_SIGHT_CHECKS,
            ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS,
            ConditionEffectKind.ADVANTAGE_ATTACKS_AGAINST,
        }
    ),
    "prone": frozenset(
        {
            ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS,
            ConditionEffectKind.ADVANTAGE_ATTACKS_AGAINST,
            ConditionEffectKind.DISADVANTAGE_ATTACKS_AGAINST,
        }
    ),
    "paralyzed": frozenset(
        {
            ConditionEffectKind.AUTO_FAIL_SAVE,
            ConditionEffectKind.ADVANTAGE_ATTACKS_AGAINST,
            ConditionEffectKind.SPEED_ZERO,
            ConditionEffectKind.AUTO_CRIT_WITHIN_5FT,
        }
    ),
    "stunned": frozenset(
        {ConditionEffectKind.AUTO_FAIL_SAVE, ConditionEffectKind.ADVANTAGE_ATTACKS_AGAINST}
    ),
    "petrified": frozenset(
        {
            ConditionEffectKind.AUTO_FAIL_SAVE,
            ConditionEffectKind.ADVANTAGE_ATTACKS_AGAINST,
            ConditionEffectKind.SPEED_ZERO,
            ConditionEffectKind.RESIST_ALL_DAMAGE,
            ConditionEffectKind.IMMUNE_TO_CONDITION,
        }
    ),
    "unconscious": frozenset(
        {
            ConditionEffectKind.AUTO_FAIL_SAVE,
            ConditionEffectKind.ADVANTAGE_ATTACKS_AGAINST,
            ConditionEffectKind.SPEED_ZERO,
            ConditionEffectKind.AUTO_CRIT_WITHIN_5FT,
        }
    ),
}

# Load definitions once, outside resolution, so roll-time projection stays pure
# and custom asset-loader configuration retains the legacy helpers' behaviour.
# Cache canonical clauses; only explicitly opted-in kinds reach the projector.
_DECLARATIVE_CONDITION_EFFECTS: dict[str, tuple[ConditionEffect, ...]] = {
    slug: tuple(definition.effects)
    for slug in _DECLARATIVE_CONDITION_MIGRATIONS
    if (definition := BundledAssetLoader().get_condition(slug)) is not None
}


def _project_condition_changes(conditions: list[str]) -> list[ActiveEffectChange]:
    """Collect migrated clauses in stable input order, ignoring duplicate names."""
    names = dict.fromkeys(c.lower() for c in conditions)
    # Preserve the same implied-condition semantics as the legacy predicates.
    for condition, implied in CONDITION_IMPLIES.items():
        if condition.value in names:
            names.update(dict.fromkeys(c.value for c in implied))
    return project_condition_effects(
        effect
        for name in names
        for effect in _DECLARATIVE_CONDITION_EFFECTS.get(name, ())
        if effect.kind in _DECLARATIVE_CONDITION_MIGRATIONS.get(name, frozenset())
    )


# Conditions that automatically include other conditions
CONDITION_IMPLIES: dict[Condition, list[Condition]] = {
    Condition.PARALYZED: [Condition.INCAPACITATED],
    Condition.PETRIFIED: [Condition.INCAPACITATED],
    Condition.STUNNED: [Condition.INCAPACITATED],
    Condition.UNCONSCIOUS: [Condition.INCAPACITATED, Condition.PRONE],
}

# Human-readable effects per condition, phrased against SRD 5.2 (2024).
#
# These strings are DESCRIPTIVE ONLY — they are surfaced by
# ``get_condition_effects`` for hosts to display, and are not what the engine
# enforces. A condition's actual mechanical effect reaches a roll through the
# projection helpers below (``project_passive_*``) and the active-effect fold in
# the orchestrator. Where the two differ, the projections are authoritative and
# the gap is tracked in BACKLOG.md; do not read this table as a statement of
# what is implemented.
CONDITION_EFFECTS: dict[Condition, list[str]] = {
    Condition.BLINDED: [
        "Automatically fails any ability check requiring sight",
        "Attack rolls against this creature have advantage",
        "This creature's attack rolls have disadvantage",
    ],
    Condition.CHARMED: [
        "Cannot attack the charmer or target them with spells",
        "Charmer has advantage on social checks against this creature",
    ],
    Condition.DEAFENED: [
        "Cannot hear",
        "Automatically fails ability checks requiring hearing",
    ],
    # SRD 5.2 replaced the 2014 six-tier ladder with two scaling penalties.
    # Both numeric penalties are enforced through canonical clause projection.
    # The level-6 death clause remains descriptive; it has no live death path.
    Condition.EXHAUSTION: [
        "D20 Tests are reduced by 2 times the creature's Exhaustion level",
        "Speed is reduced by 5 feet times the creature's Exhaustion level",
        "Exhaustion level 6 is death",
        "Finishing a Long Rest removes one level",
    ],
    Condition.FRIGHTENED: [
        "Disadvantage on ability checks and attack rolls while source of fear is in line of sight",
        "Cannot willingly move closer to source of fear",
    ],
    Condition.GRAPPLED: [
        "Speed is 0 and can't increase",
        "Disadvantage on attack rolls against any target other than the grappler",
        "The grappler can drag or carry this creature when it moves",
    ],
    Condition.INCAPACITATED: [
        "Cannot take any action, Bonus Action, or Reaction",
        "Concentration is broken",
        "Cannot speak",
        "Disadvantage on Initiative if Incapacitated when rolling it",
    ],
    Condition.INVISIBLE: [
        "Concealed: unaffected by effects that require the target to be seen",
        "Attacks by this creature have advantage",
        "Attacks against this creature have disadvantage",
    ],
    Condition.PARALYZED: [
        "Incapacitated; Speed is 0 and can't increase",
        "Automatically fails STR and DEX saving throws",
        "Attack rolls against this creature have advantage",
        "Any attack that hits is a critical hit if within 5 feet",
    ],
    Condition.PETRIFIED: [
        "Transformed into stone; incapacitated, can't move or speak",
        "Attacks against this creature have advantage",
        "Automatically fails STR and DEX saving throws",
        "Resistance to all damage",
        "Immune to the Poisoned condition",
    ],
    Condition.POISONED: [
        "Disadvantage on attack rolls and ability checks",
    ],
    Condition.PRONE: [
        "Only movement option is to crawl, unless the creature stands up",
        "Disadvantage on attack rolls",
        "An attack roll against this creature has advantage if the attacker is "
        "within 5 feet, and disadvantage otherwise",
    ],
    Condition.RESTRAINED: [
        "Speed becomes 0",
        "Attack rolls against this creature have advantage",
        "This creature's attack rolls have disadvantage",
        "Disadvantage on DEX saving throws",
    ],
    Condition.STUNNED: [
        "Incapacitated",
        "Automatically fails STR and DEX saving throws",
        "Attack rolls against this creature have advantage",
    ],
    Condition.UNCONSCIOUS: [
        "Incapacitated and Prone; remains Prone when the condition ends",
        "Speed is 0 and can't increase; drops whatever it is holding",
        "Automatically fails STR and DEX saving throws",
        "Attacks against this creature have advantage",
        "Any attack that hits from within 5 feet is a critical hit",
    ],
}


def is_condition_active(condition: Condition, active_conditions: list[str]) -> bool:
    """Check if a condition (or one that implies it) is active."""
    active_set = {c.lower() for c in active_conditions}

    if condition.value in active_set:
        return True

    # Check implied conditions
    for cond, implied in CONDITION_IMPLIES.items():
        if cond.value in active_set and condition in implied:
            return True

    return False


def apply_condition(
    condition: Condition,
    current_conditions: list[str],
) -> list[str]:
    """Add a condition (idempotent), respecting projected condition immunity."""
    if check_immunity(condition.value, project_condition_immunities(current_conditions)):
        return current_conditions
    if condition.value not in current_conditions:
        return [*current_conditions, condition.value]
    return current_conditions


def remove_condition(
    condition: Condition,
    current_conditions: list[str],
) -> list[str]:
    """Remove a condition."""
    return [c for c in current_conditions if c.lower() != condition.value]


def get_condition_effects(condition: Condition) -> list[str]:
    """Return human-readable effects of a condition."""
    return CONDITION_EFFECTS.get(condition, [])


def active_condition_names(conditions: list[ActiveCondition]) -> list[str]:
    """Extract string condition names from list[ActiveCondition] for legacy helper compatibility."""
    from dnd5e_engine.types.conditions import (
        ActiveCondition as _ActiveCondition,  # noqa: F401 (runtime import)
    )

    return [c.condition for c in conditions]


def apply_condition_with_implies(
    condition: Condition,
    source_entity_id: str,
    scope: ConditionScope,
    current_conditions: list[ActiveCondition],
    duration_rounds: int | None = None,
    save_dc: int | None = None,
    applied_round: int = 0,
    exhaustion_level: int = 1,
    source_effect_id: str | None = None,
) -> list[ActiveCondition]:
    """Apply a condition plus all implied conditions per D-04.

    Idempotent per condition name. Implied conditions get
    source_entity_id=f"implied:{condition.value}".

    source_effect_id: Effect node ID when this condition is bridged from an
    effect (FX-05). Set on the root condition only; implied conditions inherit
    source_entity_id="implied:{condition}" with no effect link.
    """
    from dnd5e_engine.types.conditions import ActiveCondition

    if check_immunity(
        condition.value, project_condition_immunities(active_condition_names(current_conditions))
    ):
        return current_conditions

    existing_names = {c.condition for c in current_conditions}
    result = list(current_conditions)

    # Apply the root condition if not already present
    if condition.value not in existing_names:
        result.append(
            ActiveCondition(
                condition=condition.value,
                source_entity_id=source_entity_id,
                scope=scope,
                duration_rounds=duration_rounds,
                save_dc=save_dc,
                applied_round=applied_round,
                exhaustion_level=exhaustion_level,
                source_effect_id=source_effect_id,
            )
        )
        existing_names.add(condition.value)

    # Apply all implied conditions
    for implied_cond in CONDITION_IMPLIES.get(condition, []):
        if implied_cond.value not in existing_names:
            result.append(
                ActiveCondition(
                    condition=implied_cond.value,
                    source_entity_id=f"implied:{condition.value}",
                    scope=scope,
                    applied_round=applied_round,
                )
            )
            existing_names.add(implied_cond.value)

    return result


def remove_condition_with_implies(
    condition: Condition,
    current_conditions: list[ActiveCondition],
) -> list[ActiveCondition]:
    """Remove root condition AND all entries implied by it.

    Removes:
    - entries with condition == condition.value
    - entries where source_entity_id == f"implied:{condition.value}"
    """
    implied_source = f"implied:{condition.value}"
    implied_names = {c.value for c in CONDITION_IMPLIES.get(condition, [])}

    result = []
    for c in current_conditions:
        # Remove the root condition itself
        if c.condition == condition.value:
            continue
        # Remove entries that were implied by this condition (by source tag)
        if c.source_entity_id == implied_source and c.condition in implied_names:
            continue
        result.append(c)
    return result


def check_immunity(condition_name: str, immunities: list[str]) -> bool:
    """Check if condition_name is in the immunities list."""
    return condition_name in immunities


def conditions_grant_disadvantage_on_ability_checks(
    conditions: list[str], *, fear_source_in_sight: bool = True
) -> bool:
    """Consume unconditional and fear-sight-gated ability-check flags.

    The default preserves callers without runtime context, including unknown
    fear sources. Live consumers supply the existing visibility predicate.
    Poisoned's ungated clause remains independent of fear-source visibility.
    Exhaustion still supplies a numeric D20 penalty, not disadvantage.
    """
    changes = _project_condition_changes(conditions)
    return projected_boolean_flag(changes, "flags.disadvantage.check") or (
        fear_source_in_sight
        and projected_boolean_flag(changes, "flags.disadvantage.check.gate.fear_source_in_sight")
    )


def conditions_cannot_attack_charmer(conditions: list[str]) -> bool:
    """Whether a migrated clause forbids attacking the runtime charmer."""
    return projected_boolean_flag(
        _project_condition_changes(conditions), "targeting.cannot_attack_charmer"
    )


def conditions_auto_fail_check(conditions: list[str], sense: str) -> bool:
    return sense in ("sight", "hearing") and projected_boolean_flag(
        _project_condition_changes(conditions), f"flags.auto_fail.check.{sense}"
    )


def conditions_charmer_social_advantage(conditions: list[str]) -> bool:
    return projected_boolean_flag(
        _project_condition_changes(conditions), "flags.advantage.check.charmer_social"
    )


def conditions_cannot_move_toward_fear_source(conditions: list[str]) -> bool:
    """Whether approach is restricted, independently of source visibility."""
    return projected_boolean_flag(
        _project_condition_changes(conditions), "movement.cannot_move_toward_fear_source"
    )


def conditions_unseen(conditions: list[str], *, observer_can_see_bearer: bool) -> bool:
    """Consume concealment with the observer's runtime special-sense context."""
    return not observer_can_see_bearer and projected_boolean_flag(
        _project_condition_changes(conditions), "visibility.unseen.gate.observer_cannot_see_bearer"
    )


def _attack_flag_present(changes: list[ActiveEffectChange], key: str) -> bool:
    """Consume exact boolean overrides without stacking duplicate clauses."""
    return any(
        change.key == key and change.mode == "override" and change.value is True
        for change in changes
    )


def conditions_grant_advantage_on_attack(
    attacker_conditions: list[str],
    target_conditions: list[str],
    *,
    distance_ft: int | None = None,
    grappler_id: str | None = None,
    target_id: str | None = None,
    attacker_invisibility_pierced: bool = False,
    target_invisibility_pierced: bool = False,
    fear_source_in_sight: bool = True,
) -> tuple[bool, bool]:
    """
    Returns (attacker_has_advantage, attacker_has_disadvantage) based on conditions.
    Does NOT account for ranged vs melee distinction (caller's responsibility).

    Keyword inputs (all optional — absent means "unknown" and the row that
    needs it stays inert, so pre-0.6 callers are unaffected):

    * ``distance_ft`` — attacker→target distance for the Prone target row
      (SRD 5.2 Prone: advantage within 5 ft, disadvantage otherwise).
    * ``grappler_id`` / ``target_id`` — identity of the creature grappling the
      ATTACKER and of the target, for the Grappled attacker row (SRD 5.2
      Grappled: disadvantage against any target other than the grappler).
    * ``attacker_invisibility_pierced`` — True iff the TARGET can somehow see
      the Invisible ATTACKER (a special sense — Blindsight/Truesight — reaches
      AND line of sight holds). SRD 5.2 Invisible: "If a creature can somehow
      see you, you don't gain this benefit against that creature." Default
      ``False`` preserves pre-C16b behaviour (no vision model wired).
    * ``target_invisibility_pierced`` — the same, but for the ATTACKER seeing
      the Invisible TARGET.
    * ``fear_source_in_sight`` — SRD 5.2 Frightened: "Disadvantage on ...
      attack rolls while the source of fear is within line of sight." True
      (the SRD-conservative default) means either the attacker isn't
      Frightened, or the gate doesn't apply / can't be evaluated (unknown
      source), or the source is actually visible — so the disadvantage row
      below still fires. Only an explicit ``False`` — a known, tracked
      source the Frightened attacker cannot currently see — drops it.
    """
    # Consume the flags by side: target clauses grant advantage against that
    # creature; its own-attack disadvantage (including implied Prone) does not.
    target_changes = _project_condition_changes(target_conditions)
    attacker_changes = _project_condition_changes(attacker_conditions)
    advantage = (
        _attack_flag_present(target_changes, "flags.advantage.attack")
        or attack_distance_flag_applies(
            target_changes, ConditionEffectKind.ADVANTAGE_ATTACKS_AGAINST, distance_ft
        )
        or (
            not attacker_invisibility_pierced
            and _attack_flag_present(
                attacker_changes, "flags.advantage.attack.gate.observer_cannot_see_bearer"
            )
        )
    )
    disadvantage = (
        _attack_flag_present(attacker_changes, "flags.disadvantage.attack")
        or attack_distance_flag_applies(
            target_changes, ConditionEffectKind.DISADVANTAGE_ATTACKS_AGAINST, distance_ft
        )
        or (
            fear_source_in_sight
            and _attack_flag_present(
                attacker_changes, "flags.disadvantage.attack.gate.fear_source_in_sight"
            )
        )
        or (
            not target_invisibility_pierced
            and _attack_flag_present(
                target_changes, "flags.disadvantage.attack.gate.observer_cannot_see_bearer"
            )
        )
        or (
            grappler_id is not None
            and target_id != grappler_id
            and _attack_flag_present(attacker_changes, "flags.disadvantage.attack.except_grappler")
        )
    )

    return advantage, disadvantage


# ── SRD 5.2 condition predicates and numeric projections (C12) ──────────────


def exhaustion_level_of(conditions: list[ActiveCondition]) -> int:
    """The creature's Exhaustion level: the highest ``exhaustion_level`` carried
    by any ``exhaustion`` entry (0 when the condition is absent)."""
    return max(
        (ac.exhaustion_level for ac in conditions if ac.condition.lower() == "exhaustion"),
        default=0,
    )


def d20_test_penalty(conditions: list[ActiveCondition]) -> int:
    """The signed flat modifier SRD 5.2 Exhaustion applies to EVERY D20 Test
    (attack rolls, saving throws — death saves included — and ability checks).

    The canonical per-level multiplier is applied to the highest runtime
    Exhaustion level; absent clauses or no Exhaustion contribute zero.
    """
    multiplier = projected_scalar_value(
        _project_condition_changes([Condition.EXHAUSTION.value]), "d20_test.penalty_per_level"
    )
    return -(multiplier or 0) * exhaustion_level_of(conditions)


def project_speed(base_speed: int, condition_names: list[str], exhaustion_level: int = 0) -> int:
    """The creature's effective walking Speed under its conditions.

    A projected zero-speed override forces 0 ("and can't increase" — the
    orchestrator's Dash adds THIS projection, not ``base_speed``); otherwise
    Exhaustion subtracts its canonical multiplier times the supplied runtime
    level, floored at 0. The explicit level remains effective even if the caller
    omits Exhaustion from condition_names (the historical helper contract).
    """
    if any(
        change.key == "speed.override"
        and change.mode == "override"
        and type(change.value) is int
        and change.value == 0
        for change in _project_condition_changes(condition_names)
    ):
        return 0
    multiplier = projected_scalar_value(
        _project_condition_changes([Condition.EXHAUSTION.value]), "speed.penalty_per_level"
    )
    return max(0, base_speed - (multiplier or 0) * exhaustion_level)


def conditions_block_actions(condition_names: list[str]) -> bool:
    """SRD 5.2 Incapacitated: "You can't take any action, Bonus Action, or
    Reaction." Canonical clauses supply the boolean, including Incapacitated
    reached through the existing ``CONDITION_IMPLIES`` chain."""
    return projected_boolean_flag(
        _project_condition_changes(condition_names), "condition.cannot_take_actions"
    )


def conditions_break_concentration(condition_names: list[str]) -> bool:
    """Project concentration break independently of other incapacitation lifecycles."""
    return projected_boolean_flag(
        _project_condition_changes(condition_names), "condition.breaks_concentration"
    )


def conditions_disadvantage_initiative(condition_names: list[str]) -> bool:
    """Project the canonical initiative clause, including implied conditions."""
    return projected_boolean_flag(
        _project_condition_changes(condition_names), "flags.disadvantage.initiative"
    )


def conditions_advantage_initiative(condition_names: list[str]) -> bool:
    """Project ungated Initiative advantage independently of observer visibility."""
    return projected_boolean_flag(
        _project_condition_changes(condition_names), "flags.advantage.initiative"
    )


def conditions_auto_crit_within_5ft(
    target_condition_names: list[str], *, distance_ft: int | None = 5
) -> bool:
    """Whether a hit at this distance receives a projected automatic crit.

    The historical one-argument API queries at 5 ft. Resolvers always pass the
    actual distance, including None for unknown; the threshold comes from data.
    """
    return attack_distance_flag_applies(
        _project_condition_changes(target_condition_names),
        ConditionEffectKind.AUTO_CRIT_WITHIN_5FT,
        distance_ft,
    )


# ── Per-effect sidecar projection (combat orchestrator hydration) ────────────
#
# The combat orchestrator hydrates the active-effect projection sidecars from the live
# combatant's conditions immediately before invoking the per-effect
# evaluator. The handlers under ``app/combat/effects/*.py`` read three
# tables off the store:
#
#   * ``_passive_damage_modifiers[target_id]`` →
#       ``{"resistances": [...], "vulnerabilities": [...], "immunities": [...]}``
#     (consumed by ``damage.py``; ``"all"`` is the catch-all damage-type
#     marker used by Petrified's "resistance to all damage")
#   * ``_save_modifiers[target_id]`` →
#       ``{"passive_save_adv": [ability_code, ...],
#         "passive_save_dis": [ability_code, ...]}``
#     (consumed by ``save.py``; ability codes are upper-case STR/DEX/CON/
#     INT/WIS/CHA)
#   * ``_check_modifiers[actor_id]`` →
#       ``{"passive_check_adv": [...], "passive_check_dis": [...]}``
#     (consumed by ``check.py``; ``"all"`` is the catch-all for conditions
#     that impose dis/adv on *every* ability check — Frightened, Poisoned)
#
# This is the condition portion of the sidecar payload: migrated clauses
# come from canonical data; the remaining clauses keep their legacy rules.
# Active-effect modifiers layer on top in the orchestrator, which owns the
# transport-level merge.


def project_condition_immunities(conditions: list[str]) -> list[str]:
    """Project opted-in canonical clauses into stable, unique condition immunities."""
    return condition_immunity_slugs(_project_condition_changes(conditions))


def project_passive_damage_modifiers(conditions: list[str]) -> dict[str, list[str]]:
    """Return the damage sidecar from opted-in canonical clause projection.

    Condition immunity is separate: it never grants damage immunity.
    """
    out: dict[str, list[str]] = {"resistances": [], "vulnerabilities": [], "immunities": []}
    if any(
        change.key == "damage.resistance.all" and change.mode == "override" and change.value is True
        for change in _project_condition_changes(conditions)
    ):
        out["resistances"].append("all")
    return out


def project_passive_save_modifiers(conditions: list[str]) -> dict[str, list[str]]:
    """Return passive save adv / dis / auto-fail ability-code lists.

    Migrated canonical SRD 5.2 clauses supply the ability scopes:

    * Restrained → disadvantage on DEX saves.
    * Paralyzed / Stunned / Petrified / Unconscious → auto-fail STR + DEX
      saves. Surfaced as ``passive_save_auto_fail`` so the save handler
      short-circuits the d20 roll entirely (no rng consumption, no
      modifier math). ``passive_save_dis`` is also populated as a
      belt-and-suspenders fallback so a save handler that doesn't yet
      honor auto-fail still resolves in the correct direction.
    """
    changes = _project_condition_changes(conditions)
    disadvantage = save_flag_abilities(changes, ConditionEffectKind.DISADVANTAGE_SAVE)
    auto_fail = save_flag_abilities(changes, ConditionEffectKind.AUTO_FAIL_SAVE)
    return {
        "passive_save_adv": [],
        # Keep the existing defensive disadvantage fallback for auto-fail saves,
        # after explicit disadvantage entries, without duplicating abilities.
        "passive_save_dis": list(dict.fromkeys([*disadvantage, *auto_fail])),
        "passive_save_auto_fail": auto_fail,
    }


def project_passive_check_modifiers(
    conditions: list[str], *, fear_source_in_sight: bool = True
) -> dict[str, list[str]]:
    """Return ``passive_check_adv`` / ``passive_check_dis`` lists.

    Conditions that impose disadvantage on *every* ability check use the
    ``"all"`` catch-all marker the ``check.py`` handler already recognizes
    (see ``_reconcile_adv_dis``):

    * Frightened — its projected flag requires runtime fear-source sight.
    * Poisoned — its projected flag applies regardless of fear-source sight.
    * Exhaustion — NOT projected here (SRD 5.2: numeric ``-2 x level`` penalty
      on every D20 Test, see ``d20_test_penalty``).
    """
    out: dict[str, list[str]] = {"passive_check_adv": [], "passive_check_dis": []}
    if conditions_grant_disadvantage_on_ability_checks(
        conditions, fear_source_in_sight=fear_source_in_sight
    ):
        out["passive_check_dis"].append("all")
    return out


__all__ = [
    "CONDITION_EFFECTS",
    "CONDITION_IMPLIES",
    "Condition",
    "active_condition_names",
    "apply_condition",
    "apply_condition_with_implies",
    "check_immunity",
    "conditions_advantage_initiative",
    "conditions_auto_crit_within_5ft",
    "conditions_block_actions",
    "conditions_break_concentration",
    "conditions_cannot_attack_charmer",
    "conditions_cannot_move_toward_fear_source",
    "conditions_disadvantage_initiative",
    "conditions_grant_advantage_on_attack",
    "conditions_grant_disadvantage_on_ability_checks",
    "conditions_unseen",
    "d20_test_penalty",
    "exhaustion_level_of",
    "get_condition_effects",
    "is_condition_active",
    "project_condition_immunities",
    "project_passive_check_modifiers",
    "project_passive_damage_modifiers",
    "project_passive_save_modifiers",
    "project_speed",
    "remove_condition",
    "remove_condition_with_implies",
]
