"""Seeded dice for the Activity resolver.

MIRRORS, does not import from, the existing ``effects/`` dice code:

* ``roll_expr`` parses a closed integer/dice grammar, then draws each die from
  the *passed-in* ``random.Random`` in declaration order. Validation is draw-free;
  neither Python eval nor a global RNG is used.
* ``roll_damage_part`` mirrors ``effects/damage.py:_roll_damage`` — apply scaling
  (cantrip / upcast) BEFORE crit doubling, then walk via the seeded rng. The crit
  doubler mirrors ``_apply_crit_doubling``: double each dice node's count only,
  never numeric modifiers (SRD §Critical Hits — dice twice, modifier once).

Damage-part → expression mirrors Foundry's ``shared/damage-field.mjs``
(``_automaticFormula`` / ``scaledFormula``): ``{number}d{denomination}`` with an
optional ``+{bonus}``, ``custom.formula`` overriding when ``custom.enabled``, and
``scaling.mode in {"whole","half"}`` driving how many dice an ``increase`` adds.
"""

from __future__ import annotations

import logging
import random
import re
from typing import TYPE_CHECKING

from .arithmetic import evaluate, parse_expression

if TYPE_CHECKING:
    from dnd5e_srd_data.schema.common import DamagePart, DamagePartBlock

_LOGGER = logging.getLogger(__name__)


def damage_part_to_expr(part: DamagePart | DamagePartBlock) -> str:
    """Build a ``d20`` dice expression string from a damage primitive.

    * Weapon ``DamagePart`` (``common.py:125``) carries a verbatim ``.dice``
      string ("1d8", "2d6+3") — passed through unchanged.
    * Activity ``DamagePartBlock`` (``common.py:313``) mirrors Foundry's
      ``DamageData._automaticFormula``: ``{number}d{denomination}`` plus an
      optional ``+{bonus}``; ``custom.formula`` wins when ``custom.enabled``.
    """
    dice = getattr(part, "dice", None)
    if dice is not None:
        return str(dice)
    return _block_to_expr(part)  # type: ignore[arg-type]


def _block_to_expr(part: DamagePartBlock, *, die_increase: int = 0) -> str:
    """Foundry ``DamageData._automaticFormula(increase)`` — number+increase dice
    of the configured denomination, with the bonus appended."""
    if part.custom.enabled:
        formula = part.custom.formula
        if die_increase:
            formula = _bump_first_die_count(formula, die_increase)
        return formula

    number = (part.number or 0) + die_increase
    formula = ""
    if number and part.denomination:
        formula = f"{number}d{part.denomination}"
    if part.bonus:
        formula = f"{formula}+{part.bonus}" if formula else part.bonus
    return formula


def _bump_first_die_count(formula: str, die_increase: int) -> str:
    """Foundry ``scaledFormula`` custom-branch: increase the leading die count.

    Mirrors ``formula.replace(/^(\\d)+d/, n => (n + dieIncrease) + 'd')`` — only
    the first die term's count is bumped; the rest of the formula is untouched.
    """

    def _bump(match: re.Match[str]) -> str:
        return f"{int(match.group(1)) + die_increase}d"

    return re.sub(r"^(\d+)d", _bump, formula, count=1)


def validate_expression(expr: str, *, allow_dice: bool = True) -> None:
    """Validate the complete evaluator grammar without drawing any dice."""
    parse_expression(expr, allow_dice=allow_dice)


def roll_expr(expr: str, rng: random.Random, *, crit: bool = False) -> int:
    """Validate the whole expression, then draw dice in declaration order."""
    return evaluate(parse_expression(expr), rng, crit=crit)


def roll_damage_part(
    part: DamagePart | DamagePartBlock,
    rng: random.Random,
    *,
    crit: bool = False,
    die_floor: int | None = None,
    character_level: int | None = None,
    slot_level: int | None = None,
    base_level: int | None = None,
) -> int:
    """Roll a damage part against ``rng``, applying scaling then crit doubling.

    Order mirrors ``effects/damage.py:_roll_damage``: cantrip / upcast scaling is
    folded into the dice count FIRST, then crit doubling doubles the (already
    scaled) dice count. Crit doubles dice only, never the modifier.

    * ``die_floor`` raises every rolled face to at least that value — Great
      Weapon Fighting's 3.
    * ``character_level`` drives cantrip scaling (SRD §Cantrips: 1 die ≤4, 2
      dice 5–10, 3 dice 11–16, 4 dice 17+), applied via the part's
      ``scaling.number`` step size — mirrors Foundry treating cantrip growth as
      an ``increase`` of (tier − 1).
    * ``slot_level`` / ``base_level`` drive upcast scaling. ``increase`` =
      slots above base, halved for ``scaling.mode == "half"``. There are two
      Foundry scaling mechanisms; a part uses whichever its ``scaling`` block
      sets. The SRD corpus is XOR (a part sets one or the other), and the guard
      below ENFORCES that XOR — if both are set, ``scaling.formula`` wins and the
      dice-count path is skipped (see ``_scaling_die_increase``):
        - ``scaling.number`` set (and no ``scaling.formula``) → each step adds
          ``scaling.number`` dice of the part's denomination (Foundry
          ``scaledFormula`` die-count branch).
        - ``scaling.formula`` set → each step adds the (already @-token-resolved)
          ``scaling.formula`` once, rolled off the same seeded rng so a dice
          formula stays deterministic (Foundry ``scaledFormula`` formula branch).
    """
    steps = _scaling_steps(
        part,
        character_level=character_level,
        slot_level=slot_level,
        base_level=base_level,
    )
    die_increase = _scaling_die_increase(part, steps)
    expr = (
        _block_to_expr(part, die_increase=die_increase)  # type: ignore[arg-type]
        if getattr(part, "dice", None) is None
        else str(part.dice)  # type: ignore[union-attr]
    )

    node = parse_expression(expr)
    scaling = getattr(part, "scaling", None)
    if scaling is not None and steps and scaling.formula:
        validate_expression(scaling.formula)
    total = evaluate(node, rng, crit=crit, die_floor=die_floor)
    return total + _scaling_formula_bonus(part, steps, rng)


