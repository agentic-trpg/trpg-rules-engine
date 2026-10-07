"""Active-effect resolver helpers — Foundry-shaped changes vocabulary.

retires apply_effect_modifiers / derive_applicable_action_types /
derive_condition_scope / filter_stacking / get_bridged_conditions.
Replacements:
  - apply_changes_to_check    folds add-mode and override-mode changes
                              into a check bucket's running total.
  - filter_changes_by_bucket  selects ActiveEffectChange entries whose
                              key matches a target bucket.
  - dedupe_by_identity        dedupes effects by (target_id, id, origin)
                              — the Foundry-shaped identity tuple.

Pure functions, zero I/O.
"""

from __future__ import annotations

import random
from collections.abc import Iterable

from dnd5e_srd_data.schema.condition import (
    ConditionEffect,
    ConditionEffectGate,
    ConditionEffectKind,
)

from dnd5e_engine.rules.character import ABILITY_NAME_BY_CODE
from dnd5e_engine.types.effects import ActiveEffect, ActiveEffectChange

# Reuse the existing full-name save flag suffixes (e.g. ``save.dexterity``).
# Both projection and sidecar consumption share this vocabulary; no key parsing
# or condition-specific ability scopes are needed at resolution time.
_SAVE_FLAG_KEYS: dict[ConditionEffectKind, dict[str, str]] = {
    kind: {code: f"{prefix}.{name}" for code, name in ABILITY_NAME_BY_CODE.items()}
    for kind, prefix in (
        (ConditionEffectKind.DISADVANTAGE_SAVE, "flags.disadvantage.save"),
        (ConditionEffectKind.AUTO_FAIL_SAVE, "flags.auto_fail.save"),
    )
}

_ATTACK_DISTANCE_FLAG_KEYS = {
    ConditionEffectKind.ADVANTAGE_ATTACKS_AGAINST: "flags.advantage.attack.within_ft",
    ConditionEffectKind.DISADVANTAGE_ATTACKS_AGAINST: "flags.disadvantage.attack.beyond_ft",
    ConditionEffectKind.AUTO_CRIT_WITHIN_5FT: "flags.auto_crit.attack.within_ft",
}

_CONDITION_SCALAR_KEYS = {
    **_ATTACK_DISTANCE_FLAG_KEYS,
    ConditionEffectKind.D20_TEST_PENALTY_PER_LEVEL: "d20_test.penalty_per_level",
    ConditionEffectKind.SPEED_PENALTY_PER_LEVEL: "speed.penalty_per_level",
}


_GATED_CONDITION_FLAG_KEYS: dict[tuple[ConditionEffectKind, ConditionEffectGate | None], str] = {
    (
        ConditionEffectKind.ADVANTAGE_OWN_ATTACKS,
        ConditionEffectGate.OBSERVER_CANNOT_SEE_BEARER,
    ): "flags.advantage.attack.gate.observer_cannot_see_bearer",
    (
        ConditionEffectKind.DISADVANTAGE_ATTACKS_AGAINST,
        ConditionEffectGate.OBSERVER_CANNOT_SEE_BEARER,
    ): "flags.disadvantage.attack.gate.observer_cannot_see_bearer",
    (
        ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS,
        ConditionEffectGate.FEAR_SOURCE_IN_SIGHT,
    ): "flags.disadvantage.attack.gate.fear_source_in_sight",
    (
        ConditionEffectKind.DISADVANTAGE_ABILITY_CHECKS,
        ConditionEffectGate.FEAR_SOURCE_IN_SIGHT,
    ): "flags.disadvantage.check.gate.fear_source_in_sight",
    (
        ConditionEffectKind.UNSEEN,
        ConditionEffectGate.OBSERVER_CANNOT_SEE_BEARER,
    ): "visibility.unseen.gate.observer_cannot_see_bearer",
}


def _project_gated_condition_effect(effect: ConditionEffect) -> list[ActiveEffectChange]:
    """Unsupported gates and mixed distance/gate clauses stay inert."""
    key = _GATED_CONDITION_FLAG_KEYS.get((effect.kind, effect.gate))
    return (
        [ActiveEffectChange(key=key, mode="override", value=True)]
        if key is not None and effect.value is None
        else []
    )


