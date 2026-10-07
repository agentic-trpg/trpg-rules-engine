"""Incapacitated's three migrated mechanics follow canonical clauses independently."""

from __future__ import annotations

import random

import pytest
from dnd5e_srd_data.schema.condition import ConditionEffect, ConditionEffectKind

from dnd5e_engine import orchestrator as orch
from dnd5e_engine.events import ConditionApplied, EffectExpired
from dnd5e_engine.rules import conditions as rules
from dnd5e_engine.rules.effects import project_condition_effects, projected_boolean_flag
from dnd5e_engine.types.effects import ActiveEffect, ActiveEffectChange
from tests.c20_support import act, combatant, pc, start

K = ConditionEffectKind
CONDITIONS = ["incapacitated", "paralyzed", "petrified", "stunned", "unconscious"]
CLAUSES = [
    (K.CANNOT_TAKE_ACTIONS, "condition.cannot_take_actions", rules.conditions_block_actions),
    (
        K.BREAKS_CONCENTRATION,
        "condition.breaks_concentration",
        rules.conditions_break_concentration,
    ),
    (
        K.DISADVANTAGE_INITIATIVE,
        "flags.disadvantage.initiative",
        rules.conditions_disadvantage_initiative,
    ),
]


@pytest.mark.parametrize("kind,key,consumer", CLAUSES)
def test_exact_boolean_vocabulary_ignores_qualifier(kind, key, consumer):
    clause = ConditionEffect(kind=kind, qualifier="false; don't break anything; cannot speak")
    before = clause.model_dump()
    assert project_condition_effects([clause]) == [
        ActiveEffectChange(key=key, mode="override", value=True)
    ]
    assert clause.model_dump() == before


@pytest.mark.parametrize("kind,key,consumer", CLAUSES)
@pytest.mark.parametrize("value", [False, 0, 1, "True", "1", None, 1.0])
def test_consumers_reject_non_true_values(kind, key, consumer, value, monkeypatch):
    change = ActiveEffectChange(key=key, mode="override", value=True).model_copy(
        update={"value": value}
    )
    monkeypatch.setattr(rules, "_project_condition_changes", lambda _: [change])
    assert projected_boolean_flag([change], key) is False
    assert consumer(["incapacitated"]) is False


@pytest.mark.parametrize("kind,key,consumer", CLAUSES)
@pytest.mark.parametrize("mode", ["custom", "add", "multiply", "downgrade", "upgrade"])
def test_consumers_reject_other_modes(kind, key, consumer, mode, monkeypatch):
    change = ActiveEffectChange(key=key, mode=mode, value=True)
    monkeypatch.setattr(rules, "_project_condition_changes", lambda _: [change])
    assert consumer(["incapacitated"]) is False


@pytest.mark.parametrize("kind,key,consumer", CLAUSES)
@pytest.mark.parametrize("key_form", ["prefix", "suffix", "upper"])
def test_consumers_reject_similar_keys(kind, key, consumer, key_form, monkeypatch):
    wrong_key = {"prefix": f"other.{key}", "suffix": f"{key}.other", "upper": key.upper()}[key_form]
    change = ActiveEffectChange(key=wrong_key, mode="override", value=True)
    monkeypatch.setattr(rules, "_project_condition_changes", lambda _: [change])
    assert consumer(["incapacitated"]) is False


@pytest.mark.parametrize("condition", CONDITIONS)
def test_implication_reaches_only_the_incapacitated_canonical_mechanics(condition):
    changes = rules._project_condition_changes([condition.upper(), condition])
    for kind, key, consumer in CLAUSES:
        assert [c for c in changes if c.key == key] == [
            ActiveEffectChange(key=key, mode="override", value=True)
        ]
        assert consumer([condition]) is True
        if condition != "incapacitated":
            assert kind not in rules._DECLARATIVE_CONDITION_MIGRATIONS[condition]


@pytest.mark.parametrize("condition", CONDITIONS)
@pytest.mark.parametrize("removed_kind,removed_key,removed_consumer", CLAUSES)
@pytest.mark.parametrize("remove_from", ["allowlist", "canonical"])
def test_clause_removal_disables_only_its_own_mechanic(
    condition, removed_kind, removed_key, removed_consumer, remove_from, monkeypatch
):
    before = rules._project_condition_changes([condition])
    if remove_from == "allowlist":
        monkeypatch.setitem(
            rules._DECLARATIVE_CONDITION_MIGRATIONS,
            "incapacitated",
            rules._DECLARATIVE_CONDITION_MIGRATIONS["incapacitated"] - {removed_kind},
        )
    else:
        monkeypatch.setitem(
            rules._DECLARATIVE_CONDITION_EFFECTS,
            "incapacitated",
            tuple(
                c
                for c in rules._DECLARATIVE_CONDITION_EFFECTS["incapacitated"]
                if c.kind != removed_kind
            ),
        )
    assert rules._project_condition_changes([condition]) == [
        c for c in before if c.key != removed_key
    ]
    for kind, _key, consumer in CLAUSES:
        assert consumer([condition]) is (kind != removed_kind)


