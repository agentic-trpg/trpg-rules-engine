"""Public execution and consumer acceptance for point environmental spells."""

import asyncio

import pytest
from dnd5e_srd_data import BundledAssetLoader

from dnd5e_engine import orchestrator as orch
from dnd5e_engine.activities.passive_stats import CombatantSenses
from dnd5e_engine.events import AreaCreated, CastFailed
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.specs import GridScene
from tests.c21_support import act, combatant, events, foe, pc
from tests.test_execution_integrity import snapshot
from tests.test_spell_effect_selection import take_turn

HERO = "char:hero"
SPELLS = ("fog-cloud", "darkness", "daylight")


@pytest.fixture(autouse=True)
def loader():
    loader = BundledAssetLoader()
    set_lib_loader_for_tests(loader)
    yield loader
    set_lib_loader_for_tests(None)


def table(*, party=None, encounter=None, **scene):
    result = asyncio.run(
        orch.start_combat(
            session_id="environment",
            party=party
            or [
                pc(
                    class_slug="sorcerer",
                    character_level=9,
                    charisma=18,
                    spells_known=list(SPELLS),
                    spell_slots={1: 3, 2: 3, 3: 3, 4: 3},
                )
            ],
            encounter=encounter or [foe(zone_id="15,15")],
            grid_scene=GridScene(width=55, height=35, **scene),
            rng_seed=19,
        )
    )
    return result.handle, orch._get_live(result.handle)


def cast(handle, slug, level, origin="8,0"):
    act(
        handle,
        HERO,
        intent_type="cast_spell",
        spell_id=slug,
        slot_level=level,
        target_zone_id=origin,
    )


@pytest.mark.parametrize("slug,level", [("fog-cloud", 1), ("darkness", 2), ("daylight", 3)])
def test_public_point_cast_registers_authoritative_source_without_rng(slug, level):
    handle, live = table(default_lighting="dark")
    before = live.rng.getstate()
    cast(handle, slug, level)
    assert not events(live, CastFailed)
    assert len(events(live, AreaCreated)) == len(live.persistent_areas.areas) == 1
    assert live.rng.getstate() == before
    if slug == "fog-cloud":
        assert live.topology.obscurement_on_cell("8,0") == "heavy"
    elif slug == "darkness":
        assert not live.topology.can_see("0,0", "8,0", CombatantSenses(darkvision=120))
    else:
        assert live.topology.light_on_cell("8,0") == "bright"


def test_truesight_does_not_pierce_heavy_fog():
    from dnd5e_engine.spatial import GridTopology

    topology = GridTopology(GridScene(width=5, height=5, obscurement_cells={"1,0": "heavy"}))
    assert not topology.can_see("0,0", "1,0", CombatantSenses(truesight=120))


@pytest.mark.parametrize("sense,dim", [("truesight", True), ("blindsight", False)])
def test_special_senses_distinguish_physical_light_obscurement_from_dim_light(sense, dim):
    from dnd5e_engine.live_checks import _dim_for_viewer

    _, live = table(obscurement_cells={"1,0": "light"})
    viewer = combatant(live).model_copy(update={"senses": CombatantSenses(**{sense: 120})})
    assert _dim_for_viewer(live, viewer, "1,0") is dim


@pytest.mark.parametrize(
    "senses", [CombatantSenses(), CombatantSenses(blindsight=120), CombatantSenses(truesight=120)]
)
def test_total_cover_blocks_all_vision_including_special_senses(senses):
    from dnd5e_engine.spatial import GridTopology

    topology = GridTopology(GridScene(width=5, height=5, cover_cells={"1,0": "total"}))
    assert not topology.can_see("0,0", "2,0", senses)


@pytest.mark.parametrize("level,radius", [(1, 20), (2, 40), (3, 60), (4, 80)])
def test_fog_upcast_radius_is_shared_by_events_state_and_projection(level, radius):
    handle, live = table()
    cast(handle, "fog-cloud", level)
    source = orch.get_live(handle).environment_sources[0]
    assert source.radius_ft == events(live, AreaCreated)[0].size_ft == radius
    assert source.spell_level == level
    edge = f"{8 + radius // 5},0"
    outside = f"{9 + radius // 5},0"
    assert live.topology.obscurement_on_cell(edge) == "heavy"
    assert live.topology.obscurement_on_cell(outside) == "none"


