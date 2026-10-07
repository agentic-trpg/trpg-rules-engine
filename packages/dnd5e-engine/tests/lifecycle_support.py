"""Explicit typed registrations for lifecycle-focused test fixtures."""

from dnd5e_srd_data.schema.lifecycle import EffectLifecycleSpec, RepeatSaveSpec

from dnd5e_engine.effect_lifecycle import EffectLifecycleApplication, OngoingEffectLifecycle
from dnd5e_engine.types.effects import ActiveEffect


def seed_repeat_save(
    live,
    identity,
    *,
    ability="wis",
    dc=30,
    source_id="mon:foe",
    is_magical=False,
):
    application = EffectLifecycleApplication(
        spec=EffectLifecycleSpec(repeat_save=RepeatSaveSpec()),
        source_id=source_id,
        source_kind="spell" if is_magical else "feature",
        source_slug="test-source",
        activity_id="test-activity",
        save_ability=ability,
        save_dc=dc,
        is_magical=is_magical,
    )
    live.effect_lifecycles[identity] = OngoingEffectLifecycle.from_application(
        identity, application, live.turn_serial, live.round_number
    )
    target_id, effect_id, origin = identity
    if not any(
        effect.id == effect_id and effect.origin == origin
        for effect in live.active_effects.get(target_id, [])
    ):
        live.active_effects.setdefault(target_id, []).append(
            ActiveEffect(id=effect_id, name="Test lifecycle", origin=origin, target_id=target_id)
        )
