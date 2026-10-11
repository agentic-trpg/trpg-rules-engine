# Stateful → Stateless migration progress

R9 baseline: `fix/re-eval-contract-stabilization@33cb12fb657b8fa511f297ece62e57cdb8c9a46d`.
The goal is to replace the existing Stateful Rule Engine with one stateless evaluator.
**Full conversion is incomplete.** R10 starts at R9 main
`f4726ef873bcc470ca30daebb4c7d1aa08126890` and removes the remaining Item/Closure
runtime dependencies. R11–R13 add bounded explicit Turn, Movement and a local
initialization core. All admitted slices execute independently; public creation remains
blocked C-16. This extracts existing rules rather than adding content support.

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
| Combat initialization / Start | Complete bounded local candidate; **public ABI blocked C-16** | **Yes, R13–R14** for bounded core and explicit ordinary NPC binding | `evaluation_initialization` / shared initiative_rules and turn_rules; explicit NonCombat setup and closed creation candidate. Legacy start, general hydration and consumer cutover remain |
| Turn / Action Economy | Bounded explicit Pass and Attack/Item; R20/R21 invocation turn completion | **Yes, R11** for bounded lifecycle | `evaluation_turn` plus shared `turn_rules` / death saves; complete reset/dead skipping/round wrap/ordinary dying PC. R22 admits reviewed complete Cleric/Fighter/Wizard Pass/reset; R25 adds one declaration expiry. Wider classes, effects/reactions and extended actions remain incomplete; R17 adds bounded Poison |
| Movement / positioning | Bounded Walk, Action Dash/Disengage | **Yes, R12** for admitted subset | `evaluation_movement` reuses GridTopology/movement/turn rules and closed PositionUpdate. R25 adds one authorized weapon OA; special modes, stat-block reaction templates, forced movement, effects/areas remain Legacy |
| Ordinary PC/NPC weapon or explicit stat-block single Attack | Fully migrated bounded subset | **Yes, R9** | `evaluation_preflight` → `evaluation_attack.execute_attack` → Activity Resolver → `evaluation_projection.attack_delta`; no Legacy execution context |
| Attack / Damage / Death overall | Partially migrated | Partially | Ordinary roll/hit/miss/crit/R-I-V/temp HP/death/payment/turn and attack-induced next-turn death save migrated; riders, trained Masteries, Multiattack/recharge/legendary/effects remain Legacy |
| Adjudicated noncombat ability/skill check | Fully migrated bounded subset | Yes | `evaluation_checks` → `activities/check_pipeline`; explicit DC/GM flags/proficiency/expertise/Jack/Reliable Talent; no Legacy runtime |
| Standalone saving throws | Fully migrated bounded NonCombat actor subset | **Yes, R15** | Shared actor_stats, condition projection and roll_save; six abilities, explicit DC/flags, permanent session auto-fail; no Legacy hydration |
| Concentration damage / lineage | Complete bounded internal candidate; production source ABI C-16 OPEN | **Yes, R18/R23** for existing Hold Person, including actual Attack damage | Shared completed-instance/DC/save/lineage; R23 aggregates ordered post-defense damage types and composes full cleanup with Attack. R24 composes Sacred Flame. Public source and unrelated/mixed effect graphs remain restricted |
| Saves / skill checks overall | Partially migrated | Partially | Tools/senses, combat source/cover, effect and finite modifier consumption remain restricted |
| Owned single-use self-healing consumable | Fully migrated bounded external contract | **Yes, R10** | `evaluation_items` → shared CombatComputation / HealActivity / item_rules; inventory/HP/budget/turn/events/RNG remain complete |
| Inventory / resource payment overall | Partially migrated: owned consumable, bounded rests and rule-priced owned payments | **Yes, R10/R16/R19** for those slices | Inventory/resource mirror guards; shared Slot selection and Feature pricing. General equip/transfer/item recharge/ammunition and production caller migration remain |
| Bounded existing Poison lifecycle | Fully migrated local subset | **Yes, R17** | Authorized single Cunning Strike Poison → shared Save/repeat/expiry/lineage kernels → closed Effect/Condition/Turn proposals; no Apply API |
| Conditions / effects / concentration overall | Partially migrated | Partially | R17 Poison, attack-produced dying conditions and R23 existing Hold Person cleanup migrated; general multi-effect/concentration creation, areas and child cascades remain Legacy |
| Bounded Cure Wounds / Second Wind | Complete bounded local contract | **Yes, R20** | Explicit class/source/preparation/components, shared Delivery/Feature pricing/HealActivity/Turn, atomic pool/mirror/spend/HP/budget/events/RNG |
| Canonical instant Sacred Flame | Complete bounded local contract | **Yes, R24** | Full Cleric 1 PC/NPC class source, shared Delivery/Save/Damage, no Slot cost, real R23 concentration composition, atomic budgets/HP/turn/events/RNG |
| Spellcasting / Features overall | Partially migrated | Partially | R20 direct healing/Second Wind and R24 Sacred Flame execute independently for reviewed profiles; general repertoire/costs/timing/grants/riders/delegated casts remain Legacy |
| One predeclared Counterspell / Cure Wounds trigger | Complete bounded local contract | **Yes, R21** | Real Wizard 5 Evoker; pure matching/targeting and SaveActivity, one atomic trigger/Reaction/payment/delta/events/RNG; no Live dispatcher |
| One authorized weapon Opportunity Attack / Walk | Complete bounded local contract | **Yes, R25** | Explicit Snapshot consent, shared reach/LOS and Attack/Damage, full path-prefix/death/budget/consumption/expiry delta; no Live hooks |
| Reaction / pending choice overall | Partially migrated | Partially | Draw-free weapon/missing-DC choices, R21 Counterspell and R25 weapon OA; other reactions, priorities, nested chains, durable windows and interactive continuation remain Legacy/OPEN |
| Areas / environment / objects | Legacy Stateful Only | No | `persistent_areas`, `environment`, `combat_objects`, `ongoing_spell_activation`; state captured then refused; source/lifetime/geometry/history Delta missing |
| Bounded victory / TPK closure | Fully migrated bounded external contract | **Yes, R10** | `evaluation_closure` → shared outcome_rules on explicit values; XP/history/events and unchanged RNG |
| Closure / outcome overall | Partially migrated | No | Flight/forced end/effect handoff missing; loot generation and durable cross-combat effects were already Host/deferred gaps |
| Bounded Short Rest | Fully migrated local subset | **Yes, R16** | Explicit HD/Pact/Second Wind ownership and capacities → shared rest kernels → closed resource/HP/events/RNG proposals |
| Bounded Long Rest / payment | Complete bounded local contract | **Yes, R19** | Shared recovery/Slot/Scale/Feature pricing; atomic resource mirrors, HP/events and unchanged RNG |
| Rest / derivation overall | Partially migrated | Pure algorithms retained; consumer/general recovery migration incomplete | Other feature/item/species recovery, drain/exhaustion effects and general derivation remain outside the admitted path |
| General 3D / arbitrary narrative / precise interrupted resume | Unsupported / Post-MVP | Not applicable | Preserve original Host/Deferred boundaries and Meta MVP scope |

