"""Shared attack rules. No runtime, registry, event queue or external state."""

from collections.abc import Callable, Collection, Iterable, Mapping, Sequence, Set
from typing import Any

from dnd5e_srd_data.schema.common import ActivationBlock, AttackActivity, SaveActivity
from dnd5e_srd_data.schema.item import Weapon, WeaponProperty

from dnd5e_engine.activities.actor_stats import proficiency_bonus_of
from dnd5e_engine.activities.conjuration import StatBlockMagnitudes
from dnd5e_engine.rules.conditions import (
    active_condition_names,
    conditions_block_actions,
    project_passive_damage_modifiers,
)
from dnd5e_engine.spatial import SpatialTopology
from dnd5e_engine.types.combat import Combatant


def weapon_attack_range_ft(weapon: Weapon | None) -> tuple[int, int] | None:
    """Resolve the effective attack range BANDS for a typed weapon, in feet.

    Returns ``(normal, max)`` — SRD 5.2 §Range: "Your attack roll has
    Disadvantage when your target is beyond normal range, and you can't
    attack a target beyond long range." A distance ``<= normal`` rolls
    plain; ``normal < distance <= max`` rolls with Disadvantage (a LEGAL
    attack — ``"range:long"`` in ``attack.py``); ``distance > max`` is
    illegal (``AttackFailed(reason="out_of_range")``).

    Reads the typed ``Weapon.range`` block (lib loader):

      * a melee weapon (``range.kind == "melee"``) WITHOUT the ``thrown``
        property reaches 5ft, or 10ft when it carries the ``reach``
        property (glaive/halberd/pike) — both bands equal, so an ordinary
        melee swing never rolls the disadvantage tier. ``range.value`` is
        NOT the melee reach — Foundry leaves it ``None`` for standard melee
        and reuses it for the THROWN range on thrown weapons (dagger=20,
        handaxe=20), so deriving reach from the ``reach`` property
        reproduces the old wrapper's ``reach_ft`` (5/10) faithfully;
      * a ranged weapon (crossbow/bow) carries ``range.value`` as its
        normal band and ``range.long`` as its max band (falling back to
        ``range.value`` when a weapon carries no distinct long band);
      * a MELEE weapon WITH the ``thrown`` property (dagger, handaxe) can
        also be thrown (SRD §Thrown): within melee reach it's an ordinary
        melee swing, and beyond reach out to ``range.value`` it's an
        ordinary (un-penalized) thrown attack, so its normal band is
        ``max(reach, range.value)``; the disadvantage tier then runs from
        there out to ``range.long`` (falling back to ``range.value`` — or
        ``reach`` if that too is absent). SRD §Thrown also pins the
        governing ability: "use the same ability modifier for the attack
        and damage rolls that you use for a melee attack with that
        weapon" — automatic here, since ``_weapon_default_ability`` keys
        off ``weapon_category``/``finesse``, not distance.

    Returns ``None`` when the weapon is missing or carries no usable
    range — the orchestrator skips the gate in that case.
    """
    if weapon is None:
        return None
    rng = weapon.range
    if rng.kind == "melee":
        reach = weapon_melee_reach_ft(weapon)
        if WeaponProperty.THROWN not in weapon.properties:
            return reach, reach
        thrown_normal = rng.value if isinstance(rng.value, int) and rng.value > 0 else None
        if thrown_normal is None:
            return reach, reach
        thrown_band = max(reach, thrown_normal)
        thrown_long = rng.long if isinstance(rng.long, int) and rng.long > 0 else thrown_normal
        return thrown_band, max(thrown_band, thrown_long)
    ranged_normal = rng.value if isinstance(rng.value, int) and rng.value > 0 else None
    if ranged_normal is None:
        return None
    long_band = rng.long if isinstance(rng.long, int) and rng.long > 0 else ranged_normal
    return ranged_normal, max(ranged_normal, long_band)


def weapon_melee_reach_ft(weapon: Weapon) -> int:
    """SRD 5.2 Reach property: "This weapon adds 5 feet to your reach when you
    attack with it" — 10 ft for a Reach weapon, 5 ft for any other."""
    return 10 if WeaponProperty.REACH in weapon.properties else 5


def versatile_grip_applies(weapon: Weapon | None, distance_ft: int | None) -> bool:
    """SRD 5.2 Versatile — "The weapon deals that damage when used with two
    hands to make a melee attack."

    ``True`` only when ``weapon`` carries ``WeaponProperty.VERSATILE`` AND
    this particular swing is an actual melee attack, not a ranged one. A
    Versatile weapon is always melee-kind, but a handful (Spear, Trident)
    ALSO carry Thrown, so the same weapon can be thrown at range — reuses
    the reach-band classification from ``weapon_attack_range_ft``: beyond
    melee reach (5ft, or 10ft with Reach) the swing is a thrown attack, and
    a two-handed grip declared for it is ignored (SRD "to make a melee
    attack"). ``distance_ft is None`` (an untracked position) is treated as
    within reach.
    """
    if weapon is None or WeaponProperty.VERSATILE not in weapon.properties:
        return False
    if WeaponProperty.THROWN in weapon.properties and distance_ft is not None:
        reach = 10 if WeaponProperty.REACH in weapon.properties else 5
        if distance_ft > reach:
            return False
    return True


