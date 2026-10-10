# Stateless rule evaluation: current local contract

This is a migration of the existing Rule Engine toward one stateless evaluator.
State Machine owns every authoritative runtime fact and the final transaction.
The conversion is incomplete; see [migration progress and the finite roadmap](stateless-migration-progress.md)
and [the complete field inventory](stateless-evaluation-inventory.md).

## Versions and breaking migration

The current local candidate versions are:

| Contract | Current version | Replaces |
| --- | --- | --- |
| RuleEvaluationRequest / Result | `engine-evaluation/6` | `/1`–`/5` |
| StateSnapshot | `engine-snapshot/4` | `/1`–`/3` |
| Availability request/result | `engine-availability/3` | `/1`–`/2` |
| RulesetBinding evaluator | `dnd5e-evaluation/12` | `/11` and earlier |

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
| combat.intent | One ordinary PC/NPC weapon Attack, or an exact reviewed stat-block AttackActivity |
| rules.check | Explicitly adjudicated ordinary noncombat ability/skill check |
| combat.item | Owned accessible single-use Potion of Healing, living self-drinker with declared free hand |
| combat.close | Consistent terminal victory / TPK; no loot/effects/flight/forced end |

Common weapon properties Finesse/Versatile/Two-Handed/Reach/Heavy use the shared resolver
and explicit held grip. Only untrained Mastery tuples are admitted; trained Masteries,
Light/Loading/Ammunition/Thrown, effects, classes/features, extended budgets, reactions,
complex grids, timing, areas, objects, summons/transforms remain unsupported. Stat-block
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
their lifecycle is not newly admitted. Shared turn advancement skips recorded dead
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
restored only after admission. Item compatibility preflight still uses its private sentinel. No-draw and event-only stream/world version policies
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

Item still uses `_submit_live_intent` and a disposable `_LiveCombat`/CombatHandle;
closure still constructs `_LiveCombat` for the shared outcome projector. Check already
uses a pure pipeline. Those remaining adapters are migration debt, not the final
architecture. Exceptions discard private computations; no database/publication or
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
