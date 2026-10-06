"""Runtime source identity, check LOS, atomic movement and concealment consumers."""

from __future__ import annotations

import random

import pytest
from dnd5e_srd_data.schema.common import CheckActivity
from dnd5e_srd_data.schema.condition import ConditionEffectKind

from dnd5e_engine import PlayerIntent
from dnd5e_engine.activities.context import ActivityResolutionContext
from dnd5e_engine.activities.d20 import AdvantageSources, roll_d20_test
from dnd5e_engine.activities.passive_stats import CombatantSenses
from dnd5e_engine.activities.resolver import resolve_activity
from dnd5e_engine.events import (
    ActorMoved,
    AttackFailed,
    AttackRolled,
    CastFailed,
    CheckRolled,
    MoveFailed,
)
from dnd5e_engine.orchestrator import (
    _build_hydration_payload,
    _charmed_target_violation,
    _combatant_can_see,
    _condition_names,
    _condition_source_entity,
    _fear_source_in_sight,
    _find_combatant,
    _frightened_approach_blocked,
    _get_live,
    _opportunity_attackers,
    _select_monster_targets,
    start_combat,
    submit_player_intent,
)
from dnd5e_engine.rules import conditions as rules
from dnd5e_engine.specs import EncounterMemberSpec, GridScene, PartyMemberSpec, WallSegment
from dnd5e_engine.types.conditions import ActiveCondition
from dnd5e_engine.types.effects import ActiveEffect
from tests.e2e.harness import cell, events_of, run_async

HERO = "char:0123456789ab"
SOURCE = "mon:abcdefabcdef"
OTHER = "char:000000000001"
K = ConditionEffectKind


class RecordingRandom(random.Random):
    def __init__(self, seed=7):
        super().__init__(seed)
        self.draws = []

    def randint(self, a, b):
        value = super().randint(a, b)
        self.draws.append((a, b, value))
        return value


def _start(*, source_cell=None, grid=None, effects=(), ally=False, near_enemy=False):
    async def run():
        party = [
            PartyMemberSpec(
                entity_id=HERO,
                name="Hero",
                initiative=20,
                hp_current=30,
                hp_max=30,
                zone_id=cell(0, 0),
                spells_known=["fire-bolt", "guidance"],
                dexterity=16,
            )
        ]
        if ally:
            party.append(
                PartyMemberSpec(
                    entity_id=OTHER,
                    name="Ally",
                    initiative=10,
                    hp_current=40,
                    hp_max=40,
                    zone_id=cell(0, 1),
                )
            )
        foes = [
            EncounterMemberSpec(
                entity_id=SOURCE,
                entity_type="Monster",
                name="Source",
                initiative=1,
                hp_current=50,
                hp_max=50,
                ac=10,
                zone_id=source_cell or cell(1, 0),
            )
        ]
        if near_enemy:
            foes.append(
                EncounterMemberSpec(
                    entity_id="mon:000000000002",
                    entity_type="Monster",
                    name="Reactor",
                    initiative=2,
                    hp_current=50,
                    hp_max=50,
                    zone_id=cell(0, 1),
                )
            )
        result = await start_combat(
            session_id="source-relative",
            party=party,
            encounter=foes,
            grid_scene=grid or GridScene(width=10, height=10),
            rng_seed=7,
            active_effects=list(effects),
        )
        live = _get_live(result.handle)
        live.rng = RecordingRandom()
        return result.handle, live, _find_combatant(live, HERO), _find_combatant(live, SOURCE)

    return run_async(run())


def _give(live, combatant, condition, source="implied:test"):
    combatant.conditions.append(
        ActiveCondition(
            condition=condition,
            source_entity_id=source,
            scope="combat",
        )
    )
    live.active_conditions.setdefault(combatant.entity_id, set()).add(condition)


