"""Expiry owns actual attachments, never suppressed raw effect statuses."""

from __future__ import annotations

import asyncio
import json
import os
import random
import subprocess
import sys
from itertools import permutations

import pytest
from dnd5e_srd_data.schema.common import PassiveEffect, UtilityActivity
from dnd5e_srd_data.schema.condition import ConditionEffectKind

from dnd5e_engine.activities.context import ActivityResolutionContext
from dnd5e_engine.activities.effects import apply_activity_effects
from dnd5e_engine.events import ConditionApplied, ConditionRemoved, EffectApplied, EffectExpired
from dnd5e_engine.orchestrator import (
    _drop_concentration,
    _emit,
    _find_combatant,
    _LiveCombat,
    _record_effect_lifecycle_links,
    _seed_active_effects,
)
from dnd5e_engine.rules import conditions as rules
from dnd5e_engine.types.combat import Combatant
from dnd5e_engine.types.effects import ActiveEffect, ActiveEffectChange
from tests.e2e.harness import grid_scene

TARGET = "mon:target"
CASTER = "char:caster"


class _NoDrawRandom(random.Random):
    def random(self):
        raise AssertionError("Condition attachment/expiry must not draw RNG")

    def getrandbits(self, k):
        raise AssertionError("Condition attachment/expiry must not draw RNG")


def _live(immunities=()):
    return _LiveCombat(
        handle_id="lineage",
        session_id="lineage",
        initiative=[
            Combatant(
                entity_id=TARGET,
                entity_type="Monster",
                name="Target",
                initiative=1,
                hp_current=30,
                hp_max=30,
                condition_immunities=list(immunities),
            ),
            Combatant(
                entity_id=CASTER,
                entity_type="Character",
                name="Caster",
                initiative=20,
                hp_current=30,
                hp_max=30,
            ),
        ],
        party_ids={CASTER},
        encounter_ids={TARGET},
        topology=grid_scene(),
        rng=_NoDrawRandom(7),
        event_queue=asyncio.Queue(),
        scene_location_id="loc:lineage",
    )


@pytest.fixture
def live():
    state = _live()
    before = state.rng.getstate()
    yield state
    assert state.rng.getstate() == before


def _effect(name, statuses=("poisoned",), *, origin="test:lineage", concentration=False):
    return ActiveEffect(
        id=f"effect:{name}",
        name=name,
        target_id=TARGET,
        origin=origin,
        statuses=set(statuses),
        changes=[ActiveEffectChange(key="test.rider", mode="add", value=2)],
        flags={"concentration": concentration},
    )


def _key(effect):
    return (effect.target_id, effect.id, effect.origin)


def _attach(live, effect, path):
    if path == "runtime":
        _emit(live, EffectApplied(effect=effect))
    else:
        _seed_active_effects(live, [effect])


def _expire(live, effect):
    _emit(
        live,
        EffectExpired(
            target_id=effect.target_id,
            effect_id=effect.id,
            origin=effect.origin,
            reason="duration",
        ),
    )


def _assert_conditions(live, expected):
    assert {c.condition for c in _find_combatant(live, TARGET).conditions} == set(expected)
    assert live.active_conditions.get(TARGET, set()) == set(expected)