def _scaling_steps(
    part: DamagePart | DamagePartBlock,
    *,
    character_level: int | None,
    slot_level: int | None,
    base_level: int | None,
) -> int:
    """Number of scaling steps above base for ``part``.

    Weapon ``DamagePart`` has no scaling block → never scales (0 steps). For a
    ``DamagePartBlock`` the step count is gated by the spell's BASE level so the
    two SRD scaling mechanisms never both fire on one part:

    * cantrip (``base_level == 0`` — the EXPLICIT cantrip marker):
      ``character_level`` → SRD tier dice count → steps = tier−1. A leveled
      spell's ``character_level`` is inert here.
    * leveled spell (``base_level >= 1``): ``slot_level − base_level``, halved
      when ``scaling.mode == "half"``. The cantrip table is never consulted.
    * non-spell (``base_level is None`` — item / monster activity): NO scaling
      at all, even when a ``character_level`` is passed. Items don't cantrip-scale
      (an explicit cantrip carries ``base_level == 0``), and a ``None`` base level
      cannot drive the upcast path either.

    A non-``half``/``whole`` mode with scaling content is unsupported; when such
    a part is upcast it is logged loudly and yields 0 steps rather than silently
    mis-scaling.
    """
    scaling = getattr(part, "scaling", None)
    if scaling is None:
        return 0

    # Cantrip scaling requires the EXPLICIT cantrip marker ``base_level == 0``.
    # ``base_level is None`` is a non-spell (item / monster) activity: a passed
    # ``character_level`` must NOT cantrip-scale it (regression — item activities
    # like Ring of the Ram / basic poison carry no spell level but still pass the
    # caster's level through ``resolve_attack`` / ``resolve_damage``).
    if base_level == 0 and character_level is not None:
        return _cantrip_dice_count(character_level) - 1
    if slot_level is not None and base_level is not None and base_level >= 1:
        steps = max(slot_level - base_level, 0)
        if scaling.mode == "half":
            return steps // 2
        if scaling.mode in ("whole", ""):
            return steps
        if steps and (scaling.number is not None or scaling.formula):
            _LOGGER.warning(
                "scaling_mode_unsupported mode=%s number=%s formula=%s",
                scaling.mode,
                scaling.number,
                scaling.formula,
            )
        return 0
    return 0


def _scaling_die_increase(part: DamagePart | DamagePartBlock, steps: int) -> int:
    """Foundry ``scaledFormula`` die-count delta: ``scaling.number * steps``.

    Defaults the per-step die count to 1 when ``scaling.number`` is unset (cantrip
    growth). A part that scales via ``scaling.formula`` contributes NO extra dice
    here — its growth is the flat bonus, not dice.

    ``scaling.formula`` and the dice-count path are treated as MUTUALLY EXCLUSIVE:
    when a formula is set it WINS and the dice-count path is skipped entirely, even
    if ``scaling.number`` is also set. The canonical SRD corpus is XOR (a part sets
    one or the other), so live cases are unaffected; the guard only stops a
    hypothetical both-set block from over-scaling (extra dice AND an extra formula).
    This diverges from Foundry's ``scaledFormula``, which sums both when both are
    set — a deliberate choice: nothing in the corpus exercises that branch, and
    over-scaling silently is the worse failure mode.
    """
    scaling = getattr(part, "scaling", None)
    if scaling is None or not steps:
        return 0
    # ``formula`` set → flat-formula path wins; add no dice (XOR guard).
    if scaling.formula:
        return 0
    # ``number`` unset (and no formula): cantrip growth defaults to 1 die/step.
    if scaling.number is None:
        return steps
    return int(scaling.number) * steps


def _scaling_formula_bonus(
    part: DamagePart | DamagePartBlock, steps: int, rng: random.Random
) -> int:
    """Foundry ``scaledFormula`` formula branch: add ``scaling.formula`` per step.

    The formula's @-tokens are resolved upstream (``formula.resolve_damage_block``)
    before this point; a literal ("5") folds to its value and a dice formula draws
    from the seeded ``rng`` so determinism holds. Added ``steps`` times.
    """
    scaling = getattr(part, "scaling", None)
    if scaling is None or not steps or not scaling.formula:
        return 0
    # Roll the formula once per step (independent draws when it carries dice;
    # identical to multiply for the flat literals in the SRD corpus).
    return sum(roll_expr(scaling.formula, rng) for _ in range(steps))


def _cantrip_dice_count(character_level: int) -> int:
    """SRD §Cantrips scaling table — mirrors ``effects/damage.py:_cantrip_dice_count``."""
    if character_level < 5:
        return 1
    if character_level < 11:
        return 2
    if character_level < 17:
        return 3
    return 4
