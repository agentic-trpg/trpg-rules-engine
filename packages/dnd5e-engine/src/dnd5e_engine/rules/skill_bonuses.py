"""Always-on canonical skill check bonuses, projected without prose or RNG."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from dnd5e_srd_data.schema.common import PassiveEffectChange

from dnd5e_engine.events import Ability
from dnd5e_engine.rules.skills import SKILL_CODE_TO_SLUG, Skill
from dnd5e_engine.rules.uses import UsesRollData, evaluate_uses_formula

SKILL_CHECK_CHANGE_KEYS = {
    f"system.skills.{code}.bonuses.check": slug for code, slug in SKILL_CODE_TO_SLUG.items()
}


def skill_check_bonuses(
    changes: Sequence[PassiveEffectChange], ability_modifiers: Mapping[Ability, int]
) -> dict[Skill, int]:
    """Exact Foundry skill keys and ADD mode; only closed scalar expressions.

    Callers pass transfer effects only. Conditional Primal Knowledge has no
    activation/ability-substitution carrier and deliberately cannot enter here.
    """
    out: dict[Skill, int] = {}
    data = UsesRollData(ability_modifiers=ability_modifiers)
    for change in changes:
        skill = SKILL_CHECK_CHANGE_KEYS.get(change.key)
        if skill is None or change.mode != 2:
            continue
        amount = evaluate_uses_formula(change.value, data)
        if amount is None:
            raise ValueError(f"unresolvable always-on skill bonus: {change.key}={change.value}")
        out[skill] = out.get(skill, 0) + amount
    return out