def _remove_clause(monkeypatch, slug, kind, remove_from):
    if remove_from == "allowlist":
        monkeypatch.setitem(
            rules._DECLARATIVE_CONDITION_MIGRATIONS,
            slug,
            rules._DECLARATIVE_CONDITION_MIGRATIONS[slug] - {kind},
        )
    else:
        monkeypatch.setitem(
            rules._DECLARATIVE_CONDITION_EFFECTS,
            slug,
            tuple(c for c in rules._DECLARATIVE_CONDITION_EFFECTS[slug] if c.kind != kind),
        )


@pytest.mark.parametrize("obstacle", ["wall", "darkness", "invisibility"])
def test_unseen_fear_source_gates_checks_but_not_atomic_movement(obstacle, monkeypatch):
    fields = {}
    if obstacle == "wall":
        fields["wall_segments"] = [WallSegment(x1=3, y1=0, x2=3, y2=2)]
    elif obstacle == "darkness":
        fields["lighting"] = {cell(5, 0): "dark"}
    handle, live, hero, source = _start(
        source_cell=cell(5, 0),
        grid=GridScene(width=10, height=10, **fields),
        near_enemy=True,
    )
    _give(live, hero, "frightened", SOURCE)
    if obstacle == "invisibility":
        _give(live, source, "invisible")
    assert _combatant_can_see(live, hero, source) is False
    assert _fear_source_in_sight(live, hero) is False
    entry = _build_hydration_payload(live)["check_modifiers"][HERO]
    assert entry["passive_check_dis"] == []
    assert entry["disadvantage"] is False
    assert rules.conditions_grant_advantage_on_attack(
        ["frightened"], [], fear_source_in_sight=_fear_source_in_sight(live, hero)
    ) == (False, False)

    def forbid_opportunity(*args, **kwargs):
        pytest.fail("movement rejection must precede every opportunity attack")

    from dnd5e_engine import orchestrator as orch

    monkeypatch.setattr(orch, "_fire_opportunity_attacks_on_step", forbid_opportunity)
    budget = hero.movement_remaining
    state = live.rng.getstate()
    offset = len(live.event_log)
    run_async(
        submit_player_intent(
            handle,
            actor_id=HERO,
            intent=PlayerIntent(intent_type="move", target_zone_id=cell(2, 0)),
        )
    )
    assert events_of(live, MoveFailed)[-1].reason == "frightened"
    assert not events_of(live, ActorMoved)
    assert not events_of(live, AttackRolled)
    assert live.actor_zone[HERO] == cell(0, 0)
    assert _find_combatant(live, HERO).movement_remaining == budget
    assert live.rng.draws == []
    assert live.rng.getstate() == state
    assert [e.type for e in live.event_log[offset:]] == ["move_failed"]


@pytest.mark.parametrize("context", ["unknown", "missing", "dead", "untracked", "distance"])
def test_unresolvable_approach_stays_inert(context, monkeypatch):
    _handle, live, hero, source = _start(source_cell=cell(5, 0))
    source_id = "implied:test" if context == "unknown" else SOURCE
    if context == "missing":
        source_id = "mon:000000000099"
    _give(live, hero, "frightened", source_id)
    if context == "dead":
        source.is_alive = False
    elif context == "untracked":
        del live.actor_zone[SOURCE]
    elif context == "distance":
        monkeypatch.setattr(type(live.topology), "distance_ft", lambda *args: None)
    assert _frightened_approach_blocked(live, hero, [cell(0, 0), cell(1, 0)]) is False
    assert live.rng.draws == []