@pytest.mark.parametrize("kind,key,consumer", CLAUSES)
def test_generic_support_does_not_migrate_an_unrelated_condition(kind, key, consumer, monkeypatch):
    clause = ConditionEffect(kind=kind)
    assert project_condition_effects([clause]) == [
        ActiveEffectChange(key=key, mode="override", value=True)
    ]
    monkeypatch.setitem(rules._DECLARATIVE_CONDITION_EFFECTS, "charmed", (clause,))
    assert rules._project_condition_changes(["charmed"]) == []
    assert consumer(["charmed"]) is False


def test_cannot_speak_remains_outside_opt_in_even_with_future_generic_support(monkeypatch):
    clauses = rules._DECLARATIVE_CONDITION_EFFECTS["incapacitated"]
    assert K.CANNOT_SPEAK in {c.kind for c in clauses}
    assert K.CANNOT_SPEAK not in rules._DECLARATIVE_CONDITION_MIGRATIONS["incapacitated"]

    def future_projector(effects):
        effects = tuple(effects)
        changes = project_condition_effects(effects)
        if any(c.kind == K.CANNOT_SPEAK for c in effects):
            changes.append(
                ActiveEffectChange(key="condition.cannot_speak", mode="override", value=True)
            )
        return changes

    monkeypatch.setattr(rules, "project_condition_effects", future_projector)
    assert rules._project_condition_changes(["incapacitated"]) == [
        ActiveEffectChange(key=key, mode="override", value=True)
        for _kind, key, _consumer in CLAUSES
    ]


def _ongoing_lifecycles():
    concentration = ActiveEffect(
        id="effect:bless",
        name="Bless",
        target_id="char:hero",
        origin="cast:bless:char:hero",
        flags={"concentration": True},
    )
    grapple = ActiveEffect(
        id="effect:grapple",
        name="Grappled",
        target_id="mon:foe",
        origin="grapple:unarmed-strike:char:hero",
        statuses={"grappled"},
    )
    rage = ActiveEffect(
        id="effect:rage",
        name="Rage",
        target_id="char:hero",
        origin="cast:rage:char:hero",
    )
    handle, live = start(
        [pc(class_slug="druid", character_level=4)],
        seed=7,
        active_effects=(concentration, grapple),
    )
    act(handle, "char:hero", intent_type="use_feature", feature_id="wild-shape", form_id="wolf")
    assert live.transforms
    assert live.concentration_chain
    # Isolate clause independence with deliberately contradictory internal
    # state. The public Rage entry/seed boundary now ends Concentration.
    live.active_effects["char:hero"].append(rage)
    return live


def test_breaks_concentration_is_independent_of_grapple_rage_and_wild_shape(monkeypatch):
    live = _ongoing_lifecycles()
    monkeypatch.setitem(
        rules._DECLARATIVE_CONDITION_MIGRATIONS,
        "incapacitated",
        rules._DECLARATIVE_CONDITION_MIGRATIONS["incapacitated"] - {K.BREAKS_CONCENTRATION},
    )
    before = live.rng.getstate()
    orch._emit(live, ConditionApplied(target_id="char:hero", condition="paralyzed"))
    assert live.concentration_chain
    assert not any(e.id == "effect:rage" for e in live.active_effects["char:hero"])
    assert not any(e.id == "effect:grapple" for e in live.active_effects["mon:foe"])
    assert live.transforms == {}
    assert "grappled" not in live.active_conditions["mon:foe"]
    assert live.rng.getstate() == before


def test_an_explicit_break_clause_on_another_condition_ends_only_concentration(monkeypatch):
    live = _ongoing_lifecycles()
    monkeypatch.setitem(
        rules._DECLARATIVE_CONDITION_MIGRATIONS,
        "poisoned",
        rules._DECLARATIVE_CONDITION_MIGRATIONS["poisoned"] | {K.BREAKS_CONCENTRATION},
    )
    monkeypatch.setitem(
        rules._DECLARATIVE_CONDITION_EFFECTS,
        "poisoned",
        (
            *rules._DECLARATIVE_CONDITION_EFFECTS["poisoned"],
            ConditionEffect(kind=K.BREAKS_CONCENTRATION),
        ),
    )
    before = live.rng.getstate()
    mark = len(live.event_log)
    orch._emit(live, ConditionApplied(target_id="char:hero", condition="poisoned"))
    assert live.concentration_chain == {}
    assert any(e.id == "effect:rage" for e in live.active_effects["char:hero"])
    assert any(e.id == "effect:grapple" for e in live.active_effects["mon:foe"])
    assert live.transforms
    assert [e.effect_id for e in live.event_log[mark:] if isinstance(e, EffectExpired)] == [
        "effect:bless"
    ]
    assert live.rng.getstate() == before


