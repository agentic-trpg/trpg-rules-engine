"""Admit the SRD Aid potency and select one effective same-name application."""

import re
from collections.abc import Iterable
from functools import lru_cache
from hashlib import sha256
from typing import Literal

from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.common import Activity
from dnd5e_srd_data.schema.spell import Spell
from pydantic import Field

from dnd5e_engine.activities.effects import passive_effect_to_active_effect
from dnd5e_engine.evaluation_base import EvaluationModel
from dnd5e_engine.evaluation_effects import EffectState, effect_state
from dnd5e_engine.rules.effects import strongest_latest
from dnd5e_engine.types.effects import ActiveEffect, ActiveEffectDuration


class PairedHPTransition(EvaluationModel):
    """Checked current and maximum HP after one effective bonus changes."""

    expected_current: int = Field(ge=0)
    expected_maximum: int = Field(ge=1)
    current: int = Field(ge=0)
    maximum: int = Field(ge=1)
    old_bonus: int = Field(ge=0)
    new_bonus: int = Field(ge=0)
    zero_consequence: Literal["none", "unconscious", "death"]


@lru_cache(maxsize=1)
def _reviewed_aid() -> Spell:
    """Keep the bundled, reviewed SRD definition as the admission reference."""
    spell = BundledAssetLoader().get_spell("aid")
    if spell is None:
        raise ValueError("bundled Aid is missing")
    return spell


def is_reviewed_aid(spell: Spell) -> bool:
    """Match the bound definition to the supported SRD Aid asset."""
    return spell.slug == "aid" and spell == _reviewed_aid()


@lru_cache(maxsize=8)
def _aid_template(level: int) -> EffectState:
    """Normalize each reviewed level once, then substitute only exact identity fields."""
    _, active = aid_profile(
        _reviewed_aid(), level=level, target_id="reviewed-target", caster_id="reviewed-caster"
    )
    return effect_state(active)


def aid_profile(
    spell: Spell, *, level: int, target_id: str, caster_id: str
) -> tuple[Activity, ActiveEffect]:
    """Build the exact single-target application from one selected Aid level."""
    if spell != _reviewed_aid() or not 2 <= level <= 9:
        raise ValueError("Aid requires its reviewed definition and supported slot")
    activity = spell.activities[0]
    bonus = 5 * (level - 1)
    matches = [
        passive
        for passive in spell.passive_effects
        if len(passive.changes) == 1
        and passive.changes[0].key == "system.attributes.hp.tempmax"
        and passive.changes[0].mode == 2
        and passive.changes[0].value == str(bonus)
    ]
    if len(matches) != 1:
        raise ValueError("Aid level has no unique bound HP effect")
    active = passive_effect_to_active_effect(matches[0], target_id=target_id, caster_id=caster_id)
    active = active.model_copy(
        update={
            "id": "effect:aid",
            "origin": f"cast:aid:{caster_id}",
            "duration": ActiveEffectDuration(seconds=28800),
        }
    )
    return activity, active


def aid_potency(effect: EffectState) -> int:
    """Read only a fully admitted typed Aid application, independent of source."""
    if effect.id != "effect:aid" or not effect.origin.startswith("cast:aid:"):
        raise ValueError("not an Aid application")
    source = effect.origin.removeprefix("cast:aid:")
    caster, marker, digest = source.partition(":recast:")
    if not caster:
        raise ValueError("Aid origin has no caster")
    if marker and not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Aid recast origin is malformed")
    if len(effect.changes) != 1:
        raise ValueError("Aid requires one typed maximum HP change")
    change = effect.changes[0]
    if (
        change.key != "system.attributes.hp.tempmax"
        or change.mode != "add"
        or type(change.value) is not str
        or not re.fullmatch(r"(?:5|10|15|20|25|30|35|40)", change.value)
    ):
        raise ValueError("Aid potency is not an admitted slot increment")
    potency = int(change.value)
    expected = _aid_template(potency // 5 + 1).model_copy(
        update={"target_id": effect.target_id, "origin": effect.origin}
    )
    if effect != expected:
        raise ValueError("Aid effect differs from its reviewed definition")
    return potency


def admit_aid_overlap(
    active: ActiveEffect, existing: Iterable[EffectState], command_id: str
) -> ActiveEffect:
    """Retain every source; qualify same-source recasts without replacing older rows."""
    aid_potency(effect_state(active))
    origins = {
        effect.origin
        for effect in existing
        if effect.target_id == active.target_id and effect.id == active.id
    }
    if active.origin not in origins:
        return active
    origin = f"{active.origin}:recast:{sha256(command_id.encode('utf-8')).hexdigest()}"
    if origin in origins:
        raise ValueError("Aid cast identity already exists")
    return active.model_copy(update={"origin": origin})


def reviewed_hp_bonus(effect: EffectState) -> int | None:
    """Return an admitted current/maximum HP effect's typed potency when supported."""
    if effect.id == "effect:aid":
        return aid_potency(effect)
    return None


def effective_hp_bonus(effects: Iterable[EffectState]) -> int:
    """Select strongest admitted same-name HP bonus, latest on equal strength."""
    ranked = []
    for effect in effects:
        potency = reviewed_hp_bonus(effect)
        if potency is None:
            raise ValueError("effect is not an admitted HP bonus")
        ranked.append((effect, potency))
    winner = strongest_latest(ranked, lambda item: item[1])
    return winner[1] if winner is not None else 0


def paired_hp_transition(
    current: int, maximum: int, old_bonus: int, new_bonus: int, *, entity_type: str
) -> PairedHPTransition:
    """Preserve HP changes and identify a non-damage positive-to-zero life transition."""
    if not (0 <= current <= maximum and 0 <= old_bonus < maximum and new_bonus >= 0):
        raise ValueError("Aid HP input is outside its supported balance")
    if entity_type not in {"Character", "NPC", "Monster"}:
        raise ValueError("Unsupported effect target kind")
    new_maximum = maximum - old_bonus + new_bonus
    if new_maximum < 1:
        raise ValueError("Aid would leave no base maximum")
    new_current = min(new_maximum, max(0, current + new_bonus - old_bonus))
    return PairedHPTransition(
        expected_current=current,
        expected_maximum=maximum,
        current=new_current,
        maximum=new_maximum,
        old_bonus=old_bonus,
        new_bonus=new_bonus,
        zero_consequence=("unconscious" if entity_type == "Character" else "death")
        if current > 0 and new_current == 0
        else "none",
    )
