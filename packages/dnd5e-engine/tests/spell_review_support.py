"""Explicit reviewed variants for tests of mechanisms beyond admission."""

from __future__ import annotations

from types import MappingProxyType

import pytest
from dnd5e_srd_data.schema.spell import Spell

from dnd5e_engine import spell_execution


def review_spell_variant(monkeypatch: pytest.MonkeyPatch, spell: Spell) -> None:
    """Declare one intentional fixture variant reviewed, preserving its contract.

    Public execution correctly refuses changed copied identities. Tests of
    target selection/counting deliberately use noncanonical target carriers;
    give those exact fixtures separate review evidence instead of bypassing
    runtime admission or relying on the canonical spell's stale digest.
    """
    reviews = dict(spell_execution.spell_reviews())
    review = reviews[spell.foundry_uuid]
    assert review.slug == spell.slug
    assert [(a.id, a.kind) for a in spell.activities] == [
        (a.activity_id, a.kind) for a in review.activities
    ]
    reviews[spell.foundry_uuid] = review.model_copy(
        update={"canonical_sha256": spell_execution.semantic_digest(spell)}
    )
    monkeypatch.setattr(spell_execution, "spell_reviews", lambda: MappingProxyType(reviews))
