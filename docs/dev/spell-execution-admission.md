# Spell execution admission

Admission is a pure capability check before paid combat execution. It does not
implement spells, resolve Activities, hydrate actors, emit events, draw random
numbers or write lifecycle state. Delivery still follows
`SpellDeliverySpec → plan_delivery → execution → Activity Resolver`.

## Contracts and evidence

`spell_execution_reviews.json` is a packaged, typed review inventory keyed by
canonical UUID, with slug identity checks, canonical semantic digests, source
URLs, Activity IDs/roles, missing mechanism codes and explicit limitations.
There is no runtime description parsing or spell-slug resolver dispatch.
Unknown sources, kinds or Activity IDs fail closed. Updating canonical data
requires reviewing the affected record; the audit rejects missing, stale and
orphaned records. Digests use normalized validated JSON, independent of Windows
newlines or Python hash seed. Runtime admission recomputes that same
`semantic_digest()` for the actual loaded spell, including Activities, effects
and formulas. A custom loader cannot reuse a canonical UUID/slug to inherit a
review for different mechanical content; mismatches return the existing typed
`unreviewed_spell` failure before payment. The digest is not cached by identity,
so changing a loaded spell after an earlier review does not retain admission.

The corpus has 339 spells and 464 Activities. Current classes are **28
executable**, **44 bounded**, **26 host narrative**, and **241 deferred**.
This is a static admission inventory, not 339 end-to-end execution proofs.
The earlier 234 mechanical-kind / 105 inert-kind probe did not distinguish
required missing semantics and must not be used as a support percentage.
No new D&D mechanics are implemented by this batch.

Executable means the reviewed combat creature payload within existing engine
boundaries, not world-object interactions, component inventory or a complete
3D environment. Bounded records explicitly retain supported canonical
operations without claiming complete SRD support. Host narrative records
delegate their result to the host, including any world/narrative adjudication;
the engine reports `SpellCast.execution_class = host_narrative`, rather than
promising a mechanical effect. Unsupported *mechanical* work must not be
classified narrative merely because its resolver logs and returns.

