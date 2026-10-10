"""Shared action legality, independent of runtime policy lookup."""

from dnd5e_engine.events import AttackFailed, CastFailed, CombatEvent
from dnd5e_engine.intents import IntentRejectedError, PlayerIntent
from dnd5e_engine.types.combat import Combatant


def action_economy_gate_failure(
    current: Combatant,
    intent: PlayerIntent,
    *,
    is_bonus_action: bool,
    is_reaction_cast: bool,
    extra_action: bool = False,
) -> CombatEvent | None:
    """The action-economy budget gate for ``submit_player_intent``: returns
    the rejection event to emit (turn-keeping), or ``None`` when the intent
    may proceed to budget consumption. Raises ``IntentRejectedError`` for
    the cases with no typed event surface today: a non-cast, non-attack
    Action-costed intent with no Action left, and the FIRST swing of an
    Attack action with no Action left (fix round 1 — restores the pre-C14
    hard Action gate so a turn-keeping Action intent, e.g. Dash or
    Disengage, cannot be chained into a free attack sequence).

    ``attack`` gets its own branch (SRD §Extra Attack, R2): an exhausted
    ``attacks_remaining`` is always a turn-KEEPING ``AttackFailed`` —
    unlike every other Action-costed intent, a later same-Action swing
    owes no further Action spend, so its rejection must not look like a
    fresh "no Action" failure. But the FIRST swing (``attack_action_engaged``
    False) still owes the Action itself, exactly like every other
    Action-costed intent.

    FINAL-REVIEW FIX (F1): ``"pass"`` is exempt from every branch below —
    it was never an Action ("I'm done" needs no budget) and it must ALWAYS
    be accepted and end the turn. Without this exemption, every turn-
    keeping intent (attack/move/cast_spell/drop_concentration/dash/...)
    that leaves ``action_available`` False with nothing left to spend it on
    (e.g. after a multi-attack actor's swings, or after a plain Dash) has
    no way to end the turn: the generic ``not current.action_available``
    branch below would hard-reject ``pass`` itself, deadlocking the turn.

    SRD 5.2 Action Surge (C20): with the base Action spent, an unspent extra
    action admits any Action-costed intent but a Magic action, and an attack
    whose Attack action's swings are spent takes another Attack action on it
    (``_consume_attack_budget``).
    """
    if intent.intent_type == "pass":
        return None
    if is_bonus_action:
        if not current.bonus_action_available:
            return CastFailed(
                actor_id=current.entity_id,
                spell_id=intent.spell_id or "",
                reason="no_action_economy",
            )
        return None
    if is_reaction_cast:
        if not current.reaction_available:
            return CastFailed(
                actor_id=current.entity_id,
                spell_id=intent.spell_id or "",
                reason="no_action_economy",
            )
        return None
    if intent.intent_type == "attack":
        if current.attacks_remaining <= 0 and not current.action_available and not extra_action:
            return AttackFailed(
                actor_id=current.entity_id,
                target_id=intent.target_id,
                reason="no_action_economy",
            )
        # SRD §Action Economy — the FIRST swing of the Attack action still
        # owes the hard Action requirement (fix round 1: a turn-keeping
        # Action intent — Dash, Disengage — must not let a same-turn attack
        # sequence resolve for free). Subsequent swings this Action
        # (``attack_action_engaged`` True) skip this: the Action was
        # already paid for, or soft-consumed, by the first swing.
        if not current.attack_action_engaged and not current.action_available and not extra_action:
            raise IntentRejectedError(
                "no_action_economy",
                f"actor_id={current.entity_id!r} has no Action remaining this turn",
            )
        return None
    if not current.action_available and not extra_action:
        if intent.intent_type == "cast_spell":
            return CastFailed(
                actor_id=current.entity_id,
                spell_id=intent.spell_id or "",
                reason="no_action_economy",
            )
        raise IntentRejectedError(
            "no_action_economy",
            f"actor_id={current.entity_id!r} has no Action remaining this turn",
        )
    return None