def test_removing_action_clause_changes_pc_gate_without_changing_other_mechanics(monkeypatch):
    from dnd5e_engine import PlayerIntent
    from dnd5e_engine.orchestrator import IntentRejectedError
    from tests.e2e.harness import run_async

    handle, live = start([pc()], seed=7)
    orch._emit(live, ConditionApplied(target_id="char:hero", condition="incapacitated"))
    intent = PlayerIntent(intent_type="attack", weapon_id="mace", target_id="mon:foe")
    with pytest.raises(IntentRejectedError, match="actor_incapacitated"):
        run_async(orch.submit_player_intent(handle, actor_id="char:hero", intent=intent))
    monkeypatch.setitem(
        rules._DECLARATIVE_CONDITION_MIGRATIONS,
        "incapacitated",
        rules._DECLARATIVE_CONDITION_MIGRATIONS["incapacitated"] - {K.CANNOT_TAKE_ACTIONS},
    )
    assert rules.conditions_break_concentration(["incapacitated"])
    assert rules.conditions_disadvantage_initiative(["incapacitated"])
    run_async(orch.submit_player_intent(handle, actor_id="char:hero", intent=intent))
    assert combatant(live).conditions[0].condition == "incapacitated"


def test_allowed_intents_remain_pass_move_and_drop_concentration():
    assert {"pass", "move", "drop_concentration"} == orch._INCAPACITATED_ALLOWED_INTENTS


def test_ally_qualification_uses_the_shared_declarative_action_gate(monkeypatch):
    _handle, live = start(
        [pc(), pc("char:ally", initiative=15, zone_id="0,1")],
        seed=7,
        active_effects=[
            ActiveEffect(
                id="effect:disabled",
                name="Disabled",
                target_id="char:ally",
                origin="test:ally",
                statuses={"paralyzed"},
            )
        ],
    )
    caster = combatant(live)
    target = orch._find_combatant(live, "mon:foe")
    assert orch._sneak_ally_adjacent_map(live, caster, [target]) == {}
    monkeypatch.setitem(
        rules._DECLARATIVE_CONDITION_MIGRATIONS,
        "incapacitated",
        rules._DECLARATIVE_CONDITION_MIGRATIONS["incapacitated"] - {K.CANNOT_TAKE_ACTIONS},
    )
    assert orch._sneak_ally_adjacent_map(live, caster, [target]) == {"mon:foe": True}


_BASE_RANDOM = random.Random


@pytest.mark.parametrize("condition", CONDITIONS)
@pytest.mark.parametrize("surprised", [False, True])
@pytest.mark.parametrize("fixed", [False, True])
def test_initiative_preserves_exact_draw_order_and_combines_disadvantage_once(
    condition, surprised, fixed, monkeypatch
):
    from dnd5e_engine.specs import EncounterMemberSpec, GridScene, PartyMemberSpec
    from tests.e2e.harness import run_async

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
    effect = ActiveEffect(
        id="effect:disabled",
        name="Disabled",
        target_id="char:hero",
        origin="test:initiative",
        statuses={condition},
    )
    result = run_async(
        orch.start_combat(
            session_id="initiative-contract",
            rng_seed=11,
            grid_scene=GridScene(width=5, height=5),
            party=[
                PartyMemberSpec(
                    entity_id="char:hero",
                    name="Hero",
                    initiative=17 if fixed else None,
                    is_surprised=surprised,
                    dexterity=16,
                    hp_current=30,
                    hp_max=30,
                    zone_id="0,0",
                ),
                PartyMemberSpec(
                    entity_id="char:ally",
                    name="Ally",
                    initiative=None,
                    dexterity=12,
                    hp_current=30,
                    hp_max=30,
                    zone_id="1,0",
                ),
            ],
            encounter=[
                EncounterMemberSpec(
                    entity_id="mon:foe",
                    entity_type="Monster",
                    name="Foe",
                    initiative=None,
                    dexterity=10,
                    hp_current=30,
                    hp_max=30,
                    zone_id="2,0",
                )
            ],
            active_effects=[effect],
        )
    )
    live = orch._get_live(result.handle)
    expected_rng = _BASE_RANDOM(11)
    draws = [expected_rng.randint(1, 20) for _ in range(2 if fixed else 4)]
    expected = {
        "char:hero": 17 if fixed else min(draws[0:2]) + 3,
        "char:ally": draws[0 if fixed else 2] + 1,
        "mon:foe": draws[1 if fixed else 3],
    }
    assert {c.entity_id: c.initiative for c in live.initiative} == expected
    assert instances == [live.rng]
    assert live.rng.draws == [(1, 20, value) for value in draws]
    assert live.rng.getstate() == expected_rng.getstate()
