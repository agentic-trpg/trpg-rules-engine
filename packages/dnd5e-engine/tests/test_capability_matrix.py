"""Keeps ``docs/capabilities.md`` honest.

The capability matrix publishes hard counts ("105 of 339 spells resolve to
nothing"). A published number that drifts is worse than no number, so the counts
are recomputed from the shipped corpus here and compared against what the page
claims. Change the behaviour, and this test tells you which sentence to update.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.condition import ConditionEffectKind

from dnd5e_engine.activities.conjuration import CONJURATION_ALLOWLIST
from dnd5e_engine.rules.conditions import _DECLARATIVE_CONDITION_MIGRATIONS

CAPABILITIES_MD = Path(__file__).resolve().parents[3] / "docs" / "capabilities.md"

#: Activity kinds the resolver actually turns into ``CombatEvent``s. A
#: ``utility`` activity counts only when it carries effect riders — mirrors
#: ``dnd5e_engine.activities.resolver.resolve_activity``; an allowlisted
#: conjuration (``_CONJURED_SPELLS``) resolves too.
MECHANICAL_KINDS = frozenset({"attack", "damage", "save", "heal", "check", "cast"})


def _resolves(activity: dict[str, Any]) -> bool:
    kind = activity.get("kind")
    if kind in MECHANICAL_KINDS:
        return True
    return kind == "utility" and bool(activity.get("effects"))


#: Spells whose ``summon`` / ``enchant`` activity the engine resolves (C21):
#: the conjuration allowlist's construct, enchant and summon entries — never
#: any kind wholesale. Polymorph's form rides its ``save`` activity, which
#: already counts, and Wild Shape is a feature.
_CONJURED_SPELLS = frozenset(
    slug
    for slug, kind in CONJURATION_ALLOWLIST.items()
    if kind in {"construct", "enchant", "summon"}
)


def _spell_resolves(spell: dict[str, Any]) -> bool:
    return spell.get("slug") in _CONJURED_SPELLS or any(
        _resolves(a) for a in spell.get("activities", [])
    )


@pytest.fixture(scope="module")
def canonical_dir() -> Path:
    import dnd5e_srd_data

    return Path(dnd5e_srd_data.__file__).parent / "canonical"


@pytest.fixture(scope="module")
def spell_stats(canonical_dir: Path) -> dict[str, int]:
    total = inert = inert_concentration = 0
    for path in sorted((canonical_dir / "spells").glob("*.json")):
        spell = json.loads(path.read_text())
        total += 1
        if not _spell_resolves(spell):
            inert += 1
            if spell.get("concentration"):
                inert_concentration += 1
    return {
        "total": total,
        "inert": inert,
        "resolving": total - inert,
        "inert_concentration": inert_concentration,
    }


@pytest.fixture(scope="module")
def matrix_text() -> str:
    assert CAPABILITIES_MD.is_file(), f"missing {CAPABILITIES_MD}"
    return CAPABILITIES_MD.read_text()


def test_published_spell_counts_match_the_corpus(
    spell_stats: dict[str, int], matrix_text: str
) -> None:
    for label, key in (
        ("Spells in the corpus", "total"),
        ("Resolve to at least one mechanical activity", "resolving"),
        ("Load but resolve to nothing", "inert"),
    ):
        match = re.search(rf"{re.escape(label)}.*?\*\*(\d+)\*\*", matrix_text)
        assert match, f"capabilities.md no longer publishes a count for {label!r}"
        assert int(match.group(1)) == spell_stats[key], (
            f"capabilities.md says {match.group(1)} for {label!r}, corpus says {spell_stats[key]}"
        )

    conc = re.search(r"of which are concentration spells.*?\*\*(\d+)\*\*", matrix_text)
    assert conc, "capabilities.md no longer publishes the inert-concentration count"
    assert int(conc.group(1)) == spell_stats["inert_concentration"]


def test_feature_audit_counts_separate_standalone_and_bound_attack_execution(
    matrix_text: str,
) -> None:
    from dnd5e_engine.feature_audit import audit_document

    audit = audit_document(BundledAssetLoader())
    for classification, phrase in (
        ("fully_resolvable", "standalone resolvable"),
        ("unsupported_preflight", "unsupported"),
        ("semantic_special_case", "semantic cases"),
        ("attack_rider_executable", "executable attack riders"),
        ("attack_rider_deferred", "deferred attack activities"),
    ):
        assert f"{audit['counts'][classification]} {phrase}" in matrix_text
    assert "An executable rider still rejects standalone `use_feature`" in matrix_text


def test_named_inert_concentration_spells_really_are_inert(canonical_dir: Path) -> None:
    """The page names specific staples as inert; verify each actually is."""
    for slug in (
        "blur",
        "darkness",
        "fog-cloud",
        "wall-of-force",
        "silent-image",
        "globe-of-invulnerability",
        "expeditious-retreat",
    ):
        spell = json.loads((canonical_dir / "spells" / f"{slug}.json").read_text())
        assert spell.get("concentration"), f"{slug} is no longer a concentration spell"
        assert not _spell_resolves(spell), (
            f"{slug} now resolves — remove it from the inert list in capabilities.md"
        )


def test_published_timing_counts_and_acceptance_sources_match_canonical(
    canonical_dir: Path, matrix_text: str
) -> None:
    from collections import Counter

    triggers = Counter(
        activity.get("timing", {}).get("trigger", "immediate")
        for path in sorted((canonical_dir / "spells").glob("*.json"))
        for activity in json.loads(path.read_text()).get("activities", [])
    )
    supported = triggers["turn_start"] + triggers["turn_end"]
    assert f"**{supported} supported timed activities**" in matrix_text
    assert f"**{triggers['manual']} manual activities**" in matrix_text
    assert triggers == {"immediate": 444, "turn_start": 3, "turn_end": 3, "manual": 14}
    for slug, trigger, recurring in (
        ("weird", "turn_end", True),
        ("vitriolic-sphere", "turn_end", False),
        ("stinking-cloud", "turn_start", True),
    ):
        timings = [a["timing"] for a in _canonical_spell(slug)["activities"] if "timing" in a]
        assert len(timings) == 1
        assert (timings[0]["trigger"], timings[0]["recurring"]) == (trigger, recurring)


def test_published_legendary_action_count_matches_the_corpus(
    canonical_dir: Path, matrix_text: str
) -> None:
    with_legendary = sum(
        1
        for path in (canonical_dir / "monsters").glob("*.json")
        if json.loads(path.read_text()).get("legendary_actions")
    )
    match = re.search(r"(\d+) monsters carry them in the data", matrix_text)
    assert match, "capabilities.md no longer publishes the legendary-action count"
    assert int(match.group(1)) == with_legendary


def test_legendary_actions_and_traits_are_consumed() -> None:
    """If these regress to unconsumed again, this test fails and the page must
    be updated (mirrors the intent of the probe this replaces: pin the gap by
    design so an implementation forces a docs update, just in the other
    direction now that C18 has consumed them)."""
    import dnd5e_engine.activities.monster_actions as monster_actions_module
    import dnd5e_engine.orchestrator as orchestrator_module

    orchestrator_source = Path(orchestrator_module.__file__).read_text()
    assert "legendary_actions_remaining" in orchestrator_source, (
        "legendary actions are no longer tracked by the engine — "
        "update docs/capabilities.md and BACKLOG.md, then update this test"
    )
    monster_actions_source = Path(monster_actions_module.__file__).read_text()
    assert "rank_monster_actions" in monster_actions_source, (
        "recharge/limited-use ranking is no longer in monster_actions.py — "
        "update docs/capabilities.md and BACKLOG.md, then update this test"
    )


# ── status-row probes ────────────────────────────────────────────────────────
#
# The counts above are pinned; the ✅/⚠️/❌ status rows were not, and drifted
# (see BACKLOG.md "Documentation drift"). Each probe below is a cheap,
# grep-level fact about the shipped source that the corresponding row claims.
# The test asserts the row and the probe agree in BOTH directions: a row that
# claims a capability the code lost fails, and so does a row still claiming a
# gap the code has since closed.


def _src(rel: str) -> str:
    """Source text of an engine module, by path relative to the package root."""
    import dnd5e_engine

    return (Path(dnd5e_engine.__file__).parent / rel).read_text()


def _event_class_body(name: str) -> str:
    """The body of one ``events.py`` event class, up to the next ``class``."""
    source = _src("events.py")
    head = source.split(f"\nclass {name}(", 1)
    assert len(head) == 2, f"events.py no longer defines {name}"
    return head[1].split("\nclass ", 1)[0]


def _canonical_spell(slug: str) -> dict[str, Any]:
    """One shipped spell's canonical JSON."""
    import dnd5e_srd_data

    path = Path(dnd5e_srd_data.__file__).parent / "canonical" / "spells" / f"{slug}.json"
    return dict(json.loads(path.read_text()))