@pytest.mark.parametrize("remove_from", ["allowlist", "canonical"])
def test_frightened_movement_removal_keeps_check_and_attack_clauses(remove_from, monkeypatch):
    handle, live, hero, _source = _start(source_cell=cell(5, 0))
    _give(live, hero, "frightened", SOURCE)
    assert _frightened_approach_blocked(live, hero, [cell(0, 0), cell(1, 0)])
    _remove_clause(monkeypatch, "frightened", K.CANT_MOVE_TOWARD_FEAR_SOURCE, remove_from)
    assert rules.conditions_grant_advantage_on_attack(["frightened"], []) == (False, True)
    assert _build_hydration_payload(live)["check_modifiers"][HERO]["disadvantage"] is True
    run_async(
        submit_player_intent(
            handle,
            actor_id=HERO,
            intent=PlayerIntent(intent_type="move", target_zone_id=cell(1, 0)),
        )
    )
    assert not events_of(live, MoveFailed)
    assert live.actor_zone[HERO] == cell(1, 0)
    assert _find_combatant(live, HERO).movement_remaining == hero.movement_remaining - 5
    assert live.rng.draws == []


@pytest.mark.parametrize(
    "conditions,visible,expected_mode,count",
    [
        (["frightened"], True, "disadvantage", 2),
        (["frightened"], False, "normal", 1),
        (["poisoned"], False, "disadvantage", 2),
        (["poisoned", "frightened"], False, "disadvantage", 2),
    ],
)
def test_live_check_context_and_seeded_draw_order(conditions, visible, expected_mode, count):
    grid = GridScene(width=10, height=10, lighting={} if visible else {cell(1, 0): "dark"})
    _handle, live, hero, _source = _start(grid=grid)
    for condition in conditions:
        _give(live, hero, condition, SOURCE)
    payload = _build_hydration_payload(live)
    events = []
    ctx = ActivityResolutionContext(
        rng=live.rng,
        caster=hero,
        targets=[],
        event_emitter=events.append,
        caster_abilities={},
        check_modifiers=payload["check_modifiers"],
    )
    resolve_activity(CheckActivity(check={"ability": "dex"}), ctx)
    [rolled] = [e for e in events if isinstance(e, CheckRolled)]
    mirror = RecordingRandom()
    faces = [mirror.randint(1, 20) for _ in range(count)]
    assert rolled.advantage == expected_mode
    assert rolled.natural == (min(faces) if count == 2 else faces[0])
    assert live.rng.draws == mirror.draws
    assert live.rng.getstate() == mirror.getstate()


@pytest.mark.parametrize("visible", [True, False])
@pytest.mark.parametrize("advantage", [True, False])
def test_check_cancellation_preserves_d20_draw_discipline(visible, advantage):
    disadvantage = rules.conditions_grant_disadvantage_on_ability_checks(
        ["frightened"], fear_source_in_sight=visible
    )
    rng, mirror = RecordingRandom(), RecordingRandom()
    result = roll_d20_test(
        rng,
        3,
        AdvantageSources(
            advantage=("effect",) if advantage else (),
            disadvantage=("condition:attacker",) if disadvantage else (),
        ),
    )
    mode = "normal" if advantage == disadvantage else "advantage" if advantage else "disadvantage"
    faces = [mirror.randint(1, 20) for _ in range(1 if mode == "normal" else 2)]
    assert result.mode == mode
    assert result.kept == (max(faces) if mode == "advantage" else min(faces))
    assert rng.draws == mirror.draws
    assert rng.getstate() == mirror.getstate()


@pytest.mark.parametrize("intent_type", ["attack", "cast_spell"])
def test_charmed_rejection_retains_events_economy_and_zero_draws(intent_type):
    handle, live, hero, _source = _start()
    _give(live, hero, "charmed", SOURCE)
    before = hero.model_dump()
    state, offset = live.rng.getstate(), len(live.event_log)
    intent = PlayerIntent(
        intent_type=intent_type,
        target_id=SOURCE,
        weapon_id="dagger" if intent_type == "attack" else None,
        spell_id="fire-bolt" if intent_type == "cast_spell" else None,
    )
    run_async(submit_player_intent(handle, actor_id=HERO, intent=intent))
    failure_type = AttackFailed if intent_type == "attack" else CastFailed
    assert events_of(live, failure_type)[-1].reason == "target_is_charmer"
    assert [e.type for e in live.event_log[offset:]] == [
        "attack_failed" if intent_type == "attack" else "cast_failed",
    ]
    assert _find_combatant(live, HERO).model_dump() == before
    assert live.rng.draws == []
    assert live.rng.getstate() == state


