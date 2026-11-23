"""Plan a draw-free exploration Bardic Inspiration grant from fixed facts."""

from typing import Annotated, Literal, Self

from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.common import UtilityActivity
from pydantic import Field, model_validator

from dnd5e_engine.activities.context import ActivityResolutionContext
from dnd5e_engine.activities.effects import passive_effect_to_active_effect
from dnd5e_engine.activities.scale import build_scale_values
from dnd5e_engine.evaluation_actor import combatant
from dnd5e_engine.evaluation_base import EvaluationModel
from dnd5e_engine.evaluation_delta import ResourceUpdate
from dnd5e_engine.evaluation_effects import EffectState, effect_state
from dnd5e_engine.evaluation_payments import feature_payments
from dnd5e_engine.evaluation_ruleset import RulesetBinding, get_default_ruleset, verify_ruleset
from dnd5e_engine.evaluation_state import NonCombatSnapshot
from dnd5e_engine.feature_runtime import DrawFreeRandom, FeatureInvocation, preflight_feature
from dnd5e_engine.rules.character import leveled_feature_levels
from dnd5e_engine.rules.conditions import conditions_block_actions
from dnd5e_engine.spatial import GridTopology, canonical_cell_id
from dnd5e_engine.specs import GridScene

_FEATURE = "bardic-inspiration"


class ExplorationFeaturePayload(EvaluationModel):
    """Select the exact Bardic feature activity and one recipient."""

    kind: Literal["world.use_feature"]
    feature_id: Literal["bardic-inspiration"]
    target_id: Annotated[str, Field(min_length=1)]


class BardicGrantRequest(EvaluationModel):
    """Bind a typed feature invocation to host-owned position and recipient sight facts."""

    kind: Literal["world.use_feature"]
    session_id: Annotated[str, Field(min_length=1)]
    command_id: Annotated[str, Field(min_length=1)]
    actor_id: Annotated[str, Field(min_length=1)]
    target_id: Annotated[str, Field(min_length=1)]
    feature_id: Literal["bardic-inspiration"]
    source_cell: Annotated[str, Field(min_length=1)]
    target_cell: Annotated[str, Field(min_length=1)]
    visible_source_id: Annotated[str, Field(min_length=1)] | None
    tick: Annotated[int, Field(ge=0)]
    snapshot: NonCombatSnapshot
    ruleset_binding: RulesetBinding

    @model_validator(mode="after")
    def fixed_identity(self) -> Self:
        """Reject a mismatched or self-directed fixed request."""
        if self.actor_id == self.target_id or self.snapshot.session_id != self.session_id:
            raise ValueError("Bardic Inspiration requires another creature")
        return self


class BardicGrantPlan(EvaluationModel):
    """Return one exact feature payment and one timed effect, without mutating state."""

    payment: ResourceUpdate
    effect: EffectState
    duration_seconds: Annotated[int, Field(gt=0)]


