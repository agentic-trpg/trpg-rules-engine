# R26–R29 independent migration review

Date: 2026-10-12. Source baseline `0f27035003395695cb4d1b896a1c4004f4e189c8`;
reviewed source HEAD `117f4ec1294bf1ca3b0ae36b293b22f465446c46`.
Scope is four bounded extractions/corrections, not complete Stateful-to-Stateless
conversion, production State Machine integration, or formal ABI approval.

## Delivery chain and local validation

Every branch starts from the previous pushed source SHA. All four remote refs
were checked against the local commits; main remains at the baseline locally.
No merge/rebase/force-push or other-repository write occurred. The unrelated
untracked `.evaluation-worktree/` is preserved and excluded from commits.

| Batch | Branch | Source commit | Local Integration seconds |
| --- | --- | --- | ---: |
| R26 | `fix/r26-lifecycle-wire-schema` | `19fe95c3fab9682b3f3d7c91e047ec9a2d325ac7` | 1084.12 |
| R27 | `refactor/r27-equipped-class-attack` | `eb8e5e37cfd2593f146ac101df57a572dc3f9bda` | 973.83 |
| R28 | `refactor/r28-hold-person-turn-lifecycle` | `21ae8df58ff186862723bdf10586d3850a318aab` | 791.71 |
| R29 | `refactor/r29-statblock-opportunity-attack` | `117f4ec1294bf1ca3b0ae36b293b22f465446c46` | 967.58 |

Each final plan was inspected against its recorded starting SHA. Executed command:
`python tools/validate.py integration --base <starting-SHA>`, the root Makefile's
equivalent entrypoint on Windows without GNU Make. No redundant Fast gate was
executed. Unknown preserved worktree paths conservatively broadened the plans.
All four gates passed package lint/format/types, full workspace runtime suites,
24 tooling tests, examples and strict MkDocs. Windows interpreter: Python 3.14.7.
R29 totals: Data 671 passed / 1 skipped (172.88 s), Engine 8,632 passed
(705.58 s), Bridge 139 (12.37 s), Demo 54 (25.12 s). Original tests and coverage
thresholds were not removed or weakened. Evidence-only docs receive strict builds.

