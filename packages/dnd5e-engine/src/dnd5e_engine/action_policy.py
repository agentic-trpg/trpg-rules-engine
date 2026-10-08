"""Effect-owned action constraints shared by public, monster and reaction paths."""

from typing import TYPE_CHECKING, Literal

from dnd5e_engine.rules.effects import effective_effects

if TYPE_CHECKING:
    from dnd5e_srd_data.schema.action_policy import ActionPolicy
    from dnd5e_srd_data.schema.item import Weapon
    from dnd5e_srd_data.schema.spell import Spell

    from dnd5e_engine.orchestrator import AttackFunding, PlayerIntent, _ActionCost, _LiveCombat
    from dnd5e_engine.types.combat import Combatant


def policies(live: "_LiveCombat", actor_id: str) -> tuple["ActionPolicy", ...]:
    return tuple(
        e.action_policy
        for e in effective_effects(live.active_effects.get(actor_id, ()))
        if not e.disabled and e.action_policy is not None
    )


def denial(
    live: "_LiveCombat",
    actor: "Combatant",
    cost: Literal["action", "bonus", "reaction"],
    *,
    attack: bool = False,
) -> bool:
    for policy in policies(live, actor.entity_id):
        own_turn = live.current_actor_id == actor.entity_id
        if cost == "reaction" and policy.deny_reactions:
            return True
        if cost == "bonus" and (
            policy.deny_bonus_actions
            or (own_turn and policy.action_or_bonus and actor.action_taken_this_turn)
        ):
            return True
        if cost == "action" and (
            policy.deny_actions
            or (own_turn and policy.action_or_bonus and actor.bonus_action_taken_this_turn)
        ):
            return True
        if (
            attack
            and policy.attack_count_cap is not None
            and actor.attack_action_attacks_made >= policy.attack_count_cap
            and not actor.action_available
            and actor.extra_actions_remaining <= 0
        ):
            return True
    return False


def validate_grant(live: "_LiveCombat", actor: "Combatant", intent: "PlayerIntent") -> None:
    """An explicit full effect identity selects one live restricted Action."""
    from dnd5e_engine.orchestrator import IntentRejectedError

    if intent.action_grant is None:
        return
    grant = next(
        (
            e.action_policy.extra_action
            for e in effective_effects(live.active_effects.get(actor.entity_id, ()))
            if not e.disabled
            and (e.id, e.origin) == intent.action_grant
            and e.action_policy is not None
        ),
        None,
    )
    if (
        grant is None
        or intent.action_grant in actor.action_grants_spent
        or intent.intent_type not in grant.actions
        or intent.use_bonus_action
        or intent.intent_type not in ("attack", "dash", "disengage", "hide")
    ):
        raise IntentRejectedError(
            "action_restricted", "invalid, spent or incompatible action grant"
        )
    enforce(live, actor, "action")


def grant_payment(actor: "Combatant", grant: tuple[str, str]) -> dict[str, object]:
    return {
        "action_grants_spent": (*actor.action_grants_spent, grant),
        "action_taken_this_turn": True,
    }


def enforce(
    live: "_LiveCombat",
    actor: "Combatant",
    cost: Literal["action", "bonus", "reaction"],
    *,
    attack: bool = False,
) -> None:
    from dnd5e_engine.orchestrator import IntentRejectedError

    if denial(live, actor, cost, attack=attack):
        raise IntentRejectedError("action_restricted", "active effect restricts this action")


def preflight_intent_policy(
    live: "_LiveCombat",
    actor: "Combatant",
    intent: "PlayerIntent",
    cost: "_ActionCost",
    funding: "AttackFunding",
    weapon: "Weapon | None",
) -> "AttackFunding":
    """Funding decides whether an attack belongs to the Action or Bonus Action."""
    if intent.action_grant is not None:
        funding = "action"
    if (
        funding == "light_offhand"
        and weapon is not None
        and weapon.mastery == "nick"
        and needs_new_attack_action(live, actor)
    ):
        from dnd5e_engine.orchestrator import IntentRejectedError

        if intent.use_bonus_action or not (actor.action_available or actor.extra_actions_remaining):
            raise IntentRejectedError("action_restricted", "Attack action attack cap reached")
        # A normal request may start another paid Attack Action. Explicit
        # Light/Nick requests remain bound to the exhausted previous Action.
        funding = "action"
    if not cost.is_free_action and intent.intent_type != "pass":
        bonus = (
            cost.is_bonus_action
            or funding in ("flurry", "martial_arts_bonus", "construct_bonus")
            or (
                funding == "light_offhand" and not (weapon is not None and weapon.mastery == "nick")
            )
        )
        payment: Literal["action", "bonus", "reaction"] = (
            "reaction" if cost.is_reaction_cast else "bonus" if bonus else "action"
        )
        enforce(
            live,
            actor,
            payment,
            attack=intent.intent_type in ("attack", "grapple", "shove")
            and payment == "action"
            and intent.action_grant is None,
        )
    return funding


def attack_cap(live: "_LiveCombat", actor_id: str) -> int | None:
    caps = [p.attack_count_cap for p in policies(live, actor_id) if p.attack_count_cap is not None]
    return min(caps) if caps else None


def needs_new_attack_action(live: "_LiveCombat", actor: "Combatant") -> bool:
    cap = attack_cap(live, actor.entity_id)
    return cap is not None and actor.attack_action_attacks_made >= cap


def attack_budget(live: "_LiveCombat", actor: "Combatant", base: int) -> int:
    cap = attack_cap(live, actor.entity_id)
    return min(cap, base) if cap is not None else base


def has_action_grant(live: "_LiveCombat", actor: "Combatant") -> bool:
    return not denial(live, actor, "action") and any(
        not e.disabled
        and e.action_policy is not None
        and e.action_policy.extra_action is not None
        and bool(
            set(e.action_policy.extra_action.actions) & {"attack", "dash", "disengage", "hide"}
        )
        and (e.id, e.origin) not in actor.action_grants_spent
        for e in effective_effects(live.active_effects.get(actor.entity_id, ()))
    )


def record_budget_changes(live: "_LiveCombat", before: "Combatant | None") -> None:
    from dnd5e_engine import orchestrator as orch

    if before is None:
        return
    after = orch._find_combatant(live, before.entity_id)
    if after is None:
        return
    fields = {}
    if (
        before.action_available and not after.action_available
    ) or before.extra_actions_remaining > after.extra_actions_remaining:
        fields["action_taken_this_turn"] = True
    if before.bonus_action_available and not after.bonus_action_available:
        fields["bonus_action_taken_this_turn"] = True
    if fields:
        orch._update_combatant(live, before.entity_id, **fields)


def somatic_chance(live: "_LiveCombat", actor_id: str, spell: "Spell") -> int:
    return (
        max((p.somatic_failure_percent for p in policies(live, actor_id)), default=0)
        if "S" in spell.components
        else 0
    )


def fail_somatic_attempt(live: "_LiveCombat", actor_id: str, spell: "Spell", chance: int) -> bool:
    from dnd5e_engine import orchestrator as orch
    from dnd5e_engine.events import CastFailed, SomaticSpellRolled

    if not chance:
        return False
    roll = live.rng.randint(1, 100)
    failed = roll <= chance
    orch._emit(
        live,
        SomaticSpellRolled(
            actor_id=actor_id, spell_id=spell.slug, roll=roll, failure_percent=chance, failed=failed
        ),
    )
    if failed:
        orch._emit(
            live, CastFailed(actor_id=actor_id, spell_id=spell.slug, reason="somatic_failure")
        )
    return failed
