"""Public object/environment execution, trusted Host boundaries and transactions."""

import asyncio
from copy import deepcopy

import pytest
from dnd5e_srd_data import BundledAssetLoader
from pydantic import ValidationError

from dnd5e_engine import (
    CombatObject,
    ObjectMutation,
    get_live,
    mutate_combat_object,
    register_combat_objects,
)
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.events import AreaCreated, AreaExpired, CastFailed, CombatObjectChanged, Death
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.persistent_areas import FollowObjectEmanation
from dnd5e_engine.specs import GridScene
from tests.c21_support import act, combatant, events, foe, pc
from tests.test_execution_integrity import snapshot
from tests.test_moving_persistent_areas import until


def next_turn(handle, live):
    if live.initiative[live.current_turn_index].entity_id == HERO:
        act(handle, HERO, intent_type="pass")
    until(handle, live, HERO)


HERO = "char:hero"
ALLY = "char:ally"
OWNER = "host:scene"
OBJECT = "object:stone"


@pytest.fixture(autouse=True)
def loader():
    set_lib_loader_for_tests(BundledAssetLoader())
    yield
    set_lib_loader_for_tests(None)


def table(*, hero="8,3", ally="7,3", encounter=None, **scene):
    fields = dict(
        class_slug="sorcerer",
        character_level=11,
        charisma=18,
        constitution=-30,
        hp_current=500,
        hp_max=500,
        spells_known=["darkness", "daylight", "fog-cloud", "moonbeam", "thunderwave"],
        spell_slots={1: 5, 2: 5, 3: 5, 4: 5, 6: 5},
    )
    result = asyncio.run(
        orch.start_combat(
            session_id="objects",
            rng_seed=19,
            party=[
                pc(**fields, zone_id=hero, initiative=30),
                pc(ALLY, **fields, zone_id=ally, initiative=20),
            ],
            encounter=encounter or [foe(zone_id="59,24")],
            grid_scene=GridScene(width=60, height=25, default_lighting="dark", **scene),
        )
    )
    return result.handle, orch._get_live(result.handle)


def obj(identity=OBJECT, position="8,3", **kwargs):
    return CombatObject(
        id=identity, owner_id=OWNER, source_id="host:initial", position=position, **kwargs
    )


def register(handle, *records, owner=OWNER):
    asyncio.run(register_combat_objects(handle, owner_id=owner, objects=records))


def change(handle, operation, identity=OBJECT, owner=OWNER, **kwargs):
    asyncio.run(
        mutate_combat_object(
            handle,
            owner_id=owner,
            mutation=ObjectMutation(
                object_id=identity,
                source_id="host:interaction",
                operation=operation,
                **kwargs,
            ),
        )
    )


def cast(handle, slug="darkness", *, actor=HERO, identity=OBJECT, level=None, **kwargs):
    act(
        handle,
        actor,
        intent_type="cast_spell",
        spell_id=slug,
        slot_level=level or {"darkness": 2, "daylight": 3}[slug],
        target_object_id=identity,
        **kwargs,
    )


def whole(live):
    return snapshot(live), deepcopy(vars(live.topology))


