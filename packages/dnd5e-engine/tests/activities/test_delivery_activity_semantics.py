"""Pure delivery contracts: saves, movement requests and delegated provenance."""

from __future__ import annotations

import random
from dataclasses import FrozenInstanceError, replace

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader
from dnd5e_srd_data.schema.common import CastActivity, ForcedMovementSpec, SaveActivity
from dnd5e_srd_data.schema.monster import MonsterTraitMechanic

from dnd5e_engine.activities.apply import apply_damage
from dnd5e_engine.activities.cast import resolve_cast
from dnd5e_engine.activities.context import ActivityResolutionContext
from dnd5e_engine.activities.forced_movement import ForcedMovementRequest
from dnd5e_engine.activities.resolver import resolve_activity
from dnd5e_engine.activities.save_primitive import roll_save
from dnd5e_engine.events import CombatEvent, DamageApplied, SaveRolled
from dnd5e_engine.spell_delivery import SpellDeliverySpec
from dnd5e_engine.types.combat import Combatant

LOADER = BundledAssetLoader()


def _creature(entity_id: str, *, creature_type: str = "humanoid") -> Combatant:
    return Combatant(
        entity_id=entity_id,
        entity_type="Monster",
        name=entity_id,
        initiative=10,
        hp_current=100,
        hp_max=100,
        creature_type=creature_type,
    )


def _context(
    events: list[CombatEvent], targets: list[Combatant] | None = None
) -> ActivityResolutionContext:
    return ActivityResolutionContext(
        rng=random.Random(41),
        caster=_creature("char:caster"),
        targets=targets or [_creature("mon:target")],
        event_emitter=events.append,
        caster_abilities={ability: 16 for ability in ("str", "dex", "con", "int", "wis", "cha")},
        spellcasting_ability="int",
        caster_proficiency_bonus=3,
    )


@pytest.mark.parametrize("ability", ["str", "dex", "con", "int", "wis", "cha"])
def test_automatic_success_consumes_clause_without_any_save_dice(ability: str) -> None:
    events: list[CombatEvent] = []
    ctx = _context(events)
    target = ctx.targets[0]
    consumed: list[str] = []
    ctx = replace(
        ctx,
        target_auto_success_ids=frozenset({target.entity_id}),
        consume_next_save_modifier=lambda target_id: consumed.append(target_id) or True,
        passive_save_auto_fail={target.entity_id: [ability.upper()]},
        passive_save_adv={target.entity_id: [ability.upper()]},
        passive_save_dis={target.entity_id: [ability.upper()]},
        passive_save_bonus={target.entity_id: "1d4"},
        legendary_resistance_armed={target.entity_id: 1},
        legendary_resistances_remaining_by_entity={target.entity_id: 3},
    )
    before = ctx.rng.getstate()

    roll = roll_save(ctx, target, ability, 19)

    assert (roll.succeeded, roll.natural, roll.total, roll.modifier, roll.mode) == (
        True,
        None,
        19,
        0,
        "normal",
    )
    assert roll.sources == ()
    assert roll.legendary_resistance_remaining is None
    assert consumed == [target.entity_id]
    assert ctx.legendary_resistance_armed == {target.entity_id: 1}
    assert ctx.legendary_resistances_remaining_by_entity == {target.entity_id: 3}
    assert ctx.rng.getstate() == before


def test_dust_automatic_success_still_emits_save_and_never_applies_failure_effect() -> None:
    dust = LOADER.get_item("dust-of-sneezing-and-choking")
    assert dust is not None
    activity = next(activity for activity in dust.activities if isinstance(activity, SaveActivity))
    events: list[CombatEvent] = []
    ctx = _context(events, [_creature("mon:construct", creature_type="construct")])
    ctx = replace(
        ctx,
        target_auto_success_ids=frozenset({"mon:construct"}),
        source_passive_effects=dust.passive_effects,
    )
    before = ctx.rng.getstate()

    resolve_activity(activity, ctx)

    assert [event.type for event in events] == ["save_rolled"]
    assert isinstance(events[0], SaveRolled)
    assert events[0].succeeded
    assert events[0].natural is None
    assert ctx.rng.getstate() == before