@pytest.mark.parametrize(
    "cell,light,sunlight",
    [
        ("8,0", "bright", True),
        ("20,0", "bright", True),
        ("21,0", "dim", False),
        ("32,0", "dim", False),
        ("33,0", "dark", False),
    ],
)
def test_daylight_bright_dim_and_sunlight_boundaries(cell, light, sunlight):
    handle, live = table(default_lighting="dark")
    cast(handle, "daylight", 3)
    assert live.topology.light_on_cell(cell) == light
    assert live.topology.sunlight_on_cell(cell) is sunlight
    assert HERO not in live.concentration_chain
    assert live.topology._default_lighting == "dark"
    assert live.topology._lighting == live.topology._obscurement == {}


@pytest.mark.parametrize("slug,range_ft", [("fog-cloud", 120), ("darkness", 60), ("daylight", 60)])
@pytest.mark.parametrize(
    "senses,visible",
    [
        ({}, False),
        ({"darkvision": 120}, False),
        ({"blindsight": 120}, True),
        ({"truesight": 120}, True),
        ({"tremorsense": 120}, False),
    ],
)
def test_public_sources_feed_distinct_special_senses(slug, range_ft, senses, visible):
    handle, live = table()
    cast(handle, slug, {"fog-cloud": 1, "darkness": 2, "daylight": 3}[slug])
    expected = (
        True
        if slug == "daylight"
        else visible and not (slug == "fog-cloud" and "truesight" in senses)
    )
    assert live.topology.can_see("0,0", "8,0", CombatantSenses(**senses)) is expected
    assert live.topology.has_line_of_sight("0,0", "8,0")  # Fog is not Total Cover.


@pytest.mark.parametrize("slug", ["fog-cloud", "darkness"])
def test_opaque_dynamic_area_intercepts_ray_without_changing_line_of_effect(slug):
    handle, live = table()
    cast(handle, slug, 1 if slug == "fog-cloud" else 2)
    assert not live.topology.can_see("0,0", "15,0")
    assert not live.topology.can_see("15,0", "0,0")
    assert live.topology.has_line_of_sight("0,0", "15,0")
    assert live.topology.can_see("0,0", "15,0", CombatantSenses(blindsight=75))
    assert not live.topology.can_see("0,0", "15,0", CombatantSenses(blindsight=70))


@pytest.mark.parametrize("slug,level", [("fog-cloud", 1), ("darkness", 2)])
@pytest.mark.parametrize("cleanup", ["drop", "duration", "dispel_anchor", "death", "departure"])
def test_concentration_environment_cleanup_restores_static_projection(slug, level, cleanup):
    from dnd5e_engine.events import AreaExpired, Death, EffectExpired

    handle, live = table(lighting={"8,0": "dim"}, obscurement_cells={"8,0": "light"})
    cast(handle, slug, level)
    before = live.rng.getstate()
    take_turn(live)
    if cleanup == "drop":
        act(handle, HERO, intent_type="drop_concentration")
    elif cleanup == "duration":
        live.concentration_rounds_remaining[HERO] = 1
        act(handle, HERO, intent_type="pass")
    elif cleanup == "dispel_anchor":
        target, effect, origin = live.persistent_areas.areas[0].concentration_identity
        orch._emit(
            live,
            EffectExpired(target_id=target, effect_id=effect, origin=origin, reason="dispelled"),
        )
    elif cleanup == "death":
        orch._emit(live, Death(target_id=HERO, reason="damage"))
    else:
        orch._leave_roster(live, HERO, "zero_hp")
    assert not live.persistent_areas.areas
    assert not orch.get_live(handle).environment_sources
    assert len(events(live, AreaExpired)) == 1
    assert live.topology.light_on_cell("8,0") == "dim"
    assert live.topology.obscurement_on_cell("8,0") == "light"
    assert live.rng.getstate() == before


