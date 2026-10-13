# Stateless rule evaluation: current local contract

This is a migration of the existing Rule Engine toward one stateless evaluator.
Each game domain owns its authoritative facts; shared commit facilities coordinate
their atomic transaction, expected values, versions, RNG and receipt. The existing
SM/Combat Handler is the current process entrypoint, not a central owner required
to understand every domain's business fields.
The conversion is incomplete; see [migration progress and the finite roadmap](stateless-migration-progress.md)
and [the complete field inventory](stateless-evaluation-inventory.md).

## Versions and breaking migration

The current local candidate versions are:

| Contract | Current version | Replaces |
| --- | --- | --- |
| RuleEvaluationRequest / Result | `engine-evaluation/24` | `/1`–`/23` |
| StateSnapshot | `engine-snapshot/17` | `/1`–`/16` |
| Availability request/result | `engine-availability/13` | `/1`–`/12` |
| RulesetBinding evaluator | `dnd5e-evaluation/37` | `/36` and earlier |

R37 adds required `stage_facts` to every Result: accepted carries typed coverage,
other statuses require explicit null. Stage facts `/2` cover audited basic Attack,
ordinary Pass and Walk; other original rule paths retain their mechanical result
with independent uncovered metadata. Input budget/ledger misalignment that existing
Attack admits is explicitly uncovered, never a new rule rejection. Call
`result.verify_request(original_request)` after Python/JSON transport to validate
stage identity, semantics and covered final Delta consistency. It never executes
the action or draws again. No old envelope or private `/1` stage fallback exists.
Snapshot/Availability shapes are unchanged. Initialization/concentration candidate
envelopes sharing EvaluationVersion also use `/24` with explicit uncovered stages.
See [audited stage facts](event-stage-facts.md) and [SM handoff](event-stage-sm-handoff.md).

Old envelopes, snapshots and results are explicitly rejected. There are no compatibility
aliases for `InventoryCombatSnapshot`, `combat_inventory`, missing equipment or missing
inventory. This is a breaking local protocol revision, not a silent shape change.
Legacy public coroutine signatures remain; their corrected death/turn/payment/Mastery
behavior is shared with the new execution path. A Legacy `CombatOutcome` is a separate
host handoff API and must not be applied as a stateless StateDelta.

RF-RE3 removes `event_log` and `legendary_resistance_applied_event_indices` from
CombatState and every enclosing public/internal candidate schema. Computations
return private candidate mechanics and this call's ordered typed proposals
separately. The new evaluate/availability path never reads audit history or calls
a Legacy compatibility adapter. Existing `ended`, `turn_serial`, damage sequence,
usage markers and effect/reaction clocks supply the admitted current rule facts;
no duplicate summary or new RNG-consuming identity is introduced. StateDelta is
unchanged, and the candidate snapshot is never a whole-state persistence command.

Ordinary requests reject old formats. The explicitly invoked
`evaluation_migration.migrate_snapshot_16` accepts only a complete serialized `/16`
source bound to evaluator `/35` and identical effective rules data. It requires
explicit typed audit history and an empty Legacy event-index set, validates
ended/history consistency, event fields/references and all current fact closure,
then returns `/17` without audit history. Earlier incomplete sources, missing
facts, unknown event fields, contradictions and unconvertible indices reject;
they are never defaulted to empty. The owner retains the original audit records.
Legacy parity capture validates that same history/ended boundary, while the
test-only Legacy oracle factory requires audit events as a separate argument.

Meta MODULE_CONTRACTS §4.2 DECIDED require exactly **CombatSnapshot /
NonCombatSnapshot** scene discriminators. Both now require typed `InventoryState(entries=...)`,
complete `CharacterStateV2` records, explicit EffectState records and SceneState.
CombatSnapshot additionally carries the complete CombatState dependency closure.
Every field is required, including nulls, empty collections and `entries=()` for a
known empty inventory. No RNG, callbacks, queues, loaders or retained handles occur
in Snapshot. Both Python instances and JSON are strictly revalidated.

InventoryEntry carries instance identity, owner, item slug, quantity, charges remaining
per unit and accessibility. Quantities/charges cannot be negative; owners must exist
in the supplied actors and instance IDs must be unique. This homogeneous-stack model
does not claim mixed per-unit charges. Actor held weapon/grip/occupied hand/trained
Mastery facts are explicit. Carried slugs do not imply inventory quantities or charges.
`capture_combat_snapshot` is an internal parity adapter requiring an explicitly supplied
InventoryState; it cannot derive stock from Legacy runtime. `capture_evaluation_snapshot`
preserves the input inventory and actor ordering. The mechanical projector fails on
any unexpected inventory/component change.

The three-field RulesetBinding still pins ruleset_id, effective data_revision and
evaluator_version. SHA-256 includes all enumerated effective typed assets and overlays,
checks accessor/enumeration identity, and has no stale cache. Loaders must remain immutable
during evaluation. Final shared fingerprint/wire formats remain OPEN in Meta.

R26 corrects lifecycle wire schemas without changing Snapshot `/13`'s field shape.
`OngoingEffectLifecycleState` defines every clock field and nests the explicit
application/spec/repeat DTOs. Validation and serialization JSON Schemas require
every field, including explicit nulls and empty tuples/JSON arrays. Runtime retains
the original immutable clock and shared algorithms; strict wire validation rejects
omission, extra fields and coercible wrong types. The existing full-field validator
remains. Local schema/runtime agreement removes the inherited R17 schema discrepancy;
it does not approve cross-repository ABI, source authority or C-15/C-16.

## Operation and result semantics

Import `evaluate` from `dnd5e_engine.evaluation`, DTOs from `evaluation_contracts` /
`evaluation_state`, deltas from `evaluation_delta`, RNG from `evaluation_rng` and
availability from `evaluation_availability`. Each request has session_id, command_id,
operation_kind, real actor_id, typed payload, complete Snapshot, binding and RNGContext.
No system/source-authority or combat.start payload is invented while those contracts
remain OPEN. SM authenticates/authorizes the caller and approved write scope.

