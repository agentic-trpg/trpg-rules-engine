"""Pure creature-size qualifiers; size does not determine a spatial footprint."""

from __future__ import annotations

from dnd5e_srd_data.schema.advancement import AdvancementType
from dnd5e_srd_data.schema.monster import CreatureSize
from dnd5e_srd_data.schema.species import Species

_SIZES = tuple(CreatureSize)
_FOUNDRY_SIZES = dict(zip(("tiny", "sm", "med", "lg", "huge", "grg"), _SIZES, strict=True))


def size_rank(size: CreatureSize) -> int:
    return _SIZES.index(size)


def one_size_larger(size: CreatureSize) -> CreatureSize | None:
    rank = size_rank(size) + 1
    return _SIZES[rank] if rank < len(_SIZES) else None


def two_or_more_sizes_apart(first: CreatureSize, second: CreatureSize) -> bool:
    return abs(size_rank(first) - size_rank(second)) >= 2


def target_at_most_one_size_larger(attacker: CreatureSize, target: CreatureSize) -> bool:
    return size_rank(target) <= size_rank(attacker) + 1


def size_at_most(actual: CreatureSize, maximum: CreatureSize) -> bool:
    return size_rank(actual) <= size_rank(maximum)


def species_size_options(species: Species) -> tuple[CreatureSize, ...]:
    """Read only canonical Size advancement codes, never species prose."""
    entries = [entry for entry in species.advancement if entry.type == AdvancementType.SIZE]
    if not entries:
        return (species.size,)
    options: list[CreatureSize] = []
    for entry in entries:
        codes = entry.configuration.get("sizes")
        if not isinstance(codes, list) or not codes:
            raise ValueError(f"species {species.slug!r} has an invalid Size advancement")
        for code in codes:
            if not isinstance(code, str) or code not in _FOUNDRY_SIZES:
                raise ValueError(f"species {species.slug!r} has an unsupported size code: {code!r}")
            size = _FOUNDRY_SIZES[code]
            if size not in options:
                options.append(size)
    return tuple(options)


def resolve_species_size(species: Species, choice: CreatureSize | None) -> CreatureSize:
    options = species_size_options(species)
    if choice is None:
        if len(options) != 1:
            raise ValueError(f"species {species.slug!r} requires an explicit size_choice")
        return options[0]
    if choice not in options:
        raise ValueError(f"size_choice {choice.value!r} is not allowed by species {species.slug!r}")
    return choice