def test_stationary_environment_does_not_follow_caster_and_concentration_replaces_it():
    handle, live = table()
    cast(handle, "fog-cloud", 1)
    original = orch.get_live(handle).environment_sources[0]
    take_turn(live)
    act(handle, HERO, intent_type="move", target_zone_id="0,2")
    assert orch.get_live(handle).environment_sources[0] == original
    take_turn(live)
    cast(handle, "darkness", 2)
    assert [s.source_id for s in orch.get_live(handle).environment_sources] == ["darkness"]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("dark_level,dispelled", [(2, True), (3, True), (4, False)])
def test_daylight_dispels_darkness_by_actual_spell_level_in_either_cast_order(
    reverse, dark_level, dispelled
):
    from dnd5e_engine.events import AreaExpired

    handle, live = table(default_lighting="dark")
    # Independent source ownership, without adding AI or changing the action economy.
    ally = combatant(live).model_copy(update={"entity_id": "char:ally", "initiative": 19})
    orch._insert_into_roster(live, ally, 1, zone_id="0,1")
    live.party_ids.add(ally.entity_id)
    live.spell_slots_by_entity[ally.entity_id] = {3: 3, 4: 3}
    live.spells_known_by_entity[ally.entity_id] = list(SPELLS)
    sequence = [(HERO, "darkness", dark_level), (ally.entity_id, "daylight", 3)]
    for actor, slug, level in reversed(sequence) if reverse else sequence:
        take_turn(live, actor)
        act(
            handle,
            actor,
            intent_type="cast_spell",
            spell_id=slug,
            slot_level=level,
            target_zone_id="8,0",
        )
    assert not events(live, CastFailed)
    sources = orch.get_live(handle).environment_sources
    assert {s.source_id for s in sources} == (
        {"daylight"} if dispelled else {"daylight", "darkness"}
    )
    assert (HERO in live.concentration_chain) is not dispelled
    assert live.topology.light_on_cell("8,0") == "bright"
    assert live.topology.can_see("0,0", "8,0")
    assert len(events(live, AreaExpired)) == int(dispelled)
    if dispelled:
        assert events(live, AreaExpired)[0].reason == "dispelled"
    else:
        daylight = next(a for a in live.persistent_areas.areas if a.source_id == "daylight")
        live.persistent_areas.expire(live, daylight, "duration")
        assert live.topology.light_on_cell("8,0") == "dark"
        assert not live.topology.can_see("0,0", "8,0", CombatantSenses(darkvision=120))


def test_daylight_does_not_dispel_disjoint_darkness_and_fog_stays_opaque():
    handle, live = table()
    cast(handle, "daylight", 3)
    take_turn(live)
    cast(handle, "fog-cloud", 1)
    assert len(orch.get_live(handle).environment_sources) == 2
    assert live.topology.light_on_cell("8,0") == "bright"
    assert live.topology.obscurement_on_cell("8,0") == "heavy"
    assert not live.topology.can_see("0,0", "8,0", CombatantSenses(truesight=120))
    take_turn(live)
    act(handle, HERO, intent_type="drop_concentration")
    assert live.topology.can_see("0,0", "8,0")
    # Darkness placed outside the entire Daylight footprint (including its Dim ring).
    live.actor_zone[HERO] = "40,0"
    take_turn(live)
    cast(handle, "darkness", 2, "45,0")
    assert {s.source_id for s in orch.get_live(handle).environment_sources} == {
        "darkness",
        "daylight",
    }


def test_nonconcentration_duration_uses_round_lifecycle_and_survives_caster_death():
    from dnd5e_engine.events import AreaExpired, Death

    handle, live = table(default_lighting="dark")
    cast(handle, "daylight", 3)
    area = live.persistent_areas.areas[0]
    assert area.duration.rounds == 600
    assert area.environment_expires_round == 601
    orch._emit(live, Death(target_id=HERO, reason="damage"))
    assert orch.get_live(handle).environment_sources
    # Seat the final authoritative round boundary, then execute it through the public turn API.
    live.round_number = 600
    live.current_turn_index = 1
    asyncio.run(orch.advance_monster_turn(handle))
    assert live.round_number == 601
    assert not orch.get_live(handle).environment_sources
    assert events(live, AreaExpired)[-1].reason == "duration"
    assert live.topology.light_on_cell("8,0") == "dark"


@pytest.mark.parametrize("slug", SPELLS)
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"target_zone_id": "99,0"},
        {"target_zone_id": "-1,0"},
        {"target_zone_id": "13,0"},
        {"target_zone_id": "1,0", "target_id": "mon:foe"},
        {"target_zone_id": "1,0", "target_ids": [HERO]},
        {"target_zone_id": "1,0", "excluded_target_ids": []},
        {"target_zone_id": "1,0", "direction": [1, 0]},
    ],
)
def test_invalid_environment_placement_is_refused_before_cost_concentration_or_rng(slug, payload):
    handle, live = table(blocked_cells=["13,0"])
    cast(handle, "fog-cloud", 1)
    take_turn(live)
    before = snapshot(live)
    projection = live.topology.environment_sources
    act(
        handle,
        HERO,
        intent_type="cast_spell",
        spell_id=slug,
        slot_level={"fog-cloud": 1, "darkness": 2, "daylight": 3}[slug],
        **payload,
    )
    assert events(live, CastFailed)
    # The sole allowed mutation is the typed failure event and its publication.
    after = snapshot(live)
    before[0].pop("event_log")
    after[0].pop("event_log")
    assert after[:2] == before[:2]
    assert after[3] == before[3]
    assert live.topology.environment_sources == projection


@pytest.mark.parametrize("slug", SPELLS)
def test_exception_after_real_environment_creation_rolls_back_all_state_events_and_rng(
    monkeypatch, slug
):
    from dnd5e_engine import environment

    handle, live = table(default_lighting="dark")
    cast(handle, "fog-cloud", 1)
    take_turn(live)
    before = snapshot(live)
    spatial_before = dict(vars(live.topology))
    observed = []
    live.event_listeners.append(observed.append)
    original = environment.reconcile_environment

    def broken(combat):
        original(combat)
        assert combat.topology.environment_sources
        assert not combatant(combat).action_available
        combat.rng.randint(1, 20)
        raise RuntimeError("environment fault after registration")

    with monkeypatch.context() as patch:
        patch.setattr(environment, "reconcile_environment", broken)
        with pytest.raises(RuntimeError, match="environment fault"):
            cast(handle, slug, {"fog-cloud": 1, "darkness": 2, "daylight": 3}[slug])
    assert snapshot(live) == before
    assert vars(live.topology) == spatial_before
    assert observed == []


def test_host_strong_wind_dispersal_is_spatial_typed_rng_free_and_leaves_light():
    from dnd5e_engine import StrongWind, apply_strong_wind
    from dnd5e_engine.events import AreaExpired

    handle, live = table()
    cast(handle, "daylight", 3)
    take_turn(live)
    cast(handle, "fog-cloud", 2)
    before = live.rng.getstate()
    asyncio.run(apply_strong_wind(handle, StrongWind(source_id="weather:gust", cells=("17,0",))))
    assert len(live.topology.environment_sources) == 2  # Beyond upcast radius.
    asyncio.run(apply_strong_wind(handle, StrongWind(source_id="weather:gust", cells=("16,0",))))
    assert [s.source_id for s in live.topology.environment_sources] == ["daylight"]
    assert HERO not in live.concentration_chain
    assert live.rng.getstate() == before
    assert events(live, AreaExpired)[-1].reason == "strong_wind"
    assert events(live, AreaExpired)[-1].cause_id == "weather:gust"


@pytest.mark.parametrize("cells", [("99,0",), ("01,0",), ("1,0", "1,0"), ("bad",)])
def test_invalid_host_wind_is_atomic(cells):
    from dnd5e_engine import StrongWind, apply_strong_wind

    handle, live = table()
    cast(handle, "fog-cloud", 1)
    before = snapshot(live)
    with pytest.raises(orch.IntentRejectedError, match="target_invalid"):
        asyncio.run(apply_strong_wind(handle, StrongWind(source_id="weather", cells=cells)))
    assert snapshot(live) == before


def test_environment_replay_matches_events_authoritative_state_and_rng():
    from dnd5e_engine import StrongWind, apply_strong_wind

    def run():
        handle, live = table(default_lighting="dark")
        cast(handle, "daylight", 3)
        take_turn(live)
        cast(handle, "fog-cloud", 2)
        take_turn(live)
        act(
            handle,
            HERO,
            intent_type="check",
            check={
                "actor_id": HERO,
                "ability": "wis",
                "skill": "perception",
                "required_sense": "sight",
                "target_id": "mon:foe",
            },
        )
        asyncio.run(apply_strong_wind(handle, StrongWind(source_id="weather", cells=("8,0",))))
        return snapshot(live), live.topology.environment_sources

    assert run() == run()


