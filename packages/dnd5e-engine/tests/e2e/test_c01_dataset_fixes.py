"""C01 — Dataset fixes: typeless damage parts in canonical spells.

Transcribed from specs/e2e-scenario-catalog.md, Cluster 1.
"""

from __future__ import annotations

from dnd5e_engine import PlayerIntent
from dnd5e_engine.events import CastFailed, DamageApplied
from dnd5e_engine.orchestrator import _emit, _get_live, start_combat, submit_player_intent
from dnd5e_engine.specs import EncounterMemberSpec, PartyMemberSpec
from tests.e2e.harness import cell, events_of, grid_scene, run_async


def test_c01_s01_call_lightning_repeat_bolt_applies_lightning_damage(caplog):
    """C01-S01: Call Lightning's repeat-bolt activity applies lightning-typed damage.

    SRD 5.2 §Spell Descriptions (Call Lightning: "taking 3d10 Lightning damage
    on a failed save... you can take a Magic action to call down lightning in
    that way again"); foundry:
    packages/dnd5e-srd-data/raw_sources/foundry/packs/_source/spells24/3rd-level/call-lightning.yml
    (activity `dnd5eactivity200`, `type: damage`, `damage.parts[0].types: []`);
    canonical packages/dnd5e-srd-data/src/dnd5e_srd_data/canonical/spells/call-lightning.json
    (same activity, same empty `types`).
    """

    async def _run():
        start = await start_combat(
            session_id="e2e-c01-s01",
            party=[
                PartyMemberSpec(
                    entity_id="char:druid",
                    name="Druid",
                    initiative=20,
                    hp_current=40,
                    hp_max=40,
                    spells_known=["call-lightning"],
                    spell_slots={3: 1},
                    character_level=5,
                    zone_id=cell(0, 0),
                )
            ],
            encounter=[
                EncounterMemberSpec(
                    entity_id="mon:foe",
                    entity_type="Monster",
                    name="Foe",
                    initiative=1,
                    ac=1,
                    hp_current=200,
                    hp_max=200,
                    zone_id=cell(13, 0),
                )
            ],
            # 65 ft apart, not the mechanical 30 ft: the 60-ft area around the
            # foe must not reach the caster.
            grid_scene=grid_scene(width=14),
            rng_seed=3,
        )
        live = _get_live(start.handle)
        await submit_player_intent(
            start.handle,
            actor_id="char:druid",
            intent=PlayerIntent(
                intent_type="cast_spell", spell_id="call-lightning", target_id="mon:foe"
            ),
        )
        return live

    live = run_async(_run())
    # A complete paid storm is deferred; retain the original typed damage
    # regression at the resource-free Activity primitive boundary.
    assert [e.reason for e in events_of(live, CastFailed)] == ["unsupported_activity"]
    assert live.spell_slots_by_entity["char:druid"][3] == 1
    from dnd5e_srd_data import BundledAssetLoader

    from dnd5e_engine.activities.build_context import build_activity_context
    from dnd5e_engine.activities.resolver import resolve_activity

    spell = BundledAssetLoader().get_spell("call-lightning")
    caster = next(c for c in live.initiative if c.entity_id == "char:druid")
    target = next(c for c in live.initiative if c.entity_id == "mon:foe")
    ctx = build_activity_context(
        caster,
        [target],
        rng=live.rng,
        event_emitter=lambda e: _emit(live, e),
        slot_level=3,
        base_spell_level=3,
        spellcasting_ability="wis",
        concentration=True,
        source_passive_effects=list(spell.passive_effects),
        spell_book={},
        passive_damage_modifiers={},
        save_modifiers={},
    )
    for activity in spell.activities:
        resolve_activity(activity, ctx)
    assert not any("damage_part_untyped" in r.message for r in caplog.records)
    lightning_hits = [
        e
        for e in events_of(live, DamageApplied)
        if e.target_id == "mon:foe" and e.damage_type == "lightning"
    ]
    assert len(lightning_hits) == 2
    amounts = sorted(e.amount for e in lightning_hits)
    initial_bolt, repeat_bolt = amounts
    assert 3 <= initial_bolt <= 30
    assert 4 <= repeat_bolt <= 40


def test_c01_s02_freezing_sphere_damage_parts_apply_cold_damage(caplog):
    """C01-S02: Freezing Sphere's damage parts apply cold-typed damage.

    SRD 5.2 §Spell Descriptions (Freezing Sphere: "taking 10d6 Cold damage on
    failed save or half as much damage on a successful one"); foundry:
    packages/dnd5e-srd-data/raw_sources/foundry/packs/_source/spells24/6th-level/freezing-sphere.yml
    (activities `adCBWrctRmLQmb8M` "Cast and Fire" and `NKBsnjBBIgsaOPaY`
    "Throw Held Globe", both `damage.parts[0].types: []`); canonical
    packages/dnd5e-srd-data/src/dnd5e_srd_data/canonical/spells/freezing-sphere.json
    (same two activities, same empty `types`).
    """

    async def _run():
        start = await start_combat(
            session_id="e2e-c01-s02",
            party=[
                PartyMemberSpec(
                    entity_id="char:wiz",
                    name="Wizard",
                    initiative=20,
                    hp_current=60,
                    hp_max=60,
                    spells_known=["freezing-sphere"],
                    spell_slots={6: 1},
                    character_level=11,
                    zone_id=cell(0, 0),
                )
            ],
            encounter=[
                EncounterMemberSpec(
                    entity_id="mon:foe",
                    entity_type="Monster",
                    name="Foe",
                    initiative=1,
                    ac=1,
                    hp_current=200,
                    hp_max=200,
                    zone_id=cell(13, 0),
                )
            ],
            # 65 ft apart, not the mechanical 30 ft: the 60-ft area around the
            # foe must not reach the caster.
            grid_scene=grid_scene(width=14),
            rng_seed=3,
        )
        live = _get_live(start.handle)
        await submit_player_intent(
            start.handle,
            actor_id="char:wiz",
            intent=PlayerIntent(
                intent_type="cast_spell", spell_id="freezing-sphere", target_id="mon:foe"
            ),
        )
        return live

    live = run_async(_run())
    assert not any("damage_part_untyped" in r.message for r in caplog.records)
    cold_hits = [
        e
        for e in events_of(live, DamageApplied)
        if e.target_id == "mon:foe" and e.damage_type == "cold"
    ]
    assert cold_hits
    assert all(10 <= e.amount <= 60 for e in cold_hits)