Original capabilities remain in the canonical spell capability audit, reviewed data
and original regression suites. Haste, Moonbeam relocation, Darkness/Daylight anchors,
summons/transforms, rest, saving throws and feature/rider rules have not been deleted;
their general stateless execution has not yet migrated; R15 admits bounded standalone saves. Potion of Speed and other originally
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
General Reaction/Effect/Spell and extended actions remain rejected before payment/RNG; R17 admits only its reviewed Poison End Turn slice.

Attack projects HPDelta, TempHPSet, DeathStateUpdate, ConditionsUpdate,
ActionBudgetUpdate, DamageAttributionUpdate, TurnUpdate, MovementLedgerUpdate,
DamageSequenceUpdate, ProcessedDamageUpdate and DeathLedgerUpdate as applicable.
Every operation guards its expected value. Event history is an ordered prefix plus
proposals; resources/inventory/effects/scene/roster and all unrepresented fields are
checked unchanged. Damage rules accumulate typed packets by damage-instance identity,
not narration, before deciding Character massive damage. Attack-induced death saves
preserve natural-20 HP/prone/movement restoration and explicit RNG draws.

R9 initially excluded dying/unconscious input. R11 now admits its bounded dying-PC
Pass path; R13–R16 add closed initialization, position and resource/HD operations,
and R17 adds one existing Poison lifecycle. General revive, concentration/effect
graphs, reaction continuation, areas/objects and roster changes still need closed
DTO operations and independent consumer tests.

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
5. No currently admitted evaluation path constructs Legacy runtime. execution_context
   and evaluation_snapshot remain isolated regression adapters. Bridge/Demo still
   use the production Legacy chains above.

No Bridge/Demo production route changed. A Session must not combine Legacy runtime
ownership with SM authoritative evaluation commits. Public API compatibility here is
a migration constraint, not justification to preserve Legacy indefinitely.

## Cross-repository ABI checkpoint

Read-only observation: SM `4e5cc5ac2765be89f0b4164bc6923083539374c4`, branch
`feat/sm-b14-npc-persistent-state`; dependency pin remains Engine/Data
`602dcb8d448e670049427bbb751d6ed226005298`. Request builder uses evaluation `/1`/`/4`,
Snapshot `/1`/`/2`; manifest accepts evaluator `/4`/`/8`. It cannot consume this branch's
evaluation `/12`, Snapshot `/8`, availability `/7` and evaluator **`/18`**.
No SM file was changed and no SM tests/integration result are claimed.

R10 changes only evaluator implementation binding `/11` → `/12`; strict external shapes
remain unchanged from stabilization. Old evaluator bindings fail visibly. A coordinated
SM batch must upgrade dependency/manifest/request/query versions, supply real equipment
and inventory facts, handle complete allowed Delta operations and historical closure
reports correctly, and atomically commit state/RNG/events/receipt/phase/XP/outbox.
No-draw/event-only policy and source authority remain explicit architecture questions.

## R10–R13 foundation batches

R9's previously proposed initialization scope is superseded by this Attack extraction.
These completed bounded batches do not establish full conversion. General mechanisms and consumer cutover remain.

| Batch | Scope / workload | Verifiable exit |
| --- | --- | --- |
| R10: Item and Closure | Completed bounded runtime extraction | Independent successful execution, event/RNG and complete-state parity, post-compute fault isolation |
| R11: standalone Turn | Completed bounded: explicit Pass/End Turn, bounded death saves, reset/round/dead skipping | Independent successful lifecycle and complete Delta, pre-draw hook refusal, rollback and parity |
| R12: movement and positioning | Completed bounded: typed position Delta; ordinary move/Dash/Disengage, existing grid/terrain/ledger algorithms; cover remains refused; refuse reaction-dependent movement pending interrupt migration | Independent path/collision/range/cost parity and expected-value consumer tests; rejected movement no payment/RNG; no Legacy movement runtime |
| R13: combat initialization | Completed local bounded core; public C-16 blocked: bounded NonCombatSnapshot mechanical initialization, local closed creation candidate | Initiative/equipment/first-turn parity without registration; source authority remains OPEN |

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

## R10 verification scope

Real successful Item/Closure executions forbid Legacy constructors, handles, registry,
dispatchers, context factory and outcome wrappers. Tests compare full item state,
event ordering and RNG (including last-resource turn advancement), terminal XP and
historical outcome, parallel repeatability, post-heal faults/cancellation and projection
faults. No Legacy tests were removed and no coverage gates were reduced.

R10 review also covers active walk/swim/climb ledger projection at an empty-area
turn boundary and the remaining movement window after a Bonus Action. The projection
reuses movement.project_speeds/speed_for_mode; it does not create another Speed formula.
The massive-damage capability probe now follows its shared computation call site.

## R11 turn migration evidence

Explicit Pass shares CombatComputation and the original death-save algorithm; it imports
no Legacy runtime or hooks. PC/NPC turns, unused/spent Action, dead skipping, two rounds,
ledger/budget reset, stable PCs and natural 1/20 are checked against independent values
and Legacy event/state/RNG output. Repeated/concurrent evaluations, stale guards and
post-roll failure/cancellation preserve inputs and expose no partial proposals.
Snapshot /5 allows the real terminal failure counter of four; envelope /7, availability
/4 and evaluator /13 reject stale schema identities. General expiry remains unmigrated.

## R12 movement verification scope

Full guarded position/budget/ledger proposals match Legacy events/state/RNG for PC/NPC
Walk, Dash, Disengage, weighted detours, terrain and allied space traversal. Independent
expectations cover movement cost, Action retention, long Dash movement and turn retention.
Whole-route OA preflight, actual weapon Reach, walls/occupied/unreachable/oversized targets,
unsupported dependencies, stale world/position guards, parallel repeats and post-move
fault/cancellation are tested. No supported path calls live_movement or the registry.

## R13 extraction and verification scope

Shared initiative_rules owns fixed/rolled values, surprise/d20 and deterministic ties;
Legacy delegates to it. The local core uses validated actor facts and shared reset,
with no registry/handle/context/dispatcher. Fixed zero, DEX/identity ties, rolled/surprised
values, fresh budget/ledger/first-turn events and unchanged owned equipment/inventory/
resource balances have independent expected values plus full Legacy state/event/RNG
comparisons. Successful parallel evaluation disables Legacy dependencies. Post-roll
exceptions/cancellation and stale/duplicate creation guards expose no partial result.
Required precombat timing/area/object facts refuse when nonempty, before RNG.

