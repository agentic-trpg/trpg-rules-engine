# R34–R37: local event-stage mechanical facts

## Scope, baseline and progress

RE only: `D:/Projects/agentic-trpg/trpg-rules-engine`, origin
`https://github.com/agentic-trpg/trpg-rules-engine.git`, branch
`feat/re-r34-r37-event-stage-facts`. Actual R34 starting HEAD is
`63a59f345aea4bac353bb715fdfa007d1ff306d4`, the checked history-free main.
The unrelated untracked `.evaluation-worktree/` is preserved and remains part
of conservative validation planning. No reset, rebase, force push or merge.

Read-only references were inspected at architecture `8141402592946942ff5f6a69f04858ef382bd734`
and SM `80bc1af59993d29f3281e99b06de60915dc450a9`. The change from the draft's
`7da1137` reference was inspected: GC3 adds item transfers and routes object
evidence through WorldFacts; CombatHandler and RF4's engine-stage gap are unchanged.
SM has completed RF4 and its
position-source repair, still pinning RE/SRD `675552406e67470bf3456fe90e88c4f1595bf324`.
Its Engine events deliberately use `engine.intermediate_phase_unavailable`.
Neither reference repository nor its dependency pin is changed here.

| Batch | Start | Status |
|---|---|---|
| R34 | `63a59f345aea4bac353bb715fdfa007d1ff306d4` | pushed `aa8997d5a9c7562d4f32632171681f818d18787a`; gate below |
| R35 | `aa8997d5a9c7562d4f32632171681f818d18787a` | pushed `9846e359d112c8dc4003cdbdc673041b5a7ebf7c`; gate below |
| R36 | `9846e359d112c8dc4003cdbdc673041b5a7ebf7c` | ordinary Pass/Walk sampling; gate below |
| R37 | preceding pushed R36 commit | pending: versioned evaluate Result and handoff |

R34 introduces candidate types only. Public Evaluation remains `/23`, Snapshot
`/17`, Availability `/13`, evaluator `/36` until the versioned R37 integration.
C-19/C-24 and the shared ABI remain OPEN. Stage facts are not StateDelta,
committed WorldEvents, complete world Evidence, Signals, noticing, OB or Episode.

## Mutation and emission audit

The actual call chain is preflight -> execute_attack -> shared Activity Resolver
-> computation emit/damage callback -> finish_turn -> exhaustive Delta projector.
Pass selects base/EffectTurn/HoldTurn/Shield computations. Walk uses a temporary
base computation, shared topology/cost kernels, step reactions and a run summary.

| Operation / actual point | Event relationship and sample semantics | Available bounded mechanics | Dependencies not implied |
|---|---|---|---|
| Attack emit IntentSubmitted, before spend_attack_budget | audit declaration; after its fold still unpaid; never proof of visible attack start | input actor/target reference | no Signal/observable-animation claim |
| Attack spend budget; before resolve_activity | explicit execution boundary after payment, before resolution | actor budget/cell, unchanged target reference | no second action execution or roll |
| emit AttackRolled | append then increment attack_rolls_made_this_turn | post-fold attacker budget; event preserves hit/miss/crit roll | rolled audit is not a perception result |
| emit DamageApplied | append; fold HP/temp/attribution; capture packet before nested Death | post-packet target balances and damage sequence | not yet completed zero-HP instance |
| record_death | fold life/dead membership/death record, then direct events.append | post-death life/dead membership; actual Death ordinal | cannot rely only on emit interception |
| damage_completed | shared zero_hp_damage after final packet; may change death saves/processed IDs before emitting conditions/death | instance-completed target/ledger; separate checkpoint | some changes have no new event |
| death_save | emits roll/events before assigning result.combatant; critical recovery emits condition changes afterward | event-time and completed-save checkpoints differ | complex effects/reactions remain separate profiles |
| finish_turn TurnPhase(end), TurnEnded | emitted before next_turn_index and last_ended_turn update | end-event facts still old turn | no blanket after-function sampling |
| next_turn_index / round update | RoundStarted and round-start phase after index/round change | changed turn projection | no invented elapsed time |
| TurnStarted | emitted before turn_serial/ledger/reset_turn_budget | explicit pre-reset sample | does not carry refreshed budget |
| reset_turn_budget and global sneak flag cleanup | direct actor/state mutations before TurnPhase(start) | post-reset actor/ledger and turn sample | account for other affected supplied actors |
| record_budget_changes / Sap postprocessing | may occur after last emitted event | explicit operation-end checkpoint | final event is not final candidate |
| Walk each accepted step | update actor_zone, ledger and remaining before run flush | actual successive cell/cost/budget checkpoint | no fictional per-cell public Event |
| Walk ActorMoved flush | one event summarizes an actual run; can reference several prior step checkpoints | completed run and endpoint | OA flush/interrupt/continuation is not ordinary coverage |
| Dash/Disengage | direct payment/ledger or disengaging mutation surrounds their audit emit | audited separately before any inclusion | not automatically ordinary Walk |
| Concentration/EffectTurn/Hold/Shield overrides | before/after super.emit, direct appends, nested effects, expiry and payment | preserve existing rule result | base capture cannot declare these complete |

