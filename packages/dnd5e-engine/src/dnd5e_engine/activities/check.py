"""Canonical CheckActivity adapter for the shared typed ability-check pipeline.

First target rolls when present, otherwise the caster. Canonical ability/skill
and DC adapters remain pure and public; explicit standalone modifier sidecars
are supported. Live checks use typed actor state and real Combatant magnitudes.
Effect riders retain their existing placement after the authoritative check.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final, get_args

from dnd5e_engine.activities.check_pipeline import activity_check_modifier, resolve_check_request
from dnd5e_engine.activities.dice import roll_expr
from dnd5e_engine.activities.effects import apply_activity_effects
from dnd5e_engine.activities.formula import resolve_roll_data
from dnd5e_engine.events import Ability
from dnd5e_engine.rules.skills import SKILL_CODE_TO_SLUG
from dnd5e_engine.types.checks import CheckRequest

if TYPE_CHECKING:
    from dnd5e_srd_data.schema.common import CheckActivity

    from .context import ActivityResolutionContext

_LOGGER = logging.getLogger(__name__)

# The closed set of SRD ability codes, sourced from the Ability Literal so the
# validation and the event field never drift.
_ABILITIES: Final[frozenset[str]] = frozenset(get_args(Ability))

# Foundry 3-letter skill code → governing SRD ability. Source: CONFIG.DND5E.skills
# in foundry/module/config.mjs (each skill's ``ability`` field). The canonical
# ``check.associated`` field carries these codes verbatim. Mirrors
# ``effects/check.py:_SKILL_TO_ABILITY`` (which keys the legacy evaluator long-form names).
_SKILL_TO_ABILITY: Final[dict[str, Ability]] = {
    "acr": "dex",
    "ani": "wis",
    "arc": "int",
    "ath": "str",
    "dec": "cha",
    "his": "int",
    "ins": "wis",
    "itm": "cha",
    "inv": "int",
    "med": "wis",
    "nat": "int",
    "prc": "wis",
    "prf": "cha",
    "per": "cha",
    "rel": "int",
    "slt": "dex",
    "ste": "dex",
    "sur": "wis",
}

# Foundry 3-letter skill code → canonical SRD skill slug. The translation now
# lives in ``rules/skills.py`` (``SKILL_CODE_TO_SLUG``), shared with the sheet
# derivation (``build_spec.py``) — this used to be the single site that read
# the sidecar by skill; it no longer is, but the sidecar shape it feeds
# (``ctx.check_modifiers[actor]["skills"]``, keyed by the long-form slug) is
# unchanged.

# Test-determinism seam for the natural check d20 (our own code; effects/check.py
# has none and relies on a seeded ctx.rng).
FORCE_CHECK_D20: Final = "force_check_d20"
_check_modifier = activity_check_modifier  # retained compatibility seam


def resolve_check(activity: CheckActivity, ctx: ActivityResolutionContext) -> None:
    """Roll one ability/skill check, emit ``CheckRolled``, apply effect riders.

    The DC is resolved once (it may be ``None`` for an informational check). One
    actor — the first target when present, else the caster — rolls a natural d20
    (honoring ``force_check_d20``) plus its skill-or-ability modifier off
    ``ctx.check_modifiers``. ``succeeded`` is ``total >= dc`` (or ``None`` when
    there is no DC). The activity's effect riders then fire on that actor.
    """
    dc = _resolve_dc(activity, ctx)
    skill, ability = _resolve_skill_ability(activity)
    actor = ctx.targets[0] if ctx.targets else ctx.caster

    slug = SKILL_CODE_TO_SLUG.get(skill or "")
    request = ctx.check_request or CheckRequest(
        actor_id=actor.entity_id,
        ability=ability,
        skill=slug,
        tool=skill if skill and slug is None else None,
        dc=dc,
        context="activity",
    )
    resolve_check_request(
        request, ctx, cost_owner="activity", activity_id=activity.id, skill_label=skill
    )

    # Riders apply to the rolling actor (e.g. manacles "Bind" → Restrained on the
    # bound creature). A check carries no save outcome, so the on-save gate is
    # inert (``save_succeeded=None`` applies unconditionally).
    cast_level = ctx.slot_level or ctx.base_spell_level or 0
    apply_activity_effects(activity, ctx, actor, save_succeeded=None, cast_level=cast_level)


# ── DC resolution ─────────────────────────────────────────────────────────────


def _resolve_dc(
    activity: CheckActivity, ctx: ActivityResolutionContext, *, allow_dice: bool = True
) -> int | None:
    """Resolve ``check.dc`` to a concrete int, or ``None`` for a no-DC check.

    Mirrors Foundry ``check-data.mjs`` prepareFinalData:

    * ``"spellcasting"`` → ``8 + prof + ability_mod(spellcasting_ability)`` (needs
      a caster spellcasting ability; absent → ``ValueError``).
    * ``"flat"`` → the parsed ``check.dc.formula`` (@-tokens resolved off the
      seeded rng; a flat DC carries no dice in the SRD corpus).
    * EMPTY calculation → the flat ``formula`` when one is present (Foundry's
      ``simplifyBonus(formula)`` branch — every canonical check ships
      ``calculation=""`` + a literal formula); ``None`` only when calculation AND
      formula are BOTH empty (a no-DC informational check).
    * any other calculation → ``ValueError`` (loud; never silently default).
    """
    calculation = activity.check.dc.calculation
    formula = activity.check.dc.formula

    if calculation == "spellcasting":
        if ctx.spellcasting_ability is None:
            raise ValueError(
                "check.dc.calculation == 'spellcasting' requires a caster "
                "spellcasting ability but the context supplies none"
            )
        return 8 + ctx.caster_proficiency_bonus + ctx.ability_mod(ctx.spellcasting_ability)

    if calculation in ("flat", ""):
        if not formula:
            # Empty calculation AND empty formula → a no-DC informational check.
            return None
        resolved = resolve_roll_data(formula, ctx, ability=ctx.spellcasting_ability)
        if not allow_dice and "d" in resolved.lower():
            raise ValueError("live check DC must be a scalar")
        return roll_expr(resolved, ctx.rng)

    raise ValueError(
        f"check.dc.calculation {calculation!r} is not resolvable "
        f"(expected 'spellcasting', 'flat', or empty)"
    )


def _resolve_skill_ability(activity: CheckActivity) -> tuple[str | None, Ability]:
    """The (skill, ability) the check is rolled with.

    ``check.associated[0]`` (when present) names the skill — a Foundry 3-letter
    code whose governing ability comes from ``_SKILL_TO_ABILITY``. With no
    associated skill (a raw-ability check) the ability is ``check.ability``,
    validated against the closed Ability set. A check naming neither a resolvable
    skill nor an explicit ability cannot be rolled and raises.

    An ``associated`` entry that is NOT a known skill code (a tool slug) falls
    back to the explicit ``check.ability``; the slug is still surfaced as the
    ``CheckRolled.skill`` label.
    """
    associated = activity.check.associated
    skill = associated[0] if associated else None

    if skill is not None and skill in _SKILL_TO_ABILITY:
        return skill, _SKILL_TO_ABILITY[skill]

    # Raw-ability check, or a tool slug whose ability is named explicitly.
    ability = activity.check.ability
    if ability not in _ABILITIES:
        raise ValueError(
            f"check has no resolvable ability: associated={associated!r} "
            f"ability={ability!r} (expected a known skill code or one of "
            f"{sorted(_ABILITIES)})"
        )
    return skill, ability  # type: ignore[return-value]


# Public, pure adapters also used by pre-payment live validation.
check_dc = _resolve_dc
check_skill_ability = _resolve_skill_ability