@pytest.mark.parametrize("slug,radius", [("darkness", 15), ("daylight", 60)])
@pytest.mark.parametrize("include", [False, True])
def test_actual_cast_consumes_foreign_owned_object_true_emanation_and_colocated_creatures(
    slug, radius, include
):
    handle, live = table(hero="8,3", ally="8,4")
    register(handle, obj())
    rng = live.rng.getstate()
    slots = live.spell_slots_by_entity[HERO].copy()
    cast(handle, slug, object_include_origin=include)
    area = live.persistent_areas.areas[0]
    assert isinstance(area.geometry, FollowObjectEmanation)
    assert area.geometry.object_id == OBJECT
    assert area.geometry.includes_origin_object is include
    assert area.template.shape == "emanation"
    assert area.template.size_ft == radius
    assert area.contains(live, HERO)
    assert area.contains(live, ALLY)
    created = events(live, AreaCreated)[0]
    assert created.placement == "follow-object"
    assert created.shape == "emanation"
    assert created.origin_object_id == OBJECT
    assert created.includes_origin_object is include
    view = get_live(handle)
    source = view.environment_sources[0]
    assert source.origin_object_id == OBJECT
    assert source.includes_origin_object is include
    assert "8,3" in source.cells
    assert f"{8 + radius // 5},3" in source.cells
    assert f"{9 + radius // 5},3" not in source.cells
    assert view.combat_objects[0].area_ids == (area.id,)
    assert view.combat_objects[0].object.owner_id != HERO
    assert (
        live.spell_slots_by_entity[HERO][area.base_spell_level] == slots[area.base_spell_level] - 1
    )
    assert not combatant(live).action_available
    assert live.rng.getstate() == rng
    change(handle, "pickup", holder_id=HERO)
    assert get_live(handle).environment_sources[0].origin == live.actor_zone[HERO]
    assert live.topology.light_on_cell("8,3") == ("dark" if slug == "darkness" else "bright")
    # Snapshots are immutable and stable after later authoritative operations.
    assert view.combat_objects[0].object.disposition == "unattended"


@pytest.mark.parametrize("slug", ["darkness", "daylight"])
@pytest.mark.parametrize("disposition", ["carried", "worn"])
def test_initial_worn_or_carried_refused_before_payment_or_concentration(slug, disposition):
    handle, live = table()
    register(handle, obj(position=None, disposition=disposition, holder_id=HERO))
    before = whole(live)
    cast(handle, slug)
    assert events(live, CastFailed)[-1].reason == "target_invalid"
    after = whole(live)
    before[0][0].pop("event_log")
    after[0][0].pop("event_log")
    assert after[0][:2] == before[0][:2]
    assert after[1] == before[1]
    assert not live.persistent_areas.areas
    assert not live.concentration_chain


@pytest.mark.parametrize(
    "payload",
    [
        {"identity": "object:missing"},
        {"identity": HERO},
        {"target_zone_id": "8,3"},
        {"target_id": ALLY},
        {"target_ids": [ALLY]},
        {"excluded_target_ids": []},
        {"direction": (1, 0)},
    ],
)
def test_illegal_cast_inputs_preserve_existing_source_costs_rng(payload):
    handle, live = table()
    register(handle, obj())
    act(
        handle,
        HERO,
        intent_type="cast_spell",
        spell_id="fog-cloud",
        slot_level=1,
        target_zone_id="8,3",
    )
    next_turn(handle, live)
    before = whole(live)
    cast(handle, **payload)
    after = whole(live)
    assert events(live, CastFailed)
    before[0][0].pop("event_log")
    after[0][0].pop("event_log")
    assert after[0][:2] == before[0][:2]
    assert after[1] == before[1]


@pytest.mark.parametrize(
    "payload",
    [
        {"position": "08,3"},
        {"position": "99,3"},
        {"position": "10,3"},
        {"position": None, "holder_id": "char:absent", "disposition": "carried"},
    ],
)
def test_invalid_snapshot_is_atomic(payload):
    handle, live = table(blocked_cells=["10,3"] if payload == {"position": "10,3"} else [])
    before = whole(live)
    with pytest.raises(orch.IntentRejectedError):
        register(handle, obj(**payload))
    assert whole(live) == before