@pytest.mark.parametrize("path", ["runtime", "seed"])
@pytest.mark.parametrize("source", ["direct", "effect"])
@pytest.mark.parametrize("static", [False, True])
def test_suppressed_effect_expiry_keeps_earlier_poisoned(live, source, path, static):
    original = _effect("original")
    if source == "direct":
        _emit(live, ConditionApplied(target_id=TARGET, condition="poisoned"))
    else:
        _attach(live, original, path)
    if static:
        target = _find_combatant(live, TARGET)
        live.initiative[0] = target.model_copy(update={"condition_immunities": ["poisoned"]})
    else:
        _attach(live, _effect("stone", ["petrified"]), path)
    suppressed = _effect("suppressed")
    original_event = EffectApplied(effect=suppressed).model_dump()
    mark = len(live.event_log)
    _attach(live, suppressed, path)
    _emit(live, ConditionApplied(target_id=TARGET, condition="poisoned"))
    _record_effect_lifecycle_links(live, _find_combatant(live, CASTER), mark)
    assert live.conditions_by_effect.get(_key(suppressed), []) == []
    assert suppressed in live.active_effects[TARGET]
    assert suppressed.statuses == {"poisoned"}
    if path == "runtime":
        assert [e.model_dump() for e in live.event_log[mark:]] == [original_event]
    _expire(live, suppressed)
    _assert_conditions(live, {"poisoned"} if static else {"poisoned", "petrified"})
    poisoned = [c for c in _find_combatant(live, TARGET).conditions if c.condition == "poisoned"]
    assert [c.source_effect_id for c in poisoned] == [None if source == "direct" else original.id]
    assert _key(suppressed) not in live.conditions_by_effect


@pytest.mark.parametrize("path", ["runtime", "seed"])
@pytest.mark.parametrize("static", [False, True])
def test_suppressed_source_cannot_keep_original_effect_alive(live, path, static):
    original = _effect("original")
    _attach(live, original, path)
    if static:
        target = _find_combatant(live, TARGET)
        live.initiative[0] = target.model_copy(update={"condition_immunities": ["poisoned"]})
    else:
        _attach(live, _effect("stone", ["petrified"]), path)
    suppressed = _effect("suppressed")
    _attach(live, suppressed, path)
    assert live.conditions_by_effect.get(_key(suppressed), []) == []
    _expire(live, original)
    _assert_conditions(live, set() if static else {"petrified"})
    assert suppressed in live.active_effects[TARGET]
    _expire(live, suppressed)
    _assert_conditions(live, set() if static else {"petrified"})


@pytest.mark.parametrize("path", ["runtime", "seed"])
def test_static_immunity_suppresses_lineage_and_retains_raw_riders(live, path):
    target = _find_combatant(live, TARGET)
    live.initiative[0] = target.model_copy(update={"condition_immunities": ["poisoned"]})
    effect = _effect("immune")
    _attach(live, effect, path)
    _assert_conditions(live, set())
    assert live.conditions_by_effect.get(_key(effect), []) == []
    assert live.active_effects[TARGET] == [effect]
    _expire(live, effect)
    _assert_conditions(live, set())


@pytest.mark.parametrize("path", ["runtime", "seed"])
def test_ordinary_effect_apply_expire_cleans_both_stores(live, path):
    effect = _effect("ordinary", ["poisoned", "blinded"])
    mark = len(live.event_log)
    _attach(live, effect, path)
    for status in sorted(effect.statuses):
        _emit(live, ConditionApplied(target_id=TARGET, condition=status))
    _record_effect_lifecycle_links(live, _find_combatant(live, CASTER), mark)
    _assert_conditions(live, {"poisoned", "blinded"})
    assert live.conditions_by_effect[_key(effect)] == ["blinded", "poisoned"]
    _expire(live, effect)
    _assert_conditions(live, set())
    assert _key(effect) not in live.conditions_by_effect
    assert live.active_effects[TARGET] == []


@pytest.mark.parametrize("path", ["runtime", "seed"])
@pytest.mark.parametrize("same_id", [False, True])
@pytest.mark.parametrize("first_to_expire", [0, 1])
def test_stacked_effects_retain_other_source_until_it_expires(live, path, same_id, first_to_expire):
    effects = [
        _effect("a", origin="test:a"),
        _effect("a" if same_id else "b", origin="test:b"),
    ]
    for effect in effects:
        _attach(live, effect, path)
        assert live.conditions_by_effect[_key(effect)] == ["poisoned"]
    _expire(live, effects[first_to_expire])
    _assert_conditions(live, {"poisoned"})
    assert {c.source_effect_id for c in _find_combatant(live, TARGET).conditions} == {
        effects[1 - first_to_expire].id
    }
    _expire(live, effects[1 - first_to_expire])
    _assert_conditions(live, set())


