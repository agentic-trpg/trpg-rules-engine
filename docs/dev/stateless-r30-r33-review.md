# R30–R33 independent migration review

Date: 2026-10-12. Repository: `agentic-trpg/trpg-rules-engine`.
Source baseline `d662ce367a8b41693d4978ad369608f52b7c7944`;
reviewed source `cf667f7ef337a783b9c5ec187346d0ef213d0bc1`.
These are four bounded replacements of original Stateful execution dependencies.
**Full Stateful-to-Stateless conversion is incomplete.** Production State Machine
integration and formal shared ABI approval are separate, unfinished work.

## Delivery chain and local evidence

Every branch starts at the preceding pushed source commit. The four remote refs
were verified against their local commits. No merge, rebase, force-push, upstream
Nat20 change or other-repository write occurred. Main contains none of these new
commits. The unrelated untracked `.evaluation-worktree/` remains untouched and
excluded from commits. Workspace: `D:\Projects\agentic-trpg\trpg-rules-engine`;
origin: `https://github.com/agentic-trpg/trpg-rules-engine.git`.

| Batch | Branch | Source commit | Local Integration seconds |
| --- | --- | --- | ---: |
| R30 | `refactor/r30-equipped-cleric-spell` | `5ec8cee45974481dbcdcd98efb23125b2af48dd0` | 813.48 |
| R31 | `refactor/r31-fighter-sap-mastery` | `4af6aa9a1129b7c91065515831cc8c12e9102f17` | 726.30 |
| R32 | `refactor/r32-stateless-hold-person-cast` | `eba5d51c71e7d3419e8a548f8fd0e08189adfa8b` | 818.66 |
| R33 | `refactor/r33-predeclared-shield-lifecycle` | `cf667f7ef337a783b9c5ec187346d0ef213d0bc1` | 866.36 |

Each final impact plan was inspected against its exact starting SHA. Executed
`python tools/validate.py integration --base <start-SHA>`, the root Makefile's
equivalent entrypoint on Windows without GNU Make. Windows Python: 3.14.7.
Unknown preserved worktree paths conservatively broadened each gate. All four
passed full workspace runtime suites, package Ruff/format/Mypy, 24 tooling tests,
three examples and strict MkDocs. No redundant unchanged Fast gate was run.

R33 local totals: Data **671 passed / 1 skipped** (118.69 s), Engine **8,819**
(671.04 s), Bridge **139** (9.93 s), Demo **54** (18.31 s). Final focused R33
combination: **644 passed in 46.68 s**, including 44 new Shield cases and original
reaction suites. New batch suites include 61 equipped-spell, 39 Sap and 43 initial
Hold Person cases. Test counts support the execution evidence below; they are
not a migration-completion metric. No original test, assertion or coverage floor
was deleted or weakened. Evidence-only docs receive a strict build.

