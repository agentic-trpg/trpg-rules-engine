"""Resource identity from canonical action through planning and actual execution."""

from __future__ import annotations

import asyncio
import copy
import random
from dataclasses import asdict

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd5e_srd_data.schema.common import (
    AttackActivity,
    RangeBlock,
    SaveActivity,
    SaveBlock,
    SaveDcBlock,
    SummonBonusesBlock,
    SummonMatchBlock,
    UsesBlock,
)
from dnd5e_srd_data.schema.monster import MonsterAction, MonsterActionKind

from dnd5e_engine import (
    EncounterMemberSpec,
    GridScene,
    PartyMemberSpec,
    PlayerIntent,
    advance_monster_turn,
    get_live,
    start_combat,
    submit_player_intent,
)
from dnd5e_engine.activities.conjuration import SummonRequest
from dnd5e_engine.activities.monster_actions import (
    MonsterActionPlan,
    plan_monster_action,
    rank_monster_actions,
)
from dnd5e_engine.events import AttackRolled, IntentSubmitted, RechargeRolled, SaveRolled, SpellCast
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from dnd5e_engine.orchestrator import (
    _apply_transform,
    _end_transform,
    _get_live,
    _monster_action_available,
    _monster_activity_available,
    _monster_cast_candidate,
    _resolve_monster_activities,
    _resolve_monster_attack_activities,
    _resolve_monster_cast,
    _roll_recharges,
    _seat_summon,
)
from dnd5e_engine.spatial import cell_id
from dnd5e_engine.types.conditions import ActiveCondition
from dnd5e_engine.types.effects import ActiveEffect


@pytest.fixture(autouse=True)
def loader():
    bundled = BundledAssetLoader()
    set_lib_loader_for_tests(bundled)
    yield bundled
    set_lib_loader_for_tests(None)


def action(monster, slug):
    return next(a for a in monster.actions if a.slug == slug)


