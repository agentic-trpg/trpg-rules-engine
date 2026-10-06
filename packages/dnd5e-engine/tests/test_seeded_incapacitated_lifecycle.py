"""Hydrate every seed before reconciling actual incapacitation invariants."""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from itertools import permutations

import pytest

from dnd5e_engine import orchestrator as orch
from dnd5e_engine.activities.conjuration import TRANSFORM_FORM_FLAG
from dnd5e_engine.events import ConditionApplied, EffectApplied
from dnd5e_engine.rules import conditions as rules
from dnd5e_engine.specs import EncounterMemberSpec, GridScene, PartyMemberSpec
from dnd5e_engine.types.effects import ActiveEffect
from tests.e2e.harness import run_async

HERO = "char:hero"
ALLY = "char:ally"
FOE = "mon:foe"
CONDITIONS = ["incapacitated", "paralyzed", "petrified", "stunned", "unconscious"]
TEARDOWNS = {"concentration_dropped", "effect_expired", "condition_removed"}
_BASE_RANDOM = random.Random


def _seeds(statuses):
    return (
        ActiveEffect(
            id="effect:disabled",
            name="Disabled",
            origin="test:seed",
            target_id=HERO,
            statuses=set(statuses),
        ),
        ActiveEffect(
            id="effect:bless",
            name="Bless",
            origin=f"cast:bless:{HERO}",
            target_id=ALLY,
            statuses={"poisoned"},
            flags={"concentration": True},
        ),
        ActiveEffect(
            id="effect:bless",
            name="Bless",
            origin=f"cast:bless:{HERO}",
            target_id=FOE,
            statuses={"blinded"},
            flags={"concentration": True},
        ),
        ActiveEffect(
            id="effect:grapple",
            name="Grappled",
            origin=f"grapple:unarmed-strike:{HERO}",
            target_id=FOE,
            statuses={"grappled"},
        ),
        ActiveEffect(id="effect:rage", name="Rage", origin=f"cast:rage:{HERO}", target_id=HERO),
    )


def _start(seeds, *, immunities=(), level=3, hero_initiative=20):
    result = run_async(
        orch.start_combat(
            session_id="seeded-invariants",
            rng_seed=7,
            grid_scene=GridScene(width=5, height=5),
            party=[
                PartyMemberSpec(
                    entity_id=HERO,
                    name="Hero",
                    initiative=hero_initiative,
                    hp_current=30,
                    hp_max=30,
                    zone_id="0,0",
                    class_slug="barbarian",
                    character_level=level,
                    condition_immunities=list(immunities),
                ),
                PartyMemberSpec(
                    entity_id=ALLY,
                    name="Ally",
                    initiative=15,
                    hp_current=30,
                    hp_max=30,
                    zone_id="0,1",
                ),
            ],
            encounter=[
                EncounterMemberSpec(
                    entity_id=FOE,
                    entity_type="Monster",
                    name="Foe",
                    initiative=1,
                    hp_current=30,
                    hp_max=30,
                    zone_id="1,0",
                )
            ],
            active_effects=seeds,
        )
    )
    return result, orch._get_live(result.handle)


def _snapshot(live):
    return {
        "initiative": [c.model_dump(mode="json") for c in live.initiative],
        "effects": {
            entity: [
                e.model_dump(mode="json") for e in sorted(effects, key=lambda e: (e.id, e.origin))
            ]
            for entity, effects in sorted(live.active_effects.items())
        },
        "conditions": {
            entity: sorted(names) for entity, names in sorted(live.active_conditions.items())
        },
        "lineage": [
            (identity, conditions)
            for identity, conditions in sorted(live.conditions_by_effect.items())
        ],
        "concentration": live.concentration_chain,
        "events": [e.model_dump(mode="json") for e in live.event_log if e.type in TEARDOWNS],
        "rng": live.rng.getstate(),
    }


def _assert_reconciled(live, status):
    assert live.concentration_chain == {}
    assert live.concentration_rounds_remaining == {}
    assert orch._find_combatant(live, HERO).concentration_effect_id is None
    assert {c.condition for c in orch._find_combatant(live, HERO).conditions} == {status}
    assert live.active_conditions[HERO] == {status}
    assert live.active_conditions[ALLY] == set()
    assert live.active_conditions[FOE] == set()
    assert live.active_effects[ALLY] == []
    assert live.active_effects[FOE] == []
    assert [e.id for e in live.active_effects[HERO]] == ["effect:disabled"]
    assert live.conditions_by_effect == {(HERO, "effect:disabled", "test:seed"): [status]}
    assert not any(isinstance(e, (ConditionApplied, EffectApplied)) for e in live.event_log)
    assert live.rng.getstate() == _BASE_RANDOM(7).getstate()


@pytest.mark.parametrize("status", CONDITIONS)
def test_all_seed_permutations_have_identical_reconciled_state_and_events(status):
    seeds = _seeds([status])
    expected = None
    # Every ordering of concentration, grapple, Rage and the condition itself.
    # This includes condition-first and condition-last, and reverses the two
    # concentration targets so event traversal is tested independently too.
    for order in permutations(seeds):
        _result, live = _start(order)
        _assert_reconciled(live, status)
        snapshot = _snapshot(live)
        if expected is None:
            expected = snapshot
            assert [e["type"] for e in snapshot["events"]] == [
                "concentration_dropped",
                "effect_expired",
                "condition_removed",
                "concentration_dropped",
                "effect_expired",
                "condition_removed",
                "condition_removed",
                "effect_expired",
            ]
        else:
            assert snapshot == expected


