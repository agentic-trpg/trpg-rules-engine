"""Acceptance for the typed combat check boundary and draw discipline."""

from __future__ import annotations

import random

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.common import CheckActivity
from dnd5e_srd_data.schema.monster import CreatureSize
from pydantic import ValidationError

from dnd5e_engine import CheckRequest, CombatInstance, build_party_member, make_build_spec
from dnd5e_engine.activities.check import _check_modifier, resolve_check
from dnd5e_engine.activities.check_pipeline import resolve_check_request
from dnd5e_engine.activities.context import ActivityResolutionContext
from dnd5e_engine.events import CheckRolled, EffectExpired, TurnStarted
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.live_checks import project_check_states, resolve_live_check
from dnd5e_engine.orchestrator import IntentRejectedError, _emit, _update_combatant
from dnd5e_engine.specs import GridScene
from dnd5e_engine.types.checks import CheckActorState, HelpCheckGrant
from dnd5e_engine.types.conditions import ActiveCondition
from dnd5e_engine.types.effects import ActiveEffect, ActiveEffectChange
from tests.c20_support import act, combatant, events, foe, pc, start


class CountingRandom(random.Random):
    def __init__(self, seed=1):
        super().__init__(seed)
        self.draws = []

    def randint(self, a, b):
        value = super().randint(a, b)
        self.draws.append((a, b, value))
        return value


@pytest.fixture(autouse=True)
def bundled():
    set_lib_loader_for_tests(BundledAssetLoader())
    yield
    set_lib_loader_for_tests(None)


def condition(name, source="implied:scenario", **kwargs):
    return ActiveCondition(condition=name, source_entity_id=source, scope="combat", **kwargs)


def table(**fields):
    handle, live = start([pc(**fields)], seed=7)
    live.rng = CountingRandom(7)
    return handle, live


def request(**fields):
    return CheckRequest(**({"actor_id": "char:hero", "ability": "wis", "dc": 15} | fields))


@pytest.mark.parametrize(
    "grants,draws,mode",
    [
        ({}, 1, "normal"),
        ({"advantage": ("flag",)}, 2, "advantage"),
        ({"disadvantage": ("flag",)}, 2, "disadvantage"),
        ({"advantage": ("flag", "effect"), "disadvantage": ("flag",)}, 1, "normal"),
    ],
)
def test_typed_action_pays_once_and_composes_draws(grants, draws, mode):
    handle, live = table(wisdom=16, character_level=5, skill_proficiencies=("perception",))
    act(handle, "char:hero", intent_type="check", check=request(skill="perception", **grants))
    event = events(live, CheckRolled)[-1]
    assert (event.modifier, event.advantage, event.cost_owner) == (6, mode, "action")
    assert len(live.rng.draws) == draws
    assert not combatant(live).action_available
    assert event.roll_total == event.natural + event.modifier


@pytest.mark.parametrize(
    "name,sense,failed",
    [
        ("blinded", "sight", True),
        ("blinded", "hearing", False),
        ("deafened", "hearing", True),
        ("deafened", "sight", False),
        ("blinded", "none", False),
        ("deafened", "none", False),
    ],
)
def test_sensory_auto_failure_is_semantic_and_draw_free(name, sense, failed):
    handle, live = table(reliable_talent=True, skill_proficiencies=("perception",))
    _update_combatant(live, "char:hero", conditions=[condition(name)])
    act(
        handle,
        "char:hero",
        intent_type="check",
        check=request(
            skill="perception",
            required_sense=sense,
            advantage=("flag",),
            disadvantage=("effect",),
        ),
    )
    event = events(live, CheckRolled)[-1]
    assert len(live.rng.draws) == (0 if failed else 1)
    assert event.auto_failure == (sense if failed else None)
    if failed:
        assert event.natural is None
        assert event.succeeded is False
        assert not event.reliable_talent_applied
    assert not combatant(live).action_available