@pytest.mark.parametrize("path", ["runtime", "seed"])
def test_allowed_effect_does_not_take_ownership_of_a_direct_condition(live, path):
    _emit(live, ConditionApplied(target_id=TARGET, condition="poisoned"))
    effect = _effect("also_poisoned")
    _attach(live, effect, path)
    _expire(live, effect)
    _assert_conditions(live, {"poisoned"})
    assert [c.source_effect_id for c in _find_combatant(live, TARGET).conditions] == [None]


@pytest.mark.parametrize("path", ["runtime", "seed"])
def test_repeated_identity_retains_its_source_until_last_instance_expires(live, path):
    effect = _effect("repeated")
    _attach(live, effect, path)
    _attach(live, effect, path)
    _expire(live, effect)
    _assert_conditions(live, {"poisoned"})
    assert live.conditions_by_effect[_key(effect)] == ["poisoned"]
    _expire(live, effect)
    _assert_conditions(live, set())
    assert _key(effect) not in live.conditions_by_effect


def test_concentration_drop_preserves_direct_condition_and_other_effect(live):
    _emit(live, ConditionApplied(target_id=TARGET, condition="poisoned"))
    original = _effect("original", ["blinded"])
    _emit(live, EffectApplied(effect=original))
    dropped = _effect("concentrated", ["poisoned", "blinded"], concentration=True)
    mark = len(live.event_log)
    _emit(live, EffectApplied(effect=dropped))
    for status in dropped.statuses:
        _emit(live, ConditionApplied(target_id=TARGET, condition=status))
    _record_effect_lifecycle_links(live, _find_combatant(live, CASTER), mark)
    _drop_concentration(live, CASTER)
    _assert_conditions(live, {"poisoned", "blinded"})
    assert not any(isinstance(e, ConditionRemoved) for e in live.event_log)
    _expire(live, original)
    _assert_conditions(live, {"poisoned"})


def test_event_pairing_cannot_attribute_an_unrelated_direct_condition(live):
    effect = _effect("blinded_only", ["blinded"])
    mark = len(live.event_log)
    _emit(live, EffectApplied(effect=effect))
    _emit(live, ConditionApplied(target_id=TARGET, condition="poisoned"))
    _record_effect_lifecycle_links(live, _find_combatant(live, CASTER), mark)
    assert live.conditions_by_effect[_key(effect)] == ["blinded"]
    _expire(live, effect)
    _assert_conditions(live, {"poisoned"})


@pytest.mark.parametrize("path", ["runtime", "seed"])
def test_multi_status_effect_preserves_poisoned_that_predates_it(live, path):
    _emit(live, ConditionApplied(target_id=TARGET, condition="poisoned"))
    effect = _effect("both", ["petrified", "poisoned"])
    _attach(live, effect, path)
    _assert_conditions(live, {"petrified", "poisoned"})
    assert live.conditions_by_effect[_key(effect)] == ["petrified"]
    _expire(live, effect)
    _assert_conditions(live, {"poisoned"})


class _OrderedStatuses(set):
    """Exercise both possible raw set traversals regardless of hash seed."""

    def __init__(self, values):
        super().__init__(values)
        self.order = tuple(values)

    def __iter__(self):
        return iter(self.order)


