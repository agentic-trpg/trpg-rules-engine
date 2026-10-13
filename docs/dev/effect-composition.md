# R38–R41: bounded Hold Person / Shield composition

## Scope and actual batch baseline

RE only, branch `feat/re-r38-r41-effect-composition`, starting at
`e5254a2ac7166a830a24c46eb35b2b1e0c4f4b80`. Both remote main and the feature
branch were verified at that commit. The original untracked `.evaluation-worktree/`
is preserved and excluded from commits. SM, architecture and dependency pins are
read-only. Accepted means an uncommitted proposal. Each domain owns its facts;
shared commit facilities coordinate their atomic persistence, fencing and receipt.
RE owns no database, service locator, retained handle or cross-request cache.
C-04/C-15/C-16/C-19/C-24 and the common ABI remain OPEN.

| Batch | Actual start | Delivery |
|---|---|---|
| R38 | `e5254a2ac7166a830a24c46eb35b2b1e0c4f4b80` | pushed `470c3dc236df5a2ab2ed340183e82422afec8adc`; full-identity ownership and standalone refactor |
| R39 | `470c3dc236df5a2ab2ed340183e82422afec8adc` | pushed `03cd42bb751d6219f28881b1de6996ff525fe819`; one real composed Attack |
| R40 | `03cd42bb751d6219f28881b1de6996ff525fe819` | pushed `4965450994881b4cf10fc68650c4016181c2a909`; actual mixed lifecycle continuation |
| R41 | `4965450994881b4cf10fc68650c4016181c2a909` | actual bounded same-Wizard replacement; final validation below |

## R38 ownership and projection audit

The new read-only `EffectGraphIndex` checks unique full `(target, id, origin)`
identities and exact effect/clock/condition-link closure before a family view.
`EffectOwnership` then partitions the entire input by encoded source/application,
checks concentration-chain/round/caster mirrors, condition attachments, Shield
owner/response/expiry and same-caster ownership. Unknown effects and encoded known
but out-of-scope effects reject; no filtered residue is silently treated as a
complete input. Classification alone is never mechanical admission: canonical
spell, exact application/condition/clock, class, equipment, source/material,
resource and operation-boundary checks remain necessary.

| Owned records | Source and association | Existing writer/cleanup |
|---|---|---|
| Hold Person applied effect | captured canonical spell lifecycle source; exact target/id/origin | shared SaveActivity creates it; concentration/target expiry removes only that identity |
| Hold empty anchor | caster-side canonical concentration anchor | all successful initial saves still pay and retain the reviewed empty anchor |
| Hold clocks and Paralyzed attachment | full identity, captured repeat save, application/last-repeat clocks, source_effect_id | shared target turn-end repeat scheduler; same identity lineage filter |
| Concentration chain/duration/caster mirror | one exact source with all and only Hold identities | completed damage save/drop, incapacitation/death, caster turn-end cap |
| Shield effect and empty condition link | exact canonical self-target +5 AC effect | actual admitted hit reaction; persists independently of concentration |
| Shield response and expiry ledger | exact effect identity and owner; response absent only for a recorded-dead owner | next owner turn-start expiry; death removes declaration/response, not blanket all effects |
| Known other lifecycle / unknown record | explicit outside-scope or unknown classification | refuse before payment/RNG; never discard from a view |

Standalone Hold and Shield retain their original public execution/admission.
Hold validation now shares structural indexing and pure clock checks. Shield
validation/projection now uses proven ownership; `plain_shield` explicitly refuses
a Hold graph. `without_owned_shield` removes only its validated identity/link/
response/expiry and preserves Hold effects, clocks, conditions, concentration and
all other supplied facts. These are local validation/projector views, never
Snapshot replacement commands or separately executable evaluations.

The existing projector composition is intentionally layered: resource payments
and their actor mirrors/expended ledger are accounted once; concentration owns its
chain/effect/clock/lineage and caster mirror; Shield owns its independent graph;
one base mechanical projection owns HP/temp, Conditions, budgets, turn/death and
damage-instance facts. A future combined projector must normalize each proven
owned component once and run the original exhaustive omission guard once, rather
than concatenate two independently projected HP/Condition/budget proposals.
Expected pre-values remain bound to the original complete Snapshot.

