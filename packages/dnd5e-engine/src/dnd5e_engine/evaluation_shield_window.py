"""Private post-hit checkpoint for a player's Shield decision on an NPC attack."""

from collections.abc import Awaitable, Callable
from hashlib import sha256

from dnd5e_srd_data.loader import AssetLoader

from dnd5e_engine.activities.context import AttackHitContext
from dnd5e_engine.evaluation import _evaluate, evaluate
from dnd5e_engine.evaluation_contracts import (
    CombatIntentPayload,
    RuleEvaluationRequest,
    RuleEvaluationResult,
)
from dnd5e_engine.evaluation_payments import spell_payment
from dnd5e_engine.evaluation_reaction_state import reaction_state
from dnd5e_engine.evaluation_ruleset import get_default_ruleset, verify_ruleset
from dnd5e_engine.evaluation_shield import (
    has_shield,
    shield_source_id,
    shield_support_failure,
)
from dnd5e_engine.evaluation_state import CombatSnapshot
from dnd5e_engine.reaction_effect_rules import provisional_attack_evidence
from dnd5e_engine.reactions import PendingReaction
from dnd5e_engine.rules.conditions import conditions_block_actions


class _ShieldPauseError(Exception):
    """Stop a private attack before damage and carry its original hit."""

    def __init__(self, token: str):
        """Carry an opaque binding to the original hit and RNG."""
        self.token = token


def shield_response_request(
    request: RuleEvaluationRequest, loader: AssetLoader
) -> RuleEvaluationRequest | None:
    """Return the existing canonical predeclaration for an eligible PC defender."""
    snapshot, payload = request.state_snapshot, request.payload
    if (
        not isinstance(snapshot, CombatSnapshot)
        or snapshot.snapshot_schema_version != "engine-snapshot/20"
        or not isinstance(payload, CombatIntentPayload)
        or payload.intent_type != "attack"
        or payload.stat_block_action_id is None
        or request.actor_id not in snapshot.combat_state.encounter_ids
        or payload.target_id not in snapshot.combat_state.party_ids
        or snapshot.resource_state is None
        or has_shield(snapshot)
        or snapshot.effect_states
        or snapshot.combat_state.pending_reactions
    ):
        return None
    verify_ruleset(request.ruleset_binding, loader)
    owner = next(a for a in snapshot.character_states if a.entity_id == payload.target_id)
    if (
        not owner.reaction_available
        or not owner.is_alive
        or owner.hp_current <= 0
        or owner.entity_id in snapshot.combat_state.dead_ids
        or conditions_block_actions([condition.condition for condition in owner.conditions])
    ):
        return None
    source = shield_source_id(snapshot, owner.entity_id)
    if source is None:
        return None
    try:
        spell_payment(snapshot.resource_state, owner.entity_id, 1)
    except ValueError:
        return None
    spell = loader.get_spell("shield")
    if spell is None or spell != get_default_ruleset().get_spell("shield"):
        return None
    activity = spell.activities[0]
    if activity.reaction is None or not activity.activation.reaction_conditions:
        return None
    declaration = reaction_state(
        PendingReaction(
            owner_id=owner.entity_id,
            spell_id=spell.slug,
            activity_id=activity.id,
            slot_level=1,
            conditions=tuple(activity.activation.reaction_conditions),
            semantics=activity.reaction,
        ),
        spell_source_id=source,
    )
    armed = snapshot.model_copy(
        update={
            "combat_state": snapshot.combat_state.model_copy(
                update={"pending_reactions": [declaration]}
            )
        }
    )
    if shield_support_failure(armed, loader):
        return None
    return request.model_copy(update={"state_snapshot": armed})


def _checkpoint(request: RuleEvaluationRequest, attack: AttackHitContext) -> str:
    """Bind the original attack and RNG input to the observed hit."""
    digest = sha256(request.model_dump_json().encode("utf-8"))
    evidence = provisional_attack_evidence(attack)
    assert evidence is not None
    digest.update(evidence.model_dump_json().encode("utf-8"))
    return digest.hexdigest()


async def evaluate_for_shield(
    request: RuleEvaluationRequest,
    *,
    loader: AssetLoader,
    ordinary_evaluator: Callable[[RuleEvaluationRequest], Awaitable[RuleEvaluationResult]]
    | None = None,
) -> RuleEvaluationResult | str:
    """Return an ordinary result or the private checkpoint of an eligible NPC hit."""
    armed = shield_response_request(request, loader)
    if armed is None:
        if ordinary_evaluator is not None:
            return await ordinary_evaluator(request)
        return await evaluate(request, loader=loader)

    def pause(attack: AttackHitContext) -> None:
        """Capture a provisional hit before damage or candidate effect payment."""
        raise _ShieldPauseError(_checkpoint(request, attack))

    try:
        return await _evaluate(request, loader=loader, before_hit=pause)
    except _ShieldPauseError as signal:
        return signal.token


async def answer_shield(
    request: RuleEvaluationRequest,
    token: str,
    *,
    accept: bool,
    loader: AssetLoader,
) -> tuple[RuleEvaluationRequest, RuleEvaluationResult]:
    """Reproduce the original hit from its fixed input, then settle exactly once."""
    armed = shield_response_request(request, loader)
    if armed is None:
        raise ValueError("Shield response requires the original eligible attack")
    reproduced = False

    def verify_hit(attack: AttackHitContext) -> None:
        """Fail at the original hit boundary before any damage or Shield payment."""
        nonlocal reproduced
        if reproduced or _checkpoint(request, attack) != token:
            raise ValueError("Shield response diverged from its original hit")
        reproduced = True

    fixed = armed if accept else request
    result = await _evaluate(fixed, loader=loader, before_hit=verify_hit)
    if not reproduced or result.status != "accepted":
        raise ValueError("Shield response could not resolve the original hit")
    return fixed, result
