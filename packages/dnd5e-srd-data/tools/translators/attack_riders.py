"""Reviewed feature/activity attack bindings; runtime never interprets prose.

Inventory was checked across the entire canonical feature corpus, including
empty activation conditions and foundations with no activities. Deferral is
per activity/option, preserving every clause instead of executing fragments.
"""

from dnd5e_srd_data.schema.common import Activity, PassiveEffect, PassiveEffectChange
from dnd5e_srd_data.schema.feature import (
    AttackRiderSemantics,
    RiderEffectSpec,
    RiderForcedMovement,
)
from dnd5e_srd_data.schema.monster import CreatureSize
from tools.translators.effect_lifecycle import reviewed_effect_lifecycle

_RECKLESS = (
    "requires first-attack Reckless decision, Strength Advantage and incoming Advantage lifecycle"
)
_RIDERS: dict[tuple[str, str], AttackRiderSemantics] = {
    ("sneak-attack", "a1T6nHaqmvbLpyJr"): AttackRiderSemantics(
        trigger="final_hit",
        qualification="finesse_or_ranged",
        phase="damage_preparation",
        once_per_turn=True,
        inherit_damage_type=True,
        automatic=True,
    ),
    ("stunning-strike", "Xto99a8Zt46VLwaR"): AttackRiderSemantics(
        trigger="final_hit",
        qualification="monk_weapon_or_unarmed",
        phase="final_hit_before_damage",
        once_per_turn=True,
        effects=(
            RiderEffectSpec(
                effect_id="vofnieSTB0l8rpRg", outcome="failure", expiry="source_next_turn_start"
            ),
            RiderEffectSpec(
                effect_id="cj9HhBNKtF6iOsH4", outcome="success", expiry="source_next_turn_start"
            ),
        ),
    ),
    ("open-hand-technique", "1jdSaWanuRrdkVs3"): AttackRiderSemantics(
        trigger="flurry_hit",
        qualification="flurry_unarmed",
        phase="after_damage",
        choice_group="open-hand-technique",
        effects=(RiderEffectSpec(effect_id="uQ474o5Wsv3sEAJK", expiry="target_next_turn_start"),),
    ),
    ("open-hand-technique", "XoaS0RtDCGAqrQsf"): AttackRiderSemantics(
        trigger="flurry_hit",
        qualification="flurry_unarmed",
        phase="after_damage",
        choice_group="open-hand-technique",
        forced_movement=RiderForcedMovement(max_distance_ft=15),
    ),
    ("open-hand-technique", "5Qgc0K3TfuonkPIG"): AttackRiderSemantics(
        trigger="flurry_hit",
        qualification="flurry_unarmed",
        phase="after_damage",
        choice_group="open-hand-technique",
        effects=(RiderEffectSpec(effect_id="E1A8QE6kPsLgqTAP", outcome="failure"),),
    ),
    ("cunning-strike", "n64fvJMT9fPUy7DH"): AttackRiderSemantics(
        trigger="sneak_attack_damage",
        qualification="finesse_or_ranged",
        phase="after_damage",
        choice_group="cunning-strike",
        sneak_dice_cost=1,
        requires_carried_items=("poisoners-kit",),
        effects=(
            RiderEffectSpec(
                effect_id="RazTM6biKVtNtjEW",
                outcome="failure",
                lifecycle=reviewed_effect_lifecycle(
                    "feature", "cunning-strike", "n64fvJMT9fPUy7DH", "RazTM6biKVtNtjEW"
                ),
            ),
        ),
    ),
    ("cunning-strike", "dWcCw1vTWRMx4YzD"): AttackRiderSemantics(
        trigger="sneak_attack_damage",
        qualification="finesse_or_ranged",
        phase="after_damage",
        choice_group="cunning-strike",
        sneak_dice_cost=1,
        target_size_max=CreatureSize.LARGE,
        effects=(RiderEffectSpec(effect_id="La47n2N3VtECtnA9", outcome="failure"),),
    ),
    ("cunning-strike", "jR7KqMuPOZYUCDyO"): AttackRiderSemantics(
        trigger="sneak_attack_damage",
        qualification="finesse_or_ranged",
        phase="damage_preparation",
        inherit_damage_type=True,
        deferred_reason=(
            "legacy Cunning Sneak Attack damage helper is replaced by authoritative dice sacrifice"
        ),
    ),
    ("cunning-strike", "m2bRZ1YeD3yf9nV7"): AttackRiderSemantics(
        trigger="sneak_attack_damage",
        qualification="finesse_or_ranged",
        phase="after_damage",
        choice_group="cunning-strike",
        sneak_dice_cost=1,
        deferred_reason=(
            "requires immediate selected half-Speed movement with OA "
            "exemption limited to that movement"
        ),
    ),
    ("devious-strikes", "4TnBjQTJzt9UjUos"): AttackRiderSemantics(
        trigger="sneak_attack_damage",
        qualification="finesse_or_ranged",
        phase="after_damage",
        choice_group="cunning-strike",
        sneak_dice_cost=2,
        deferred_reason=(
            "requires authoritative move/action/Bonus Action mutual restriction on next turn"
        ),
    ),
    ("devious-strikes", "3eq7lcmpkJJBU2KO"): AttackRiderSemantics(
        trigger="sneak_attack_damage",
        qualification="finesse_or_ranged",
        phase="after_damage",
        choice_group="cunning-strike",
        sneak_dice_cost=6,
        effects=(
            RiderEffectSpec(
                effect_id="C2IGgt4PnRMxZVey",
                outcome="failure",
                lifecycle=reviewed_effect_lifecycle(
                    "feature", "devious-strikes", "3eq7lcmpkJJBU2KO", "C2IGgt4PnRMxZVey"
                ),
            ),
        ),
    ),
    ("devious-strikes", "ki4lIPVGNA0HjEzH"): AttackRiderSemantics(
        trigger="sneak_attack_damage",
        qualification="finesse_or_ranged",
        phase="after_damage",
        choice_group="cunning-strike",
        sneak_dice_cost=3,
        effects=(
            RiderEffectSpec(
                effect_id="Fb1n6Yk2MlOAckf0", outcome="failure", expiry="target_next_turn_end"
            ),
        ),
    ),
    ("brutal-strike", "nN5gsB6AcSQ4uQPN"): AttackRiderSemantics(
        trigger="reckless_hit",
        qualification="strength",
        phase="damage_preparation",
        choice_group="brutal-strike",
        inherit_damage_type=True,
        deferred_reason=_RECKLESS,
        deferred_options={
            "forceful-blow": (
                f"{_RECKLESS}; also requires selected straight-toward "
                "half-Speed follow movement without OA"
            ),
            "hamstring-blow": (
                f"{_RECKLESS}; latest-only speed reduction awaits complete foundation"
            ),
        },
    ),
    ("improved-brutal-strike", "UmRlsf4QWW98I4FS"): AttackRiderSemantics(
        trigger="reckless_hit",
        qualification="strength",
        phase="after_damage",
        choice_group="brutal-strike",
        deferred_reason=(
            f"{_RECKLESS}; also requires next attack by another creature +5 consumption"
        ),
    ),
    ("improved-brutal-strike", "I30qGlPDcyKwz65H"): AttackRiderSemantics(
        trigger="reckless_hit",
        qualification="strength",
        phase="after_damage",
        choice_group="brutal-strike",
        deferred_reason=f"{_RECKLESS}; also requires next-save Disadvantage consumption",
    ),
}

