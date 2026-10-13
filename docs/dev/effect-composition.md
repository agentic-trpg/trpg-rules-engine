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
| R40 | `03cd42bb751d6219f28881b1de6996ff525fe819` | actual mixed lifecycle continuation; final validation below |
| R41 | after validated/pushed R40 | not yet delivered |

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