Push CI successes separately verified:
[R26](https://github.com/agentic-trpg/trpg-rules-engine/actions/runs/38147382047),
[R27](https://github.com/agentic-trpg/trpg-rules-engine/actions/runs/38150632958),
[R28](https://github.com/agentic-trpg/trpg-rules-engine/actions/runs/38152758219),
[R29](https://github.com/agentic-trpg/trpg-rules-engine/actions/runs/38154954353).
Final native Linux Full: **PASSED in 664.58 s** on the exact reviewed source
`117f4ec1294bf1ca3b0ae36b293b22f465446c46`,
[run 38154957469](https://github.com/agentic-trpg/trpg-rules-engine/actions/runs/38154957469).
The Linux Python 3.12 job ran the original root `make check` and each unchanged
package `make check` recipe. This is separate from the Windows Integration gates;
no local Windows Full run is claimed.

| Original Full recipe | Tests | Coverage | Original floor |
| --- | --- | ---: | ---: |
| Data | 631 passed / 41 skipped, 16.91 s | 97.62% | 96% |
| Engine | 8,632 passed, 557.99 s | 95.49% | 90% |
| Bridge | 139 passed, 13.24 s | 97.06% | 85% |
| Demo | 54 passed, 15.15 s | No original coverage gate | Unchanged |

The Data public-repository recipe is hermetic and existing tests conditionally skip
unpopulated maintainer raw-source inputs. All reported skip counts are retained;
the local Data gate separately passed 671 tests with one skip. Full also passed
the three existing Bandit recipes, package Ruff/format/Mypy, actionlint, 24 tooling
tests, all three examples and strict MkDocs. Three built wheels imported from an
isolated clean environment and passed installed-corpus/grid/Haste/Utilize/Lethargy,
Moonbeam relocation and Bridge HTTP intent/retry/authority/object/cover smoke checks.
Existing dependency deprecation warnings remain visible; no recipe or threshold
was weakened. Full does not prove production State Machine integration.

## Two independent progress dimensions

| Batch | Stateless External Contract | Legacy Runtime Dependency Removed |
| --- | --- | --- |
| R26 | Strict explicit lifecycle clock/application/spec/repeat wire fields match both generated schema modes and runtime Python/JSON validation. No defaults repair missing runtime facts. | No additional runtime migration claimed for a schema correction. Existing isolated paths and original immutable lifecycle rules are preserved. |
| R27 | Full Cleric 1 selected Protector grants, class proficiencies, canonical mace/quarterstaff, chain shirt and optional shield, unique accessible owned inventory and legal hand/grip facts are retained. Shared Armor AC validates attacker/target dependencies. | Equipped PC and equivalent NPC Attack/Damage plus Pass/reset/next Attack execute without Orchestrator, registry, handles or live hooks, using shared Attack/Armor/weapon/versatile/Turn rules. |
| R28 | Complete existing authorized Hold Person graph, captured Save/DC, independent target clocks/lineage, concentration duration/chain and guarded subset/tick/removal transitions. No new source operation. | Caster/target/free turns, repeat Save failures/successes, partial multi-target release, natural expiry, source death/concentration loss and legal later actions use the existing snapshot scheduler/shared Save/duration/cleanup kernels without live effect hooks. |
| R29 | Explicit owner/mover/monster/action OA declaration; complete pinned Ogre facts, typed Greatclub attack/reach, Reaction payment, actual movement prefix, damage/death/consumption/expiry/events/RNG in one guarded result. | Authorized real Ogre OA and later walking/Pass/reset/ordinary Ogre movement/Attack execute without Monster Dispatcher, Live Reaction Hooks, registry, handles or recapture. Equipped Cleric PC/equivalent NPC movers compose without losing class/equipment/resources. |

The new R28 actual Paralyzed Attack test confirmed and closed a missing condition
gate before RNG/payment. R29 full event parity required preserving the original
delivery source attribution; Legacy and stateless paths now share that identity rule.

## Independent findings and merge decision

A separate read-only reviewer inspected the four source commits, actual successful
execution paths and tests, original capabilities and canonical spell audit, relevant
SRD data, and the latest Meta `MODULE_CONTRACTS.md v0.9`,
`STATE_MACHINE_ARCHITECTURE.md` and `MVP_SCOPE.md`. The reviewer wrote no repository
files and did not repeat Full or Integration.

**No confirmed P0, P1 or P2 findings. Recommend merge as bounded stateless migration.**
This is a review recommendation; no merge or production-session cutover was performed.
R17's previously confirmed local lifecycle Schema/Runtime defect is resolved by R26,
including validation/serialization Required, recursive omission, strict types and
Python/JSON round trips. Existing runtime completeness validators remain intact.

The following successful paths have actual isolation and complete mechanical
state/event/RNG evidence:

- R27: equipped Cleric PC and equivalent NPC Attack, Damage, Pass/reset and later
  Attack. Independent expectations include STR 16 + PB 2 = +5 to hit, mace 1d6+3,
  legal quarterstaff versatile dice, chain-shirt DEX cap and shield AC.
- R28: source/target/free turns, repeated WIS Saves at each target's turn end,
  successful individual release, full duration expiry, source death/concentration
  loss and later legal actions. Partial release preserves other targets' chain;
  the source duration ticks only at its own turn end. The shared Action Gate now
  rejects Paralyzed Attack before RNG/payment, as it already did for casting.
- R29: visible opposing mover leaves canonical Ogre's 5-ft reach, pays only
  Reaction, resolves +6 / 2d8+4 (critical 4d8+4), and returns the actual completed
  movement prefix after zero HP/death. Hit/miss/critical, defenses, Temp HP,
  non-triggered retention, consumed/expired declarations and later movement /
  ordinary Ogre Attack compose without a live runtime.

Independent extra probes, executed once without repository changes and with Legacy
runtime forbidden, also passed in **1.43 s** (process wall time **4.00 s**):

1. Seed 971, two targets, 18 actual failed repeat Saves and 28 Pass evaluations:
   complete source-duration sequence 10 through 1 to expiry, followed by complete
   Effect/Condition/Clock/Chain cleanup.
2. Equipped Cleric PC and equivalent NPC, seven steps each: Ogre OA movement,
   reapproach, Cleric Attack, ordinary Ogre Attack, both actors Pass/reset, and
   another Cleric Attack. Every result accepted, deterministic reevaluation matched,
   and complete equipment/resource/casting facts remained preserved.

R28 concentration transitions permit only an ordered chain subset, one source
duration tick or complete clear, with expected-value guards. The shared projection
refuses unrepresented actor/combat-field changes. ReadSet uses the conservative
whole session/world version fence. No authoritative mutation, hidden pending
handle or cross-request state is introduced.

The bounded exclusions are explicit safe refusals, not unreported migration:
unreviewed class/equipment combinations, trained Mastery, incomplete/mixed effect
graphs, noncanonical monster actions, Recharge/Multiattack/legendary/Traits and
competing/interactive reactions remain refused before payment/RNG.

## Capability inventory and remaining contracts

| Classification | Actual original capability coverage |
| --- | --- |
| Bounded migrated | Ordinary PC/NPC single Attack and R27 equipped Cleric; Walk/Dash/Disengage; authorized weapon OA and canonical Ogre OA; explicit Pass/reset and bounded dying/death saves; noncombat Check/Save; self-healing consumable; bounded resource payment/rest; Cure Wounds, Second Wind, Sacred Flame; one predeclared Counterspell; existing Poison/Hold Person lifecycle; bounded closure and local initialization candidate. |
| Existing capabilities still Legacy | Other class/equipment/Mastery/riders and action economy; general effect/concentration creation and mixed graphs; most spells/features/timed activities; Shield/Hellish Rebuke; complex monster actions; persistent areas/environment/objects; Haste, Moonbeam relocation, Darkness/Daylight anchors, summons/transforms; production Bridge/Demo and public facade. |
| Existing Deferred / Host / Post-MVP | Potion of Speed, unclosed item-cast concentration, Feather Fall without authoritative falling producer, arbitrary custom Ready, general 3D/flying height/multi-cell footprints, exact interrupted-combat resume, and original narrative/manual/world boundaries. |

Original Legacy mechanics are retained; their lack of admission by this evaluator
does not erase their existing implementation. The canonical spell audit is static
admission evidence, not proof that every parsed Activity executes statelessly.

Production dependency edges remain explicit: BridgeState still stores handles,
combat routes use Legacy start/intent/advance/end, and combat execution reads live
serial/events. Demo replay and the public facade also retain Legacy callers. These
are not removed by proving an isolated evaluator path.

Latest Meta ownership remains unchanged: SM alone verifies trusted identity/source,
authorizes operations and owns state, versions, idempotency, RNG, events, receipts
and atomic commits. Agent/Host can propose or transmit declarations.

Remaining OPEN contracts and integration:

- C-04: complex interactive Reaction continuation and intermediate choices.
- C-15: formal shared complex Effect/Reaction/Delta fields and combination invariants.
- C-16: trusted sources, creation and later orchestration.
- Shared wire/fingerprint/data-revision encoding, no-draw RNG version policy (C-03),
  event-only world-version policy (C-07), and independent SM consumer acceptance.

Current local versions are envelope `/19`, Snapshot `/14`, Availability `/12`,
evaluator `/31`. They are correct local versioned candidates, not an approved
cross-repository ABI. Exactly CombatSnapshot and NonCombatSnapshot remain.

## Next four extraction batches

These recommendations target existing execution dependencies, not file/DTO counts.
They are proposals for a later request, not work implemented in this delivery.

| Proposed batch | Concrete successful slice and acceptance | Legacy dependency to remove |
| --- | --- | --- |
| R30: equipped Cleric spell composition | Connect complete R27 equipment to existing Cure Wounds/Sacred Flame. Validate one-hand quarterstaff/free-hand and shield conflicts, armor/training/source/resources; spell → Pass/reset → Attack. Current `evaluation_spells.source_failure` rejects any held weapon. | Equipped actors still returning to Legacy for existing simple casting. |
| R31: equipped Fighter Sap | Complete Fighter 1 grants/training/equipment; real Sap proc, next Attack consumption and expiry; closed marks delta. Keep other unreviewed Masteries refused. | `activities/mastery.py` proc still folded/consumed/expired through Orchestrator sidecars. |
| R32: direct Hold Person production | Complete Wizard source; initial Save, successful/failed targets, effect/condition/clock/chain creation, all-target-success anchor, Slot/Action and later R28 turns. Review the required C-15/C-16 local creation contract first; add no unapproved `rules.effect` ABI. | Legacy-only production of the initial trusted Hold Person graph before stateless continuation. |
| R33: one predeclared Shield lifetime | Ordinary Attack/Ogre OA provisional hit → Reaction/Slot → reevaluate the same attack roll → later attacks see +5 AC → owner-next-turn expiry. Atomic Effect/Clock/Budget/Events/RNG; interactive C-04 remains OPEN. | Live attack reaction hook, Shield application and expiry chain. |

Areas/objects, complex monsters and production Bridge/SM caller cutover still need
later exit plans. Continue to report External Contract and Runtime Dependency
Removed separately and prove actual composed execution for each batch.

The complete field and mechanism inventory remains in
[stateless-evaluation-inventory](stateless-evaluation-inventory.md) and
[stateless-migration-progress](stateless-migration-progress.md).
