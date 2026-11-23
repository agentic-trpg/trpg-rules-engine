"""Propose exact early endings for reviewed exploration spell applications."""

from dataclasses import dataclass
from typing import Annotated, Literal

from dnd5e_srd_data.loader import AssetLoader
from dnd5e_srd_data.schema.spell import Spell
from pydantic import Field

from dnd5e_engine.evaluation_base import EvaluationModel
from dnd5e_engine.evaluation_effects import EffectState, effect_state
from dnd5e_engine.evaluation_ruleset import RulesetBinding, get_default_ruleset, verify_ruleset
from dnd5e_engine.evaluation_world_effect import EffectIdentity
from dnd5e_engine.events import CombatEvent, ObjectIdentified, SpellCast
from dnd5e_engine.rules.conditions import conditions_block_actions

SPELL_TERMINATION_SCHEMA_VERSION = "engine-evaluation/37"
SpellCastEvaluationVersion = Literal[
    "engine-evaluation/29",
    "engine-evaluation/31",
    "engine-evaluation/32",
    "engine-evaluation/33",
    "engine-evaluation/34",
    "engine-evaluation/35",
    "engine-evaluation/36",
    "engine-evaluation/40",
    "engine-evaluation/41",
    "engine-evaluation/42",
    "engine-evaluation/45",
    "engine-evaluation/46",
    "engine-evaluation/47",
]
EndReason = Literal["spell_cast", "dismissed", "dispelled"]
Nonempty = Annotated[str, Field(min_length=1)]


@dataclass(frozen=True)
class TargetEffectNeed:
    """Select an exact effect definition on the actor who produced the trigger."""

    trigger: Literal["spell_cast"]
    effect_id: str
    spell_id: str


_INVISIBILITY = TargetEffectNeed("spell_cast", "effect:invisibility", "invisibility")


class SpellApplicationFacts(EvaluationModel):
    """Supply one persisted application, owner, deadline and known actual cast level.

    Older /37 cast-trigger inputs may omit the level; /42 Dispel requires it, including
    level zero for cantrips and the bound base level for slotless Ritual casts.
    """

    effect: EffectState
    caster_id: Nonempty
    spell_id: Nonempty
    cast_command_id: Nonempty
    expires_tick: Annotated[int, Field(ge=1)]
    concentration_id: Nonempty | None
    cast_slot_level: Annotated[int, Field(ge=0, le=9)] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @property
    def identity(self) -> EffectIdentity:
        """Return the full effect key in the host's target store."""
        return (self.effect.target_id, self.effect.id, self.effect.origin)


class SpellEndFacts(EvaluationModel):
    """Fix the native cast protocol and the current target-local applications."""

    cast_schema_version: SpellCastEvaluationVersion
    applications: Annotated[tuple[SpellApplicationFacts, ...], Field(min_length=1)]


class WorldSpellEnd(EvaluationModel):
    """Propose exact removal of a verified ongoing world spell application."""

    kind: Literal["world.spell_end"] = "world.spell_end"
    expected: SpellApplicationFacts
    reason: EndReason


def reviewed_invisibility(spell: Spell | None) -> bool:
    """Match the bound spell to the reviewed SRD Invisibility definition."""
    return spell is not None and spell == get_default_ruleset().get_spell(_INVISIBILITY.spell_id)


def cast_termination_needs(loader: AssetLoader) -> tuple[TargetEffectNeed, ...]:
    """Declare which target-local application facts a possible cast may need."""
    return (
        (_INVISIBILITY,) if reviewed_invisibility(loader.get_spell(_INVISIBILITY.spell_id)) else ()
    )


def spell_cast_ends(
    actor_id: str,
    events: tuple[CombatEvent | ObjectIdentified, ...],
    applications: tuple[SpellApplicationFacts, ...],
) -> tuple[WorldSpellEnd, ...]:
    """Select endings only after the affected actor actually casts a spell."""
    if not any(isinstance(event, SpellCast) and event.actor_id == actor_id for event in events):
        return ()
    return tuple(WorldSpellEnd(expected=fact, reason="spell_cast") for fact in applications)


def spell_preserves_observer_sight(
    spell_id: str | None, effect_id: str, statuses: list[str]
) -> bool:
    """The reviewed Invisible condition does not impair its own target's sight."""
    return (
        spell_id == _INVISIBILITY.spell_id
        and effect_id == _INVISIBILITY.effect_id
        and statuses == ["invisible"]
    )


def verify_cast_applications(actor_id: str, facts: SpellEndFacts, loader: AssetLoader) -> None:
    """Admit only complete, distinct target facts for the bound trigger declarations."""
    from dnd5e_engine.evaluation_timed_spells import _passive_profile

    declared = {need.effect_id: need for need in cast_termination_needs(loader)}
    seen = set()
    for application in facts.applications:
        need = declared.get(application.effect.id)
        if (
            need is None
            or need.spell_id != application.spell_id
            or application.effect.target_id != actor_id
            or application.concentration_id != f"actor:{application.cast_command_id}"
            or application.identity in seen
        ):
            raise ValueError("Spell end input differs from its reviewed target application")
        spell = loader.get_spell(need.spell_id)
        assert spell is not None  # The declared trigger requires a bound spell.
        _, active, _ = _passive_profile(spell, target_id=actor_id, caster_id=application.caster_id)
        if application.effect != effect_state(active):
            raise ValueError("Spell end input differs from its bound effect")
        seen.add(application.identity)


def plan_spell_dismissal(
    application: SpellApplicationFacts,
    *,
    actor_id: str,
    actor_conditions: tuple[str, ...],
    binding: RulesetBinding,
    loader: AssetLoader,
) -> WorldSpellEnd:
    """Apply SRD Duration to the exact original caster's reviewed elapsed spell."""
    from dnd5e_engine.evaluation_aid import aid_potency
    from dnd5e_engine.evaluation_timed_spells import is_canonical_elapsed_longstrider

    verify_ruleset(binding, loader)
    spell_id = application.spell_id
    reviewed = spell_id in ("longstrider", "aid") and loader.get_spell(
        spell_id
    ) == get_default_ruleset().get_spell(spell_id)
    effect_matches = False
    if spell_id == "longstrider":
        effect_matches = is_canonical_elapsed_longstrider(application.effect)
    elif spell_id == "aid":
        try:
            aid_potency(application.effect)
        except ValueError:
            pass
        else:
            effect_matches = True
    if (
        application.caster_id != actor_id
        or application.concentration_id is not None
        or conditions_block_actions(list(actor_conditions))
        or not reviewed
        or not effect_matches
    ):
        raise ValueError("The acting caster cannot dismiss this application")
    return WorldSpellEnd(expected=application, reason="dismissed")