Public combat.start/create source authorization and exact shared field mapping remain
blocked C-16; this repository-local candidate is not a production SM integration.
R13 local versions at that boundary: evaluation /9, Snapshot /6, availability /5, evaluator /15.
Legacy Bridge/Demo production behavior is preserved, not migrated or retired.

## Historical priorities after R13 (superseded by R17 checkpoint)

| Priority | Existing capability to migrate | Required exit |
| --- | --- | --- |
| 1 | Bounded Effect/Condition/Concentration and turn expiry | Typed lifecycle/source closure and deltas; independent event/state/RNG parity without live_effect_lifecycle |
| 2 | Predeclared Reaction and interrupt windows | Resolve C-04 continuation; complete payment/window state and no Legacy reaction dispatcher |
| 3 | Existing damage/healing/save Spell and Feature families | Reuse original resolvers with migrated lifecycle/resources; no spell/feature Live wrappers |
| 4 | Persistent areas, timing, environment and combat objects | Typed geometry/source/history/lifetime transitions and position-trigger parity without runtime sidecars |

In parallel, review C-16 plus shared wire/binding/Delta/RNG policies, then coordinate
SM pin/DTO/atomic consumer upgrade and later Bridge/Demo cutover. Those prerequisites
are not completed by Engine test coverage. Only after original retained capabilities
and all consumers migrate can the legacy executor exit.


## R14 NPC initialization closure

R14 starts at merged R13 `6dad057f5b988a75f0d6d0102a1dd57033f41f51`.
The external contract is complete for explicitly approved ordinary NPC template
bindings; the internal runtime dependency is removed for the successful
NonCombat initialization → CombatSnapshot → explicit stat-block attack chain.
Canonical projection is shared with Legacy, not recreated around LiveCombat.
Binding carries actor/template/data revision and all admitted mechanical facts are
verified before RNG. Typed creation retains inventory, HP, resources and explicit
monster identity/use maps. Public creation remains blocked C-16; general hydration,
complex templates and reaction-dependent movement remain Legacy/unsupported.

New regressions compare real SRD Bandit fixed/rolled initialization and explicit
Light Crossbow attack with independent values and the separately invoked Legacy API,
including complete state, event order and RNG. Forbidden Legacy factories/hooks/
registry/global random, repeated evaluation, malformed bindings/facts, stale creation
preconditions and post-draw faults test the actual execution boundary. Integration
and final review outcomes are recorded in the batch delivery evidence.


## R15 saving throw extraction

R15 starts at R14 `a67be96dbce94b5622fbabf0d750e982bf0e1c49`.
Both dimensions are complete for the bounded standalone NonCombat Actor Save. The
new execution uses full actor facts → shared modifier/condition projection → roll_save;
it never constructs Legacy hydration, handles, registry or a Stateful Orchestrator.
The shared primitive gains only optional explicit source provenance; its existing
Legacy roll, auto-fail and RNG algorithms remain the single implementation.

Independent expected values and separately invoked Legacy SaveActivity compare six
abilities, PC/NPC proficiency, natural 1/20, success/failure, advantage cancellation,
auto-failure and exact RNG successor. Concurrent replay and post-resolution failure/
cancellation leave inputs untouched. Missing targets, noncanonical clauses, cover,
features, active effects and finite-use save modifiers refuse before RNG. Snapshot
/7 and availability /6 are unchanged; local evaluation /11 and evaluator /17 pin the
new Actor operation. This does not migrate general saves, concentration or C-16.


## R16 resource and rest migration

R16 starts at R15 `315242732c3a34616e89cde834b0c4445c02592e`.
Both dimensions are complete for the bounded Short Rest path: explicit typed Hit Dice,
Pact Slots and Second Wind recovery with guarded resource/HP proposals and ordered
RNG/events. The original rest algorithms were already pure. The removed execution
dependency is the need to orchestrate their inputs and resource writeback through
Legacy slot/custom-counter containers or manual Host-only result fragments. The new
request reads only the supplied authoritative resource/actor components and proposes
closed writes; it does not construct a new Stateful Runtime.

Independent values and separately invoked original Rest/recovery resolvers verify
mixed pool draw order, per-die minimum-one healing, HP caps, last units, actual SRD
Fighter Second Wind recovery, Pact versus ordinary slots, replay/concurrency and
pre-draw unsupported dependencies. Expected-value consumers, stale versions and
post-roll/post-projection faults prove HP/resources/RNG remain atomic proposals.
At R16 (historical; see R19 for bounded Long Rest), explicit item-instance and
action-budget ownership remain separate. Long Rest and
general feature/item/effect recovery are restricted; world time and rest permission
remain SM work. Local versions are evaluation /12, Snapshot /8, availability /7,
evaluator /18; shared encodings and complex Delta ABI remain OPEN.


## R17 ongoing effect extraction

R17 starts at R16 `8f33de17de42577493b7893b336413ab20489c4e`.
Both dimensions are complete for one already authorized, nonconcentration SRD Cunning
Strike Poison lifecycle. Explicit Pass retains R11 turn advancement, calls the R15
Save core, and shares original OngoingEffectLifecycle repeat/cap arithmetic and the
extracted condition lineage filter. Removed dependency edges for this successful
chain: live_effect_lifecycle.run_repeats/roll_live_save, Legacy hydration/reaction hooks,
registered Turn hooks, _emit expiry folds and Snapshot recapture. No replacement
persistent runtime or general hook dispatcher is introduced.

Full old/new Effect/Lifecycle/ConditionLink guards, Condition/Turn deltas, event order,
RNG and whole-world fence are complete for the selected slice. Real canonical Poison,
PC/NPC repeat success/failure, same-boundary duration cap, no-draw source turns,
multiple committed turns, replay/concurrency, pre-draw refusals and post-Save/expiry/
projection faults have independent expected values and separately invoked Legacy parity.
No C-16 application API or concentration support is claimed. General child effects,
multiple origins/effects, one-use modifiers, areas and interruption remain restricted.
Local versions: evaluation /13, Snapshot /9, availability /8, evaluator /19.
Lifecycle clock/application/spec/repeat wire fields are required and serialize fully;
missing dependencies cannot be repaired with Legacy authoring defaults.

## Historical next four migration priorities after R17