@pytest.mark.parametrize(
    "sense,allowed",
    [
        ({}, True),
        ({"darkvision": 120}, True),
        ({"truesight": 120}, True),
        ({"blindsight": 120}, False),
    ],
)
def test_public_hide_consumes_dynamic_fog_and_enemy_senses(sense, allowed):
    from dnd5e_engine.events import CheckRolled

    handle, live = table(encounter=[foe(zone_id="8,0", senses=CombatantSenses(**sense))])
    cast(handle, "fog-cloud", 1, "0,0")
    take_turn(live)
    before = live.rng.getstate()
    if allowed:
        act(handle, HERO, intent_type="hide")
        assert events(live, CheckRolled)[-1].skill == "stealth"
        assert live.rng.getstate() != before
    else:
        with pytest.raises(orch.IntentRejectedError, match="line of sight"):
            act(handle, HERO, intent_type="hide")
        assert live.rng.getstate() == before
        assert combatant(live).action_available


@pytest.mark.parametrize("blindsight", [False, True])
def test_public_attack_visibility_uses_dynamic_environment(blindsight):
    from dnd5e_engine.events import AttackRolled

    handle, live = table(encounter=[foe(zone_id="8,0")])
    cast(handle, "fog-cloud", 1)
    take_turn(live)
    orch._update_combatant(
        live, HERO, senses=CombatantSenses(blindsight=60) if blindsight else CombatantSenses()
    )
    act(handle, HERO, intent_type="attack", weapon_id="shortbow", target_id="mon:foe")
    attack = events(live, AttackRolled)[-1]
    assert attack.sources.count("unseen") == (1 if blindsight else 2)
    assert attack.advantage == ("advantage" if blindsight else "normal")


@pytest.mark.parametrize("blindsight", [False, True])
def test_public_movement_opportunity_attacks_use_dynamic_environment(blindsight):
    from dnd5e_engine.events import AttackRolled

    handle, live = table(
        encounter=[
            foe(
                zone_id="1,0",
                senses=CombatantSenses(blindsight=30) if blindsight else CombatantSenses(),
            )
        ]
    )
    cast(handle, "fog-cloud", 1, "0,0")
    take_turn(live)
    act(handle, HERO, intent_type="move", target_zone_id="0,3")
    attacks = events(live, AttackRolled)
    assert bool(attacks) is blindsight
    assert combatant(live, "mon:foe").reaction_available is not blindsight


@pytest.mark.parametrize("blindsight", [False, True])
def test_public_dodge_only_protects_when_dodger_sees_attacker_in_fog(blindsight):
    from dnd5e_engine.events import AttackRolled

    handle, live = table(encounter=[foe(zone_id="1,0")])
    cast(handle, "fog-cloud", 1, "0,0")
    take_turn(live)
    orch._update_combatant(
        live, HERO, senses=CombatantSenses(blindsight=30) if blindsight else CombatantSenses()
    )
    act(handle, HERO, intent_type="dodge")
    asyncio.run(orch.advance_monster_turn(handle))
    attack = events(live, AttackRolled)[-1]
    assert ("dodge" in attack.sources) is blindsight


@pytest.mark.parametrize(
    "slug,level,sense,auto_failure,mode",
    [
        ("fog-cloud", 1, {}, "sight", "normal"),
        ("fog-cloud", 1, {"truesight": 120}, "sight", "normal"),
        ("fog-cloud", 1, {"blindsight": 120}, None, "normal"),
        ("darkness", 2, {"darkvision": 120}, "sight", "normal"),
        ("darkness", 2, {"truesight": 120}, None, "normal"),
        ("daylight", 3, {}, None, "normal"),
    ],
)
def test_public_sight_required_check_resolves_or_auto_fails_without_random_draw(
    slug, level, sense, auto_failure, mode
):
    from dnd5e_engine.events import CheckRolled

    handle, live = table(encounter=[foe(zone_id="8,0")])
    cast(handle, slug, level)
    take_turn(live)
    orch._update_combatant(live, HERO, senses=CombatantSenses(**sense))
    before = live.rng.getstate()
    act(
        handle,
        HERO,
        intent_type="check",
        check={
            "actor_id": HERO,
            "ability": "wis",
            "skill": "perception",
            "required_sense": "sight",
            "target_id": "mon:foe",
            "dc": 10,
        },
    )
    check = events(live, CheckRolled)[-1]
    assert check.auto_failure == auto_failure
    assert check.advantage == mode
    assert (live.rng.getstate() == before) is (auto_failure is not None)