def test_duplicate_cross_owner_and_tombstoned_id_are_rejected_without_events():
    handle, live = table()
    before = whole(live)
    with pytest.raises(orch.IntentRejectedError):
        register(handle, obj(), obj())
    assert whole(live) == before
    with pytest.raises(orch.IntentRejectedError):
        register(handle, obj(), owner="host:other")
    assert whole(live) == before
    register(handle, obj())
    before = whole(live)
    for operation in ["cover", "remove", "move", "pickup"]:
        inputs = (
            {"position": "9,3"}
            if operation == "move"
            else {"holder_id": HERO}
            if operation == "pickup"
            else {}
        )
        with pytest.raises(orch.IntentRejectedError):
            change(handle, operation, owner="host:other", **inputs)
        assert whole(live) == before
    with pytest.raises(orch.IntentRejectedError):
        register(handle, obj())
    assert whole(live) == before
    change(handle, "remove")
    before = whole(live)
    with pytest.raises(orch.IntentRejectedError):
        register(handle, obj())
    assert whole(live) == before


@pytest.mark.parametrize(
    "payload",
    [
        {"position": None},
        {"position": "8,3", "holder_id": HERO},
        {"position": "8,3", "disposition": "carried", "holder_id": HERO},
        {"position": None, "disposition": "worn"},
        {"opaque_cover": "yes"},
        {"id": HERO},
        {"id": "object:"},
        {"owner_id": ""},
        {"source_id": ""},
    ],
)
def test_object_schema_is_closed_strict_and_single_position(payload):
    data = obj().model_dump() | payload
    with pytest.raises(ValidationError):
        CombatObject.model_validate(data)


@pytest.mark.parametrize(
    "payload",
    [
        {"operation": "drop", "position": "1,0"},
        {"operation": "move"},
        {"operation": "pickup"},
        {"operation": "cover", "holder_id": HERO},
        {"operation": "invented"},
        {"operation": "cover", "source_id": ""},
    ],
)
def test_mutation_schema_rejects_ambiguous_or_missing_inputs(payload):
    with pytest.raises(ValidationError):
        ObjectMutation.model_validate({"object_id": OBJECT, "source_id": "host:a"} | payload)


@pytest.mark.parametrize("slug", ["darkness", "daylight"])
def test_cover_carry_wear_voluntary_movement_drop_move_restore_original_clock(slug):
    handle, live = table()
    register(handle, obj())
    cast(handle, slug)
    original = live.persistent_areas.areas[0]
    end_round = original.environment_expires_round
    change(handle, "pickup", holder_id=HERO)
    change(handle, "wear", holder_id=HERO)
    change(handle, "cover")
    assert get_live(handle).environment_sources[0].suppressed
    assert not get_live(handle).environment_sources[0].cells
    assert not get_live(handle).environment_sources[0].dim_cells
    assert live.topology.light_on_cell("8,3") == "dark"
    next_turn(handle, live)
    clocks = dict(live.concentration_rounds_remaining)
    act(handle, HERO, intent_type="move", target_zone_id="8,5")
    assert get_live(handle).combat_objects[0].position == "8,5"
    assert get_live(handle).environment_sources[0].origin == "8,5"
    rng = live.rng.getstate()
    action = combatant(live).action_available
    change(handle, "uncover")
    assert live.persistent_areas.areas[0] is original
    assert original.environment_expires_round == end_round
    assert live.concentration_rounds_remaining == clocks
    assert combatant(live).action_available == action
    assert live.rng.getstate() == rng
    source = get_live(handle).environment_sources[0]
    assert not source.suppressed
    assert "8,5" in source.cells
    change(handle, "drop")
    assert get_live(handle).combat_objects[0].object.position == "8,5"
    act(handle, HERO, intent_type="move", target_zone_id="8,6")
    assert get_live(handle).environment_sources[0].origin == "8,5"
    change(handle, "move", position="20,10")
    assert get_live(handle).environment_sources[0].origin == "20,10"
    assert live.topology._lighting == {}
    assert live.topology._obscurement == {}
    assert len(events(live, AreaCreated)) == 1


