"""Repository-local initialization candidate; no public combat.start authority ABI.

This internal calculation seam is deliberately absent from evaluate's operation union.
It must not be connected to untrusted callers until Meta C-16 is decided.
"""

import copy
from typing import Literal, Self, TypedDict

from dnd5e_srd_data.loader import AssetLoader
from pydantic import Field, model_validator

from dnd5e_engine.activities.d20 import AdvantageSources
from dnd5e_engine.evaluation_actor import actor_state, combatant
from dnd5e_engine.evaluation_base import EvaluationModel
from dnd5e_engine.evaluation_contracts import (
    EvaluationVersion,
    ReadVersion,
    RuleError,
    RuleEvaluationResult,
)
from dnd5e_engine.evaluation_delta import (
    AttackBudgetState,
    CombatCreationCandidate,
    InitialActorState,
    InitialActorUpdate,
    StateDelta,
)
from dnd5e_engine.evaluation_initial_state import initial_combat_state
from dnd5e_engine.evaluation_rng import RNGContext, RNGTransition
from dnd5e_engine.evaluation_ruleset import RulesetBinding, verify_ruleset
from dnd5e_engine.evaluation_state import (
    NonCombatSnapshot,
    ObjectState,
    PersistentAreasState,
    TimedActivitiesState,
)
from dnd5e_engine.events import RoundStarted, TurnPhase, TurnStarted
from dnd5e_engine.initiative_rules import initiative_order_key, resolve_initiative_value
from dnd5e_engine.movement import project_speeds
from dnd5e_engine.rules.dice import ability_modifier
from dnd5e_engine.spatial import GridTopology
from dnd5e_engine.specs import GridScene
from dnd5e_engine.turn_rules import reset_turn_budget


class CombatInitializationCandidatePayload(EvaluationModel):
    kind: Literal["combat.init.local-candidate"]


class CombatInitializationCandidateRequest(EvaluationModel):
    """Calculation input only: no fabricated principal/source permission fields."""

    schema_version: EvaluationVersion
    payload: CombatInitializationCandidatePayload
    session_id: str = Field(min_length=1)
    command_id: str = Field(min_length=1)
    state_snapshot: NonCombatSnapshot
    ruleset_binding: RulesetBinding
    rng_context: RNGContext

    @model_validator(mode="after")
    def identities(self) -> Self:
        if (
            self.session_id != self.state_snapshot.session_id
            or self.state_snapshot.combat_setup is None
        ):
            raise ValueError(
                "initialization candidate requires matching session and explicit setup"
            )
        return self


class InitializationIdentity(TypedDict):
    schema_version: EvaluationVersion
    session_id: str
    command_id: str
    input_world_version: int
    input_ruleset_binding: RulesetBinding
    read_set: tuple[ReadVersion, ...]