| Operation | Current admitted subset |
| --- | --- |
| combat.intent | One ordinary PC/NPC weapon Attack, an exact reviewed stat-block AttackActivity, explicit plain Pass / End Turn (including the bounded R17 Poison lifecycle), ordinary Walk, Action Dash / Disengage; R20 bounded Cure Wounds / Second Wind; R21 one predeclared Counterspell during Cure Wounds |
| rules.check | Explicitly adjudicated ordinary noncombat ability/skill check |
| rules.save | Explicit noncombat actor saving throw; six abilities and fixed DC |
| rules.rest | Bounded noncombat Short/Long Rest: explicit HP/Hit Dice/Slots and reviewed Feature recovery |
| combat.item | Owned accessible single-use Potion of Healing, living self-drinker with declared free hand |
| combat.close | Consistent terminal victory / TPK; no loot/effects/flight/forced end |

Common weapon properties Finesse/Versatile/Two-Handed/Reach/Heavy use the shared resolver
and explicit held grip. Only untrained Mastery tuples are admitted; trained Masteries,
Light/Loading/Ammunition/Thrown, general effects, classes/features, extended budgets, reactions
for ordinary Attack/Item,
complex grids for Attack/Item, timing, areas, objects, summons/transforms remain unsupported. Stat-block
Multiattack prose, recharge and legendary mechanics are refused. A parsed Activity or
a retained DTO never proves support. War Pick's missing typed Versatile damage remains
a data-related refusal. No new Spell, Feat, Monster or Item support was added here.

Checks support six abilities, eighteen skills, proficiency/expertise/Jack/Reliable Talent,
explicit GM advantage/disadvantage flags and reviewed numeric modifiers. Missing DC
returns one unambiguous `check.adjudication` choice with `required_fields=("dc",)` before
RNG. Alternate governing ability with a nonzero projected skill bonus remains unsupported.
Tools, senses, consumable modifiers and complex contexts need further migration.

The Potion's actual HealActivity supplies dice, Bonus Action activation and one itemUses
payment; shared healing computes the cap. Feeding/revival, shields, occupied hands,
attunement/passives, persistent slug charge pools and non-single-use units are refused.
InventoryConsume guards the complete old entry, changes exactly quantity minus one,
preserves remaining-unit charges/access/ownership and retains a zero-quantity tombstone.
HP, budget and any turn change accompany the same proposal. Full-HP drinking still
consumes inventory, Bonus Action and dice without inventing an HPDelta. No private slug charge counter is created; the shared typed charge-cost rule pays
exactly one unit through InventoryConsume. It cannot become a second payment.

| Result status | Mechanical material | Other required fields |
| --- | --- | --- |
| accepted | Complete StateDelta (operations may be empty), ordered ProposedEvents, explicit RNGTransition | choice/error null |
| rejected | No delta/RNG/events | Structured RuleError |
| needs_choice | No delta/RNG/events | Typed preflight choice, no error |
| unsupported | No delta/RNG/events | Structured capability error |

Legal misses/failed checks are accepted with their actual costs/draws. Schema/binding,
internal/cancellation and transport failures are outside these four statuses. Results
verify request identity/version/binding and exact RNG input. ProposedEvents have no
authoritative event ID/sequence. Responsible domains validate ownership, references
and their invariants; shared commit facilities validate common expected values,
permissions, request association and OCC, then atomically commit the participating
Delta/RNG/events/receipt/outbox changes.
Engine returns no durable receipt, does no SQL write or external publication, and never
calculates commit success. Whole-world read fencing is conservative and explicit.

## Death, turn and closure stabilization

The shared terminal Death fold now synchronizes dead_ids, DeathRecord and is_alive=False
for all creature kinds. Zero-HP Characters still awaiting death saves are distinct;
their bounded lifecycle is admitted by explicit Pass (R11); Attack/Item admission remains narrower. Shared turn advancement skips recorded dead
creatures without their start/end hooks or RNG and bounds the all-dead scan. Dead
roster entries and the explicit death ledger remain available for outcome projection. After killing one
enemy, remaining live actors can continue legal attacks with the complete committed
Snapshot. Attack, item and closure admission share exact death-ledger consistency checks;
unrecorded/contradictory death, HP, life state, kind, location or killer is refused before
RNG. Dead targets are rejected before payment.

CombatClose now has only combat_id, expected_ended=False, ended=True, derived reason,
**xp_increments** and **historical: HistoricalCombatOutcome**. The historical record
contains deaths, residual HP/temp HP, expended resources and the explicitly empty loot
list. Those values already exist in the input: they are reports, never new writes.
Only ended and XP increments are new consequences, committed once under both world
and ended fences. Old flat residual_hp/expended_resources/xp_awarded fields are rejected.
Do not apply HP/deaths, pay resources, or grant XP by translating historical reports
back into mutations. The explicit `ended` fact rejects a repeated closure; Legacy
conversion refuses contradictions between that fact and its original history.

Closure calls the shared reason/outcome projector on disposable supplied state and
returns one CombatEnded plus an unchanged RNGTransition without restoring execution
RNG. Victory priority when both sides are dead matches Legacy, with no XP for dead PCs.
Nonempty loot or any newly unrepresented outcome consequence fails closed.

The shared slot payment ledger was corrected independently of closure: `_take_spell_slot`
records the actual owner and `spell_slot:<level>` or `pact_slot:<level>` after decrementing
that exact pool. Applying concentration to a target does not infer payment or use effect
names as resource identities. This ledger reports slot spends, not a claim that all
Legacy item/feature expenditure families have migrated. Snapshot retains complete actual
slot/counter balances; future resource deltas must use those authoritative facts.
Nick Action Policy and attack-origin classification now share the same Mastery-training
predicate as payment. Legacy None training retains its compatibility behavior; new
Snapshot never admits that sentinel.

## RNG, isolation and remaining compatibility work

RNGContext remains separate from Snapshot. `stdlib-mt19937/1` carries all 624 words,
index and finite/null Gaussian cache. Restoring uses Random(0) + setstate, never pickle
or module-global draws. Attack preflight constructs no RNG; real request state is
restored only after admission. Item preflight also restores no execution RNG before admission. No-draw and event-only stream/world version policies
remain SM/Meta OPEN decisions. Engine does not advance authoritative versions.