@pytest.mark.parametrize("status", CONDITIONS)
@pytest.mark.parametrize("reverse", [False, True])
def test_suppressed_status_never_owns_or_triggers_lifecycle(status, reverse):
    seeds = _seeds([status])
    _result, live = _start(tuple(reversed(seeds)) if reverse else seeds, immunities=[status])
    assert orch._find_combatant(live, HERO).conditions == []
    assert live.active_conditions[HERO] == set()
    assert live.conditions_by_effect[(HERO, "effect:disabled", "test:seed")] == []
    assert {e.id for e in live.active_effects[HERO]} == {"effect:disabled", "effect:rage"}
    assert len(live.concentration_chain[HERO]) == 2
    assert live.active_conditions[FOE] == {"grappled", "blinded"}
    assert not any(e.type in TEARDOWNS for e in live.event_log)
    assert live.rng.getstate() == _BASE_RANDOM(7).getstate()


@pytest.mark.parametrize("status", CONDITIONS)
@pytest.mark.parametrize("reverse", [False, True])
def test_persistent_rage_keeps_its_existing_unconscious_exception(status, reverse):
    condition, _conc_a, _conc_b, _grapple, rage = _seeds([status])
    _result, live = _start((rage, condition) if reverse else (condition, rage), level=15)
    remains = status != "unconscious"
    assert any(e.id == "effect:rage" for e in live.active_effects[HERO]) is remains
    assert [e.effect_id for e in live.event_log if e.type == "effect_expired"] == (
        [] if remains else ["effect:rage"]
    )
    assert live.rng.getstate() == _BASE_RANDOM(7).getstate()


def test_seeded_reconciliation_keeps_action_concentration_and_legacy_authorities_separate(
    monkeypatch,
):
    monkeypatch.setitem(
        rules._DECLARATIVE_CONDITION_MIGRATIONS,
        "incapacitated",
        rules._DECLARATIVE_CONDITION_MIGRATIONS["incapacitated"]
        - {
            rules.ConditionEffectKind.BREAKS_CONCENTRATION,
            rules.ConditionEffectKind.CANNOT_TAKE_ACTIONS,
        },
    )
    _result, live = _start(tuple(reversed(_seeds(["paralyzed"]))))
    assert live.concentration_chain
    assert live.active_conditions[FOE] == {"blinded"}
    assert live.active_conditions[ALLY] == {"poisoned"}
    assert not any(e.id == "effect:rage" for e in live.active_effects[HERO])
    assert not any(e.id == "effect:grapple" for e in live.active_effects[FOE])
    assert not rules.conditions_block_actions(["paralyzed"])
    assert rules.conditions_disadvantage_initiative(["paralyzed"])
    assert live.rng.getstate() == _BASE_RANDOM(7).getstate()


def test_wild_shape_form_hydration_remains_an_explicit_follow_up():
    condition = _seeds(["incapacitated"])[0]
    raw_form = ActiveEffect(
        id="effect:wild-shape",
        name="Wild Shape",
        target_id=HERO,
        origin=f"cast:wild-shape:{HERO}",
        flags={TRANSFORM_FORM_FLAG: "wolf"},
    )
    _result, live = _start([raw_form, condition])
    # start_combat has no actual transform-state input. A raw carrier is not a
    # hydrated form; this batch must not create or pretend to tear down one.
    assert live.transforms == {}
    assert raw_form in live.active_effects[HERO]
    assert not any(e.type in TEARDOWNS for e in live.event_log)


def test_pre_seat_immunity_filtering_agrees_with_attachment():
    seeds = _seeds(["paralyzed"])
    _result, live = _start(seeds, immunities=["paralyzed"], hero_initiative=None)
    expected_rng = _BASE_RANDOM(7)
    # Rejected Paralyzed no longer imposes Incapacitated's disadvantage pre-seat.
    expected = expected_rng.randint(1, 20)
    assert orch._find_combatant(live, HERO).initiative == expected
    assert live.rng.getstate() == expected_rng.getstate()
    assert orch._find_combatant(live, HERO).conditions == []
    assert live.concentration_chain
    assert not any(e.type in TEARDOWNS for e in live.event_log)


def test_reconciliation_never_draws_rng_and_is_idempotent(monkeypatch):
    def forbid_rng(*args):
        pytest.fail("Seeded reconciliation must never draw RNG")

    monkeypatch.setattr(orch.random.Random, "randint", forbid_rng)
    monkeypatch.setattr(orch.random.Random, "random", forbid_rng)
    monkeypatch.setattr(orch.random.Random, "getrandbits", forbid_rng)
    _result, live = _start(_seeds(["petrified"]))
    before = _snapshot(live)
    orch._reconcile_seeded_condition_lifecycle(live)
    assert _snapshot(live) == before


def _hash_snapshot():
    seeds = _seeds(
        ["petrified", "poisoned", "incapacitated", "stunned", "unconscious", "paralyzed"]
    )
    _result, live = _start(tuple(reversed(seeds)))
    assert not any(isinstance(e, (ConditionApplied, EffectApplied)) for e in live.event_log)
    assert live.rng.getstate() == _BASE_RANDOM(7).getstate()
    return _snapshot(live)


def test_seeded_teardown_is_stable_across_python_hash_seeds():
    script = "import json; from tests.test_seeded_incapacitated_lifecycle import _hash_snapshot; print(json.dumps(_hash_snapshot(), sort_keys=True))"
    runs = [
        json.loads(
            subprocess.run(
                [sys.executable, "-c", script],
                env={**os.environ, "PYTHONHASHSEED": seed},
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )
        for seed in ("0", "1", "2", "3", "4", "5")
    ]
    assert all(run == runs[0] for run in runs)
