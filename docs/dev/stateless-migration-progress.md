# Stateful → Stateless migration progress

R9 baseline: `fix/re-eval-contract-stabilization@33cb12fb657b8fa511f297ece62e57cdb8c9a46d`.
The goal is to replace the existing Stateful Rule Engine with one stateless evaluator.
**Full conversion is incomplete.** R9 removes the Legacy execution runtime from the
already admitted ordinary single-target Attack path; it adds no D&D content support.

## Architecture decisions and progress dimensions

Latest Meta MODULE_CONTRACTS §4–6/14.1, STATE_MACHINE_ARCHITECTURE and MVP_SCOPE
are authoritative. DECIDED: SM alone owns authoritative state and final commits;
exactly CombatSnapshot / NonCombatSnapshot share typed mechanical components;
complete dependencies, closed deltas, ordered typed events, explicit RNG and binding
are required. Accepted is not committed. The final evaluator must not depend on
`_LiveCombat`, CombatHandle, `_REGISTRY` or Stateful Orchestrator, even when rebuilt
and discarded for every request. Existing adapters are temporary migration debt.

OPEN: shared wire/fingerprint encodings, RNG no-draw version policy (C-03), event-only
world version (C-07), complex Reaction continuation (C-04), complex Delta DTOs (C-15)
and combat.start/create source authority (C-16). Local versioned DTOs do not settle
these decisions. First Blush remains private MVP acceptance, separate from public
synthetic regressions. No Meta decision or other repository was modified.

Two independent dimensions are mandatory:

- **Stateless External Contract**: the bounded operation has complete input facts,
  proposals, RNG and failure semantics; its internal execution may still use Legacy.
- **Legacy Runtime Dependency Removed**: successful execution is proven independent
  of Legacy runtime/dispatcher/handle/registry. A pure algorithm or external DTO alone
  does not establish this dimension.

## Mechanism and execution inventory

Paths below are relative to `packages/dnd5e-engine/src/dnd5e_engine/`. Full/partial
classification applies only to the stated bounded capability, not its whole family.

| Mechanism | Stateless External Contract | Legacy Runtime Dependency Removed | Execution / remaining work |
| --- | --- | --- | --- |
| Result/binding/RNG envelope | Fully migrated local candidate | Yes for envelope | `evaluation_contracts`, `evaluation_ruleset`, `evaluation_rng`; durable commit/receipt is SM work |
| Combat initialization / Start | Legacy Stateful Only | No | `start_combat/_start_combat`, `build_party`; registration, initiative/hydration, first turn and hooks; no start/create operation |
| Turn / Action Economy | Partially migrated through attack/item | Yes for admitted Attack; no for general turn/item | Shared `turn_rules`, `action_economy_rules`; ordinary payment/reset/dead skipping in Attack. Standalone pass, extra/restricted Actions, general expiry/death-save admission need work |
| Movement / positioning | Legacy Stateful Only | No | `live_movement`, `movement`, `spatial`; pure geometry/costs exist, but position Delta and evaluation entry absent; terrain/cover/OA remain Legacy |
| Ordinary PC/NPC weapon or explicit stat-block single Attack | Fully migrated bounded subset | **Yes, R9** | `evaluation_preflight` → `evaluation_attack.execute_attack` → Activity Resolver → `evaluation_projection.attack_delta`; no Legacy execution context |
| Attack / Damage / Death overall | Partially migrated | Partially | Ordinary roll/hit/miss/crit/R-I-V/temp HP/death/payment/turn and attack-induced next-turn death save migrated; initial dying states, riders, trained Masteries, Multiattack/recharge/legendary/effects remain Legacy |
| Adjudicated noncombat ability/skill check | Fully migrated bounded subset | Yes | `evaluation_checks` → `activities/check_pipeline`; explicit DC/GM flags/proficiency/expertise/Jack/Reliable Talent; no Legacy runtime |
| Saves / skill checks overall | Partially migrated | Partially | `check`, `save_primitive`, `live_save_modifiers`; standalone Save envelope, tools/senses/modifier consumption and effect dependencies missing |
| Owned single-use self-healing consumable | Fully migrated bounded external contract | **No** | `evaluation_items` → `execution_context` → `_submit_live_intent`; inventory/HP/budget/turn Delta exists, runtime extraction remains |
| Inventory / resource payment overall | Partially migrated | No for general execution | Mandatory typed InventoryState; complete consume guard. Equip/transfer/recharge/ammunition/general pool/restore operations absent; Legacy slot/counter/rest machinery remains |
| Conditions / effects / concentration | Legacy Stateful Only for general inputs | No | Complete Snapshot records retained and refused; `effect_lifecycle`, `live_effect_lifecycle`, condition/concentration folds. Attack-produced unconscious/prone/death-save state is a bounded exception |
| Spellcasting / Features | Legacy Stateful Only | No | `spell_execution`, `feature_runtime`, `live_features`, `live_spell_delivery`, `activities/cast`; existing repertoire/costs/timing/grants/riders remain, no evaluation entry |
| Reaction / pending choice | Partially migrated choices only | Yes for draw-free choices; no for reactions | Weapon/missing-DC choices typed; `reactions`, `live_reactions`, `timed_activities` execution/windows/continuation remain Legacy; complex ABI OPEN |
| Areas / environment / objects | Legacy Stateful Only | No | `persistent_areas`, `environment`, `combat_objects`, `ongoing_spell_activation`; state captured then refused; source/lifetime/geometry/history Delta missing |
| Bounded victory / TPK closure | Fully migrated bounded external contract | **No** | `evaluation_closure` → `execution_context` → `_derive_ended_reason/_project_outcome`; no registry/end_combat call, but still `_LiveCombat` dependent |
| Closure / outcome overall | Partially migrated | No | Flight/forced end/effect handoff missing; loot generation and durable cross-combat effects were already Host/deferred gaps |
| Rest / derivation | Legacy Stateful Only as evaluation operation | Pure algorithms already exist; no complete path | `rest`, `build_spec`, `build_party`; resource/HD/recovery envelope and Delta missing |
| General 3D / arbitrary narrative / precise interrupted resume | Unsupported / Post-MVP | Not applicable | Preserve original Host/Deferred boundaries and Meta MVP scope |

