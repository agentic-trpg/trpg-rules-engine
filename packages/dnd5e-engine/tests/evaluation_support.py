"""Original synthetic rules and encounters for the evaluation migration."""

import asyncio
from datetime import date

from dnd5e_srd_data import MemoryAssetLoader, Provenance, ReviewState
from dnd5e_srd_data.schema.common import ActivationBlock, AttackActivity, DamagePart, Range
from dnd5e_srd_data.schema.item import Weapon

from dnd5e_engine.events import TempHpApplied
from dnd5e_engine.lib_loader import scoped_lib_loader
from dnd5e_engine.orchestrator import _emit, _get_live, start_combat
from dnd5e_engine.specs import EncounterMemberSpec, GridScene, PartyMemberSpec

HERO = "char:synthetic"
FOE = "npc:synthetic"
WEAPON = "synthetic-training-blade"


def synthetic_loader():
    return MemoryAssetLoader(
        items=[
            Weapon(
                slug=WEAPON,
                name="Training blade",
                description="Original synthetic regression asset.",
                weight=1,
                cost_gp=1,
                rarity="common",
                provenance=Provenance(
                    source="foundry",
                    source_url="https://example.invalid/synthetic",
                    ingest_date=date(2026, 10, 10),
                    ingest_version="synthetic/1",
                    srd_version=frozenset({"5.2"}),
                ),
                review=ReviewState(),
                weapon_category="simple_melee",
                damage_parts=[DamagePart(dice="1d6", damage_type="slashing")],
                range=Range(kind="melee", value=5, units="ft"),
                activities=[
                    AttackActivity(
                        id="synthetic-attack",
                        activation=ActivationBlock(type="action", value=1),
                    )
                ],
            )
        ]
    )


def synthetic_combat(*, seed=0, ac=10, hp=100, temp_hp=0):
    loader = synthetic_loader()
    with scoped_lib_loader(loader):
        result = asyncio.run(
            start_combat(
                session_id="synthetic-evaluation",
                party=[
                    PartyMemberSpec(
                        entity_id=HERO,
                        name="Synthetic hero",
                        initiative=20,
                        hp_current=40,
                        hp_max=40,
                        strength=16,
                        attack_bonus=5,
                        equipment=(WEAPON,),
                        zone_id="0,0",
                    )
                ],
                encounter=[
                    EncounterMemberSpec(
                        entity_id=FOE,
                        entity_type="Monster",
                        name="Synthetic sentinel",
                        initiative=1,
                        hp_current=hp,
                        hp_max=hp,
                        ac=ac,
                        zone_id="1,0",
                    )
                ],
                grid_scene=GridScene(width=3, height=3),
                rng_seed=seed,
            )
        )
    live = _get_live(result.handle)
    # The evaluation fixture explicitly declares its equipment facts. The
    # capture adapter is forbidden to invent them from legacy None sentinels.
    live.initiative = [
        actor.model_copy(
            update={
                "weapon_in_hands": WEAPON if actor.entity_id == HERO else None,
                "weapon_grip": "one_handed" if actor.entity_id == HERO else "none",
                "other_hand_occupied": False,
                "weapon_mastery_slugs": (),
            }
        )
        for actor in live.initiative
    ]
    if temp_hp:
        _emit(live, TempHpApplied(target_id=FOE, amount=temp_hp))
    # Baselines begin after initiative/turn initialization, with explicit PRNG state.
    live.rng.seed(seed)
    live.event_log.clear()
    while not live.event_queue.empty():
        live.event_queue.get_nowait()
    return result.handle, live
