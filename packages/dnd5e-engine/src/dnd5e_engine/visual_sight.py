"""Pure creature sight over the existing grid geometry and typed visual grants."""

from dnd5e_engine.activities.passive_stats import CombatantSenses
from dnd5e_engine.spatial import GridTopology

SEE_INVISIBLE_KEY = "vision.see_invisible"


def can_see_creature(
    topology: GridTopology,
    origin: str,
    target: str,
    senses: CombatantSenses,
    *,
    blinded: bool = False,
    invisible: bool = False,
    can_see_invisible: bool = False,
) -> bool:
    """Apply observer and target facts after the grid's own sight and cover rules.

    Blindsight works while blinded. Truesight and the See Invisibility grant
    remain optical, so neither restores sight to a blinded observer.
    """
    if not topology.can_see(origin, target, senses):
        return False
    blind = senses.blindsight is not None and topology.within_range(
        origin, target, senses.blindsight
    )
    if blinded:
        return blind
    true = senses.truesight is not None and topology.within_range(origin, target, senses.truesight)
    return not invisible or blind or true or can_see_invisible
