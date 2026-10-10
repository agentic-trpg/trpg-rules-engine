"""Typed commands and refusals, independent of combat execution/storage."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from dnd5e_engine.attack_riders import AttackRiderRequest
from dnd5e_engine.events import IntentType
from dnd5e_engine.movement import MovementMode
from dnd5e_engine.reactions import ReactionTriggerCompatibility
from dnd5e_engine.spatial import cell_id, parse_cell
from dnd5e_engine.spell_delivery import EffectSelection
from dnd5e_engine.types.checks import CheckRequest, HelpCheckSpec

GrantedDie = Literal["feature_grant:bardic-inspiration"]


class PlayerIntent(BaseModel):
    """A PC's submitted intent for the current turn.

    The seam carries the union of optional asset references the intent-
    to-IR resolver consumes. The orchestrator chooses the right slot by
    ``intent_type`` (e.g. ``"attack"`` consumes ``weapon_id``;
    ``"cast_spell"`` consumes ``spell_id``; ``"use_item"`` consumes
    ``item_id``); ``feature_id`` rides alongside for class-feature
    activations the cutover prompt extends the IntentType enum to
    surface.
    """

    model_config = ConfigDict(extra="forbid")
    action_grant: tuple[str, str] | None = Field(default=None, exclude_if=lambda v: v is None)

    intent_type: IntentType
    source_id: str | None = None
    check: CheckRequest | None = None
    help_check: HelpCheckSpec | None = None
    spell_id: str | None = None
    target_id: str | None = None
    # C17 — SRD 5.2 "one creature or several": per-instance targets for a spell
    # whose activity carries a ``target.affects.count`` formula (Magic Missile
    # darts, Hold Person's extra Humanoids; duplicates = several darts on one
    # creature), or the creatures an area of "up to N creatures" affects (Slow,
    # Mass Cure Wounds: at most N, no repeats). Ignored (with ``target_id`` used)
    # for activities without a count.
    target_ids: tuple[str, ...] | None = None
    effect_selections: tuple[EffectSelection, ...] = ()
    willing_target_ids: tuple[str, ...] = ()
    item_id: str | None = None
    weapon_id: str | None = None
    feature_id: str | None = None
    # SRD §Channel Divinity — the specific activity to resolve when a
    # USE_FEATURE names a multi-activity feature that is a repertoire of
    # ALTERNATIVES (Channel Divinity: Divine Spark Heal vs Save vs Turn Undead;
    # Cunning Strike's four options). Names one of the feature's activity ids.
    # ``None`` (the common case) leaves single-activity features unchanged and
    # keeps the safe no-op reject for a multi-activity feature (never guess).
    activity_id: str | None = None
    attack_riders: tuple[AttackRiderRequest, ...] = ()
    reckless_attack: bool = Field(default=False, strict=True)
    slot_level: int | None = None
    # Charges to spend on a variable-cost item invocation (wand upcast).
    # Validated by the use_item charge gate against consumption.scaling.
    charges_to_spend: int | None = Field(default=None, ge=1)
    # SRD 5.2 Lay on Hands: "draw power from the pool of healing to restore a
    # number of Hit Points to that creature, up to the maximum amount remaining
    # in the pool." The points a ``use_feature`` draws from an activity whose
    # own-pool cost scales by amount (Foundry ``consumption.scaling``): both its
    # ``@scaling`` value and what it spends. Omitted → 1. Refused with
    # ``CastFailed(reason="invalid_charge_spend")`` on an activity that doesn't
    # scale by amount, or above the points left. Ignored by other intent types.
    pool_points: int | None = Field(default=None, ge=1)
    # SRD 5.2 Bardic Inspiration: "Once within the next hour when the creature
    # fails a D20 Test, the creature can roll the Bardic Inspiration die and add
    # the number rolled to the d20, potentially turning the failure into a
    # success." On an ``attack``: roll the die the attacker holds if the attack
    # roll misses — a hit or a natural 1 keeps it banked. No die to roll →
    # ``AttackFailed(reason="no_granted_die")`` before anything is spent.
    # Ignored by other intent types (saves and checks carry no such choice).
    redeem_granted_die: GrantedDie | None = None
    # Deprecated input compatibility only. Canonical reaction_conditions own
    # the trigger set; a supplied alias must match that existing set.
    reaction_trigger: ReactionTriggerCompatibility | None = None
    # SRD §Movement — the destination cell id (``cell_id(col, row)``) of a
    # ``"move"``; also the space a conjuration names (Spiritual Weapon's force,
    # Summon Dragon's spirit).
    target_zone_id: str | None = None
    target_object_id: str | None = None
    object_include_origin: bool = Field(default=False, strict=True)
    movement_mode: MovementMode = "walk"

    @field_validator("target_zone_id")
    @classmethod
    def _canonical_target_zone(cls, value: str | None) -> str | None:
        return None if value is None else cell_id(*parse_cell(value))

    # C16 — SRD 5.2 §Areas of Effect: a Cone / Line / Cube "extends … in a
    # direction its creator chooses". Grid offset vector ``(dcol, drow)``; only
    # the sign of each component matters (one of the 8 grid directions). When
    # omitted for a directional template the orchestrator aims from the caster
    # through ``target_id``. Ignored for sphere / cylinder and non-AoE intents.
    direction: tuple[int, int] | None = None
    # SRD 5.2 "Each creature of your choice in a 5-foot-radius Sphere" — the
    # creatures an area of your choice spares. ``None`` (the default) spares
    # the actor's allies and the actor itself when the area harms (a save or
    # damage), and its enemies when it helps; ``()`` spares nobody, so the
    # actor opts itself in. Refused with ``target_invalid`` before anything is
    # spent when an id is not in the combat, or when an attack, cast, item use
    # or feature use resolves no area of your choice. Other intents ignore it.
    excluded_target_ids: tuple[str, ...] | None = None
    # SRD §Combat — Dash / Disengage budget choice. False → Action (default).
    # True → Bonus Action: for ``dash`` and ``disengage`` only with Cunning
    # Action among the granted features (SRD 5.2 Rogue 2), else
    # ``IntentRejectedError("no_action_economy")``. Carried from
    # ``ParsedIntent.use_bonus_action``.
    # On an Unarmed Strike ``attack`` by an attacker whose Martial Arts is
    # active it asks for SRD 5.2's "Bonus Unarmed Strike. You can make an
    # Unarmed Strike as a Bonus Action."; without Martial Arts it changes
    # nothing there (an Attack-action swing).
    use_bonus_action: bool = False
    # SRD 5.2 Unarmed Strike — Shove: "you either push it 5 feet away or
    # cause it to have the Prone condition" — the shover's pre-declared
    # choice (no player-facing choice prompt exists at this seam). False
    # (default) -> Prone; True -> a 5-ft forced push via ``push_combatant``.
    # Duck-typed hosts that never set this field keep the default Prone
    # behaviour unaffected.
    shove_push: bool = False
    # SRD 5.2 Versatile property — "The weapon deals that damage when used
    # with two hands to make a melee attack." The attacker's pre-declared
    # grip choice for THIS attack (no player-facing choice prompt exists at
    # this seam). False (default) keeps the one-handed die. Ignored unless
    # the weapon carries ``WeaponProperty.VERSATILE`` and the attack is an
    # actual melee swing (a two-handed grip declared on a thrown/ranged use
    # of the same weapon is ignored, per SRD "to make a melee attack").
    two_handed: bool = False
    # SRD 5.2 §Rituals: "To cast a spell as a Ritual, a spellcaster must have
    # it prepared" — the Ritual version "takes 10 minutes longer to cast than
    # normal, but it doesn't expend a spell slot." The turn economy has no
    # room to host that extra 10 minutes, so in combat this flag is a hard
    # reject (``CastFailed(reason="ritual_in_combat")``, slot untouched).
    # Out-of-combat rituals resolve via ``spellcasting.resolve_ritual_cast``.
    as_ritual: bool = False
    # SRD 5.2 Wild Shape: "you shape-shift into a Beast form that you have
    # learned for this feature"; Polymorph: "That form can be any Beast you
    # choose that has a Challenge Rating equal to or less than the target's".
    # The corpus monster slug of the chosen form, for a ``use_feature`` of
    # Wild Shape or a ``cast_spell`` of Polymorph; a missing or illegal form is
    # refused with ``CastFailed(reason="invalid_form")`` before anything is
    # spent. Ignored by other intents.
    form_id: str | None = None
    # SRD 5.2 Wild Shape: "Your game statistics are replaced by the Beast's
    # stat block". An action slug on the actor's current stat block (its Beast
    # form, or a monster's own): an ``attack`` that makes one attack with that
    # action instead of a weapon. An action the stat block lacks, or one that
    # makes no attack roll, is refused with
    # ``AttackFailed(reason="action_unavailable")``. Ignored by other intents.
    stat_block_action_id: str | None = None

    @field_validator("direction")
    @classmethod
    def _direction_nonzero(cls, value: tuple[int, int] | None) -> tuple[int, int] | None:
        if value is not None and value == (0, 0):
            raise ValueError("direction must be a nonzero grid vector")
        return value

    @field_validator("excluded_target_ids")
    @classmethod
    def _excluded_ids_distinct(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is not None and ("" in value or len(set(value)) != len(value)):
            raise ValueError("excluded_target_ids must be distinct, non-empty ids")
        return value


class CombatSeamError(Exception):
    """Base class for typed errors raised by the public combat seam."""


class IntentRejectedError(CombatSeamError):
    """Raised when ``submit_player_intent`` rejects an intent.

    Carries a typed ``reason`` so callers can branch on the rejection
    cause without re-parsing the error message.
    """

    RejectionReason = Literal[
        "spell_required",
        "action_restricted",
        "invalid_spell_activation",
        "actor_not_in_initiative",
        "not_actor_turn",
        "combat_ended",
        "no_action_economy",
        "actor_incapacitated",
        "target_invalid",
        # SRD 5.2 Unarmed Strike — Grapple/Shove: the target must be within
        # reach (5 ft). Mirrors ``CastFailedReason``'s "out_of_range" but as
        # a direct raise (like Help's "target_invalid") since neither option
        # has a dedicated ``...Failed`` event.
        "out_of_range",
        # SRD 5.2 Prone, Restricted Movement — ``stand_up`` has no dedicated
        # ``...Failed`` event (like Grapple/Help above), so its two failure
        # modes raise directly. Mirrors ``MoveFailed.reason``'s identically
        # named members (a separate Literal on the move-intent event) —
        # same SRD rule, different seam.
        "speed_zero",
        "insufficient_movement",
        # C18 §Monster action economy — ``advance_monster_turn(legendary=True)``
        # when no encounter member currently qualifies (see
        # ``_eligible_legendary_actor``).
        "no_legendary_action",
        # C18 §Monster action economy — ``resolve_legendary_resistance``
        # when ``entity_id`` is not an encounter member with the Legendary
        # Resistance trait, or has no unarmed use left in its per-day pool.
        "no_legendary_resistance",
    ]

    def __init__(self, reason: RejectionReason, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail
