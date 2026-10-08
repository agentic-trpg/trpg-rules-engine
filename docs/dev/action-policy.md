# Effect-owned action policies

Batch B5 adds a closed `ActionPolicy` to canonical passive effects and the
existing runtime `ActiveEffect`. Effective, enabled effects project permissions
using the existing latest-applies stacking contract. No execution branch reads
a spell name or description. Source-reviewed ingestion attaches Slow and Haste policies;
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
payment. A replay-stable application identity preserves every cast's lineage;
a stacking-group allowance prevents a newer/older Haste source from supplying a
second extra Action during the same turn. Ordinary and Surge budgets are untouched.
Slow's exclusion also applies to a granted Action.

## Reviewed Action classification (B6)

`ActionType` distinguishes Attack, Dash, Disengage, Hide, Utilize, Magic and
other Actions. Basic intents have fixed typed categories; item/feature operations
read the actual selected activity's reviewed `action_type`. A `CastActivity` is
Magic regardless of its parent item. Bonus/free activations cannot masquerade as
Utilize. Unknown or mixed classifications fail closed for restricted grants and
Action Surge; ordinary invocation retains its existing contract.

Pinned source hashes authorize the deployment activities of Ball Bearings and
Caltrops as Utilize. Both execute the existing consuming persistent-area pipeline,
including placement, save/condition triggers and cleanup. Recovery operations and
other items/features gain no inferred permissions. Runtime classification never
reads a name, slug or prose. Explicit activity IDs and default selection use the
same item selection as payment/resolution.

## Haste execution (B6)

The reviewed Haste cast requires one willing visible creature within 30 feet.
Monster AI cannot declare willing targets and therefore refuses Haste before
payment. Host willingness attestation, visibility, range, Action/slot eligibility, admission
and payload validation complete before payment or concentration replacement.
Legal Counterspell retains the established slot-sparing interruption contract.

The ordinary ActiveEffect carries Speed x2, AC +2, DEX save Advantage and one
sourced extra Action per target turn: Attack (one attack, including a single
Unarmed Strike Grapple/Shove option), Dash, Disengage, Hide or reviewed Utilize.
It cannot fund casting, ongoing Magic, stat-block Multiattack or unreviewed
operations. Extra Attack/Action Surge keep their independent ledgers. Light can
open a separate Bonus Action attack even from the granted Attack; Nick cannot
add an attack inside that restricted Action, and Cleave is similarly excluded.
Normal Attack Nick windows survive interleaving. Martial Arts and paid Flurry
strikes retain their separate Bonus Action funding.

Existing effect consumers project numerical changes. Speed multipliers compose
before rounding, so Haste and Slow cancel independent of attachment order. Flat
modifiers, Prone crawl/standing, Dash and Speed-zero effects share the spent-distance
ledger; Speed zero dominates without forgiving already spent movement. Enabled,
effective sources alone project save Advantage and AC.

Ending the actual source captures and applies Lethargy through the generic
[effect-end producer](effect-lifecycle.md#effect-end-follow-ups-b6). It imposes
Incapacitated and Speed zero through the target's **next** turn end, including
when Haste ends during the current target turn. Same-spell sources project only
the latest equal-potency application; suppressed sources retain concentration
and clocks. Each actual source ending produces its own penalty once. Removing
one penalty preserves other conditions and their ownership.

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

Slow and Haste are Executable within the existing combat/Host boundary.
Potion of Speed stays Deferred: its CastActivity delegates to concentration Haste,
and shared item preflight refuses that child before consumption. Its independent
SRD contract requires a one-minute non-concentration effect with no Lethargy and
no ordinary spell components. No item-specific effect override/ownership review
exists, so this batch does not certify it by inheriting spell admission.

`tests/test_action_policy.py` in both packages covers B5 policy regressions and
pinned translator contracts. `tests/test_haste_contract.py` exercises public
casts, every normal/grant/Surge order, all admitted Actions with real Utilize,
Light/Nick/Cleave, Monk bonuses, Slow, saves, movement, end reasons/timings,
overlap, Counterspell, rollback and retry replay. Capability probes execute
public Slow and Haste contracts. Canonical semantic admission and generated
spell/lifecycle audits record only these reviewed capabilities.

Natural initiative E2E uses public casts, attacks, pass and monster advancement
to verify damage-broken concentration, caster/target death, overlapping casters,
self-drop during the target turn and duration expiry at its turn end. Replacing
self-Haste starts the existing concentration cascade before the new payload;
Lethargy's Incapacitated state prevents that payload and new concentration.
The legal casting attempt retains its ordinary Action/slot cost. Mechanism
tests separately exercise exact start/middle/end boundary serials and duplicate
expiry, without treating synthetic event emission as a public E2E proof.
