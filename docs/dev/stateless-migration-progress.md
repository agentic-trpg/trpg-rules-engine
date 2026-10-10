# Stateful → Stateless migration progress

Audit baseline: Batch 8 `00d933d9e56a17d489b81e63ecb2bd34e6a4fde4`.
This stabilization is a step toward replacing the existing Stateful Rule Engine,
not a second permanent engine. Full conversion is **not complete**.

## Decisions and open contracts

The adjacent Meta repository's ADR-001, MODULE_CONTRACTS §4–6/13,
STATE_MACHINE_ARCHITECTURE §5/7 and MVP_SCOPE govern ownership. DECIDED:
SM is the sole authoritative state/commit owner; exactly CombatSnapshot and
NonCombatSnapshot contexts share typed mechanical components; complete dependencies,
closed deltas, explicit RNG, binding checks and accepted ≠ committed are mandatory.
Per-call disposable `_LiveCombat` adapters are expressly permitted.

OPEN: final wire encodings/fingerprint canonicalization, RNG no-draw version policy
(C-03), event-only world version (C-07), complex Reaction continuation (C-04), complex
Delta DTOs (C-15), combat.start/create and trusted source authority fields (C-16).
This repository's versioned implementation does not settle those cross-repository
decisions. No DECIDED decision is reversed here. First Blush remains the private
local MVP adventure; public synthetic regressions do not replace its acceptance.

## Execution and mechanism inventory

Paths in this table are relative to `packages/dnd5e-engine/src/dnd5e_engine/`.
“Fully Migrated” applies only to the named bounded leaf, including its state/RNG/
events/failure contract. It never means the whole surrounding D&D mechanism.
The categories describe Engine evaluation, separately from SM consumer readiness.

| Mechanism | Classification | Current paths and remaining state dependencies |
| --- | --- | --- |
| Four-result envelope, binding, explicit RNG | Stateless Fully Migrated | `evaluation.py`, `evaluation_contracts.py`, `evaluation_ruleset.py`, `evaluation_rng.py`; local versioned candidate, whole-world OCC fence; durable receipts/commit belong to SM |
| Combat initialization / Start | Legacy Stateful Only | `orchestrator.start_combat/_start_combat`, `build_party.py`; initiative, seeding, roster, positions, slots, passives, hooks and first turn are initialized in registry runtime; no `combat.start` payload or `combat.create` Delta |
| Turn advancement / action economy | Stateless Partially Migrated | `evaluation_preflight.py`, `action_policy.py`, shared `_consume_intent_budget`, `_end_turn_and_advance`; attack/item can return full budget/turn/reset deltas; standalone pass/end-turn, Ready, death saves, extra/restricted actions and general lifecycle boundaries are not admitted |
| Movement / positioning | Legacy Stateful Only | `movement.py`, `spatial.py`, `live_movement.py`, orchestrator movement/forced movement; Snapshot contains positions and ledgers, but no position Delta or movement evaluation; terrain, LoS, cover, opportunity windows remain Legacy |
| Bounded ordinary PC/NPC weapon attack | Stateless Fully Migrated | `evaluation_preflight.py` → disposable `evaluation_context.py` → `_submit_live_intent` → `activities/attack.py`; complete HP/temp/budget/attribution/damage/death/turn deltas, events and RNG, including misses and crits |
| Attack / damage / death overall | Stateless Partially Migrated | Common reviewed properties/static R/I/V and exact ordinary stat-block actions work; terminal recorded dead creatures no longer block survivors. Dying PCs/death saves, rider resources, trained Masteries, Multiattack/recharge/legendary and effect-driven defenses remain Legacy |
| Adjudicated ordinary noncombat ability/skill checks | Stateless Fully Migrated | `evaluation_checks.py` → `activities/check_pipeline.py`; explicit DC/GM flags/proficiency/expertise/Jack/Reliable Talent, typed CheckRolled and RNG; legal failed checks are accepted with empty mechanical Delta |
| Saves / skill checks overall | Stateless Partially Migrated | `check.py`, `activities/save_primitive.py`, `live_save_modifiers.py`; standalone Save envelope, tools, sensory/action contexts, modifier consumption and effect-dependent checks not migrated; existing pure formulas are available, not missing rules |
| Owned single-use self-healing consumable | Stateless Fully Migrated | `evaluation_items.py` → shared item gates/payment and HealActivity resolver; required InventoryState, full-entry inventory.consume guard, capped HP, Bonus Action/turn, RNG/events and rollback |
| Item / inventory / resource payment overall | Stateless Partially Migrated | `evaluation_state.InventoryState`, `evaluation_delta.InventoryConsume`; no transfer/equip/recharge/ammunition/mixed charges/general resource Delta. Legacy slot/counter machinery and inventory-dependent Light/Loading/Thrown remain; carried slugs do not establish stack quantity |
| Conditions / active effects / concentration | Legacy Stateful Only | `evaluation_effects.py` captures explicit records, but execution refuses them; `live_effect_lifecycle.py`, `effect_lifecycle.py`, `rules/conditions.py`, effect folds/concentration chain and captured child templates still need complete Delta/hydration |
| Spellcasting / Features | Legacy Stateful Only | `spell_execution.py`, `feature_runtime.py`, `live_features.py`, `live_spell_delivery.py`, `activities/cast.py`, `activities/resolver.py`; repertoire, costs, cast timing, grants, riders and resources exist in Legacy; casting/feature evaluation is unsupported |
| Reaction / pending choice | Stateless Partially Migrated | Preflight weapon and missing-DC choices are fully typed and draw-free; `reactions.py`, `live_reactions.py`, `timed_activities.py` queues/windows/continuations and interrupts are Legacy. Mid-resolution continuation ABI remains OPEN |
| Persistent areas / environment / objects | Legacy Stateful Only | `persistent_areas.py`, `environment.py`, `combat_objects.py`, `ongoing_spell_activation.py`; source geometry/lifetime, trigger history, object anchors, environmental projections are captured, then rejected for execution; typed lifecycle/position/object Deltas missing |
| Bounded terminal victory / TPK closure | Stateless Fully Migrated | `evaluation_closure.py` → shared `_derive_ended_reason/_project_outcome`; new ended fence + XP increments, separate historical report, CombatEnded, unchanged RNG; no registry/handle/end_combat call |
| Closure / outcome overall | Stateless Partially Migrated | Legacy flight/forced end and final-effect snapshot are not migrated; world phase conversion belongs to SM. Loot generation and durable cross-combat effect carryover were already Host/deferred gaps, not lost executable rules |
| Rest / recovery and character derivation | Legacy Stateful Only | `rest.py`, `build_spec.py`, `build_party.py` already provide pure computations; still lack evaluation envelopes, complete relevant state components and closed resource/HD/recovery deltas. “Legacy only” here means outside the new protocol, not inherently stateful mathematics |
| General 3D simulation, arbitrary narrative mechanics, precise interrupted-combat resume | Unsupported / Post-MVP | Preserve original Host/Deferred boundaries and Meta's MVP scope; do not confuse new-envelope gaps with original rule support |