# Other reviewed attack-triggered corpus activities remain explicitly deferred.
_DEFERRED: tuple[tuple[str, str, str, str], ...] = (
    (
        "blessed-strikes-divine-strike",
        "uYVz8MBW0EZ2X0zD",
        "any_weapon",
        "requires on-own-turn qualification and validated Divine Strike choice/scaling",
    ),
    (
        "blessed-strikes-divine-strike",
        "MbCGfaQAeW2rzNWb",
        "any_weapon",
        "requires on-own-turn qualification and validated Divine Strike choice/scaling",
    ),
    (
        "elemental-fury-primal-strike",
        "lHdiTksQmeJBFrf7",
        "any_weapon",
        "requires weapon-or-Wild-Shape qualification, on-own-turn gate and elemental damage choice",
    ),
    (
        "fires-burn",
        "002cv19dj8WGzSl2",
        "any_attack",
        "requires positive triggering damage and linked species resource damage rider",
    ),
    (
        "frosts-chill",
        "6goWQaWLexUqgig8",
        "any_attack",
        "requires positive triggering damage and linked species resource damage/speed rider",
    ),
    (
        "hills-tumble",
        "I2wKOUDxhIb5hHb7",
        "any_attack",
        "requires positive triggering damage and linked species resource",
    ),
    (
        "hunters-prey",
        "Ek6m8ZY5Df7Mp6DO",
        "any_weapon",
        "requires validated Colossus Slayer option and target missing-HP qualification",
    ),
    (
        "eldritch-smite",
        "CXJlzDUkMYU9w9i9",
        "pact_weapon",
        "requires authoritative pact-weapon binding and Pact Magic slot",
    ),
    (
        "lifedrinker",
        "LDIKT3JVs25qBVuL",
        "pact_weapon",
        "requires authoritative pact-weapon binding and selected damage type",
    ),
    (
        "lifedrinker",
        "emx17h6kO4xmSqC6",
        "pact_weapon",
        "requires pact-weapon binding, typed Hit Die spend and linked damage/healing choice",
    ),
    (
        "lifedrinker",
        "oMHxbcnQh38wZEKU",
        "pact_weapon",
        "requires pact-weapon binding, typed Hit Die spend and linked damage/healing choice",
    ),
    (
        "hurl-through-hell",
        "vIY5Deb2vCh5POdP",
        "any_attack",
        "requires banishment/return lifecycle and Fiend damage exclusion",
    ),
    (
        "quivering-palm",
        "O1mQr2rPpzRpB3yJ",
        "unarmed",
        "requires one-target vibration binding, plane identity "
        "and later selected release lifecycle",
    ),
    (
        "quivering-palm",
        "lBn87Q4yyJXWzLqg",
        "unarmed",
        "requires previously bound vibration target and selected "
        "Attack-action replacement or Action release",
    ),
    (
        "repelling-blast",
        "OXhI1TDQxORrGAgc",
        "spell_attack",
        "requires validated selected cantrip invocation binding",
    ),
)
_DEFERRED_TARGET_SIZES = {
    ("hills-tumble", "I2wKOUDxhIb5hHb7"): CreatureSize.LARGE,
    ("eldritch-smite", "CXJlzDUkMYU9w9i9"): CreatureSize.HUGE,
    ("repelling-blast", "OXhI1TDQxORrGAgc"): CreatureSize.LARGE,
}
for _slug, _activity, _qualification, _reason in _DEFERRED:
    _RIDERS[(_slug, _activity)] = AttackRiderSemantics.model_validate(
        {
            "trigger": "final_hit",
            "qualification": _qualification,
            "phase": "after_damage",
            "target_size_max": _DEFERRED_TARGET_SIZES.get((_slug, _activity)),
            "deferred_reason": _reason,
        }
    )