def monster_attack_range_ft(activities: Sequence[Any], melee_reach_ft: int) -> int | None:
    """Resolve a monster turn's effective attack range from typed activities.

    The range gate keys off the FIRST offensive activity the turn will resolve
    (multiattack fans out to homogeneous sub-attacks, so the first activity's
    range governs the whole turn — matching the legacy single ``range_ft`` the
    loader wrapper carried). Only an explicit ``AttackActivity`` yields a
    finite reach the movement gate should honor:

      * an explicit numeric ``units == "ft"`` range (e.g. a ``"80"`` shortbow
        band) is used verbatim;
      * Foundry melee attacks ship ``units == "self"`` / no value (reach is
        implied), so they fall back to the monster's ``Combatant.melee_reach_ft``
        (5 by default, 10 for reach creatures) — reproducing the old
        ``range_ft == 5`` melee wrappers.

    A non-``AttackActivity`` offensive activity (a ``SaveActivity``)
    splits two ways:

      * a self-centered AoE (breath weapon: ``range.units == "self"`` OR a
        populated ``target.template.type``) carries NO movement reach — the
        monster resolves the save/effect from its current position, so we
        return ``None`` and the caller skips the gate (treating a self/template
        AoE as melee reach was the regression that forced dragons to close to
        5ft);
      * a ranged single-target save (giant-spider web ~60ft, mummy
        dreadful-glare ~30ft: ``range.units == "ft"`` with a real positive
        value and no measured template) is a genuine ranged gate — the monster
        must be within that range and closes the distance if it is not.

    Returns ``None`` when no offensive activity carries a usable finite reach —
    the caller then skips the movement gate (the legacy ``range_ft`` absence
    did the same).
    """
    for activity in activities:
        if not isinstance(activity, (AttackActivity, SaveActivity)):
            continue
        if not isinstance(activity, AttackActivity):
            # A non-attack offensive activity (SaveActivity). Two shapes:
            #   * self-centered AoE (breath weapon): ``range.units == "self"``
            #     OR a measured ``target.template.type`` — resolves from
            #     position, NO movement gate (return None);
            #   * ranged single-target save (giant-spider web ~60ft, mummy
            #     dreadful-glare ~30ft): ``range.units == "ft"`` with a real
            #     positive value and no measured template — a real ranged gate
            #     the monster must close to satisfy.
            rng = activity.range
            template_type = activity.target.template.type
            if rng.units == "self" or template_type:
                return None
            if rng.units == "ft" and rng.value is not None:
                try:
                    parsed = int(rng.value)
                except ValueError:
                    parsed = 0
                if parsed > 0:
                    return parsed
            return None
        rng = activity.range
        if rng.units == "ft":
            value = rng.value
            if value is not None:
                try:
                    parsed = int(value)
                except ValueError:
                    parsed = 0
                if parsed > 0:
                    return parsed
            # ``units == "ft"`` with an empty/zero value is an explicit "no
            # range" datum, not a melee attack — fall through to reach.
        # Foundry melee (``units == "self"``) or an unusable ft value: the
        # monster's reach governs.
        return melee_reach_ft if melee_reach_ft > 0 else None
    return None


def in_range_with_los(topology: SpatialTopology, a: str, b: str, range_ft: int) -> bool:
    """True iff ``b`` is within ``range_ft`` of ``a``, ``a`` has line of sight to
    ``b``, AND ``b`` does not have total cover from ``a``.

    The single range+LoS+cover predicate every attack/cast gate routes
    through. SRD 5.2 §Cover: a target with total cover "can't be targeted
    directly" — reuses the same rejection surface (``AttackFailed(reason=
    "out_of_range")``) every other range/LoS rejection already uses. On a
    backend/scene with no wall or cover geometry, ``has_line_of_sight`` is
    always True and ``cover_between`` is always ``"none"``, so this is
    behaviour-identical to a bare ``within_range`` (byte-for-byte preserved).
    """
    return (
        topology.within_range(a, b, range_ft)
        and topology.has_line_of_sight(a, b)
        and topology.cover_between(a, b) != "total"
    )


def synthesize_attack_from_weapon(weapon: Weapon) -> AttackActivity:
    """Build a base-weapon ``AttackActivity`` for a weapon with no
    activities of its own.

    A handful of magic weapons (frost-brand, flame-tongue, …) ship empty
    ``activities`` because their attack rides the base mundane weapon they
    enchant. A bare ``AttackActivity`` (empty ``attack.ability`` ⇒ the
    resolver picks the weapon's SRD default ability; empty ``damage.parts``
    with ``include_base=True`` ⇒ the handler rolls ``weapon.damage_parts``)
    reproduces the OLD ``_synthesize_weapon_attack`` behavior: one melee/ranged
    swing dealing the weapon's own dice plus the governing-ability mod.
    """
    return AttackActivity(
        id=f"synth:{weapon.slug}",
        activation=ActivationBlock(type="action", value=1),
    )