The canonical spell capability audit (`spell_capability_audit.py`), reviewed data and
existing per-mechanic tests remain the baseline for original executable/bounded/
host_narrative/deferred support. A parsed Activity or a DTO in Snapshot is not an
executed capability. Existing Haste/restricted actions, Moonbeam relocation,
Darkness/Daylight object anchoring, summons/transforms, rest, saving throws and
feature/rider rules have **not** been deleted; they have not yet crossed the new
admission/Delta boundary. Potion of Speed and other originally Deferred mechanics
must remain Deferred until their independent contracts are complete.

Original gaps must not become fictitious migration promises: `_project_outcome` already
returns an empty loot list; typed LootDrop does not implement loot generation. Legacy
`end_combat` returns `final_active_effects`, but durable cross-combat effect persistence
was already Host/deferred. Ordinary ranged attacks/Loading budgets exist in Legacy;
per-arrow stack accounting does not follow from the Ammunition property and is not
demonstrated by those attacks. Any new loot/ammunition/transfer mechanics require a
separate support decision and independent rule tests, rather than being counted as
restoring a supposedly lost implementation.

## State that has and has not migrated

The full field index remains in [the inventory](stateless-evaluation-inventory.md).
Current admitted operations preserve every unchanged component and exhaustively
reject any unrepresented change. There is no arbitrary patch or Snapshot overwrite.