@pytest.mark.parametrize(
    "op,kwargs",
    [
        ("pickup", {"holder_id": ALLY}),
        ("wear", {"holder_id": "char:missing"}),
        ("move", {"position": "99,0"}),
        ("move", {"position": "08,3"}),
        ("drop", {}),
    ],
)
def test_invalid_operations_are_draw_free_and_atomic(op, kwargs):
    handle, live = table()
    register(handle, obj())
    cast(handle)
    before = whole(live)
    with pytest.raises(orch.IntentRejectedError):
        change(handle, op, **kwargs)
    assert whole(live) == before


def test_held_object_cannot_be_moved_or_stolen_by_holder_override():
    handle, live = table(hero="8,3", ally="8,4")
    register(handle, obj())
    change(handle, "pickup", holder_id=HERO)
    live.actor_zone[ALLY] = "8,3"  # isolated co-location geometry fixture
    before = whole(live)
    for op, kwargs in [
        ("move", {"position": "9,3"}),
        ("pickup", {"holder_id": ALLY}),
        ("wear", {"holder_id": ALLY}),
    ]:
        with pytest.raises(orch.IntentRejectedError):
            change(handle, op, **kwargs)
        assert whole(live) == before


@pytest.mark.parametrize("mode", ["point", "object"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("offset,overlap", [(15, True), (16, False), (27, False), (28, False)])
@pytest.mark.parametrize("dark_level", [2, 3, 4])
def test_daylight_primary_area_dispel_boundary_both_modes_orders_and_actual_levels(
    mode, reverse, offset, overlap, dark_level
):
    dark_cell = f"{8 + offset},3"
    handle, live = table(hero="8,3", ally=dark_cell)
    if mode == "object":
        register(handle, obj("object:day"), obj("object:dark", dark_cell))
    sequence = [
        (HERO, "daylight", 6, "object:day", "8,3"),
        (ALLY, "darkness", dark_level, "object:dark", dark_cell),
    ]
    for actor, slug, level, identity, cell in reversed(sequence) if reverse else sequence:
        until(handle, live, actor)
        if mode == "object":
            cast(handle, slug, actor=actor, level=level, identity=identity)
        else:
            act(
                handle,
                actor,
                intent_type="cast_spell",
                spell_id=slug,
                slot_level=level,
                target_zone_id=cell,
            )
    assert not events(live, CastFailed)
    dispelled = overlap and dark_level <= 3
    assert {s.source_id for s in get_live(handle).environment_sources} == (
        {"daylight"} if dispelled else {"daylight", "darkness"}
    )
    assert (ALLY in live.concentration_chain) is not dispelled
    assert len(events(live, AreaExpired)) == int(dispelled)
    if offset == 16:
        # The near edge of Darkness lies only in emitted Dim illumination.
        assert live.topology.light_on_cell("21,3") == "dim"
        assert not live.topology.sunlight_on_cell("21,3")


@pytest.mark.parametrize("covered", ["daylight", "darkness"])
@pytest.mark.parametrize("reverse", [False, True])
def test_cover_blocks_projection_and_all_dispels_move_then_uncover(covered, reverse):
    handle, live = table(hero="8,3", ally="30,3")
    register(
        handle,
        obj("object:day"),
        obj("object:dark", "30,3"),
    )
    sequence = [(HERO, "daylight", "object:day"), (ALLY, "darkness", "object:dark")]
    for actor, spell, identity in reversed(sequence) if reverse else sequence:
        until(handle, live, actor)
        cast(handle, spell, actor=actor, identity=identity)
    identity = "object:day" if covered == "daylight" else "object:dark"
    change(handle, "cover", identity=identity)
    change(handle, "move", identity=identity, position="30,3" if covered == "daylight" else "8,3")
    assert len(live.persistent_areas.areas) == 2
    assert not events(live, AreaExpired)
    change(handle, "uncover", identity=identity)
    assert [s.source_id for s in get_live(handle).environment_sources] == ["daylight"]
    assert events(live, AreaExpired)[0].reason == "dispelled"
    assert ALLY not in live.concentration_chain


@pytest.mark.parametrize("slug", ["darkness", "daylight"])
@pytest.mark.parametrize(
    "ending",
    [
        "remove",
        "caster_death",
        "caster_left",
        "holder_death",
        "holder_left",
        "combat_end",
        "duration",
        "drop_concentration",
    ],
)
def test_object_and_spell_lifecycles_have_no_ghosts_and_distinct_owners(slug, ending):
    handle, live = table(hero="8,3", ally="8,4")
    register(handle, obj())
    cast(handle, slug)
    change(handle, "move", position="8,4")
    change(handle, "pickup", holder_id=ALLY)
    change(handle, "cover")
    if ending == "remove":
        change(handle, "remove")
    elif ending in ("caster_death", "holder_death"):
        orch._emit(
            live, Death(target_id=HERO if ending == "caster_death" else ALLY, reason="damage")
        )
    elif ending in ("caster_left", "holder_left"):
        orch._leave_roster(live, HERO if ending == "caster_left" else ALLY, "zero_hp")
    elif ending == "combat_end":
        asyncio.run(orch.end_combat(handle))
    elif ending == "duration":
        if slug == "darkness":
            live.concentration_rounds_remaining[HERO] = 1
            next_turn(handle, live)
            act(handle, HERO, intent_type="pass")
        else:
            live.round_number = 600
            next_turn(handle, live)
    else:
        until(handle, live, HERO)
        act(handle, HERO, intent_type="drop_concentration")
    survives = ending == "holder_death" or (
        slug == "daylight" and ending in ("caster_death", "drop_concentration")
    )
    assert bool(live.persistent_areas.areas) is survives
    assert bool(get_live(handle).environment_sources) is survives
    assert (
        not get_live(handle).environment_sources
        or get_live(handle).environment_sources[0].suppressed
    )
    if ending == "holder_death":
        view = get_live(handle).combat_objects[0]
        assert view.object.disposition == "unattended"
        assert view.position == "8,4"
        change(handle, "uncover")
        assert get_live(handle).environment_sources[0].cells
    if ending in ("holder_left", "remove", "combat_end"):
        assert not get_live(handle).combat_objects


@pytest.mark.parametrize("fault", [False, True])
def test_public_forced_carrier_movement_uses_authoritative_holder_position_and_rollback(
    monkeypatch, fault
):
    from dnd5e_engine import environment

    handle, live = table()
    register(handle, obj())
    cast(handle, "daylight")
    change(handle, "pickup", holder_id=HERO)
    until(handle, live, ALLY)
    before = whole(live)
    observed = []
    live.event_listeners.append(observed.append)
    original = environment.reconcile_environment

    def broken(combat):
        original(combat)
        assert get_live(handle).environment_sources[0].origin == combat.actor_zone[HERO]
        assert combat.actor_zone[HERO] != "8,3"
        raise RuntimeError("forced object fault")

    if fault:
        monkeypatch.setattr(environment, "reconcile_environment", broken)

    def invoke():
        act(
            handle,
            ALLY,
            intent_type="cast_spell",
            spell_id="thunderwave",
            slot_level=1,
            direction=(1, 0),
        )

    if fault:
        with pytest.raises(RuntimeError, match="forced object fault"):
            invoke()
        assert whole(live) == before
        assert observed == []
    else:
        invoke()
        assert get_live(handle).combat_objects[0].position == live.actor_zone[HERO] == "10,3"
        assert get_live(handle).environment_sources[0].origin == "10,3"


@pytest.mark.parametrize(
    "operation,kwargs", [("cover", {}), ("move", {"position": "10,3"}), ("remove", {})]
)
def test_host_mutation_fault_restores_all_authority_projection_events_rng(
    monkeypatch, operation, kwargs
):
    from dnd5e_engine import environment

    handle, live = table()
    register(handle, obj())
    cast(handle)
    before = whole(live)
    observed = []
    live.event_listeners.append(observed.append)
    original = environment.reconcile_environment

    def broken(combat):
        original(combat)
        combat.rng.randint(1, 20)
        raise RuntimeError("object mutation fault")

    monkeypatch.setattr(environment, "reconcile_environment", broken)
    with pytest.raises(RuntimeError, match="object mutation fault"):
        change(handle, operation, **kwargs)
    assert whole(live) == before
    assert observed == []


def test_snapshot_creation_fault_and_real_cast_fault_restore_identity_registry(monkeypatch):
    from dnd5e_engine import combat_objects, environment

    handle, live = table()
    before = whole(live)
    original = combat_objects._changed

    def broken(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("creation fault")

    with monkeypatch.context() as patch:
        patch.setattr(combat_objects, "_changed", broken)
        with pytest.raises(RuntimeError, match="creation fault"):
            register(handle, obj())
    assert whole(live) == before
    register(handle, obj())
    before = whole(live)

    def cast_broken(combat):
        assert combat.persistent_areas.areas
        assert not combatant(combat).action_available
        combat.rng.randint(1, 20)
        raise RuntimeError("cast object fault")

    monkeypatch.setattr(environment, "reconcile_environment", cast_broken)
    with pytest.raises(RuntimeError, match="cast object fault"):
        cast(handle)
    assert whole(live) == before


def test_same_seed_replay_preserves_full_state_projection_event_order_and_rng():
    def run():
        handle, live = table(hero="8,3", ally="30,3")
        register(handle, obj("object:day"), obj("object:dark", "30,3"))
        cast(handle, "daylight", identity="object:day")
        change(handle, "cover", identity="object:day")
        until(handle, live, ALLY)
        cast(handle, "darkness", actor=ALLY, identity="object:dark")
        change(handle, "move", identity="object:day", position="30,3")
        change(handle, "uncover", identity="object:day")
        changed = events(live, CombatObjectChanged)[-1]
        expired = events(live, AreaExpired)[-1]
        assert live.event_log.index(changed) < live.event_log.index(expired)
        before = get_live(handle)
        assert before.combat_objects[1].area_ids == ()
        change(handle, "remove", identity="object:day")
        state, topology = whole(live)
        state[0].pop("handle_id")
        return state, topology

    assert run() == run()


@pytest.mark.parametrize("include", [False, True])
def test_object_origin_exclusion_does_not_exclude_other_creatures_in_origin_cell(include):
    handle, live = table()
    register(handle, obj())
    cast(handle, "daylight", object_include_origin=include)
    change(handle, "pickup", holder_id=HERO)
    # Ordinary combat starts/moves forbid occupied end cells. This isolated
    # geometry fixture also verifies co-location supplied by a future Host model.
    live.actor_zone[ALLY] = live.actor_zone[HERO]
    area = live.persistent_areas.areas[0]
    assert area.contains(live, HERO)
    assert area.contains(live, ALLY)
    assert live.topology.light_on_cell(live.actor_zone[ALLY]) == "bright"


@pytest.mark.parametrize("slug", ["darkness", "daylight"])
@pytest.mark.parametrize("invalid", ["range", "cover", "opaque"])
def test_object_cast_clear_path_and_range_fail_before_payment(slug, invalid):
    scene = {"cover_cells": {"9,3": "total"}} if invalid == "cover" else {}
    handle, live = table(**scene)
    register(
        handle,
        obj(position="30,3" if invalid == "range" else "10,3", opaque_cover=invalid == "opaque"),
    )
    before = whole(live)
    cast(handle, slug)
    assert events(live, CastFailed)
    after = whole(live)
    before[0][0].pop("event_log")
    after[0][0].pop("event_log")
    assert after[0][:2] == before[0][:2]
    assert after[1] == before[1]


@pytest.mark.parametrize("slug", ["fog-cloud", "thunderwave"])
@pytest.mark.parametrize("payload", [{"target_object_id": OBJECT}, {"object_include_origin": True}])
def test_unsupported_object_delivery_never_silently_pays(slug, payload):
    handle, live = table()
    register(handle, obj())
    before = whole(live)
    act(
        handle,
        HERO,
        intent_type="cast_spell",
        spell_id=slug,
        slot_level=1,
        target_zone_id="10,3",
        **payload,
    )
    assert events(live, CastFailed)
    after = whole(live)
    before[0][0].pop("event_log")
    after[0][0].pop("event_log")
    assert after[0][:2] == before[0][:2]
    assert after[1] == before[1]


@pytest.mark.parametrize("slug", ["darkness", "daylight"])
def test_object_counterspell_creates_no_anchor_or_source_and_preserves_slot_rules(slug):
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
                spells_known=[slug],
                spell_slots={2: 3, 3: 3},
            ),
        ],
        seed=7,
        encounter=[foe(zone_id="8,8")],
    )
    register(handle, obj(position="4,0"))
    arm(handle, REACTOR, "counterspell")
    slots = live.spell_slots_by_entity[HERO].copy()
    cast(handle, slug)
    assert events(live, CastFailed)[-1].reason == "countered"
    assert not live.persistent_areas.areas
    assert not get_live(handle).environment_sources
    assert not get_live(handle).combat_objects[0].area_ids
    assert live.spell_slots_by_entity[HERO] == slots
    assert not combatant(live).action_available


