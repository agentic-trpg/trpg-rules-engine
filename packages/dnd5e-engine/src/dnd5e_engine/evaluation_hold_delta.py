"""Closed internal C-15/C-16 candidate for one authorized Hold Person cast."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from dnd5e_engine.concentration_rules import concentration_anchor
from dnd5e_engine.effect_lifecycle import OngoingEffectLifecycle
from dnd5e_engine.evaluation_base import EvaluationModel
from dnd5e_engine.evaluation_effects import EffectState, effect_state
from dnd5e_engine.evaluation_ruleset import get_default_ruleset
from dnd5e_engine.evaluation_state import ConditionLink, ConditionRecord, LifecycleRecord
from dnd5e_engine.hold_person_rules import hold_person_effect
from dnd5e_engine.spellcasting import resolve_target_count
from dnd5e_engine.types.conditions import ActiveCondition


class HoldPersonCreatedEntry(EvaluationModel):
    """Represent one target entry created in the Hold Person graph."""

    effect: EffectState
    clock: LifecycleRecord | None
    lineage: ConditionLink
    expected_effect: None
    expected_clock: None
    expected_lineage: None
    expected_conditions: tuple[()]
    condition: ConditionRecord | None


class HoldPersonGraphCreate(EvaluationModel):
    """Propose creation of the complete guarded Hold Person graph."""

    kind: Literal["combat.hold_person_graph_create"]
    combat_id: Annotated[str, Field(min_length=1)]
    source_id: Annotated[str, Field(min_length=1)]
    spell_source_id: Annotated[str, Field(min_length=1)]
    spell_slug: Literal["hold-person"]
    actor_ids: tuple[str, ...]
    selected_target_ids: tuple[str, ...]
    slot_level: Literal[2, 3]
    save_dc: Annotated[int, Field(gt=0)]
    applied_turn_serial: Annotated[int, Field(ge=1)]
    applied_round: Annotated[int, Field(ge=1)]
    expected_effects: tuple[()]
    expected_clocks: tuple[()]
    expected_lineages: tuple[()]
    expected_concentration_id: None
    expected_chain: tuple[()]
    expected_rounds: None
    entries: Annotated[tuple[HoldPersonCreatedEntry, ...], Field(min_length=1)]
    concentration_id: Literal["effect:paralyzed", "effect:hold-person"]
    chain: tuple[tuple[str, str, str], ...]
    rounds: Literal[9]

    @model_validator(mode="after")
    def closed_graph(self) -> Self:
        """Validate the complete Hold Person graph and its identity relationships."""
        ids = set(self.actor_ids)
        targets = self.selected_target_ids
        identities = tuple((e.effect.target_id, e.effect.id, e.effect.origin) for e in self.entries)
        spell = get_default_ruleset().get_spell("hold-person")
        assert spell is not None
        count = resolve_target_count(
            spell.activities[0].target.affects.count, cast_level=self.slot_level
        )
        if (
            not ids
            or len(ids) != len(self.actor_ids)
            or self.source_id not in ids
            or not targets
            or len(set(targets)) != len(targets)
            or count is None
            or len(targets) > count
            or self.source_id in targets
            or not set(targets) <= ids
            or len(set(identities)) != len(identities)
            or self.chain != identities
            or any(i[1] != self.concentration_id for i in identities)
        ):
            raise ValueError("Hold Person graph has duplicate, absent or contradictory references")
        for entry, identity in zip(self.entries, identities, strict=True):
            effect = entry.effect
            if entry.lineage.identity != identity:
                raise ValueError("Hold Person lineage identity mismatch")
            if self.concentration_id == "effect:hold-person":
                if (
                    len(self.entries) != 1
                    or effect != effect_state(concentration_anchor(spell, self.source_id))
                    or entry.clock is not None
                    or entry.lineage.statuses
                    or entry.condition is not None
                ):
                    raise ValueError(
                        "only the exact empty caster-side concentration anchor is allowed"
                    )
            else:
                expected = hold_person_effect(spell, self.source_id, effect.target_id, self.save_dc)
                assert expected.lifecycle is not None
                clock = LifecycleRecord(
                    identity=identity,
                    state=OngoingEffectLifecycle.from_application(
                        identity, expected.lifecycle, self.applied_turn_serial, self.applied_round
                    ),
                )
                condition = ConditionRecord.model_validate(
                    ActiveCondition(
                        condition="paralyzed",
                        source_entity_id="implied:effect",
                        scope="combat",
                        source_effect_id=expected.id,
                    ).model_dump()
                )
                if (
                    effect.target_id not in targets
                    or effect != effect_state(expected)
                    or entry.clock != clock
                    or entry.lineage.statuses != ["paralyzed"]
                    or entry.condition != condition
                ):
                    raise ValueError(
                        "Hold Person effect/condition/clock differs from captured canonical binding"
                    )
        return self
