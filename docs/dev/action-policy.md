# Effect-owned action policies

Batch B5 adds a closed `ActionPolicy` to canonical passive effects and the
existing runtime `ActiveEffect`. Effective, enabled effects project permissions
using the existing latest-applies stacking contract. No execution branch reads
a spell name or description. Source-reviewed ingestion attaches Slow's policy;
the existing semantic digest includes the policy and lifecycle data.

## Shared permissions and payment

Policies can deny Actions, Bonus Actions or Reactions, exclude Action and Bonus
Action use on the same owner turn, and cap an Attack Action at one attack.
Existing Combatant budgets remain authoritative. Four small fields record
Action/Bonus use, attacks in the current Attack Action and spent full effect
identities; `TurnStarted` resets them. Public feature/item and ongoing Magic
paths share these permissions. Monster execution and the common Reaction gate
consume the same projection. Refusals are typed `action_restricted` errors
before payment, events and RNG.

The cap applies to each **Attack Action**. Action Surge can purchase a new
one-attack Action, retaining its existing prohibition on Magic. Nick and Cleave
cannot add another attack inside the capped Action. Flurry is a Bonus Action:
its paid strikes remain legal when the creature has taken no Action that turn.
The separate stat-block Multiattack Action retains its existing declared
activities; it is not the Attack Action. Monster AI currently chooses one
ordinary Action and does not autonomously combine Bonus Actions.

An explicit `PlayerIntent.action_grant=(effect_id, origin)` selects an enabled,
effective effect on the actor. Each source grants at most one Action per owner
turn. Attack performs one swing without changing the ordinary Extra Attack or
Action Surge ledger; Dash, Disengage and Hide reuse their existing handlers.
Missing, expired, suppressed, spent or incompatible sources refuse before
payment. Slow's exclusion still applies to a granted Action. No grant funds
`cast_spell`, `activate_spell`, arbitrary features or items. The schema names
Utilize, but that operation deliberately fails closed until item activation
has a reviewed action classification. No canonical Haste grant is admitted.

## Slow execution and casting ruling

[SRD 5.2.1](https://media.dndbeyond.com/compendium-images/srd/5.2/SRD_CC_v5.2.1.pdf),
Slow (printed page 163), specifies up to six selected creatures within a
40-foot Cube at 120 feet, an initial WIS save, half Speed, AC and DEX save −2,
Reaction denial, Action/Bonus exclusion, one attack per Attack Action and a
25-percent failure chance when casting a spell with a Somatic component.
Each target repeats WIS at its turn end; success ends only that target's effect.

The shared delivery planner validates the Cube and selected creatures before
costs. Existing effect sidecars project AC/DEX modifiers; exact reviewed
Foundry movement keys project multiplication across movement modes. The
movement ledger captures spent distance before effect application/removal,
so ending Slow restores only the unspent budget. Same-spell sources do not
stack; older effects retain their own repetition and concentration ownership.
The existing caster-keyed concentration countdown owns the one-minute duration:
ten caster turn ends, including the cast turn. Repeat saves use the captured DC
and current defenses, with ordinary Legendary Resistance behavior.

The SRD does not specify percentile timing or special resource refunds for
Slow. This engine uses the following explicit project ruling:

1. Complete legal preflight and pay the attempt's casting-time budget.
2. Begin the cast, including replacement of existing concentration, then open
   the ordinary Counterspell opportunity. Counterspell keeps its established
   Action payment and slot-preservation contract.
3. For a non-countered cast, pay the spell slot and emit `SpellCast`. If the
   caster's policy at the attempt's start applies to Somatic casting, roll
   `d100`; 1–25 fails. `SomaticSpellRolled` records the roll and threshold.
4. A mechanical failure emits `CastFailed(reason="somatic_failure")`, keeps
   the legal attempt's costs, and produces no spell payload/new concentration.
   An unexpected execution exception instead rolls back the entire transaction.

Invalid casts draw no percentile. Verbal-only spells draw none. The same
attempt helper serves monster and reaction spells; Slow itself denies those
Reactions before release. Ordinary magic-item spell activation does not require
components, and ongoing activation is not casting, so neither gains a Somatic
roll. Components, free hands and material availability remain the existing
Host boundary, as do 3-D positioning and out-of-combat clocks.

## Admission, evidence and remaining gaps

Slow is Executable within the existing combat/Host boundary. Haste stays
Deferred. It needs complete Utilize-versus-Magic item permissions and a generic
effect-end producer for Lethargy's Incapacitated/Speed-zero penalty until the
target's next turn ends, including replacement and overlapping sources.
Numeric Haste modifiers or the new grant schema do not constitute full support.
Potion of Speed retains the same fail-closed missing-mechanism boundary.

`tests/test_action_policy.py` in both packages covers exact pinned-source
regeneration/schema drift, public cast/selection, Action/Bonus orders, Extra
Attack/Surge/Nick, Martial Arts/Flurry, OA/readied/damage reactions, item/ongoing
permissions, paid Somatic attempts, monster costs, independent repeats,
duration/concentration, sourced grants, tampered Admission, rollback and replay.
The capability matrix executes a public Slow probe. Spell and lifecycle audit
documents are regenerated from canonical data; classification changes only for
the completed Slow contract.
