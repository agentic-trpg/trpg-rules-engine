"""Project Beacon of Hope's reviewed canonical passive consequences."""

from functools import lru_cache

from dnd5e_srd_data.loader import AssetLoader, BundledAssetLoader
from dnd5e_srd_data.schema.spell import Spell

from dnd5e_engine.activities.effects import passive_effect_to_active_effect
from dnd5e_engine.evaluation_effects import EffectState, effect_state
from dnd5e_engine.types.effects import ActiveEffect, ActiveEffectChange

BEACON_SLUG = "beacon-of-hope"


@lru_cache(maxsize=1)
def _canonical_beacon() -> Spell:
    """Return the bundled source whose save and healing behavior was reviewed."""
    spell = BundledAssetLoader().get_spell(BEACON_SLUG)
    if spell is None:
        raise ValueError("bundled Beacon of Hope is missing")
    return spell


def reviewed_beacon_spell(spell: Spell) -> bool:
    """Require the complete reviewed asset before projecting Beacon mechanics."""
    return spell.slug == BEACON_SLUG and spell == _canonical_beacon()


def beacon_effect(spell: Spell, target_id: str, caster_id: str) -> ActiveEffect:
    """Build the timed effect with native Wisdom and death save advantage and maximum healing."""
    active = passive_effect_to_active_effect(
        spell.passive_effects[0], target_id=target_id, caster_id=caster_id, concentration=True
    )
    # The imported asset has native save keys but no mechanical healing field.
    # These consequences are bound to the exact SRD asset above.
    return active.model_copy(
        update={
            "changes": [
                *(
                    ActiveEffectChange(
                        key="flags.advantage.save.death", mode="override", value=True
                    )
                    if change.key == "system.attributes.death.roll.mode"
                    else change
                    for change in active.changes
                ),
                ActiveEffectChange(key="healing.received.maximize", mode="override", value=True),
            ]
        }
    )


@lru_cache(maxsize=1)
def _canonical_beacon_effect() -> EffectState:
    """Cache the bound rule's normalized effect apart from its instance IDs."""
    return effect_state(beacon_effect(_canonical_beacon(), "reviewed-target", "reviewed-source"))


def is_reviewed_beacon_effect(effect: EffectState, loader: AssetLoader) -> bool:
    """Recognize this binding's exact Beacon effect while preserving application identity."""
    prefix = "cast:hopeful:"
    if effect.id != "effect:hopeful" or not effect.origin.startswith(prefix):
        return False
    caster_id = effect.origin.removeprefix(prefix)
    spell = loader.get_spell(BEACON_SLUG)
    return bool(
        caster_id.strip()
        and effect.flags.get("concentration") is True
        and all(change.value is True for change in effect.changes)
        and spell is not None
        and reviewed_beacon_spell(spell)
        and effect
        == _canonical_beacon_effect().model_copy(
            update={"target_id": effect.target_id, "origin": effect.origin}
        )
    )