| State family | Input ownership / current evaluation | Work remaining |
| --- | --- | --- |
| HP/temp/death, attribution, damage IDs, processed zero-HP instances | Required actor/combat records; typed deltas from shared folds; dead_ids/DeathRecord/is_alive agree | Dying/stable/death-save admission, revive and effect-linked damage dependencies |
| Equipment / inventory | Required explicit CharacterStateV2 and InventoryState in both contexts; attack/closure preserve inventory, item decrements one stack | One coherent authority for carried/equipped identities, transfers, mixed charge units and item resource pools; never infer units from slugs |
| Budget/initiative/round/turn/movement ledger | Required; full supported action/turn changes projected; dead roster retained and skipped by shared turn core | Init/explicit turn/move operations, position Delta, lifecycle-triggered boundaries |
| Spell/Pact slots, known spells, custom counters, expenditure history | Required; untouched by current bounded attack/check/closure; item uses one private derived charge then removes it | Typed resource payment/restore operations; general expenditure accounting. Shared slot ledger now records payer + actual pool/level at payment, not effect target/name |
| Effect/condition/concentration, reactions, timed activities, areas, objects, summons/transforms | Complete fields retained, but presence generally returns unsupported before execution RNG | Strict nested DTO review and full rehydration for each admitted capability, expected-value operations and referential/lineage invariants |
| Event history / world version / RNG | Explicit immutable input evidence and RNG context; ordered proposals and transitions returned | SM event-only/no-draw policy decisions, durable history/outbox/receipt integration |
| Loader/topology/indexes/hooks/queues/listeners/transactions | Loader pinned; derived indexes rebuilt; queues/hooks private, no external listeners or registry | Extract more supplied-context seams; never serialize callbacks/locks/queue or persist an Engine runtime |

`evaluation_context.py` currently reconstructs only the admitted subset. Its empty
complex stores are safe solely because admission rejects the actual nonempty input
dependencies. Expanding that admission without first restoring and projecting those
components is forbidden. Old equipment `None` sentinels cannot be upgraded to empty
training/no occupied hand. Old inventory data without quantity/access/charges cannot
be upgraded by supplying invented defaults.

## Remaining production Legacy Registry / CombatHandle chains

1. `packages/nat20-bridge/.../routes_combat.py` create → `start_combat` →
   `_start_combat` → `_REGISTRY[handle_id]`; `BridgeState.combats` stores handles.
   Intent/advance/end routes call `submit_player_intent`, `advance_monster_turn`,
   `end_combat`; these resolve `_get_live` and use the Legacy transaction wrapper.
   The background event collector drains the retained queue into Bridge history/WebSocket.
2. `nat20_bridge/combat_execution.py` calls `get_live` for execution serial, event
   count and final outcome around the route operations; process-local locks/receipts
   provide adapter retry isolation, not SM's durable atomic commit.
3. `apps/demo/src/nat20_demo/replay.py` opens/replays/closes Legacy combats and uses
   `get_live`/`drain_pending_events`. Its “stateless replay” describes UI log replay;
   it is not Snapshot → RuleEvaluationResult execution and still registers handles.
4. Public Engine `get_live`, active-effect queries, queue drains, narration and
   legendary-resistance entrypoints resolve handles. `combat_objects.register_combat_objects`,
   `mutate_combat_object`, and `environment.apply_strong_wind` also use `_get_live`.
   Ended-runtime retention/cleanup and test registry reset remain in orchestrator.
5. New evaluate and availability use only supplied-context helpers. Temporary
   CombatHandle values still occur inside the attack/item compatibility call chain,
   but resolve no registry and cannot escape as authoritative state. Closure/check
   do not need them. Removing these temporary values is a cleanup, not the ownership
   blocker; removing production retained handles is the blocker.

No production Bridge/Demo route has been converted in this task. One real Session
must never mix those Legacy state owners with SM authoritative evaluation commits.

## Cross-repository ABI checkpoint

Read-only SM observation: `3e8fdb6ecba97c6206f7c7ba3cfc2516f6e5d1f1`, branch
`feat/sm-b10-b13-combat-close-checks`, with in-progress user changes. Its pyproject/
lock pin Engine/Data `602dcb8d448e670049427bbb751d6ed226005298`; its request builder
uses evaluation `/1` or `/4`, snapshots `/1`/`/2`, and manifest binding accepts
evaluator `/4` or `/8`. This is newer than the original attack-only SM baseline,
but it is **not compatible** with evaluation `/6`, snapshot `/4`, availability `/3`
and evaluator `/10` delivered here. SM source/tests were neither edited nor run.

