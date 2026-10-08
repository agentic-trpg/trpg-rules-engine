"""Legal candidate selection precedes the established monster HP priority."""

from __future__ import annotations

import copy

import pytest
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd5e_srd_data.schema.common import RangeBlock, TargetBlock, TargetCreatureFilter
from dnd5e_srd_data.schema.monster import MonsterAction, MonsterActionKind

from dnd5e_engine import orchestrator as orch
from dnd5e_engine.events import AttackRolled, DamageApplied, IntentSubmitted, SaveRolled
from dnd5e_engine.lib_loader import set_lib_loader_for_tests
from tests.c20_support import act, events
from tests.e2e.harness import run_async
from tests.spell_review_support import review_spell_variant
from tests.test_delivery_hardening import _restricted_action, _state
from tests.test_monster_spatial_execution import setup
from tests.test_monster_spell_delivery import _cast_action


@pytest.fixture(autouse=True)
def loader():
    bundled = BundledAssetLoader()
    set_lib_loader_for_tests(bundled)
    yield bundled
    set_lib_loader_for_tests(None)


def _multi(*, description="The creature makes two [[/item Burst]] attacks."):
    return MonsterAction(
        slug="multiattack",
        name="Multiattack",
        kind=MonsterActionKind.ACTION,
        description=description,
        uses_per_day=2,
    )


def _candidates(live, *, hp=(5, 30, 30), types=("humanoid", "undead", "undead")):
    for i, (health, creature_type) in enumerate(zip(hp, types, strict=True)):
        target = next(c for c in live.initiative if c.entity_id == f"char:{i}")
        target.hp_current = health
        target.creature_type = creature_type
        live.tracked_hp[target.entity_id] = health


def _drive(live, *, legendary=False):
    run_async(
        orch.advance_monster_turn(
            orch.CombatHandle(live.handle_id),
            legendary=legendary,
            actor_id="mon:actor" if legendary else None,
        )
    )


def _legendary_window(live, actor, monster, action, *, spells=()):
    template = monster.model_copy(update={"actions": [], "legendary_actions": [action]})
    # This helper deliberately installs a synthetic legendary encounter fixture.
    live.ruleset_loader = MemoryAssetLoader(monsters=[template], spells=list(spells))
    set_lib_loader_for_tests(live.ruleset_loader)
    actor.legendary_actions_max = actor.legendary_actions_remaining = 1
    _drive(live)  # No ordinary action; open the first PC's turn.
    act(orch.CombatHandle(live.handle_id), "char:0", intent_type="pass")


@pytest.mark.parametrize("mode", ["action", "multiattack", "legendary", "spell", "legendary_spell"])
def test_public_monster_chooses_legal_undead_over_lower_hp_humanoid(loader, mode, monkeypatch):
    def run():
        child = _restricted_action(loader)
        if mode == "multiattack":
            # A ready recharge action outranks Multiattack; isolate the wrapper
            # using a one-use child, so the second invocation cannot spend it.
            child = child.model_copy(update={"recharge": "", "uses_per_day": 1})
        root = _multi() if mode == "multiattack" else child
        spells = []
        if mode in ("spell", "legendary_spell"):
            spell = loader.get_spell("sacred-flame")
            filtered = spell.activities[0].model_copy(update={"target": child.activities[0].target})
            spell = spell.model_copy(update={"activities": [filtered]})
            review_spell_variant(monkeypatch, spell)
            spells = [spell]
            root = _cast_action(spell)
        live, actor, _, monster = setup(
            loader,
            root,
            [(6, 5), (7, 5), (8, 5)],
            extra_actions=[child] if mode == "multiattack" else [],
        )
        _candidates(live)
        actor.spellcasting_ability = "int"
        actor.intelligence = 18
        if mode in ("legendary", "legendary_spell"):
            _legendary_window(live, actor, monster, root, spells=spells)
        elif spells:
            live.ruleset_loader = MemoryAssetLoader(monsters=[monster], spells=spells)
            set_lib_loader_for_tests(live.ruleset_loader)
        pre = len(live.event_log)
        _drive(live, legendary=mode in ("legendary", "legendary_spell"))
        assert [e.target_id for e in live.event_log[pre:] if isinstance(e, SaveRolled)] == [
            "char:1"
        ]
        assert not [
            e
            for e in live.event_log[pre:]
            if isinstance(e, DamageApplied) and e.target_id == "char:0"
        ]
        if mode in ("legendary", "legendary_spell"):
            assert (
                next(
                    c for c in live.initiative if c.entity_id == actor.entity_id
                ).legendary_actions_remaining
                == 0
            )
        else:
            pool = live.monster_action_uses_by_entity[actor.entity_id][root.slug]
            assert pool.action_uses_remaining == 1
            assert events(live, IntentSubmitted)[-1].target_id == "char:1"
        return (
            [e.model_dump_json() for e in live.event_log[pre:]],
            live.rng.getstate(),
            _state(live),
        )

    assert run() == run()