def evaluate_combat_initialization_candidate(
    request: CombatInitializationCandidateRequest, *, loader: AssetLoader
) -> RuleEvaluationResult:
    request = CombatInitializationCandidateRequest.model_validate(copy.deepcopy(request))
    verify_ruleset(request.ruleset_binding, loader)
    snapshot = request.state_snapshot
    setup = snapshot.combat_setup
    assert setup is not None
    common: InitializationIdentity = dict(
        schema_version=request.schema_version,
        session_id=request.session_id,
        command_id=request.command_id,
        input_world_version=snapshot.world_version,
        input_ruleset_binding=request.ruleset_binding,
        read_set=(
            ReadVersion(kind="world", ref=request.session_id, version=snapshot.world_version),
        ),
    )

    def refuse(code: str, reason: str) -> RuleEvaluationResult:
        return RuleEvaluationResult(
            **common,
            status="unsupported",
            state_delta=None,
            proposed_events=(),
            rng_transition=None,
            choice=None,
            error=RuleError(code=code, reason=reason),
        )

    if (
        setup.timed_activities != TimedActivitiesState(pending=[], next_sequence=0)
        or setup.persistent_areas
        != PersistentAreasState(areas=[], next_sequence=0, next_turn_start_effects=())
        or setup.combat_objects != ObjectState(objects=[], used_ids=set())
    ):
        return refuse("init.scene_mechanics", "precombat timing/areas/objects are not migrated")
    if snapshot.effect_states:
        return refuse("init.effects", "effect hydration is not migrated")
    actors = {a.entity_id: a for a in snapshot.character_states}
    grid = GridTopology(GridScene.model_validate(snapshot.scene_state.grid.model_dump()))
    if any(not grid.is_valid_cell(c) for c in setup.actor_zone.values()) or len(
        set(setup.actor_zone.values())
    ) != len(actors):
        return refuse("init.position", "opening cells must be legal and uniquely occupied")
    for actor in actors.values():
        if (
            not actor.is_alive
            or actor.hp_current <= 0
            or actor.conditions
            or actor.concentration_effect_id
            or actor.trait_mechanics
            or actor.class_slug
            or actor.classes
            or actor.subclass_slug
            or actor.jack_of_all_trades
            or actor.reliable_talent
            or actor.species_slug
            or actor.granted_features
            or actor.fighting_styles
            or actor.legendary_actions_max
            or actor.legendary_resistances_max
            or actor.has_fled
            or actor.last_damaged_by
            or actor.weapon_mastery_slugs
            or actor.base_speed < 0
            or not 1 <= actor.dexterity <= 30
            or (
                actor.death_saves is not None
                and (
                    actor.death_saves.successes
                    or actor.death_saves.failures
                    or actor.death_saves.is_stable
                )
            )
        ):
            return refuse(
                "init.actor", "only living ordinary actors without lifecycle hooks are migrated"
            )
        for slug in actor.carried_item_slugs:
            item = loader.get_item(slug)
            if item is None or item.passive_effects or item.requires_attunement:
                return refuse(
                    "init.equipment", "unknown/passive equipment initialization is not migrated"
                )
    for actor_id, slug in setup.opportunity_attack_weapons.items():
        if slug not in actors[actor_id].carried_item_slugs or loader.get_weapon(slug) is None:
            return refuse("init.weapon", "reaction weapon must be explicit owned equipment")
    rng = request.rng_context.state.restore()
    values = {}
    updates = []
    for actor_id in setup.roll_order:
        original = actors[actor_id]
        modifier = setup.initiative_modifiers[actor_id]
        value = resolve_initiative_value(
            setup.fixed_initiative[actor_id],
            ability_modifier(original.dexterity) if modifier is None else modifier,
            AdvantageSources(),
            actor_id in setup.surprised_ids,
            rng,
        )
        local = combatant(original)
        local = reset_turn_budget(
            local,
            effective_speed=project_speeds(local.base_speed, local.movement_modes).walk,
            attacks=1,
        )
        local = local.model_copy(
            update={"initiative": value, "sneak_attack_spent_this_turn": False}
        )
        after = actor_state(local, original)
        fields = tuple(AttackBudgetState.model_fields)
        updates.append(
            InitialActorUpdate(
                actor_id=actor_id,
                expected=InitialActorState(
                    initiative=original.initiative,
                    budget=AttackBudgetState.model_validate(
                        {n: getattr(original, n) for n in fields}
                    ),
                ),
                value=InitialActorState(
                    initiative=after.initiative,
                    budget=AttackBudgetState.model_validate({n: getattr(after, n) for n in fields}),
                ),
            )
        )
        values[actor_id] = value
    order = tuple(
        sorted(
            setup.roll_order, key=lambda a: initiative_order_key(values[a], actors[a].dexterity, a)
        )
    )
    state = initial_combat_state(setup, order)
    events = (
        RoundStarted(round_number=1),
        TurnPhase(actor_id=None, phase="round_start", round_number=1),
        TurnStarted(actor_id=order[0]),
        TurnPhase(actor_id=order[0], phase="turn_start", round_number=1),
    )
    transition = RNGTransition.between(request.rng_context, rng)
    transition.verify_input(request.rng_context)
    return RuleEvaluationResult(
        **common,
        status="accepted",
        state_delta=StateDelta(
            expected_world_version=snapshot.world_version,
            operations=(
                CombatCreationCandidate(
                    kind="combat.create_candidate",
                    expected_absent=True,
                    expected_setup=setup,
                    value=state,
                    actor_updates=tuple(updates),
                ),
            ),
        ),
        proposed_events=events,
        rng_transition=transition,
        choice=None,
        error=None,
    )