def is_proficient_with_weapon(current: Combatant, weapon: Weapon | None) -> bool:
    """SRD 5.2 §Weapon Proficiency — "Anyone can wield a weapon, but you must
    have proficiency with it to add your Proficiency Bonus to an attack roll
    you make with it." (packs/_source/content24/chapter-6/equipment.yml, id
    dWQ2ZTLOuKr3PMAx). "A monster is proficient with any weapon in its stat
    block" (same source) — monsters never carry an explicit
    ``weapon_proficiencies`` list, so ``current.weapon_proficiencies is None``
    covers them for free.

    ``current.weapon_proficiencies is None`` is the C15 R1 sentinel: the host
    never opted into enforcement (``PartyMemberSpec.weapon_proficiencies``
    unset), so proficiency is assumed — this reproduces every pre-C15
    fixture byte-identically. ``weapon is None`` covers non-weapon resolution
    paths (spells, features) where proficiency never applies. Otherwise,
    proficient iff the weapon's category or its own slug is in the caster's
    explicit (possibly empty — "proficient in nothing") list.
    """
    if current.weapon_proficiencies is None or weapon is None:
        return True
    return (
        weapon.weapon_category in current.weapon_proficiencies
        or weapon.slug in current.weapon_proficiencies
    )


def attack_action_is_spent(current: Combatant) -> bool:
    """Whether the paid Attack action has no remaining swings."""
    return not current.attack_action_engaged or current.attacks_remaining <= 0


def damage_modifiers(actors: Iterable[Combatant]) -> dict[str, dict[str, list[str]]]:
    return {c.entity_id: static_damage_modifiers(c) for c in actors if static_damage_modifiers(c)}


def dodge_benefit_active(actor: Combatant, *, effective_speed: int) -> bool:
    """Dodge is lost while Incapacitated or at Speed zero, regardless of movement spent."""
    return (
        actor.dodging
        and not conditions_block_actions(active_condition_names(actor.conditions))
        and effective_speed > 0
    )


def hostile_adjacent_to_attacker(
    caster: Combatant,
    actors: Iterable[Combatant],
    *,
    allies: Collection[str],
    dead_ids: Set[str],
    positions: Mapping[str, str],
    topology: SpatialTopology,
    can_see: Callable[[Combatant, Combatant], bool],
) -> bool:
    if not allies or (origin := positions.get(caster.entity_id)) is None:
        return False
    for hostile in actors:
        if hostile.entity_id in allies or hostile.entity_id in dead_ids or not hostile.is_alive:
            continue
        if conditions_block_actions(active_condition_names(hostile.conditions)):
            continue
        destination = positions.get(hostile.entity_id)
        if destination is None or not topology.within_range(origin, destination, 5):
            continue
        if can_see(hostile, caster):
            return True
    return False


def static_damage_modifiers(c: Combatant) -> dict[str, list[str]]:
    cond_names = [ac.condition for ac in c.conditions]
    damage_proj = project_passive_damage_modifiers(cond_names)
    # Merge per-creature damage_resistances / damage_immunities (from the
    # monster/character stat block) into the condition-derived projection.
    # SRD §Damage Resistance / §Damage Immunity — both sources are
    # additive (resistance + resistance does not stack per SRD, but
    # union-set membership reflects that correctly: the handler only
    # checks set membership, not count).
    if c.damage_resistances:
        merged_res = list(damage_proj.get("resistances", []) or [])
        for dt in c.damage_resistances:
            if dt not in merged_res:
                merged_res.append(dt)
        damage_proj["resistances"] = merged_res
    if c.damage_immunities:
        merged_imm = list(damage_proj.get("immunities", []) or [])
        for dt in c.damage_immunities:
            if dt not in merged_imm:
                merged_imm.append(dt)
        damage_proj["immunities"] = merged_imm
    # fold the creature's static damage vulnerabilities into the same
    # sidecar (the ONLY producer — vulnerability has no condition-derived
    # source). ``apply.py`` reads ``sidecar["vulnerabilities"]`` and doubles a
    # matching hit. Mirrors the resistances/immunities merge above exactly.
    if c.damage_vulnerabilities:
        merged_vuln = list(damage_proj.get("vulnerabilities", []) or [])
        for dt in c.damage_vulnerabilities:
            if dt not in merged_vuln:
                merged_vuln.append(dt)
        damage_proj["vulnerabilities"] = merged_vuln
    return damage_proj


def stat_block_magnitudes(
    current: Combatant,
    *,
    proficiency_bonus: int | None = None,
    attack_bonus: int | None = None,
    transformed: bool = False,
) -> StatBlockMagnitudes:
    return StatBlockMagnitudes(
        ability_scores={
            "str": current.strength,
            "dex": current.dexterity,
            "con": current.constitution,
            "int": current.intelligence,
            "wis": current.wisdom,
            "cha": current.charisma,
        },
        proficiency_bonus=proficiency_bonus_of(current)
        if proficiency_bonus is None
        else proficiency_bonus,
        attack_bonus=attack_bonus if transformed else current.attack_bonus,
    )
