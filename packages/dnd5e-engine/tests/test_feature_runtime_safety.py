"""Feature invocations either resolve, or leave all rules state and RNG intact."""

from __future__ import annotations

import copy
import dataclasses
import json
import random
from dataclasses import replace
from pathlib import Path

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader
from pydantic import BaseModel

from dnd5e_engine.activities.arithmetic import parse_expression, scalar
from dnd5e_engine.activities.context import SourceUses
from dnd5e_engine.activities.dice import roll_expr, validate_expression
from dnd5e_engine.activities.formula import resolve_roll_data
from dnd5e_engine.build_party import build_party_member
from dnd5e_engine.build_spec import CombatInstance, derive_sheet, make_build_spec
from dnd5e_engine.events import (
    CastFailed,
    CheckRolled,
    ConditionApplied,
    ConditionRemoved,
    EffectApplied,
    EffectExpired,
    HealingApplied,
    SaveRolled,
    TempHpApplied,
)
from dnd5e_engine.feature_audit import (
    audit_context,
    audit_document,
    audit_features,
    reachable_actors,
)
from dnd5e_engine.feature_repertoire import feature_repertoire
from dnd5e_engine.feature_runtime import resource_identity, resource_payments
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.orchestrator import _emit, _granted_feature_slugs
from dnd5e_engine.spatial import cell_id
from dnd5e_engine.specs import PartyMemberSpec
from dnd5e_engine.types.checks import CheckRequest
from dnd5e_engine.types.effects import ActiveEffect, ActiveEffectChange
from tests.c20_support import act, combatant, events, pc, start

LOADER = BundledAssetLoader()
ACTORS = reachable_actors(LOADER)
ROWS = audit_features(LOADER)


@pytest.fixture(autouse=True)
def _loader():
    set_lib_loader_for_tests(LOADER)
    yield
    set_lib_loader_for_tests(None)


def _snapshot(live):
    excluded = {"event_log", "event_queue", "event_listeners", "lifecycle", "topology", "rng"}
    state = {key: value for key, value in vars(live).items() if key not in excluded}

    def normalize(value):
        if isinstance(value, BaseModel):
            return normalize(value.model_dump(mode="json"))
        if dataclasses.is_dataclass(value):
            return normalize(dataclasses.asdict(value))
        if isinstance(value, dict):
            return {str(key): normalize(item) for key, item in value.items()}
        if isinstance(value, (set, frozenset)):
            return sorted(normalize(item) for item in value)
        if isinstance(value, (tuple, list)):
            return [normalize(item) for item in value]
        return value

    return json.dumps(normalize((state, live.rng.getstate())), sort_keys=True).encode()


def _member(actor):
    fields = actor.model_dump()
    fields = {key: value for key, value in fields.items() if key in PartyMemberSpec.model_fields}
    fields.pop("attack_bonus", None)
    fields.pop("weapon_proficiencies", None)
    fields.update(entity_id="char:hero", zone_id=cell_id(0, 0), initiative=20)
    return pc(**fields)


def test_audit_is_deterministic_and_matches_reviewed_artifact():
    document = audit_document(LOADER)
    assert document == audit_document(LOADER)
    path = Path(__file__).parents[3] / "docs/dev/feature-runtime-audit.json"
    assert document == json.loads(path.read_text(encoding="utf-8"))
    assert len(ROWS) == sum(
        len(LOADER.get_feature(slug).activities) for slug in LOADER.list_slugs("features")
    )


@pytest.mark.parametrize("row", ROWS, ids=lambda r: f"{r.slug}/{r.activity_id}")
def test_corpus_invocations_are_safe_before_spend(row):
    member = _member(ACTORS[row.slug])
    handle, live = start([member], seed=71)
    before, pre = _snapshot(live), len(live.event_log)
    feature = LOADER.get_feature(row.slug)
    activity = next(a for a in feature.activities if a.id == row.activity_id)
    args = dict(intent_type="use_feature", feature_id=row.slug, activity_id=row.activity_id)
    if activity.target.affects.type not in ("", "self"):
        args["target_id"] = "mon:foe"
    if row.slug == "wild-shape":
        args["form_id"] = "wolf"
    act(handle, "char:hero", **args)
    emitted = live.event_log[pre:]
    if row.classification != "fully_resolvable":
        assert len(emitted) == 1
        assert isinstance(emitted[0], CastFailed)
        assert emitted[0].reason == "unsupported_feature"
        assert _snapshot(live) == before
    else:
        assert not any(isinstance(e, CastFailed) for e in emitted), emitted