@pytest.mark.parametrize("remove_from", ["allowlist", "canonical"])
@pytest.mark.parametrize("intent_type", ["attack", "cast_spell"])
def test_charmed_clause_removal_disables_player_monster_and_opportunity_gates(
    remove_from, intent_type, monkeypatch
):
    # Effect origin resolves the source; the migrated mechanic owns no identity lookup.
    effect = ActiveEffect(
        id="charm",
        name="Charm",
        origin=f"cast:charm-person:{SOURCE}",
        target_id=HERO,
        statuses={"charmed"},
    )
    handle, live, hero, source = _start(effects=[effect], ally=True)
    _give(live, source, "charmed", HERO)
    intent = PlayerIntent(
        intent_type=intent_type,
        target_id=SOURCE,
        weapon_id="dagger",
        spell_id="fire-bolt",
    )
    assert _condition_source_entity(live, hero, "charmed") == SOURCE
    assert _charmed_target_violation(live, hero, intent) == SOURCE
    assert [c.entity_id for c in _select_monster_targets(live, source)] == [OTHER]
    assert _opportunity_attackers(
        live,
        mover_id=SOURCE,
        from_cell=cell(1, 0),
        to_cell=cell(2, 0),
    ) == [OTHER]
    _remove_clause(monkeypatch, "charmed", K.CANT_ATTACK_CHARMER, remove_from)
    assert _condition_source_entity(live, hero, "charmed") == SOURCE
    assert _charmed_target_violation(live, hero, intent) is None
    assert [c.entity_id for c in _select_monster_targets(live, source)] == [HERO, OTHER]
    assert _opportunity_attackers(
        live,
        mover_id=SOURCE,
        from_cell=cell(1, 0),
        to_cell=cell(2, 0),
    ) == [HERO, OTHER]
    run_async(submit_player_intent(handle, actor_id=HERO, intent=intent))
    assert not events_of(live, AttackFailed)
    assert not events_of(live, CastFailed)
    assert events_of(live, AttackRolled)
    assert live.rng.draws


def test_charmed_unknown_source_and_existing_non_harmful_intents_stay_allowed():
    _handle, live, hero, _source = _start()
    _give(live, hero, "charmed")
    assert (
        _charmed_target_violation(live, hero, PlayerIntent(intent_type="attack", target_id=SOURCE))
        is None
    )
    hero.conditions.clear()
    _give(live, hero, "charmed", SOURCE)
    for fields in [
        {"intent_type": "cast_spell", "spell_id": "guidance"},
        {"intent_type": "grapple"},
        {"intent_type": "shove"},
    ]:
        assert (
            _charmed_target_violation(live, hero, PlayerIntent(target_id=SOURCE, **fields)) is None
        )
    assert live.rng.draws == []


@pytest.mark.parametrize("remove_from", ["allowlist", "canonical"])
def test_unseen_removal_keeps_attack_clauses_and_attack_removal_keeps_unseen(
    remove_from, monkeypatch
):
    _handle, live, hero, source = _start()
    _give(live, source, "invisible")
    assert _combatant_can_see(live, hero, source) is False
    attacks = rules.conditions_grant_advantage_on_attack(["invisible"], ["invisible"])
    with monkeypatch.context() as m:
        _remove_clause(m, "invisible", K.UNSEEN, remove_from)
        assert _combatant_can_see(live, hero, source) is True
        assert rules.conditions_grant_advantage_on_attack(["invisible"], ["invisible"]) == attacks
    for kind in [K.ADVANTAGE_OWN_ATTACKS, K.DISADVANTAGE_ATTACKS_AGAINST]:
        _remove_clause(monkeypatch, "invisible", kind, remove_from)
    assert rules.conditions_grant_advantage_on_attack(["invisible"], ["invisible"]) == (
        False,
        False,
    )
    assert _combatant_can_see(live, hero, source) is False
    assert live.rng.draws == []


