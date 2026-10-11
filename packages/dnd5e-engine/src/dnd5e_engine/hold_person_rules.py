"""Captured canonical Hold Person effect construction, shared with its validators."""

from dnd5e_srd_data.schema.common import SaveActivity
from dnd5e_srd_data.schema.spell import Spell

from dnd5e_engine.activities.effects import bind_effect_lifecycle, passive_effect_to_active_effect
from dnd5e_engine.types.effects import ActiveEffect


def hold_person_effect(spell: Spell, source: str, target: str, dc: int) -> ActiveEffect:
    activity = spell.activities[0]
    assert isinstance(activity, SaveActivity)
    binding = activity.effects[0]
    assert binding.lifecycle is not None
    definition = next(p for p in spell.passive_effects if p.id == binding.id)
    return bind_effect_lifecycle(
        passive_effect_to_active_effect(
            definition, target_id=target, caster_id=source, concentration=True
        ),
        binding.lifecycle,
        source_id=source,
        source_kind="spell",
        source_slug=spell.slug,
        activity_id=activity.id,
        save_ability="wis",
        save_dc=dc,
        is_magical=True,
    )