def plan_bardic_grant(
    request: BardicGrantRequest, *, loader: AssetLoader | None = None
) -> BardicGrantPlan:
    """Validate the canonical Bard activity, two actors, sight, range and owned use.

    This is an uncommitted proposal. The host must recheck fixed facts and atomically
    adopt its resource update and effect against the current transaction stage.
    """
    assets = loader if loader is not None else get_default_ruleset()
    verify_ruleset(request.ruleset_binding, assets)
    feature = assets.get_feature(_FEATURE)
    if feature is None or feature != get_default_ruleset().get_feature(_FEATURE):
        raise ValueError("Bardic Inspiration differs from the canonical feature")
    bard_class = assets.get_class("bard")
    if bard_class is None or bard_class != get_default_ruleset().get_class("bard"):
        raise ValueError("Bard class differs from the canonical source")
    if len(feature.activities) != 1 or len(feature.passive_effects) != 1:
        raise ValueError("Bardic Inspiration requires one activity and passive")
    activity, passive = feature.activities[0], feature.passive_effects[0]
    if (
        not isinstance(activity, UtilityActivity)
        or activity.activation.type != "bonus"
        or activity.activation.value != 1
        or activity.target.affects.type != "creature"
        or activity.target.affects.count != "1"
        or activity.range.units != "ft"
        or activity.range.value != "60"
        or activity.duration.units != "hour"
        or activity.duration.value != "1"
        or len(activity.effects) != 1
        or activity.effects[0].id != passive.id
        or passive.duration != {"seconds": 3600}
    ):
        raise ValueError("Bardic Inspiration activity is unsupported")
    actors = {actor.entity_id: actor for actor in request.snapshot.character_states}
    bard, target = actors.get(request.actor_id), actors.get(request.target_id)
    if (
        bard is None
        or target is None
        or not bard.is_alive
        or bard.hp_current <= 0
        or conditions_block_actions([condition.condition for condition in bard.conditions])
        or not target.is_alive
        or target.hp_current <= 0
        or bard.class_slug != "bard"
        or "bardic-inspiration" not in (bard.granted_features or ())
        or "bardic-inspiration"
        not in leveled_feature_levels(
            [(bard_class, bard.classes.get("bard", bard.character_level))]
        )
        or request.visible_source_id != request.actor_id
        or any(
            effect.target_id == request.target_id and effect.id == "effect:inspired"
            for effect in request.snapshot.effect_states
        )
    ):
        raise ValueError("Bardic Inspiration actor or recipient is ineligible")
    topology = GridTopology(
        GridScene.model_validate(request.snapshot.scene_state.grid.model_dump())
    )
    source, destination = (
        canonical_cell_id(request.source_cell),
        canonical_cell_id(request.target_cell),
    )
    if (
        not topology.is_valid_cell(source)
        or not topology.is_valid_cell(destination)
        or not topology.within_range(source, destination, int(activity.range.value))
    ):
        raise ValueError("Bardic Inspiration recipient is out of range")
    if request.snapshot.resource_state is None:
        raise ValueError("Bardic Inspiration needs an owned resource pool")
    caster, recipient = combatant(bard), combatant(target)
    levels = bard.classes or {"bard": bard.character_level}
    scales = build_scale_values(
        class_slug=bard.class_slug,
        subclass_slug=bard.subclass_slug,
        species_slug=bard.species_slug,
        level=bard.character_level,
        classes=levels,
        loader=assets,
    )
    context = ActivityResolutionContext(
        rng=DrawFreeRandom(0),
        caster=caster,
        targets=[recipient],
        event_emitter=lambda _event: None,
        caster_abilities={
            key: getattr(bard, value)
            for key, value in (
                ("str", "strength"),
                ("dex", "dexterity"),
                ("con", "constitution"),
                ("int", "intelligence"),
                ("wis", "wisdom"),
                ("cha", "charisma"),
            )
        },
        caster_level=bard.character_level,
        scale_values=scales,
        class_levels=levels,
    )
    invocation = preflight_feature(
        feature,
        FeatureInvocation(activities=[activity], passive_effects=[passive], is_bonus_action=True),
        context,
        loader=assets,
        repertoire=bard.granted_features or (),
        spent={
            key.removeprefix("feature_use:"): value["spent"]
            for key, value in bard.custom_counters.items()
        },
    )
    payments = feature_payments(
        request.snapshot.resource_state, request.actor_id, invocation.payments
    )
    die = scales.get("bard.inspiration.die")
    if len(payments) != 1 or payments[0].amount != -1 or die not in ("d6", "d8", "d10", "d12"):
        raise ValueError("Bardic Inspiration lacks its priced use or canonical die")
    effect = effect_state(
        passive_effect_to_active_effect(
            passive,
            target_id=request.target_id,
            caster_id=request.actor_id,
            extra_flags={
                "bardic_inspiration": {
                    "grant_id": request.command_id,
                    "die_sides": int(die[1:]),
                }
            },
        )
    )
    if effect.id != "effect:inspired" or effect.origin != f"cast:inspired:{request.actor_id}":
        raise ValueError("Bardic Inspiration effect identity differs")
    return BardicGrantPlan(
        payment=payments[0], effect=effect, duration_seconds=passive.duration["seconds"]
    )