def _legacy_can_see(live, viewer, target):
    """Frozen pre-migration visibility algorithm for same-input parity checks."""
    from dnd5e_engine.orchestrator import _blindsight_reaches_zone, _special_sense_reaches
    from dnd5e_engine.rules.conditions import Condition, is_condition_active

    viewer_zone, target_zone = (
        live.actor_zone.get(viewer.entity_id),
        live.actor_zone.get(target.entity_id),
    )
    if viewer_zone is None or target_zone is None:
        return True
    if is_condition_active(
        Condition.BLINDED, _condition_names(viewer)
    ) and not _blindsight_reaches_zone(live, viewer, target_zone):
        return False
    if is_condition_active(
        Condition.INVISIBLE, _condition_names(target)
    ) and not _special_sense_reaches(live, viewer, target):
        return False
    return live.topology.can_see(viewer_zone, target_zone, viewer.senses)


@pytest.mark.parametrize(
    "senses", [{}, {"darkvision": 60}, {"blindsight": 5}, {"blindsight": 30}, {"truesight": 30}]
)
@pytest.mark.parametrize("blinded", [True, False])
@pytest.mark.parametrize("scene", ["lit", "dark", "wall", "untracked"])
def test_visibility_algorithm_matches_legacy_and_has_no_rng(senses, blinded, scene):
    fields = {}
    if scene == "dark":
        fields["lighting"] = {cell(5, 0): "dark"}
    elif scene == "wall":
        fields["wall_segments"] = [WallSegment(x1=3, y1=0, x2=3, y2=2)]
    _handle, live, hero, source = _start(
        source_cell=cell(5, 0), grid=GridScene(width=10, height=10, **fields)
    )
    hero.senses = CombatantSenses(**senses)
    _give(live, source, "invisible")
    if blinded:
        _give(live, hero, "blinded")
    if scene == "untracked":
        del live.actor_zone[SOURCE]
    assert _combatant_can_see(live, hero, source) == _legacy_can_see(live, hero, source)
    assert live.rng.draws == []


@pytest.mark.parametrize("scenario", ["dodge", "opportunity", "hide", "fear"])
def test_visibility_consumers_retain_events_and_rng(scenario, monkeypatch):
    from dnd5e_engine import orchestrator as orch

    def run():
        grid = (
            GridScene(width=10, height=10, obscurement_cells={cell(0, 0): "heavy"})
            if scenario == "hide"
            else None
        )
        handle, live, hero, source = _start(grid=grid)
        _give(live, hero, "invisible")
        if scenario == "dodge":
            source.dodging = True
        if scenario == "fear":
            _give(live, source, "frightened", HERO)
            assert (
                _build_hydration_payload(live)["check_modifiers"][SOURCE]["disadvantage"] is False
            )
        if scenario == "opportunity":
            orch._fire_opportunity_attacks_on_step(
                live,
                mover_id=HERO,
                from_cell=cell(0, 0),
                to_cell=cell(0, 2),
            )
        else:
            intent = (
                PlayerIntent(intent_type="hide")
                if scenario == "hide"
                else PlayerIntent(intent_type="attack", weapon_id="dagger", target_id=SOURCE)
            )
            run_async(submit_player_intent(handle, actor_id=HERO, intent=intent))
        return (
            live.event_log,
            [c.model_dump() for c in live.initiative],
            live.rng.draws,
            live.rng.getstate(),
        )

    projected = run()
    monkeypatch.setattr(orch, "_combatant_can_see", _legacy_can_see)
    assert run() == projected