@pytest.mark.parametrize(
    "skill,proficient,expertise,jack,expected",
    [
        (None, False, False, True, 2),
        ("athletics", False, False, True, 3),
        ("athletics", True, False, True, 5),
        ("athletics", True, True, True, 8),
        ("athletics", False, True, False, 2),
    ],
)
def test_real_modifier_projection_and_jack_scope(skill, proficient, expertise, jack, expected):
    _, live = table(
        strength=14,
        character_level=5,
        jack_of_all_trades=jack,
        skill_proficiencies=("athletics",) if proficient else (),
        skill_expertise=("athletics",) if expertise else (),
    )
    event = resolve_live_check(live, request(ability="str", skill=skill))
    assert event.modifier == expected


@pytest.mark.parametrize("mode", ["normal", "advantage", "disadvantage"])
@pytest.mark.parametrize("proficient", [False, True])
def test_reliable_talent_floors_kept_die_without_additional_draws(mode, proficient):
    _, live = table(reliable_talent=True, skill_proficiencies=("stealth",) if proficient else ())
    live.rng = CountingRandom(2)  # first two d20: 2, 3
    grants = {} if mode == "normal" else {mode: ("flag",)}
    event = resolve_live_check(live, request(ability="dex", skill="stealth", **grants))
    expected_raw = 3 if mode == "advantage" else 2
    assert event.rolled_natural == expected_raw
    assert event.natural == (10 if proficient else expected_raw)
    assert event.reliable_talent_applied is proficient
    assert len(live.rng.draws) == (1 if mode == "normal" else 2)


def test_reliable_talent_on_explicit_tool_proficiency_and_not_raw_ability():
    _, live = table(reliable_talent=True, tool_proficiencies=("thieves-tools",))
    live.rng = CountingRandom(2)
    event = resolve_live_check(live, request(ability="dex", tool="thieves-tools"))
    assert (event.natural, event.modifier) == (10, 2)
    live.rng = CountingRandom(2)
    assert resolve_live_check(live, request(ability="dex")).natural == 2


def test_hide_uses_shared_pipeline_with_noisy_armor_and_poisoned():
    handle, live = start(
        [pc(stealth_disadvantage=True)],
        seed=7,
        grid_scene=GridScene(width=10, height=10, cover_cells={"0,0": "three_quarters"}),
    )
    _update_combatant(live, "char:hero", conditions=[condition("poisoned")])
    live.rng = CountingRandom(7)
    act(handle, "char:hero", intent_type="hide")
    event = events(live, CheckRolled)[-1]
    assert (event.context, event.ability, event.skill, event.dc) == ("hide", "dex", "stealth", 15)
    assert event.disadvantage_sources == ["condition:attacker", "armor"]
    assert len(live.rng.draws) == 2


@pytest.mark.parametrize(
    "social,source,expected",
    [(True, "char:hero", True), (False, "char:hero", False), (True, "char:other", False)],
)
def test_social_advantage_uses_actual_effect_lineage(social, source, expected):
    _, live = table()
    effect = ActiveEffect(
        name="Charm", id="charm", target_id="mon:foe", origin=f"cast:charm:{source}"
    )
    live.active_effects["mon:foe"] = [effect]
    _update_combatant(live, "mon:foe", conditions=[condition("charmed", source_effect_id="charm")])
    event = resolve_live_check(
        live, request(ability="cha", target_id="mon:foe", social_interaction=social)
    )
    assert ("charmed" in event.advantage_sources) is expected
    assert len(live.rng.draws) == (2 if expected else 1)


def test_charmed_help_poisoned_and_armor_cancel_and_consume_only_matching_help():
    _, live = table(stealth_disadvantage=True)
    effect = ActiveEffect(
        name="Charm", id="charm", target_id="mon:foe", origin="cast:charm:char:hero"
    )
    live.active_effects["mon:foe"] = [effect]
    _update_combatant(live, "mon:foe", conditions=[condition("charmed", source_effect_id="charm")])
    _update_combatant(live, "char:hero", conditions=[condition("poisoned")])
    live.help_check_grants = [HelpCheckGrant("char:helper", "char:hero", "stealth")]
    event = resolve_live_check(
        live, request(ability="dex", skill="stealth", social_interaction=True, target_id="mon:foe")
    )
    assert event.advantage_sources == ["help", "charmed"]
    assert event.disadvantage_sources == ["condition:attacker", "armor"]
    assert event.advantage == "normal"
    assert len(live.rng.draws) == 1
    assert not live.help_check_grants