Original capabilities remain in the canonical spell capability audit, reviewed data
and original regression suites. Haste, Moonbeam relocation, Darkness/Daylight anchors,
summons/transforms, rest, saving throws and feature/rider rules have not been deleted;
their stateless execution has not yet migrated. Potion of Speed and other originally
Deferred mechanics remain Deferred. Parsed Activities and captured DTOs are not proof
of executable support. Legacy loot is already empty; arrow stack accounting, transfers
and durable cross-combat effects must not become fictitious restoration promises.

## R9 actual extraction and removed dependency edges

Before: `evaluate` → `prepare_attack` → `execution_context` → `_LiveCombat` + default
hooks/private queue; validation via CombatHandle and Orchestrator gates; execution via
`_submit_live_intent`, Legacy folds/turn hooks; recapture via `evaluation_snapshot`.
Availability also constructed and recaptured the Legacy runtime.

After: `evaluate` → read-only `prepare_attack` / immutable AttackPlan → `execute_attack`
→ `build_activity_context` → `resolve_activity` → typed local fold → `attack_delta`
→ existing RuleEvaluationResult/RNGTransition. Availability uses the same draw-free
plan. The core imports none of Orchestrator, evaluation_context or evaluation_snapshot.
`evaluation_actor` preserves required actor fields and supplied resource facts.

Removed Attack/availability edges: context factory; `_LiveCombat` construction; temporary
CombatHandle; `_submit_live_intent`; `_validate_intent_preconditions`; Live action-policy
lookup; `_intent_pre_resolution_failure`; `_emit`/queues/listeners/default hooks;
Legacy turn dispatcher and snapshot recapture. `_get_live`/Registry access remain absent
and are now explicitly prohibited alongside the other edges during successful tests.

The local computation retains only actor copies, immutable combat-record updates, typed
proposed events and a completed-hit accumulator; it has no general sidecar runtime,
transactions, callbacks from a prior request, locks, queues or registered lifetime.
Snapshot retention is read-only input, not a renamed `_LiveCombat`. The private post-state
is only projection input: output remains guarded typed operations, never Snapshot replacement.

| Shared algorithm module | Extracted / retained responsibilities; Legacy reuse |
| --- | --- |
| `attack_rules` | Existing weapon range/reach/grip/proficiency, activity synthesis, action-spent predicate, adjacent hostile legality, static defense projection and stat-block magnitudes; Legacy delegates/aliases |
| `action_economy_rules` | Existing budget refusal algorithm with explicit policy facts; Legacy policy wrapper delegates |
| `turn_rules` | Ordinary Action payment, attack spend, complete reset, next living initiative slot, budget bookkeeping; Legacy payment/consume/reset/open-turn/policy wrappers delegate |
| `damage_rules` | Temp HP/HP balances, full-instance zero-HP outcomes, typed condition materialization and death-record identity; Legacy damage/zero-HP/condition/death folds delegate |
| Existing `activities/resolver`, `attack`, `apply`, `dice`, d20 primitives | Attack ability/to-hit, hit/crit, damage parts and R/I/V remain the original single implementations |
| Existing `build_context`, `actor_stats`, `rules/conditions`, `death_saves` | Ability/PB carriers, condition/speed and natural-1/20 death-save rules reused directly |
| `intents` | Canonical typed command/refusal vocabulary moved out of Orchestrator; Legacy reexports preserve class identity and public imports |