R38 opens no new mixed operation, changes no wire shape/binding version and adds
no stage sampler. Existing ordinary complete stages and explicit complex uncovered
metadata remain unchanged. Later batches must add real evaluate/Availability
admission, one computation, lifecycle continuation and a closed replacement Delta
before claiming those capabilities.

## Executed evidence

Initial standalone Hold/Shield/concentration differential regression after the
index refactor: **191 passed in 33.46 s**. After clock extraction, an existing test
exposed a lost module re-export of OngoingEffectLifecycle (205 passed / 1 failed);
the export was preserved without changing that test. Final focused regression:
**206 passed in 36.21 s**, including new graph ownership/negative/view tests and
all five existing standalone suites. Strict Mypy: 154 source files passed.
Independent read-only R38 review found no blocker and confirmed the bounded
canonical-validation responsibility of later callers.

Plan command: `uv run --no-project python tools/validate.py fast --plan --base
e5254a2ac7166a830a24c46eb35b2b1e0c4f4b80`. It requires Integration, conservatively
including all workspace packages because `.evaluation-worktree/` is unknown.
Final gate is `uv run --no-project python tools/validate.py integration --base`
with that same full start SHA and `PYTHONUTF8=1`. Actual outcome/commit/CI follows
execution; none of the later batch capabilities or whole migration is claimed here.