def test_help_action_scopes_consumes_and_expires_at_helpers_next_start():
    handle, live = start(
        [pc("char:helper", skill_proficiencies=("perception",)), pc(initiative=15, zone_id="0,1")],
        seed=7,
    )
    act(
        handle,
        "char:helper",
        intent_type="help",
        help_check={
            "beneficiary_id": "char:hero",
            "skill": "perception",
            "assistance": "verbal",
            "range_ft": 10,
            "assistance_possible": True,
        },
    )
    assert not combatant(live, "char:helper").action_available
    live.rng = CountingRandom(7)
    assert resolve_live_check(live, request(skill="insight")).advantage == "normal"
    assert len(live.help_check_grants) == 1
    assert resolve_live_check(live, request(skill="perception")).advantage == "advantage"
    assert not live.help_check_grants
    live.help_check_grants.append(HelpCheckGrant("char:helper", "char:hero", "perception"))
    _emit(live, TurnStarted(actor_id="char:hero"))
    assert len(live.help_check_grants) == 1
    _emit(live, TurnStarted(actor_id="char:helper"))
    assert not live.help_check_grants


@pytest.mark.parametrize(
    "changes",
    [
        {"skill": "arcana"},
        {"range_ft": 0},
        {"assistance_possible": False},
        {"beneficiary_id": "mon:foe"},
        {"beneficiary_id": "char:helper"},
    ],
)
def test_illegal_help_has_no_payment_events_or_rng(changes):
    handle, live = start(
        [pc("char:helper", skill_proficiencies=("perception",)), pc(initiative=15, zone_id="0,1")],
        seed=7,
    )
    before, rng = list(live.event_log), live.rng.getstate()
    spec = {
        "beneficiary_id": "char:hero",
        "skill": "perception",
        "assistance": "physical",
        "range_ft": 5,
        "assistance_possible": True,
    } | changes
    with pytest.raises(IntentRejectedError):
        act(handle, "char:helper", intent_type="help", help_check=spec)
    assert live.event_log == before
    assert live.rng.getstate() == rng
    assert combatant(live, "char:helper").action_available


@pytest.mark.parametrize(
    "fields",
    [
        {"actor_id": "mon:foe"},
        {"target_id": "missing"},
        {"context": "internal"},
        {"context": "search", "ability": "str"},
        {"context": "study", "ability": "int", "skill": "history", "dc": None},
        {"redeem_granted_die": "feature_grant:bardic-inspiration"},
    ],
)
def test_illegal_check_preserves_all_state_and_rng(fields):
    handle, live = table()
    before = [e.model_dump_json() for e in live.event_log]
    with pytest.raises(IntentRejectedError):
        act(handle, "char:hero", intent_type="check", check=request(**fields))
    assert [e.model_dump_json() for e in live.event_log] == before
    assert not live.rng.draws
    assert combatant(live).action_available


def test_untyped_or_free_checks_are_rejected():
    with pytest.raises(ValidationError):
        request(free=True)
    handle, live = table()
    with pytest.raises(IntentRejectedError):
        act(handle, "char:hero", intent_type="check")
    with pytest.raises(IntentRejectedError):
        act(handle, "char:hero", intent_type="check", check=request(), use_bonus_action=True)
    assert combatant(live).action_available
    assert not live.rng.draws


@pytest.mark.parametrize(
    "context,ability,skill",
    [
        ("search", "wis", "perception"),
        ("study", "int", "history"),
        ("influence", "cha", "persuasion"),
    ],
)
def test_action_wrappers_use_explicit_dc_and_semantics(context, ability, skill):
    handle, live = table()
    act(
        handle,
        "char:hero",
        intent_type="check",
        check=request(
            context=context,
            ability=ability,
            skill=skill,
            dc=17,
            social_interaction=context == "influence",
            target_id="mon:foe",
        ),
    )
    event = events(live, CheckRolled)[-1]
    assert event.dc == 17
    assert event.context == context