| Batch candidate | Existing execution dependency to remove | Required exit |
| --- | --- | --- |
| R18: concentration and multi-effect lineage | live concentration checks/drop/linked expiry and _emit cascades | Explicit authorized graph, shared save/damage core, closed atomic cleanup and complete event/RNG parity; no new source API |
| R19: broader resource payment and Long Rest | Legacy slot/feature/item payment and recovery containers | Typed owned capacities, original cost/recovery resolvers, finite modifier consumption and last-unit/rollback parity |
| R20: bounded existing Spell/Feature execution | live spell/feature preparation and resolver contexts | Complete target/source/repertoire/cost/effect closure using R15–R19 kernels; no Live hydration |
| R21: predeclared Reaction continuation | Legacy reaction dispatcher/interrupt runtime | Resolve Meta C-04 before ABI publication; complete window/payment/RNG/continuation state and replay parity |

These are proposed priorities, not new approved APIs. Persistent area/object/timing,
complex monsters, general derivation, authoritative SM consumer integration and the
Bridge/Demo production cutover still follow. Public creation and C-16, complex Delta
C-15, shared wire/RNG/commit policies stay OPEN. Migration completion is measured by
removed original execution chains and migrated existing rules, not DTO/file counts.


## R18 concentration and effect lineage candidate

R18 starts at merged R17 `4f320a2e752826297fc78bf53cbac439934a9461`.
The **internal** `evaluate_concentration_candidate` accepts complete CombatSnapshot,
pinned binding, explicit RNG and already authorized/resolved positive single-packet
damage instances. This is not a public `rules.effect`, attack or source-authorization
API; C-16 remains OPEN. One canonical existing Hold Person source may link several
explicit distinct targets. Full source/target, immutable apply-time save metadata,
clock, condition provenance and concentration countdown are verified before RNG.
Unknown/mixed/overlapping effect graphs and additional mechanics refuse atomically.

The chain uses shared damage balances/zero-HP/death, the R15 Save primitive, the
extracted original 2024 concentration DC formula and shared lineage filtering. It
removes the successful slice's dependency on live Save hooks, concentration folds,
Registry, effect hooks and Snapshot recapture. Each positive instance checks the
actual CON modifier/proficiency; later instances after loss make no extra save.
Temp HP still counts toward damage taken. Terminal death/incapacitation ends the
complete graph even after a successful save. Resolved multi-type packets within one
instance remain unsupported; this candidate cannot hide unresolved damage rules.

Canonical Wizard/Fighter/Cleric/Warlock class identity and canonical reviewed
Spellcasting, Arcane Recovery and Second Wind may be retained when they have no
passive damage/CON-save dependency. Complete explicit grants remain required;
modified/unknown grants, subclass/species dependencies, traits and other effects
are refused. Only the admission copy strips facts already proved irrelevant;
actual computation, preserved snapshot and returned deltas keep the original facts.

Local concentration removal plus existing EffectLifecycleUpdate/Condition/HP/death/
attribution/sequence writes form one StateDelta with ordered proposals, RNG and a
whole-world read fence. They are local **C-15 candidates**, not approved permanent
cross-module ABI. Public operations remain the existing six. Evaluation `/14` and
evaluator `/20` pin the local Delta addition; Snapshot `/9` is unchanged. The R17
generated Lifecycle JSON Schema required-field mismatch remains a publication debt.
Two dimensions: external production integration is **not complete** (C-15/C-16),
while runtime dependency is removed for this tested internal damage/lineage slice.


## R19 resource payment and Long Rest

R19 starts at R18 `7a12853f04ec3d11f550f6db167ffa1e9313c83d`.
Both dimensions are complete for bounded Actor Long Rest and the rule-priced
internal payment core. The original `resolve_long_rest` / recovery and Scale/
Slot derivation algorithms are reused, not rewritten. Slot selection/arithmetic
is extracted from Legacy `_take_spell_slot`; Legacy keeps only its transitional
container writeback. New payments consume explicit owned ResourceState and original
FeatureInvocation payments with verified maximum/cost. No Live runtime is read.

A pool and its actor balance are one mechanical fact: ResourceUpdate's consumer
checks expected pool, snapshot mirror closure and world version, then writes all
mirrors atomically with HP/events/RNG. Multiple owners cannot fund one another.
Long Rest restores all HP/Hit Dice and actual ordinary/Pact maxima. Only canonical
Second Wind and Arcane Recovery finite-feature recovery are admitted, with canonical
class/grant ownership and shared formula/Scale capacity checks. Spellcasting and
Ritual Adept do not independently recover resources. Existing Short Rest behavior
and its Pact/Hit Dice/Second Wind rules are retained; Long Rest with Hit Dice spends
is now explicitly rejected as an illegal input, rather than an absent capability.

No world clock, permission, interruption, Scene or Quest is calculated. Exhaustion,
HP/ability-drain effects, species/feat recovery, other counters and owned items with
unmigrated recovery/passives are unsupported. Uncharged single-use nonrecovering
inventory remains unchanged, never regenerated. Long Rest is no-draw, including
full-HP/no-change results; event-only version policy remains Meta OPEN.

Independent full-pool/HP/mirror expectations, original resolver comparisons,
actual Second Wind preflight, Legacy Slot order/ledger, last unit, foreign owner,
malformed capacities, replay/conflicts and post-recovery/proposal faults cover the
real paths. Versions: evaluation `/15`, Snapshot `/10`, availability `/9`, evaluator
`/21`; Snapshot changes pin the expanded closed Rest event history shape. These
local encodings do not approve C-15 or change the six public operation kinds.


## R20 — actual immediate Spell and Feature extraction

Starts at R19 `0f09be00ad1f48d80ff5c3c3883d8ee3110c9d9a`, on
`refactor/r20-stateless-spell-feature`. Local versions advance to envelope `/16`,
Snapshot `/11`, Availability `/10`, evaluator `/22`; old shapes fail explicitly.

- **External contract:** real Cleric 1 Cure Wounds (known/prepared reviewed source,
  explicit Verbal/free Somatic, one live Touch target, first ordinary casting
  window, Action and owned level-1 Slot) and real Fighter 1 Second Wind (canonical
  full grants, shared class scale, Bonus Action, owned Feature Uses) return complete
  atomic candidates. Bounded PC/NPC mechanics are identical. No arbitrary spend or
  source-authority API is added.
- **Dependency removal:** admitted execution uses Delivery/Activity Resolver,
  feature_runtime and R19 payments directly. It does not instantiate a live combat,
  call the Legacy spell/feature orchestrator, fold live hooks or use a handle/registry.
  Only regression setup/capture invokes Legacy. Touch conversion is shared with the
  old executor rather than implementing another distance rule.
- **Evidence:** new tests compare entire Legacy post-snapshots, proposal ordering,
  RNG and independently known 2d8+Wis / 1d10+Fighter-level outcomes; include legal
  costly no-heal Swarm, full HP, final owned unit, refusal before draws/payment,
  replay/concurrency, version/reapply guards, exceptions/cancellation and AST isolation.