@pytest.mark.parametrize(
    ("trigger", "natural", "expected"),
    [("failed_save", 1, True), ("failed_save", 20, False), ("successful_save", 20, True)],
)
@pytest.mark.parametrize("direction", ["away_from_source", "toward_source"])
def test_save_records_only_matching_typed_movement_after_damage(
    trigger: str, natural: int, expected: bool, direction: str
) -> None:
    spell = LOADER.get_spell("thunderwave")
    assert spell is not None
    activity = spell.activities[0].model_copy(
        update={
            "forced_movement": ForcedMovementSpec(
                trigger=trigger, distance_ft=10, direction=direction
            )
        }
    )
    events: list[CombatEvent] = []
    ctx = _context(events)
    ctx = replace(
        ctx,
        base_spell_level=1,
        slot_level=1,
        activity_source_id=f"spell:{spell.slug}:{activity.id}",
        source_parent_id="item:reviewed-wrapper:cast",
    )
    ctx.variables["force_save_d20"] = natural
    requests_at_events: list[int] = []
    ctx = replace(
        ctx,
        event_emitter=lambda event: (
            events.append(event),
            requests_at_events.append(len(ctx.forced_movement_requests)),
        ),
    )

    resolve_activity(activity, ctx)

    assert [event.type for event in events] == ["save_rolled", "damage_applied"]
    assert requests_at_events == [0, 0]
    assert bool(ctx.forced_movement_requests) is expected
    if expected:
        assert ctx.forced_movement_requests == [
            ForcedMovementRequest("char:caster", "mon:target", 10, direction, activity.id)
        ]
    damage = next(event for event in events if isinstance(event, DamageApplied))
    assert damage.source_id == f"spell:{spell.slug}:{activity.id}"
    assert damage.source_parent_id == "item:reviewed-wrapper:cast"


def test_forced_movement_is_canonical_not_spell_slug_selected() -> None:
    spell = LOADER.get_spell("thunderwave")
    assert spell is not None
    activity = spell.activities[0].model_copy(update={"forced_movement": None})
    ctx = replace(_context([]), lifecycle_source_slug="thunderwave", base_spell_level=1)
    ctx.variables["force_save_d20"] = 1

    resolve_activity(activity, ctx)

    assert ctx.forced_movement_requests == []


def test_movement_request_value_is_immutable() -> None:
    request = ForcedMovementRequest("char:a", "mon:b", 10, "toward_source", "activity")
    with pytest.raises(FrozenInstanceError):
        request.distance_ft = 15  # type: ignore[misc]


@pytest.mark.parametrize(("natural", "expected"), [(1, False), (20, True)])
def test_attack_hit_records_typed_movement_and_spell_damage_identity(
    natural: int, expected: bool
) -> None:
    spell = LOADER.get_spell("fire-bolt")
    assert spell is not None
    activity = spell.activities[0].model_copy(
        update={
            "forced_movement": ForcedMovementSpec(
                trigger="hit", distance_ft=5, direction="toward_source"
            )
        }
    )
    events: list[CombatEvent] = []
    ctx = replace(
        _context(events),
        base_spell_level=0,
        activity_source_id=f"spell:{spell.slug}:{activity.id}",
    )
    ctx.variables["force_d20"] = natural

    resolve_activity(activity, ctx)

    assert bool(ctx.forced_movement_requests) is expected
    if expected:
        assert ctx.forced_movement_requests[0].source_activity_id == activity.id
        damage = next(event for event in events if isinstance(event, DamageApplied))
        assert damage.source_id == f"spell:{spell.slug}:{activity.id}"


def test_standalone_delegation_sets_child_activity_and_parent_identity() -> None:
    item = LOADER.get_item("wand-of-fireballs")
    spell = LOADER.get_spell("fireball")
    assert item is not None
    assert spell is not None
    activity = next(activity for activity in item.activities if isinstance(activity, CastActivity))
    events: list[CombatEvent] = []
    parent_identity = f"item:{item.slug}:{activity.id}"
    ctx = replace(
        _context(events),
        activity_source_id=parent_identity,
        spell_book={activity.spell.uuid: spell},
    )
    ctx.variables["force_save_d20"] = 1

    resolve_cast(activity, ctx)

    damage = next(event for event in events if isinstance(event, DamageApplied))
    assert damage.source_id == f"spell:{spell.slug}:{spell.activities[0].id}"
    assert damage.source_parent_id == parent_identity
    assert ctx.activity_source_id == parent_identity
    save = next(event for event in events if isinstance(event, SaveRolled))
    assert save.dc == 15