Rules sources are the linked, pinned canonical Foundry sources and their
stored descriptions, checked against the [official SRD 5.2.1](https://media.dndbeyond.com/compendium-images/srd/5.2/SRD_CC_v5.2.1.pdf).
This batch does not update the corpus's existing 5.2 provenance or certify all
5.2.1 errata. Each deferred row names missing mechanics and the typed inputs,
consumers or lifecycle needed to implement them; broad subsystem codes are
supplemented with per-spell details for action-specific rules.

## Selection and entrypoints

Reviews distinguish casting payloads, independently invoked alternatives,
delayed payloads and persistent owners. `delivery_activities` uses these roles:
default cast payloads plus required delayed/persistent work, or a selected
alternative plus lifecycle work. Required missing **same-cast** mechanics are
checked at spell level, so selecting Conjure Animals' save cannot bypass its
missing area/entity contract. Finger of Death requires conditional next-turn
Zombie creation and is deferred. Freezing Sphere's immediate Cast and Fire
operation is bounded; its optional held-globe operations are rejected.
Sunbeam's initial and repeat beams are alternatives, so a default cast does not
execute both. The repeat operation is deferred until an ongoing spell activation
carrier can spend its Action without spending another spell slot.

`preflight_delivery` retains existing target/geometry failure precedence, checks
admission before payment, and recurses into
every selected `CastActivity` child. A child refusal occurs before the parent
Action/charge is committed. Formula validation reuses the existing validator with
a disposable draw-prohibited context and the wrapper's ability/DC overrides.
Missing `@mod` inputs and malformed expressions refuse before costs.
Existing fixed DCs, parent identity and source
attribution remain in the existing cast dispatcher. Item concentration remains
a separate unsupported ownership capability. C21 Magic Weapon, Spiritual
Weapon, Summon Dragon and Polymorph retain their existing direct-cast carrier
gates; delegated/monster entrypoints cannot pretend to supply those inputs.

PC direct/item paths reach this preflight in `_area_target_failure`, before
`IntentSubmitted`, budget payment, `begin_spell_cast`, reactions and slot/charge
payment. Monster candidate selection and execution revalidate the same preflight
before committing Action/daily uses. Unsupported candidates can fall back to a
legal spell/action. Reaction declaration and release recheck admission through
the existing reaction payload validation. Counterspell itself is admitted by
its typed response contract, not by a damage/effect heuristic.

Public refused intents emit only `CastFailed(reason=unsupported_activity)`
with typed `execution_failure` details; pure preflight emits nothing. Rejection
preserves state, current concentration, pending reactions, every resource and
RNG. A legal cast can still be countered after casting-time payment under the
existing slot-sparing interruption contract. `execute_spell_delivery` also
defensively checks admission before dispatch. The standalone, resource-free
`resolve_activity` primitive is deliberately not a complete-spell API.

Slow and Haste now reject paid casts: their geometry and effect primitives
remain available, but missing action rules are necessary mechanics. Likewise
Blur, Misty Step, Animate Dead, True Polymorph and Dimension Door cannot succeed
as effect markers or incidental damage. This is an intentional admission change,
not removal of their targeting/geometry capability.

Ensnaring Strike and Searing Smite also require an authoritative triggering-hit
carrier before paid casting can be admitted. Their existing delayed payloads
remain implemented and tested at the resource-free scheduler boundary. Admission
does not turn a working timed damage primitive into a claim that the hit-triggered
spell transaction is complete.

Harm's maximum-HP reduction, Sleep's second-save transition and immunity rules,
Protection from Poison's removal and condition-specific save advantage,
Stinking Cloud's action denial, and Enthrall's combat auto-success rule are also
mandatory deferred mechanics. Their existing damage/effect/area primitives do
not admit a paid spell transaction. Tests for those primitives remain separate
from public admission refusal tests.

## Execution integrity and ruleset lifetime (Batch B1)

`submit_player_intent` and `advance_monster_turn` share one snapshot and event
buffer transaction, including delegated child spells, automatically triggered
reactions and legendary actions. Unexpected execution exceptions and cancellation
restore every live state field and the original RNG state before propagating.
Restoration includes action budgets, slots, charges, HP, effects, concentration,
timed activities, persistent areas and pending reactions. Events from a failed
execution reach neither the narration queue nor host listeners. Normal typed
refusals and legal Counterspell interruptions return normally and retain their
existing costs and event order.

Host listeners run after state and the complete event batch commit. A listener
exception propagates without undoing committed combat state or already published
events; external observer side effects are outside the rollback boundary.

Each combat captures its loader at opening and binds it for all nested execution
and closing operations. `configure_lib_loader` changes the default for future
combats. Bridge Homebrew import, overwrite, delete and forge return HTTP 409
before persistence while that Bridge has an open combat; opening and updates are
serialized. Installed custom loaders and their assets must remain immutable:
in-place mutation by host code is unsupported. The resource-free Activity
Resolver remains an unchecked host building block.

Public regressions inject failures after real payment and resolution, compare
complete authoritative state, RNG, queued events and listener isolation, and
verify retry replay, real concentration/timed/area restoration, normal successful
casts, failed saves, Counterspell costs and ruleset update conflicts. This adds
execution integrity guarantees without increasing admitted SRD coverage.

## Typed effect choices (Batch B2)

Guidance, Enhance Ability and Protection from Energy are executable through
`submit_player_intent()`. Their reviewed canonical Utility Activities declare
`effect_selection="one_per_target"`; `effects[]` supplies the legal canonical
candidate IDs. Every selected target requires exactly one `EffectSelection`
containing `spell_id`, `activity_id`, `target_id` and `effect_id`. These declarations
travel through `PlayerIntent.effect_selections` and `SpellDeliverySpec`, including
recursive delegated preflight. Missing, duplicate, foreign, level-ineligible or
incomplete choices refuse before payment, concentration replacement or RNG.

For example, choose Guidance's canonical Perception effect without a runtime
spell-name or effect-name lookup:

```python
from dnd5e_engine import EffectSelection, PlayerIntent

intent = PlayerIntent(
    intent_type="cast_spell",
    spell_id="guidance",
    target_id="char:ally",
    effect_selections=(EffectSelection(
        spell_id="guidance",
        activity_id="R6Fs3rsgXlgHPOFn",
        target_id="char:ally",
        effect_id="qPowjrduxnzKpH8x",
    ),),
    willing_target_ids=("char:ally",),
)
```

The Host explicitly attests willingness for Guidance and Protection from Energy;
allegiance never implies consent. Enhance Ability does not require willingness.
Live creature eligibility, touch distance and clear path remain authoritative
engine checks. Material inventory and narrative/world circumstances remain Host
responsibilities. Monster AI supplies no choice declaration and refuses these
casts; item concentration ownership remains deferred even with valid choices.

Reviewed Foundry skill bonus keys map to the existing Check bonus arithmetic:
Guidance rolls 1d4 after the d20 only for the selected Skill, on every matching
check while active. Reviewed ability-check mode keys grant Enhance Ability's
Advantage for the selected ability, including matching Skill checks, never
attacks or saves. Its canonical `@item.level - 1` target cap permits additional
distinct creatures at higher slots, each with an independent ability selection.
Protection from Energy selects Acid, Cold, Fire, Lightning or Thunder and reuses
the existing damage resistance union and immunity ordering.

The Effect Resolver filters refs before application and retains the established
runtime effect identity/origin convention, canonical ref IDs, lifecycle and
concentration links. Reviewed `latest_applies` lifecycle groups suppress older
equal-potency casts in consumer projection without deleting their authoritative
effects or concentration. They resume when a newer cast ends. Guidance's minute
and the other spells' hour retain existing combat duration boundaries.
These choices and stacking rules were checked against
[SRD 5.2.1, pp. 106, 127, 138, 157](https://media.dndbeyond.com/compendium-images/srd/5.2/SRD_CC_v5.2.1.pdf).

Canonical translation pins IDs, Change Keys, modes, formulas, target counts and
durations; regeneration fails on mechanical source drift. Only these three
reviews/digests change classification, reducing Deferred from 244 to 241.
Public regressions cover each candidate, per-target upcasting, all payment/RNG
refusals, check/damage consumption, overlapping casters, expiry, replacement,
Counterspell, post-payment fault rollback and deterministic replay. Other choice
spells retain their existing reviews and missing-mechanism restrictions.

## Regeneration and validation

```sh
uv run --package dnd5e-engine python -m dnd5e_engine.spell_capability_audit --output docs/audits/spell-execution.json
uv run --package dnd5e-engine pytest packages/dnd5e-engine/tests/test_spell_execution_admission.py
make check-plan BASE=<batch-start-sha>
make check-integration BASE=<batch-start-sha>
```

The generated inventory must compare byte-for-byte after regeneration and its
test recomputes all records from bundled canonical data. Regression tests cover
public direct/item/monster refusals, narrative execution, mandatory mixed
payloads, independent alternatives, preserved concentration and Counterspell,
plus exact event/RNG/final-state replay. Existing delivery, save-policy,
blocked-origin, timing, persistent-area, forced-movement, source-identity and
fixed-DC suites remain necessary integration coverage. Existing Slow and Sleep delivery
tests now exercise pure target planning and separately assert the paid admission
refusal; geometry assertions are retained. Tests of deferred spells' already
implemented lifecycle primitives call the resource-free scheduler directly;
budget, cover and reaction tests use admitted canonical spell fixtures. Full validation is not a default
for this batch; the shared resolver/public boundary changes require Focused.
