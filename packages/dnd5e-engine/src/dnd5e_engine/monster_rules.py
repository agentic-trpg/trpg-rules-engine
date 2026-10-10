"""Shared canonical monster mechanical facts, without hydration or runtime state."""

from typing import Any

from dnd5e_srd_data.schema.monster import Monster

from dnd5e_engine.activities.actor_stats import skill_ability
from dnd5e_engine.activities.passive_stats import CombatantMovementModes
from dnd5e_engine.rules.dice import ability_modifier


def monster_rule_facts(monster: Monster) -> dict[str, Any]:
    """Original template projection; explicit Host overrides remain Legacy-only."""
    sc = monster.ability_scores
    return {
        "creature_size": monster.creature_size,
        "movement_modes": CombatantMovementModes(
            climb=monster.movement.climb,
            swim=monster.movement.swim,
            fly=monster.movement.fly,
            burrow=monster.movement.burrow,
        ),
        "strength": sc.str,
        "constitution": sc.con,
        "intelligence": sc.int,
        "wisdom": sc.wis,
        "charisma": sc.cha,
        "proficiency_bonus_override": monster.proficiency_bonus,
        "save_proficiencies": [
            a
            for a in ("str", "dex", "con", "int", "wis", "cha")
            if getattr(monster.saving_throws, a) is not None
        ],
        "skill_proficiencies": [k for k, v in monster.skills.model_dump().items() if v is not None],
        "skill_expertise": [
            k
            for k, v in monster.skills.model_dump().items()
            if v is not None
            and (ability := skill_ability(k)) is not None
            and v == ability_modifier(getattr(sc, ability)) + 2 * monster.proficiency_bonus
        ],
        "trait_mechanics": [
            a.mechanic for a in monster.special_abilities if a.mechanic is not None
        ],
        # SRD §Spellcasting — the ability a monster's innate/
        # prepared spells key off (C18 Task 5). ``None`` for a
        # template with no cast-bearing actions (unchanged
        # ``Combatant`` default).
        "spellcasting_ability": monster.spellcasting_ability,
    }