@pytest.mark.parametrize("path", ["runtime", "seed"])
@pytest.mark.parametrize("order", list(permutations(["petrified", "poisoned"])))
@pytest.mark.parametrize("repeat", range(3))
def test_multi_status_attachment_is_deterministic(live, path, order, repeat):
    effect = _effect("both").model_copy(update={"statuses": _OrderedStatuses(order)})
    mark = len(live.event_log)
    _attach(live, effect, path)
    for status in effect.statuses:
        _emit(live, ConditionApplied(target_id=TARGET, condition=status))
    _record_effect_lifecycle_links(live, _find_combatant(live, CASTER), mark)
    _assert_conditions(live, {"petrified"})
    assert live.conditions_by_effect[_key(effect)] == ["petrified"]
    assert effect.statuses == {"petrified", "poisoned"}
    assert not any(
        isinstance(e, ConditionApplied) and e.condition == "poisoned" for e in live.event_log[mark:]
    )
    _expire(live, effect)
    _assert_conditions(live, set())


@pytest.mark.parametrize(
    "immunities, expected", [([], {"petrified"}), (["petrified"], {"poisoned"})]
)
@pytest.mark.parametrize("fold", [False, True])
def test_activity_emission_uses_the_same_status_policy(immunities, expected, fold):
    live = _live(immunities)
    before = live.rng.getstate()
    events = []

    def emit(event):
        events.append(event)
        if fold:
            _emit(live, event)

    passive = PassiveEffect(id="both", name="Both", statuses=["poisoned", "petrified"])
    ctx = ActivityResolutionContext(
        caster=_find_combatant(live, CASTER),
        caster_abilities={},
        targets=[_find_combatant(live, TARGET)],
        rng=live.rng,
        event_emitter=emit,
        source_passive_effects=[passive],
    )
    activity = UtilityActivity.model_validate({"id": "both", "effects": [{"id": "both"}]})
    apply_activity_effects(
        activity, ctx, _find_combatant(live, TARGET), save_succeeded=None, cast_level=0
    )
    assert isinstance(events[0], EffectApplied)
    assert events[0].effect.statuses == {"petrified", "poisoned"}
    assert {e.condition for e in events[1:]} == expected
    if fold:
        _assert_conditions(live, expected)
        assert set(live.conditions_by_effect[_key(events[0].effect)]) == expected
    assert live.rng.getstate() == before


def test_immunity_priority_follows_machine_clauses_instead_of_slug_order(live, monkeypatch):
    monkeypatch.setitem(
        rules._DECLARATIVE_CONDITION_EFFECTS,
        "petrified",
        tuple(
            clause.model_copy(update={"condition_slugs": ["blinded"]})
            if clause.kind == ConditionEffectKind.IMMUNE_TO_CONDITION
            else clause
            for clause in rules._DECLARATIVE_CONDITION_EFFECTS["petrified"]
        ),
    )
    effect = _effect("machine_scope", ["blinded", "petrified"])
    _emit(live, EffectApplied(effect=effect))
    _assert_conditions(live, {"petrified"})
    assert live.conditions_by_effect[_key(effect)] == ["petrified"]


def _hash_seed_snapshot():
    snapshots = []
    for path in ("runtime", "seed"):
        live = _live()
        before = live.rng.getstate()
        effect = _effect("both", ["petrified", "poisoned"])
        _attach(live, effect, path)
        _assert_conditions(live, {"petrified"})
        snapshots.append(
            {
                "path": path,
                "typed": [c.model_dump() for c in _find_combatant(live, TARGET).conditions],
                "coarse": sorted(live.active_conditions[TARGET]),
                "lineage": live.conditions_by_effect[_key(effect)],
                "event": EffectApplied(effect=effect).model_dump(),
            }
        )
        _expire(live, effect)
        _assert_conditions(live, set())
        assert live.rng.getstate() == before
    return {"raw_order": list(effect.statuses), "result": snapshots}


def test_real_hash_seed_changes_cannot_change_attachment_or_expiry():
    script = (
        "import json; from tests.test_effect_condition_lineage import _hash_seed_snapshot; "
        "print(json.dumps(_hash_seed_snapshot(), sort_keys=True))"
    )
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
    assert len({tuple(run["raw_order"]) for run in runs}) == 2
    assert all(run["result"] == runs[0]["result"] for run in runs)
