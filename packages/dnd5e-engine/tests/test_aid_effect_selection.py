"""Select Aid's effective identity while preserving suppressed applications."""

from dnd5e_srd_data.loader import BundledAssetLoader

from dnd5e_engine.evaluation_aid import (
    admit_aid_overlap,
    aid_potency,
    aid_profile,
    paired_hp_transition,
)
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.rules.effects import strongest_latest


def test_equal_strength_latest_identity_and_stronger_winner():
    spell = BundledAssetLoader().get_spell("aid")
    assert spell is not None
    _, first = aid_profile(spell, level=2, target_id="target", caster_id="caster")
    later = admit_aid_overlap(first, (effect_state(first),), "second-cast")
    weak = (effect_state(first), effect_state(later))
    assert weak[0] != weak[1]
    assert strongest_latest(weak, aid_potency) == weak[1]
    _, strong = aid_profile(spell, level=3, target_id="target", caster_id="other")
    assert strongest_latest((*weak, effect_state(strong)), aid_potency) == effect_state(strong)
    assert strongest_latest((effect_state(strong), *weak), aid_potency) == effect_state(strong)


def test_paired_hp_transition_keeps_intervening_damage():
    assert paired_hp_transition(8, 20, 10, 5).model_dump() == {
        "expected_current": 8,
        "expected_maximum": 20,
        "current": 3,
        "maximum": 15,
        "old_bonus": 10,
        "new_bonus": 5,
    }
    # Base 12/20 becomes 22/30, then independently loses 9 and heals 4.
    weaker = paired_hp_transition(17, 30, 10, 5)
    assert (weaker.current, weaker.maximum) == (12, 25)
    base = paired_hp_transition(weaker.current, weaker.maximum, 5, 0)
    assert (base.current, base.maximum) == (7, 20)