@pytest.mark.parametrize("health,expected", [((5, 30, 20), "char:2"), ((5, 30, 30), "char:1")])
def test_legal_candidates_keep_lowest_hp_and_initiative_tie_break(loader, health, expected):
    action = _restricted_action(loader)
    live, _, _, _ = setup(loader, action, [(6, 5), (7, 5), (8, 5)])
    _candidates(live, hp=health)
    _drive(live)
    assert [e.target_id for e in events(live, SaveRolled)] == [expected]


@pytest.mark.parametrize("departure", ["dead", "departed", "changed_type"])
def test_public_selection_ignores_stale_candidate_and_uses_next_legal_enemy(loader, departure):
    action = _restricted_action(loader)
    live, _, _, _ = setup(loader, action, [(6, 5), (7, 5), (8, 5)])
    _candidates(live)
    target = next(c for c in live.initiative if c.entity_id == "char:1")
    if departure == "dead":
        live.dead_ids.add(target.entity_id)
    elif departure == "departed":
        live.initiative.remove(target)
    else:
        target.creature_type = "humanoid"
    _drive(live)
    assert [e.target_id for e in events(live, SaveRolled)] == ["char:2"]


def test_action_eligibility_is_pure_and_keeps_out_of_range_legal_candidate(loader):
    action = _restricted_action(loader)
    activity = action.activities[0].model_copy(update={"range": RangeBlock(units="ft", value="5")})
    action = action.model_copy(update={"activities": [activity]})
    live, actor, _, _ = setup(loader, action, [(6, 5), (10, 5), (11, 5)])
    _candidates(live)
    before = (_state(live), live.rng.getstate(), list(live.event_log))
    assert orch._monster_activity_available(live, actor, action, activity)
    assert orch._monster_activity_available(live, actor, action, activity)
    assert (_state(live), live.rng.getstate(), live.event_log) == before
    _drive(live)
    assert live.actor_zone[actor.entity_id] != "5,5"
    assert [e.target_id for e in events(live, SaveRolled)] == ["char:1"]


@pytest.mark.parametrize("fallback", [False, True])
def test_no_legal_candidate_preserves_restricted_action_resources_and_rng(loader, fallback):
    action = _restricted_action(loader)
    claw = next(a for a in loader.get_monster("magma-mephit").actions if a.slug == "claw")
    live, actor, _, _ = setup(
        loader, action, [(6, 5), (7, 5), (8, 5)], extra_actions=[claw] if fallback else []
    )
    _candidates(live, types=("humanoid",) * 3)
    before = copy.deepcopy(live.monster_action_uses_by_entity)
    rng = live.rng.getstate()
    _drive(live)
    assert live.monster_action_uses_by_entity == before
    assert not events(live, SaveRolled)
    assert bool(events(live, AttackRolled)) == fallback
    if not fallback:
        assert live.rng.getstate() == rng
        assert actor.action_available