_RIDERS[("frenzy", "myPBq8xozti108Mc")] = AttackRiderSemantics(
    trigger="reckless_hit",
    qualification="strength",
    phase="damage_preparation",
    inherit_damage_type=True,
    deferred_reason=(
        f"{_RECKLESS}; also requires active Rage and first qualifying target per own turn"
    ),
)

_CONTEXTS: dict[str, AttackRiderSemantics] = {
    "reckless-attack": AttackRiderSemantics(
        trigger="reckless_hit",
        qualification="strength",
        phase="damage_preparation",
        deferred_reason=_RECKLESS,
    ),
    "improved-brutal-strike-2": AttackRiderSemantics(
        trigger="reckless_hit",
        qualification="strength",
        phase="damage_preparation",
        choice_group="brutal-strike",
        deferred_reason=f"{_RECKLESS}; scales damage and permits two distinct options",
    ),
    "improved-cunning-strike": AttackRiderSemantics(
        trigger="sneak_attack_damage",
        qualification="finesse_or_ranged",
        phase="damage_preparation",
        choice_group="cunning-strike",
        deferred_reason=(
            "choice-capacity modifier; consumed by owned concrete "
            "riders rather than standalone execution"
        ),
    ),
    "blessed-strikes": AttackRiderSemantics(
        trigger="final_hit",
        qualification="any_weapon",
        phase="after_damage",
        deferred_reason=(
            "choice container; concrete Divine Strike options carry separate activities"
        ),
    ),
    "elemental-fury": AttackRiderSemantics(
        trigger="final_hit",
        qualification="any_weapon",
        phase="after_damage",
        deferred_reason=(
            "choice container; concrete Primal Strike option carries a separate activity"
        ),
    ),
    "giant-ancestry": AttackRiderSemantics(
        trigger="final_hit",
        qualification="any_attack",
        phase="after_damage",
        deferred_reason=(
            "species choice container; concrete Fire/Frost/Hill options carry separate activities"
        ),
    ),
    "radiant-strikes": AttackRiderSemantics(
        trigger="final_hit",
        qualification="melee_weapon_or_unarmed",
        phase="damage_preparation",
        deferred_reason=(
            "passive damage projection exists; complete inherited "
            "melee-weapon versus unarmed qualification is not a declared rider"
        ),
    ),
}
_CONTEXTS = {
    slug: context.model_copy(
        update={"inventory_role": "passive" if slug == "radiant-strikes" else "foundation"}
    )
    for slug, context in _CONTEXTS.items()
}
_CONTEXTS.update(
    {
        "monks-focus": AttackRiderSemantics(
            trigger="flurry_hit",
            qualification="flurry_unarmed",
            phase="after_damage",
            inventory_role="producer",
            related_activity_ids=("2ghJTBhilLrFn9xT",),
            deferred_reason=(
                "Flurry attack-funding producer; concrete hit options live on Open Hand Technique"
            ),
        ),
        "heightened-focus": AttackRiderSemantics(
            trigger="flurry_hit",
            qualification="flurry_unarmed",
            phase="after_damage",
            inventory_role="producer",
            related_activity_ids=("XLma1NEOJNEBD6mH",),
            deferred_reason=(
                "Flurry funding upgrade; full Heightened Focus shared effects remain deferred"
            ),
        ),
        "defensive-tactics": AttackRiderSemantics(
            trigger="final_hit",
            qualification="any_attack",
            phase="after_damage",
            inventory_role="defensive",
            related_activity_ids=("ioagLQG0o1axCBtr",),
            deferred_reason=(
                "incoming-hit defense requires attacker-specific remaining-turn Disadvantage"
            ),
        ),
        "deflect-attacks": AttackRiderSemantics(
            trigger="final_hit",
            qualification="any_attack",
            phase="final_hit_before_damage",
            inventory_role="defensive",
            related_activity_ids=("rQwRKkuZ7WhnB7v7", "dJv36KHyIVsbD00p"),
            deferred_reason=(
                "incoming-hit reaction requires damage reduction, "
                "damage-type qualification and redirected damage lifecycle"
            ),
        ),
        "deflect-energy": AttackRiderSemantics(
            trigger="final_hit",
            qualification="any_attack",
            phase="final_hit_before_damage",
            inventory_role="defensive",
            related_activity_ids=("F7NbTRmkUTDDZxj7", "AnEiO80Ga3etD595"),
            deferred_reason=(
                "incoming-hit reaction requires damage reduction "
                "and redirected elemental damage lifecycle"
            ),
        ),
        "uncanny-dodge": AttackRiderSemantics(
            trigger="final_hit",
            qualification="any_attack",
            phase="final_hit_before_damage",
            inventory_role="defensive",
            related_activity_ids=("7krJIcGhZl9RqzYl",),
            deferred_reason=(
                "incoming-hit reaction requires attacker visibility "
                "and triggering damage-instance halving"
            ),
        ),
        "sacred-weapon": AttackRiderSemantics(
            trigger="final_hit",
            qualification="any_weapon",
            phase="damage_preparation",
            inventory_role="passive",
            related_activity_ids=("Ew70lNTD8dR3vREt",),
            deferred_reason=(
                "pre-attack weapon enchantment requires bound "
                "melee weapon and per-hit Radiant damage choice"
            ),
        ),
        "superior-hunters-prey": AttackRiderSemantics(
            trigger="final_hit",
            qualification="any_attack",
            phase="after_damage",
            inventory_role="passive",
            related_activity_ids=("yqOAxbsX16RkOx06",),
            deferred_reason=(
                "requires attributed Hunter's Mark triggering damage "
                "and chosen second visible target"
            ),
        ),
        "foe-slayer": AttackRiderSemantics(
            trigger="final_hit",
            qualification="any_attack",
            phase="damage_preparation",
            inventory_role="passive",
            related_activity_ids=("GG4mc3Bw4ArJEUJY",),
            deferred_reason=(
                "Hunter's Mark damage-die upgrade requires active marked-target binding"
            ),
        ),
        "remarkable-athlete": AttackRiderSemantics(
            trigger="final_hit",
            qualification="any_attack",
            phase="after_damage",
            inventory_role="passive",
            deferred_reason=(
                "critical-hit movement requires a selected half-Speed "
                "post-hit movement grant without Opportunity Attacks"
            ),
        ),
    }
)