- **Remaining:** only these exact profiles/mechanisms are admitted. General spell
  history, material components/equipment, other class levels/sources/modifiers,
  Reaction, Concentration, multi-Activity and ongoing/delegated execution stay
  unsupported. R18 Concentration is still an internal candidate, not composed with
  R20 healing. C-04/C-15/C-16 and production integration stay OPEN/unimplemented.

R20 review also found the shared Context Builder's old class-NPC approximation:
identical Cleric attributes healed 17 for a PC and 19 for a Monster-controlled
class actor. Explicit class NPCs now use the same real ability/PB branch as PCs;
classless/template compatibility behavior remains separate. Independent expected
value tests and full Legacy differentials verify the corrected shared path.
The initial gate was stopped before this correction; only the subsequent final
gate is completion evidence.


## R21 — bounded predeclared Counterspell extraction

Starts at R20 `3a1676156eb2ac586c79802ae80e8036cef8694d`, branch
`refactor/r21-predeclared-reactions`. Both progress dimensions are complete only
for one existing Counterspell declaration during the admitted Cure Wounds cast.
The full Wizard 5 Evoker class/subclass grants and explicit known/prepared source
remain present. Shared pure reaction matching/targeting, Slot pricing, actor save
modifiers and SaveActivity Resolver replace Live reaction eligibility/dispatcher,
Slot containers, hydration/hook callbacks and pending queues for this slice.

Action/Reaction costs, declaration consumption, owner-specific Slot/mirror/ledger,
CON Save, possible cast cancellation, healing/turn/events and RNG form one proposal.
Countered Cure Wounds costs Action but preserves its Slot. Eligible Reaction pays
once even when the CON save succeeds; no healing dice are drawn on failure. The
canonical S-only Counterspell does not require Verbal availability. Same mechanics
serve PCs/NPCs; strategy, authorization and final commit remain outside Engine.

Tests use real canonical class/subclass grants and original Legacy execution for
full snapshot/event/RNG comparison, plus independent DC/save/cost/draw-order values.
They cover retained ineligible declarations, malformed sources/typed responses,
last Slot, complete death records, unsupported incapacity, replay/concurrency,
world/reapply guards, nested required fields and post-Reaction failure/cancellation.
Only a private local candidate is computed; failures cannot publish partial costs.

Current versions: envelope `/17`, Snapshot `/12`, Availability `/11`, evaluator
`/23`. C-04 complex interactive continuation, C-15 publication and C-16 creation/
source authority remain OPEN. General Shield/OA, multiple predeclarations, chains,
spell/effect combinations and production Bridge/Demo integration remain unmigrated.
R18 remains an internal Concentration candidate, not composed with R20/R21 casts.
The earlier R17 next-batch table is historical planning, not an ABI requirement;
R21 deliberately needs no external decision or continuation.


R21 pre-commit cumulative review reproduced an R18 malformed-state defect:
a complete recorded-dead source could still retain Concentration and enter a
CON draw. A failing-before-fix regression now proves that dead/zero-HP sources
fail closed before RNG. The correction is included in R21; no live graph is
silently repaired. The initial R21 Integration was stopped before this edit and
is not passing evidence; the subsequent final gate covers the correction.


## Global Review R18–R21

Review baseline: `main@4f320a2e752826297fc78bf53cbac439934a9461`.
All batch source, tests, development docs, commits and pushes belong to
`agentic-trpg/trpg-rules-engine`. The separate `agentic-trpg/agentic-trpg`
architecture documents were read as constraints; that repository was not modified.
Reviewed actual commit ancestry, cumulative source/test diff, calculation/projection
modules, canonical data and acceptance assertions separately from batch summaries.
The four branches are stacked in this order, without merge/rebase/force push:

| Batch | Branch | Actual commit |
| --- | --- | --- |
| R18 | `refactor/r18-concentration-lineage` | `7a12853f04ec3d11f550f6db167ffa1e9313c83d` |
| R19 | `refactor/r19-resource-payment-long-rest` | `0f09be00ad1f48d80ff5c3c3883d8ee3110c9d9a` |
| R20 | `refactor/r20-stateless-spell-feature` | `3a1676156eb2ac586c79802ae80e8036cef8694d` |
| R21 | `refactor/r21-predeclared-reactions` | `f9200283d490ff094d2e112778a7c0a199ee0e37` |

### Architecture and actual dependency removal

The mechanism matrix above separates the two required dimensions. R18's existing
Hold Person damage/CON-save/full-lineage cleanup is an **internal candidate**, not
public damage source authorization. R19 removes live resource/recovery containers
from bounded Long Rest and original rule-priced payment. R20 removes the actual
Cure Wounds / Second Wind live executor dependencies for full reviewed Cleric 1 /
Fighter 1 profiles. R21 removes live Counterspell selection/queue/dispatcher/hooks
for one authorized predeclaration inside its triggering cast. None constructs a
LiveCombat, resolves a Handle/Registry or retains cross-request execution state.
Isolation tests prohibit those calls during successful real rule execution.

Existing Resolver/dice/healing/save/Rest/Feature/Scale/Slot/Turn rules are reused.
Only ordinary Slot selection, Touch range conversion and concentration DC were
extracted for shared Legacy/new use; no second D&D formula implementation or new
SRD content was introduced. Per-call actor copies, graph selection and projections
produce proposals; they are not an authoritative commit engine or a rebuilt live
runtime. Typed deltas, complete owner-specific mirrors/spend attribution, ordered
events, RNG and a conservative whole-world read fence are returned atomically.
Independent consumers guard all expected values before publishing; production SM
persistence/authorization/idempotency/commit integration is still unimplemented.

Meta MODULE_CONTRACTS remains highest authority. Exactly two Snapshot contexts,
State Machine ownership, four-state results and accepted-versus-committed remain
unchanged. C-04 durable interactive continuation, C-15 complex DTO publication,
C-16 creation/source authority and other OPEN choices remain OPEN. Local version
`engine-evaluation/17`, Snapshot `/12`, Availability `/11`, evaluator `/23` do not
approve those cross-module ABIs. Legacy APIs remain transitional regression and
production-caller debt, never the final engine architecture.

### Confirmed findings versus unsupported work