def test_no_legal_legendary_candidate_preserves_entire_preflight_state(loader):
    action = _restricted_action(loader)
    live, actor, _, monster = setup(loader, action, [(6, 5), (7, 5), (8, 5)])
    _candidates(live, types=("humanoid",) * 3)
    _legendary_window(live, actor, monster, action)
    before = (
        _state(live),
        live.rng.getstate(),
        list(live.event_log),
        copy.deepcopy(live.legendary_windows_used),
    )
    with pytest.raises(orch.IntentRejectedError, match="no usable legendary action"):
        _drive(live, legendary=True)
    assert (
        _state(live),
        live.rng.getstate(),
        live.event_log,
        live.legendary_windows_used,
    ) == before


def test_multiattack_children_choose_their_own_legal_target(loader):
    undead = _restricted_action(loader).model_copy(update={"recharge": ""})
    humanoid = undead.model_copy(
        update={
            "slug": "other",
            "name": "Other",
            "recharge": "",
            "activities": [
                undead.activities[0].model_copy(
                    update={
                        "id": "other:save",
                        "target": TargetBlock(
                            creature_filter=TargetCreatureFilter(
                                include_creature_types=("humanoid",)
                            )
                        ),
                    }
                )
            ],
        }
    )
    root = _multi(description="The creature uses [[/item Burst]] and [[/item Other]].")
    live, actor, _, _ = setup(
        loader, root, [(6, 5), (7, 5), (8, 5)], extra_actions=[undead, humanoid]
    )
    _candidates(live)
    _drive(live)
    assert [e.target_id for e in events(live, SaveRolled)] == ["char:1", "char:0"]
    pools = live.monster_action_uses_by_entity[actor.entity_id]
    assert all(pools[slug].action_uses_remaining == 1 for slug in ("burst", "other", "multiattack"))


@pytest.mark.parametrize("mover_type", ["humanoid", "undead"])
def test_opportunity_attack_only_checks_triggering_mover_before_reaction_payment(
    loader, mover_type
):
    claw = next(a for a in loader.get_monster("magma-mephit").actions if a.slug == "claw")
    attack = claw.activities[0].model_copy(
        update={
            "target": TargetBlock(
                creature_filter=TargetCreatureFilter(include_creature_types=("undead",))
            ),
        }
    )
    action = claw.model_copy(update={"activities": [attack]})
    live, actor, _, _ = setup(loader, action, [(6, 5), (5, 6)])
    _candidates(live, hp=(5, 30), types=(mover_type, "undead"))
    _drive(live)
    pre = len(live.event_log)
    rng = live.rng.getstate()
    act(orch.CombatHandle(live.handle_id), "char:0", intent_type="move", target_zone_id="7,5")
    attacks = [e for e in live.event_log[pre:] if isinstance(e, AttackRolled)]
    assert [e.target_id for e in attacks] == (["char:0"] if mover_type == "undead" else [])
    assert next(
        c for c in live.initiative if c.entity_id == actor.entity_id
    ).reaction_available == (mover_type != "undead")
    if mover_type == "humanoid":
        assert live.rng.getstate() == rng


@pytest.mark.parametrize("fallback", [False, True])
def test_multiattack_with_no_legal_child_falls_back_without_spending_wrapper(loader, fallback):
    child = _restricted_action(loader).model_copy(update={"recharge": ""})
    root = _multi()
    claw = next(a for a in loader.get_monster("magma-mephit").actions if a.slug == "claw")
    live, actor, _, _ = setup(
        loader, root, [(6, 5), (7, 5), (8, 5)], extra_actions=[child, *([claw] if fallback else [])]
    )
    _candidates(live, types=("humanoid",) * 3)
    before = copy.deepcopy(live.monster_action_uses_by_entity)
    rng = live.rng.getstate()
    _drive(live)
    assert bool(events(live, AttackRolled)) == fallback
    assert not events(live, SaveRolled)
    assert live.monster_action_uses_by_entity == before
    assert events(live, IntentSubmitted)[-1].target_id == "char:0"
    if not fallback:
        assert live.rng.getstate() == rng
        assert actor.action_available


