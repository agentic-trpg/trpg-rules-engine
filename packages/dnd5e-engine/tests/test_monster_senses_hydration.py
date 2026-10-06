"""Template senses cross the spec boundary into existing visibility consumers."""

from __future__ import annotations

import random

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader
from pydantic import ValidationError

from dnd5e_engine import PlayerIntent
from dnd5e_engine import orchestrator as orch
from dnd5e_engine.activities.passive_stats import CombatantSenses
from dnd5e_engine.events import AttackRolled, CheckRolled
from dnd5e_engine.rules import conditions as rules
from dnd5e_engine.specs import EncounterMemberSpec, GridScene, PartyMemberSpec, WallSegment
from dnd5e_engine.types.conditions import ActiveCondition
from dnd5e_engine.types.effects import ActiveEffect
from tests.e2e.harness import run_async

HERO, FOE = "char:aaaaaaaaaaaa", "mon:bbbbbbbbbbbb"
_BASE_RANDOM = random.Random
_OMITTED = object()
TEMPLATES = [
    ("goblin-warrior", CombatantSenses(darkvision=60)),
    ("animated-armor", CombatantSenses(blindsight=60)),
    ("avatar-of-death", CombatantSenses(truesight=60)),
    ("earth-elemental", CombatantSenses(darkvision=60, tremorsense=60)),
    ("bulette", CombatantSenses(darkvision=60, tremorsense=120)),
    ("commoner", CombatantSenses()),
]


def _canonical_senses(slug):
    monster = BundledAssetLoader().get_monster(slug)
    assert monster is not None
    return CombatantSenses(
        darkvision=monster.senses.darkvision,
        blindsight=monster.senses.blindsight,
        tremorsense=monster.senses.tremorsense,
        truesight=monster.senses.truesight,
    )


def _hero(**overrides):
    # Extra Attack keeps one test attack on this turn, isolating its RNG from AI.
    fields = dict(
        entity_id=HERO,
        name="Hero",
        initiative=20,
        hp_current=400,
        hp_max=400,
        strength=16,
        dexterity=16,
        ac=1,
        attack_bonus=5,
        zone_id="0,0",
        class_slug="fighter",
        character_level=5,
    )
    return PartyMemberSpec(**(fields | overrides))


def _foe(slug, senses=_OMITTED, **overrides):
    fields = dict(
        entity_id=FOE,
        entity_type="Monster",
        name="Foe",
        initiative=1,
        hp_current=400,
        hp_max=400,
        ac=10,
        zone_id="1,0",
        monster_template_slug=slug,
    )
    if senses is not _OMITTED:
        fields["senses"] = senses
    return EncounterMemberSpec(**(fields | overrides))


def _start(foe, *, hero=None, grid=None, extra_foes=(), effects=()):
    result = run_async(
        orch.start_combat(
            session_id="monster-senses",
            rng_seed=7,
            party=[hero or _hero()],
            encounter=[foe, *extra_foes],
            grid_scene=grid or GridScene(width=30, height=5),
            active_effects=effects,
        )
    )
    return result.handle, orch._get_live(result.handle)


def _condition(live, entity_id, condition, source="implied:test"):
    combatant = orch._find_combatant(live, entity_id)
    combatant.conditions.append(
        ActiveCondition(
            condition=condition,
            source_entity_id=source,
            scope="combat",
        )
    )
    live.active_conditions.setdefault(entity_id, set()).add(condition)


@pytest.fixture
def recorded_rng(monkeypatch):
    instances = []

    class RecordingRandom(_BASE_RANDOM):
        def __init__(self, seed):
            super().__init__(seed)
            self.draws = []
            instances.append(self)

        def randint(self, a, b):
            value = super().randint(a, b)
            self.draws.append((a, b, value))
            return value

    monkeypatch.setattr(orch.random, "Random", RecordingRandom)
    return instances


def _assert_mirror(live):
    mirror = _BASE_RANDOM(7)
    for a, b, value in live.rng.draws:
        assert mirror.randint(a, b) == value
    assert live.rng.getstate() == mirror.getstate()