Push CI successes separately verified:
[R30](https://github.com/agentic-trpg/trpg-rules-engine/actions/runs/38165114050),
[R31](https://github.com/agentic-trpg/trpg-rules-engine/actions/runs/38167496041),
[R32](https://github.com/agentic-trpg/trpg-rules-engine/actions/runs/38169558344),
[R33](https://github.com/agentic-trpg/trpg-rules-engine/actions/runs/38172284937).
Final native Linux Python 3.12 Full: **PASSED in 698.26 s** on exact source
`cf667f7ef337a783b9c5ec187346d0ef213d0bc1`,
[run 38172298121](https://github.com/agentic-trpg/trpg-rules-engine/actions/runs/38172298121).
It ran original root/package `make check` recipes on Ubuntu. This is separate from
the local Windows Integration gates; no local Windows Full run is claimed.

| Original Full recipe | Tests | Coverage | Original floor |
| --- | --- | ---: | ---: |
| Data | 631 passed / 41 skipped, 17.12 s | 97.62% | 96% |
| Engine | 8,819 passed, 587.96 s | 95.47% | 90% |
| Bridge | 139 passed, 13.73 s | 97.06% | 85% |
| Demo | 54 passed, 15.25 s | No original coverage gate | Unchanged |

Data's existing hermetic public-repository recipe conditionally skips unpopulated
maintainer raw-source inputs; all skip counts remain visible. Local Data separately
passed 671 tests with one skip. Full also passed original Bandit recipes, package
lint/format/types, actionlint, 24 tooling tests, three examples and strict MkDocs.
All three wheels built and imported in an isolated clean environment; installed
grid and Bridge HTTP smoke passed, including retained original Haste/Utilize/
Lethargy, Moonbeam and Bridge behavior. Existing deprecation warnings remain visible.
No original recipe, assertion or coverage threshold was weakened.

The subsequent evidence/review commit changes only documentation/navigation and
receives a strict docs build. Its source/tests/dependencies/workflow remain exactly
those of the Full-validated source commit above.

## Two independent progress dimensions

| Batch | Stateless External Contract | Legacy Runtime Dependency Removed |
| --- | --- | --- |
| R30 | Complete bounded equipped Cleric 1 source, owned gear/proficiencies/AC, concrete V/S component facts, casting/resources and guarded costs. | Yes: equipped Cure Wounds/Sacred Flame → Pass/reset → same-weapon Attack uses original Delivery/Activity/Turn kernels independently of Legacy spell execution. |
| R31 | Complete bounded Fighter 1/Defense/training/equipment closure; closed expected/new source-target Sap marks and lifecycle invariants. | Yes: original AttackActivity emits the actual Sap proc; shared pure transitions apply, consume on the next AttackRolled and expire at source next start. |
| R32 | Complete bounded Wizard 5 Evoker source, owned pouch/explicit iron facts, named Humanoids, slots 2/3 and closed initial graph candidate. C-15/C-16 remain OPEN. | Yes: original Delivery/Save/Effect Binding initializes full graph or empty concentration anchor, directly feeding R28 repeat/expiry and actual Attack concentration cleanup. |
| R33 | Complete bounded single authorized Wizard Shield policy, typed original attack evidence, costs and closed expected/new effect/response/expiry candidate. C-04/C-15 remain OPEN. | Yes: original Attack/Utility Activities and shared scalar AC fold reclassify one roll; later ordinary Attack reads persisted +5 and Attack/Pass expires at owner next start. |

All accepted results remain uncommitted candidates with closed typed operations,
ordered proposed events, explicit RNG transition and conservative world-version
ReadSet. SM alone authenticates sources/actors, authorizes policies, checks versions
and expected values, and atomically commits mechanical state, RNG, receipt and events.
No new generic `rules.effect` API, third Snapshot context or authoritative runtime
is introduced. Current local versions: envelope `/22`, Snapshot `/16`, evaluator
`/35`; Availability `/12` requires the current nested Snapshot. Prior versions are
rejected. These are local candidates, not an approved cross-repository ABI.

## Removed runtime dependency edges

Successful admitted paths no longer construct `_LiveCombat`, retain CombatHandle,
read Registry, dispatch Stateful intents, recapture Legacy state or invoke original
spell/reaction/Sap/expiry hooks. Per-call computations contain private actor/component
copies and event buffers; no queue, registered lifetime or cross-request state is
required. Full state/events/RNG Legacy differentials are separate from independent
numeric/component/cost expectations, isolation, fault/cancellation, stale/duplicate
guard and concurrent replay tests.

Original shared Activity Resolver, typed SRD spell/weapon metadata, Attack/Save/
Heal/Damage, delivery/count, resource pricing, component, concentration, effect
attachment, AC and turn algorithms are reused. Legacy wrappers also reuse extracted
pure helpers. Original Legacy implementations remain for unmigrated capabilities.

## Independent findings

A separate read-only reviewer inspected the four actual commits, source, tests,
complete dependency closures and current Meta contracts. The reviewer made no
repository or memory changes and did not repeat broad validation.

**No confirmed P0, P1 or P2 findings in the admitted paths.** The review recommends
accepting four bounded extractions; the separate native Full gate has now passed.
The user decides whether and when to merge; no merge was performed.

The reviewer also executed independent probes with Legacy execution forbidden
during every new evaluation:

1. **128 evaluations, 8.95 s:** Shield Attack followed by three Pass operations,
   across 16 seeds and PC/NPC owners. Complete mechanical Snapshot, ordered events
   and RNG matched Legacy, with complete graph cleanup.
2. **56 evaluations, 2.28 s:** actual two-target Hold Person cast followed by all
   27 Pass operations through natural expiry for PC/NPC Wizards. Seed 4 and WIS 1
   targets gave 20 independently checked d20/WIS−5/DC15 saves per run. Source
   concentration ticked `9 → 8 … 1 → cleanup`; effects, clocks, conditions and
   chains were removed completely.

One documentation evidence issue was corrected after review: the old SM checkpoint
was unqualified as historical. The progress checkpoint now separates its historical
`4e5cc5ac` observation from current read-only SM source `e23cde5497d53957068b967e36e527a1574b9c7f`,
branch `feat/sm-b19-b22-gameplay-domain-bridge`. ABI incompatibility is still verified:
SM `pyproject.toml:13–14` pins Engine/Data `602dcb8`; `dnd_runtime.py:306–309` builds
evaluation `/1` or `/4`; `availability.py:35–37` uses `/1` or `/2`; `package.py:288,331`
admits evaluator `/4` or `/8`. These paths are in the adjacent `trpg-state-machine`
checkout. No SM tests or runtime integration were executed.

## Retained capabilities and ABI boundaries

| Classification | Current scope |
| --- | --- |
| Bounded migrated additions | Equipped Cleric immediate healing/damage spells and later weapon Attack; Fighter Sap across attacks/turns; real Wizard Hold Person initial cast into R28 lifecycle; single predeclared Shield across ordinary attacks and owner-start expiry. Existing earlier bounded Attack/Movement/OA/Check/Save/Rest/Item/Closure/Counterspell slices remain. |
| Existing original capabilities still Legacy | Other class/equipment/Mastery/rider/action-economy combinations; general and mixed effect/concentration graphs; most spells/features/timed activities; Hellish Rebuke and complex reactions; Multiattack/recharge/legendary/complex monsters; persistent areas/environment/objects; Haste, Moonbeam relocation, Darkness/Daylight anchors, summons/transforms; production Bridge/Demo/public facade. |
| Existing Deferred / Host / Post-MVP | Potion of Speed, unclosed item-cast concentration, Feather Fall without authoritative falling producer, arbitrary custom Ready, general 3D/flying height/multi-cell footprints, exact interrupted resume and original narrative/manual/world boundaries. |

R33 retains original Magic Missile response metadata; stateless Magic Missile
casting is not delivered. Mixed Sap/Hold Person/Shield graphs, competing/nested
reactions and unreviewed profiles refuse before RNG/payment. Unsupported original
capabilities have not been deleted or silently counted as migrated.

Production edges remain concrete: BridgeState stores CombatHandle; combat routes
still call Legacy start/submit/end, and combat execution reads live serial/events.
Demo replay also calls Legacy APIs. These consumers must migrate before §14.1
exit criteria are satisfied.

Remaining cross-repository work: C-04 interactive continuation; C-15 formal complex
Effect/Reaction/Delta combination contracts; C-16 trusted source/creation authority;
shared wire/fingerprint encoding; C-03 no-draw RNG version policy; C-07 event-only
world-version policy; approved independent State Machine consumer acceptance.

The final review also reads current Meta MODULE_CONTRACTS v0.10 and State Machine
Architecture v0.12 (2026-10-12), including the newer C-19 event-stage Evidence
Capture/binding protocol OPEN. Ordered mechanical events and R33's original roll
evidence do not establish event-time perception support. A final Snapshot or Delta
must not be treated as sufficient to reconstruct every intermediate event stage;
SM must not recompute D&D mechanics from ProposedEvents. The actual trusted SM/RE
stage protocol and atomic Event/Signals/Evidence/Outbox integration remain outside
this isolated delivery and require separate cross-repository acceptance.

## Next extraction batches

These are proposals for a later request, not additional implemented capabilities.
They target remaining execution dependencies and consumer acceptance rather than
file/DTO counts.

| Proposed batch | Concrete scope and acceptance |
| --- | --- |
| R34: coordinated ABI/consumer acceptance | Review dependency/manifest/request/query pins and closed Delta consumption with SM, including actual stage-evidence requirements. Freeze only approved fields; prove atomic state/RNG/event/receipt/outbox application and conflicts. C-19 and other OPEN decisions must remain explicit if unresolved. |
| R35: Hold Person + Shield composition | Admit one complete reviewed combined graph, preserve captured source/DC/concentration and Shield AC/response/expiry, and prove saves, attacks, damage-induced concentration loss and owner-start cleanup without Legacy fallback. |
| R36: Magic Missile + Shield | Reuse canonical delivery and complete damage-instance semantics, apply retained Shield response to actual stateless casting, and prove costs, damage/concentration and existing effect duration without rerolling or hidden hooks. |
| R37: Fighter Slow mastery | Use the original actual mastery proc with a complete reviewed weapon/class profile, then consume the persisted speed effect in real Walk/Dash/turn expiry. Keep other unreviewed masteries refused. |

See [migration progress](stateless-migration-progress.md) and
[the field inventory](stateless-evaluation-inventory.md) for remaining mechanisms.