| Severity / category | Finding and disposition |
| --- | --- |
| P0 | None confirmed in reviewed bounded execution. |
| P1, resolved in R20 | Shared Context Builder used artificial NPC ability magnitudes for explicit class actors: identical Cleric healed 17 as PC and 19 as Monster. Full class NPCs now use real ability/level; independent values and full Legacy/event/RNG regressions cover the correction. |
| P2, resolved in R21 | R18 accepted a recorded-dead source still carrying Concentration, drawing CON RNG and retaining the stale graph on success. A regression failed before the fix; positive-HP/alive/dead-ledger admission now refuses before RNG and publishes nothing. |
| P2, unresolved publication debt inherited from R17 | Generated OngoingEffectLifecycle JSON Schema lists `expires_round`, `expiry_actor_id`, `last_repeat_turn_serial`, `remaining_one_use_modifiers` as optional, while LifecycleRecord runtime requires every field. Actual missing-field requests fail closed. Align published schema before external ABI release; this does not make missing facts executable. |
| Unsupported composition | R18 Concentration is not wired into public Attack/Spell. General effects/conditions, mixed lineage, multi-type damage, nested/interactive reactions and other spell/feature/equipment profiles remain unsupported. |
| Unsupported workflow, executed review probes | After actual R20 Cure Wounds / Second Wind, complete-class Long Rest returns `unsupported: rest.dependencies`; next ordinary Pass with the full class roster returns `unsupported: turn.capability`. No delta/events/RNG publish. Thus these are complete bounded invocations, **not** a fully stateless class-character encounter/rest loop. |
| Architecture debt | General class-aware dependency profiles, spell/feature Availability, production Bridge/Demo caller cutover, durable SM integration, broad Runtime retirement and the private First Blush acceptance remain incomplete. No unsupported/Deferred content is promoted by these tests. |

No unresolved confirmed P0/P1 remains in these bounded paths. The schema debt is
visible and blocks ABI publication, not local candidate execution. Coverage and
Legacy parity do not imply all supported production mechanics are migrated.

### Executed evidence

Each batch ran its final Integration gate against the exact preceding commit.
The preserved pre-existing untracked `.evaluation-worktree/` caused conservative
all-package fallback; it was neither hidden, deleted nor staged. No tests were
deleted or coverage thresholds lowered. Interrupted gates are not green evidence.

| Final gate | Data | Engine | Bridge | Demo | Total gate time |
| --- | --- | --- | --- | --- | --- |
| R18 Integration | 671 passed, 1 skipped | 8101 passed | 139 passed | 54 passed | 700.87 s |
| R19 Integration | 671 passed, 1 skipped | 8139 passed | 139 passed | 54 passed | 710.20 s |
| R20 Integration | 671 passed, 1 skipped | 8192 passed | 139 passed | 54 passed | 692.11 s |
| R21 Integration | 671 passed, 1 skipped | 8237 passed | 139 passed | 54 passed | 730.14 s |

Final combined focused tests: `test_stateless_concentration.py`,
`test_stateless_long_rest_payments.py`, `test_stateless_spell_feature.py`,
`test_stateless_reactions.py`: **177 passed in 7.61 s**. Full snapshot/event/RNG
Legacy differential is combined with independent DC/formula/cost/draw-order
expectations, full-HP/costly-no-heal, last unit, owner/capacity/mirror refusal,
complete death/unsupported incapacity, replay/concurrency, JSON, world/reapply
guards, exceptions/cancellation and structural isolation. The independent review
also executed the four unsupported class workflow probes described above and
inspected the actual Lifecycle JSON Schema/runtime mismatch.

Full Validation **PASSED in 833.89 s** on reviewed R21 source
`f9200283d490ff094d2e112778a7c0a199ee0e37`.
Command: `python tools/validate.py full --base 4f320a2e752826297fc78bf53cbac439934a9461`.

| Full package gate | Actual tests | Actual coverage | Original threshold |
| --- | --- | --- | --- |
| Data | 671 passed, 1 skipped | 97.56% | 96% |
| Engine | 8237 passed | 95.37% | 90% |
| Bridge | 139 passed | 97.05% | 85% |
| Demo | 54 passed | Not configured | Unchanged |

Ruff, format checks, Mypy, all 24 tooling tests, Bandit, examples and strict
MkDocs passed. All three wheels built and imported in an isolated clean
installation; installed corpus/grid/Haste/Moonbeam and Bridge HTTP smoke passed.
On Windows the verified temporary Makefile adapter reads each original package
recipe, executes its commands and fails on errors; no coverage/security/build/
wheel step is replaced by a success stub. The complete log is retained locally
as `R18-R21-full.log` in the temporary `agentic-trpg-make-r10-r13` directory.
This is local Windows evidence, not a claim that native GitHub Actions or private
MVP end-to-end acceptance ran. Subsequent review-status edits affect docs only
and receive a separate strict documentation gate.

### Next four batches that reduce remaining migration work

| Proposed batch | Existing dependency to remove | Completion evidence |
| --- | --- | --- |
| R22: shared operation-specific class dependency profiles | Broad global class/grant refusal in Pass/Attack/Move/Rest prevents full R20/R21 actor workflows | Retain real complete grants and support reviewed no-extra-effect combinations across heal → turn → rest → heal; refuse unknown modifiers before costs/RNG; remove corresponding live eligibility/turn/recovery calls. |
| R23: compose concentration with actual Attack/damage | R18 still needs separately resolved internal damage rather than the public executor | Produce real damage instances through the shared Resolver, run CON saves and full graph cleanup in the same guarded result, with source closure and multi-packet aggregation; obtain needed C-15/C-16 decisions before publishing ABI. |
| R24: existing instant Save/Damage Spell family | Live spell hydration/damage/save orchestration for a reviewed existing spell | Reuse R19 payments, original Delivery/Save/Damage Resolver and composed R23 effects; prove actual class PC/NPC costs, per-target outcomes, event/RNG order and rollback without live hooks. |
| R25: existing Shield / opportunity-attack predeclaration | Live Attack/Movement reaction hooks and effect/turn expiry | One complete no-new-choice trigger/response, payment, effect lifetime and follow-up action in one evaluation; no hidden handle; general interactive C-04 stays OPEN. |

Priority is removing remaining real Stateful execution dependencies and closing
workflows, not adding spells, DTOs, adapters or files. With the final Full gate
passing and no unresolved confirmed P0/P1, merging these **bounded cumulative
extractions** is recommended as migration progress, with the P2 publication debt
and unsupported workflow gaps explicit. Do not label the full Rule Engine
migration/MVP/ABI complete. No merge is executed by this task; no other repository
is modified.


## R22 operation-specific complete class admission

Base: `ac0fe93060b7377eb08662f12afc6230b1d33f45`; branch:
`refactor/r22-class-operation-dependencies`. Evaluator binding `/24`; no wire DTO change.