def attack_riders(slug: str, activity_ids: list[str]) -> dict[str, AttackRiderSemantics]:
    return {
        activity_id: _RIDERS[(slug, activity_id)].model_copy(deep=True)
        for activity_id in activity_ids
        if (slug, activity_id) in _RIDERS
    }


def attack_rider_context(slug: str) -> AttackRiderSemantics | None:
    context = _CONTEXTS.get(slug)
    return context.model_copy(deep=True) if context is not None else None


def attack_rider_choice_limits(slug: str) -> dict[str, int]:
    if slug == "improved-cunning-strike":
        return {"cunning-strike": 2}
    if slug == "improved-brutal-strike-2":
        return {"brutal-strike": 2}
    return {}


def attack_rider_activities(slug: str, activities: list[Activity]) -> list[Activity]:
    if slug != "stunning-strike":
        return activities
    return [
        activity.model_copy(
            update={
                "effects": [
                    ref.model_copy(update={"on_save": ref.id == "cj9HhBNKtF6iOsH4"})
                    if ref.id in ("cj9HhBNKtF6iOsH4", "vofnieSTB0l8rpRg")
                    else ref
                    for ref in getattr(activity, "effects", ())
                ]
            }
        )
        if activity.id == "Xto99a8Zt46VLwaR"
        else activity
        for activity in activities
    ]


def attack_rider_effects(slug: str, effects: list[PassiveEffect]) -> list[PassiveEffect]:
    changes = {
        ("stunning-strike", "cj9HhBNKtF6iOsH4"): (
            PassiveEffectChange(key="speed.multiplier", mode=1, value="0.5"),
            PassiveEffectChange(key="flags.attack.next_advantage", mode=5, value="true"),
        ),
        ("open-hand-technique", "uQ474o5Wsv3sEAJK"): (
            PassiveEffectChange(key="flags.cannot_make_opportunity_attacks", mode=5, value="true"),
        ),
    }
    return [
        effect.model_copy(
            update={
                "changes": [
                    *effect.changes,
                    *(
                        change.model_copy(deep=True)
                        for change in additions
                        if change not in effect.changes
                    ),
                ]
            }
        )
        if (additions := changes.get((slug, effect.id))) is not None
        and not all(change in effect.changes for change in additions)
        else effect
        for effect in effects
    ]
