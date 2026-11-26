"""F2 — the single SRD 5.2 D20 Test primitive (attack, save, check).

SRD 5.2 "Advantage and Disadvantage": when a roll is affected by any number
of advantage sources and any number of disadvantage sources at once, they
cancel out — the roll is made normally, regardless of how many of each kind
apply. Multiple sources of the *same* kind never stack.

Foundry parity: ``module/dice/d20-roll.mjs`` (``advantageMode`` selects
``kh``/``kl`` on a 2d20 formula rather than branching on separate rolls).

Purity boundary (CLAUDE.md): this module imports nothing from
``orchestrator`` — it is a pure function of an RNG, a modifier, and a typed
set of advantage/disadvantage sources.

Draw discipline: normal mode consumes exactly one ``rng.randint(1, 20)``
call so pre-F2 seeded combat scenarios stay byte-identical; only
advantage/disadvantage mode consumes two. ``forced_natural`` (fumble/crit
test scaffolding, future rules hooks) consumes zero draws.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from dnd5e_engine.events import AdvantageMode, AdvantageSource


@dataclass(frozen=True)
class AdvantageSources:
    """Retain ordered typed causes for one D20 Test; only presence affects its mode."""

    advantage: tuple[AdvantageSource, ...] = ()
    disadvantage: tuple[AdvantageSource, ...] = ()


@dataclass(frozen=True)
class D20Result:
    """The outcome of a single SRD 5.2 D20 Test.

    ``first`` is the FIRST d20 drawn (the only one in ``normal`` mode);
    ``kept`` is the face actually used for the total once
    advantage/disadvantage resolution has picked higher/lower. The two are
    equal in ``normal`` mode and whenever ``forced_natural`` is supplied.

    The field is deliberately NOT called ``natural``: ``AttackRolled.natural``
    is the KEPT die (the die the natural-20 crit / natural-1 fumble test
    reads), and two same-named fields meaning different things is a footgun.
    ``faces`` records both draws in order for advantage/disadvantage; ties
    retain the first face. A forced natural records its supplied face although
    it consumes no RNG. Directional source lists retain exact causes; the
    flattened ``sources`` tuple keeps historical event labels.
    """

    first: int
    kept: int
    modifier: int
    total: int
    mode: AdvantageMode
    sources: tuple[AdvantageSource, ...]
    faces: tuple[int, ...] = ()
    kept_index: int = 0
    forced: bool = False
    advantage_sources: tuple[AdvantageSource, ...] = ()
    disadvantage_sources: tuple[AdvantageSource, ...] = ()


def legacy_sources(sources: AdvantageSources) -> tuple[AdvantageSource, ...]:
    """Keep flattened event tokens stable while directional evidence names each rule."""

    def collapse(values: tuple[AdvantageSource, ...]) -> tuple[AdvantageSource, ...]:
        """Coalesce detailed condition names without dropping legacy contributions."""
        result: list[AdvantageSource] = []
        seen_condition_sides: set[AdvantageSource] = set()
        for value in values:
            if value.startswith("attacker:") or value.startswith("target:"):
                coarse: AdvantageSource = (
                    "condition:attacker" if value.startswith("attacker:") else "condition:target"
                )
                if coarse in seen_condition_sides:
                    continue
                seen_condition_sides.add(coarse)
                result.append(coarse)
            else:
                if value in ("condition:attacker", "condition:target"):
                    seen_condition_sides.add(value)
                result.append("trait" if value == "trait:MagicResistance" else value)
        return tuple(result)

    return collapse(sources.advantage) + collapse(sources.disadvantage)


def resolve_mode(s: AdvantageSources) -> AdvantageMode:
    """SRD 5.2 "Advantage and Disadvantage": any advantage + any
    disadvantage cancel to normal, regardless of how many of each apply."""
    if s.advantage and s.disadvantage:
        return "normal"
    if s.advantage:
        return "advantage"
    if s.disadvantage:
        return "disadvantage"
    return "normal"


def roll_d20_test(
    rng: random.Random,
    modifier: int,
    sources: AdvantageSources,
    *,
    forced_natural: int | None = None,
) -> D20Result:
    """Resolve one SRD 5.2 D20 Test.

    Normal mode draws exactly one d20; advantage/disadvantage draws
    exactly two and keeps the higher/lower. ``forced_natural`` bypasses
    the RNG entirely (zero draws) and is used by callers that need to
    pin the natural roll (e.g. deterministic crit/fumble scaffolding).
    """
    mode = resolve_mode(sources)
    applied = legacy_sources(sources)
    faces: tuple[int, ...]
    if forced_natural is not None:
        kept = first = int(forced_natural)
        faces = (first,)
    elif mode == "normal":
        kept = first = rng.randint(1, 20)
        faces = (first,)
    else:
        first, second = rng.randint(1, 20), rng.randint(1, 20)
        kept = max(first, second) if mode == "advantage" else min(first, second)
        faces = (first, second)
    kept_index = faces.index(kept)
    return D20Result(
        first,
        kept,
        modifier,
        kept + modifier,
        mode,
        applied,
        faces,
        kept_index,
        forced_natural is not None,
        sources.advantage,
        sources.disadvantage,
    )
