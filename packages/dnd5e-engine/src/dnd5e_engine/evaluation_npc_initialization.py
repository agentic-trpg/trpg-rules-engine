"""Validate complete, explicitly bound NPC facts before initialization RNG."""

from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.common import AttackActivity
from dnd5e_srd_data.schema.monster import MonsterActionKind

from dnd5e_engine.evaluation_preflight import activity_support_failure
from dnd5e_engine.evaluation_state import NonCombatSnapshot
from dnd5e_engine.monster_rules import monster_rule_facts


def npc_initialization_failure(
    snapshot: NonCombatSnapshot, data_revision: str, loader: AssetLoader
) -> str | None:
    setup = snapshot.combat_setup
    assert setup is not None
    actors = {a.entity_id: a for a in snapshot.character_states}
    for binding in setup.npc_stat_blocks:
        actor = actors[binding.actor_id]
        monster = loader.get_monster(binding.monster_slug)
        if binding.data_revision != data_revision:
            return "NPC binding does not identify the pinned effective data revision"
        if monster is None or monster.slug != binding.monster_slug:
            return "NPC binding refers to an absent or inconsistent template identity"
        if actor.entity_type not in {"NPC", "Monster"}:
            return "NPC template binding requires an explicit encounter NPC/Monster actor"
        if (
            monster.special_abilities
            or monster.legendary_actions
            or monster.lair_actions
            or monster.legendary_action_uses
            or monster.legendary_resistance_uses
            or monster.spellcasting_ability
            or monster.movement.hover
            or not monster.actions
        ):
            return "complex template traits, legendary, spellcasting or movement are not migrated"
        if len({a.slug for a in monster.actions}) != len(monster.actions):
            return "ambiguous template action identity"
        for action in monster.actions:
            if (
                action.slug == "multiattack"
                or action.kind != MonsterActionKind.ACTION
                or action.mechanic
                or action.recharge
                or action.uses_per_day
                or action.legendary_cost
                or len(action.activities) != 1
                or not isinstance(action.activities[0], AttackActivity)
                or activity_support_failure(action.activities[0], stat_block=True)
            ):
                return "only ordinary explicit single-attack template actions are migrated"
        expected = monster_rule_facts(monster)
        expected.update(
            dexterity=monster.ability_scores.dex,
            hp_max=monster.hp,
            ac=monster.ac,
            creature_type=monster.creature_type,
            base_speed=monster.movement.walk,
            damage_resistances=list(monster.damage_resistances),
            damage_immunities=list(monster.damage_immunities),
            damage_vulnerabilities=list(monster.damage_vulnerabilities),
            condition_immunities=list(monster.condition_immunities),
            physical_resistances_nonmagical_only=False,
            attack_bonus=None,
        )
        for name, value in expected.items():
            actual = getattr(actor, name)
            if hasattr(value, "model_dump"):
                value, actual = value.model_dump(), actual.model_dump()
            if actual != value:
                return f"NPC actor contradicts pinned template mechanical fact: {name}"
        expected_senses = monster.senses.model_dump(exclude={"passive_perception"})
        if actor.senses.model_dump() != expected_senses:
            return "NPC actor contradicts pinned template senses"
        modifier = setup.initiative_modifiers[binding.actor_id]
        if modifier != monster.initiative_modifier:
            return "bound NPC requires its explicit canonical initiative modifier"
    return None