@pytest.mark.parametrize(
    "expr",
    [
        "1d8 +",
        "1d8 / 0",
        "1d8 ** 2",
        "max(1)",
        "abs(1d8)",
        "1d8 + secret",
        "1d0",
        "(1d4)d6",
        "2d6kh1",
    ],
)
def test_closed_grammar_rejects_before_any_draw(expr):
    rng = random.Random(61)
    before = rng.getstate()
    with pytest.raises(ValueError):
        roll_expr(expr, rng)
    assert rng.getstate() == before


@pytest.mark.parametrize(
    "expr", ["2d8 + 1d4 - 3", "max(1, 1d8 + -2)", "min(2d6, 1d4 + 3)", "(2 + 1)d12", "5 * 4"]
)
def test_arithmetic_validation_and_draw_order(expr):
    rng, expected = random.Random(22), random.Random(22)
    before = rng.getstate()
    validate_expression(expr)
    assert rng.getstate() == before
    if expr == "2d8 + 1d4 - 3":
        result = expected.randint(1, 8) + expected.randint(1, 8) + expected.randint(1, 4) - 3
    elif expr == "max(1, 1d8 + -2)":
        result = max(1, expected.randint(1, 8) - 2)
    elif expr == "min(2d6, 1d4 + 3)":
        result = min(expected.randint(1, 6) + expected.randint(1, 6), expected.randint(1, 4) + 3)
    elif expr == "(2 + 1)d12":
        result = sum(expected.randint(1, 12) for _ in range(3))
    else:
        result = 20
    assert roll_expr(expr, rng) == result
    assert rng.getstate() == expected.getstate()


def test_source_use_carrier_and_ability_dc_are_draw_free():
    ctx = replace(
        audit_context(ACTORS["relentless-rage"], "relentless-rage", LOADER),
        source_uses=SourceUses(2, 6),
    )
    expr = resolve_roll_data("10 + @item.uses.spent * 5", ctx)
    assert scalar(parse_expression(expr)) == 20
    assert resolve_roll_data("@item.uses.value + @item.uses.max", ctx) == "4 + 6"
    with pytest.raises(ValueError, match="carrier"):
        resolve_roll_data("@item.uses.spent", replace(ctx, source_uses=None))


def test_selected_choice_build_reaches_live_invocation():
    build = make_build_spec(
        classes={"warlock": 2}, species_slug="human", selected_choices=("fiendish-vigor",)
    )
    assert "fiendish-vigor" in derive_sheet(build, loader=LOADER).features
    member = build_party_member(
        build,
        CombatInstance(entity_id="char:hero", name="Hero", initiative=20, zone_id=cell_id(0, 0)),
        loader=LOADER,
    )
    assert "fiendish-vigor" in member.granted_features
    handle, live = start([member], seed=4)
    assert "fiendish-vigor" in _granted_feature_slugs(combatant(live))
    before = live.rng.getstate()
    act(handle, "char:hero", intent_type="use_feature", feature_id="fiendish-vigor")
    assert [e.amount for e in events(live, TempHpApplied)] == [12]
    assert not combatant(live).action_available
    assert live.rng.getstate() == before


def test_repertoire_sentinel_and_illegal_host_injection():
    member = pc(class_slug="fighter", character_level=2)
    assert "action-surge" in [o.slug for o in feature_repertoire(member, LOADER)]
    assert feature_repertoire(member.model_copy(update={"granted_features": ()}), LOADER) == ()
    with pytest.raises(ValueError, match="illegal grants"):
        feature_repertoire(member.model_copy(update={"granted_features": ("rage",)}), LOADER)