Event ordinals are retained at the actual append, before nested emission. An
immunity-filtered ConditionApplied has no returned Event and must have no dangling
reference. No event suffix from old history or position reconstruction is used.

## Candidate projection and association

`EventStageFacts` references the enclosing `request.state_snapshot`; the enclosing
Result supplies Session/command/world version/binding. It creates no parallel ID
protocol. `MechanicalStage.sequence` is a request-local ordinal and `event_cursor`
is the number emitted at sampling, never durable event_seq. Each unique
`EventStageReference.event_index` names exactly one actual proposed_event and may
associate multiple real checkpoints (notably one ActorMoved run). References are
typed, ordered, closed and range checked; no authoritative event IDs are accepted.

`StageActorFacts` projects HP/temp, life/death saves, Conditions, attribution,
position, the existing AttackBudgetState and MovementLedgerState. `StageCombatFacts`
projects the existing TurnState, dead membership, damage sequence and processed
instances. Static attributes, senses, traits, equipment and full Scene geometry
are input references, not per-event copies. Death reports and every other field
remain outside this bounded projection and under the final Delta omission guard.

Stages contain sparse changed participant projections only. Unchanged values
mean the explicit input/previous-stage value within this declared projection;
they do not claim unchanged Snapshot-external actors, objects or environment.
`coverage=complete` is only complete for these declared participant fields, with
`world_coverage=outside_snapshot_unknown`. An uncovered profile contains a typed
reason and no misleading partial execution records. Rule status is independent.

All nested carriers use frozen typed models, tuples and frozensets; no retained
mutable list/dict is exposed. Detachment tests mutate later Conditions, positions,
counters and dead membership, and attempt nested writes. R37 must additionally
verify result identity, final projected Delta consistency and actual event linkage.
Final Typed Delta remains the only mechanical commit proposal.

## Baseline, review and validation

The inherited draft's prior timing/reviewer assertions are not current execution
evidence. This run rechecked mutation ordering, including nested death ordinals,
pre-reset TurnStarted, damage completion without events and Walk flush timing.
No core rule execution has changed in R34. A fresh independent read-only R34
review found no blocking defect and confirmed these boundaries; it explicitly
requires semantic event binding, internal consistency and final Delta verification
in R37. Final plan/Integration and pushed commit evidence is recorded below;
independent overall review follows the complete batch.

R34 plan: `uv run --no-project python tools/validate.py fast --plan --base
63a59f345aea4bac353bb715fdfa007d1ff306d4`. Final `integration --base` with
the same SHA **PASSED in 1229.89 s**: 24 tooling tests, Data 671 passed / 1
existing skip (236.66 s), Engine 8,870 passed (866.26 s), Bridge 139 passed
(17.85 s), Demo 54 passed, all scoped static checks, three examples and strict
MkDocs. Local Windows evidence only; no remote CI result is inferred.

## SM boundary for subsequent handoff