def project_condition_effects(effects: Iterable[ConditionEffect]) -> list[ActiveEffectChange]:
    """Translate supported typed clauses into the active-effect override vocabulary.

    Pure projection: no condition-name rules, I/O, RNG, or input mutation.
    Selecting which conditions have migrated is the caller's responsibility;
    unsupported kinds stay on their legacy paths.
    """
    flag_keys = {
        ConditionEffectKind.DISADVANTAGE_OWN_ATTACKS: "flags.disadvantage.attack",
        ConditionEffectKind.DISADVANTAGE_ABILITY_CHECKS: "flags.disadvantage.check",
        ConditionEffectKind.AUTO_FAIL_SIGHT_CHECKS: "flags.auto_fail.check.sight",
        ConditionEffectKind.AUTO_FAIL_HEARING_CHECKS: "flags.auto_fail.check.hearing",
        ConditionEffectKind.CHARMER_SOCIAL_ADVANTAGE: "flags.advantage.check.charmer_social",
        ConditionEffectKind.RESIST_ALL_DAMAGE: "damage.resistance.all",
        ConditionEffectKind.CANNOT_TAKE_ACTIONS: "condition.cannot_take_actions",
        ConditionEffectKind.BREAKS_CONCENTRATION: "condition.breaks_concentration",
        ConditionEffectKind.DISADVANTAGE_INITIATIVE: "flags.disadvantage.initiative",
        ConditionEffectKind.ADVANTAGE_INITIATIVE: "flags.advantage.initiative",
        ConditionEffectKind.DISADVANTAGE_ATTACKS_EXCEPT_GRAPPLER: (
            "flags.disadvantage.attack.except_grappler"
        ),
    }
    changes: list[ActiveEffectChange] = []
    for effect in effects:
        if effect.gate is not None:
            changes.extend(_project_gated_condition_effect(effect))
            continue
        if effect.kind in {
            ConditionEffectKind.CANT_ATTACK_CHARMER,
            ConditionEffectKind.CANT_MOVE_TOWARD_FEAR_SOURCE,
        }:
            if effect.value is None:
                restriction_key = (
                    "targeting.cannot_attack_charmer"
                    if effect.kind == ConditionEffectKind.CANT_ATTACK_CHARMER
                    else "movement.cannot_move_toward_fear_source"
                )
                changes.append(ActiveEffectChange(key=restriction_key, mode="override", value=True))
        elif effect.kind == ConditionEffectKind.ADVANTAGE_ATTACKS_AGAINST and effect.value is None:
            changes.append(
                ActiveEffectChange(key="flags.advantage.attack", mode="override", value=True)
            )
        elif key := _CONDITION_SCALAR_KEYS.get(effect.kind):
            if type(effect.value) is int:
                changes.append(ActiveEffectChange(key=key, mode="override", value=effect.value))
        elif key := flag_keys.get(effect.kind):
            changes.append(ActiveEffectChange(key=key, mode="override", value=True))
        elif effect.kind == ConditionEffectKind.SPEED_ZERO:
            changes.append(ActiveEffectChange(key="speed.override", mode="override", value=0))
        elif effect.kind == ConditionEffectKind.IMMUNE_TO_CONDITION:
            for slug in dict.fromkeys(s.strip().lower() for s in effect.condition_slugs):
                if slug:
                    changes.append(
                        ActiveEffectChange(key="condition.immunity", mode="override", value=slug)
                    )
        elif save_keys := _SAVE_FLAG_KEYS.get(effect.kind):
            # Canonical scopes are ability codes. Normalize case/whitespace,
            # preserve their declaration order, and ignore unknown/empty scopes
            # rather than accidentally emitting a flag for every save.
            for ability in dict.fromkeys(a.strip().lower() for a in effect.abilities):
                if key := save_keys.get(ability):
                    changes.append(ActiveEffectChange(key=key, mode="override", value=True))
    return changes


def condition_immunity_slugs(changes: Iterable[ActiveEffectChange]) -> list[str]:
    """Read exact string overrides into stable, unique condition scopes."""
    return list(
        dict.fromkeys(
            change.value
            for change in changes
            if change.key == "condition.immunity"
            and change.mode == "override"
            and type(change.value) is str
            and change.value
        )
    )


def projected_boolean_flag(changes: Iterable[ActiveEffectChange], key: str) -> bool:
    """Read only an exact-key boolean True override; other modes/types are inert."""
    return any(
        change.key == key and change.mode == "override" and change.value is True
        for change in changes
    )


