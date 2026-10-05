"""Migrated condition clauses reach SaveRolled without changing the RNG stream."""

from __future__ import annotations

import random
from dataclasses import replace

import pytest
from dnd5e_srd_data.schema.common import SaveActivity, SaveBlock, SaveDcBlock
from dnd5e_srd_data.schema.condition import ConditionEffectKind
from dnd5e_srd_data.schema.monster import MonsterTraitMechanic

from dnd5e_engine.activities.build_context import build_activity_context
from dnd5e_engine.activities.context import ActivityResolutionContext
from dnd5e_engine.activities.save import resolve_save
from dnd5e_engine.events import Ability, CombatEvent, SaveRolled
from dnd5e_engine.rules import conditions as condition_rules
from dnd5e_engine.types.combat import Combatant
from dnd5e_engine.types.conditions import ActiveCondition

ABILITIES: tuple[Ability, ...] = ("str", "dex", "con", "int", "wis", "cha")


def _context(
    conditions: list[str], *, seed: int = 7, magic_resistance: bool = False
) -> tuple[ActivityResolutionContext, list[CombatEvent]]:
    caster = Combatant(
        entity_id="char:caster",
        entity_type="Character",
        name="Caster",
        initiative=20,
        hp_current=20,
        hp_max=20,
    )
    target = Combatant(
        entity_id="mon:target",
        entity_type="Monster",
        name="Target",
        initiative=1,
        hp_current=20,
        hp_max=20,
        conditions=[
            ActiveCondition(condition=name, source_entity_id="implied:scenario", scope="combat")
            for name in conditions
        ],
        trait_mechanics=[MonsterTraitMechanic.MAGIC_RESISTANCE] if magic_resistance else [],
    )
    events: list[CombatEvent] = []
    projection = condition_rules.project_passive_save_modifiers(conditions)
    ctx = build_activity_context(
        caster,
        [target],
        rng=random.Random(seed),
        event_emitter=events.append,
        slot_level=1,
        base_spell_level=1,
        spellcasting_ability="wis",
        concentration=False,
        source_passive_effects=[],
        spell_book={},
        passive_damage_modifiers={},
        save_modifiers={target.entity_id: {**projection, "saves": dict.fromkeys(ABILITIES, 3)}},
    )
    # These standalone fixture activities carry their own explicit DC.
    return replace(ctx, save_dc_override=None), events


def _save(ability: Ability) -> SaveActivity:
    return SaveActivity(
        _id="save:condition",
        save=SaveBlock(ability=[ability], dc=SaveDcBlock(calculation="flat", formula="12")),
    )


@pytest.mark.parametrize(
    "condition", ["restrained", "paralyzed", "stunned", "petrified", "unconscious"]
)
@pytest.mark.parametrize("ability", ABILITIES)
@pytest.mark.parametrize("seed", [1, 7])
def test_condition_save_scope_preserves_events_and_exact_rng_state(
    condition: str, ability: Ability, seed: int
) -> None:
    ctx, events = _context([condition], seed=seed)
    before = ctx.targets[0].model_dump()
    reference = random.Random(seed)
    auto_fail = condition != "restrained" and ability in {"str", "dex"}
    disadvantaged = condition == "restrained" and ability == "dex"
    if auto_fail:
        natural, modifier, total, succeeded, mode, sources = None, 0, 0, False, "normal", []
    else:
        natural = reference.randint(1, 20)
        if disadvantaged:
            natural = min(natural, reference.randint(1, 20))
        modifier = 3
        total = natural + modifier
        succeeded = total >= 12
        mode = "disadvantage" if disadvantaged else "normal"
        sources = ["condition:target"] if disadvantaged else []

    resolve_save(_save(ability), ctx)

    assert events == [
        SaveRolled(
            target_id="mon:target",
            ability=ability,
            dc=12,
            roll_total=total,
            succeeded=succeeded,
            advantage=mode,
            natural=natural,
            modifier=modifier,
            sources=sources,
        )
    ]
    assert ctx.rng.getstate() == reference.getstate()
    assert ctx.targets[0].model_dump() == before


@pytest.mark.parametrize("ability", ["dex", "wis"])
def test_restrained_save_advantage_cancels_only_the_scoped_disadvantage(ability: Ability) -> None:
    ctx, events = _context(["RESTRAINED", "restrained"], magic_resistance=True)
    reference = random.Random(7)
    natural = reference.randint(1, 20)
    if ability == "dex":
        mode, sources = "normal", ["trait", "condition:target"]
    else:
        natural = max(natural, reference.randint(1, 20))
        mode, sources = "advantage", ["trait"]

    resolve_save(_save(ability), ctx)

    assert events == [
        SaveRolled(
            target_id="mon:target",
            ability=ability,
            dc=12,
            roll_total=natural + 3,
            succeeded=natural + 3 >= 12,
            advantage=mode,
            natural=natural,
            modifier=3,
            sources=sources,
        )
    ]
    assert ctx.rng.getstate() == reference.getstate()


@pytest.mark.parametrize(
    ("condition", "kind"),
    [
        ("restrained", ConditionEffectKind.DISADVANTAGE_SAVE),
        ("paralyzed", ConditionEffectKind.AUTO_FAIL_SAVE),
        ("stunned", ConditionEffectKind.AUTO_FAIL_SAVE),
        ("petrified", ConditionEffectKind.AUTO_FAIL_SAVE),
        ("unconscious", ConditionEffectKind.AUTO_FAIL_SAVE),
    ],
)
def test_removing_save_opt_in_reaches_the_resolver_without_legacy_fallback(
    condition: str, kind: ConditionEffectKind, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        condition_rules._DECLARATIVE_CONDITION_MIGRATIONS,
        condition,
        condition_rules._DECLARATIVE_CONDITION_MIGRATIONS[condition] - {kind},
    )
    ctx, events = _context([condition.upper(), condition])
    reference = random.Random(7)
    natural = reference.randint(1, 20)

    resolve_save(_save("dex"), ctx)

    assert events == [
        SaveRolled(
            target_id="mon:target",
            ability="dex",
            dc=12,
            roll_total=natural + 3,
            succeeded=natural + 3 >= 12,
            advantage="normal",
            natural=natural,
            modifier=3,
            sources=[],
        )
    ]
    assert ctx.rng.getstate() == reference.getstate()
