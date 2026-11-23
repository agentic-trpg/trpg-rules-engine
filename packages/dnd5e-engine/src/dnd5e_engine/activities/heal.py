"""``heal`` kind handler for the Activity resolver.

Foundry ``heal-data.mjs`` stores healing as a single ``DamagePartBlock``
(``healing``), not a list. The ``types`` list carries a
single token: ``"healing"`` for HP restoration, ``"temphp"`` for a temporary
HP buffer — both confirmed against canonical SRD 5.2 data (cure-wounds.json
emits ``"healing"``; false-life.json emits ``"temphp"``). When ``temphp`` is
present we emit ``TempHpApplied``, otherwise ``HealingApplied``.

MIRRORS, does not import from, ``effects/temphp.py`` for the temp-hp token
distinction. The keep-higher replacement policy lives only on the legacy
``effects/`` path (which carries an existing-temp-HP lookup); the typed
Activity resolver emits the rolled grant directly, matching how ``apply.py``
emits the rolled magnitude for damage.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from dnd5e_srd_data.schema.monster import MonsterTraitMechanic

from dnd5e_engine.activities.dice import roll_damage_part
from dnd5e_engine.activities.effects import apply_activity_effects
from dnd5e_engine.activities.formula import resolve_damage_block
from dnd5e_engine.events import (
    HealingApplied,
    HealingDieDraw,
    HealingRollDetail,
    RollFixedModifier,
    TempHpApplied,
)

if TYPE_CHECKING:
    from dnd5e_srd_data.schema.common import HealActivity

    from .context import ActivityResolutionContext

_LOGGER = logging.getLogger(__name__)

# Foundry healing type token for a temporary HP grant (vs "healing" for HP
# restoration). Confirmed as the only temp-hp marker in canonical SRD 5.2 data.
_TEMP_HP_TYPE: Final[str] = "temphp"


@dataclass(frozen=True)
class HealingModifiers:
    """Validated changes to one recipient's immediate HP restoration."""

    bonus: int = 0
    maximize_dice: bool = False


def resolve_heal(
    activity: HealActivity,
    ctx: ActivityResolutionContext,
    *,
    modifiers: Mapping[str, HealingModifiers] | None = None,
    record_roll_detail: bool = False,
) -> None:
    """Resolve one heal per target, with caller-supplied modifiers only for HP restoration.

    No-op only when the healing block carries nothing to restore: no dice
    (``number``/``denomination``), no ``bonus``, AND no active ``custom.formula``.
    A custom-only block (Heroism's ``custom.formula == "@mod"`` temp-HP grant) is
    NOT empty — its magnitude lives entirely in ``custom.formula``.
    ``record_roll_detail`` captures only the reviewed additive exploration spell
    profile; maximized dice have no raw RNG faces and retain absent detail.
    """
    healing = activity.healing
    has_dice = healing.number is not None and healing.denomination is not None
    has_custom = healing.custom.enabled and bool(healing.custom.formula)
    if not has_dice and not healing.bonus and not has_custom:
        return

    # The healing formulas carry Foundry roll-data tokens (Cure Wounds' ``@mod``
    # bonus, Heroism's ``@mod`` custom formula, the flat upcast ``scaling.formula``);
    # resolve them against the caster's spellcasting ability before the dice helper
    # parses them. ``roll_damage_part`` stays ctx-agnostic.
    healing = resolve_damage_block(healing, ctx, ability=ctx.spellcasting_ability)

    is_temp_hp = _TEMP_HP_TYPE in healing.types
    cast_level = ctx.slot_level or ctx.base_spell_level or 0
    unmodified = HealingModifiers()
    for target in ctx.targets:
        # C18 §Monster action economy — SRD 5.2 stat-block trait "Swarm":
        # "The swarm can't regain Hit Points or gain Temporary Hit Points"
        # (e.g. swarm-of-rats.json). A swarm target never gains HP or temp
        # HP from a heal activity. Skipped BEFORE rolling — a swarm target
        # draws no dice (determinism: only a target actually carrying the
        # trait changes the seeded stream).
        if MonsterTraitMechanic.SWARM in target.trait_mechanics:
            _LOGGER.info("swarm_no_hp_gain target_id=%s", target.entity_id)
            continue
        contribution = (
            modifiers.get(target.entity_id, unmodified)
            if modifiers and not is_temp_hp
            else unmodified
        )
        additive_profile = (
            record_roll_detail
            and not is_temp_hp
            and not contribution.maximize_dice
            and not healing.custom.enabled
            and not healing.scaling.formula
        )
        draws: list[tuple[int, int]] | None = [] if additive_profile else None
        amount = (
            roll_damage_part(
                healing,
                ctx.rng,
                slot_level=ctx.slot_level,
                base_level=ctx.base_spell_level,
                maximize_dice=contribution.maximize_dice,
                die_draws=draws,
            )
            + contribution.bonus
        )
        if is_temp_hp:
            ctx.event_emitter(TempHpApplied(target_id=target.entity_id, amount=amount))
        else:
            detail = None
            if additive_profile and draws is not None:
                # The reviewed exploration Cure Wounds profile is additive dice + resolved @mod;
                # no custom expression or scaling formula is interpreted as a bonus.
                detail = HealingRollDetail(
                    dice=tuple(HealingDieDraw(die_size=size, face=face) for size, face in draws),
                    fixed_addends=(
                        RollFixedModifier(source="spell_bonus", value=int(healing.bonus or 0)),
                        RollFixedModifier(source="recipient_bonus", value=contribution.bonus),
                    ),
                )
            ctx.event_emitter(
                HealingApplied(target_id=target.entity_id, amount=amount, roll_detail=detail)
            )
        # Heal carries effect riders too (Aid's +max-HP, Heroism's frightened-
        # immunity): apply them PER target AFTER that target's heal event, matching
        # save.py's event-then-rider order. Heal has no save, so save_succeeded=None.
        apply_activity_effects(activity, ctx, target, save_succeeded=None, cast_level=cast_level)