@pytest.mark.parametrize("slug,expected", TEMPLATES)
@pytest.mark.parametrize("entity_type", ["Monster", "NPC"])
def test_omitted_senses_hydrate_typed_canonical_fields_without_mutation(
    slug,
    expected,
    entity_type,
    recorded_rng,
):
    monster = BundledAssetLoader().get_monster(slug)
    assert monster is not None
    assert monster.senses.passive_perception is not None
    assert _canonical_senses(slug) == expected  # Verified corpus, never inferred from names.
    spec = _foe(slug, entity_type=entity_type)
    before, fields_before, canonical_before = (
        spec.model_dump(),
        set(spec.model_fields_set),
        monster.model_dump(),
    )
    assert "senses" not in spec.model_fields_set
    _handle, live = _start(spec)
    senses = orch._find_combatant(live, FOE).senses
    assert senses == expected
    assert set(senses.model_dump()) == {"darkvision", "blindsight", "tremorsense", "truesight"}
    assert "passive_perception" not in CombatantSenses.model_fields
    assert spec.model_dump() == before
    assert spec.model_fields_set == fields_before
    assert monster.model_dump() == canonical_before
    assert recorded_rng == [live.rng]
    assert live.rng.draws == []
    assert live.rng.getstate() == _BASE_RANDOM(7).getstate()


@pytest.mark.parametrize(
    "senses",
    [
        CombatantSenses(),
        CombatantSenses(darkvision=0),
        CombatantSenses(darkvision=5, blindsight=10, tremorsense=15, truesight=20),
    ],
)
@pytest.mark.parametrize("wire", [False, True])
def test_explicit_senses_including_empty_and_zero_always_override_template(senses, wire):
    spec = _foe("animated-armor", senses)
    if wire:
        spec = EncounterMemberSpec.model_validate(spec.model_dump(exclude_unset=True))
    assert "senses" in spec.model_fields_set
    before = spec.model_dump()
    _handle, live = _start(spec)
    assert orch._find_combatant(live, FOE).senses == senses
    assert spec.model_dump() == before


@pytest.mark.parametrize("slug", [None, "not-a-bundled-monster"])
@pytest.mark.parametrize("senses", [_OMITTED, CombatantSenses(), CombatantSenses(truesight=15)])
def test_no_or_unresolved_template_retains_existing_fallback(slug, senses):
    spec = _foe(slug, senses)
    _handle, live = _start(spec)
    assert orch._find_combatant(live, FOE).senses == (
        CombatantSenses() if senses is _OMITTED else senses
    )


def test_senses_wire_boundary_rejects_passive_perception():
    with pytest.raises(ValidationError, match="passive_perception"):
        _foe("goblin-warrior", {"darkvision": 60, "passive_perception": 9})


@pytest.mark.parametrize(
    "slug,distance,obscurement,expected",
    [
        ("goblin-warrior", 60, False, True),
        ("goblin-warrior", 65, False, False),
        ("goblin-warrior", 5, True, False),
        ("commoner", 5, False, False),
        ("bulette", 75, False, False),  # Tremorsense reaches 120; Darkvision stops at 60.
    ],
)
def test_darkness_and_obscurement_match_equivalent_explicit_senses(
    slug, distance, obscurement, expected
):
    target_cell = f"{distance // 5},0"
    grid = GridScene(
        width=30,
        height=5,
        lighting={target_cell: "dark"},
        obscurement_cells={target_cell: "heavy"} if obscurement else {},
    )
    outcomes = []
    for senses in (_OMITTED, _canonical_senses(slug)):
        _handle, live = _start(
            _foe(slug, senses, zone_id="0,0"), hero=_hero(zone_id=target_cell), grid=grid
        )
        viewer, target = orch._find_combatant(live, FOE), orch._find_combatant(live, HERO)
        assert orch._combatant_can_see(live, viewer, target) is expected
        assert live.topology.can_see("0,0", target_cell, viewer.senses) is expected
        target_unseen, attacker_unseen = orch._target_visibility_maps(live, viewer, [target])
        assert target_unseen == {HERO: not expected}
        assert attacker_unseen == {HERO: False}
        outcomes.append((target_unseen, attacker_unseen, live.rng.getstate()))
    assert outcomes[0] == outcomes[1]