An independent coordinated SM batch must upgrade dependency/manifest/request/query
versions; materialize genuine inventory/equipment facts; update closure consumption
to `xp_increments` + read-only `historical`; admit inventory.consume and positive HP
for the correct operation; preserve full authorization/expected-value/affected-actor
checks; commit Delta, RNG, events, receipt, phase/XP and outbox together. Existing
saved snapshots require a reviewed fact-preserving migration or explicit rejection.
Event-only check commits still depend on SM policy; Engine success does not establish
cross-repository integration success. Old requests/results fail visibly, not coercively.

## Next four refactor batches, in dependency order

These are the next four bounded batches, **not a promise that four batches finish all
Legacy capability families**. Each has a finite acceptance boundary; afterwards the
remaining roster below determines work, rather than repeating these foundation tasks.

| Batch | Workload and concrete scope | Exit evidence |
| --- | --- | --- |
| R9: supplied-context initialization and explicit turn boundary | Large: extract start hydration from registration; design local versioned combat.start/create candidate and typed pass/end-turn; complete initiative/first turn, ordinary reset, dead skipping and death-save boundary. Source-authority fields need cross-repo review before production enablement | Noncombat → combat → two rounds differential state/events/RNG; dead and dying distinct; init/turn faults leave no registry or partial result; no Monster AI acting for Sub-agent |
| R10: movement and spatial state | Medium/large: ordinary movement/Dash/Disengage, typed position + ledger updates, existing grid legality/LoS/cover. Detect reaction-dependent movement and refuse it until R12; no new geometry rules | Traversal/terrain/range budget parity, rejected collision/out-of-range no cost, forced move explicitly deferred when dependencies unavailable; complete atomic expected-value deltas |
| R11: common resource/equipment authority | Large: typed slot/Pact/counter/HD payment/restore, inventory identity/equip/transfer schema, rest/recovery envelope; remove dual slug/unit accounting for admitted items; coordinate SM ABI/consumer separately | Actual payer/pool tests for costs/rests, concurrent last-unit consumer conflicts, full state+RNG+events rollback, no second payment at closure; original unsupported content stays unsupported |
| R12: effect lifecycle and bounded interrupts | Very large; stage explicit reviewed substeps: hydrate strict Effect/Condition/Concentration graph, captured end follow-ups, turn expiry/damage, predeclared bounded Reaction windows. Resolve OPEN continuation before admitting mid-resolution pause | At least one existing effect chain and one predeclared reaction with complete lifecycle/resource/condition deltas, counter/interruption fault and seeded parity; unknown child/lineage/continuation refused before payment |

After these foundations, migrate existing spell/feature activity families once:
ordinary attack/save/heal/damage/effect first, then ongoing timing/area/object sources,
then summons/transforms/legendary/Multiattack and remaining bounded Legacy content.
Use the canonical capability audit and original regression suites as an explicit
checklist. Extract each Legacy supplied-context executor and its Delta projector;
do not create a separate formula implementation. Finally switch Bridge/Demo consumers
or retire their Legacy mode in a separately reviewed compatibility release. Validate
flight/forced closure, loot/effect carryover and combat→noncombat restoration before
removing end_combat's world handoff. These later families are real remaining work,
not silently included in R12's workload.

## Acceptance conditions for removing Legacy Stateful Runtime

1. Every originally executable/bounded production mechanism has either a tested
   stateless path with all dependencies/deltas or an explicit reviewed retirement;
   original Deferred/Host classification is preserved. No support-count promotion
   substitutes for independent rules/Legacy/event/RNG parity oracles.
2. Init, turns, movement, death saves, resource/equipment, conditions/effects,
   spells/features, reactions, areas/objects and closure have complete strict input
   DTOs and closed operations; injected failures never leak partial results or RNG.
3. Production Bridge/Demo/SM execution uses one authority per Session, with no
   `_REGISTRY`, retained CombatHandle or default global RNG execution dependency.
   Temporary contexts are released after each evaluation; public Legacy API removal
   is explicitly versioned and all consumers migrated or retired.
4. SM atomically validates and commits all expected writes plus RNG/events/receipt/
   outbox; replay, stale version, duplicate command, last-unit races and response-loss
   tests prove exactly one commit. Engine does no database writes/publication.
5. Ordinary completed combat closes once, rewards once and resumes noncombat with
   correct balances/effects. Noncombat save/restart succeeds; interrupted active combat
   is explicitly blocked or handled under approved policy, not silently rolled back.
6. Full Engine/Bridge/Demo/static/security/docs/wheel checks and coordinated SM
   integration pass against matching pins; the private First Blush acceptance is
   separately executed. Passing Engine Full alone is not full conversion or MVP proof.