def projected_scalar_value(changes: Iterable[ActiveEffectChange], key: str) -> int | None:
    """Read the first integer override for an exact key, or None when absent.

    Booleans, strings, and other modes are inert. Declaration order determines
    the result, so duplicate clauses do not add or multiply their magnitudes.
    """
    return next(
        (
            change.value
            for change in changes
            if change.key == key and change.mode == "override" and type(change.value) is int
        ),
        None,
    )


def attack_distance_flag_applies(
    changes: Iterable[ActiveEffectChange],
    kind: ConditionEffectKind,
    distance_ft: int | None,
) -> bool:
    """Consume one scoped attack flag family; unknown distance is inert.

    Thresholds are integer overrides, never boolean flags or formula strings.
    Multiple matching clauses contribute only a boolean, without stacking.
    """
    key = _ATTACK_DISTANCE_FLAG_KEYS.get(kind)
    if key is None or distance_ft is None:
        return False
    return any(
        change.key == key
        and change.mode == "override"
        and type(change.value) is int
        and (
            distance_ft > change.value
            if kind == ConditionEffectKind.DISADVANTAGE_ATTACKS_AGAINST
            else distance_ft <= change.value
        )
        for change in changes
    )


def save_flag_abilities(
    changes: Iterable[ActiveEffectChange], kind: ConditionEffectKind
) -> list[str]:
    """Read a save flag family into stable, unique UPPER-case sidecar codes."""
    abilities_by_key = {key: code.upper() for code, key in _SAVE_FLAG_KEYS.get(kind, {}).items()}
    return list(
        dict.fromkeys(
            abilities_by_key[change.key]
            for change in changes
            if change.mode == "override" and change.value is True and change.key in abilities_by_key
        )
    )


def roll_dice_str(expr: str, rng: random.Random | None = None) -> int:
    """Roll a plain ``"NdM"`` / ``"NdM+K"`` / ``"NdM-K"`` expression.

    Pass a seeded ``random.Random`` as ``rng`` for a reproducible result. With
    ``rng=None`` the dice are drawn from the process-global ``random`` module,
    which is only reproducible if the caller seeds it. In-combat rolls never
    come through here — they use the seeded generator threaded from
    ``start_combat(rng_seed=...)``.
    """
    expr = expr.strip()
    if not expr:
        return 0
    sign = 1
    if expr.startswith("-"):
        sign = -1
        expr = expr[1:]
    if "+" in expr:
        dice_part, _, flat = expr.partition("+")
        flat_v = int(flat)
    elif "-" in expr[1:]:
        head = expr[0] if expr[0].isdigit() else ""
        rest = expr[len(head) :]
        dice_part, _, flat = rest.partition("-")
        dice_part = head + dice_part
        flat_v = -int(flat)
    else:
        dice_part = expr
        flat_v = 0
    n_str, _, d_str = dice_part.partition("d")
    n = int(n_str or "1")
    d = int(d_str)
    draw = (rng or random).randint
    total = sum(draw(1, d) for _ in range(n))
    return sign * (total + flat_v)


def filter_changes_by_bucket(
    effects: Iterable[ActiveEffect], bucket: str
) -> list[ActiveEffectChange]:
    """Return changes whose `key == bucket`, preserving order across
    effects then in-effect order."""
    out: list[ActiveEffectChange] = []
    for eff in effects:
        for ch in eff.changes:
            if ch.key == bucket:
                out.append(ch)
    return out