#: row substring → (probe over the shipped source, substring the row must carry
#: iff the probe is True).
def _monster_action_economy_resolves() -> bool:
    """Probe canonical resource identity and conditional/alternative planning."""
    from dnd5e_engine.activities.monster_actions import (
        action_resources_available,
        plan_monster_action,
    )
    from dnd5e_engine.orchestrator import _hydrate_monster_action_uses

    loader = BundledAssetLoader()
    doppelganger = loader.get_monster("doppelganger")
    djinni = loader.get_monster("djinni")
    vrock = loader.get_monster("vrock")
    assert doppelganger is not None
    assert djinni is not None
    assert vrock is not None
    uses = _hydrate_monster_action_uses(doppelganger)
    multiattack = next(a for a in doppelganger.actions if a.slug == "multiattack")
    before = plan_monster_action(doppelganger, multiattack)
    uses["unsettling-visage"].recharge_spent = True
    after = plan_monster_action(
        doppelganger,
        multiattack,
        is_available=lambda a: action_resources_available(uses.get(a.slug)),
    )
    storm = plan_monster_action(djinni, next(a for a in djinni.actions if a.slug == "multiattack"))
    return (
        [s.source_action.slug for s in before.executions] == ["slam", "slam", "unsettling-visage"]
        and [s.source_action.slug for s in after.executions] == ["slam", "slam"]
        and len(storm.executions) == 3
        and {s.source_action.slug for s in storm.executions} <= {"storm-blade", "storm-bolt"}
        and _hydrate_monster_action_uses(vrock)["stunning-screech"].action_uses_remaining == 1
    )


def _monster_magnitudes_resolve() -> bool:
    """Probe the ordinary template carrier and its shared formula resolver."""
    import random
    from types import SimpleNamespace

    from dnd5e_engine import EncounterMemberSpec
    from dnd5e_engine.activities.context import ActivityResolutionContext
    from dnd5e_engine.activities.formula import resolve_roll_data
    from dnd5e_engine.orchestrator import _build_foe_combatants, _stat_block_magnitudes_of

    spec = EncounterMemberSpec(
        entity_id="mon:probe",
        entity_type="Monster",
        name="Crocodile",
        initiative=1,
        hp_current=22,
        hp_max=22,
        zone_id="0,0",
        monster_template_slug="crocodile",
        dexterity=10,
    )
    combatants = []
    _build_foe_combatants([spec], combatants, {}, {}, {}, {})
    actor = combatants[0]
    live = SimpleNamespace(
        transforms={}, summons={}, monster_slug_by_entity={actor.entity_id: "crocodile"}
    )
    magnitudes = _stat_block_magnitudes_of(live, actor)
    if magnitudes is None:
        return False
    ctx = ActivityResolutionContext(
        rng=random.Random(1),
        caster=actor,
        targets=[],
        event_emitter=lambda _: None,
        caster_abilities=dict(magnitudes.ability_scores),
        caster_proficiency_bonus=magnitudes.proficiency_bonus,
        stat_block_magnitudes=magnitudes,
    )
    aboleth = BundledAssetLoader().get_monster("aboleth")
    return (
        actor.attack_bonus is None
        and actor.dexterity == 10
        and resolve_roll_data("@mod + @prof", ctx, ability="str") == "2 + 2"
        and resolve_roll_data("@skills.ath.passive", ctx) == "12"
        and aboleth is not None
        and aboleth.initiative_modifier == 7
    )


def _attack_rider_options_resolve(
    feature_slug: str,
    activity_ids: tuple[str, ...],
    *,
    class_slug: str,
    level: int,
    flurry: bool = False,
    carried_items: tuple[str, ...] = (),
    option_ids: tuple[str, ...] = (),
    automatic: bool = False,
    subclass_slug: str | None = None,
) -> bool:
    """Exercise the typed, draw-free planner with real class/subclass grants."""
    from dnd5e_srd_data.schema.monster import CreatureSize

    from dnd5e_engine.activities.build_context import build_activity_context
    from dnd5e_engine.activities.scale import build_scale_values
    from dnd5e_engine.attack_riders import (
        AttackRiderRequest,
        automatic_attack_riders,
        plan_attack_riders,
    )
    from dnd5e_engine.feature_runtime import DrawFreeRandom, FeaturePreflightError
    from dnd5e_engine.types.combat import Combatant

    loader = BundledAssetLoader()
    actor = Combatant(
        entity_id="char:probe",
        entity_type="Character",
        name="Probe",
        initiative=20,
        hp_current=100,
        class_slug=class_slug,
        character_level=level,
        subclass_slug="hand" if flurry else subclass_slug,
        dexterity=18,
        wisdom=18,
        carried_item_slugs=carried_items,
    )
    weapon = loader.get_weapon("dagger" if class_slug == "rogue" else "quarterstaff")
    assert weapon is not None
    if flurry:
        weapon = weapon.model_copy(update={"slug": "unarmed-strike"})
    ctx = build_activity_context(
        actor,
        [],
        rng=DrawFreeRandom(0),
        slot_level=None,
        base_spell_level=None,
        concentration=False,
        event_emitter=lambda _: None,
        spellcasting_ability=None,
        source_passive_effects=[],
        spell_book={},
        passive_damage_modifiers={},
        save_modifiers={},
        class_levels={class_slug: level},
        scale_values=build_scale_values(
            class_slug=class_slug,
            subclass_slug=actor.subclass_slug,
            species_slug=None,
            level=level,
            loader=loader,
        ),
        is_feature_invocation=True,
    )
    feature = loader.get_feature(feature_slug)
    assert feature is not None
    for activity_id in activity_ids:
        semantics = feature.attack_riders.get(activity_id)
        if semantics is None:
            return False
        request = AttackRiderRequest(
            feature_id=feature_slug,
            activity_id=activity_id,
            push_distance_ft=15 if semantics.forced_movement else None,
            option_ids=option_ids or semantics.option_ids[:1],
        )
        try:
            plans = (
                automatic_attack_riders(actor, ctx, loader)
                if automatic
                else plan_attack_riders(
                    actor,
                    (request,),
                    weapon=weapon,
                    origin="flurry" if flurry else "action",
                    ctx=ctx,
                    loader=loader,
                    spent={},
                    used_features=set(),
                    cell_size_ft=5,
                    target_size=CreatureSize.MEDIUM,
                )
            )
        except FeaturePreflightError:
            return False
        if not any(plan.activity.id == activity_id for plan in plans):
            return False
    return (
        re.search(r"\bprepare_intent_riders\(\s*live,", _src("orchestrator.py")) is not None
        and re.search(r"\battach_attack_riders\(\s*live,", _src("orchestrator.py")) is not None
        and "resolve_activity(" in _src("live_attack_riders.py")
    )


