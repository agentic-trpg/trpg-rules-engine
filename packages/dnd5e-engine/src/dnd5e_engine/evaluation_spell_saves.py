"""Resolve and verify ordered spell saves with a private Bardic choice boundary."""

import copy
from collections.abc import Iterable
from dataclasses import dataclass
from random import Random
from typing import Literal

from dnd5e_srd_data.loader import AssetLoader

from dnd5e_engine.activities.save_primitive import SaveBonusDie
from dnd5e_engine.evaluation_contracts import RuleEvaluationResult, SavePayload
from dnd5e_engine.evaluation_effects import EffectState
from dnd5e_engine.evaluation_rng import RNGContext, RNGState
from dnd5e_engine.evaluation_saves import (
    _held_inspiration,
    resolve_bardic_save_choice,
    resolve_snapshot_save,
)
from dnd5e_engine.evaluation_state import CharacterStateV2
from dnd5e_engine.events import Ability, SaveRolled


@dataclass(frozen=True)
class SpellSaveStep:
    """One ordered save and its RNG boundaries before and after a choice."""

    target_id: str
    original: SaveRolled
    final: SaveRolled | None
    rng_after: RNGState
    rng_after_bonus: RNGState
    choice: Literal["accept", "decline"] | None = None
    grant: EffectState | None = None


@dataclass(frozen=True)
class SpellSaveProgress:
    """Keep uncommitted save evidence until the entire cast can be adopted."""

    steps: tuple[SpellSaveStep, ...]
    pending_target_id: str | None
    result: RuleEvaluationResult | None = None
    spent_grants: tuple[EffectState, ...] = ()


@dataclass(frozen=True)
class SpellSaveContinuation:
    """Carry the private prefix and authorized recipient of a spell's choice."""

    prior_steps: tuple[SpellSaveStep, ...] = ()
    choice: bool | None = None
    choice_die_sides: int | None = None
    choice_target_id: str | None = None
    pause: bool = False
    replay: bool = False


ORDINARY_SPELL_SAVES = SpellSaveContinuation()


def split_spell_save_effects(
    effects: Iterable[EffectState], loader: AssetLoader
) -> tuple[list[EffectState], dict[str, EffectState]]:
    """Separate exact active grants from save modifiers before the first draw."""
    modifiers = []
    grants = {}
    for effect in effects:
        if effect.id != "effect:inspired" or effect.disabled:
            modifiers.append(effect)
            continue
        if not _held_inspiration(effect, loader) or effect.target_id in grants:
            raise ValueError("target has invalid or duplicate Inspiration grants")
        grants[effect.target_id] = effect
    return modifiers, grants


def _offers_choice(save: SaveRolled, grant: EffectState | None) -> bool:
    """Recognize a genuine rolled failure with an eligible fixed grant."""
    detail = save.roll_detail
    return (
        grant is not None
        and save.succeeded is False
        and detail is not None
        and detail.status == "rolled"
        and detail.d20 is not None
        and detail.d20.origin == "rng"
    )


def _answer_step(
    step: SpellSaveStep,
    grant: EffectState,
    use: bool,
    context: RNGContext,
) -> SpellSaveStep:
    """Resolve from the original post-save RNG using the exact grant's die."""
    final, transition = resolve_bardic_save_choice(
        step.original,
        grant.flags["bardic_inspiration"]["die_sides"],
        context.model_copy(update={"state": step.rng_after}),
        use=use,
    )
    return SpellSaveStep(
        step.target_id,
        step.original,
        final,
        step.rng_after,
        transition.next_state,
        "accept" if use else "decline",
        grant,
    )


def _roll_step(
    target: CharacterStateV2,
    ability: Ability,
    dc: int,
    rng: Random,
    bonus: tuple[SaveBonusDie, ...],
    equipment: tuple[tuple[str, int], ...],
) -> SpellSaveStep:
    """Resolve one target using the ordinary bound save calculation."""
    save = resolve_snapshot_save(
        target,
        SavePayload(
            kind="rules.save",
            target_id=target.entity_id,
            ability=ability,
            dc=dc,
            advantage=(),
            disadvantage=(),
            is_magical=True,
        ),
        rng,
        bonus_dice=bonus,
        equipment_bonus=equipment,
    )
    after = RNGState.capture(rng)
    return SpellSaveStep(target.entity_id, save, None, after, after)


def resolve_ordered_spell_saves(
    context: RNGContext,
    targets: Iterable[CharacterStateV2],
    *,
    ability: Ability,
    dc: int,
    bonus: dict[str, tuple[SaveBonusDie, ...]],
    equipment: dict[str, tuple[tuple[str, int], ...]],
    grants: dict[str, EffectState],
    continuation: SpellSaveContinuation = ORDINARY_SPELL_SAVES,
) -> tuple[SpellSaveProgress, Random]:
    """Reuse a staged prefix or independently replay its exact rolls from origin."""
    prior = continuation.prior_steps
    choice = continuation.choice
    last = len(prior) - 1
    if choice is not None and (not continuation.pause or not prior or prior[-1].choice is not None):
        raise ValueError("spell choice lacks a pending ordered save")
    if continuation.choice_die_sides is not None and choice is None:
        raise ValueError("spell choice die requires an explicit answer")
    rng = context.state.restore()
    steps: list[SpellSaveStep] = []
    spent: list[EffectState] = []
    answered = False
    for index, target in enumerate(targets):
        target_id = target.entity_id
        grant = (
            grants.get(target_id)
            if continuation.pause and continuation.choice_target_id in (None, target_id)
            else None
        )
        previous = prior[index] if index <= last else None
        if previous is not None and previous.target_id != target_id:
            raise ValueError("spell save prefix differs from fixed target order")
        if previous is not None and not continuation.replay:
            step = copy.deepcopy(previous)
            rng.setstate(step.rng_after_bonus.restore().getstate())
        else:
            step = _roll_step(
                target, ability, dc, rng, bonus.get(target_id, ()), equipment[target_id]
            )
        if previous is not None and previous.choice is not None:
            if not _offers_choice(step.original, grant) or previous.grant != grant:
                raise ValueError("spell save choice differs from the authorized fixed grant")
            if continuation.replay:
                assert grant is not None
                step = _answer_step(step, grant, previous.choice == "accept", context)
        elif _offers_choice(step.original, grant):
            if previous is None or (index == last and choice is None):
                steps.append(step)
                return SpellSaveProgress(tuple(steps), target_id, spent_grants=tuple(spent)), rng
            if index != last or choice is None:
                raise ValueError("spell choice must answer the last save in its pending prefix")
            assert grant is not None
            if continuation.choice_die_sides != grant.flags["bardic_inspiration"]["die_sides"]:
                raise ValueError("spell choice die differs from the fixed grant")
            step = _answer_step(step, grant, choice, context)
            answered = True
        if continuation.replay and previous != step:
            raise ValueError("spell save evidence differs from fixed RNG or grant")
        if step.choice == "accept":
            assert step.grant is not None
            spent.append(step.grant)
        if step.choice is not None:
            rng.setstate(step.rng_after_bonus.restore().getstate())
        steps.append(step)
    if len(steps) < len(prior) or (choice is not None and not answered):
        raise ValueError("spell save prefix or answer does not match the cast")
    return SpellSaveProgress(tuple(steps), None, spent_grants=tuple(spent)), rng