def test_owner_class_spellcasting_dc_in_multiclass_combat():
    handle, live = start(
        [
            pc(
                class_slug="wizard",
                classes={"wizard": 2, "cleric": 3},
                character_level=5,
                intelligence=20,
                wisdom=16,
            )
        ],
        seed=7,
    )
    before = live.rng.getstate()
    act(
        handle,
        "char:hero",
        intent_type="use_feature",
        feature_id="channel-divinity-cleric",
        activity_id="OY9UrTXvlRL0JUoI",
        target_id="mon:foe",
    )
    assert events(live, SaveRolled)[0].dc == 14
    assert (
        live.custom_counters_by_entity["char:hero"]["feature_use:channel-divinity-cleric"]["spent"]
        == 1
    )
    expected = random.Random()
    expected.setstate(before)
    expected.randint(1, 8)
    expected.randint(1, 20)
    assert live.rng.getstate() == expected.getstate()


@pytest.mark.parametrize(
    ("activity_id", "cost", "dodging"),
    [("EFzidO6yAapw8d60", 0, False), ("7xj7b6e8tDznDSrE", 1, True)],
)
def test_patient_defense_uses_real_action_state(activity_id, cost, dodging):
    handle, live = start([pc(class_slug="monk", character_level=3)], seed=5)
    before = live.rng.getstate()
    act(
        handle,
        "char:hero",
        intent_type="use_feature",
        feature_id="monks-focus",
        activity_id=activity_id,
    )
    hero = combatant(live)
    assert hero.action_available
    assert not hero.bonus_action_available
    assert hero.disengaging_this_turn
    assert hero.dodging is dodging
    assert (
        live.custom_counters_by_entity.get("char:hero", {})
        .get("feature_use:monks-focus", {})
        .get("spent", 0)
        == cost
    )
    assert live.rng.getstate() == before
    assert not any(e.condition == "dodging" for e in events(live, ConditionApplied))


@pytest.mark.parametrize("effect_sources", [False, True])
def test_remove_poison_emits_authoritative_removal_and_costs_five(effect_sources):
    handle, live = start([pc(class_slug="paladin", character_level=2)], seed=5)
    _emit(live, ConditionApplied(target_id="char:hero", condition="poisoned"))
    if effect_sources:
        for index in range(2):
            _emit(
                live,
                EffectApplied(
                    effect=ActiveEffect(
                        id=f"effect:poison:{index}",
                        name="Multiple statuses",
                        origin=f"item:poison:{index}",
                        target_id="char:hero",
                        statuses={"poisoned", "deafened"},
                        changes=[ActiveEffectChange(key="ac.bonus", mode="add", value=1)],
                    )
                ),
            )
    observed = []
    live.event_listeners.append(observed.append)
    pre = len(live.event_log)
    before = live.rng.getstate()
    act(
        handle,
        "char:hero",
        intent_type="use_feature",
        feature_id="lay-on-hands",
        activity_id="K6UeXQwTyDHWvis8",
    )
    assert [e.condition for e in events(live, ConditionRemoved)] == ["poisoned"]
    assert events(live, ConditionRemoved)[0].all_sources
    assert "poisoned" not in live.active_conditions["char:hero"]
    assert all(c.condition != "poisoned" for c in combatant(live).conditions)
    if effect_sources:
        assert "deafened" in live.active_conditions["char:hero"]
        assert len(live.active_effects["char:hero"]) == 2
        for effect in live.active_effects["char:hero"]:
            assert effect.statuses == {"deafened"}
            assert effect.changes[0].key == "ac.bonus"
        assert all("poisoned" not in values for values in live.conditions_by_effect.values())
    assert live.custom_counters_by_entity["char:hero"]["feature_use:lay-on-hands"]["spent"] == 5
    assert live.rng.getstate() == before
    assert observed == live.event_log[pre:]