Event ordering and computation wiring differ by entry; mechanical formulas do not fork.
Legacy effect/reactive orchestration is retained around these same kernels for unmigrated
capabilities. Expanding admission before complete rule dependencies/Delta exist is forbidden.

## State dependencies and complete Delta

The [field inventory](stateless-evaluation-inventory.md) remains complete. R9 changes
no Snapshot fields or Delta shapes. Required inventory/equipment/effect/resource facts
are never silently filled. Complex sidecars, traits/features, trained Masteries,
Reaction/Effect/Spell and extended actions remain rejected before payment/RNG.

Attack projects HPDelta, TempHPSet, DeathStateUpdate, ConditionsUpdate,
ActionBudgetUpdate, DamageAttributionUpdate, TurnUpdate, MovementLedgerUpdate,
DamageSequenceUpdate, ProcessedDamageUpdate and DeathLedgerUpdate as applicable.
Every operation guards its expected value. Event history is an ordered prefix plus
proposals; resources/inventory/effects/scene/roster and all unrepresented fields are
checked unchanged. Damage rules accumulate typed packets by damage-instance identity,
not narration, before deciding Character massive damage. Attack-induced death saves
preserve natural-20 HP/prone/movement restoration and explicit RNG draws.

Existing dying/unconscious input remains outside admission; this is not a general
standalone death-save or revive migration. Initialization, position, resource/HD,
effect/condition/concentration graph, reaction continuation, areas/objects and roster
changes still need closed DTO operations and independent consumer tests.

## Remaining production Legacy chains

1. Bridge combat routes create → `start_combat/_start_combat` → `_REGISTRY`; BridgeState
   retains CombatHandles. Intent/advance/end call public Legacy APIs, `_get_live` and
   transaction wrappers. Event collector drains retained queues to history/WebSocket.
2. `nat20_bridge/combat_execution` reads live serial/events/outcome for process-local
   locks/receipts. This is not SM's durable atomic commit.
3. Demo `replay` opens/drives/closes Legacy combats and drains queues. Its UI log replay
   is not Snapshot evaluation and still owns handles.
4. Engine view/query/drain/narration/legendary-resistance APIs and combat-object/wind
   mutation resolve live handles. Ended-runtime retention and cleanup remain.
5. **Item** still constructs `_LiveCombat` and temporary CombatHandle, then calls
   `_submit_live_intent`; **Closure** constructs `_LiveCombat` for outcome rules.
   Attack and availability no longer do either. Check uses no Legacy runtime.

No Bridge/Demo production route changed. A Session must not combine Legacy runtime
ownership with SM authoritative evaluation commits. Public API compatibility here is
a migration constraint, not justification to preserve Legacy indefinitely.

## Cross-repository ABI checkpoint

Read-only observation: SM `4e5cc5ac2765be89f0b4164bc6923083539374c4`, branch
`feat/sm-b14-npc-persistent-state`; dependency pin remains Engine/Data
`602dcb8d448e670049427bbb751d6ed226005298`. Request builder uses evaluation `/1`/`/4`,
Snapshot `/1`/`/2`; manifest accepts evaluator `/4`/`/8`. It cannot consume this branch's
evaluation `/6`, Snapshot `/4`, availability `/3` and evaluator **`/11`**.
No SM file was changed and no SM tests/integration result are claimed.

R9 changes only evaluator implementation binding `/10` → `/11`; strict external shapes
remain unchanged from stabilization. Old evaluator bindings fail visibly. A coordinated
SM batch must upgrade dependency/manifest/request/query versions, supply real equipment
and inventory facts, handle complete allowed Delta operations and historical closure
reports correctly, and atomically commit state/RNG/events/receipt/phase/XP/outbox.
No-draw/event-only policy and source authority remain explicit architecture questions.

## Next four bounded batches

R9's previously proposed initialization scope is superseded by this Attack extraction.
These next batches are a finite foundation plan, not a promise of full conversion in four.

| Batch | Scope / workload | Verifiable exit |
| --- | --- | --- |
| R10: standalone turn and remaining bounded adapters | Medium/large: extract ordinary turn/death-save lifecycle and outcome projection; typed pass/end-turn; move existing consumable/closure onto small computations with shared rules | Attack/item/closure/turn successful isolation forbids all Legacy runtime; two-round/death/save/closure/payment differential tests; no new feature/effect support |
| R11: combat initialization | Large: separate source validation, roster/slot/equipment hydration, initiative and first-turn computation from registration; local start/create DTO only after OPEN trusted-source review | Noncombat → combat → two rounds parity; complete first-state Delta, no registration/hooks; invalid-source and post-roll fault tests; source authority decision explicitly reviewed |
| R12: movement and positioning | Medium/large: typed position Delta; ordinary move/Dash/Disengage, existing grid/terrain/LoS/cover/ledger algorithms; refuse reaction-dependent movement pending interrupt migration | Independent path/collision/range/cost parity and expected-value consumer tests; rejected movement no payment/RNG; no Legacy movement runtime |
| R13: common resource and equipment authority | Large: bounded typed spell/Pact/counter payment/restore and rest/HD recovery; coherent owned/equipped item identity, explicit inventory changes | Actual payer/pool/resource maxima and rest parity; last-unit conflicts, closure no second payment; complete rollback and strict schemas; separately coordinate SM consumer ABI |

Next, migrate general Effect/Condition/Concentration and captured expiry chains, then
bounded predeclared Reaction/interrupts after resolving continuation ABI. Migrate existing
spell/feature families through those shared dependencies once, then ongoing timing,
areas/environment/object sources, summons/transforms/legendary/Multiattack and remaining
bounded content. Use canonical audits plus original tests as the explicit checklist.
Finally switch or retire Legacy Bridge/Demo mode in a reviewed compatibility release.

## Acceptance for removing Legacy Stateful Runtime

1. Every originally executable/bounded production mechanic has a complete stateless
   path with all dependencies/Delta and successful runtime-isolation proof, or explicit
   reviewed retirement. Original Host/Deferred classifications remain truthful.
2. Init/turn/movement/death/resource/equipment/effect/spell/feature/reaction/area/object/
   closure paths reuse shared rules, with independent state/event/RNG parity and fault tests.
3. Production consumers use one state owner; no evaluation path constructs `_LiveCombat`
   or resolves handles/Registry, even temporarily. All Legacy APIs are migrated/retired
   before versioned deletion; no default global RNG dependency survives.
4. SM expected writes, authorization, RNG/events/receipts/outbox commit atomically;
   replay/stale-version/duplicate/last-unit/response-loss tests prove one commit.
5. Combat closes/rewards once and restores correct noncombat balances/effects. Saved
   state migration/rejection and interrupted-combat policy are explicit.
6. Full Engine/Data/Bridge/Demo/static/security/docs/wheel and matching-pin SM integration
   pass; private First Blush acceptance runs separately. Engine Full alone proves neither
   complete Stateless Conversion nor MVP completion.

## R9 verification scope

Existing independent expected-value consumers and Legacy comparisons cover complete
state, ordered events, hit/miss/natural 1/20, R/I/V, temp HP, payment, NPC identities,
turn wrapping and death. New structural tests prohibit Legacy execution during successful
PC/NPC attacks, repeated/concurrent evaluations, post-damage failure/cancellation and a
three-attack multi-enemy sequence compared against the independently called Legacy API.
Extra natural-1/20 death-save tests include Unconscious immunity. A discovered missing
movement reprojection on natural-20 recovery was repaired without weakening equivalence.
Original test fault injection now targets the new actual resolver/fold as well as the
Legacy failure path. Final delivery reports actual Full/coverage/static/security/docs/
wheel commands and timings; unit results do not imply unexecuted CI or SM success.

R9 review also confirmed and repaired a Speed-zero Dodge projection discrepancy.
The existing loss predicate now lives in shared `attack_rules.dodge_benefit_active`,
used by both entries. PC/NPC differential tests cover Dodge on/off, Speed zero/30
and no unspent movement, preserving the distinction between Speed and movement budget.

Final review also reproduced a pre-existing legality hole: a consistent death ledger
with a stale current-turn pointer admitted an attack by a recorded-dead actor. The
shared action gate now rejects that ordinary attack before payment or RNG. PC/NPC
regressions first failed, then passed for both entries, with no proposed changes,
events or RNG transition. This checks supplied terminal death facts; it does not
delegate death rules to SM or change attack-induced death-save admission.
