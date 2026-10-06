"""Pure monster action ranking and planning with preserved resource identity.

Multiattack joins explicit Foundry item references to typed siblings. Fixed
sequences preserve prose order; free combinations restrict the candidate set;
optional uses ask the caller's live availability callback. Unknown references
never authorize unrelated actions. Monster turns execute typed plans; the
activity-only projection remains for stat-block commands and opportunity attacks.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from dnd5e_srd_data.schema.common import AttackActivity, CastActivity, SaveActivity

if TYPE_CHECKING:
    from dnd5e_srd_data.schema.common import Activity
    from dnd5e_srd_data.schema.monster import Monster, MonsterAction

    from dnd5e_engine.types.combat import MonsterActionUses

_LOGGER = logging.getLogger(__name__)

_MULTIATTACK_SLUG = "multiattack"


@dataclass(frozen=True)
class MonsterActionExecution:
    """One invocation; source identity owns recharge and action-level uses.

    ``None`` is reserved for a legacy attack with no canonical action.
    Activity-level pools are owned by the activities on this invocation.
    """

    source_action: MonsterAction | None
    activities: tuple[Activity, ...]


@dataclass(frozen=True)
class MonsterActionPlan:
    """A selected action and its ordered invocations, with no live mutation."""

    source_action: MonsterAction | None = None
    executions: tuple[MonsterActionExecution, ...] = ()

    @property
    def activities(self) -> tuple[Activity, ...]:
        return tuple(a for step in self.executions for a in step.activities)


def action_resources_available(uses: MonsterActionUses | None) -> bool:
    """Shared action gate, independent of activity selection or live state."""
    return uses is None or (
        not uses.recharge_spent
        and (uses.action_uses_remaining is None or uses.action_uses_remaining > 0)
    )


def activity_resources_available(
    action: MonsterAction, activity: Activity, uses: MonsterActionUses | None
) -> bool:
    """An action pool gates every mode; otherwise each activity owns its pool."""
    if not action_resources_available(uses):
        return False
    if uses is None or uses.action_uses_remaining is not None:
        return True
    return uses.uses_remaining.get(f"{action.slug}:{activity.id}", 1) > 0


# Count words in a multiattack description ("makes two attacks…"). The corpus
# also writes counts as digits ("makes 2 Pincer attacks"), so both parse.
_NUMBER_WORD: dict[str, int] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
}

# Foundry item enricher, in the two joinable shapes the corpus ships:
#   ``[[/item .<id>]]{Label}``  -> group "label"
#   ``[[/item Name]]``          -> group "name"
# A bare ``[[/item .<id>]]`` with no label matches but yields neither, which is
# what makes it unjoinable — the id is not a field on ``MonsterAction``.
_ITEM_TOKEN_RE = re.compile(
    r"\[\[/item\s+(?:\.(?P<id>[A-Za-z0-9.]+)\]\](?:\{(?P<label>[^}]*)\})?"
    r"|(?P<name>[^\].{}][^\]]*)\]\])"
)

# A count immediately preceding an item token: "makes two [[/item Claw]]",
# "uses [[/item Reel]]", "makes 2 [[/item Pincer]]". Captured lazily so the
# scan stays anchored to the token that follows it.
_COUNT_BEFORE_TOKEN_RE = re.compile(
    r"(?:\b(?P<word>" + "|".join(_NUMBER_WORD) + r")\b|\b(?P<digits>\d{1,2})\b)"
    r"(?:\s+\w+){0,3}?\s*$",
    re.IGNORECASE,
)


def _activity_is_offensive(activity: Activity) -> bool:
    return isinstance(activity, (AttackActivity, SaveActivity))


def _activity_is_offensive_or_cast(activity: Activity) -> bool:
    """``_activity_is_offensive`` plus ``CastActivity`` — a spellcasting
    action is offensive-in-spirit even though its own activity carries no
    attack/save block (the referenced spell does)."""
    return _activity_is_offensive(activity) or isinstance(activity, CastActivity)


def _action_has_offense(action: MonsterAction) -> bool:
    return any(_activity_is_offensive(a) for a in action.activities)


def _limited_use_cast_activities(action: MonsterAction) -> list[CastActivity]:
    """``action``'s cast activities with an integer ``uses.max`` and a
    ``day`` recovery period (SRD 5.2 Innate Spellcasting's "N/Day" shape).

    An at-will cast (empty ``uses.max``) or one recovering on a rest period
    other than ``day`` doesn't qualify — this is deliberately narrow to the
    one shape ``rank_monster_actions`` ranks above multiattack.
    """
    return [
        activity
        for activity in action.activities
        if isinstance(activity, CastActivity)
        and activity.uses.max.strip().isdigit()
        and any(entry.period == "day" for entry in activity.uses.recovery)
    ]


def rank_monster_actions(
    actions: Sequence[MonsterAction],
    *,
    is_available: Callable[[MonsterAction], bool],
    has_limited_use_remaining: Callable[[MonsterAction], bool] | None = None,
) -> list[MonsterAction]:
    """Order ``actions`` by SRD 5.2 selection priority for a monster's turn.

    Tiers, ties broken by ``actions`` list order within each tier:

    1. offensive actions with ``recharge`` set that pass ``is_available``
       (a live breath weapon/AoE is the monster's biggest hitter and was
       structurally unreachable under the old first-in-list selection);
    2. actions carrying a limited-use offensive cast activity (integer
       ``uses.max`` with a ``day`` recovery period) that pass
       ``is_available`` AND, when the caller supplies
       ``has_limited_use_remaining``, for which it reports that the cast the
       turn would actually resolve is such a limited-use one with a use
       left — Innate Spellcasting's N/Day slots. Once every N/Day cast is
       spent, an action that can still cast an at-will spell falls to
       tier 4, behind multiattack;
    3. the ``multiattack`` action, if present;
    4. every other offensive action (offensive now includes any
       ``CastActivity``, e.g. an at-will spell) that passes ``is_available``,
       in list order.

    An action that fails ``is_available`` drops out entirely rather than
    sinking to a lower tier. Pure — ``is_available`` and
    ``has_limited_use_remaining`` are the caller's seams into live
    per-entity state (recharge/uses tracking); this function reads no
    orchestrator state itself. Omitting ``has_limited_use_remaining`` keeps
    the static tier-2 predicate (the shape alone).
    """
    placed: set[str] = set()
    ranked: list[MonsterAction] = []

    for action in actions:
        if action.recharge and _action_has_offense(action) and is_available(action):
            ranked.append(action)
            placed.add(action.slug)

    for action in actions:
        if action.slug in placed:
            continue
        if (
            _limited_use_cast_activities(action)
            and is_available(action)
            and (has_limited_use_remaining is None or has_limited_use_remaining(action))
        ):
            ranked.append(action)
            placed.add(action.slug)

    for action in actions:
        if action.slug in placed:
            continue
        if action.slug == _MULTIATTACK_SLUG and is_available(action):
            ranked.append(action)
            placed.add(action.slug)

    for action in actions:
        if action.slug in placed:
            continue
        if any(_activity_is_offensive_or_cast(a) for a in action.activities) and is_available(
            action
        ):
            ranked.append(action)
            placed.add(action.slug)

    return ranked


def _attack_siblings(monster: Monster, exclude: MonsterAction) -> list[MonsterAction]:
    """Actions with an attack/save activity, excluding the given action."""
    return [a for a in monster.actions if a.slug != exclude.slug and _action_has_offense(a)]


def _first_offensive_activity(action: MonsterAction) -> Activity | None:
    for activity in action.activities:
        if _activity_is_offensive(activity):
            return activity
    return None


# Foundry's monster-manual action ids are mnemonic: ``mmRottingFist000`` is
# "Rotting Fist", ``mmBite0000000000`` is "Bite". The id is zero-padded to a
# fixed width and camel-cased. Ids that do NOT follow this convention (a random
# document key like ``w3cX0piuU875Hc2M``) yield nothing and stay unjoinable.
_FOUNDRY_MM_ID_RE = re.compile(r"^mm([A-Za-z][A-Za-z]*?)0*$")
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")

# Sentence break. The corpus is not consistently spaced ("attacks.It can
# replace…"), so a period followed by whitespace OR a capital letter ends it.
_SENTENCE_BREAK_RE = re.compile(r"\.(?:\s+|(?=[A-Z]))")


def _multiattack_clause(description: str) -> str:
    """The first sentence — the multiattack proper, minus any rider clause.

    Later sentences describe substitutions the engine does not model ("It can
    replace one attack with a use of Spellcasting"). Counting their tokens as
    extra attacks would inflate every dragon's action economy, so they are cut.
    """
    # Opaque Foundry ids can start with a capital (.XbN...), which is not a
    # sentence boundary. Mask references while preserving offsets.
    masked = _ITEM_TOKEN_RE.sub(lambda match: " " * len(match.group()), description)
    boundary = _SENTENCE_BREAK_RE.search(masked)
    return description[: boundary.start()] if boundary else description


def _name_from_foundry_id(foundry_id: str) -> str | None:
    """Recover an action name from a mnemonic Foundry id, or ``None``.

    ``mmDreadfulGlare0`` -> ``"Dreadful Glare"``. Purely a *hint*: the caller
    only uses it when the recovered name joins to exactly one typed sibling, so
    a bad guess degrades to the existing fallback rather than mis-resolving.
    """
    match = _FOUNDRY_MM_ID_RE.match(foundry_id)
    if match is None:
        return None
    words = _CAMEL_BOUNDARY_RE.split(match.group(1))
    return " ".join(words) if words else None


def _parse_multiattack_count(description: str) -> int:
    """Parse the leading count ("makes two attacks…" → 2, "makes 2 …" → 2).

    Defaults to 1 (logged) when nothing parses, so an unparseable multiattack
    degrades to a single attack rather than guessing arity.
    """
    for word, count in _NUMBER_WORD.items():
        if re.search(rf"\bmakes? {word}\b", description, re.IGNORECASE):
            return count
    digits = re.search(r"\bmakes? (\d{1,2})\b", description, re.IGNORECASE)
    if digits:
        return max(1, int(digits.group(1)))
    _LOGGER.warning("multiattack_count_unparsed default=1 description=%r", description)
    return 1


def _parse_item_counts(description: str) -> list[tuple[str, int]] | None:
    """Parse the multiattack clause into ordered ``(sibling name, count)`` pairs.

    Returns ``None`` when the clause cannot be read precisely — any token that
    is a bare Foundry id (unjoinable), or a free-choice clause ("Slam or Force
    Bolt in any combination", "three Radiant Sword attacks or uses Holy Burst
    twice") where the prose describes alternatives rather than a fixed sequence.
    This legacy count-only helper serves host-driven Attack budgets. Driven
    monster turns use the reference-preserving planner below instead.
    """
    clause = _multiattack_clause(description)
    matches = list(_ITEM_TOKEN_RE.finditer(clause))
    if not matches:
        return None
    if re.search(r"\bor\b", clause, re.IGNORECASE):
        return None  # alternatives use the leading count for host Attack budgets

    pairs: list[tuple[str, int]] = []
    for match in matches:
        name = match.group("label") or match.group("name")
        if not name or not name.strip():
            # Bare id — recoverable only when Foundry's mnemonic id convention
            # yields a name; otherwise the token cannot identify a sibling.
            name = _name_from_foundry_id(match.group("id") or "") or ""
        if not name.strip():
            return None
        preceding = clause[: match.start()]
        count_match = _COUNT_BEFORE_TOKEN_RE.search(preceding)
        if count_match is None:
            count = 1
        elif count_match.group("word"):
            count = _NUMBER_WORD[count_match.group("word").lower()]
        else:
            count = max(1, int(count_match.group("digits")))
        pairs.append((name.strip(), count))
    return pairs


def multiattack_count(monster: Monster) -> int:
    """How many attacks ``monster``'s Multiattack makes: the sum of the named
    counts when its clause parses precisely ("one Bite attack and one Claw
    attack" is 2), else its leading count ("two attacks, using Bite or Claw in
    any combination" is 2); 1 for a monster without a Multiattack."""
    action = next((a for a in monster.actions if a.slug == _MULTIATTACK_SLUG), None)
    if action is None:
        return 1
    parsed = _parse_item_counts(action.description)
    if parsed:
        return sum(count for _, count in parsed)
    return _parse_multiattack_count(_multiattack_clause(action.description))


def _activity_range_ft(activity: Activity, melee_reach_ft: int) -> int | None:
    """The effective reach, in feet, of an attack/save activity for sibling choice.

    Mirrors ``orchestrator._monster_attack_range_ft``'s per-activity reading so
    the fallback selection and the movement gate agree on each sibling's range:

      * an ``AttackActivity`` with an explicit ``units == "ft"`` positive
        value uses it verbatim (the Scout longbow's ``"150"``); a Foundry melee
        attack (``units == "self"`` / empty value) falls back to ``melee_reach_ft``;
      * a ranged single-target ``SaveActivity`` (``units == "ft"``, positive
        value) uses that value; a self/template save carries no positional reach.

    Returns ``None`` when no finite reach is resolvable — the caller treats an
    unknown-range sibling as unable to disqualify itself (never over-filters).
    """
    rng = activity.range
    if rng.units == "ft" and rng.value:
        try:
            parsed = int(rng.value)
        except ValueError:
            parsed = 0
        if parsed > 0:
            return parsed
    if isinstance(activity, AttackActivity):
        # units == "self" / empty ft value ⇒ melee reach (mirrors the gate).
        return melee_reach_ft if melee_reach_ft > 0 else None
    return None


def select_typed_monster_action(monster: Monster) -> MonsterAction | None:
    """Pick which action this monster should use this turn.

    Mirrors ``monster_ai.select_monster_action`` mechanical priority:
    multiattack first (the signature "use your full action budget" choice),
    else the first action whose activities contain an attack or save.
    Behaviour/flee gating stays with the caller (it owns the live Combatant).
    """
    for action in monster.actions:
        if action.slug == _MULTIATTACK_SLUG:
            return action
    for action in monster.actions:
        if _action_has_offense(action):
            return action
    return None


_ANY_COMBINATION_RE = re.compile(r"\bin any combination\b", re.IGNORECASE)


def _distribute_any_combination(
    named: list[MonsterAction],
    count: int,
    target_distance_ft: int | None,
    behavior_profile: str | None,
    melee_reach_ft: int,
    *,
    repeat_primary: bool = False,
) -> list[MonsterAction]:
    """SRD "makes N attacks, using A and B in any combination": the monster
    chooses the mix. Candidates are the named siblings whose reach covers the
    live distance (all of them when the distance is unknown or none covers);
    one candidate ⇒ repeat it; a ``RANGED`` monster repeats its longest-reach
    candidate. Preserve the old repeat-primary policy for "or" alternatives;
    otherwise alternate over the candidates in list order."""

    def _reach(sibling: MonsterAction) -> int | None:
        activity = _first_offensive_activity(sibling)
        return _activity_range_ft(activity, melee_reach_ft) if activity is not None else None

    candidates = [s for s in named if _first_offensive_activity(s) is not None]
    if target_distance_ft is not None:
        covering = [
            s for s in candidates if (_reach(s) is None or (_reach(s) or 0) >= target_distance_ft)
        ]
        if covering:
            candidates = covering
    if not candidates:
        return []
    if len(candidates) > 1 and behavior_profile == "RANGED":
        candidates = [max(candidates, key=lambda s: _reach(s) or 0)]
    if repeat_primary:
        candidates = candidates[:1]
    return [candidates[index % len(candidates)] for index in range(count)]


def _direct_activities(
    action: MonsterAction, is_available: Callable[[MonsterAction, Activity], bool]
) -> tuple[Activity, ...]:
    """Collapse alternative attack modes, filtering exhausted activities."""
    resolved: list[Activity] = []
    seen_attack = False
    # Individually limited save modes (the Sphinx's successive Roars) are
    # alternatives, not three simultaneous saves on a single invocation.
    offensive = [a for a in action.activities if _activity_is_offensive(a)]
    limited_save_modes = bool(offensive) and all(
        isinstance(a, SaveActivity) and a.uses.max.strip().isdigit() for a in offensive
    )
    seen_save = False
    for activity in action.activities:
        if not is_available(action, activity):
            continue
        if isinstance(activity, AttackActivity):
            if seen_attack:
                continue
            seen_attack = True
        if limited_save_modes and isinstance(activity, SaveActivity):
            if seen_save:
                continue
            seen_save = True
        resolved.append(activity)
    return tuple(resolved)


@dataclass(frozen=True)
class _MultiattackReference:
    source_action: MonsterAction
    count: int
    preceding: str
    following: str


def _multiattack_references(
    clause: str, siblings: Sequence[MonsterAction]
) -> list[_MultiattackReference]:
    """Join only explicit item tokens to unique typed sibling names."""
    matches = list(_ITEM_TOKEN_RE.finditer(clause))
    references: list[_MultiattackReference] = []
    for index, match in enumerate(matches):
        name = (
            match.group("label")
            or match.group("name")
            or _name_from_foundry_id(match.group("id") or "")
            or ""
        ).strip()
        joined = [s for s in siblings if s.name.casefold() == name.casefold()]
        if len(joined) != 1:
            _LOGGER.warning("multiattack_join_unresolved reference=%r description=%r", name, clause)
            continue
        preceding = clause[matches[index - 1].end() if index else 0 : match.start()]
        following = clause[
            match.end() : matches[index + 1].start() if index + 1 < len(matches) else None
        ]
        counted = _COUNT_BEFORE_TOKEN_RE.search(preceding)
        count = (
            int(counted.group("digits"))
            if counted and counted.group("digits")
            else _NUMBER_WORD[counted.group("word").lower()]
            if counted
            else 1
        )
        references.append(_MultiattackReference(joined[0], max(1, count), preceding, following))
    return references


def _select_referenced_sequence(
    references: Sequence[_MultiattackReference],
    is_available: Callable[[MonsterAction], bool],
) -> list[MonsterAction]:
    selected: list[MonsterAction] = []
    skip_next = False
    for index, ref in enumerate(references):
        if skip_next:
            skip_next = False
            continue
        if re.search(r"\buses either\s*$", ref.preceding, re.IGNORECASE) and index + 1 < len(
            references
        ):
            other = references[index + 1]
            if re.fullmatch(r"\s*or\s*", ref.following, re.IGNORECASE) and re.match(
                r"\s*if available\.?\s*$", other.following, re.IGNORECASE
            ):
                chosen = next(
                    (r.source_action for r in (ref, other) if is_available(r.source_action)),
                    None,
                )
                if chosen is not None:
                    selected.append(chosen)
                skip_next = True
                continue
        if re.search(r"\bor\b", ref.preceding, re.IGNORECASE):
            continue  # unsupported alternative branch never adds an extra use
        # A condition on a later alternative (Clay Golem's Hasten) does not
        # make the preceding fixed branch conditional.
        following = re.split(r"\bor\b", ref.following, maxsplit=1, flags=re.IGNORECASE)[0]
        if re.search(r"\bif\b", following, re.IGNORECASE) and not re.match(
            r"\s*if available\.?\s*$", following, re.IGNORECASE
        ):
            continue
        if is_available(ref.source_action):
            selected.extend([ref.source_action] * ref.count)
    return selected


def plan_monster_action(
    monster: Monster,
    action: MonsterAction,
    *,
    target_distance_ft: int | None = None,
    behavior_profile: str | None = None,
    melee_reach_ft: int = 5,
    is_available: Callable[[MonsterAction], bool] = lambda _: True,
    is_activity_available: Callable[[MonsterAction, Activity], bool] = lambda _a, _b: True,
) -> MonsterActionPlan:
    """Plan ordered invocations without spending resources or drawing dice.

    Fixed sequences preserve prose order. Free combinations distribute only
    over referenced siblings. ``uses X if available`` and ``uses either X or Y
    if available`` gate the optional use. Unknown conditionals/alternatives
    conservatively retain only the first referenced branch; unknown references
    never authorize an unrelated sibling. Mandatory unavailable uses are omitted
    without replacement. Execution rechecks resources after earlier steps.
    """
    if not is_available(action):
        return MonsterActionPlan(action)
    if action.slug != _MULTIATTACK_SLUG:
        activities = _direct_activities(action, is_activity_available)
        return MonsterActionPlan(action, (MonsterActionExecution(action, activities),))

    clause = _multiattack_clause(action.description)
    references = _multiattack_references(clause, _attack_siblings(monster, exclude=action))
    if not references:
        _LOGGER.warning(
            "multiattack_join_unresolved monster=%s description=%r", monster.slug, clause
        )
        return MonsterActionPlan(action)

    if _ANY_COMBINATION_RE.search(clause):
        selected = _select_combination(
            references, is_available, target_distance_ft, behavior_profile, melee_reach_ft
        )
    else:
        selected = _select_referenced_sequence(references, is_available)
    steps: list[MonsterActionExecution] = []
    for sibling in selected:
        # Preserve the existing fan-out: one offensive mode per child invocation.
        # Exhausted finite modes advance to the next available mode (Roar).
        activity = next(
            (
                a
                for a in sibling.activities
                if _activity_is_offensive(a) and is_activity_available(sibling, a)
            ),
            None,
        )
        if activity is not None:
            steps.append(MonsterActionExecution(sibling, (activity,)))
    return MonsterActionPlan(action, tuple(steps))


def _select_combination(
    references: Sequence[_MultiattackReference],
    is_available: Callable[[MonsterAction], bool],
    target_distance_ft: int | None,
    behavior_profile: str | None,
    melee_reach_ft: int,
) -> list[MonsterAction]:
    """N attacks using A/B, optionally preceded by fixed attacks (Tarrasque)."""
    start = next(
        (
            i
            for i, ref in enumerate(references)
            if re.search(r"\busing\s*$", ref.preceding, re.IGNORECASE)
        ),
        None,
    )
    if start is None:
        # Ambiguous combination: retain only the referenced primary branch.
        return _select_referenced_sequence(references[:1], is_available)
    counted = _COUNT_BEFORE_TOKEN_RE.search(references[start].preceding.replace(",", ""))
    if counted is None:
        return _select_referenced_sequence(references[:start], is_available)
    count = (
        int(counted.group("digits"))
        if counted.group("digits")
        else _NUMBER_WORD[counted.group("word").lower()]
    )
    named = [ref.source_action for ref in references[start:] if is_available(ref.source_action)]
    return _select_referenced_sequence(
        references[:start], is_available
    ) + _distribute_any_combination(
        named,
        count,
        target_distance_ft,
        behavior_profile,
        melee_reach_ft,
        repeat_primary=any(
            re.search(r"\bor\b", ref.following, re.IGNORECASE) for ref in references[start:]
        ),
    )


def expand_action_to_activities(
    monster: Monster,
    action: MonsterAction,
    *,
    target_distance_ft: int | None = None,
    behavior_profile: str | None = None,
    melee_reach_ft: int = 5,
) -> list[Activity]:
    """Compatibility projection for callers needing activities without live uses.

    Monster turns execute the typed plan instead, preserving resource identity.
    """
    return list(
        plan_monster_action(
            monster,
            action,
            target_distance_ft=target_distance_ft,
            behavior_profile=behavior_profile,
            melee_reach_ft=melee_reach_ft,
        ).activities
    )