def test_stunning_strike_resolves_the_shared_focus_identity_without_spending():
    feature = LOADER.get_feature("stunning-strike")
    actor = ACTORS[feature.slug]
    ctx = audit_context(actor, feature.slug, LOADER)
    payments = resource_payments(
        feature,
        feature.activities[0],
        ctx,
        LOADER,
        tuple(o.slug for o in feature_repertoire(actor, LOADER)),
    )
    assert [(p.feature_slug, p.cost, p.maximum) for p in payments] == [("monks-focus", 1, 20)]
    assert resource_identity("feat:monks-focus", feature, LOADER) == "monks-focus"


class FeatureOverlay(BundledAssetLoader):
    def __init__(self, feature):
        super().__init__()
        self.feature = feature

    def get_feature(self, slug):
        return self.feature if slug == self.feature.slug else super().get_feature(slug)


@pytest.mark.parametrize(
    "formula", ["@unknown", "1d10 * nope", "max(1)", "1d10 / 2", "1d10 + @classes.wizard.levels"]
)
def test_malformed_invocation_leaves_every_rule_field_and_rng_unchanged(formula):
    feature = LOADER.get_feature("second-wind").model_copy(deep=True)
    activity = feature.activities[0]
    custom = activity.healing.custom.model_copy(update={"enabled": True, "formula": formula})
    feature.activities[0] = activity.model_copy(
        update={"healing": activity.healing.model_copy(update={"custom": custom})}
    )
    set_lib_loader_for_tests(FeatureOverlay(feature))
    handle, live = start([pc(class_slug="fighter", character_level=2, spell_slots={1: 2})], seed=9)
    before, pre = _snapshot(live), len(live.event_log)
    act(handle, "char:hero", intent_type="use_feature", feature_id=feature.slug)
    assert _snapshot(live) == before
    assert [e.reason for e in live.event_log[pre:]] == ["unsupported_feature"]


@pytest.mark.parametrize("focus_spent", [0, 3])
def test_multiple_resource_costs_validate_together_and_commit_once(focus_spent):
    feature = LOADER.get_feature("second-wind").model_copy(deep=True)
    target = LOADER.get_feature("stunning-strike").activities[0].consumption.targets[0]
    feature.activities[0].consumption.targets.append(target.model_copy(deep=True))
    set_lib_loader_for_tests(FeatureOverlay(feature))
    handle, live = start(
        [
            pc(
                class_slug="fighter",
                classes={"fighter": 2, "monk": 3},
                character_level=5,
                hp_current=1,
                custom_counters={"feature_use:monks-focus": {"spent": focus_spent}},
            )
        ],
        seed=9,
    )
    before = _snapshot(live)
    act(handle, "char:hero", intent_type="use_feature", feature_id=feature.slug)
    if focus_spent:
        assert _snapshot(live) == before
        assert events(live, CastFailed)[-1].reason == "no_uses_remaining"
    else:
        counters = live.custom_counters_by_entity["char:hero"]
        assert counters["feature_use:second-wind"]["spent"] == 1
        assert counters["feature_use:monks-focus"]["spent"] == 1
        assert len(events(live, HealingApplied)) == 1


def test_unexpected_resolver_value_error_is_rolled_back_and_propagates(monkeypatch):
    import dnd5e_engine.orchestrator as orch

    handle, live = start([pc(class_slug="fighter", character_level=2)], seed=9)
    before, log = _snapshot(live), copy.deepcopy(live.event_log)
    queue_size = live.event_queue.qsize()
    observed = []
    live.event_listeners.append(observed.append)

    def broken(activity, ctx, **kwargs):
        ctx.rng.randint(1, 8)
        ctx.event_emitter(HealingApplied(target_id="char:hero", amount=3))
        raise ValueError("injected resolver defect")

    monkeypatch.setattr(orch, "resolve_activity", broken)
    with pytest.raises(ValueError, match="injected resolver defect"):
        act(handle, "char:hero", intent_type="use_feature", feature_id="second-wind")
    assert _snapshot(live) == before
    assert live.event_log == log
    assert live.event_queue.qsize() == queue_size
    assert observed == []


