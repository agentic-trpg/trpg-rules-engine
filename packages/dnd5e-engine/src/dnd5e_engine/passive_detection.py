"""Bound D&D 2024 rules for passive detection on current World facts."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from dnd5e_srd_data.schema.item import Armor, Item, MagicItem, Weapon

DetectionSource = Literal["spell_effect", "magic_item"]
BarrierMaterial = Literal["stone", "dirt", "wood", "metal", "lead"]


@dataclass(frozen=True, slots=True)
class DetectionBarrier:
    """Known barrier material and thickness in thousandths of an inch."""

    material: BarrierMaterial
    thickness_mils: int

    def __post_init__(self):
        """Reject unsupported material or missing/nonpositive thickness."""
        if self.material not in ("stone", "dirt", "wood", "metal", "lead") or (
            type(self.thickness_mils) is not int or self.thickness_mils <= 0
        ):
            raise ValueError("Barrier needs a supported material and positive integer mils")


@dataclass(frozen=True, slots=True)
class PassiveDetectionProfile:
    """Identify the supported passive capability and its maximum reach in feet."""

    spell_id: str
    clue: str
    range_ft: int

    def detects(
        self,
        source: DetectionSource,
        distance_ft: int,
        barriers: Iterable[DetectionBarrier | None],
    ) -> bool:
        """Use D&D 2024 thresholds, withholding a clue when a crossed barrier is unknown."""
        return (
            source in ("spell_effect", "magic_item")
            and 0 <= distance_ft <= self.range_ft
            and all(
                barrier is not None
                and barrier.material != "lead"
                and barrier.thickness_mils < (1_000 if barrier.material == "metal" else 12_000)
                for barrier in barriers
            )
        )


DETECT_MAGIC = PassiveDetectionProfile("detect-magic", "magic_present", 30)
PASSIVE_DETECTIONS = (DETECT_MAGIC,)


def is_magic_item(item: Item | None) -> bool:
    """Classify a ruleset item using structured magic properties, independent of name or rarity."""
    return isinstance(item, MagicItem) or (isinstance(item, (Armor, Weapon)) and item.magical)