def test_delegate_passes_choices_and_overrides_to_dispatcher_for_child_replanning() -> None:
    item = LOADER.get_item("wand-of-fireballs")
    spell = LOADER.get_spell("fireball")
    assert item is not None
    assert spell is not None
    activity = next(activity for activity in item.activities if isinstance(activity, CastActivity))
    events: list[CombatEvent] = []
    seen: list[ActivityResolutionContext] = []
    declaration = SpellDeliverySpec(
        primary_target_id="mon:target",
        selected_target_ids=("mon:target",),
        origin_cell="cell:8:4",
        direction=(1, 0),
        excluded_target_ids=("char:ally",),
        source_item_id=item.slug,
    )
    parent_identity = f"item:{item.slug}:{activity.id}"

    def dispatch(child_spell: object, child_ctx: ActivityResolutionContext) -> None:
        assert child_spell is spell
        assert not events
        seen.append(child_ctx)
        # The dispatcher owns target planning. It may replace the parent's
        # named target with the full child area before using the pure resolver.
        resolve_activity(
            spell.activities[0],
            replace(
                child_ctx,
                targets=[_creature("mon:area-a"), _creature("mon:area-b")],
                activity_source_id=f"spell:{spell.slug}:{spell.activities[0].id}",
            ),
        )

    ctx = replace(
        _context(events),
        spell_book={activity.spell.uuid: spell},
        spell_dispatch=dispatch,
        spell_delivery=declaration,
        activity_source_id=parent_identity,
        cast_level_override=5,
        target_auto_success_ids=frozenset({"mon:target"}),
    )

    resolve_cast(activity, ctx)

    assert len(seen) == 1
    child = seen[0]
    assert child.spell_delivery == declaration.model_copy(
        update={"source_kind": "item_cast", "source_activity_id": activity.id}
    )
    assert child.save_dc_override == 15
    assert child.slot_level == 5
    assert child.base_spell_level == 3
    assert child.lifecycle_source_kind == "spell"
    assert child.lifecycle_source_slug == "fireball"
    assert child.source_parent_id == parent_identity
    assert child.activity_source_id is None
    assert child.target_auto_success_ids == frozenset()
    assert child.parent_chain == (activity.spell.uuid,)
    assert declaration.source_kind == "direct_spell"
    assert [event.target_id for event in events if isinstance(event, SaveRolled)] == [
        "mon:area-a",
        "mon:area-b",
    ]


def test_direct_damage_uses_context_identity_and_omits_absent_parent() -> None:
    spell = LOADER.get_spell("magic-missile")
    assert spell is not None
    events: list[CombatEvent] = []
    identity = f"spell:{spell.slug}:{spell.activities[0].id}"
    ctx = replace(_context(events), base_spell_level=1, activity_source_id=identity)

    resolve_activity(spell.activities[0], ctx)

    damage = next(event for event in events if isinstance(event, DamageApplied))
    assert damage.source_id == identity
    assert damage.source_parent_id is None
    assert "source_parent_id" not in damage.model_dump()


def test_activity_auto_success_does_not_leak_into_an_independent_trait_save() -> None:
    events: list[CombatEvent] = []
    target = _creature("mon:undead", creature_type="undead")
    target.hp_current = 1
    target.trait_mechanics = [MonsterTraitMechanic.UNDEAD_FORTITUDE]
    ctx = replace(_context(events, [target]), target_auto_success_ids=frozenset({target.entity_id}))
    before = ctx.rng.getstate()

    apply_damage(target, {"thunder": 100}, ctx)

    save = next(event for event in events if isinstance(event, SaveRolled))
    assert save.dc == 105
    assert save.natural is not None
    assert not save.succeeded
    assert ctx.rng.getstate() != before