def _numeric_value(value: bool | int | str) -> int | None:
    """Coerce a `multiply`/`upgrade`/`downgrade` change value to an int.

    These three modes are scalar-only per the Foundry core semantics they
    port (`ActiveEffectChange` docstring: "int for scalar `add`/`multiply`");
    unlike `add`, they never carry dice formulas. Returns `None` when a
    string value doesn't parse as a plain integer.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(value.strip())
    except ValueError:
        return None


def apply_changes_to_check(
    base_total: int,
    bucket: str,
    effects: Iterable[ActiveEffect],
    rng: random.Random | None = None,
) -> tuple[int, list[str]]:
    """Fold `bucket`-matching `ActiveEffectChange`s into `base_total`.

    Modes:
      - `add` — int, bool, or dice-formula value added to the bucket's
        running contribution (bool/plain-int strings coerce to flat ints;
        strings containing "d" roll as dice formulas).
      - `override` — on a `flags.*` key, surfaces in the breakdown for
        narrator visibility but does not alter the total (advantage/
        disadvantage is a roll-mechanic flag, not a numeric change).
      - `multiply` — multiplies the bucket's accumulated numeric
        contribution SO FAR (not `base_total`, which may include an
        RNG-rolled die — multiplying the whole running total would make
        the result seed-dependent) by `ch.value`.
      - `upgrade` — raises the bucket's accumulated contribution to
        `max(contribution, ch.value)`; never lowers it.
      - `downgrade` — the mirror: `min(contribution, ch.value)`; never
        raises it.
      - `custom` — no Foundry-core semantics to port (Foundry delegates
        `custom` to host-registered callbacks); documented no-op, tracked
        as a Blocked backlog entry.

    Ordering: changes are applied in ascending `ActiveEffectChange.priority`
    order (ties preserve the input order — `sorted` is stable), mirroring
    Foundry's own apply-in-priority-order semantics. This lets a `multiply`
    or `upgrade`/`downgrade` change target contributions from earlier-
    priority `add` changes on the same bucket deterministically, independent
    of the effects' declaration order.

    Returns `(new_total, narrator breakdown lines)`.
    """
    contribution = 0
    breakdown: list[str] = []
    changes = sorted(filter_changes_by_bucket(effects, bucket), key=lambda ch: ch.priority)
    for ch in changes:
        if ch.mode == "add":
            if isinstance(ch.value, str):
                # some SRD asset templates
                # encode flat bonuses as plain integer strings ("1", "-1")
                # rather than dice formulas (Haste / Warding Bond / etc.).
                # Try integer parse first; fall back to the dice parser
                # only when the value contains a 'd'.
                stripped = ch.value.strip()
                if "d" in stripped:
                    rolled = roll_dice_str(ch.value, rng)
                    contribution += rolled
                    breakdown.append(f"effect({ch.value}:{rolled})")
                else:
                    try:
                        flat = int(stripped)
                    except ValueError:
                        breakdown.append(f"effect({ch.value}:unparsed)")
                        continue
                    contribution += flat
                    breakdown.append(f"effect({flat:+d})")
            elif isinstance(ch.value, bool):
                contribution += int(ch.value)
                breakdown.append(f"effect({int(ch.value):+d})")
            else:
                contribution += ch.value
                breakdown.append(f"effect({ch.value:+d})")
        elif ch.mode == "override":
            if ch.key.startswith("flags.advantage."):
                breakdown.append("effect(advantage)")
            elif ch.key.startswith("flags.disadvantage."):
                breakdown.append("effect(disadvantage)")
            else:
                breakdown.append(f"effect({ch.key}=override)")
        elif ch.mode == "multiply":
            factor = _numeric_value(ch.value)
            if factor is None:
                breakdown.append(f"effect({ch.value}:unparsed)")
                continue
            contribution *= factor
            breakdown.append(f"effect(x{factor})")
        elif ch.mode == "upgrade":
            target = _numeric_value(ch.value)
            if target is None:
                breakdown.append(f"effect({ch.value}:unparsed)")
                continue
            contribution = max(contribution, target)
            breakdown.append(f"effect(upgrade:{target})")
        elif ch.mode == "downgrade":
            target = _numeric_value(ch.value)
            if target is None:
                breakdown.append(f"effect({ch.value}:unparsed)")
                continue
            contribution = min(contribution, target)
            breakdown.append(f"effect(downgrade:{target})")
        # custom mode reserved per BACKLOG.md "## Blocked" — no Foundry-core
        # semantics to port (host-callback driven); documented no-op.
    return base_total + contribution, breakdown


def dedupe_by_identity(
    effects: Iterable[ActiveEffect],
) -> list[ActiveEffect]:
    """Dedupe by (target_id, id, origin) — Foundry-shaped identity tuple.
    Keeps the FIRST instance per identity."""
    seen: set[tuple[str, str, str]] = set()
    out: list[ActiveEffect] = []
    for eff in effects:
        key = (eff.target_id, eff.id, eff.origin)
        if key in seen:
            continue
        seen.add(key)
        out.append(eff)
    return out