R38 final Integration **PASSED in 1267.32 s**: tooling 24; SRD 671 / 1 existing
skip (225.93 s); Engine 9,008 (901.94 s); Bridge 139 (16.94 s); Demo 54
(31.95 s); lint/format/types, public examples and strict docs. Native CI
[38408895697](https://github.com/agentic-trpg/trpg-rules-engine/actions/runs/38408895697)
was independently queried and succeeded on its exact R38 source SHA.

## R39 one-call composition

`prepare_attack` (also Availability), `execute_attack` and `evaluate` select the
bounded mixed profile before any draw. `ComposedEffectComputation` has one parent
ShieldComputation and one explicit ConcentrationMechanics component referencing
the same actors/state/events/RNG. It executes the Activity Resolver once. Original
Shield hit reaction decides qualification, pays one owner slot/reaction, resolves
the exact utility effect and returns the effective AC for that original Attack
roll. Actual damage completes once; shared concentration checks occur once per
completed instance, including temporary-HP absorption, followed by existing
zero-HP/condition/death handling. Concentration cleanup removes only its owned
Hold graph; Shield remains independently governed by its exact response/expiry.

Combatant intentionally excludes resource fields. A current typed actor_snapshot
view overlays already priced ResourceUpdate mirrors before a later concentration
save. A new regression first exposed the old input mirror (1 failed / 43 passed,
11.01 s); the corrected view now supplies the paid slot count. This is not another
resource store or extra payment. The final resource/actor mirror and expended
ledger are guarded against the original Snapshot by the existing paid projector.

The mixed projector removes only the proven Shield-owned graph from read-only
before/after views, runs reaction/payment/concentration projection and one base
omission guard, then appends only the disjoint Shield graph transition. HP,
Conditions, budgets and resource writes are not independently concatenated.
Unknown effects or malformed cross-links cannot disappear into a view.

R39 supports one/two Hold targets or the reviewed empty anchor, with one Shield
declaration or active Shield belonging to that same complete Wizard 5 Evoker.
The ordinary attacker must be neither caster nor held target and must attack
that caster. All supplied actors start alive with positive HP. No mixed Pass,
held-target repeat-save or caster turn-end boundary is admitted yet; these return
unsupported before RNG/payment and remain R40 work. Same-spell recast is R41 work.
OA, competing reactions, Magic Missile, other effects/classes/levels and generic
concentration replacement remain outside this bounded delivery.

Existing standalone Legacy comparisons retain their assertions. Composed expected
rolls, +5 AC, flat damage, costs, condition/clock ownership and RNG are asserted
explicitly; Legacy mixed execution has not been assumed a trustworthy oracle.
Real initial Hold casting -> independently guarded Delta -> test Host declaration
-> real Attack is exercised. The test Host does not implement a persistent
production reaction window. JSON roundtrips and independent next-state application
retain complete graphs rather than rebuilding idealized templates.

R39 focused related regression **235 passed in 34.50 s**; expanded composed suite
**50 passed in 12.10 s**. Coverage includes untriggered/blocked/still-hit Shield,
CON success/failure, active Shield on a second ordinary attacker, mixed packets
sharing one instance, temporary HP, NPC death/PC dying, source/material/unknown
graph refusal, resource unavailability, cancellation, interleaved sessions,
stale application, paid mirrors and omission guards. Strict Mypy passes 156 source
files. Wire schemas are unchanged; evaluator binding is `/39`. Original complete
ordinary stages remain; mixed stages are explicitly uncovered.

Independent R39 read-only review found a correctly rebound effective corpus
missing Hold Person could reach a source helper assertion. The new negative test
first failed (1 case, 5.90 s); canonical concentration validation now runs before
that source helper and returns unsupported before RNG/payment. Final reviewer
confirmed this finding closed and no remaining blocker. Final related regression,
including original stages: **378 passed in 56.37 s**. R39 plan requires Integration;
the exact base for plan and final `tools/validate.py integration --base` is
`470c3dc236df5a2ab2ed340183e82422afec8adc` (with `PYTHONUTF8=1`). Actual final gate
outcome and pushed SHA are recorded after execution.

R39 final Integration **PASSED in 1149.15 s**: tooling 24; SRD 671 / 1 existing
skip (207.32 s); Engine 9,059 (823.76 s); Bridge 139 (14.50 s); Demo 54
(27.60 s); static checks, three public examples and strict docs. Native CI
[38415705753](https://github.com/agentic-trpg/trpg-rules-engine/actions/runs/38415705753)
was separately queried and succeeded on the exact R39 source commit.

## R40 actual mixed lifecycle continuation

Ordinary mixed Pass is now admitted by the real evaluate entrypoint. One actual
TurnPhase(end) invokes the shared target-repeat scheduler, identity-specific expiry
and the caster-only concentration cap. Existing standalone Hold turns call the
same component. A request-local `(actor, turn_serial)` set prevents duplicated
processing of that same end boundary; authoritative repeat clocks still travel
in the typed Snapshot/Delta. No global cache or uniform per-call decrement exists.
Shield remains on its original owner-next-turn-start expiry, after TurnStarted,
budget/serial/ledger reset and the actual start phase. No Hold clock or remaining
duration changes merely because Shield changes.

One target's successful repeat removes only that identity/clock/attachment. The
last target or caster-duration expiry closes the chain and caster mirror. Shared
condition filters, damage/death and turn rules are retained. After concentration
ends, the remaining Shield graph/declaration can continue through its original
standalone path. NPC death removes the owner's response/declaration but preserves
the existing independently owned Shield effect/expiry behavior; no blanket effect
clear is introduced. The positive-HP mixed admission does not invent a wider
dying/dead Hold graph profile. Ordinary attacks remain directed at the concentrating
Wizard; arbitrary mixed-target attacks, effects and reactions remain unsupported.

The new continuous tests apply complete typed proposals with an independent
guarded consumer, serialize/deserialize each next Request, and use only its
returned mechanical state/RNG. A test Host advances existing world/RNG versions
and command association at accepted-proposal boundaries; this is not an RE commit
API or proof of production Host integration. Expected repeat naturals, modifiers,
DCs, clock ownership, caster-only cap, round wrap, budget refresh and exact expiry
event ordering are asserted independently. The R39 temporary plain-Pass rejection
is replaced by real continuation positives; malformed Pass still refuses before RNG.

First continuous suite: **7 passed in 8.39 s**, including partial/all target
release, exact independent Shield expiry and an actual nine-caster-end/eighteen-
repeat-save duration run with no idealized per-step Snapshot reconstruction.
Mixed stages stay uncovered; subsequent fully ordinary states recover the original
complete stage profile. R40 changes evaluator binding to `/40` only; envelope,
Snapshot, Availability, StageFacts and existing Delta schemas are unchanged.

Expanded R40 lifecycle/composition/standalone regression: **252 passed in
45.48 s**, including NPC-death/PC-dying continuation, request-local phase
idempotence, multi-session determinism and rejection of an old whole Delta after
the test Host's version advancement. Initial fixture seed assumptions were
corrected without weakening the branch assertions. Final Integration/review
evidence follows actual execution; R41 replacement is not yet delivered.

R40 stage/composition regression after preserving the original plain-grid Attack
gate: **182 passed in 25.16 s**. Strict Mypy passes all 156 source files.
Plan: `uv run --no-project python tools/validate.py fast --plan --base
03cd42bb751d6219f28881b1de6996ff525fe819`; it requires Integration. The same actual
base is used for the final `integration --base` gate with `PYTHONUTF8=1`.


R40 final Integration **PASSED in 1441.49 s**: tooling 24; SRD 671 / 1 existing
skip (228.47 s); Engine 9,069 (1047.83 s); Bridge 139 (17.79 s); Demo 54
(35.76 s); static checks, three examples and strict docs. Independent R40 read-only
review found no blocker. Native CI
[38421666529](https://github.com/agentic-trpg/trpg-rules-engine/actions/runs/38421666529)
was separately queried and succeeded on exact R40 SHA `4965450994881b4cf10fc68650c4016181c2a909`.

## R41 same-source canonical concentration replacement

The public `evaluate` -> `evaluate_hold_cast` path now admits a subsequent legal
Wizard 5 Evoker Hold Person -> Hold Person cast, with the same/changed one/two
Humanoid targets and reviewed level 2/3 slots. It proves the entire original
canonical Hold graph, attachments, clocks, source, optional same-caster canonical
pending Shield and all existing actor dependencies before a narrow initial-cast
dependency view. An active Shield at its owner's current turn is stale relative
to the existing owner-next-start rule and is refused, not silently expired or
ignored. Other graphs/families, multi-source concentration, competing reactions,
Magic Missile/OA expansion and broader class/equipment profiles remain unsupported.

After all target/source/material/action/resource/delivery checks, one computation
emits IntentSubmitted, prices its ordinary Action, drops every owned old Hold
identity with shared concentration cleanup, emits SpellCast and invokes the
original SaveActivity once. Thus old drop/condition removal precede every new
target save/application. No extra RNG, payment or turn occurs. All new successful
saves are an accepted paid cast with old targets released and one canonical empty
caster-side anchor. The new cap starts at the shared maximum 10 and becomes 9 at
this actual caster turn end, just like the original initial cast. Existing
ordinary turn reset/round/ledger rules then run once.

The Legacy `_record_effect_lifecycle_links` audit found that it folds prior chains
after new resolution and silently refreshes identical target identities. Its
`_apply_concentration_anchor` handles an all-success cast only in that later fold.
Consequently Legacy is not a trusted replacement-order/clock oracle here. Tests
assert explicit start-of-cast drop ordering, canonical DC/save outcomes, resource
arithmetic and actual clocks independently, while retaining the original
standalone Legacy parity suites. No new path calls Legacy or infers rules from
a spell name/prose; it uses the encoded canonical concentration flag, spell
activity, shared cleanup, anchor, duration, delivery and budget functions.

`HoldPersonGraphReplace` is a new closed Delta variant, never arbitrary Patch. It
guards complete old effect values/full identities, lifecycle/last-repeat clocks,
lineages, every actor's exact condition footprint, caster concentration id, chain
and rounds. Its canonical `creation` uses the unchanged strict GraphCreate
expected-absence shape only **after atomic guarded old deletion**. Consumers must
validate every old value before any write, delete the old owned graph/attachments,
install the new canonical graph and apply all base/resource/turn operations in
one participating-domain transaction. The operation owns conditions and caster
concentration; base projection normalizes exactly those owned components back to
the original input and runs its full omission guard once. It still guards actual
movement released by old Paralyzed cleanup, budgets and unrelated facts. Resource
Update/slot mirrors and one expended-slot ledger apply once; pending Shield is
preserved verbatim. No two HP/condition/budget deltas are concatenated.

New target clocks capture the real later turn serial/round with last-repeat unset.
Same identity is allowed, but full old clock values distinguish generations. Old
whole cleanup proposals are additionally fenced by world version, request/RNG
association and turn pre-values. Empty anchors have no target clock; their safety
depends on those mandatory whole-proposal atomic version fences, not a fictional
new identity. Applying individual operations out of that transaction is unsupported.

The typed union changes Evaluation `/24` -> `/25` and evaluator `/40` -> `/41`.
Schemas are generated directly by the DTOs and the public schema-export example;
tests deserialize the new union and reject missing/extra/wrong-type/referenced
fields and old formats. Existing request/result consumers and public example are
updated. Snapshot `/17`, Availability `/13`, StageFacts `/2`, dependencies and SM
pins do not change. Availability's existing attack gate is shared with evaluation;
no draw or action is executed for that query. No new spell-availability API is
introduced. New mixed and replacement paths remain stage-uncovered; R37 ordinary
complete stages remain unchanged.

First R41 related regression: **47 passed in 10.29 s** (new continuous replacement
and original initial casts). Expanded replacement suite: **25 passed in 11.70 s**.
The suite applies independent guarded Typed Deltas and JSON-roundtrips next inputs,
including initial cast -> trusted test-Host Shield declaration -> enemy Shield
attack -> actual repeats/expiry -> replacement -> later attack/turns. It covers
one/two old/new targets, changed targets, all-success empty-anchor replacement and
a later anchor-to-target cast, pending Shield preservation and a second real paid
Shield response, source/resource/material/unknown/clock refusal, fault/cancellation
after actual drop/save, omission guards, stale generations and interleaved sessions.
A second-Shield test initially chose a deterministic miss (1 failed / 21 passed,
13.14 s); it now uses a verified initial seed that reaches the asserted trigger.
Production admission was unchanged and the assertion was preserved. Strict Mypy
passes all 158 source files. Final expanded regression, independent review and
Integration outcomes follow actual execution.

R41 plan/final Integration use the exact batch start
`4965450994881b4cf10fc68650c4016181c2a909` with `PYTHONUTF8=1`. Commands are
`uv run --no-project python tools/validate.py fast --plan --base <start>` and
`uv run --no-project python tools/validate.py integration --base <start>`, equivalent
to the repository Make targets on this Windows host. No redundant Fast or Full
gate is substituted. Fixed source-candidate SHA and final CI evidence are added
after source commit/push, without claiming production SM/ABI integration.


R41 final expanded focused command: `uv run --no-sync --package dnd5e-engine
pytest -q` with `test_effect_composition_{recast,ownership,attack,turn}.py`,
`test_stateless_{hold_cast,hold_turn,attack_concentration,concentration,shield}.py`,
`test_event_stage_result.py` and `test_evaluation_contracts.py` (all under
`packages/dnd5e-engine/tests/`): **434 passed in 79.20 s**. A subsequently added
explicit R40 envelope/evaluator rejection test passed **1 / 30 deselected in
2.16 s**. Public `examples/event_stage_evaluation.py --schema-dir` exports current
request/result schemas, verified with Evaluation `/25` and HoldPersonGraphReplace
in the generated result definitions. No checked-in duplicate schema is maintained.

Independent R41 review reproduced two classes of defect before final gate:
non-authoritative actor/effect array ordering was accidentally constrained to
initiative/chain ordering, and a same-generation old clock was refused too late
in result construction. Fences now emit in initiative order and compare keyed
actors; old effects/chains retain actual pre-values with unique full-identity set
closure. Canonical old application and last-repeat serials must strictly precede
the new cast during preflight, before payment/RNG. Three serialized legal-order
positive cases and two draw-forbidden clock negatives cover the fixes. Expanded
focused tests above include them. Reviewer independently confirmed all findings
closed and found no new blocker; no existing admission was narrowed to hide the
valid-order cases. The remaining full-branch review follows all four source commits.


R41 final Integration **PASSED in 930.61 s** on the actual R40 start SHA:
tooling 24; SRD 671 / 1 existing skip (142.41 s); Engine **9,100** (698.04 s);
Bridge **139** (9.33 s); Demo **54**; Ruff/format/Mypy, all three public examples
and strict MkDocs. Native CI is a separate post-push observation, not this local
evidence. All four gates were run sequentially before their own commit/push.
The final R41 source SHA, whole-branch review and post-push CI follow below.