@pytest.mark.parametrize(
    "slug,blind,distance,wall,expected",
    [
        ("animated-armor", False, 60, False, True),
        ("animated-armor", True, 60, False, True),
        ("animated-armor", True, 65, False, False),
        ("animated-armor", False, 5, True, False),
        ("avatar-of-death", False, 60, False, True),
        ("avatar-of-death", False, 65, False, False),
        ("avatar-of-death", True, 5, False, False),
        ("avatar-of-death", False, 5, True, False),
        ("earth-elemental", False, 5, False, False),
        ("earth-elemental", True, 5, False, False),
        ("goblin-warrior", False, 5, False, False),
    ],
)
def test_blindsight_truesight_and_tremorsense_preserve_composite_sight_semantics(
    slug,
    blind,
    distance,
    wall,
    expected,
):
    target_cell = f"{distance // 5},0"
    grid = GridScene(
        width=30, height=5, wall_segments=[WallSegment(x1=1, y1=0, x2=1, y2=5)] if wall else []
    )
    for senses in (_OMITTED, _canonical_senses(slug)):
        _handle, live = _start(
            _foe(slug, senses, zone_id="0,0"), hero=_hero(zone_id=target_cell), grid=grid
        )
        _condition(live, HERO, "invisible")
        if blind:
            _condition(live, FOE, "blinded")
        viewer, target = orch._find_combatant(live, FOE), orch._find_combatant(live, HERO)
        assert orch._combatant_can_see(live, viewer, target) is expected
        assert orch._pierces_invisibility(live, viewer, target) is expected
        if slug == "earth-elemental":
            assert viewer.senses.tremorsense == 60
            assert not orch._special_sense_reaches(live, viewer, target)
            assert not orch._special_sense_reaches_zone(live, viewer, target_cell)
            assert not orch._blindsight_reaches_zone(live, viewer, target_cell)


def test_template_truesight_keeps_existing_heavy_obscurement_limitation():
    _handle, live = _start(
        _foe("avatar-of-death"),
        grid=GridScene(
            width=30,
            height=5,
            obscurement_cells={"0,0": "heavy"},
        ),
    )
    # This branch preserves GridTopology's recorded Truesight/fog limitation.
    assert orch._combatant_can_see(
        live, orch._find_combatant(live, FOE), orch._find_combatant(live, HERO)
    )


def _consumer_run(consumer, senses, *, slug="animated-armor"):
    hero, foe = _hero(), _foe(slug, senses)
    extra_foes = []
    if consumer == "ranged":
        extra_foes = [_foe("commoner", entity_id="mon:far", zone_id="6,0", initiative=0, ac=1)]
    if consumer == "frightened":
        foe.initiative = 30
    handle, live = _start(foe, hero=hero, extra_foes=extra_foes)
    if consumer in ("invisible", "dodge", "frightened"):
        _condition(live, HERO, "invisible")
    if consumer == "dodge":
        orch._set_dodging(live, FOE)
    if consumer == "ranged":
        _condition(live, FOE, "blinded")
    if consumer == "frightened":
        _condition(live, FOE, "frightened", source=HERO)
    mark = len(live.event_log)
    run_async(
        orch.submit_player_intent(
            handle,
            actor_id=FOE if consumer == "frightened" else HERO,
            intent=PlayerIntent(
                intent_type="attack",
                weapon_id="longbow" if consumer == "ranged" else "longsword",
                target_id=HERO
                if consumer == "frightened"
                else "mon:far"
                if consumer == "ranged"
                else FOE,
            ),
        )
    )
    events = live.event_log[mark:]
    rolls = [e for e in events if isinstance(e, AttackRolled)]
    assert len(rolls) == 1
    rolled = rolls[0]
    # One real attack: kept d20(s), then one weapon damage die, no AI draws.
    dice_count = 1 if rolled.advantage == "normal" else 2
    assert [(a, b) for a, b, _ in live.rng.draws] == [(1, 20)] * dice_count + [(1, 8)]
    mirror = _BASE_RANDOM(7)
    dice = [mirror.randint(1, 20) for _ in range(dice_count)]
    kept = max(dice) if rolled.advantage == "advantage" else min(dice)
    assert rolled.natural == kept
    mirror.randint(1, 8)
    assert live.rng.getstate() == mirror.getstate()
    event_types = [e.type for e in events]
    assert event_types.index("attack_rolled") < event_types.index("damage_applied")
    _assert_mirror(live)
    return rolled, {
        "events": [e.model_dump(mode="json") for e in events],
        "rng": live.rng.getstate(),
        "draws": live.rng.draws,
        "hp": live.tracked_hp,
        "zones": live.actor_zone,
    }