def _cunning_rider_scope_matches() -> bool:
    return (
        _attack_rider_options_resolve(
            "devious-strikes",
            ("ki4lIPVGNA0HjEzH",),
            class_slug="rogue",
            level=14,
        )
        and _attack_rider_options_resolve(
            "cunning-strike", ("dWcCw1vTWRMx4YzD",), class_slug="rogue", level=14
        )
        and _attack_rider_options_resolve(
            "cunning-strike",
            ("n64fvJMT9fPUy7DH",),
            class_slug="rogue",
            level=14,
            carried_items=("poisoners-kit",),
        )
        and not _attack_rider_options_resolve(
            "cunning-strike", ("n64fvJMT9fPUy7DH",), class_slug="rogue", level=14
        )
        and _attack_rider_options_resolve(
            "devious-strikes", ("3eq7lcmpkJJBU2KO",), class_slug="rogue", level=14
        )
        and _attack_rider_options_resolve(
            "cunning-strike", ("m2bRZ1YeD3yf9nV7",), class_slug="rogue", level=14
        )
        and not _attack_rider_options_resolve(
            "devious-strikes", ("4TnBjQTJzt9UjUos",), class_slug="rogue", level=14
        )
    )


def _brutal_rider_scope_matches() -> bool:
    loader = BundledAssetLoader()
    reckless = loader.get_feature("reckless-attack")
    return (
        reckless is not None
        and reckless.attack_rider_context is not None
        and reckless.attack_rider_context.deferred_reason is None
        and reckless.attack_rider_context.declaration == "reckless_attack"
        and reckless.attack_rider_context.effects[0].lifecycle.expiry_boundary
        == "source_next_turn_start"
        and all(
            _attack_rider_options_resolve(slug, (activity,), class_slug="barbarian", level=17)
            for slug, activity in (
                ("brutal-strike", "nN5gsB6AcSQ4uQPN"),
                ("improved-brutal-strike", "UmRlsf4QWW98I4FS"),
                ("improved-brutal-strike", "I30qGlPDcyKwz65H"),
            )
        )
        and _attack_rider_options_resolve(
            "brutal-strike",
            ("nN5gsB6AcSQ4uQPN",),
            class_slug="barbarian",
            level=17,
            option_ids=("staggering-blow", "sundering-blow"),
        )
        and not _attack_rider_options_resolve(
            "brutal-strike",
            ("nN5gsB6AcSQ4uQPN",),
            class_slug="barbarian",
            level=13,
            option_ids=("staggering-blow", "sundering-blow"),
        )
        and "observe_attack_roll(live," in _src("orchestrator.py")
        and "attack_pre_roll=" in _src("live_attack_riders.py")
    )


def _frenzy_contract_resolves() -> bool:
    feature = BundledAssetLoader().get_feature("frenzy")
    if feature is None:
        return False
    rider = feature.attack_riders["myPBq8xozti108Mc"]
    return (
        rider.automatic
        and rider.once_per_turn
        and rider.own_turn
        and rider.requires_rage
        and rider.requires_reckless
        and rider.inherit_damage_type
        and not rider.pre_roll_commit
        and _attack_rider_options_resolve(
            "frenzy",
            ("myPBq8xozti108Mc",),
            class_slug="barbarian",
            level=17,
            automatic=True,
            subclass_slug="berserker",
        )
        and "damage_contributions=" in _src("live_attack_riders.py")
    )


def _scoped_movement_contract_resolves() -> bool:
    from dnd5e_engine.live_movement import execute_movement_grant, preflight_movement_grant
    from dnd5e_engine.movement import MovementChoice

    loader = BundledAssetLoader()
    withdraw = loader.get_feature("cunning-strike").attack_riders["m2bRZ1YeD3yf9nV7"]
    forceful = loader.get_feature("brutal-strike").attack_rider_options["forceful-blow"]
    return (
        withdraw.movement_grant.direction == "any"
        and forceful.movement_grant.direction == "straight_toward_target"
        and callable(execute_movement_grant)
        and callable(preflight_movement_grant)
        and MovementChoice(destination_cell=" 1, 2 ").destination_cell == "1,2"
        and "grant_remaining_ft=remaining" in _src("live_movement.py")
        and "execute_movement_grant(" in _src("live_attack_riders.py")
    )


def _physical_movement_contract_resolves() -> bool:
    from dnd5e_srd_data.schema.monster import CreatureSize

    from dnd5e_engine.movement import MovementLedger, can_enter_creature_space, step_cost

    ledger = MovementLedger().spend(cost_ft=10, distance_ft=5, mode="crawl", effective_speed=30)
    return (
        ledger.remaining(40) == 30
        and ledger.add_dash().remaining(40) == 70
        and step_cost(5, mode="crawl", difficult_terrain=True) == 15
        and can_enter_creature_space(CreatureSize.MEDIUM, CreatureSize.HUGE)
        and not can_enter_creature_space(CreatureSize.MEDIUM, CreatureSize.LARGE)
        and "live_movement.handle_move(live," in _src("orchestrator.py")
        and "live_movement.take_step(live," in _src("orchestrator.py")
    )


def _typed_lifecycle_contract_resolves() -> bool:
    from dnd5e_srd_data.schema.lifecycle import EffectLifecycleSpec, RepeatSaveSpec

    from dnd5e_engine.effect_lifecycle import EffectLifecycleApplication, OngoingEffectLifecycle

    application = EffectLifecycleApplication(
        spec=EffectLifecycleSpec(repeat_save=RepeatSaveSpec(), maximum_rounds=10),
        source_id="char:source",
        source_kind="feature",
        source_slug="canonical-source",
        activity_id="canonical-activity",
        save_ability="con",
        save_dc=15,
    )
    state = OngoingEffectLifecycle.from_application(
        ("mon:target", "effect:canonical", "feature:source"),
        application,
        7,
        2,
    )
    return (
        state.repeats_at("mon:target", 7)
        and not state.repeated(7).repeats_at("mon:target", 7)
        and not state.expires_at("mon:target", "end", 8, 11)
        and state.expires_at("mon:target", "end", 8, 12)
        and "roll_save(ctx, target," in _src("live_effect_lifecycle.py")
        and "register_effect(live, eff)" in _src("orchestrator.py")
        and "damage_instance_completed(live, damage)" in _src("live_reactions.py")
        and "consume_next_save_modifier" in _src("activities/save_primitive.py")
    )


def _intimidating_presence_scope_matches() -> bool:
    from dnd5e_engine.feature_runtime import FeaturePreflightError, feature_operation

    feature = BundledAssetLoader().get_feature("intimidating-presence")
    assert feature is not None
    initial, recharge = feature.activities
    spec = initial.effects[0].lifecycle
    try:
        feature_operation(feature, recharge)
    except FeaturePreflightError:
        recharge_deferred = True
    else:
        recharge_deferred = False
    return (
        feature_operation(feature, initial) == "activity"
        and spec is not None
        and spec.maximum_rounds == 10
        and spec.repeat_save is not None
        and feature.uses is not None
        and feature.uses.max == "1"
        and recharge_deferred
    )