| Operation | Reviewed neutral dependencies | Remaining mechanical blockers |
| --- | --- | --- |
| Cure Wounds / Second Wind | Complete canonical Cleric 1 / Fighter 1 grants and capacities; Defense without armor | Unknown grants, modifiers, equipment; R21 alone admits Counterspell |
| Pass / full turn reset | Same profiles plus complete Wizard 5 Evoker; activated features do not run on plain boundaries | Effects use separate R17 admission; Reaction, species, class hooks outside reviewed levels |
| Walk / Action Dash / Disengage | Canonical complete class grants; no speed-granting feature at these levels | Potential OA, effects, template reach, special movement remain restricted |
| Ordinary Attack with reviewed class roster | Unarmored actors have no passive AC/damage hooks; no trained mastery execution | Trained mastery, armor, class equipment, traits, riders and effects still fail closed |
| Combat Close / local Start candidate | Complete class/pool facts remain; ordinary outcome and initiative/reset rules | General source creation C-16, effects, unknown initiative hooks |
| Long Rest | Canonical full base/subclass grants and all finite pools; shared feature/Slot/HD recovery | Species/items/effects and unreviewed recovery remain blocked |
| Short Rest | Full Cleric 1 / Fighter 1 use existing HD/Second Wind recovery | Wizard's optional Arcane Recovery invocation is not inferred |

`evaluation_class_dependencies` validates original class, subclass, feature, equipment,
custom-counter and pool facts. `snapshot_support_failure` calls the operation's reviewed
actor checker directly; Healing no longer erases class/feature/trait fields in a copied
Snapshot. Unknown features and actual attack modifiers cannot be waived as neutral.
Previously admitted R16/R19 recovery-only profiles retain their existing checks.

Stateless External Contract: complete Cleric/Fighter invocation -> Pass/reset -> bounded
Walk -> ally's lethal Attack -> Combat Close -> Long Rest -> local Start -> invocation
again. Tests consume guarded typed operations; the Host scopes actors after closure and
supplies a distinct new enemy, preserving every retained actor field. Wizard Evoker
Counterspell -> multiple Pass/reset -> Long Rest is also exercised. Full combat state,
resource mirrors, effects/reactions, event order and RNG match Legacy on the shared
encounter sequence; repeated evaluation and stale payment guards are checked.

Legacy Runtime Dependency Removed: admitted operations execute with Legacy runtime
entrypoints disabled. Existing turn/movement/rest/initialization/outcome kernels remain
shared. No production Bridge/Demo caller was switched or Legacy function deleted:
remaining callers still require it. C-04/C-15/C-16 and formal cross-repository ABI remain
OPEN. R17's required-field/schema discrepancy is still a publication blocker.

Development evidence: 98 existing Rest/Initialization/Attack regressions passed in
5.94s; eight new complete-class workflow/refusal cases passed in 2.10s. The first wider
run exposed admission regressions in existing recovery-only and classless initialization
profiles; these were fixed without modifying those tests. Windows uses the root
Makefile's exact `tools/validate.py` entrypoint because GNU Make is absent.


R22 final gate: `python tools/validate.py fast --plan --base ac0fe93...` inspected
(the full SHA above was used), then `python tools/validate.py integration --base
ac0fe93060b7377eb08662f12afc6230b1d33f45` passed in **827.96s**. The existing untracked
`.evaluation-worktree/` conservatively broadened selection; it was preserved and not
staged. Local Python 3.14.7: Data **671 passed / 1 skipped** (134.73s), Engine
**8,245 passed** (610.05s), Bridge **139** (8.96s), Demo **54** (16.69s), tooling **24**;
all selected Ruff/format/Mypy, examples and strict MkDocs passed. Integration does not
run Full's coverage/security/wheel gates; the requested final R25 Full must run them.
This is local evidence, not a GitHub Actions result.

## R23 actual Attack / Concentration composition

Base: `b503f4e89c68a5d6f124735d29609680e7792dea`; branch:
`refactor/r23-attack-concentration-composition`. Evaluator binding `/25`.

Stateless External Contract: an ordinary legal weapon Attack against the one reviewed
Hold Person source now resolves hit, all damage types, a CON save and complete linked
effect cleanup in one guarded result. Full Wizard 5 Evoker facts remain in the snapshot.
The graph admits multiple distinct held targets with captured canonical effects,
condition provenance and lifecycle clocks; unrelated/overlapping effects, unknown save
hooks, missing sources, reactions and dead concentration sources refuse before RNG.
Successful saves retain the graph. Failure, death or the actual Unconscious condition
removes source, chain, effects, conditions and clocks together. No public `rules.damage`
or `rules.effect` operation, source authorization or ABI freeze is introduced.

Legacy Runtime Dependency Removed: the admitted Attack uses the shared Activity/Damage
Resolver and R18's private concentration fold, without live damage/concentration hooks.
The intact graph replaces R18's stripped validation view. DC and saving throws use the
existing shared kernels. The completed typed DamageInstanceContext carries post-defense
damage across types, including damage absorbed by Temp HP; zero damage skips the save.
A confirmed Legacy defect made two CON checks for one mixed-type attack. Its hook now
uses that same completed-instance boundary (one 24-damage instance -> one DC 12 save).
Legacy remains for other callers and wider effects/reactions; no production caller is
cut over and no live function is deleted.

Development evidence: the original mixed-type regression failed with two DC 10 checks.
After repair, **124 tests passed in 5.36s**, including real PC/NPC attacks, independent
damage/DC expectations, full Legacy state/event/RNG parity, miss, resistance, immunity,
Temp HP, lethal damage, multiple held targets, replay/concurrency, guarded stale/duplicate
application and injected cancellation/fault after the CON roll. R18's separate candidate
and existing concentration/lifecycle/class workflow suites passed. C-04/C-15/C-16 and
R17's formal lifecycle-schema publication blocker remain OPEN.

R23 final local gate: the exact root Makefile entrypoint `python tools/validate.py
integration --base b503f4e89c68a5d6f124735d29609680e7792dea` passed in **717.88s** after
the plan was inspected. Data **671 passed / 1 skipped** (105.63s), Engine **8,276 passed**
(537.28s), Bridge **139**, Demo **54**, tooling **24**, static checks, examples and strict
MkDocs passed. Final expanded focused regression: **129 passed in 6.12s**. Earlier
attempts were interrupted for admission/immunity repairs or failed Ruff complexity;
they are not passing-gate evidence. The final docs-only edit corrects a reported anchor
and records results, followed by another strict docs build. Existing untracked
`.evaluation-worktree/` broadened selection and remains unstaged. This is local
Integration evidence; coverage/security/wheel checks await the requested final Full.

## R24 existing instant Save / Damage spell

Base: `0fb706bfaee29436c7548b93196d1d0a1f862c77`; branch:
`refactor/r24-instant-damage-spells`. Evaluator binding `/26`; no DTO/schema change.