def test_multiple_daylight_sources_cover_remove_and_round_clock_after_caster_death():
    handle, live = table()
    register(handle, obj(), obj("object:second", "7,3"))
    cast(handle, "daylight")
    until(handle, live, ALLY)
    cast(handle, "daylight", actor=ALLY, identity="object:second")
    change(handle, "cover")
    assert live.topology.light_on_cell("8,3") == "bright"
    change(handle, "remove", identity="object:second")
    assert live.topology.light_on_cell("8,3") == "dark"
    orch._emit(live, Death(target_id=HERO, reason="damage"))
    assert len(live.persistent_areas.areas) == 1
    live.round_number = 600
    until(handle, live, ALLY)
    act(handle, ALLY, intent_type="pass")
    # Advance the existing public round lifecycle without relying on the dead caster.
    for _ in range(5):
        if live.round_number >= 601:
            break
        current = live.initiative[live.current_turn_index].entity_id
        if current in live.party_ids:
            act(handle, current, intent_type="pass")
        else:
            asyncio.run(orch.advance_monster_turn(handle))
    assert not live.persistent_areas.areas
    assert not get_live(handle).environment_sources
    change(handle, "uncover")
    assert not get_live(handle).environment_sources


def test_ended_combat_refuses_object_creation_and_mutation():
    handle, live = table()
    register(handle, obj())
    asyncio.run(orch.end_combat(handle))
    before = whole(live)
    with pytest.raises(orch.IntentRejectedError, match="combat_ended"):
        register(handle, obj("object:new"))
    with pytest.raises(orch.IntentRejectedError, match="combat_ended"):
        change(handle, "cover")
    assert whole(live) == before