def test_live_monster_uses_real_stat_block_magnitudes_and_sunlight():
    _, live = start(
        [pc()],
        seed=7,
        encounter=[foe(monster_template_slug="drider")],
        grid_scene=GridScene(width=10, height=10, sunlight=True),
    )
    monster = combatant(live, "mon:foe")
    asset = BundledAssetLoader().get_monster("drider")
    live.rng = CountingRandom(7)
    event = resolve_live_check(live, request(actor_id="mon:foe", skill="perception"))
    assert event.modifier == asset.skills.perception
    assert "trait" in event.disadvantage_sources
    assert len(live.rng.draws) == 2
    live.scene_sunlight = False
    assert (
        resolve_live_check(live, request(actor_id="mon:foe", skill="perception")).advantage
        == "normal"
    )
    assert monster.wisdom == asset.ability_scores.wis


def test_exhaustion_skill_branch_regression_and_real_effect_bonus_order():
    _, live = table(wisdom=14, skill_proficiencies=("perception",))
    actor = combatant(live)
    ctx = ActivityResolutionContext(
        rng=live.rng,
        caster=actor,
        targets=[],
        caster_abilities={},
        event_emitter=lambda _: None,
        check_modifiers={actor.entity_id: {"skills": {"perception": 6}}},
        d20_test_penalty={actor.entity_id: -4},
    )
    assert _check_modifier(ctx, actor, skill="prc", ability="wis") == 2
    _update_combatant(
        live, actor.entity_id, conditions=[condition("exhaustion", exhaustion_level=2)]
    )
    live.active_effects[actor.entity_id] = [
        ActiveEffect(
            name="Guidance",
            id="guidance",
            target_id=actor.entity_id,
            origin="test",
            changes=[
                ActiveEffectChange(key="system.bonuses.abilities.check", mode="add", value="1d4")
            ],
        )
    ]
    event = resolve_live_check(live, request(skill="perception"))
    assert [b for _, b, _ in live.rng.draws] == [20, 4]
    assert event.modifier == 4 - 4 + live.rng.draws[-1][2]


@pytest.mark.parametrize(
    "dc,autofail,spends", [(30, False, True), (0, False, False), (30, True, False)]
)
def test_bardic_redemption_only_rolls_and_expires_on_failed_d20(dc, autofail, spends):
    handle, live = start(
        [
            pc("char:bard", class_slug="bard", character_level=3, charisma=16),
            pc(initiative=15, zone_id="0,1"),
        ],
        seed=7,
    )
    act(
        handle,
        "char:bard",
        intent_type="use_feature",
        feature_id="bardic-inspiration",
        target_id="char:hero",
    )
    act(handle, "char:bard", intent_type="pass")
    if autofail:
        _update_combatant(live, "char:hero", conditions=[condition("blinded")])
    live.rng = CountingRandom(7)
    act(
        handle,
        "char:hero",
        intent_type="check",
        check=request(
            dc=dc,
            required_sense="sight" if autofail else "none",
            redeem_granted_die="feature_grant:bardic-inspiration",
        ),
    )
    event = events(live, CheckRolled)[-1]
    assert bool(event.inspiration_bonus) is spends
    assert [b for _, b, _ in live.rng.draws] == ([] if autofail else [20, 6] if spends else [20])
    assert bool(events(live, EffectExpired)) is spends
    assert bool(live.active_effects.get("char:hero")) is not spends


def test_canonical_check_and_live_share_the_core_with_semantics_and_reliable_talent():
    _, live = table(reliable_talent=True, skill_proficiencies=("perception",))
    actor = combatant(live)
    activity = CheckActivity(id="audit", check={"associated": ["prc"], "dc": {"formula": "15"}})
    emitted = []
    live.rng = CountingRandom(2)
    ctx = ActivityResolutionContext(
        rng=live.rng,
        caster=actor,
        targets=[],
        caster_abilities={},
        event_emitter=emitted.append,
        check_states=project_check_states(live),
    )
    resolve_check(activity, ctx)
    event = emitted[0]
    assert event.skill == "prc"
    assert event.natural == 10
    assert len(live.rng.draws) == 1
    assert event.activity_id == "audit"
    assert event.cost_owner == "activity"