@pytest.mark.parametrize(
    ("class_slug", "choice", "skill"),
    [
        ("cleric", "divine-order-thaumaturge", "religion"),
        ("druid", "primal-order-magician", "nature"),
    ],
)
def test_derived_skill_bonus_reaches_shared_live_checks(class_slug, choice, skill):
    from dnd5e_engine.activities.actor_stats import check_modifier

    build = make_build_spec(
        classes={class_slug: 1},
        species_slug="human",
        ability_scores={"wisdom": 16, "intelligence": 12},
        selected_choices=(choice,),
    )
    sheet = derive_sheet(build, loader=LOADER)
    assert sheet.skill_check_bonuses[skill] == 3
    member = build_party_member(
        build,
        CombatInstance(entity_id="char:hero", name="Hero", initiative=20, zone_id=cell_id(0, 0)),
        loader=LOADER,
    )
    handle, live = start([member], seed=9)
    hero = combatant(live)
    assert check_modifier(hero, "int", skill).total == 4
    assert check_modifier(hero, "wis", skill).total == 3
    act(
        handle,
        "char:hero",
        intent_type="check",
        check=CheckRequest(actor_id="char:hero", ability="int", skill=skill, dc=10),
    )
    assert events(live, CheckRolled)[-1].modifier == 4


@pytest.mark.parametrize("case", ["activation", "resource", "maximum", "scaling", "effect", "save"])
def test_bad_typed_data_is_rejected_before_payment(case):
    slug = "channel-divinity-cleric" if case == "save" else "second-wind"
    feature = LOADER.get_feature(slug).model_copy(deep=True)
    activity = (
        next(a for a in feature.activities if a.kind == "save")
        if case == "save"
        else feature.activities[0]
    )
    if case == "activation":
        activity.activation = activity.activation.model_copy(update={"type": "reaction"})
    elif case == "resource":
        activity.consumption.targets[0] = activity.consumption.targets[0].model_copy(
            update={"target": "feat:missing"}
        )
    elif case == "maximum":
        feature.uses.max = "1d8"
    elif case == "scaling":
        activity.consumption = activity.consumption.model_copy(
            update={"scaling": activity.consumption.scaling.model_copy(update={"max": "@unknown"})}
        )
    elif case == "effect":
        from dnd5e_srd_data.schema.common import AppliedEffectRef

        activity.effects.append(AppliedEffectRef(id="missing"))
    else:
        activity.save = activity.save.model_copy(
            update={
                "dc": activity.save.dc.model_copy(update={"calculation": "flat", "formula": "1d8"})
            }
        )
    set_lib_loader_for_tests(FeatureOverlay(feature))
    handle, live = start(
        [pc(class_slug="cleric" if case == "save" else "fighter", character_level=3)], seed=9
    )
    before = _snapshot(live)
    act(
        handle,
        "char:hero",
        intent_type="use_feature",
        feature_id=slug,
        activity_id=activity.id,
        target_id="mon:foe" if case == "save" else None,
    )
    assert events(live, CastFailed)[-1].reason == "unsupported_feature"
    assert _snapshot(live) == before


@pytest.mark.parametrize("selection", ["unknown", None])
def test_ambiguous_or_unknown_selection_is_atomic(selection):
    handle, live = start([pc(class_slug="monk", character_level=3)], seed=9)
    before = _snapshot(live)
    act(
        handle,
        "char:hero",
        intent_type="use_feature",
        feature_id="monks-focus",
        activity_id=selection,
    )
    assert _snapshot(live) == before
    assert events(live, CastFailed)[-1].reason == "unsupported_feature"


@pytest.mark.parametrize("target", ["missing", "mon:foe"])
def test_invalid_or_distant_touch_target_does_not_pay(target):
    from tests.c20_support import foe

    handle, live = start(
        [pc(class_slug="paladin", character_level=2)],
        seed=9,
        encounter=[foe(zone_id=cell_id(5, 0))],
    )
    before = _snapshot(live)
    act(
        handle,
        "char:hero",
        intent_type="use_feature",
        feature_id="lay-on-hands",
        activity_id="K6UeXQwTyDHWvis8",
        target_id=target,
    )
    assert _snapshot(live) == before
    assert events(live, CastFailed)[-1].reason == (
        "target_invalid" if target == "missing" else "out_of_range"
    )