def _damage_expiry_contract_resolves() -> bool:
    feature = BundledAssetLoader().get_feature("devious-strikes")
    assert feature is not None
    semantics = feature.attack_riders["3eq7lcmpkJJBU2KO"]
    return (
        any(
            binding.lifecycle is not None and binding.lifecycle.expire_on_positive_damage
            for binding in semantics.effects
        )
        and "damage_instance_completed(live, damage)" in _src("live_reactions.py")
        and "if amount > 0:" in _src("live_effect_lifecycle.py")
        and 'expire_effect(live, identity, "damaged")' in _src("live_effect_lifecycle.py")
    )


def _magic_resistance_repeat_contract_resolves() -> bool:
    from random import Random

    from dnd5e_srd_data.schema.monster import MonsterTraitMechanic

    from dnd5e_engine.activities.context import ActivityResolutionContext
    from dnd5e_engine.activities.save_primitive import roll_save
    from dnd5e_engine.types.combat import Combatant

    target = Combatant(
        entity_id="mon:probe",
        entity_type="Monster",
        name="Probe",
        initiative=1,
        hp_current=10,
        hp_max=10,
        trait_mechanics=[MonsterTraitMechanic.MAGIC_RESISTANCE],
    )
    ctx = ActivityResolutionContext(
        rng=Random(7),
        caster=target,
        targets=[target],
        event_emitter=lambda _: None,
        caster_abilities={},
        base_spell_level=None,
        save_is_magical=True,
    )
    result = roll_save(ctx, target, "wis", 15)
    return (
        result.mode == "advantage"
        and result.sources == ("trait",)
        and "is_magical=app.is_magical" in _src("live_effect_lifecycle.py")
        and "save_is_magical=is_magical" in _src("live_effect_lifecycle.py")
    )


