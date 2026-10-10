"""Damage application for the Activity resolver — partition by type, apply
target-side vulnerability / resistance / immunity, emit ``DamageApplied``.

SRD 5.2.1 applies resistance first (halved, rounded down), then vulnerability
(doubled); immunity yields zero. An ``"all"`` wildcard is honored in each list.
Every valid type emits DamageApplied, including immune zero damage. All types
in one hit share an instance ID and complete together through the typed callback.

The resolver merges static Combatant resistance/immunity lists with projected
sidecars. Static vulnerabilities are supplied by the live projection sidecar.

Unknown damage types (outside the SRD 13-type set) are logged with the
``damage_type_invalid`` marker and skipped — the rolled dict is keyed by free
strings supplied upstream, so an unrecognized key must be loud-but-non-fatal,
not silently dropped (and not raised: a single bad key must not abort the whole
multi-type application).
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Final, cast, get_args

from dnd5e_srd_data.schema.monster import MonsterTraitMechanic

from dnd5e_engine.activities.actor_stats import save_modifier
from dnd5e_engine.activities.context import DamageInstanceContext
from dnd5e_engine.activities.save_primitive import roll_save
from dnd5e_engine.events import DamageApplied, DamageType, LegendaryResistanceUsed, SaveRolled

if TYPE_CHECKING:
    from dnd5e_engine.activities.context import ActivityResolutionContext
    from dnd5e_engine.types.combat import Combatant

_LOGGER = logging.getLogger(__name__)

# SRD 5.1 §Damage Types — the closed 13-type set, sourced from the DamageType
# Literal so the two never drift.
_SRD_DAMAGE_TYPES: Final[frozenset[str]] = frozenset(get_args(DamageType))

# C22 — the SRD Bludgeoning / Piercing / Slashing triple. A magical source
# (``Weapon.magical`` or a spell) overcomes a target's B/P/S resistance when
# that resistance is the "nonmagical attacks" flavor (see
# ``Combatant.physical_resistances_nonmagical_only``).
_PHYSICAL_TYPES: Final[frozenset[str]] = frozenset({"bludgeoning", "piercing", "slashing"})


def _damage_immunities(target: Combatant, ctx: ActivityResolutionContext) -> set[str]:
    sidecar = ctx.passive_damage_modifiers.get(target.entity_id, {})
    return set(target.damage_immunities) | set(sidecar.get("immunities", ()))


def damage_type_can_apply(
    target: Combatant, damage_type: str, ctx: ActivityResolutionContext
) -> bool:
    """Whether a known type can deal damage, without rolling its amount.

    Attack riders use this same immunity/negation policy before sacrificing
    damage dice. Resistance may still reduce a later rolled amount to zero.
    """
    immunities = _damage_immunities(target, ctx)
    return (
        damage_type in _SRD_DAMAGE_TYPES
        and target.entity_id not in ctx.negated_spell_damage_targets
        and damage_type not in immunities
        and "all" not in immunities
    )


def _effective_resistances(
    target: Combatant, sidecar: dict[str, list[str]], *, magical: bool
) -> set[str]:
    """Static ``Combatant.damage_resistances`` + sidecar (effect-granted)
    resistances, minus the B/P/S entries a magical source overcomes.

    The nonmagical-only qualifier (``Combatant.physical_resistances_nonmagical_only``)
    applies ONLY to the static stat-block list. Effect-granted resistances
    (e.g. Rage's unconditional resistance to Bludgeoning / Piercing /
    Slashing) are always unconditional and are never bypassed by ``magical``.

    The orchestrator's ``_project_target_modifiers`` ALSO copies
    ``Combatant.damage_resistances`` into the sidecar's ``"resistances"``
    entry as a convenience (so a single sidecar dict carries every passive
    damage modifier). That copy is not itself effect-granted — it is the
    same static entry appearing twice — so it must not defeat the bypass.
    The genuinely effect-granted sidecar entries are therefore isolated by
    subtracting the target's own static list from the sidecar list BEFORE
    unioning back in; only what remains (added by an active effect, not by
    the stat block) is treated as unconditional.
    """
    static_resistances = set(target.damage_resistances)
    sidecar_resistances = set(sidecar.get("resistances", ()))
    effect_granted = sidecar_resistances - static_resistances
    if magical and target.physical_resistances_nonmagical_only:
        static_resistances -= _PHYSICAL_TYPES
    return static_resistances | effect_granted


def apply_damage(
    target: Combatant,
    rolled_by_type: dict[str, int],
    ctx: ActivityResolutionContext,
    *,
    magical: bool = False,
    source_id: str | None = None,
    is_crit: bool = False,
) -> int:
    """Apply a per-damage-type rolled amount to ``target`` and emit one
    ``DamageApplied`` per valid type.

    For each ``(damage_type, amount)``: validate the type against the SRD set
    (skip + log ``damage_type_invalid`` on miss), merge the static ``Combatant``
    resist/immune lists with the sidecar resist/immune/vuln lists, apply
    resist→vuln→immune, compute ``is_overkill``, and emit ``DamageApplied``.

    ``magical`` — the damage comes from a magic weapon (``Weapon.magical``) or
    a spell. SRD "resistance to Bludgeoning, Piercing, and Slashing from
    nonmagical attacks" does not apply to it (see
    ``Combatant.physical_resistances_nonmagical_only``); the ``"all"``
    wildcard (Petrified) and non-physical types are unaffected. This qualifier
    applies ONLY to the static stat-block resistance list — effect-granted
    (sidecar) resistances, such as Rage's, are always unconditional and are
    never bypassed by ``magical`` (see ``_effective_resistances``).

    ``source_id`` / ``is_crit`` — C15 damage-attribution metadata threaded
    straight into the emitted ``DamageApplied`` event(s); see
    ``DamageApplied.source_id`` / ``.is_crit`` for the source-id policy.

    Returns the TOTAL final (post-modifier) amount actually dealt across every
    valid type — C15 Task 6 (Vex): "hit a creature ... and deal damage to the
    creature" needs the AFTER-immunity total (a damage-immune target dealt 0
    final damage must not proc the rider), not the pre-modifier rolled sum.
    Existing callers that ignore the return are unaffected.
    """
    source_id = source_id if source_id is not None else ctx.activity_source_id
    sidecar = ctx.passive_damage_modifiers.get(target.entity_id, {})
    resistances = _effective_resistances(target, sidecar, magical=magical)
    immunities = _damage_immunities(target, ctx)
    vulnerabilities = set(sidecar.get("vulnerabilities", ()))

    total_dealt = 0
    damage_instance_id: str | None = None
    damage_types: list[DamageType] = []
    for damage_type_str, amount in rolled_by_type.items():
        if damage_type_str not in _SRD_DAMAGE_TYPES:
            _LOGGER.warning(
                "damage_type_invalid damage_type=%s target_id=%s",
                damage_type_str,
                target.entity_id,
            )
            continue
        srd_type = cast(DamageType, damage_type_str)
        if damage_instance_id is None:
            damage_instance_id = ctx.next_damage_instance_id(target.entity_id, source_id)
        damage_types.append(srd_type)
        final_amount = _apply_modifiers(amount, srd_type, resistances, immunities, vulnerabilities)
        if target.entity_id in ctx.negated_spell_damage_targets:
            final_amount = 0
        total_dealt += final_amount
        is_overkill = final_amount > target.hp_current
        # C18 §Monster action economy — SRD 5.2 stat-block trait "Undead
        # Fortitude": "If damage reduces the [monster] to 0 Hit Points, it
        # makes a Constitution saving throw (DC 5 plus the damage taken)
        # unless the damage is Radiant or from a Critical Hit. On a
        # successful save, the [monster] drops to 1 Hit Point instead."
        # One draw, gated so a non-triggering hit never touches ``ctx.rng``
        # (determinism — C18 global constraint). The ``DamageApplied``
        # emitted below still reports the FULL folded ``final_amount`` (the
        # narration is "took 12 damage but held on"); only ``is_overkill``
        # and ``target.hp_current`` reflect the trait's save.
        if (
            final_amount > 0
            and final_amount >= target.hp_current
            and MonsterTraitMechanic.UNDEAD_FORTITUDE in target.trait_mechanics
            and srd_type != "radiant"
            and not is_crit
        ):
            dc = 5 + final_amount
            # This is the monster trait's own save, independent of the incoming
            # damage's spell provenance. Pure contexts may omit save sidecars.
            save_modifiers = {
                **ctx.passive_save_modifiers,
                target.entity_id: {
                    "con": save_modifier(target, "con").total,
                    **ctx.passive_save_modifiers.get(target.entity_id, {}),
                },
            }
            save_ctx = replace(
                ctx,
                base_spell_level=None,
                save_is_magical=False,
                target_auto_success_ids=frozenset(),
                passive_save_modifiers=save_modifiers,
            )
            roll = roll_save(save_ctx, target, "con", dc, ignore_cover=True)
            succeeded = roll.succeeded
            ctx.event_emitter(
                SaveRolled(
                    target_id=target.entity_id,
                    ability="con",
                    dc=dc,
                    roll_total=roll.total,
                    succeeded=succeeded,
                    advantage=roll.mode,
                    natural=roll.natural,
                    modifier=roll.modifier,
                    sources=list(roll.sources),
                )
            )
            if roll.legendary_resistance_remaining is not None:
                ctx.event_emitter(
                    LegendaryResistanceUsed(
                        actor_id=target.entity_id,
                        uses_remaining=roll.legendary_resistance_remaining,
                    )
                )
            if succeeded:
                target.hp_current = 1
                is_overkill = False
                # Fix round 1 — live-combat write-back: the ORCHESTRATOR's
                # own HP fold (``_emit_apply_damage``) computes the
                # authoritative post-damage HP from ``live.tracked_hp`` and
                # this event's UNMODIFIED ``amount``, independent of this
                # snapshot ``target`` object; without this signal it would
                # still floor at 0 and fire Death. Setting the flag BEFORE
                # emitting ``DamageApplied`` below matters: the event
                # emitter call synchronously re-enters the orchestrator's
                # fold for THIS exact event.
                ctx.undead_fortitude_holds[target.entity_id] = True
        ctx.event_emitter(
            DamageApplied(
                target_id=target.entity_id,
                amount=final_amount,
                damage_type=srd_type,
                is_overkill=is_overkill,
                source_id=source_id,
                source_parent_id=ctx.source_parent_id,
                source_actor_id=ctx.caster.entity_id,
                damage_instance_id=damage_instance_id,
                is_crit=is_crit,
            )
        )
    if damage_instance_id is not None and ctx.damage_instance_resolved is not None:
        ctx.damage_instance_resolved(
            DamageInstanceContext(
                damage_instance_id=damage_instance_id,
                source_actor_id=ctx.caster.entity_id,
                target_id=target.entity_id,
                source_id=source_id,
                amount=total_dealt,
                damage_types=tuple(damage_types),
            )
        )
    return total_dealt


def _apply_modifiers(
    amount: int,
    damage_type: DamageType,
    resistances: set[str],
    immunities: set[str],
    vulnerabilities: set[str],
) -> int:
    """SRD 5.2.1: resistance (//2), vulnerability (×2), immunity (zero)."""
    if damage_type in resistances or "all" in resistances:
        amount //= 2
    if damage_type in vulnerabilities or "all" in vulnerabilities:
        amount *= 2
    if damage_type in immunities or "all" in immunities:
        amount = 0
    return amount