def test_invalid_live_bonus_is_preflight_refusal():
    bad = ActiveEffect(
        id="bad",
        name="Bad",
        origin="test",
        target_id="mon:foe",
        changes=[
            ActiveEffectChange(key="system.bonuses.abilities.save", mode="add", value="1d4 + nope")
        ],
    )
    handle, live = start([pc(class_slug="cleric", character_level=3)], seed=9, active_effects=[bad])
    before = _snapshot(live)
    act(
        handle,
        "char:hero",
        intent_type="use_feature",
        feature_id="channel-divinity-cleric",
        activity_id="OY9UrTXvlRL0JUoI",
        target_id="mon:foe",
    )
    assert _snapshot(live) == before
    assert events(live, CastFailed)[-1].reason == "unsupported_feature"


def test_source_uses_in_resolution_are_the_precommit_snapshot():
    feature = LOADER.get_feature("second-wind").model_copy(deep=True)
    activity = feature.activities[0]
    custom = activity.healing.custom.model_copy(
        update={
            "enabled": True,
            "formula": "max(1, @item.uses.spent + @item.uses.value * @item.uses.max)",
        }
    )
    feature.activities[0] = activity.model_copy(
        update={"healing": activity.healing.model_copy(update={"custom": custom})}
    )
    set_lib_loader_for_tests(FeatureOverlay(feature))
    handle, live = start(
        [
            pc(
                class_slug="fighter",
                character_level=2,
                hp_current=1,
                custom_counters={"feature_use:second-wind": {"spent": 1}},
            )
        ],
        seed=9,
    )
    before = live.rng.getstate()
    act(handle, "char:hero", intent_type="use_feature", feature_id="second-wind")
    assert events(live, HealingApplied)[-1].amount == 3
    assert live.custom_counters_by_entity["char:hero"]["feature_use:second-wind"]["spent"] == 2
    assert live.rng.getstate() == before


def test_choice_capacity_cannot_be_bypassed_by_host_or_build():
    picks = ("divine-order-thaumaturge", "divine-order-protector")
    with pytest.raises(ValueError, match="capacity"):
        derive_sheet(
            make_build_spec(classes={"cleric": 1}, species_slug="human", selected_choices=picks),
            loader=LOADER,
        )
    with pytest.raises(ValueError, match="capacity"):
        feature_repertoire(pc(class_slug="cleric", granted_features=picks), LOADER)


def test_rage_heavy_armor_entry_is_draw_free_and_unpaid():
    handle, live = start([pc(class_slug="barbarian", equipment=["plate-armor"])], seed=9)
    before = _snapshot(live)
    act(handle, "char:hero", intent_type="use_feature", feature_id="rage")
    assert _snapshot(live) == before
    assert events(live, CastFailed)[-1].reason == "unsupported_feature"


@pytest.mark.parametrize("intent_type", ["cast_spell", "ready"])
def test_rage_blocks_cast_and_ready_without_spending(intent_type):
    handle, live = start(
        [pc(class_slug="barbarian", spell_slots={1: 1}, spells_known=["bless"])], seed=9
    )
    act(handle, "char:hero", intent_type="use_feature", feature_id="rage")
    before = _snapshot(live)
    act(
        handle,
        "char:hero",
        intent_type=intent_type,
        spell_id="bless",
        reaction_trigger="cast_spell" if intent_type == "ready" else None,
    )
    assert _snapshot(live) == before
    assert events(live, CastFailed)[-1].reason == "raging"


