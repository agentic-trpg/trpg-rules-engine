"""Closed old-graph fence plus canonical creation after atomic owned deletion."""

from dataclasses import replace
from typing import Annotated, Literal, Self

from dnd5e_srd_data import BundledAssetLoader
from pydantic import Field, model_validator

from dnd5e_engine.concentration_rules import concentration_anchor
from dnd5e_engine.effect_lifecycle import OngoingEffectLifecycle
from dnd5e_engine.evaluation_base import EvaluationModel
from dnd5e_engine.evaluation_effects import EffectState, effect_state
from dnd5e_engine.evaluation_hold_delta import HoldPersonGraphCreate
from dnd5e_engine.evaluation_state import ConditionLink, ConditionRecord, LifecycleRecord
from dnd5e_engine.hold_person_rules import hold_person_effect
from dnd5e_engine.types.conditions import ActiveCondition


class HoldPersonConditionFence(EvaluationModel):
    actor_id: Annotated[str, Field(min_length=1)]
    conditions: tuple[ConditionRecord, ...]


class HoldPersonGraphReplace(EvaluationModel):
    kind: Literal["combat.hold_person_graph_replace"]
    combat_id: Annotated[str, Field(min_length=1)]
    source_id: Annotated[str, Field(min_length=1)]
    actor_ids: tuple[str, ...]
    expected_effects: Annotated[tuple[EffectState, ...], Field(min_length=1, max_length=2)]
    expected_clocks: tuple[LifecycleRecord, ...]
    expected_lineages: tuple[ConditionLink, ...]
    expected_conditions: tuple[HoldPersonConditionFence, ...]
    expected_concentration_id: Literal["effect:paralyzed", "effect:hold-person"]
    expected_chain: tuple[tuple[str, str, str], ...]
    expected_rounds: Annotated[int, Field(ge=1, le=10)]
    # This nested Create is valid only AFTER all guarded old members are deleted.
    # The existing standalone expected-absence contract is never relaxed.
    creation: HoldPersonGraphCreate

    @model_validator(mode="after")
    def closed_replacement(self) -> Self:
        new = self.creation
        identities = tuple((e.target_id, e.id, e.origin) for e in self.expected_effects)
        links = {c.identity: c for c in self.expected_lineages}
        clocks = {c.identity: c for c in self.expected_clocks}
        if (
            (self.combat_id, self.source_id, self.actor_ids)
            != (new.combat_id, new.source_id, new.actor_ids)
            or set(identities) != set(self.expected_chain)
            or len(set(self.expected_chain)) != len(self.expected_chain)
            or len(set(identities)) != len(identities)
            or set(links) != set(identities)
            or len(links) != len(self.expected_lineages)
            or len(clocks) != len(self.expected_clocks)
            or tuple(c.actor_id for c in self.expected_conditions) != self.actor_ids
            or any(i[1] != self.expected_concentration_id for i in identities)
        ):
            raise ValueError("replacement requires a complete same-source closed old graph")
        spell = BundledAssetLoader().get_spell("hold-person")
        assert spell is not None
        conditions: dict[str, tuple[ConditionRecord, ...]] = {}
        anchor = self.expected_concentration_id == "effect:hold-person"
        if set(clocks) != (set() if anchor else set(identities)):
            raise ValueError("old clocks must exactly cover the old target effects")
        for effect, identity in zip(self.expected_effects, identities, strict=True):
            if anchor:
                if (
                    len(identities) != 1
                    or effect != effect_state(concentration_anchor(spell, self.source_id))
                    or links[identity].statuses
                ):
                    raise ValueError("old empty anchor must be canonical and caster-owned")
                continue
            app = effect.lifecycle
            if (
                app is None
                or app.save_dc is None
                or effect.target_id not in self.actor_ids
                or effect.target_id == self.source_id
                or effect
                != effect_state(
                    hold_person_effect(spell, self.source_id, effect.target_id, app.save_dc)
                )
                or links[identity].statuses != ["paralyzed"]
            ):
                raise ValueError("old effect/source/condition differs from canonical Hold Person")
            clock = clocks[identity].state
            expected = OngoingEffectLifecycle.from_application(
                identity, app, clock.applied_turn_serial, clock.applied_round
            )
            if (
                clock != replace(expected, last_repeat_turn_serial=clock.last_repeat_turn_serial)
                or not 1 <= clock.applied_turn_serial < new.applied_turn_serial
                or not 1 <= clock.applied_round <= new.applied_round
                or (
                    clock.last_repeat_turn_serial is not None
                    and not (
                        clock.applied_turn_serial
                        <= clock.last_repeat_turn_serial
                        < new.applied_turn_serial
                    )
                )
            ):
                raise ValueError("old clock must precede the new deterministic cast generation")
            conditions[effect.target_id] = (
                ConditionRecord.model_validate(
                    ActiveCondition(
                        condition="paralyzed",
                        source_entity_id="implied:effect",
                        scope="combat",
                        source_effect_id=effect.id,
                    ).model_dump()
                ),
            )
        if any(c.conditions != conditions.get(c.actor_id, ()) for c in self.expected_conditions):
            raise ValueError("old actor condition footprint must exactly match the owned graph")
        return self