@pytest.mark.parametrize(
    "sense,mode",
    [
        ({}, "disadvantage"),
        ({"darkvision": 120}, "normal"),
        ({"blindsight": 120}, "normal"),
        ({"truesight": 120}, "normal"),
    ],
)
def test_daylight_dim_ring_changes_only_sight_perception_checks(sense, mode):
    from dnd5e_engine.events import CheckRolled

    handle, live = table(default_lighting="dark", encounter=[foe(zone_id="21,0")])
    cast(handle, "daylight", 3)
    orch._update_combatant(live, HERO, senses=CombatantSenses(**sense))
    take_turn(live)
    act(
        handle,
        HERO,
        intent_type="check",
        check={
            "actor_id": HERO,
            "ability": "wis",
            "skill": "perception",
            "required_sense": "sight",
            "target_id": "mon:foe",
        },
    )
    assert events(live, CheckRolled)[-1].advantage == mode
    take_turn(live)
    act(
        handle,
        HERO,
        intent_type="check",
        check={
            "actor_id": HERO,
            "ability": "wis",
            "skill": "perception",
            "required_sense": "hearing",
            "target_id": "mon:foe",
        },
    )
    assert events(live, CheckRolled)[-1].advantage == "normal"


@pytest.mark.parametrize("slug,level", [("fog-cloud", 1), ("darkness", 2), ("daylight", 3)])
def test_public_counterspell_prevents_environment_creation_and_preserves_existing_cost_rules(
    slug, level
):
    from dnd5e_engine.events import SpellCast
    from tests.c21_support import start
    from tests.test_reaction_runtime import REACTOR, arm, reactor

    handle, live = start(
        [
            reactor(zone_id="1,0"),
            pc(
                class_slug="sorcerer",
                character_level=9,
                charisma=18,
                constitution=-30,
                spells_known=list(SPELLS),
                spell_slots={1: 3, 2: 3, 3: 3},
            ),
        ],
        seed=7,
        encounter=[foe(zone_id="8,8")],
    )
    arm(handle, REACTOR, "counterspell")
    before = dict(live.spell_slots_by_entity[HERO])
    cast(handle, slug, level, "4,0")
    assert events(live, CastFailed)[-1].reason == "countered"
    assert not live.persistent_areas.areas
    assert not live.topology.environment_sources
    assert live.spell_slots_by_entity[HERO] == before
    assert not combatant(live).action_available
    assert [e.spell_id for e in events(live, SpellCast)] == ["counterspell"]


def test_host_wind_fault_after_real_expiry_rolls_back_environment_concentration_and_events(
    monkeypatch,
):
    from dnd5e_engine import StrongWind, apply_strong_wind
    from dnd5e_engine.persistent_areas import PersistentAreaState

    handle, live = table()
    cast(handle, "fog-cloud", 1)
    before = snapshot(live)
    projection = live.topology.environment_sources
    original = PersistentAreaState.expire

    def broken(state, combat, area, reason, **kwargs):
        original(state, combat, area, reason, **kwargs)
        combat.rng.randint(1, 20)
        raise RuntimeError("wind failure after expiry")

    with monkeypatch.context() as patch:
        patch.setattr(PersistentAreaState, "expire", broken)
        with pytest.raises(RuntimeError, match="wind failure"):
            asyncio.run(apply_strong_wind(handle, StrongWind(source_id="weather", cells=("8,0",))))
    assert snapshot(live) == before
    assert live.topology.environment_sources == projection


