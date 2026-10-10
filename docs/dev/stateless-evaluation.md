# Stateless rule evaluation: current local contract

This is a migration of the existing Rule Engine toward one stateless evaluator.
State Machine owns every authoritative runtime fact and the final transaction.
The conversion is incomplete; see [migration progress and the finite roadmap](stateless-migration-progress.md)
and [the complete field inventory](stateless-evaluation-inventory.md).

## Versions and breaking migration

The current local candidate versions are:

| Contract | Current version | Replaces |
| --- | --- | --- |
| RuleEvaluationRequest / Result | `engine-evaluation/12` | `/1`–`/11` |
| StateSnapshot | `engine-snapshot/8` | `/1`–`/7` |
| Availability request/result | `engine-availability/7` | `/1`–`/6` |
| RulesetBinding evaluator | `dnd5e-evaluation/18` | `/17` and earlier |

Old envelopes, snapshots and results are explicitly rejected. There are no compatibility
aliases for `InventoryCombatSnapshot`, `combat_inventory`, missing equipment or missing
inventory. This is a breaking local protocol revision, not a silent shape change.
Legacy public coroutine signatures remain; their corrected death/turn/payment/Mastery
behavior is shared with the new execution path. A Legacy `CombatOutcome` is a separate
host handoff API and must not be applied as a stateless StateDelta.

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

## Operation and result semantics

Import `evaluate` from `dnd5e_engine.evaluation`, DTOs from `evaluation_contracts` /
`evaluation_state`, deltas from `evaluation_delta`, RNG from `evaluation_rng` and
availability from `evaluation_availability`. Each request has session_id, command_id,
operation_kind, real actor_id, typed payload, complete Snapshot, binding and RNGContext.
No system/source-authority or combat.start payload is invented while those contracts
remain OPEN. SM authenticates/authorizes the caller and approved write scope.

| Operation | Current admitted subset |
| --- | --- |
| combat.intent | One ordinary PC/NPC weapon Attack, an exact reviewed stat-block AttackActivity, explicit plain Pass / End Turn, ordinary Walk, Action Dash / Disengage |
| rules.check | Explicitly adjudicated ordinary noncombat ability/skill check |
| rules.save | Explicit noncombat actor saving throw; six abilities and fixed DC |
| rules.rest | Bounded noncombat Short Rest: explicit Hit Dice, Pact Slots and reviewed Second Wind recovery |
| combat.item | Owned accessible single-use Potion of Healing, living self-drinker with declared free hand |
| combat.close | Consistent terminal victory / TPK; no loot/effects/flight/forced end |

Common weapon properties Finesse/Versatile/Two-Handed/Reach/Heavy use the shared resolver
and explicit held grip. Only untrained Mastery tuples are admitted; trained Masteries,
Light/Loading/Ammunition/Thrown, effects, classes/features, extended budgets, reactions,
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
authoritative event ID/sequence. SM alone validates expected values, references, combined
invariants, permissions and OCC, then atomically commits Delta/RNG/events/receipt/outbox.
Engine returns no durable receipt, does no SQL write or external publication, and never
calculates commit success. Whole-world read fencing is conservative and explicit.

## Death, turn and closure stabilization

The shared terminal Death fold now synchronizes dead_ids, DeathRecord and is_alive=False
for all creature kinds. Zero-HP Characters still awaiting death saves are distinct;
their bounded lifecycle is admitted by explicit Pass (R11); Attack/Item admission remains narrower. Shared turn advancement skips recorded dead
creatures without their start/end hooks or RNG and bounds the all-dead scan. Dead
roster entries and history remain available for outcome projection. After killing one
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
back into mutations. Already-ended/history-ended input rejects a repeated closure.

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

The SM checkout observed during this task pins Engine/Data `602dcb8`, evaluation `/1`/`/4`
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
a complete fresh typed CombatState with empty event history, and guarded per-actor
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

Other feature/item recharge, rest-modifying effects/species/subclasses and Long Rest
remain unsupported before draw. Engine decides no authorization, elapsed world time,
scene/quest change or interruption. SM validates those conditions and atomically
commits expected resource/HP writes, events, RNG and receipt. Repeated evaluation is
deterministic; expected-value/version checks prevent a second application of old writes.
No-draw/event-only version policies remain OPEN. Evaluation /12, Snapshot /8,
availability /7 and evaluator /18 are local candidates, not a frozen Meta C-15 ABI.