_PROBES: dict[str, tuple[Any, str]] = {
    "Ability & skill checks": (
        lambda: (
            "class CheckRequest(" in _src("types/checks.py")
            and "resolve_check_request(" in _src("activities/check.py")
            and "resolve_live_check(live" in _src("orchestrator.py")
            and "roll_d20_test(" in _src("activities/check_pipeline.py")
        ),
        "✅ Resolved",
    ),
    "Persistent area lifecycle": (
        lambda: (
            "class PersistentArea:" in _src("persistent_areas.py")
            and "resolve_activity(area.activity, ctx)" in _src("persistent_areas.py")
            and "after_movement_step(live, actor_id, cell)" in _src("live_movement.py")
            and "register_area_hooks(live)" in _src("orchestrator.py")
        ),
        "✅ Resolved",
    ),
    "Spell timed activity lifecycle": (
        lambda: (
            "class PendingTimedActivity" in _src("timed_activities.py")
            and "resolve_activity(pending.activity, ctx)" in _src("timed_activities.py")
            and "register_timed_activity_hooks(live)" in _src("orchestrator.py")
        ),
        "⚠️ Partial",
    ),
    # F1c/F1d: every save path adds a real ability + proficiency modifier.
    "Saving throws, half-on-save": (
        lambda: (
            "roll_live_save(live, concentrator," in _src("orchestrator.py")
            and "roll_live_save(live, target," in _src("orchestrator.py")
            and "roll_save(save_ctx, target," in _src("activities/apply.py")
            and "passive_save_bonus" in _src("activities/save_primitive.py")
        ),
        "✅",
    ),
    # F2b: activity attacks build typed AdvantageSources instead of the old
    # hard-coded ``mode: AdvantageMode = "normal"`` parameter.
    "Attack rolls, crits": (
        lambda: 'mode: AdvantageMode = "normal"' not in _src("activities/attack.py"),
        "Advantage/disadvantage is rolled on",
    ),
    # C14 Task 3: Dodge sets a live ``dodging`` flag consumed by the attack
    # and save resolvers; the intent branch owns this exact literal.
    "| Dodge |": (
        lambda: 'if intent.intent_type == "dodge":' in _src("orchestrator.py"),
        "✅",
    ),
    # C16b: Dodge's "if you can see the attacker" conjunct is applied at every
    # attack context build site (``_combatant_can_see(live, t, current)``);
    # since C24 an opportunity attack resolves through those sites too.
    'if you can see the attacker" is now enforced (C16b': (
        lambda: "_combatant_can_see(live, t, current)" in _src("orchestrator.py"),
        'if you can see the attacker" is now enforced (C16b',
    ),
    # C14 Task 4: Help (assist-an-attack-roll flavor) has a live handler —
    # the intent branch owns this exact literal.
    "| Help |": (
        lambda: 'if intent.intent_type == "help":' in _src("orchestrator.py"),
        "✅",
    ),
    # Closed (C14 Task 5): Hide has a dispatch handler.
    "| Hide |": (
        lambda: (
            'if intent.intent_type == "hide"' in _src("orchestrator.py")
            and '_require_cunning_action(current, "Hide")' in _src("orchestrator.py")
            and '_action_payment(current, "hide")' in _src("orchestrator.py")
        ),
        "✅",
    ),
    # C16b (plan ruling R1): Hide's "out of any enemy's line of sight"
    # conjunct scans hostiles via the composite predicate, skipped only when
    # the hider's own cell already carries Three-Quarters/Total cover.
    "out of every living, non-Incapacitated hostile's line of sight (C16b": (
        lambda: "_combatant_can_see(live, hostile, current)" in _src("orchestrator.py"),
        "out of every living, non-Incapacitated hostile's line of sight (C16b",
    ),
    # C16b (Hide Task 4): a dark cell satisfies "Heavily Obscured" via the
    # new ``SpatialTopology.light_on_cell`` seam.
    "`GridTopology.light_on_cell`": (
        lambda: "def light_on_cell(" in _src("spatial.py"),
        "`GridTopology.light_on_cell`",
    ),
    # F2c/C13: the damage-triggered concentration save emits the dedicated
    # event; row text no longer quotes the event name, so this probe now
    # pins the row's status instead.
    "Concentration, incl. damage-triggered saves": (
        lambda: "ConcentrationCheck(" in _src("orchestrator.py"),
        "✅",
    ),
    # Sense/social check clauses are live; held-item/crawling rules still defer.
    "Conditions (the 15 SRD conditions)": (
        lambda: (
            '"actor_incapacitated"' in _src("orchestrator.py")
            and ConditionEffectKind.DROPS_HELD_ITEMS
            not in _DECLARATIVE_CONDITION_MIGRATIONS["unconscious"]
            and ConditionEffectKind.RESTRICTED_MOVEMENT_CRAWL
            not in _DECLARATIVE_CONDITION_MIGRATIONS["prone"]
        ),
        "⚠️ Partial",
    ),
    # Frightened attack/check disadvantage uses LOS; approach is independent.
    "now gated on line of sight to a known, living, tracked fear source (C16b": (
        lambda: (
            "_fear_source_in_sight(" in _src("orchestrator.py")
            and '"frightened",' in _event_class_body("MoveFailed")
        ),
        "now gated on line of sight to a known, living, tracked fear source (C16b",
    ),
    "Frightened ability-check disadvantage consumes its canonical fear-source sight gate": (
        lambda: (
            "flags.disadvantage.check.gate.fear_source_in_sight" in _src("rules/effects.py")
            and "fear_source_in_sight=_fear_source_in_sight(live, c)" in _src("orchestrator.py")
            and ConditionEffectKind.DISADVANTAGE_ABILITY_CHECKS
            in _DECLARATIVE_CONDITION_MIGRATIONS["frightened"]
        ),
        "Frightened ability-check disadvantage consumes its canonical fear-source sight gate",
    ),
    # C12: the SRD 5.2 exhaustion penalty is a real projection, not prose.
    "| Exhaustion |": (
        lambda: "def d20_test_penalty(" in _src("rules/conditions.py"),
        "✅",
    ),
    # C12: massive damage kills outright rather than only decorating the event.
    "Instant death (massive damage)": (
        lambda: '"instant_kill"' in _src("orchestrator.py"),
        "✅",
    ),
    # F2c: the d20 breakdown is carried on the roll events.
    "carry the roll breakdown": (
        lambda: "natural:" in _event_class_body("AttackRolled"),
        "`natural`",
    ),
    # Live movement consumes weighted paths; the pure BFS remains compatible.
    "Multi-cell movement in one intent": (
        lambda: (
            '"unreachable"' in _src("live_movement.py")
            and "lowest_cost_path(" in _src("live_movement.py")
        ),
        "✅",
    ),
    "Unified spell/area delivery": (
        lambda: (
            "class SpellDeliverySpec(" in _src("spell_delivery.py")
            and "def preflight_delivery(" in _src("live_spell_delivery.py")
            and "def execute_spell_delivery(" in _src("live_spell_delivery.py")
            and "ctx.spell_dispatch(spell, child_ctx)" in _src("activities/cast.py")
        ),
        "✅",
    ),
    "Dust of Sneezing and Choking": (
        lambda: (
            BundledAssetLoader()
            .get_item("dust-of-sneezing-and-choking")
            .activities[0]
            .target.area_semantics.includes_origin
            and len(
                BundledAssetLoader()
                .get_item("dust-of-sneezing-and-choking")
                .activities[0]
                .target.creature_filter.auto_success_creature_types
            )
            == 5
            and "target_auto_success_ids" in _src("activities/save_primitive.py")
        ),
        "⚠️ Partial",
    ),
    # C16/C26: shared geometry, including monster actions and casts.
    "AoE templates (sphere / cone / line / cube / cylinder / emanation)": (
        lambda: (
            "cells_in_template(" in _src("areas.py")
            and "AreaTargeted(" in _src("live_spell_delivery.py")
            and "excluded_target_ids" in _src("orchestrator.py")
        ),
        "✅",
    ),
    "Monster area execution and aiming": (
        lambda: (
            "def _monster_area_placement(" in _src("orchestrator.py")
            and "_monster_area_placement(live, actor, spell.activities, spell=spell)"
            in _src("orchestrator.py")
            and "_monster_area_placement(live, actor, [activity])" in _src("orchestrator.py")
            and "MONSTER_AREA_DIRECTIONS" in _src("live_monster_delivery.py")
            and "_mark_monster_action_used(live, actor, action, resolved)"
            in _src("orchestrator.py")
        ),
        "✅",
    ),
    # C16: the forced-movement primitive emits CombatantMoved.
    "Forced movement (push)": (
        lambda: (
            "live_movement.push(live," in _src("orchestrator.py")
            and "CombatantMoved(" in _src("live_movement.py")
        ),
        "✅",
    ),
    # C16b: the visibility predicate feeds the attack resolver.
    "Vision and light (darkness": (
        lambda: (
            "can_see(" in _src("orchestrator.py") and '"unseen"' in _src("activities/attack.py")
        ),
        "⚠️ Partial",
    ),
    # C16b (plan ruling R4): the composite predicate folding Blinded/
    # Invisible/blindsight/truesight on top of the scene vision model.
    "composite `_combatant_can_see` predicate": (
        lambda: "def _combatant_can_see(" in _src("orchestrator.py"),
        "composite `_combatant_can_see` predicate",
    ),
    "Template monster senses": (
        lambda: (
            '"senses" not in foe.model_fields_set' in _src("orchestrator.py")
            and "senses: CombatantSenses" in _src("specs.py")
            and all(
                f"{sense}=monster.senses.{sense}" in _src("orchestrator.py")
                for sense in ("darkvision", "blindsight", "tremorsense", "truesight")
            )
        ),
        "canonical Darkvision, Blindsight, Tremorsense and Truesight",
    ),
    # C22: Magic Resistance is read from the hydrated trait list.
    "`special_abilities`": (
        lambda: (
            "trait_mechanics" in _src("orchestrator.py")
            and "MAGIC_RESISTANCE" in _src("activities/save_primitive.py")
        ),
        "Magic Resistance",
    ),
    # C22: magical damage bypasses nonmagical-only B/P/S resistance.
    "resistances/immunities/vulnerabilities": (
        lambda: "physical_resistances_nonmagical_only" in _src("activities/apply.py"),
        "overcome resistance",
    ),
    # C22: Sacred Flame's save carve-out is honoured.
    "Cover (half / three-quarters / total)": (
        lambda: "ignore_cover" in _src("activities/save_primitive.py"),
        "ignore_cover",
    ),
    # C13: the voluntary drop intent and its orchestrator call site are the
    # cheapest witnesses that the concentration lifecycle (one-at-a-time,
    # death/Incapacitated drop, timed expiry) is wired up end to end.
    "and cascade drop": (
        lambda: (
            '"drop_concentration",' in _src("events.py")
            and "_drop_concentration(live, event.target_id)" in _src("orchestrator.py")
        ),
        "✅",
    ),
    # C14 Task 1/2: Extra Attack's per-Action counter and the Light-property
    # off-hand Bonus Action window are both live.
    "Action economy": (
        lambda: (
            "_attacks_per_action(" in _src("orchestrator.py")
            and "_twf_window_open(" in _src("orchestrator.py")
            and "def _turn_can_continue(" in _src("orchestrator.py")
            and "def _attack_input_failure(" in _src("orchestrator.py")
            and "loading_weapon_fired_this_action" in _src("types/combat.py")
        ),
        "Character Action Economy",
    ),
    "restricted non-Magic Action": (
        lambda: "def _action_payment(" in _src("orchestrator.py"),
        "in either order",
    ),
    "Refused casts preserve action budgets": (
        lambda: (
            "def _apply_pre_slot_cast_gates(" in _src("orchestrator.py")
            and "def _spell_slot_unavailable(" in _src("orchestrator.py")
        ),
        "Refused casts preserve action budgets",
    ),
    "Countered casts spend their casting-time": (
        lambda: "spell_cast_opportunity(live," in _src("orchestrator.py"),
        "preserve the spell slot",
    ),
    "feature/item Magic-action classification": (
        lambda: "_MAGIC_ACTION_INTENTS" in _src("orchestrator.py"),
        "Still partial",
    ),
    # C14 Task 6/7: Grapple/Shove resolve via the shared Unarmed Strike save.
    "Grapple / Shove": (
        lambda: "_roll_unarmed_option_save(" in _src("orchestrator.py"),
        "⚠️ Partial",
    ),
    # C14 Task 2: the Light-property off-hand Bonus Action window.
    "Two-weapon fighting": (
        lambda: "_twf_window_open(" in _src("orchestrator.py"),
        "✅",
    ),
    # C14 Task 8: Surprise imposes Disadvantage on the engine-rolled Initiative.
    "| Surprise |": (
        lambda: "spec.is_surprised" in _src("orchestrator.py"),
        "✅",
    ),
    # C14 Task 8: initiative=None draws an engine d20 + DEX modifier roll.
    "Initiative order, rounds, turns": (
        lambda: "def _resolve_initiative(" in _src("orchestrator.py"),
        "✅",
    ),
    # C14 Task 8: a seeded incapacitated-implying status also imposes
    # Disadvantage on the engine-rolled Initiative roll.
    "Incapacitated's initiative disadvantage": (
        lambda: "_seeded_initiative_sources" in _src("orchestrator.py"),
        "now consume explicitly opted-in canonical clauses",
    ),
    # C24: an opportunity attack resolves through the activity context,
    # flagged on its context.
    "| Opportunity attacks |": (
        lambda: "def _opportunity_attack_of(" in _src("orchestrator.py"),
        "✅",
    ),
    # C15 Tasks 2/3: the long-range disadvantage tier and the Ranged
    # Attacks in Close Combat gate both append their own AdvantageSource
    # (previously the row called both "still inert").
    "long-range disadvantage": (
        lambda: (
            '"range:long"' in _src("activities/attack.py")
            and '"ranged_in_melee"' in _src("activities/attack.py")
        ),
        "long-range disadvantage",
    ),
    # C15 Task 3: the Heavy-property Strength gate.
    "Heavy weapon (raw Strength": (
        lambda: "def _weapon_heavy_disadvantage(" in _src("activities/attack.py"),
        "Heavy weapon (raw Strength",
    ),
    # C15 Task 4: DamageApplied gains source_id / is_crit for weapon damage.
    "DamageApplied` now carries `source_id`": (
        lambda: (
            "source_id: str | None = None" in _event_class_body("DamageApplied")
            and "is_crit: bool = False" in _event_class_body("DamageApplied")
        ),
        "DamageApplied` now carries `source_id`",
    ),
    # C15 Task 7: Push weapon mastery wired into the same forced-movement
    # primitive as Thunderwave / Shove.
    "Push mastery retains its full 10-ft distance": (
        lambda: 'elif mastery_slug == "push":' in _src("orchestrator.py"),
        "Push mastery retains its full 10-ft distance",
    ),
    # C15 Task 6: Topple's prone rider is gated by the shared
    # is_condition_immune helper (previously an ungated emit site).
    "Weapon-mastery Topple honors condition immunity": (
        lambda: 'is_condition_immune(target, "prone")' in _src("activities/mastery.py"),
        "Weapon-mastery Topple honors condition immunity",
    ),
    # C17: Long Rest reduces Exhaustion by 1, floored at 0, via an additive
    # kwarg on ``resolve_long_rest``.
    "Long Rest reduces the level by 1": (
        lambda: "exhaustion_level" in _src("rest.py"),
        "Long Rest reduces the level by 1",
    ),
    # Typed reaction eligibility validates before fire_reaction removes the
    # selected declaration, so an ineligible candidate stays queued.
    "slot-gated at the readied level and 60 ft range/LoS-gated at drain time": (
        lambda: (
            "def _condition_eligible(" in _src("live_reactions.py")
            and "def _eligible(" in _src("live_reactions.py")
            and "matching_conditions(pending, opportunity)" in _src("live_reactions.py")
        ),
        "slot-gated at the readied level and 60 ft range/LoS-gated at drain time",
    ),
    # Every supported readied spell uses the same slot gate.
    "an unexpended slot at its readied level": (
        lambda: (
            "orch._slot_available(live, reactor.entity_id, pending.slot_level)"
            in _src("live_reactions.py")
        ),
        "an unexpended slot at its readied level",
    ),
    # C17 Task 1: per-class/multiclass/Pact slot tables are derived
    # engine-side rather than accepted as a host-precomputed flat dict.
    "derive_spell_slots`, `derive_multiclass_slots`, `derive_pact_slots": (
        lambda: "def derive_multiclass_slots(" in _src("build_spec.py"),
        "derive_spell_slots`, `derive_multiclass_slots`, `derive_pact_slots",
    ),
    # C17 Task 1: the multiclass carrier itself (``classes: dict[str, int]``)
    # is the field this row names — distinct from the derivation function
    # probed above.
    "`CharacterBuildSpec.classes` carrier": (
        lambda: "classes: dict[str, int]" in _src("build_spec.py"),
        "`CharacterBuildSpec.classes` carrier",
    ),
    # C17 Task 6 (R8): a Ritual-tagged spell resolves out-of-combat only,
    # through the pure ``resolve_ritual_cast`` host seam; an in-combat
    # ``as_ritual`` cast is rejected before any slot logic.
    "Out-of-combat via `resolve_ritual_cast`": (
        lambda: "def resolve_ritual_cast(" in _src("spellcasting.py"),
        "Out-of-combat via `resolve_ritual_cast`",
    ),
    # C17 Task 6: component/material metadata now rides on every PC-path
    # cast via the ``SpellCast`` event, but nothing gates on it.
    "Metadata on `SpellCast`, not enforced": (
        lambda: "class SpellCast(" in _src("events.py"),
        "Metadata on `SpellCast`, not enforced",
    ),
    # C15: all eight 2024 weapon masteries are live. F6 — this used to be a
    # bare substring grep for each mastery slug over mastery.py, which
    # passed on COMMENT text alone: cleave and nick are documented there
    # ("resolved elsewhere entirely") but never dispatched from that file,
    # so the probe couldn't fail even if either mastery regressed. Probe
    # each mastery's actual dispatch/resolution site instead: graze/topple
    # resolve in mastery.py itself; vex/sap/slow/push report through the
    # ``ctx.mastery_procs`` writeback (their proc-append call sites);
    # cleave's chained attack lives in attack.py; Nick is pure action
    # economy, gated in orchestrator.py's off-hand consume helper.
    "All eight (C15)": (
        lambda: (
            "_resolve_graze(" in _src("activities/mastery.py")
            and "_resolve_topple(" in _src("activities/mastery.py")
            and "ctx.mastery_procs.append((_VEX" in _src("activities/mastery.py")
            and "ctx.mastery_procs.append((_SAP" in _src("activities/mastery.py")
            and "ctx.mastery_procs.append((_SLOW" in _src("activities/mastery.py")
            and "ctx.mastery_procs.append((_PUSH" in _src("activities/mastery.py")
            and "_resolve_cleave_chain(" in _src("activities/attack.py")
            and 'weapon.mastery == "nick"' in _src("orchestrator.py")
        ),
        "All eight (C15)",
    ),
    # C19: engine-side character derivation.
    "HP, AC, hit dice, skill/save proficiencies": (
        lambda: "def derive_sheet(" in _src("build_spec.py"),
        "✅ Resolved",
    ),
    "| Background |": (
        lambda: "background_slug: str | None" in _src("build_spec.py"),
        "⚠️ Partial",
    ),
    "Class, subclass, level 1–20, species": (
        lambda: "def subclass_gate_level(" in _src("rules/character.py"),
        "✅ Resolved",
    ),
    "| Jack of All Trades, Reliable Talent |": (
        lambda: (
            "reliable_talent=sheet.reliable_talent" in _src("build_party.py")
            and "actor.reliable_talent and eligible" in _src("activities/check_pipeline.py")
        ),
        "Real live",
    ),
    # C20: Rage's extension check is a registered ``turn_end`` hook.
    "Turn lifecycle — start/end of turn, top-of-round hooks": (
        lambda: "engine:rage-extension" in _src("orchestrator.py"),
        "Rage hooks remain on the shared advance path",
    ),
    # C20: Action Surge's extra action is counted on the live turn view.
    "Action Surge grants one restricted": (
        lambda: "extra_actions_remaining" in _src("views.py"),
        "extra_actions_remaining",
    ),
    # C20: the Bonus-Action Dash/Disengage read the Cunning Action feature,
    # not the class slug.
    "| Dash, Disengage |": (
        lambda: (
            "_CUNNING_ACTION not in _granted_feature_slugs(current)" in _src("orchestrator.py")
            and 'class_slug != "rogue"' not in _src("orchestrator.py")
        ),
        "Cunning Action",
    ),
    # C20: every corpus ``uses.max`` shape evaluates, and ``@scaling``
    # resolves for an amount-scaled feature activity (Lay on Hands' Heal).
    "Class/species feature activities": (
        lambda: (
            "def preflight_feature(" in _src("feature_runtime.py")
            and "scaling_value" in _src("activities/formula.py")
        ),
        "Draw-free preflight",
    ),
    # C20: the four SRD 5.2 Fighting Style feats, each at its own seam.
    "| Fighting Style feats |": (
        lambda: (
            "def defense_ac_bonus(" in _src("rules/character.py")
            and "_fighting_style_attack_bonus(" in _src("activities/attack.py")
            and "_great_weapon_fighting_floor(" in _src("activities/attack.py")
            and '"two-weapon-fighting"' in _src("orchestrator.py")
        ),
        "✅ Resolved",
    ),
    # C20: Martial Arts and the Flurry of Blows strikes.
    "| Martial Arts and Monk's Focus |": (
        lambda: (
            "def _martial_arts_active(" in _src("orchestrator.py")
            and "flurry_strikes_remaining" in _src("types/combat.py")
        ),
        "(C20)",
    ),
    "| Stunning Strike |": (
        lambda: _attack_rider_options_resolve(
            "stunning-strike", ("Xto99a8Zt46VLwaR",), class_slug="monk", level=5
        ),
        "✅ Resolved",
    ),
    "| Open Hand Technique |": (
        lambda: _attack_rider_options_resolve(
            "open-hand-technique",
            ("1jdSaWanuRrdkVs3", "XoaS0RtDCGAqrQsf", "5Qgc0K3TfuonkPIG"),
            class_slug="monk",
            level=5,
            flurry=True,
        ),
        "✅ Resolved",
    ),
    "| Cunning Strike / Devious Strikes |": (
        _cunning_rider_scope_matches,
        "⚠️ Partial",
    ),
    "| Reckless Attack / Brutal Strike |": (
        _brutal_rider_scope_matches,
        "✅ Resolved",
    ),
    "| Berserker Frenzy |": (_frenzy_contract_resolves, "✅ Resolved"),
    "| Scoped post-hit movement |": (
        _scoped_movement_contract_resolves,
        "✅ Resolved for Withdraw and Forceful",
    ),
    "| Intimidating Presence |": (_intimidating_presence_scope_matches, "⚠️ Partial"),
    "| Cunning Strike Poison |": (
        lambda: _attack_rider_options_resolve(
            "cunning-strike",
            ("n64fvJMT9fPUy7DH",),
            class_slug="rogue",
            level=14,
            carried_items=("poisoners-kit",),
        ),
        "✅ Resolved option",
    ),
    "| Devious Knock Out |": (
        lambda: _attack_rider_options_resolve(
            "devious-strikes",
            ("3eq7lcmpkJJBU2KO",),
            class_slug="rogue",
            level=14,
        ),
        "✅ Resolved option",
    ),
    "| Typed repeat-save lifecycle |": (
        _typed_lifecycle_contract_resolves,
        "✅ Resolved for reviewed bindings",
    ),
    "| Expire-on-damage |": (
        _damage_expiry_contract_resolves,
        "✅ Resolved for typed bindings",
    ),
    "| Magic Resistance on repeat save |": (
        _magic_resistance_repeat_contract_resolves,
        "✅ Resolved for typed repeats",
    ),
    "| Typed effect lifecycle and conditional expiry |": (
        _typed_lifecycle_contract_resolves,
        "⚠️ Partial",
    ),
    # C20: Rage ends unless extended (and on Incapacitated).
    "| Rage |": (
        lambda: (
            '"not_extended"' in _src("events.py")
            and "engine:rage-extension" in _src("orchestrator.py")
        ),
        "Incapacitated ends it",
    ),
    # C20: a Bardic Inspiration die is redeemed on a failed attack roll.
    "| Bardic Inspiration |": (
        lambda: (
            '"no_granted_die"' in _event_class_body("AttackFailed")
            and "granted_die" in _src("activities/context.py")
        ),
        "(C20)",
    ),
    # C20: per-class levels reach live combat through one owner walk.
    "live combat reads `PartyMemberSpec.classes` (C20)": (
        lambda: "def feature_owners(" in _src("activities/scale.py"),
        "live combat reads `PartyMemberSpec.classes` (C20)",
    ),
    # C20: the Fighting Style feats apply.
    "| Feats |": (
        lambda: "def styles_from_feats(" in _src("rules/character.py"),
        "Fighting Style feats apply (C20",
    ),
    # C21: every concentration spell concentrates — a caster-held anchor for
    # one that applies no concentration effect of its own.
    "concentration spell concentrates (C21)": (
        lambda: "def _apply_concentration_anchor(" in _src("orchestrator.py"),
        "concentration spell concentrates (C21)",
    ),
    "| Concentration |": (
        lambda: "def _apply_concentration_anchor(" in _src("orchestrator.py"),
        "(C13, C21",
    ),
    # C21: the conjuration allowlist resolves five SRD 5.2 sources, Summon
    # Dragon's creature through the summon registry; every other summon,
    # transform and enchant stays narrative, so the row is Partial.
    "Summoning / polymorph / enchant-a-weapon": (
        lambda: (
            "CONJURATION_ALLOWLIST" in _src("activities/conjuration.py")
            and "SUMMONS" in _src("activities/conjuration.py")
        ),
        "⚠️ Partial",
    ),
    # C21: Wild Shape's form gate reads the Beast Shapes table.
    "| Wild Shape |": (
        lambda: (
            "def _wild_shape_failure(" in _src("orchestrator.py")
            and "WILD_SHAPE_TIERS" in _src("activities/conjuration.py")
        ),
        "(C21)",
    ),
    # C21: one swing of an attack action on the actor's current stat block.
    "| Stat-block attack commands |": (
        _monster_magnitudes_resolve,
        "Ordinary resolvable templates now share `StatBlockMagnitudes`",
    ),
    # C23: every status row carries a probe (``test_every_status_row_has_a_probe``).
    # Each fact below is the cheapest witness of the row's claim; a ❌ row's
    # probe is True while the mechanic is still absent.
    "Effect durations (`rounds`, `turns`, `seconds`": (
        lambda: (
            'key="engine:timed-effect-expiry"' in _src("orchestrator.py")
            and "until_end_of_next_turn_of" in _src("orchestrator.py")
        ),
        "✅",
    ),
    "Death saves, stabilization": (
        lambda: (
            "def roll_death_save(" in _src("death_saves.py")
            and "Stabilized(" in _src("death_saves.py")
        ),
        "✅",
    ),
    "Temporary HP, healing": (
        lambda: (
            "TempHpApplied(" in _src("activities/heal.py")
            and "HealingApplied(" in _src("activities/heal.py")
        ),
        "✅",
    ),
    "| Cover, line of sight |": (
        lambda: (
            "def cover_on_cell(" in _src("spatial.py")
            and "def has_line_of_sight(" in _src("spatial.py")
        ),
        "✅",
    ),
    # Flanking is an optional variant, not an SRD 5.2 rule: nothing names it.
    "| Flanking |": (
        lambda: "flank" not in _src("orchestrator.py").lower(),
        "❌",
    ),
    "2-D grid, Chebyshev distance": (
        lambda: "def _chebyshev(" in _src("spatial.py"),
        "✅",
    ),
    "Blocked cells, difficult terrain": (
        lambda: "difficult_terrain_cells" in _src("spatial.py"),
        "✅",
    ),
    "| Walls / line of sight |": (
        lambda: "def has_line_of_sight(" in _src("spatial.py"),
        "✅",
    ),
    "Elevation / flying altitude": (
        lambda: not re.search("elevation|altitude", _src("spatial.py"), re.IGNORECASE),
        "❌",
    ),
    "Physical movement modes and shared ledger": (_physical_movement_contract_resolves, "✅"),
    "Creature size qualifiers": (
        lambda: (
            _physical_movement_contract_resolves()
            and "creature_size: CreatureSize" in _src("types/combat.py")
            and "size_choice: CreatureSize" in _src("build_spec.py")
        ),
        "✅",
    ),
    "Grapple dragging and range release": (
        lambda: (
            "def drag_positions(" in _src("live_movement.py")
            and "def reconcile_grapple_range(" in _src("live_movement.py")
            and "dragged_by=actor_id" in _src("live_movement.py")
        ),
        "✅",
    ),
    # Creature size qualifies rules; footprints require additional geometry.
    "Multi-tile (Large+) creature footprints": (
        lambda: "footprint" not in _src("spatial.py"),
        "❌",
    ),
    "Cost-aware pathfinding": (
        lambda: (
            "def lowest_cost_path(" in _src("spatial.py")
            and "step_cost=lambda a, b: movement_cost(live," in _src("live_movement.py")
            and "def close_to_target(" in _src("live_movement.py")
        ),
        "✅",
    ),
    "Threat-aware pathfinding": (
        lambda: "threat_cost" not in _src("live_movement.py"),
        "❌",
    ),
    "Spell attack rolls & save DCs": (
        lambda: "def _resolve_dc(" in _src("activities/save.py"),
        "✅",
    ),
    "Counterspell, Shield, Hellish Rebuke, Magic Missile interactions": (
        lambda: (
            "matching_conditions(pending, opportunity)" in _src("live_reactions.py")
            and "def _apply_magic_missile_shield_carveout(" not in _src("orchestrator.py")
            and "def _drain_counterspell_reaction(" not in _src("orchestrator.py")
        ),
        "✅",
    ),
    "| Dispel Magic |": (
        lambda: not _spell_resolves(_canonical_spell("dispel-magic")),
        "❌",
    ),
    "Typed action selection + built-in AI": (
        lambda: "def rank_monster_actions(" in _src("activities/monster_actions.py"),
        "✅",
    ),
    "| Multiattack fan-out |": (
        _monster_action_economy_resolves,
        "⚠️ Partial",
    ),
    "| Monster spellcasting |": (
        lambda: "def _resolve_monster_cast(" in _src("orchestrator.py"),
        "✅",
    ),
    # C24: the grid flee planner ranks the cells ``reachable_cells`` finds.
    "Flee / retreat behaviour": (
        lambda: (
            "live_movement.flee(live," in _src("orchestrator.py")
            and "def reachable_cells(" in _src("spatial.py")
            and "has_fled" in _src("types/combat.py")
        ),
        "✅",
    ),
    "**Legendary actions**": (
        lambda: "LegendaryActionUsed(" in _src("orchestrator.py"),
        "✅",
    ),
    "**Lair actions**": (
        lambda: "lair" not in _src("orchestrator.py").lower(),
        "❌",
    ),
    "Recharge (5–6) abilities": (
        lambda: "RechargeRolled(" in _src("orchestrator.py"),
        "✅",
    ),
    "| Regeneration |": (
        lambda: "MonsterTraitMechanic.REGENERATION" in _src("orchestrator.py"),
        "✅",
    ),
    "Dataset categories `conditions/` + `traits/`": (
        lambda: (
            hasattr(BundledAssetLoader, "get_condition")
            and hasattr(BundledAssetLoader, "get_trait")
        ),
        "✅",
    ),
    "Ability scores, proficiency, expertise": (
        lambda: "ability_score_method" in _src("build_spec.py"),
        "⚠️ Partial",
    ),
    "| Sneak Attack |": (
        lambda: (
            "def sneak_attack_triggers(" in _src("activities/attack.py")
            and "ctx.sneak_attack_commit(plan.attacker_id, plan.target_id)"
            in _src("activities/attack.py")
            and "sneak_attack_commit=commit_sneak" in _src("live_attack_riders.py")
        ),
        "✅",
    ),
    "Short/long rest, hit dice, feature & item recharge": (
        lambda: (
            "def resolve_long_rest(" in _src("rest.py")
            and "def recover_item_uses(" in _src("rest.py")
        ),
        "✅",
    ),
    # C24: one step trigger serves every walk, whoever drives the mover.
    "| Opportunity attack |": (
        lambda: (
            "def _fire_opportunity_attacks_on_step(" in _src("orchestrator.py")
            and "def _opportunity_attackers(" in _src("orchestrator.py")
        ),
        "✅",
    ),
    "Shield (incl. vs. Magic Missile)": (
        lambda: (
            "attack_hit_reaction" in _src("activities/attack.py")
            and "negate_triggering_spell_damage" in _src("live_reactions.py")
        ),
        "✅",
    ),
    "| Hellish Rebuke |": (
        lambda: (
            "damage_instance_resolved" in _src("activities/apply.py")
            and "ReactionTriggerKind.TAKES_DAMAGE" in _src("live_reactions.py")
        ),
        "✅",
    ),
    "Feather Fall falling trigger": (
        lambda: "ReactionTriggerKind.CREATURE_FALLS" not in _src("reactions.py"),
        "❌",
    ),
    # Canonical conditions are required; there is no arbitrary trigger parser.
    "Ready an action with a custom trigger": (
        lambda: (
            'return "no typed reaction conditions"' in _src("reactions.py")
            and "prearm_failure(live," in _src("orchestrator.py")
        ),
        "❌",
    ),
}