R9 Attack executes `evaluate` → `prepare_attack` → `execute_attack` → shared
`build_activity_context` / `resolve_activity` → `attack_delta`. Admission/availability
create no Legacy context. Attack creates neither `_LiveCombat` nor CombatHandle, calls
neither `execution_context` nor `_submit_live_intent`, and reads no Registry. Its local
actor updates, immutable combat-record replacements and short typed event buffer are
one attack computation, with no hooks, listeners, queues or persistent runtime.

Both entries share `attack_rules` (range/grip/proficiency/adjacent-hostile legality,
stat-block magnitudes and damage defenses), `action_economy_rules` (legality),
`turn_rules` (payment, attacks, reset, next living initiative slot, budget bookkeeping),
`damage_rules` (temp HP, zero HP, conditions, death records), the Activity Resolver,
attack/apply/d20/dice primitives, condition projections and `death_saves`. The existing
formulas were extracted or reused, rather than implementing another attack algorithm.
`intents.PlayerIntent` remains the identical class reexported by Legacy Orchestrator.

Historical R9 baseline (superseded by R10): Item used `_submit_live_intent` and a
disposable `_LiveCombat`/CombatHandle; Closure constructed `_LiveCombat` for outcome
projection. R10 removes both dependencies; the current admitted paths use pure kernels. Exceptions discard private computations; no database/publication or
external state update occurs. All unsupported dependencies remain guarded out.
R9 changes the evaluator implementation pin to `/11`; request/result `/6`, Snapshot
`/4`, availability `/3` and closed Delta shapes are unchanged. Older evaluator pins
fail binding verification explicitly.