@pytest.mark.parametrize(
    "light_level,overlap,dispelled",
    [(1, True, True), (2, True, True), (3, True, False), (2, False, False)],
)
def test_shared_darkness_light_dispel_threshold_supports_future_reviewed_light_sources(
    light_level, overlap, dispelled
):
    from dnd5e_engine.environment import reconcile_environment
    from dnd5e_engine.events import AreaExpired

    handle, live = table(default_lighting="dark")
    cast(handle, "daylight", 3)
    light = live.persistent_areas.areas[0]
    # Typed producer fixture for low-level light; this does not admit a new SRD spell.
    light.slot_level = light_level
    light.spec = light.spec.model_copy(
        update={
            "environment": light.spec.environment.model_copy(
                update={"dispels_darkness_through_level": None}
            )
        }
    )
    if not overlap:
        live.actor_zone[HERO] = "40,0"
    take_turn(live)
    cast(handle, "darkness", 2, "8,0" if overlap else "45,0")
    reconcile_environment(live)
    assert (light in live.persistent_areas.areas) is not dispelled
    if dispelled:
        assert events(live, AreaExpired)[0].reason == "dispelled"
        assert events(live, AreaExpired)[0].cause_id == live.persistent_areas.areas[0].id


def test_environment_line_of_effect_respects_walls_and_total_cover():
    from dnd5e_engine.specs import WallSegment

    handle, live = table(
        wall_segments=[WallSegment(x1=10, y1=0, x2=10, y2=35)], cover_cells={"9,1": "total"}
    )
    cast(handle, "daylight", 3)
    assert live.topology.light_on_cell("9,0") == "bright"
    assert live.topology.light_on_cell("10,0") == "bright"  # Static baseline, no dynamic light.
    source = live.topology.environment_sources[0]
    assert "10,0" not in source.cells | source.dim_cells
    assert "9,1" not in source.cells | source.dim_cells
    take_turn(live)
    before = live.rng.getstate()
    cast(handle, "darkness", 2, "10,0")
    assert events(live, CastFailed)[-1].reason == "out_of_range"
    assert live.rng.getstate() == before
    assert len(live.topology.environment_sources) == 1


@pytest.mark.parametrize("slug", SPELLS)
def test_environment_mechanical_tampering_cannot_reuse_admission_identity(loader, slug):
    from dnd5e_srd_data import MemoryAssetLoader

    spell = loader.get_spell(slug)
    activity = spell.activities[0]
    env = activity.persistent_area.environment.model_copy(
        update={"radius_increase_per_slot_ft": 999}
    )
    modified = spell.model_copy(
        update={
            "activities": [
                activity.model_copy(
                    update={
                        "persistent_area": activity.persistent_area.model_copy(
                            update={"environment": env}
                        )
                    }
                )
            ]
        }
    )
    set_lib_loader_for_tests(MemoryAssetLoader(spells=[modified]))
    handle, live = table()
    before = live.rng.getstate()
    cast(handle, slug, spell.level)
    assert events(live, CastFailed)[-1].execution_failure.code == "unreviewed_spell"
    assert combatant(live).action_available
    assert live.rng.getstate() == before
    assert not live.topology.environment_sources


@pytest.mark.parametrize(
    "slug,success", [("daylight", True), ("darkness", False), ("fog-cloud", False)]
)
def test_item_delegated_environment_uses_shared_point_preflight_and_ownership_boundary(
    loader, slug, success
):
    from dnd5e_srd_data import MemoryAssetLoader

    from tests.c21_support import start

    item = loader.get_item("wand-of-fireballs")
    spell = loader.get_spell(slug)
    wrapper = item.activities[0]
    wrapper = wrapper.model_copy(
        update={
            "spell": wrapper.spell.model_copy(
                update={"uuid": spell.foundry_uuid, "level": spell.level}
            )
        }
    )
    set_lib_loader_for_tests(
        MemoryAssetLoader(spells=[spell], items=[item.model_copy(update={"activities": [wrapper]})])
    )
    handle, live = start([pc(equipment=(item.slug,))], seed=19)
    before = live.rng.getstate()
    act(handle, HERO, intent_type="use_item", item_id=item.slug, target_zone_id="4,0")
    assert bool(live.topology.environment_sources) is success
    assert bool(events(live, CastFailed)) is not success
    assert live.rng.getstate() == before
    assert combatant(live).action_available is not success


