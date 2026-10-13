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
| R38 | `e5254a2ac7166a830a24c46eb35b2b1e0c4f4b80` | full-identity ownership and standalone refactor; validation recorded below |
| R39 | after validated/pushed R38 | not yet delivered |
| R40 | after validated/pushed R39 | not yet delivered |
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