@pytest.mark.parametrize(
    "class_slug,level,flag",
    [
        ("bard", 2, "jack_of_all_trades"),
        ("rogue", 7, "reliable_talent"),
        ("fighter", 1, "stealth_disadvantage"),
    ],
)
def test_derived_sheet_flags_reach_real_live_combat(class_slug, level, flag):
    build = make_build_spec(
        species_slug="human",
        size_choice=CreatureSize.MEDIUM,
        class_slug=class_slug,
        level=level,
        ability_scores={"dex": 14},
        equipment=("chain-mail",),
    )
    spec = build_party_member(
        build,
        CombatInstance(entity_id="char:hero", name="Hero", zone_id="0,0", initiative=20),
        loader=BundledAssetLoader(),
    )
    _, live = start([spec], seed=7)
    assert getattr(spec, flag) is True
    assert getattr(combatant(live), flag) is True


def test_same_state_typed_intents_and_seed_reproduce_bytes_and_rng():
    def replay():
        handle, live = table(
            stealth_disadvantage=True, reliable_talent=True, skill_proficiencies=("stealth",)
        )
        act(
            handle,
            "char:hero",
            intent_type="check",
            check=request(ability="dex", skill="stealth", advantage=("flag",)),
        )
        return (
            b"\n".join(e.model_dump_json().encode() for e in live.event_log),
            live.rng.getstate(),
            [c.model_dump_json() for c in live.initiative],
        )

    assert replay() == replay()


def test_canonical_item_check_preserves_its_activation_and_preflights_override(monkeypatch):
    original = BundledAssetLoader.get_item
    item = original(BundledAssetLoader(), "manacles")
    item = item.model_copy(update={"activities": item.activities[:1]})
    monkeypatch.setattr(
        BundledAssetLoader,
        "get_item",
        lambda self, slug: item if slug == "manacles" else original(self, slug),
    )
    handle, live = table(dexterity=14, skill_proficiencies=("sleight_of_hand",))
    bad = request(ability="dex", skill="sleight_of_hand", dc=99, context="activity")
    before = list(live.event_log)
    with pytest.raises(IntentRejectedError):
        act(handle, "char:hero", intent_type="use_item", item_id="manacles", check=bad)
    assert combatant(live).action_available
    assert live.event_log == before
    assert not live.rng.draws
    act(
        handle,
        "char:hero",
        intent_type="use_item",
        item_id="manacles",
        check=bad.model_copy(update={"dc": 13}),
    )
    event = events(live, CheckRolled)[-1]
    assert (event.cost_owner, event.modifier, event.skill) == ("activity", 4, "slt")
    assert not combatant(live).action_available
    assert len(live.rng.draws) == 1


def test_generic_check_cannot_ignore_exhausted_action_budget():
    handle, live = table()
    _update_combatant(live, "char:hero", action_available=False)
    with pytest.raises(IntentRejectedError, match="no_action_economy"):
        act(handle, "char:hero", intent_type="check", check=request())
    assert not events(live, CheckRolled)
    assert not live.rng.draws
    assert not combatant(live).action_available


def test_help_is_spent_on_matching_auto_failure_without_drawing():
    _, live = table()
    _update_combatant(live, "char:hero", conditions=[condition("blinded")])
    live.help_check_grants.append(HelpCheckGrant("char:helper", "char:hero", "perception"))
    event = resolve_live_check(live, request(skill="perception", required_sense="sight"))
    assert event.auto_failure == "sight"
    assert event.advantage_sources == ["help"]
    assert not live.rng.draws
    assert not live.help_check_grants


@pytest.mark.parametrize("in_sight,draws", [(True, 1), (False, 2)])
def test_frightened_help_reliable_talent_compose_with_projected_visibility(in_sight, draws):
    _, live = table(reliable_talent=True, skill_proficiencies=("perception",))
    actor = combatant(live).model_copy(update={"conditions": [condition("frightened")]})
    states = project_check_states(live)
    states[actor.entity_id] = CheckActorState(
        fear_source_in_sight=in_sight,
        help_grants=(HelpCheckGrant("char:helper", actor.entity_id, "perception"),),
    )
    live.rng = CountingRandom(2)
    ctx = ActivityResolutionContext(
        rng=live.rng,
        caster=actor,
        targets=[],
        caster_abilities={},
        event_emitter=lambda _: None,
        check_states=states,
    )
    event = resolve_check_request(request(skill="perception"), ctx)
    assert len(live.rng.draws) == draws
    assert event.natural == 10
    assert event.reliable_talent_applied