def test_rage_drops_concentration_and_ends_when_armor_changes():
    from dnd5e_engine.orchestrator import _rage_effect, _update_combatant

    concentration = ActiveEffect(
        id="effect:bless",
        name="Bless",
        origin="cast:bless:char:hero",
        target_id="char:hero",
        flags={"concentration": True},
    )
    handle, live = start([pc(class_slug="barbarian")], seed=9, active_effects=[concentration])
    act(handle, "char:hero", intent_type="use_feature", feature_id="rage")
    assert combatant(live).concentration_effect_id is None
    assert not live.concentration_chain.get("char:hero")
    assert _rage_effect(live, "char:hero").duration.rounds == 100
    assert any(e.effect_id == "effect:bless" for e in events(live, EffectExpired))
    _update_combatant(live, "char:hero", worn_armor="heavy")
    assert _rage_effect(live, "char:hero") is None
    assert events(live, EffectExpired)[-1].reason == "heavy_armor"


def test_rage_does_not_release_queued_spell_reactions():
    from dnd5e_engine.orchestrator import _PendingReaction, _pop_pending_reaction

    handle, live = start([pc(class_slug="barbarian", spell_slots={1: 1})], seed=9)
    live.pending_reactions.append(_PendingReaction("char:hero", "hit_by_attack", "shield", 1))
    act(handle, "char:hero", intent_type="use_feature", feature_id="rage")
    before = _snapshot(live)
    assert _pop_pending_reaction(live, "hit_by_attack", triggering_actor_id="mon:foe") is None
    assert _snapshot(live) == before


def test_rage_outer_cap_expires_after_one_hundred_round_ticks():
    from dnd5e_engine.activities.effects import passive_effect_to_active_effect
    from dnd5e_engine.orchestrator import _rage_effect, _tick_durations_at_turn_end

    effect = passive_effect_to_active_effect(
        LOADER.get_feature("rage").passive_effects[0],
        target_id="char:hero",
        caster_id="char:hero",
        ctx=audit_context(ACTORS["rage"], "rage", LOADER),
    )
    _handle, live = start(
        [pc(class_slug="barbarian", character_level=15)], seed=9, active_effects=[effect]
    )
    before = live.rng.getstate()
    for _ in range(99):
        _tick_durations_at_turn_end(live, "char:hero")
    assert _rage_effect(live, "char:hero").duration.rounds == 1
    _tick_durations_at_turn_end(live, "char:hero")
    assert _rage_effect(live, "char:hero") is None
    assert events(live, EffectExpired)[-1].reason == "duration"
    assert live.rng.getstate() == before


def test_new_effect_bonus_grammar_is_checked_before_it_enters_live_state():
    feature = LOADER.get_feature("rage").model_copy(deep=True)
    changes = feature.passive_effects[0].changes
    changes[0] = changes[0].model_copy(update={"value": "1d8 + bad"})
    set_lib_loader_for_tests(FeatureOverlay(feature))
    handle, live = start([pc(class_slug="barbarian")], seed=9)
    before = _snapshot(live)
    act(handle, "char:hero", intent_type="use_feature", feature_id="rage")
    assert _snapshot(live) == before
    assert events(live, CastFailed)[-1].reason == "unsupported_feature"


@pytest.mark.parametrize(
    "feature_id,activity_id,conditions",
    [
        ("bardic-inspiration", None, ("blinded", "deafened")),
        ("channel-divinity-cleric", "OY9UrTXvlRL0JUoI", ("blinded",)),
    ],
)
def test_feature_perception_gate_preserves_all_state(feature_id, activity_id, conditions):
    handle, live = start(
        [
            pc(
                class_slug="bard" if feature_id == "bardic-inspiration" else "cleric",
                character_level=3,
            )
        ],
        seed=9,
    )
    for condition in conditions:
        _emit(
            live,
            ConditionApplied(
                target_id="mon:foe" if feature_id == "bardic-inspiration" else "char:hero",
                condition=condition,
            ),
        )
    before = _snapshot(live)
    act(
        handle,
        "char:hero",
        intent_type="use_feature",
        feature_id=feature_id,
        activity_id=activity_id,
        target_id="mon:foe",
    )
    assert _snapshot(live) == before
    assert events(live, CastFailed)[-1].reason == "target_invalid"