@pytest.mark.parametrize(
    "slug,consumer,expected_mode,source",
    [
        ("animated-armor", "invisible", "normal", None),
        ("animated-armor", "dodge", "disadvantage", "dodge"),
        ("animated-armor", "ranged", "disadvantage", "ranged_in_melee"),
        ("animated-armor", "frightened", "disadvantage", "condition:attacker"),
        ("avatar-of-death", "invisible", "normal", None),
        ("avatar-of-death", "dodge", "disadvantage", "dodge"),
    ],
)
def test_template_senses_reach_attack_dodge_frightened_and_ranged_consumers(
    slug,
    consumer,
    expected_mode,
    source,
    recorded_rng,
):
    template_roll, template = _consumer_run(consumer, _OMITTED, slug=slug)
    explicit_roll, explicit = _consumer_run(consumer, _canonical_senses(slug), slug=slug)
    negative_roll, _negative = _consumer_run(consumer, CombatantSenses(), slug=slug)
    assert template == explicit
    assert template_roll == explicit_roll
    assert template_roll.advantage == expected_mode
    if source is not None:
        assert source in template_roll.sources
        assert source not in negative_roll.sources
    if consumer in ("invisible", "dodge"):
        assert "condition:attacker" not in template_roll.sources
        assert "condition:attacker" in negative_roll.sources
        assert negative_roll.advantage == "advantage"
    if consumer == "ranged":
        assert negative_roll.advantage == "normal"
    if consumer == "frightened":
        assert "condition:target" not in template_roll.sources
        assert "condition:target" in negative_roll.sources
    assert len(recorded_rng) == 3


@pytest.mark.parametrize("consumer", ["invisible", "dodge", "ranged", "frightened"])
def test_tremorsense_does_not_supply_sight_to_attack_consumers(consumer, recorded_rng):
    template_roll, template = _consumer_run(consumer, _OMITTED, slug="earth-elemental")
    empty_roll, empty = _consumer_run(consumer, CombatantSenses(), slug="earth-elemental")
    assert template == empty
    assert template_roll == empty_roll
    assert "dodge" not in template_roll.sources
    assert "ranged_in_melee" not in template_roll.sources
    if consumer == "frightened":
        assert "condition:attacker" not in template_roll.sources
        assert "condition:target" in template_roll.sources


