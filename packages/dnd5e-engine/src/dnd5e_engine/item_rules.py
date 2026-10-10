"""Shared typed item charge pricing, independent of combat execution."""

import logging
from typing import Any

from dnd5e_engine.rest import ITEM_USE_COUNTER_PREFIX

_LOGGER = logging.getLogger(__name__)


def item_use_counter_key(item_id: str) -> str:
    """The ``custom_counters`` sidecar key namespacing an item's charge tally."""
    return f"{ITEM_USE_COUNTER_PREFIX}{item_id}"


def activity_item_use_cost(item_slug: str, activity: Any) -> int:
    """Positive literal ``itemUses`` cost of ONE activity.

    Symbolic or negative targets (``-3d4``, ``-@item.uses.spent``,
    ``@item.uses.max``) are recharge/whole-pool semantics, not a spend
    cost — skipped, never coerced.
    """
    cost = 0
    for target in activity.consumption.targets:
        if target.type != "itemUses":
            continue
        try:
            value = int(str(target.value).strip())
        except ValueError:
            _LOGGER.info("item_charge_target_symbolic slug=%s value=%r", item_slug, target.value)
            continue
        if value > 0:
            cost += value
    return cost