@pytest.mark.parametrize("change", ["dead", "departed", "changed_type", "out_of_range", "blocked"])
def test_execution_rechecks_candidate_after_public_action_selection(loader, monkeypatch, change):
    action = _restricted_action(loader)
    live, actor, _, _ = setup(loader, action, [(6, 5), (7, 5), (8, 5)])
    _candidates(live)
    execute = orch._resolve_monster_execution
    rng = live.rng.getstate()

    def interrupted(live, *args, **kwargs):
        selected = next(c for c in live.initiative if c.entity_id == "char:1")
        if change == "dead":
            live.dead_ids.add(selected.entity_id)
        elif change == "departed":
            live.initiative.remove(selected)
        elif change == "changed_type":
            selected.creature_type = "humanoid"
        elif change == "out_of_range":
            live.actor_zone[selected.entity_id] = "29,29"
        else:
            live.topology = orch.GridTopology(
                orch.GridScene(width=30, height=30, cover_cells={"7,5": "total"})
            )
        return execute(live, *args, **kwargs)

    monkeypatch.setattr(orch, "_resolve_monster_execution", interrupted)
    _drive(live)
    # Selection declared B before the simulated interruption. Preserve the
    # existing no-retarget contract when a committed target becomes illegal.
    assert not events(live, SaveRolled)
    pool = live.monster_action_uses_by_entity[actor.entity_id][action.slug]
    assert pool.action_uses_remaining == 2
    assert not pool.recharge_spent
    assert live.rng.getstate() == rng


@pytest.mark.parametrize("mode", ["legendary", "spell"])
def test_stationary_selection_tries_next_in_range_legal_candidate(loader, mode, monkeypatch):
    action = _restricted_action(loader)
    spells = []
    if mode == "spell":
        spell = loader.get_spell("sacred-flame")
        spell = spell.model_copy(
            update={
                "activities": [
                    spell.activities[0].model_copy(update={"target": action.activities[0].target})
                ]
            }
        )
        review_spell_variant(monkeypatch, spell)
        spells = [spell]
        action = _cast_action(spell)
    live, actor, _, monster = setup(loader, action, [(6, 5), (29, 29), (8, 5)])
    _candidates(live)
    actor.spellcasting_ability = "int"
    if mode == "legendary":
        _legendary_window(live, actor, monster, action)
    else:
        live.ruleset_loader = MemoryAssetLoader(monsters=[monster], spells=spells)
        set_lib_loader_for_tests(live.ruleset_loader)
    _drive(live, legendary=mode == "legendary")
    assert [e.target_id for e in events(live, SaveRolled)] == ["char:2"]
    assert live.actor_zone[actor.entity_id] == "5,5"


@pytest.mark.parametrize("fallback", [False, True])
def test_no_legal_monster_spell_is_pure_before_fallback_or_pass(loader, fallback, monkeypatch):
    spell = loader.get_spell("sacred-flame")
    target = _restricted_action(loader).activities[0].target
    spell = spell.model_copy(
        update={"activities": [spell.activities[0].model_copy(update={"target": target})]}
    )
    review_spell_variant(monkeypatch, spell)
    action = _cast_action(spell)
    claw = next(a for a in loader.get_monster("magma-mephit").actions if a.slug == "claw")
    live, actor, _, monster = setup(
        loader, action, [(6, 5), (7, 5), (8, 5)], extra_actions=[claw] if fallback else []
    )
    _candidates(live, types=("humanoid",) * 3)
    live.ruleset_loader = MemoryAssetLoader(monsters=[monster], spells=[spell])
    set_lib_loader_for_tests(live.ruleset_loader)
    before = (_state(live), live.rng.getstate(), list(live.event_log))
    assert orch._monster_cast_candidate(live, actor, action) is None
    assert orch._monster_cast_candidate(live, actor, action) is None
    assert (_state(live), live.rng.getstate(), live.event_log) == before
    pool = copy.deepcopy(live.monster_action_uses_by_entity)
    _drive(live)
    assert not events(live, SaveRolled)
    assert bool(events(live, AttackRolled)) == fallback
    assert live.monster_action_uses_by_entity == pool
    if not fallback:
        assert live.rng.getstate() == before[1]
        assert actor.action_available