@pytest.mark.parametrize(
    "slug,expected",
    [
        ("animated-armor", True),
        ("earth-elemental", False),
        ("commoner", False),
    ],
)
def test_template_senses_flow_into_opportunity_trigger_with_rng_and_event_order(
    slug, expected, recorded_rng
):
    results = []
    for senses in (_OMITTED, _canonical_senses(slug), CombatantSenses()):
        handle, live = _start(_foe(slug, senses, zone_id="0,0"), hero=_hero(zone_id="1,0"))
        _condition(live, HERO, "invisible")
        mark = len(live.event_log)
        run_async(
            orch.submit_player_intent(
                handle, actor_id=HERO, intent=PlayerIntent(intent_type="move", target_zone_id="2,0")
            )
        )
        events = live.event_log[mark:]
        rolls = [e for e in events if isinstance(e, AttackRolled)]
        sees = expected and senses != CombatantSenses()
        assert len(rolls) == int(sees)
        assert orch._find_combatant(live, FOE).reaction_available is (not sees)
        if sees:
            assert rolls[0].attacker_id == FOE
            assert rolls[0].advantage == "normal"
            assert rolls[0].natural == live.rng.draws[0][2]
            assert "condition:target" not in rolls[0].sources
            assert [(a, b) for a, b, _ in live.rng.draws] == [(1, 20), (1, 6)]
            types = [e.type for e in events]
            assert (
                types.index("attack_rolled")
                < types.index("damage_applied")
                < types.index("actor_moved")
            )
        else:
            assert live.rng.draws == []
            assert not any(e.type == "damage_applied" for e in events)
        assert live.actor_zone[HERO] == "2,0"
        _assert_mirror(live)
        results.append(
            ([e.model_dump(mode="json") for e in events], live.rng.getstate(), live.tracked_hp)
        )
    assert results[0] == results[1]


@pytest.mark.parametrize(
    "slug,distance,rejected",
    [
        ("goblin-warrior", 5, True),
        ("goblin-warrior", 65, False),
        ("animated-armor", 5, True),
        ("commoner", 5, False),
        ("bulette", 75, False),
    ],
)
def test_template_senses_flow_into_hide_gate_and_rejection_draws_no_rng(
    slug, distance, rejected, recorded_rng
):
    results = []
    for senses in (_OMITTED, _canonical_senses(slug)):
        handle, live = _start(
            _foe(slug, senses, zone_id=f"{distance // 5},0"),
            hero=_hero(dexterity=18),
            grid=GridScene(width=30, height=5, lighting={"0,0": "dark"}),
        )
        mark = len(live.event_log)
        intent = PlayerIntent(intent_type="hide")
        if rejected:
            with pytest.raises(orch.IntentRejectedError, match="target_invalid"):
                run_async(orch.submit_player_intent(handle, actor_id=HERO, intent=intent))
            assert live.event_log[mark:] == []
            assert live.rng.draws == []
        else:
            run_async(orch.submit_player_intent(handle, actor_id=HERO, intent=intent))
            events = live.event_log[mark:]
            checks = [e for e in events if isinstance(e, CheckRolled)]
            assert len(checks) == 1
            assert checks[0].succeeded is True
            assert checks[0].roll_total == 15
            assert HERO in live.hidden_entities
            assert [e.type for e in events] == [
                "intent_submitted",
                "check_rolled",
                "condition_applied",
            ]
            assert live.rng.draws == [(1, 20, _BASE_RANDOM(7).randint(1, 20))]
        _assert_mirror(live)
        results.append(
            ([e.model_dump(mode="json") for e in live.event_log[mark:]], live.rng.getstate())
        )
    assert results[0] == results[1]


@pytest.mark.parametrize("senses", [_OMITTED, CombatantSenses(), CombatantSenses(truesight=60)])
def test_monster_seeing_invisible_does_not_change_invisible_initiative(senses, recorded_rng):
    effect = ActiveEffect(
        id="effect:invisible",
        name="Invisible",
        origin="test:initiative",
        target_id=HERO,
        statuses={"invisible"},
    )
    _handle, live = _start(
        _foe("avatar-of-death", senses), hero=_hero(initiative=None), effects=[effect]
    )
    mirror = _BASE_RANDOM(7)
    dice = [mirror.randint(1, 20), mirror.randint(1, 20)]
    assert orch._find_combatant(live, HERO).initiative == max(dice) + 3
    assert live.rng.draws == [(1, 20, value) for value in dice]
    assert live.rng.getstate() == mirror.getstate()
    assert recorded_rng == [live.rng]
    assert rules.conditions_advantage_initiative(["invisible"])


def test_same_template_state_intent_seed_replays_typed_events_and_rng(recorded_rng):
    assert _consumer_run("dodge", _OMITTED) == _consumer_run("dodge", _OMITTED)