async def start(monster, *, seed=7, count=1, distance=5, hero_ac=10):
    party = [
        PartyMemberSpec(
            entity_id="char:hero",
            name="Hero",
            initiative=1,
            hp_current=10000,
            hp_max=10000,
            ac=hero_ac,
            zone_id=cell_id(0, 0),
        )
    ]
    foes = [
        EncounterMemberSpec(
            entity_id=f"mon:foe{i}",
            entity_type="Monster",
            name="Foe",
            initiative=20 - i,
            hp_current=1000,
            hp_max=1000,
            ac=10,
            zone_id=cell_id(distance // 5, i),
            monster_template_slug=monster.slug,
        )
        for i in range(count)
    ]
    inputs = copy.deepcopy((party, foes, monster.model_dump()))
    result = await start_combat(
        session_id="action-economy",
        party=party,
        encounter=foes,
        grid_scene=GridScene(width=100, height=10, cell_size_ft=5),
        rng_seed=seed,
    )
    assert (party, foes, monster.model_dump()) == inputs
    live = _get_live(result.handle)
    actor = next(c for c in live.initiative if c.entity_id == "mon:foe0")
    target = next(c for c in live.initiative if c.entity_id == "char:hero")
    return result.handle, live, actor, target


def plan(live, actor, monster, chosen, **kwargs):
    return plan_monster_action(
        monster,
        chosen,
        is_available=lambda a: _monster_action_available(live, actor, a),
        is_activity_available=lambda a, b: _monster_activity_available(live, actor, a, b),
        **kwargs,
    )


def execute(live, actor, target, execution_plan):
    _resolve_monster_attack_activities(
        live,
        actor,
        [target],
        execution_plan.activities,
        execution_plan=execution_plan,
    )


def slugs(execution_plan):
    return [s.source_action.slug for s in execution_plan.executions]


def synthetic(loader, *actions):
    base = loader.get_monster("owlbear")
    monster = base.model_copy(update={"actions": list(actions)}, deep=True)
    set_lib_loader_for_tests(MemoryAssetLoader(monsters=[monster]))
    return monster


def attack(slug, *, uses=None, recharge=None, maximum="", distance=5):
    return MonsterAction(
        slug=slug,
        name=slug.title(),
        kind=MonsterActionKind.ACTION,
        description="Attack.",
        uses_per_day=uses,
        recharge=recharge,
        activities=[
            AttackActivity(
                id=f"{slug}-attack",
                range=RangeBlock(units="ft", value=str(distance)),
                uses=UsesBlock(max=maximum),
            )
        ],
    )


def multi(description, *, uses=None):
    return MonsterAction(
        slug="multiattack",
        name="Multiattack",
        kind=MonsterActionKind.ACTION,
        description=description,
        uses_per_day=uses,
    )


@pytest.mark.parametrize(
    ("slug", "action_slug", "maximum"),
    [
        ("vrock", "stunning-screech", 1),
        ("aboleth", "dominate-mind", 2),
    ],
)
def test_real_daily_uses_execute_exactly_n_times_without_mutating_inputs(
    loader, slug, action_slug, maximum
):
    monster = loader.get_monster(slug)
    chosen = action(monster, action_slug)
    before = monster.model_dump()

    async def go():
        handle, live, actor, target = await start(monster, count=2)
        entry = live.monster_action_uses_by_entity[actor.entity_id][chosen.slug]
        other = live.monster_action_uses_by_entity["mon:foe1"][chosen.slug]
        assert entry is not other
        assert entry.action_uses_remaining == maximum
        for remaining in reversed(range(maximum)):
            execution_plan = plan(live, actor, monster, chosen)
            assert execution_plan.executions[0].source_action is chosen
            execute(live, actor, target, execution_plan)
            assert entry.action_uses_remaining == remaining
            snapshot = get_live(handle).monster_action_uses_by_entity[actor.entity_id][chosen.slug]
            assert snapshot.action_uses_remaining == remaining
            assert other.action_uses_remaining == maximum
            assert _monster_action_available(live, actor, chosen) == (remaining > 0)
        events, rng = list(live.event_log), live.rng.getstate()
        execute(live, actor, target, execution_plan)  # stale plan: recheck at execution
        assert live.event_log == events
        assert live.rng.getstate() == rng
        assert not plan(live, actor, monster, chosen).executions
        assert entry.action_uses_remaining == 0

    asyncio.run(go())
    assert monster.model_dump() == before


def test_corpus_has_no_action_activity_limit_overlap_and_real_noncast_roar(loader):
    overlaps = []
    noncast = []
    for slug in loader.list_slugs("monsters"):
        monster = loader.get_monster(slug)
        for chosen in (*monster.actions, *monster.legendary_actions):
            limited = [a for a in chosen.activities if a.uses.max.strip().isdigit()]
            if chosen.uses_per_day is not None and limited:
                overlaps.append((slug, chosen.slug))
            if any(a.kind != "cast" for a in limited):
                noncast.append((slug, chosen.slug))
    assert overlaps == []  # re-review ownership if canonical data gains overlaps
    assert ("sphinx-of-valor", "roar") in noncast


@pytest.mark.parametrize("direct", [False, True])
def test_real_noncast_activity_pools_decrement_only_the_executed_roar(loader, direct):
    monster = loader.get_monster("sphinx-of-valor")
    roar = action(monster, "roar")

    async def go():
        _, live, actor, target = await start(monster)
        uses = live.monster_action_uses_by_entity[actor.entity_id]["roar"].uses_remaining
        assert list(uses.values()) == [1, 1, 1]
        for index, activity in enumerate(roar.activities):
            # Existing Multiattack resolves one offensive mode per child use.
            execution_plan = plan(
                live, actor, monster, roar if direct else action(monster, "multiattack")
            )
            assert execution_plan.executions[-1].source_action is roar
            assert execution_plan.executions[-1].activities == (activity,)
            execute(live, actor, target, execution_plan)
            assert list(uses.values()) == [0] * (index + 1) + [1] * (2 - index)
        assert not _monster_action_available(live, actor, roar)
        assert slugs(plan(live, actor, monster, action(monster, "multiattack"))) == ["claw", "claw"]

    asyncio.run(go())


def test_transformation_and_summon_lifecycles_get_fresh_independent_pools(loader):
    form = synthetic(loader, attack("strike", uses=2))
    spirit = loader.get_monster("draconic-spirit").model_copy(
        update={"actions": [attack("strike", uses=2)]}, deep=True
    )
    set_lib_loader_for_tests(MemoryAssetLoader(monsters=[form, spirit]))

    async def go():
        _, live, actor, target = await start(form)
        original = live.monster_action_uses_by_entity[actor.entity_id]
        original["strike"].action_uses_remaining = 1
        _apply_transform(
            live,
            actor.entity_id,
            form,
            source="polymorph",
            effect=ActiveEffect(
                id="effect:form",
                name="Form",
                origin="cast:polymorph:char:hero",
                target_id=actor.entity_id,
            ),
            temp_hp=form.hp,
        )
        changed = live.monster_action_uses_by_entity[actor.entity_id]
        assert changed is not original
        assert changed["strike"].action_uses_remaining == 2
        _end_transform(live, actor.entity_id, "remove_ieffect")
        assert (
            live.monster_action_uses_by_entity[actor.entity_id]["strike"].action_uses_remaining == 1
        )
        for i in range(2):
            _seat_summon(
                live,
                target,
                SummonRequest(
                    spell_id="summon-dragon",
                    owner_id=target.entity_id,
                    cell=cell_id(i + 2, 0),
                    slot_level=5,
                    bonuses=SummonBonusesBlock(),
                    match=SummonMatchBlock(),
                ),
            )
        entities = list(live.summons)
        first = live.monster_action_uses_by_entity[entities[0]]["strike"]
        second = live.monster_action_uses_by_entity[entities[1]]["strike"]
        assert first is not second
        first.action_uses_remaining = 0
        assert second.action_uses_remaining == 2
        assert spirit.actions[0].uses_per_day == 2

    asyncio.run(go())


def test_action_pool_is_authoritative_and_multiactivity_invocation_spends_once(loader):
    chosen = attack("burst", uses=2, maximum="2")
    chosen.activities.append(
        # The shared stat-block resolver reads an explicit typed DC instead
        # of the old uniform attack-bonus approximation.
        SaveActivity(
            id="burst-save",
            uses=UsesBlock(max="2"),
            save=SaveBlock(ability=["con"], dc=SaveDcBlock(calculation="flat", formula="8")),
        )
    )
    monster = synthetic(loader, chosen)
    chosen = action(monster, "burst")

    async def go():
        _, live, actor, target = await start(monster)
        entry = live.monster_action_uses_by_entity[actor.entity_id][chosen.slug]
        assert entry.action_uses_remaining == 2
        assert entry.uses_remaining == {}
        execution_plan = plan(live, actor, monster, chosen)
        assert len(execution_plan.activities) == 2
        execute(live, actor, target, execution_plan)
        assert entry.action_uses_remaining == 1
        # The same typed invocation used as a Multiattack child still spends once.
        wrapper = multi("makes one [[/item Burst]] attack.")
        execute(live, actor, target, MonsterActionPlan(wrapper, execution_plan.executions))
        assert entry.action_uses_remaining == 0
        assert not _monster_activity_available(live, actor, chosen, chosen.activities[1])

    asyncio.run(go())


def test_mixed_limited_and_unlimited_modes_never_execute_an_exhausted_activity(loader):
    chosen = attack("strike", maximum="1")
    unlimited = chosen.activities[0].model_copy(update={"id": "at-will", "uses": UsesBlock()})
    chosen.activities.append(unlimited)
    monster = synthetic(loader, chosen)
    chosen = action(monster, "strike")

    async def go():
        _, live, actor, target = await start(monster)
        for expected_id in ("strike-attack", "at-will", "at-will"):
            execution_plan = plan(live, actor, monster, chosen)
            assert [a.id for a in execution_plan.activities] == [expected_id]
            execute(live, actor, target, execution_plan)
        assert _monster_action_available(live, actor, chosen)
        assert live.monster_action_uses_by_entity[actor.entity_id]["strike"].uses_remaining == {
            "strike:strike-attack": 0
        }

    asyncio.run(go())


def test_limited_cast_keeps_its_activity_pool_and_spends_once(loader):
    monster = loader.get_monster("mage")
    chosen = action(monster, "spellcasting")

    async def go():
        _, live, actor, target = await start(monster)
        activity, spell = _monster_cast_candidate(live, actor, chosen)
        entry = live.monster_action_uses_by_entity[actor.entity_id][chosen.slug]
        before = dict(entry.uses_remaining)
        key = f"{chosen.slug}:{activity.id}"
        rng = live.rng.getstate()
        _resolve_monster_activities(live, actor, monster.slug, False, target)
        assert entry.uses_remaining == before
        assert rng == live.rng.getstate()
        _resolve_monster_cast(live, actor, target, chosen, activity, spell)
        assert entry.uses_remaining == {**before, key: before[key] - 1}
        assert len([e for e in live.event_log if isinstance(e, SpellCast)]) == 1

    asyncio.run(go())


def test_fixed_sequence_keeps_order_counts_and_source_identity(loader):
    monster = loader.get_monster("otyugh")
    execution_plan = plan_monster_action(monster, action(monster, "multiattack"))
    assert slugs(execution_plan) == ["bite", "tentacle", "tentacle"]
    assert all(
        step.source_action is action(monster, step.source_action.slug)
        for step in execution_plan.executions
    )


@pytest.mark.parametrize(
    ("distance", "profile", "expected"),
    [
        (5, "AGGRESSIVE", ["storm-blade"] * 3),
        (100, "AGGRESSIVE", ["storm-bolt"] * 3),
        (5, "RANGED", ["storm-bolt"] * 3),
    ],
)
def test_real_djinni_only_uses_referenced_attack_alternatives(loader, distance, profile, expected):
    monster = loader.get_monster("djinni")
    before = monster.model_dump()

    async def go():
        _, live, actor, target = await start(monster)
        pools = copy.deepcopy(live.monster_action_uses_by_entity)
        state = live.rng.getstate()
        execution_plan = plan(
            live,
            actor,
            monster,
            action(monster, "multiattack"),
            target_distance_ft=distance,
            behavior_profile=profile,
        )
        assert slugs(execution_plan) == expected
        assert live.rng.getstate() == state
        assert live.monster_action_uses_by_entity == pools
        execute(live, actor, target, execution_plan)
        assert len([e for e in live.event_log if isinstance(e, AttackRolled)]) == 3
        assert not [e for e in live.event_log if isinstance(e, SaveRolled)]
        assert live.monster_action_uses_by_entity == pools

    asyncio.run(go())
    assert monster.model_dump() == before


def test_real_doppelganger_optional_child_spends_its_own_recharge_and_then_is_omitted(loader):
    monster = loader.get_monster("doppelganger")
    multiattack = action(monster, "multiattack")

    async def go():
        _, live, actor, target = await start(monster, count=2)
        execution_plan = plan(live, actor, monster, multiattack)
        assert slugs(execution_plan) == ["slam", "slam", "unsettling-visage"]
        entry = live.monster_action_uses_by_entity[actor.entity_id]["unsettling-visage"]
        execute(live, actor, target, execution_plan)
        assert entry.recharge_spent
        assert entry.uses_remaining == {}
        assert not live.monster_action_uses_by_entity["mon:foe1"][
            "unsettling-visage"
        ].recharge_spent
        # The visage affects each creature in its emanation, including its ally;
        # both saves still spend only the acting doppelganger's Recharge pool.
        assert [e.target_id for e in live.event_log if isinstance(e, SaveRolled)] == [
            "mon:foe1",
            "char:hero",
        ]
        assert not [e for e in live.event_log if isinstance(e, RechargeRolled)]
        before = asdict(entry)
        shorter = plan(live, actor, monster, multiattack)
        assert slugs(shorter) == ["slam", "slam"]
        pre = len(live.event_log)
        execute(live, actor, target, shorter)
        assert not [e for e in live.event_log[pre:] if isinstance(e, SaveRolled)]
        assert asdict(entry) == before

    asyncio.run(go())


@pytest.mark.parametrize("optional", [True, False])
def test_repeated_finite_child_is_rechecked_without_replacement_or_negative_uses(loader, optional):
    wrapper = multi(
        "makes three [[/item Strike]] attacks"
        + (" and uses [[/item Burst]] if available." if optional else " and uses [[/item Burst]].")
    )
    monster = synthetic(loader, wrapper, attack("strike", uses=1), attack("burst", uses=1))

    async def go():
        _, live, actor, target = await start(monster)
        uses = live.monster_action_uses_by_entity[actor.entity_id]
        uses["burst"].action_uses_remaining = 0
        execution_plan = plan(live, actor, monster, action(monster, "multiattack"))
        assert slugs(execution_plan) == ["strike"] * 3
        execute(live, actor, target, execution_plan)
        assert len([e for e in live.event_log if isinstance(e, AttackRolled)]) == 1
        assert uses["strike"].action_uses_remaining == 0
        assert uses["burst"].action_uses_remaining == 0

    asyncio.run(go())


def test_real_aboleth_optional_alternative_daily_child_spends_only_when_selected(loader):
    monster = loader.get_monster("aboleth")
    before = monster.model_dump()

    async def go():
        _, live, actor, target = await start(monster)
        entry = live.monster_action_uses_by_entity[actor.entity_id]["dominate-mind"]
        wrapper = action(monster, "multiattack")
        first = plan(live, actor, monster, wrapper)
        assert slugs(first) == ["tentacle", "tentacle", "consume-memories"]
        execute(live, actor, target, first)
        assert entry.action_uses_remaining == 2
        # Inject runtime unavailability through the pure callback seam to select
        # the other canonical alternative; do not change the canonical grammar.
        for remaining in (1, 0):
            alternate = plan_monster_action(
                monster,
                wrapper,
                is_available=lambda a: (
                    a.slug != "consume-memories" and _monster_action_available(live, actor, a)
                ),
                is_activity_available=lambda a, b: _monster_activity_available(live, actor, a, b),
            )
            assert slugs(alternate) == ["tentacle", "tentacle", "dominate-mind"]
            execute(live, actor, target, alternate)
            assert entry.action_uses_remaining == remaining
        shorter = plan_monster_action(
            monster,
            wrapper,
            is_available=lambda a: (
                a.slug != "consume-memories" and _monster_action_available(live, actor, a)
            ),
        )
        assert slugs(shorter) == ["tentacle", "tentacle"]

    asyncio.run(go())
    assert monster.model_dump() == before


def test_selection_ranking_and_unselected_alternatives_do_not_spend(loader):
    monster = synthetic(
        loader,
        multi("makes three attacks using [[/item A]] or [[/item B]] in any combination."),
        attack("unrelated", uses=4),
        attack("a", uses=4, distance=150),
        attack("b", uses=4, distance=5),
    )

    async def go():
        _, live, actor, target = await start(monster)
        before = copy.deepcopy(live.monster_action_uses_by_entity)
        state = live.rng.getstate()
        ranked = rank_monster_actions(
            monster.actions, is_available=lambda a: _monster_action_available(live, actor, a)
        )
        assert [a.slug for a in ranked] == ["multiattack", "unrelated", "a", "b"]
        execution_plan = plan(live, actor, monster, ranked[0], target_distance_ft=100)
        assert slugs(execution_plan) == ["a"] * 3
        assert before == live.monster_action_uses_by_entity
        assert state == live.rng.getstate()
        execute(live, actor, target, execution_plan)
        uses = live.monster_action_uses_by_entity[actor.entity_id]
        assert uses["a"].action_uses_remaining == 1
        assert uses["b"].action_uses_remaining == uses["unrelated"].action_uses_remaining == 4

    asyncio.run(go())


@pytest.mark.parametrize(
    "description",
    [
        "makes two attacks.",
        "makes two [[/item .opaqueKey]].",
        "makes two [[/item Missing]] attacks.",
    ],
)
def test_ambiguous_multiattack_never_falls_back_to_an_unrelated_sibling(loader, description):
    monster = synthetic(loader, multi(description), attack("unrelated", uses=1))
    assert not plan_monster_action(monster, action(monster, "multiattack")).executions


@pytest.mark.parametrize("blocked", [False, True])
def test_range_or_precondition_failure_does_not_spend_recharge_or_daily_pool(loader, blocked):
    monster = synthetic(loader, attack("burst", uses=2, recharge="6"))

    async def go():
        handle, live, actor, _ = await start(monster, distance=250)
        if blocked:
            actor.conditions = [
                ActiveCondition(
                    condition="incapacitated", source_entity_id="implied:test", scope="combat"
                )
            ]
        before = copy.deepcopy(live.monster_action_uses_by_entity)
        await advance_monster_turn(handle)
        assert live.monster_action_uses_by_entity == before
        assert not [e for e in live.event_log if isinstance(e, AttackRolled)]

    asyncio.run(go())


class CountingRandom(random.Random):
    def __init__(self, seed):
        super().__init__(seed)
        self.draws = []

    def randint(self, low, high):
        value = super().randint(low, high)
        self.draws.append((low, high, value))
        return value


@pytest.mark.parametrize(("seed", "roll", "success"), [(1, 2, False), (19, 6, True)])
def test_conditional_recharge_exact_draws_restore_and_replay(loader, seed, roll, success):
    monster = loader.get_monster("doppelganger")

    async def go():
        _, live, actor, target = await start(monster, hero_ac=100)
        wrapper = action(monster, "multiattack")
        execute(live, actor, target, plan(live, actor, monster, wrapper))
        # Same production RNG seam, reset to a known seed at the next own start.
        # No dice are introduced by planning or spending.
        live.rng = CountingRandom(seed)
        pre = len(live.event_log)
        _roll_recharges(live, actor, monster)
        assert live.rng.draws == [(1, 6, roll)]
        assert len(live.event_log[pre:]) == 1
        event = live.event_log[-1]
        assert isinstance(event, RechargeRolled)
        assert (event.action_slug, event.roll, event.succeeded) == (
            "unsettling-visage",
            roll,
            success,
        )
        entry = live.monster_action_uses_by_entity[actor.entity_id]["unsettling-visage"]
        assert entry.recharge_spent == (not success)
        following = plan(live, actor, monster, wrapper)
        assert slugs(following) == ["slam", "slam"] + (["unsettling-visage"] if success else [])
        execute(live, actor, target, following)
        assert entry.recharge_spent
        assert [d for d in live.rng.draws if d[:2] == (1, 6)] == [(1, 6, roll)]
        return [e.model_dump() for e in live.event_log[pre:]], live.rng.getstate(), asdict(entry)

    assert asyncio.run(go()) == asyncio.run(go())


def test_direct_recharge_spends_only_on_execution_and_rolls_at_next_own_turn(loader):
    monster = loader.get_monster("magma-mephit")

    async def go():
        handle, live, actor, _ = await start(monster, seed=7)
        entry = live.monster_action_uses_by_entity[actor.entity_id]["fire-breath"]
        await advance_monster_turn(handle)
        assert entry.recharge_spent
        assert not [e for e in live.event_log if isinstance(e, RechargeRolled)]
        await submit_player_intent(
            handle, actor_id="char:hero", intent=PlayerIntent(intent_type="pass")
        )
        pre = len(live.event_log)
        await advance_monster_turn(handle)
        tail = live.event_log[pre:]
        rolls = [e for e in tail if isinstance(e, RechargeRolled)]
        assert len(rolls) == 1
        assert tail.index(rolls[0]) < next(
            i for i, e in enumerate(tail) if isinstance(e, IntentSubmitted)
        )
        assert entry.recharge_spent

    asyncio.run(go())


@pytest.mark.parametrize(("seed", "roll", "success"), [(1, 3, False), (4, 6, True)])
def test_child_recharge_turn_boundary_event_order_draw_count_and_replay(
    loader, monkeypatch, seed, roll, success
):
    monster = loader.get_monster("doppelganger")
    wrapper = action(monster, "multiattack")
    # Isolate the Multiattack path without changing the production Recharge
    # ranking tier (which otherwise selects direct Visage before Multiattack).
    monkeypatch.setattr(
        "dnd5e_engine.orchestrator.rank_monster_actions", lambda actions, **kwargs: [wrapper]
    )

    async def go():
        handle, live, _, _ = await start(monster, seed=seed, hero_ac=100)
        live.rng = CountingRandom(seed)
        pre = len(live.event_log)
        await advance_monster_turn(handle)
        first = live.event_log[pre:]
        assert len([e for e in first if isinstance(e, SaveRolled)]) == 1
        assert not [e for e in first if isinstance(e, RechargeRolled)]
        assert [hi for _, hi, _ in live.rng.draws] == [20, 20, 20]
        await submit_player_intent(
            handle, actor_id="char:hero", intent=PlayerIntent(intent_type="pass")
        )
        pre = len(live.event_log)
        await advance_monster_turn(handle)
        second = live.event_log[pre:]
        rolled = [e for e in second if isinstance(e, RechargeRolled)]
        assert len(rolled) == 1
        assert (rolled[0].roll, rolled[0].succeeded) == (roll, success)
        assert second.index(rolled[0]) < next(
            i for i, e in enumerate(second) if isinstance(e, IntentSubmitted)
        )
        assert len([e for e in second if isinstance(e, SaveRolled)]) == int(success)
        assert [hi for _, hi, _ in live.rng.draws] == [20, 20, 20, 6, 20, 20] + (
            [20] if success else []
        )
        entry = live.monster_action_uses_by_entity["mon:foe0"]["unsettling-visage"]
        assert entry.recharge_spent
        return [e.model_dump() for e in first + second], live.rng.getstate(), asdict(entry)

    assert asyncio.run(go()) == asyncio.run(go())


def test_child_out_of_range_and_unexecuted_wrapper_do_not_spend(loader):
    monster = synthetic(
        loader,
        multi("makes one [[/item Long]] attack and uses [[/item Short]].", uses=2),
        attack("long", distance=150),
        attack("short", uses=2, recharge="6"),
    )

    async def go():
        _, live, actor, target = await start(monster, distance=100)
        wrapper = action(monster, "multiattack")
        execute(live, actor, target, plan(live, actor, monster, wrapper))
        uses = live.monster_action_uses_by_entity[actor.entity_id]
        assert uses["short"].action_uses_remaining == 2
        assert not uses["short"].recharge_spent
        assert uses["multiattack"].action_uses_remaining == 1
        assert len([e for e in live.event_log if isinstance(e, AttackRolled)]) == 1
        # No executed child means the top-level limited wrapper spends nothing.
        execute(live, actor, target, MonsterActionPlan(wrapper))
        assert uses["multiattack"].action_uses_remaining == 1

    asyncio.run(go())