def test_typed_request_keeps_alternate_ability_for_proficient_skill():
    _, live = table(strength=18, wisdom=8, skill_proficiencies=("perception",))
    assert resolve_live_check(live, request(ability="str", skill="perception")).modifier == 6


def test_malformed_canonical_check_rejects_before_payment_or_draws(monkeypatch):
    original = BundledAssetLoader.get_item
    item = original(BundledAssetLoader(), "manacles")
    activity = CheckActivity(check={"ability": "nonsense"})
    item = item.model_copy(update={"activities": [activity]})
    monkeypatch.setattr(
        BundledAssetLoader,
        "get_item",
        lambda self, slug: item if slug == "manacles" else original(self, slug),
    )
    handle, live = table()
    before = list(live.event_log)
    with pytest.raises(IntentRejectedError):
        act(handle, "char:hero", intent_type="use_item", item_id="manacles")
    assert live.event_log == before
    assert combatant(live).action_available
    assert not live.rng.draws


@pytest.mark.parametrize(
    "formula,valid,dc",
    [
        ("8 + @abilities.int.mod + @prof", True, 13),
        ("8 + @mod + @prof", False, None),
        ("1d20", False, None),
        ("-1", False, None),
        ("8 + nonsense", False, None),
    ],
)
def test_canonical_dc_preflight_resolves_tokens_without_drawing(monkeypatch, formula, valid, dc):
    original = BundledAssetLoader.get_item
    item = original(BundledAssetLoader(), "manacles")
    activity = CheckActivity(check={"ability": "dex", "dc": {"formula": formula}})
    item = item.model_copy(update={"activities": [activity]})
    monkeypatch.setattr(
        BundledAssetLoader,
        "get_item",
        lambda self, slug: item if slug == "manacles" else original(self, slug),
    )
    handle, live = table(class_slug="wizard", intelligence=16)
    before = list(live.event_log)
    if valid:
        act(handle, "char:hero", intent_type="use_item", item_id="manacles")
        assert events(live, CheckRolled)[-1].dc == dc
        assert len(live.rng.draws) == 1
    else:
        with pytest.raises(IntentRejectedError):
            act(handle, "char:hero", intent_type="use_item", item_id="manacles")
        assert live.event_log == before
        assert combatant(live).action_available
        assert not live.rng.draws


def test_canonical_checks_sharing_context_cannot_reuse_consumed_help():
    _, live = table(skill_proficiencies=("perception",))
    live.help_check_grants.append(HelpCheckGrant("char:helper", "char:hero", "perception"))
    actor = combatant(live)
    ctx = ActivityResolutionContext(
        rng=live.rng,
        caster=actor,
        targets=[],
        caster_abilities={},
        event_emitter=lambda event: _emit(live, event),
        check_states=project_check_states(live),
    )
    activity = CheckActivity(check={"associated": ["prc"], "dc": {"formula": "15"}})
    resolve_check(activity, ctx)
    resolve_check(activity, ctx)
    assert [e.advantage for e in events(live, CheckRolled)] == ["advantage", "normal"]
    assert len(live.rng.draws) == 3
    assert not live.help_check_grants


def test_canonical_social_target_can_differ_from_rolling_activity_actor():
    _, live = table()
    live.active_effects["mon:foe"] = [
        ActiveEffect(name="Charm", id="charm", target_id="mon:foe", origin="cast:charm:char:hero")
    ]
    _update_combatant(live, "mon:foe", conditions=[condition("charmed", source_effect_id="charm")])
    actor = combatant(live)
    ctx = ActivityResolutionContext(
        rng=live.rng,
        caster=actor,
        targets=[],
        caster_abilities={},
        event_emitter=lambda event: _emit(live, event),
        check_states=project_check_states(live),
        check_request=request(
            ability="cha", context="activity", social_interaction=True, target_id="mon:foe"
        ),
    )
    resolve_check(CheckActivity(check={"ability": "cha", "dc": {"formula": "15"}}), ctx)
    assert events(live, CheckRolled)[-1].advantage_sources == ["charmed"]
    assert len(live.rng.draws) == 2