R35 adds `StageRecorder` to the private `CombatComputationResult` only. Ordinary
Attack explicitly enables the audited base profile; Shield/concentration subclasses,
effect/reaction graphs, Sap/trained Mastery and non-implied Conditions return typed
uncovered metadata without changing existing rule admission. At the real payment
boundary it records `attack_execution`, linked to the actual AttackRolled. Each
accepted emit is sampled after its base fold, with the Damage packet sampled before
nested Death. Direct Death append, completed damage, actual death-save completion,
turn advance, budget reset and post-bookkeeping operation end are sampled separately.
No stage-only entrypoint executes rules; official envelope integration is R37.

The recorder retains only changed bounded actor/combat projections. Event samples
inspect their supplied actor/attacker/target/source identities; direct boundary
checkpoints inspect all supplied participants because turn/reset can alter them.
There is no per-stage Snapshot/scene copy or additional RNG draw. Copy/size counts
and public result consistency validation are R37 work. Original Delta omission
guards and independent Legacy/Delta/RNG tests are retained unchanged.

R35 targeted candidate/base/Legacy/isolation/concentration/Shield/Sap regression:
**230 passed in 31.25 s**. Strict Mypy passes (152 source files). Independent
read-only review found no blocking issue or mechanical behavior change; completed
death-save counters are intentionally sampled after earlier roll/death events.
Final Integration uses the exact R35 starting SHA in the table above.
It **PASSED in 1208.89 s**: 24 tooling tests, Data 671 / 1 existing skip
(222.07 s), Engine 8,884 (853.11 s), Bridge 139 (14.96 s), Demo 54 (30.97 s),
static checks, three examples and strict docs. No Full gate or SM integration
is claimed. This is local Windows evidence; remote CI is reported separately.

R36 enables the base recorder for ordinary Pass and Walk. Pass keeps TurnEnded
at the old turn; turn_advanced precedes round-start audits; TurnStarted is before
ledger/serial/budget reset, followed by a separate budget_reset checkpoint. Only
an actually executed death save has a completed-save stage. Ordinary dying-PC
Pass is audited; effect/Poison/Hold/Shield subclasses and OA/reaction graphs are
explicitly uncovered and retain existing rule evaluation.

Each accepted Walk step is sampled immediately after cell/ledger/budget mutation,
before the next step or ActorMoved flush. Only the moving actor is projected per
step. One actual ActorMoved references all completed steps and its post-fold sample;
no per-cell public event or invented time interval is added. Dash/Disengage retain
their existing mechanics but are outside this stage profile. Internal TurnEvaluation
carries optional stage metadata; public /23 assembly still selects its original
mechanical fields explicitly until R37. It executes only once.

R36 development regression (after repairing a new test's fixture-unpack mistake
and internal TypedDict assembly errors): **345 passed in 25.91 s**, including
existing Legacy comparisons for turns, movement, weapon/stat-block OA, Hold Person,
Poison and Shield. Package-configured Mypy passes all 152 source files; a prior
root-directory Mypy invocation did not load that package configuration and is not
reported as the package gate. Final Integration uses the R36 start above.
The final 17 stage-specific tests passed in 3.71 s. Independent read-only review
found no blocking issue, false completeness or original mechanism regression.
R36 Integration **PASSED in 1395.53 s**: 24 tooling tests, Data 671 / 1 existing
skip (207.61 s), Engine 8,901 (1021.40 s), Bridge 139 (16.42 s), Demo 54
(26.13 s), static checks, three examples and strict MkDocs. R34 native CI
[38376145663](https://github.com/agentic-trpg/trpg-rules-engine/actions/runs/38376145663)
and R35 native CI
[38379525450](https://github.com/agentic-trpg/trpg-rules-engine/actions/runs/38379525450)
were independently queried and passed on their exact pushed commits.

SM must supplement Snapshot-external observer/actor/object/environment facts from
their own owners under the same input version/dependency/OCC fence. It must verify
phase ordering and covered fields, never resample current DB state asynchronously
to repair past events, recalculate damage/resources from audits, or apply stage
views as extra writes. It still owns Event Evidence validation, identities and
atomic persistence with final mechanics/RNG/version/receipt. Stage completeness
never proves noticing, and new interfaces cannot backfill old missing Evidence.
The concrete cross-module event/Episode/Evidence protocol remains OPEN.