The historical pre-R30 SM observation pins Engine/Data `602dcb8`, evaluation `/1`/`/4`
and evaluator `/4`/`/8`, with ongoing work on combat-close/check integration. It must
explicitly upgrade versions, snapshot facts and consumers; in particular closure must
read the new historical report and write only the new consequences. It does not yet
consume this version. No SM file was changed or SM test result claimed. See the
[migration progress ABI checkpoint](stateless-migration-progress.md#cross-repository-abi-checkpoint).

## Regression evidence and verification scope

Tests preserve independent expected-value consumers and Legacy comparisons for complete
state, ordered events, RNG, miss/crit/R-I-V, ownership/cost, full inventory depletion,
closure fences and rollback. Stabilization adds multi-enemy continuation/dead-turn skipping,
strict two-context inventory schemas, old-version/flat-outcome rejection, read-only
Attack/Check/Item/Closure guards, actual payer/pool and untrained Nick policy regressions.
Canonical weapon differential cases now include an actual natural-20 seed.
Legacy Demo showcase inputs were updated for skipped dead turns (including Garrick's
real turn after the first rat dies), retaining every proof-event/replay assertion and
adding a surviving-player-turn regression. Demo remains a Legacy consumer.

The first new death regression failed before repair at the dead creature's turn.
Independent Haste and untrained Nick regressions also failed before their shared-core
fixes. The final task delivery records the executed Full/static/security/docs/wheel
results; this document does not infer CI or cross-repository success from unit tests.
Windows Full uses the verified adapter executing exact checked-in package Makefile
recipes through tools/validate.py when GNU make is absent, with original coverage floors.


R9 structural regressions explicitly prohibit Legacy factories, `_LiveCombat`, handles,
registry access, dispatcher, event fold and Legacy resolver entry during **successful**
PC/NPC attacks, repeated execution, concurrent independent evaluations and injected
post-damage/cancellation failures. Independent Legacy-entry comparisons include a
three-attack multi-enemy sequence and natural 1/20 next-turn death saves, including
Unconscious immunity. Natural-20 recovery exposed and fixed the new path's missing
movement-budget reprojection on ConditionRemoved. Full validation remains the release
gate; no State Machine integration or complete conversion is implied.

R9 review also confirmed and repaired a Speed-zero Dodge projection discrepancy.
The existing loss predicate now lives in shared `attack_rules.dodge_benefit_active`,
used by both entries. PC/NPC differential tests cover Dodge on/off, Speed zero/30
and no unspent movement, preserving the distinction between Speed and movement budget.

## R10 independent item and closure computation

Attack, Check, the admitted Potion and terminal closure now execute without Legacy
runtime. Item uses shared healing, item-use-cost and Bonus Action kernels with the
small per-evaluation CombatComputation. It preserves remaining movement windows and
complete turn-boundary ledger projection. Closure uses outcome_rules with explicit
actors, sides, HP, temporary HP, XP, death records and historical expenditure. Legacy
wrappers call the same kernels. execution_context remains a regression-only adapter;
no supported evaluation operation calls it. Full-family migration remains incomplete.

## R11 explicit bounded turn lifecycle

A plain `CombatIntentPayload(intent_type="pass")` on `combat.intent` explicitly ends
the current PC or NPC turn, including an unused Action. It does not infer a command
from text or select NPC tactics. Shared turn rules skip recorded dead actors, wrap
rounds, reset the incoming actor budgets and ledger, and perform ordinary Character
death saves. Only permanent implied unconscious/prone conditions are admitted;
effects, concentration, pending reactions, expiry and complex hooks refuse before RNG.

The complete existing guarded delta family carries turn, budgets, ledger, HP, conditions
and death history; the SM commits it with ordered events and RNG. Natural 1 can take
two prior failures to four, matching the shared death-save algorithm, so snapshot /5
explicitly widens that counter and the envelope/availability/binding are versioned.
This is a local DTO revision; Meta OPEN wire and no-draw version policy remain OPEN.

## R12 bounded movement and position proposals

Ordinary Walk and Action Dash/Disengage use the same PC/NPC rules. The input
contains explicit canonical actor cells, unique living occupancy, grid dimensions,
blocked cells, walls, terrain and a complete active walking ledger. Shared
GridTopology weighted pathfinding, occupancy/size qualifiers, step_cost and
MovementLedger calculate the route and payment. Move, Dash and Disengage preserve
the current turn even when the Action or movement is exhausted; explicit Pass closes it.

PositionUpdate carries combat/scene/actor identities, expected old cell, new cell and
computed canonical route. It commits with guarded MovementLedgerUpdate and
ActionBudgetUpdate, ordered events, unchanged RNG and the world read fence. SM checks
these preconditions and commits without recomputing movement or pathfinding.

Before RNG restoration/payment, paths that may leave a hostile reaction weapon reach
return unsupported; Disengage suppresses that trigger. Conservative threat checks can
refuse blocked-vision paths too. Bound stat-block reaction templates, non-walk modes,
forced/granted movement, effects, areas, concentration and complex environment remain
unsupported. This limited geometry admission does not expand Attack/Item admission.
Envelope /8 adds the local closed PositionUpdate; binding /14 rejects old evaluators.

Explicit Pass also admits this static wall/terrain geometry, so a bounded terrain Move can end its turn through the same lifecycle. Effects and reactive hooks stay excluded.

## R13 local combat initialization candidate and public ABI block

`evaluation_initialization.evaluate_combat_initialization_candidate` is an internal
calculation seam, absent from the public evaluate operation union and Bridge routes.
Its versioned local typed Payload/Request uses complete NonCombatSnapshot, pinned
binding, explicit RNG and the normal four-state result vocabulary. It grants no
source permission. Public combat.start/create is blocked pending Meta C-16 review.

Snapshot /6 requires `combat_setup: CombatSetupState | None` on NonCombatSnapshot.
Ordinary checks supply explicit null; a pending setup cannot silently become a check.
The setup contains selected roster/sides, canonical opening positions, fixed-versus-
rolled initiative, explicit nullable modifier overrides, surprise, draw order, Host XP
and owned reaction weapons. Typed timed activities, persistent areas and objects
are also required; only explicit empty fresh components are admitted. All actor,
inventory/equipment, resource, effect and scene facts remain in the input.

The bounded core validates living ordinary actors/equipment and opening occupancy,
reuses shared Initiative d20/surprise/tie-break and turn-budget algorithms, preserves
HP/Temp HP/equipment/inventory/resources and proposes the opening round/turn events.
It refuses seeded effects, dying actors, passive features/attunement, precombat areas/
objects/timing and other unmigrated hooks before restoring RNG. Canonical stat-block
hydration is outside this local subset; full authoritative actor facts are supplied.

CombatCreationCandidate is one closed operation: expected absence, expected setup,
a complete fresh typed history-free CombatState, and guarded per-actor
Initiative/budget updates. It cannot replace an Actor, World or whole Snapshot; no
existing inventory/resources are replaced. Ordered proposals and RNG accompany it
in one uncommitted unit. Only SM can check absence/versions/permission and atomically
create the component with actor writes/events/RNG/receipt/outbox.

Envelope /9, Snapshot /6, availability /5 and evaluator /15 are local candidates, not
approved shared ABI. Existing Legacy start/registry/effect hydration remain Transitional
for regression and old consumers. The independent initialization core constructs no
LiveCombat/handle/runtime, stores no map/session and leaves no registry on faults.


## R14 explicit NPC stat-block initialization closure

Snapshot /7 requires `CombatSetupState.npc_stat_blocks`, including an explicit empty
collection for unbound rosters. Each NPCStatBlockBinding carries an approved actor ID,
canonical monster slug and exact pinned data revision. This is mechanical evidence,
not a creation permission or an identity inferred from display text. The internal
candidate verifies template identity, all admitted canonical actor mechanics, senses,
initiative modifier and ordinary single-attack activities before restoring RNG.
Complex traits, recharge, legendary, Multiattack and spellcasting remain unsupported.

The shared `monster_rule_facts` projection is extracted from Legacy initialization;
Legacy and independent admission use the same canonical projection. Complete actor
facts remain supplied by the caller. Creation preserves their HP, resources and
inventory, and populates the explicit monster identity and action-use maps. The
existing R9 resolver then executes an explicitly selected stat-block action without
calling Legacy initialization, handles, registry or Orchestrator.

Real bundled SRD Bandit initialization, fixed and rolled initiative, ranged attack,
complete state/events/RNG parity, independent expected rolls, mechanical/binding
refusal, version/absence guards and post-draw faults cover this chain. Stat-block
Movement remains unsupported pending complete opportunity-attack threat closure.
Evaluation /10, snapshot /7, availability /6 and evaluator /16 are local revisions;
public creation and C-16 source authority remain OPEN.


## R15 standalone saving throws

The local `rules.save` Actor branch requires SavePayload: saving target ID (equal to
real actor_id), ability, nonnegative explicit DC, advantage/disadvantage source tuples
and magical provenance. It admits a bounded NonCombatSnapshot with no pending setup,
cover, active effects or save-modifying equipment/features. Only adjudicated `flag`
sources are supplied externally. Reviewed persistent session conditions use the same
pinned canonical condition clauses as Legacy; time-varying/effect-linked conditions,
Exhaustion, Legendary Resistance and consumable modifiers refuse before RNG.

`evaluation_saves.resolve_snapshot_save` shares actor_stats.save_modifier, canonical
condition projection, save_primitive.roll_save and the original D20 resolver. Explicit
flag provenance merges with condition sources through an optional per-invocation
primitive argument; existing Legacy callers retain their behavior. No save formula,
LiveCombat hydration or resource consumer is copied into the new path.

Success and failure both return accepted, an empty closed Delta, a real SaveRolled
and explicit RNGTransition. Ordinary saves have no attack-style automatic natural
1/20 outcome. Condition auto-failure draws no dice; advantage cancellation draws one,
uncancelled advantage/disadvantage two, in the existing order. SM commits event/RNG/
receipt with its world fence; no-draw/event-only version policy remains OPEN.
Standalone combat saves and magical source authorization remain bounded out; no
system source_ref or rules.effect API is introduced. The shared pure save seam is
available for separately admitted lifecycle calculations. Evaluation /11 and evaluator
/17 are local candidates; Snapshot /7 and availability /6 remain unchanged.


## R16 explicit resources and bounded Short Rest

Snapshot /8 requires `resource_state: ResourceState | None` on both scene contexts.
Null explicitly means the extra capacity/ownership closure is unavailable; it is not
an empty pool, and rest cannot run with it. Existing admitted operations that do not
read or pay these pools preserve it. Capture requires the caller to supply this value;
Legacy runtime cannot invent missing Hit Dice or maxima. No third Snapshot context is
introduced. Provided components validate owner identity, current/maximum bounds,
unique mechanical pool identity and equality with retained actor slot/spent mirrors.
Those mirrors are compatibility representations of the same supplied fact.

Closed pool kinds distinguish class Hit Dice, ordinary spell slots, Pact Slots and
feature uses. InventoryEntry independently owns each item instance/stack/charge fact;
AttackBudgetState independently owns the combat action budget. They are not interchangeable
counters. ResourceUpdate retains complete expected/new pool values, owner, pool identity
and signed amount; it preserves capacity and identity. Applying a slot/feature update
also updates its validated actor mirror in the same transaction. HPDelta, typed
RestResolved/HealingApplied events and RNGTransition form one uncommitted unit.

`rules.rest` requires the real resting actor, a typed Short/Long discriminator and
ordered, unique pool-specific HitDieSpend values. The admitted Short Rest verifies
class/die binding, full Hit Dice maxima/levels, real capacities, feature ownership and
all costs before RNG. It calls the existing resolve_short_rest for each selected pool
in request order, sharing its CON modifier and per-die minimum-one semantics, caps HP
through the existing healing kernel, restores Pact Slots from supplied maxima, and
preserves ordinary spell slots, inventory and action budgets. Reviewed Second Wind
uses its pinned Short Rest recovery declaration and recover_feature_uses; its explicit
maximum/current/spent facts are never inferred from a slug or default cap.

At R16, other feature/item recharge, rest-modifying effects/species/subclasses and
Long Rest remained unsupported before draw; R19 below adds bounded Long Rest. Engine decides no authorization, elapsed world time,
scene/quest change or interruption. SM validates those conditions and atomically
commits expected resource/HP writes, events, RNG and receipt. Repeated evaluation is
deterministic; expected-value/version checks prevent a second application of old writes.
No-draw/event-only version policies remain OPEN. Evaluation /12, Snapshot /8,
availability /7 and evaluator /18 are local candidates, not a frozen Meta C-15 ABI.


## R17 existing authorized Poison lifecycle

Explicit plain Pass now admits exactly one existing Cunning Strike Poison effect,
identified by the reviewed feature/activity binding in the pinned corpus. Its full
Effect identity, source/target, captured nonmagical CON/DC, duration, lifecycle clock,
and successfully attached poisoned Condition lineage must agree. Admission compares
the complete canonical effect declaration and original lifecycle registration rules;
unknown sources, numeric modifiers, disabled/transfer/child effects, additional
conditions, areas, reactions, concentration and dying/dead branches refuse before RNG.
This is lifecycle evaluation over an already authorized Snapshot, not a new Apply API.
C-16 source permission remains unresolved and no rules.effect operation is added.

EffectTurnComputation reuses the R11 turn core. At target turn-end it uses R15's shared
resolve_snapshot_save and original roll_save, marks the explicit repeat serial, then
expires on success or the captured round cap. Repeat precedes cap expiry, matching the
original hook order. Failed saves are accepted with actual SaveRolled and RNG; other
actors' turns and already processed target boundaries draw nothing. The original
OngoingEffectLifecycle owns repeat/expiry clock arithmetic. Typed managed effects use
that clock instead of decrementing a second duration counter, exactly as Legacy does.
The original condition lineage filter is now a shared pure helper used by both folds.

A closed local EffectLifecycleUpdate guards the complete old Effect, LifecycleRecord
and ConditionLink, plus full identity/combat ID. It either advances only the repeat
serial or removes all three records together. ConditionsUpdate and ordinary turn/
budget/ledger operations accompany the same world/read fence and ordered events.
SM validates the whole combination and atomically commits Delta/RNG/events/receipt;
the new projector retains all previous omission guards. No authoritative state is
mutated during evaluation and no LiveCombat, handle, registry or Legacy hook executes.

Independent expected rolls, actual PC/NPC Legacy full-state/event/RNG comparisons,
success/failure/cap ordering, multiple committed turns, repeat/concurrent replay,
stale guards and faults after real Save/cleanup/projection cover this bounded slice.
Concentration preservation/break/cascade, general effect application/expiry, shared
condition origins, multi-effect interactions, areas and interrupts remain unmigrated.
Evaluation /13, Snapshot /9, availability /8 and evaluator /19 version this local
C-15 candidate. Lifecycle clock/application/spec/repeat fields serialize explicitly;
wire requests missing any of them are rejected rather than filled from Legacy defaults. This does not freeze a cross-repository ABI.


## R18 concentration dependency closure

The bounded internal Hold Person damage/save/lineage execution and local versions
are described in [migration progress](stateless-migration-progress.md#r18-concentration-and-effect-lineage-candidate).
C-15/C-16 remain OPEN; no public damage-source operation or cross-module ABI is added.


## R19 owned payment and Long Rest

ResourceState is the mechanical balance authority. Slot and `feature_use:*` actor
maps are validated mirrors. One ResourceUpdate atomically writes the pool and its
mirror; they must never be submitted as independently payable balances. Internal
rule-priced [payment and Long Rest progress](stateless-migration-progress.md#r19-resource-payment-and-long-rest)
uses the original Slot preference, Feature preflight and Rest resolvers. There is
no generic spend command. RestResolved explicitly reports Hit Dice, ordinary Slot,
Pact Slot and reviewed Feature recovery. World time/permission/interruption remains SM.


## R20: bounded immediate Spell / Feature execution

`combat.intent` now executes actual canonical Cure Wounds for an explicit Cleric 1
and Second Wind for an explicit Fighter 1. The complete class grants remain in
computation/results. Reviewed Divine Order, Defense and mastery grant identities
are neutral to the admitted unarmored healing and plain turn boundary. Unknown
class levels, general subclass/species, effects, conditions, equipment, extended budgets,
general Reaction/timing state and healing modifiers remain unsupported before RNG/payment.
This does not make all existing spells or all class profiles stateless.

Both snapshot contexts require `casting_state: CastingState | None`. Each explicit
actor entry carries Verbal availability and complete class-source identities,
abilities, known spells and prepared spells; prepared is a subset of known, with
unique references and an exact actor-repertoire mirror. Missing is a schema error;
null means unavailable closure. `spell_source_id` selects that supplied source,
not caller authority. SM still authorizes the snapshot/source and request; the
engine checks reviewed canonical class, actual grant, source/ability, prepared
repertoire, living target, shared Touch range/line of effect, casting time,
Action budget, Slot capacity/payment and components. Only the first ordinary
casting window is admitted: no earlier Action/Bonus Action or spent Reaction;
with no alternate/free/triggered casting mechanism this proves the per-turn Slot
limit. General per-turn casting histories and multiclass sources remain unported.

Cure Wounds reuses `plan_delivery`, `spell_execution.admission_failure`,
`resolve_activity`, healing and Turn cores. Second Wind reuses the original
`preflight_feature` / class Scale / priced ResourcePayment and HealActivity.
No stateful spell/feature dispatcher is called. A Swarm target cannot regain HP:
the legal cast spends Action/Slot but draws no healing dice, matching Legacy.
Full HP still costs the resource and draws the actual healing dice.

One `ResourceUpdate` atomically denotes the authoritative pool and its validated
Actor mirror. A closed `SlotSpendLedgerUpdate` records that same priced spend;
it is not a second balance or a generic spend command. Private post-state
projection verifies every resource/mirror/ledger change, then retains the
exhaustive ordinary mechanical-delta guard. HP, budget/turn, attribution, ordered
proposals and RNG return in one result; exceptions/cancellation publish nothing.
These are local versioned candidates, not approval of Meta C-15/C-16. Production
SM commit/receipt and Bridge/Demo caller cutover remain unimplemented.

Rule selection was checked against the packaged capability audits and
[SRD 5.2.1](https://media.dndbeyond.com/compendium-images/srd/5.2/SRD_CC_v5.2.1.pdf)
(Cleric list, Cure Wounds, Second Wind, spell components and per-turn Slot rule).


## R21: one predeclared Counterspell inside the triggering cast

This extends the existing `combat.intent` evaluation; it adds no new public operation,
registration API, pending handle or externally suspended continuation. SM supplies a
previously authorized complete declaration in CombatSnapshot. Strict local
`PendingReactionState` requires owner, canonical spell/activity, Slot level, casting
source ID, all typed trigger/response fields (including explicit nulls) and source kind.
The capture adapter requires supplied source IDs and is used only for regression.

Only one canonical level-3 Counterspell from a real Wizard 5 Evoker is admitted,
with complete base/subclass grants, known/prepared source, real Intelligence/PB,
free Somatic hand, complete ordinary Slots and Arcane Recovery pool. The canonical
Evoker grants are reviewed as irrelevant to this Abjuration save; original facts
remain in computation/results. Generic subclass/species, effects, active responses,
multiple declarations, nested counters and interactive choices remain unsupported.

The actual Cure Wounds intent generates the existing typed SEES_SPELL_CAST
opportunity. Shared matching/targeting, range/sight and Slot pricing determine
eligibility before RNG. Legal unavailable Reaction/Slot, out-of-range and recorded-dead
reactors leave the declaration armed and consume nothing. Incomplete death closure,
conditions or unreviewed dependencies reject the operation before any draw/payment.
NPCs and PCs use the same rules; their decision to declare is outside the evaluator.

Action pays before the interrupt. If eligible, the reactor consumes its declaration,
Reaction and Slot, emits ReactionTriggered / Counterspell SpellCast, then uses the
original SaveActivity Resolver and shared actor save-modifier projection for the
triggering caster's CON save. Failure emits CastFailed(countered), keeps the main
Slot and skips healing dice; success pays the main Slot and resolves Cure Wounds.
One result includes guarded PendingReactionUpdate, ResourceUpdate/mirrors, each
payer's SlotSpendLedgerUpdate, HP/budgets/turn, ordered events, RNG and read fence.
Exceptions/cancellation publish no partial candidate. Accepted still is not committed.

These are local C-15 candidates. C-04 persistence/windows/information visibility,
C-16 source authorization and production caller migration remain OPEN/unimplemented.
The new nested required facts and nonnegative spend ledger pin Snapshot `/12`,
evaluation `/17`, availability `/11`, evaluator `/23`; older shapes fail explicitly.
No existing SRD content support is newly claimed beyond its migrated executor.


See the [R18–R21 Global Review](stateless-migration-progress.md#global-review-r18r21)
for exact commits, executed gates, confirmed findings, two-dimensional progress and
the historical R21 cross-operation class limitations. R22–R25 below provide the
reviewed complete class encounter/rest loops and bounded spell/reaction composition;
wider class/mechanism coverage and production State Machine integration remain incomplete.


## R22 complete class workflows

[Operation-level admission](stateless-migration-progress.md#r22-operation-specific-complete-class-admission)
validates the intact reviewed Cleric 1, Fighter 1 and Wizard 5 Evoker facts. Plain
Pass/reset, bounded walking, closure and local initialization no longer reject merely
because these classes exist. Long Rest recovers their complete owned finite resources.
Attack still excludes trained mastery and class equipment; effects/reactions keep
separate closure checks. The evaluator binding is `/24`; no new public source API,
continuation or authoritative commit is introduced.

## R23 composed Attack and Concentration

The reviewed ordinary Attack can target an existing canonical Hold Person caster.
Its typed completed damage instance drives one shared CON save after all post-defense
damage types, including Temp HP absorption. The same result carries attack costs,
HP/Temp HP, damage bookkeeping, full concentration lineage cleanup, events and RNG.
See [the R23 boundary and evidence](stateless-migration-progress.md#r23-actual-attack-concentration-composition).
Evaluator `/25` adds no public resolved-damage command or formal cross-repository ABI.

## R24 instant Save / Damage invocation

Canonical Sacred Flame joins the existing immediate invocation executor for complete
Cleric 1 class sources. Shared Delivery/Save/Damage resolves its one target and Action;
known Cantrips cost no Slot and need no prepared entry. Save success is accepted with
no damage. Reviewed existing Concentration composes through R23 in that same result.
The [R24 boundary](stateless-migration-progress.md#r24-existing-instant-save-damage-spell)
keeps new-spell reactions, unknown modifiers and wider geometry unsupported before RNG.
Evaluator `/26` changes no formal source/continuation/commit contract.


## R25 bounded authorized weapon Opportunity Attack

`engine-snapshot/13` changes the existing pending declaration component to a closed
source_kind-discriminated spell / opportunity_attack union. The latter requires
owner_id, mover_id, weapon_id, two_handed, voluntary_leave_reach trigger and
owner_next_turn_start expiry. It is an explicit SM-validated one-shot choice,
not permission inferred from opportunity_attack_weapons and not an Engine decision.
The existing guarded PendingReactionUpdate consumes it on firing or expiry; no new
queue, handle, public operation or authoritative commit is introduced. C-04/C-15/C-16
remain OPEN, and these local records do not establish a trusted cross-repository ABI.
An Agent/Host may propose a strategy; State Machine validates trusted identity,
source and final authorization before including the declaration in Snapshot.

Only one ordinary held melee Character weapon is admitted, on either combat side,
with an accessible owned positive-quantity instance and reviewed complete grants,
mastery/grip/hand facts. A PC or template-less NPC can be the mover; complete reviewed
Cleric/Fighter/Wizard movement profiles remain intact. Stat-block reactors, concurrent
reaction choices, concentration/effect graphs, mastery procs, hidden/obscured sight,
Shield and interactive decisions remain unsupported before payment/RNG. An additional
potential reactor without explicit consent also refuses the whole route in preflight.

The shared leaves_melee_reach predicate supplies reach/LOS/total-cover geometry to
both Legacy and stateless callers. Movement checks the entire route before restoring
RNG, then flushes actual completed steps before each reaction. One Reaction is paid
on hit or miss; the original AttackActivity/Damage Resolver supplies proficiency,
ability, Dodge, crits, resistance/immunity/vulnerability and damage-instance completion.
Damage IDs name the actual reactor. Death/zero HP/zero Speed stop the walk before the
leaving step; only the completed path prefix consumes movement. All actor budgets,
HP/TempHP, conditions/death/processed-damage ledgers, position, consent, events and RNG
are returned together. Legal non-triggers retain consent, consume no Reaction/RNG and
can Pass through its explicit expiry at the owner's next turn start.

## R27 equipped complete-class ordinary attack

Evaluator `/29` adds a reviewed equipped Cleric 1 profile to ordinary Attack and
Pass/reset. Complete canonical Spellcasting/Divine Order/selected Protector grants, class/level facts,
spell slots, casting repertoire and inventory remain in the snapshot and result.
The bounded profile has one canonical mace or quarterstaff, one chain shirt and
an optional shield, each represented by one unique accessible owned instance.
Held weapon, grip, occupied hand, shield and armor category must agree. Canonical
class proficiency grants and shared Armor AC calculation validate offense/defense
facts: STR 16 gives +3, level-1 PB +2; chain shirt caps DEX at +2, shield adds +2.
The quarterstaff uses the existing versatile damage rule for a legal two-hand grip.

The explicit Protector choice adds martial proficiency/heavy training through the
shared passive-change readers; this slice still uses simple weapons/medium armor.
Trained Mastery, species/subclass modifiers,
other class/equipment profiles, passive/magical asset drift, ambiguous or missing
instances and unexplained bonuses remain unsupported before execution. R27
did not open equipped spellcasting or rest; R30 below adds the reviewed spells.
An equipped actor or target uses the same
closure; no real class or equipment facts are stripped to pass an operation gate.
C-04/C-15/C-16 and production SM authorization/commit remain OPEN.

## R28 existing Hold Person across turns

One complete SM-authorized canonical Hold Person source can Pass while its linked
targets remain Paralyzed. The shared snapshot boundary scheduler performs each
target's captured WIS repeat Save at that target's turn end, once per turn serial.
A failed Save advances only its clock; success removes that target's Effect,
Condition and lineage, preserving other targets and their common concentration.
The shared source-turn duration rule ticks only at the caster's turn end and
expires the whole remaining chain at zero. Damage/concentration loss and source
death use the same cleanup. Complete events, explicit RNG and guarded Turn,
Budget, Condition, lifecycle and concentration transitions are returned together.

The bounded turn graph requires living positive-HP actors and the reviewed complete
Wizard source, canonical effect/clock/lineage bijection and supported actor facts.
Dying graphs, new effect creation, other spells and unrelated effects remain Deferred.
The shared Action Gate rejects Paralyzed attacks before RNG; casting already uses
the shared condition prohibition. Released actors can subsequently act and Pass.
Envelope `/18` versions the expanded guarded concentration subset/tick delta;
Snapshot `/13` and Availability `/11` remain unchanged. These are local C-15/C-16
candidates, with no trusted-source ABI or authoritative commit service.

## R29 canonical stat-block Opportunity Attack

Snapshot `/14` extends the closed declaration union with stat_block_opportunity_attack.
Every field is explicit: owner_id, mover_id, monster_slug, stat_block_action_id,
voluntary_leave_reach trigger and owner_next_turn_start expiry. State Machine validates
the trusted source and authorization before supplying this one-shot choice. Presence
of a melee action never grants consent. Envelope `/19` and Availability `/12` version
the nested breaking shape; old versions refuse. Evaluator `/31` admits the semantics.

The canonical Ogre/Greatclub profile is closed against pinned complete template and
actor facts, including abilities, PB, AC, speed, senses, defenses, empty class/trait/
equipment/resource facts and reach. Every bound monster is checked before movement,
including non-trigger, spent Reaction and consumed-declaration paths. The typed melee
activity and shared stat-block magnitude/range carriers supply +6 and 2d8+4, with the
existing crit/Damage/zero-HP rules. Original delivery attribution is shared with Legacy.
The shared R25 trigger and walking core pay Reaction immediately before the leaving
step, flush completed movement, and stop on zero HP/death/zero Speed. Complete guarded
damage/budget/position/pending/death changes, ordered events and RNG return together.

PC and rule-equivalent party NPC movers, including the R27 complete equipped Cleric,
can continue after consumption and compose Pass/reset/ordinary Ogre movement/Attack.
Party NPC zero-HP behavior follows the original party death policy; massive damage
produces actual death. Javelin, other templates, recharge/limited uses, Multiattack,
legendary/trait overlays, competing reactions and interactive choices remain Deferred.
No new Pending service, live handle, public source API or formal ABI is introduced;
C-04/C-15/C-16 remain OPEN.

## R30 equipped Cleric spell composition

Evaluator `/32` admits R27's complete equipped Cleric 1 for canonical immediate
Cure Wounds and Sacred Flame. One-handed mace/quarterstaff plus chain shirt and an
empty other hand preserve class/Protector grants, canonical proficiency, AC, owned
instances, resources and CastingState. The same gear subsequently executes Attack
after Pass/reset. Shield plus held weapon or two-handed grip cannot supply the
Somatic hand; no focus, War Caster, material stock or equipment swap is inferred.

The shared spellcasting component gate uses the original typed V/S/M metadata.
Both reviewed spells require V/S and no M. Unavailable Verbal/Somatic facts reject
before RNG/payment; unknown material/focus, magical or attuned equipment, feature
and proficiency dependencies remain refused. Legacy's original component-attestation
boundary is retained for parity, not treated as independent component proof.

Shared Spell Source, Delivery, Slot/Action pricing, Save/Heal/Damage Resolver and
Turn algorithms remain the calculation path. The strict equipment closure is proved
before enabling its neutral armor facts in spell Save validation. No Snapshot or
Delta shape changes; formal C-04/C-15/C-16 remain OPEN. Equipped mixed concentration
graphs and other spell/component profiles are not admitted by this batch.


## R31 equipped Fighter Sap

Envelope `/20` and evaluator `/33` add a closed `combat.sap_marks_update` candidate.
It includes the complete expected/new typed source-target marks, roster references,
source-next-turn-start expiry and target-next-attack-roll consumption. Duplicate
targets, self marks, absent references and unchanged writes are invalid. SM alone
checks expected state/world/RNG and commits the complete candidate.

A reviewed Fighter 1 retains canonical grants, Defense, exactly three reviewed Sap
trainings (mace/flail/morningstar), owned accessible mace/chain shirt/optional shield,
explicit proficiency/grip/armor AC and Second Wind capacity. AttackActivity supplies
the actual proc on a hit even when damage is immune. The shared pure transitions
consume a mark only after an actual AttackRolled, overwrite rather than stack, and
expire remaining marks at the source's next start. No Legacy fold/hooks execute.

The next ordinary Attack independently restores Sap from the supplied Snapshot and
uses the original disadvantage/cancellation algorithm. Exact permanent implied Prone
can supply the opposing advantage with canonical clause/provenance validation.
Pass/reset and subsequent attacks preserve all class/equipment/resources. Other
masteries, conditions and mixed effect/reaction graphs stay Deferred; C-04/C-15/C-16
and full production State Machine/Bridge migration remain OPEN.


## R32 bounded Hold Person cast creation

Snapshot `/15` requires ActorCastingState.materials, a strict tuple of reviewed
HoldPersonMaterial facts keyed by casting source and owned inventory instance.
An available attestation represents the iron component in a canonical accessible
component pouch; the engine does not infer stock or authorization from prose.
Other component/material/focus profiles remain unsupported.

The actor-authorized combat.intent cast_spell for Hold Person admits a complete
Wizard 5 Evoker, level 2/3 owned slots, named explicit Humanoids and first-slot
Action window. Shared spell delivery/count and original SaveActivity/effect binding
produce one atomic HoldPersonGraphCreate with expected graph/condition/source
absence, canonical identities, captured DC/clock, condition lineage and source
concentration duration. Initial all-success/immunity preserves the original
concentration anchor; it contains no Paralyzed condition or repeat clock. Existing
R28 turn/attack concentration consumers admit this exact anchor and the regular
created graph, including cleanup. Payments, Action/turn, slots and spend attribution,
movement/conditions, events and RNG are one uncommitted candidate result.

Envelope `/21`, evaluator `/34` reject prior shapes/bindings. The graph vocabulary
is an internal local C-15/C-16 candidate, not a generic rules.effect API or a frozen
shared ABI. Recast, mixed graphs and other spell initializations remain unsupported.

## R33 predeclared Shield across requests

Envelope `/22`, Snapshot `/16` and evaluator `/35` admit one SM-authorized canonical
Shield declaration for a complete Wizard 5 Evoker, prepared/known source, owned
level-one slot and explicit Reaction/V/S facts. Availability `/12` retains its outer
version and requires the current nested Snapshot. Older versions are rejected.
The evaluator consumes the supplied declaration; State Machine owns authorization
and persistence. No NPC/Player consent decision is inferred during evaluation.

The original AttackActivity first rolls once and establishes a provisional hit.
ReactionTriggered retains typed original natural/total/AC/critical/source evidence;
the shared scalar AC change kernel reclassifies that same roll after Shield. The
original UtilityActivity applies its canonical +5 effect; slot/Reaction/spend/HP,
ordered events, RNG and turn changes are one atomic uncommitted candidate.

Closed ShieldStateTransition carries complete expected/new canonical effect, empty
condition lineage, retained response and exact owner-next-turn-start expiry ledger,
with owner/source/roster invariants. Subsequent Attack restores +5 from the Snapshot;
Attack or Pass removes the graph at the owner's next turn-start event, preserving
the original event order. Death removes the owner's response as in Legacy. No live
queue, hook, handle, registry or retained evaluation instance executes this path.

The response retains original Magic Missile negation metadata; stateless Magic
Missile casting is still Deferred. Multiple policies, nested/interactive reactions,
mixed Sap/Hold Person/other effect graphs and other equipment profiles fail closed.
Formal C-04/C-15/C-16 and production State Machine/Bridge/Demo cutover remain OPEN.

Current Meta v0.10 also leaves C-19 event-stage Evidence Capture/binding OPEN.
This local result does not claim to supply all event-time perception evidence;
final Snapshot/Delta cannot stand in for every intermediate event stage. Formal
trusted SM/RE stage integration and atomic Evidence/Outbox acceptance remain pending.