@pytest.mark.parametrize("row", sorted(_PROBES))
def test_status_rows_match_code_probes(row: str, matrix_text: str) -> None:
    probe, status_if_true = _PROBES[row]
    lines = [line for line in matrix_text.splitlines() if row in line]
    assert len(lines) == 1, f"capabilities.md has {len(lines)} lines containing {row!r}, want 1"
    assert (status_if_true in lines[0]) == probe(), (
        f"capabilities.md row {row!r} disagrees with the code: the page "
        f"{'claims' if status_if_true in lines[0] else 'does not claim'} "
        f"{status_if_true!r}, the source says {probe()}"
    )


_STATUS_MARKS = ("✅", "⚠️", "❌")


def _status_rows(text: str) -> list[str]:
    """Every table row whose status (second) cell opens with a status mark."""
    rows = []
    for line in text.splitlines():
        cells = line.strip().strip("|").split("|")
        if line.startswith("|") and len(cells) > 1 and cells[1].strip().startswith(_STATUS_MARKS):
            rows.append(line)
    return rows


def test_every_status_row_has_a_probe(matrix_text: str) -> None:
    """A status row with no probe can drift from the code unnoticed, as ten
    once did: every ✅/⚠️/❌ row must contain a ``_PROBES`` key."""
    rows = _status_rows(matrix_text)
    assert len(rows) > 70
    unprobed = [row[:70] for row in rows if not any(key in row for key in _PROBES)]
    assert unprobed == []