@pytest.mark.parametrize("slug", SPELLS)
def test_monster_ai_fails_closed_without_explicit_point_carrier(loader, slug):
    from copy import deepcopy

    from dnd5e_srd_data import MemoryAssetLoader

    from dnd5e_engine.events import SpellCast
    from tests.test_monster_spell_delivery import _spell_setup

    live, _actor, _target, monster, _action, spell = _spell_setup(loader, slug)
    live.ruleset_loader = MemoryAssetLoader(monsters=[monster], spells=[spell])
    before = deepcopy(live.monster_action_uses_by_entity), live.rng.getstate()
    asyncio.run(orch.advance_monster_turn(orch.CombatHandle(live.handle_id)))
    assert not events(live, SpellCast)
    assert not live.topology.environment_sources
    assert (live.monster_action_uses_by_entity, live.rng.getstate()) == before


@pytest.mark.parametrize("inside", [True, False])
def test_daylight_sunlight_is_consumed_by_public_trait_checks_per_cell(inside):
    from dnd5e_srd_data.schema.monster import MonsterTraitMechanic

    from dnd5e_engine.events import CheckRolled

    handle, live = table()
    cast(handle, "daylight", 3)
    take_turn(live)
    live.actor_zone[HERO] = "8,0" if inside else "21,0"
    orch._update_combatant(live, HERO, trait_mechanics=[MonsterTraitMechanic.SUNLIGHT_SENSITIVITY])
    act(handle, HERO, intent_type="check", check={"actor_id": HERO, "ability": "str"})
    assert ("trait" in events(live, CheckRolled)[-1].sources) is inside


def test_overlapping_fog_sources_keep_independent_concentration_ownership():
    handle, live = table(
        party=[
            pc(class_slug="sorcerer", spells_known=["fog-cloud"], spell_slots={1: 2}),
            pc(
                "char:ally",
                initiative=19,
                zone_id="0,1",
                class_slug="sorcerer",
                spells_known=["fog-cloud"],
                spell_slots={1: 2},
            ),
        ]
    )
    cast(handle, "fog-cloud", 1)
    act(
        handle,
        "char:ally",
        intent_type="cast_spell",
        spell_id="fog-cloud",
        slot_level=1,
        target_zone_id="8,0",
    )
    view = orch.get_live(handle)
    assert len(view.environment_sources) == len(live.concentration_chain) == 2
    take_turn(live)
    act(handle, HERO, intent_type="drop_concentration")
    assert len(live.topology.environment_sources) == 1
    assert live.topology.environment_sources[0].source_entity_id == "char:ally"
    assert live.topology.obscurement_on_cell("8,0") == "heavy"
    take_turn(live, "char:ally")
    act(handle, "char:ally", intent_type="drop_concentration")
    assert not live.topology.environment_sources
    assert live.topology.obscurement_on_cell("8,0") == "none"
    assert len(view.environment_sources) == 2  # Earlier public snapshot stays stable.


@pytest.mark.parametrize("slug,level", [("fog-cloud", 1), ("darkness", 2), ("daylight", 3)])
def test_environment_events_round_trip_and_close_cleans_projection_idempotently(slug, level):
    from pydantic import TypeAdapter

    from dnd5e_engine.events import AreaExpired, CombatEvent, EffectApplied, SpellCast

    handle, live = table()
    cast(handle, slug, level)
    created = events(live, AreaCreated)[0]
    assert created.environment == live.topology.environment_sources[0].spec
    assert TypeAdapter(CombatEvent).validate_json(created.model_dump_json()) == created
    positions = {
        type(event): index
        for index, event in enumerate(live.event_log)
        if isinstance(event, (AreaCreated, EffectApplied, SpellCast))
    }
    assert positions[SpellCast] < positions[AreaCreated]
    if slug != "daylight":
        assert positions[AreaCreated] < positions[EffectApplied]
    asyncio.run(orch.end_combat(handle))
    assert not orch.get_live(handle).environment_sources
    assert not live.persistent_areas.areas
    expired = events(live, AreaExpired)[-1]
    assert expired.reason == "combat_end"
    assert TypeAdapter(CombatEvent).validate_json(expired.model_dump_json()) == expired
    count = len(live.event_log)
    asyncio.run(orch.end_combat(handle))
    assert len(live.event_log) == count