def test_real_lethal_spell_drops_monster_held_object_and_independent_daylight_survives():
    handle, live = table(encounter=[foe(zone_id="9,3", hp_current=1)])
    register(handle, obj(position="9,3"))
    cast(handle, "daylight")
    change(handle, "pickup", holder_id="mon:foe")
    until(handle, live, HERO)
    act(
        handle,
        HERO,
        intent_type="cast_spell",
        spell_id="thunderwave",
        slot_level=1,
        direction=(1, 0),
    )
    assert "mon:foe" in live.dead_ids
    view = get_live(handle).combat_objects[0]
    assert view.object.disposition == "unattended"
    assert view.position == "9,3"
    assert view.area_ids == ("area:0",)
    assert get_live(handle).environment_sources[0].origin == "9,3"
    assert len([e for e in events(live, CombatObjectChanged) if e.operation == "holder_died"]) == 1
    assert not events(live, AreaExpired)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("moon_level", [2, 3])
def test_actual_darkness_dispels_actual_moonbeam_dim_light_only_through_fixed_level_two(
    reverse, moon_level
):
    handle, live = table(hero="8,3", ally="15,3")
    register(handle, obj(position="8,3"))
    sequence = [(HERO, "darkness"), (ALLY, "moonbeam")]
    for actor, spell in reversed(sequence) if reverse else sequence:
        until(handle, live, actor)
        if spell == "darkness":
            cast(handle, level=6)
        else:
            act(
                handle,
                actor,
                intent_type="cast_spell",
                spell_id=spell,
                slot_level=moon_level,
                target_zone_id="12,3",
            )
    assert not events(live, CastFailed)
    assert {s.source_id for s in get_live(handle).environment_sources} == (
        {"darkness"} if moon_level == 2 else {"darkness", "moonbeam"}
    )
    assert (ALLY in live.concentration_chain) is (moon_level > 2)