The canonical Sacred Flame review is already Executable, with one instant SaveActivity,
one creature, Action, 60 ft, V/S, DEX save, radiant Cantrip damage and no damage on save.
Its typed `ignore_cover` and Cantrip scaling remain in the original Resolver; this
bounded admission still excludes non-plain geometry and unreviewed class levels.
The exact canonical payload and packaged capability review are checked before payment.
No canonical content or capability classification is changed.

Stateless External Contract: full Cleric 1 PC/NPC, explicit class source and complete
known/prepared facts -> Delivery legality -> Action -> shared Save/Damage -> complete
HP/Temp HP/life/turn/damage ledger delta, events and RNG. A known Cantrip need not be
prepared and costs no Slot, including with all Slots spent. Save success is accepted.
DEX proficiency and Dodge use shared modifier/condition projections. Incoming damage
to the R23 Hold Person source also resolves its CON save and complete lineage cleanup.
Unknown traits, equipment, class mechanics, target/source/component failures and any
predeclared reaction for the new spell refuse before RNG. Cure Wounds/Second Wind and
their existing Counterspell path retain their admission and payment behavior.

Legacy Runtime Dependency Removed: one `InvocationPlan`/context/executor extends the
R20 healing path; it reuses R19 payments, Delivery, Activity Save/Damage and R23 completed
instance/cleanup. The new path does not call live spell hydration, delivery, damage or
reaction hooks. Production Bridge/Demo still use Legacy; none of their live functions
are deleted or callers switched. C-04/C-15/C-16 and the R17 publication blocker stay OPEN.

Development evidence: **169 focused tests passed in 13.86s**, including 34 new real
spell cases and the existing R20/R21/R22/R23 suites. Checks cover independent DC 13,
DEX modifier +4, damage-before-save RNG order, success/failure, immunity/resistance/
vulnerability, Temp HP, PC/NPC zero HP, Concentration success/failure, source/repertoire,
components, range, budget, no Slot, stale/duplicate application, concurrency and faults.
A further full Cleric Cantrip -> opposing Pass/reset -> Cantrip workflow checks the
complete final state and RNG against Legacy with Slots exhausted.

Final R24 gate: `python tools/validate.py fast --plan --base
0fb706bfaee29436c7548b93196d1d0a1f862c77` inspected, then `python tools/validate.py
integration --base 0fb706bfaee29436c7548b93196d1d0a1f862c77` passed in **903.14s**.
Data **671 passed / 1 skipped** (148.46s), Engine **8,312 passed** (653.14s), Bridge
**139** (12.30s), Demo **54** (26.61s), tooling **24**, static checks, examples and strict
MkDocs passed. The final **36 new cases passed in 6.24s**, including explicit refusal
when the target's feature repertoire is unknown. An earlier gate was interrupted for
that closure check; only the completed run is reported as passing. The pre-existing
untracked directory remains unstaged. Results are local Integration evidence; the
requested cumulative Full still follows R25. This evidence-only edit gets strict docs.


## R25 — bounded predeclared weapon Opportunity Attack

Starts at R24 `5d97d0bf1098d325b2de130a2f222d33b532cdfc`, branch
`refactor/r25-predeclared-attack-reactions`.

| Candidate | Trigger / explicit choice | Payment / mechanical closure | Lifecycle / decision |
| --- | --- | --- | --- |
| Opportunity Attack | Voluntary step leaves selected melee reach; visible enemy; living eligible owner; explicit owner/mover/held weapon consent | Reaction once, before leaving the old cell; shared Attack/Damage, death/conditions and actual movement-prefix ledger | Consume on firing, retain on non-trigger, expire at owner next turn start; selected |
| Shield | Hit-by-attack or Magic Missile opportunity; explicit canonical spell/source/slot/target/semantics | Reaction + Slot + AC response must alter hit determination; retain AC effect for subsequent attacks | Numeric AC effect and owner-next-turn expiry must compose with every later operation; deferred to a separate effect/reaction closure |

Stateless External Contract: the complete Snapshot holds one typed Host-authorized
weapon declaration; it never treats weapon selection as consent. Owner and target,
owned available held melee weapon, complete grant/counter/equipment dependencies,
reaction budget, actual reach/LOS, entire route and ordinary movement costs are checked.
The trigger, one shared AttackActivity/Damage resolution and any zero-HP/death stop
are one candidate delta/events/RNG/readset. A non-trigger or spent Reaction retains
the declaration without cost/RNG; Pass expires it at the explicit owner's next start.
The local snapshot is `/13`, evaluator `/27`; envelope remains `/17`.

Legacy Runtime Dependency Removed: the admitted route no longer needs
`_opportunity_attackers`, `_fire_opportunity_attacks_on_step`,
`_resolve_opportunity_attack`, `pay_reaction` or Live movement hooks. It reuses pure
weapon admission/attack rules, the original Activity Resolver and existing zero-HP
fold/typed projection. Legacy shares the extracted spatial trigger. No second D&D
attack/save/damage formula or external damage command is introduced.

The new tests compare complete Legacy mechanical state/events/RNG and independent
D20/damage values, PC and NPC movers, hit/miss/crit, defenses/TempHP, lethal stops before
and later in the path, body vs weapon reach, blocked LOS, Disengage/non-trigger/spent
Reaction, dead owner/mover, missing consent/equipment/resources, explicit expiry,
replay/concurrency/stale/duplicate commits and failure/cancellation after actual damage.
A full Cleric 1 with zero Slots completes Move -> OA -> Sacred Flame -> foe Pass/reset
-> Sacred Flame, retaining all class/grant/source/resource facts. Legacy has no OA
consent DTO; comparisons explicitly assert this new Host fact's consumption/retention
and compare all existing mechanical fields independently.

Remaining scope: one Character weapon reactor, template-less movers, plain visibility
and reviewed operation profiles. Stat-block OA, Shield, effect-bearing movement,
mastery, competing/nested declarations and mid-action interactive choices remain
Legacy/unsupported. C-04/C-15/C-16 and production State Machine integration remain OPEN.
Integration and cumulative Full evidence are recorded after execution.


R25 final Integration: `python tools/validate.py integration --base
5d97d0bf1098d325b2de130a2f222d33b532cdfc` **PASSED in 881.73 s**. All selected
Data/Engine/Bridge/Demo tests, 24 tooling tests, Ruff/format/Mypy, examples and strict
MkDocs passed; Engine has **8,358 passing tests**. New R25 cases: **46 passed in
2.82 s**; related R22–R25 plus Legacy movement/reaction regression set: **209 passed
in 12.53 s** before the final added guards/boundaries. The first Integration stopped
on a Ruff format error in the shared trigger; it was corrected and the final gate
rerun. No failing/interrupted run is counted as green. The original unknown worktree
path still broadens the planner and remains unstaged. Cumulative Full follows the
four pushed batches; these are local Integration results only. This evidence/current
inventory edit receives a separate strict documentation build.
