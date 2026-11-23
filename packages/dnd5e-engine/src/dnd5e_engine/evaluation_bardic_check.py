"""Draw-free check facts and the isolated Bardic Inspiration choice calculation.

The host retains the original failed event, checks grant identity and authorization,
and adopts the uncommitted event and RNG transition together with any grant spending.
"""

from dataclasses import asdict
from hashlib import sha256
from json import dumps

from dnd5e_srd_data.loader import AssetLoader

from dnd5e_engine.activities.actor_stats import _SCORE_ATTR, check_modifier
from dnd5e_engine.activities.check_pipeline import validate_check_effect_change
from dnd5e_engine.evaluation_actor import combatant
from dnd5e_engine.evaluation_checks import _admit_check
from dnd5e_engine.evaluation_contracts import CheckPayload, RuleEvaluationRequest
from dnd5e_engine.evaluation_rng import RNGContext, RNGTransition
from dnd5e_engine.evaluation_ruleset import verify_ruleset
from dnd5e_engine.events import CheckRolled, RollDieModifier
from dnd5e_engine.rules.conditions import (
    conditions_grant_disadvantage_on_ability_checks,
    d20_test_penalty,
)
from dnd5e_engine.rules.effects import effective_effects

BARDIC_INSPIRATION_DIE_SIDES = frozenset((6, 8, 10, 12))
BARDIC_INSPIRATION_SOURCE = "feature_grant:bardic-inspiration"


def check_bardic_fingerprint(request: RuleEvaluationRequest, loader: AssetLoader) -> str:
    """Validate and hash the fixed rule inputs of a supported untargeted exploration check.

    The digest compares an original and freshly prepared request without restoring RNG.
    It omits world versions, RNG, unrelated actors, and roller facts unused by the check.
    Unsupported or incomplete inputs raise ValueError before a digest is returned.
    """
    payload = request.payload
    if (
        request.operation_kind != "rules.check"
        or not isinstance(payload, CheckPayload)
        or payload.dc is None
        or payload.target_id is not None
    ):
        raise ValueError("Bardic Inspiration requires an explicit untargeted rules.check")
    verify_ruleset(request.ruleset_binding, loader)
    admission = _admit_check(request, loader)
    match admission:
        case (actor, _, states):
            pass
        case {"error": error}:
            raise ValueError(error.reason if error is not None else "check inputs are not admitted")
    roller = combatant(actor)
    state = states[actor.entity_id]
    conditions = [condition.condition for condition in roller.conditions]
    changes = [
        (effect.id, effect.origin, change.model_dump(mode="json"))
        for effect in effective_effects(state.effects)
        for change in effect.changes
        if validate_check_effect_change(change, payload)
    ]
    facts = {
        "ruleset": request.ruleset_binding.model_dump(mode="json"),
        "request": payload.model_dump(mode="json"),
        "ability_score": getattr(roller, _SCORE_ATTR[payload.ability]),
        "modifier": asdict(check_modifier(roller, payload.ability, payload.skill)),
        "exhaustion": d20_test_penalty(roller.conditions),
        "condition_disadvantage": conditions_grant_disadvantage_on_ability_checks(
            conditions, fear_source_in_sight=state.fear_source_in_sight
        ),
        "armor_disadvantage": payload.skill == "stealth" and roller.stealth_disadvantage,
        "reliable_talent": roller.reliable_talent and payload.skill in roller.skill_proficiencies,
        "sight": sorted(
            (fact.need.observer_id, fact.need.source_id, fact.visible)
            for fact in request.creature_sight_facts
        ),
        "changes": changes,
    }
    encoded = dumps(facts, sort_keys=True, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


def resolve_bardic_check_choice(
    original: CheckRolled, die_sides: int, rng_context: RNGContext, *, use: bool
) -> tuple[CheckRolled, RNGTransition]:
    """Decline or add one Bardic Inspiration die to an already failed check.

    Reject an ineligible die size, check, or repeated application before drawing.
    Use the caller's current stream. The original roll and its modifiers stay
    fixed and detached from the returned evidence. A nonzero inspiration bonus
    records that the grant was rolled and must be spent even on continued failure.
    """
    if type(die_sides) is not int or die_sides not in BARDIC_INSPIRATION_DIE_SIDES:
        raise ValueError("unsupported Bardic Inspiration die")
    detail = original.roll_detail
    d20 = detail.d20 if detail is not None else None
    if (
        original.context != "generic"
        or original.cost_owner != "internal"
        or original.target_id is not None
        or original.required_sense != "none"
        or original.activity_id is not None
        or original.auto_failure is not None
        or original.succeeded is not False
        or original.dc is None
        or original.roll_total >= original.dc
        or original.inspiration_bonus != 0
        or original.natural is None
        or original.modifier is None
        or original.natural + original.modifier != original.roll_total
        or detail is None
        or detail.status != "rolled"
        or d20 is None
        or d20.origin != "rng"
        or not 0 <= d20.kept_index < len(d20.faces)
        or any(not 1 <= face <= 20 for face in d20.faces)
        or d20.faces[d20.kept_index] != original.rolled_natural
        or d20.faces[d20.kept_index] + sum(item.value for item in detail.modifiers)
        != original.roll_total
        or any(item.source == BARDIC_INSPIRATION_SOURCE for item in detail.modifiers)
    ):
        raise ValueError("Bardic Inspiration requires an unmodified failed exploration d20 check")

    event = original.model_copy(deep=True)
    if not use:
        return event, RNGTransition(
            stream_id=rng_context.stream_id,
            input_version=rng_context.version,
            input_state=rng_context.state,
            next_state=rng_context.state,
            state_changed=False,
        )
    rng = rng_context.state.restore()
    face = rng.randint(1, die_sides)
    total = original.roll_total + face
    assert event.roll_detail is not None
    event.roll_detail.modifiers.append(
        RollDieModifier(source=BARDIC_INSPIRATION_SOURCE, sides=die_sides, face=face, value=face)
    )
    return (
        event.model_copy(
            update={
                "roll_total": total,
                "succeeded": total >= original.dc,
                "inspiration_bonus": face,
            }
        ),
        RNGTransition.between(rng_context, rng),
    )