def test_mutation_fault_after_actual_overlap_dispel_restores_both_sources_and_concentration(
    monkeypatch,
):
    from dnd5e_engine import environment

    handle, live = table(hero="8,3", ally="30,3")
    register(handle, obj("object:day"), obj("object:dark", "30,3"))
    cast(handle, "daylight", identity="object:day")
    until(handle, live, ALLY)
    cast(handle, actor=ALLY, identity="object:dark")
    before = whole(live)
    original = environment.reconcile_environment

    def broken(combat):
        original(combat)
        assert ALLY not in combat.concentration_chain
        assert [s.source_id for s in combat.topology.environment_sources] == ["daylight"]
        combat.rng.randint(1, 20)
        raise RuntimeError("overlap dispel fault")

    monkeypatch.setattr(environment, "reconcile_environment", broken)
    with pytest.raises(RuntimeError, match="overlap dispel fault"):
        change(handle, "move", identity="object:day", position="30,3")
    assert whole(live) == before


@pytest.mark.parametrize(
    "slug,legal", [("daylight", True), ("darkness", False), ("fog-cloud", False)]
)
def test_delegated_object_delivery_is_validated_before_item_costs(slug, legal):
    from dnd5e_srd_data import MemoryAssetLoader

    from tests.c21_support import start

    loader = BundledAssetLoader()
    item, spell = loader.get_item("wand-of-fireballs"), loader.get_spell(slug)
    wrapper = item.activities[0]
    wrapper = wrapper.model_copy(
        update={
            "spell": wrapper.spell.model_copy(
                update={
                    "uuid": spell.foundry_uuid,
                    "level": spell.level,
                }
            )
        }
    )
    set_lib_loader_for_tests(
        MemoryAssetLoader(spells=[spell], items=[item.model_copy(update={"activities": [wrapper]})])
    )
    handle, live = start([pc(equipment=(item.slug,))], seed=19)
    register(handle, obj(position="4,0"))
    before = whole(live)
    act(handle, HERO, intent_type="use_item", item_id=item.slug, target_object_id=OBJECT)
    assert bool(get_live(handle).environment_sources) is legal
    assert bool(events(live, CastFailed)) is not legal
    if legal:
        assert isinstance(live.persistent_areas.areas[0].geometry, FollowObjectEmanation)
        assert get_live(handle).environment_sources[0].origin == "4,0"
        assert events(live, AreaCreated)[0].origin == "4,0"
        assert live.persistent_areas.areas[0].cast_origin == "4,0"
        assert not combatant(live).action_available
    else:
        after = whole(live)
        before[0][0].pop("event_log")
        after[0][0].pop("event_log")
        assert after[0][:2] == before[0][:2]
        assert after[1] == before[1]
