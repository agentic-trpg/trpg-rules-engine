"""Read-only full-identity views for the bounded Hold Person / Shield graph.

Classification is based on encoded identities/application, never spell prose.
Structural closure precedes every subset view; canonical rule/source/actor checks
remain mandatory at the caller. No callback registry or effect execution lives here.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Literal

from dnd5e_engine.effect_lifecycle import EffectIdentity, OngoingEffectLifecycle
from dnd5e_engine.evaluation_effects import EffectState
from dnd5e_engine.evaluation_ruleset import _canonical
from dnd5e_engine.evaluation_shield_delta import ShieldGraphValue, response_state
from dnd5e_engine.evaluation_state import CombatSnapshot, ConditionLink, LifecycleRecord

EffectFamily = Literal["hold-person", "shield", "known_out_of_scope", "unknown"]


def effect_family(effect: EffectState) -> EffectFamily:
    app = effect.lifecycle
    if effect.id == "effect:imperceptible_barrier":
        return "shield"
    if (
        app is not None
        and app.source_kind == "spell"
        and app.source_slug == "hold-person"
        and effect.id == "effect:paralyzed"
    ) or (effect.id == "effect:hold-person" and effect.flags.get("concentration_anchor")):
        return "hold-person"
    if app is not None and app.source_kind == "feature" and app.source_slug == "cunning-strike":
        return "known_out_of_scope"
    return "unknown"


@dataclass(frozen=True)
class EffectGraphIndex:
    effects: Mapping[EffectIdentity, EffectState]
    clocks: Mapping[EffectIdentity, LifecycleRecord]
    links: Mapping[EffectIdentity, ConditionLink]


def lifecycle_clock_failure(
    snapshot: CombatSnapshot, effect: EffectState, record: LifecycleRecord
) -> str | None:
    """Validate the existing captured lifecycle clock without executing a phase."""
    state, clock = snapshot.combat_state, record.state
    identity = (effect.target_id, effect.id, effect.origin)
    if (
        _canonical(clock.application) != _canonical(effect.lifecycle)
        or clock.identity != identity
        or not (
            1 <= clock.applied_turn_serial <= state.turn_serial
            and 1 <= clock.applied_round <= state.round_number
        )
    ):
        return "lifecycle application or clock mismatch"
    last = clock.last_repeat_turn_serial
    if last is not None and not clock.applied_turn_serial <= last <= state.turn_serial:
        return "repeat clock is outside combat history"
    expected = OngoingEffectLifecycle.from_application(
        identity, clock.application, clock.applied_turn_serial, clock.applied_round
    )
    if clock != replace(expected, last_repeat_turn_serial=last):
        return "lifecycle expiry contradicts captured rule"
    return None


def effect_graph_index(snapshot: CombatSnapshot) -> EffectGraphIndex:
    """Reject duplicate/dangling graph records before selecting any family."""
    state = snapshot.combat_state
    effects = {(e.target_id, e.id, e.origin): e for e in snapshot.effect_states}
    clocks = {r.identity: r for r in state.effect_lifecycles}
    links = {r.identity: r for r in state.conditions_by_effect}
    actors = {a.entity_id for a in snapshot.character_states}
    if (
        len(effects) != len(snapshot.effect_states)
        or len(clocks) != len(state.effect_lifecycles)
        or len(links) != len(state.conditions_by_effect)
    ):
        raise ValueError("duplicate full effect/clock/lineage identity")
    if set(effects) != set(links) or set(clocks) != {
        identity for identity, effect in effects.items() if effect.lifecycle is not None
    }:
        raise ValueError("missing or dangling effect/clock/lineage record")
    if any(i[0] not in actors for i in effects) or any(
        r.state.identity != identity for identity, r in clocks.items()
    ):
        raise ValueError("effect target or lifecycle full identity is inconsistent")
    return EffectGraphIndex(
        MappingProxyType(effects), MappingProxyType(clocks), MappingProxyType(links)
    )


@dataclass(frozen=True)
class EffectOwnership:
    index: EffectGraphIndex
    hold_identities: tuple[EffectIdentity, ...]
    hold_source_id: str | None
    shield: ShieldGraphValue | None


def effect_ownership(snapshot: CombatSnapshot) -> EffectOwnership:
    """Prove full graph partition/cross-links before bounded composition views.

    This is graph ownership, not full mechanical admission. Callers must also
    validate canonical Hold, Shield declaration, class/equipment/resources and the
    operation's lifecycle boundary before RNG/payment.
    """
    index = effect_graph_index(snapshot)
    state = snapshot.combat_state
    families = {identity: effect_family(e) for identity, e in index.effects.items()}
    if any(f in {"known_out_of_scope", "unknown"} for f in families.values()):
        raise ValueError("known out-of-scope or unknown effect cannot be omitted from composition")
    hold = tuple(i for i, family in families.items() if family == "hold-person")
    shield_ids = tuple(i for i, family in families.items() if family == "shield")
    if len(state.concentration_chain) > 1 or len(shield_ids) > 1:
        raise ValueError("only one Hold Person source and one Shield graph are reviewed")
    source = next(iter(state.concentration_chain), None)
    chain = tuple(state.concentration_chain.get(source, ())) if source is not None else ()
    if (
        len(set(chain)) != len(chain)
        or set(chain) != set(hold)
        or set(state.concentration_rounds_remaining) != set(state.concentration_chain)
        or bool(hold) != (source is not None)
    ):
        raise ValueError("Hold Person ownership and concentration closure disagree")
    actors = {a.entity_id: a for a in snapshot.character_states}
    if source is not None and (
        source not in actors
        or any(
            (e.lifecycle.source_id if e.lifecycle else e.target_id) != source
            for i, e in index.effects.items()
            if i in hold
        )
        or any(i[1] != actors[source].concentration_effect_id for i in hold)
    ):
        raise ValueError("Hold Person effect source and caster mirror disagree")
    for actor in actors.values():
        if actor.entity_id != source and actor.concentration_effect_id is not None:
            raise ValueError("unlinked concentration actor mirror")
        for condition in actor.conditions:
            if condition.source_effect_id is not None and not any(
                i[0] == actor.entity_id
                and i[1] == condition.source_effect_id
                and condition.condition in index.links[i].statuses
                for i in index.effects
            ):
                raise ValueError("condition has a missing or contradictory effect attachment")
    shield = None
    if shield_ids:
        identity = shield_ids[0]
        owner = identity[0]
        if source is not None and owner != source:
            raise ValueError("composed Shield must belong to the Hold Person caster")
        if (
            state.reaction_effects_pending_expiry != {owner: [identity]}
            or len(state.active_reaction_responses) > 1
        ):
            raise ValueError("Shield response/expiry ownership is incomplete or duplicated")
        shield = ShieldGraphValue(
            effect=index.effects[identity],
            lineage=index.links[identity],
            response=response_state(state.active_reaction_responses[0])
            if state.active_reaction_responses
            else None,
            expiry_owner_id=owner,
            expiry_identity=identity,
            expires_at="owner_next_turn_start",
            owner_dead=owner in state.dead_ids,
        )
    elif state.active_reaction_responses or state.reaction_effects_pending_expiry:
        raise ValueError("dangling Shield response or expiry ledger")
    return EffectOwnership(index, hold, source, shield)


def without_owned_shield(snapshot: CombatSnapshot, ownership: EffectOwnership) -> CombatSnapshot:
    """A validation/projector view only; preserve all non-Shield facts verbatim."""
    if ownership != effect_ownership(snapshot):
        raise ValueError("ownership view does not bind this complete input graph")
    identity = ownership.shield.expiry_identity if ownership.shield else None
    return snapshot.model_copy(
        update={
            "effect_states": tuple(
                e for e in snapshot.effect_states if (e.target_id, e.id, e.origin) != identity
            ),
            "combat_state": snapshot.combat_state.model_copy(
                update={
                    "conditions_by_effect": tuple(
                        link
                        for link in snapshot.combat_state.conditions_by_effect
                        if link.identity != identity
                    ),
                    "active_reaction_responses": [],
                    "reaction_effects_pending_expiry": {},
                }
            ),
        }
    )
