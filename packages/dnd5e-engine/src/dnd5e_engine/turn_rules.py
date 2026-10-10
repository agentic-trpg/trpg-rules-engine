"""Pure turn budget and initiative rules shared by both execution entries."""

from collections.abc import Sequence, Set
from typing import Any

from dnd5e_engine.types.combat import Combatant


def reset_turn_budget(c: Combatant, *, effective_speed: int, attacks: int) -> Combatant:
    return c.model_copy(
        update={
            "action_available": True,
            "bonus_action_available": True,
            "reaction_available": True,
            "action_taken_this_turn": False,
            "bonus_action_taken_this_turn": False,
            "attack_action_attacks_made": 0,
            "action_grants_spent": (),
            "action_grant_groups_spent": (),
            # SRD §Movement — the budget refreshes to the actor's
            # EFFECTIVE Speed (Speed-0 conditions, Exhaustion) at the
            # start of their turn. Per-MOVE-intent decrement is the
            # only other writer; this is the only reset.
            "movement_remaining": effective_speed,
            # SRD §Disengage — "for the rest of the turn"; this is
            # the start of a NEW turn, so the suppression lapses.
            "disengaging_this_turn": False,
            # SRD §Extra Attack / §Two-Weapon Fighting — refresh the
            # per-Action attack budget and clear the TWF window at
            # the start of the actor's own turn.
            "attacks_remaining": attacks,
            "attack_action_engaged": False,
            "attack_rolls_made_this_turn": 0,
            "light_weapon_swing_slug": None,
            "restricted_light_weapon_swing_slug": None,
            "offhand_attack_spent": False,
            # SRD §Actions in Combat — Dodge: "until the start of
            # your next turn". The reset here, at the dodger's OWN
            # turn start, is the exact SRD expiry point.
            "dodging": False,
            # Loading starts a fresh per-Action allowance each turn.
            "loading_weapon_fired_this_action": False,
            # SRD 5.2 §Weapon Mastery — Cleave: "only once per turn";
            # the cap resets at the actor's own TurnStarted (C15
            # Task 7).
            "cleave_spent_this_turn": False,
            # SRD 5.2 Flurry of Blows — strikes still owed lapse at
            # the actor's own turn start.
            "flurry_strikes_remaining": 0,
            # SRD 5.2 Action Surge — an unspent additional action and
            # the once-per-turn mark lapse at the actor's turn start.
            "extra_actions_remaining": 0,
            "action_surge_used_this_turn": False,
        }
    )


def next_turn_index(ids: Sequence[str], dead: Set[str], start: int) -> tuple[int, bool] | None:
    if not ids:
        return None
    index = start
    for _ in ids:
        if ids[index % len(ids)] not in dead:
            return index % len(ids), index >= len(ids)
        index += 1
    return None


def spend_attack_budget(
    c: Combatant, *, new_action: bool, attacks: int, payment: dict[str, Any]
) -> Combatant:
    if new_action:
        update = {
            **payment,
            "attack_action_engaged": True,
            "loading_weapon_fired_this_action": False,
            "attacks_remaining": attacks - 1,
            "attack_action_attacks_made": 1,
        }
    else:
        update = {"attacks_remaining": c.attacks_remaining - 1}
    update.setdefault("attack_action_attacks_made", c.attack_action_attacks_made + 1)
    return c.model_copy(update=update)


def ordinary_action_payment(c: Combatant, *, extra_action: bool = False) -> dict[str, Any]:
    if extra_action:
        return {
            "extra_actions_remaining": c.extra_actions_remaining - 1,
            "action_taken_this_turn": True,
        }
    if c.action_available:
        return {"action_available": False, "action_taken_this_turn": True}
    return {}


def record_budget_changes(before: Combatant, after: Combatant) -> Combatant:
    fields = {}
    if (
        before.action_available and not after.action_available
    ) or before.extra_actions_remaining > after.extra_actions_remaining:
        fields["action_taken_this_turn"] = True
    if before.bonus_action_available and not after.bonus_action_available:
        fields["bonus_action_taken_this_turn"] = True
    return after.model_copy(update=fields)


def bonus_action_payment() -> dict[str, bool]:
    """The ordinary Bonus Action payment shared with the Stateful compatibility entry."""
    return {"bonus_action_available": False, "bonus_action_taken_this_turn": True}
