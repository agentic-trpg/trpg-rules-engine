# Validation and CI

## Session memory consumer checks — 2026-10-21

This batch changes documentation only. Use the existing scope planner with the exact starting SHA and the docs-only strict MkDocs path; do not run unrelated game suites. Local documentation checks are not runtime acceptance or GitHub Actions evidence.

For future consumer integration, follow [the fixed-input contract](stateless-evaluation.md#session-ram-consumer-architecture-2026-10-21). Verify old requests/Snapshots and history remain unchanged after SM RAM publication; candidate inputs include candidate changes; the complete mechanical closure, Schema, FrozenRuleset binding and RNG validation remain. Count Snapshot/request construction and repeated SQL/JSON preparation separately. SM must validate SQL/current RAM/old references/indexes/caches/recovery, all write paths, unknown commits, post-commit publication failure and response loss. RE supplies no independent world recovery or shared mutable state. These are planned checks **[NOT IMPLEMENTED as complete cross-repository acceptance]**, not results of this docs build; current public ABI is unchanged.

The root `AGENTS.md` defines the default development and Git workflow. More explicit
instructions in a task override its defaults. Normal batches use scoped validation;
full validation is reserved for explicit requests, freezes and releases.

| Tier | Command | Checks |
| --- | --- | --- |
| Fast | `make check-fast BASE=<start-SHA>` | Affected package lint/format/types; selected pytest without coverage; strict docs when relevant |
| Focused Integration | `make check-integration BASE=<start-SHA>` | Fast once, complete runtime tests for affected packages and required downstream packages; public API examples and relevant API docs |
| Full | `make check` | All original package checks, unchanged coverage floors (data 96%, engine 90%, bridge 85%), Bandit, examples, docs, selector tests and isolated wheel installation |

Choose one final gate, rather than running Fast then Focused then Full on unchanged
code. `make check-plan` prints a JSON plan without syncing, importing runtime packages,
executing tests, or changing state. `make check-auto` runs Fast and automatically raises
it to Focused when the planner identifies public or high-risk changes. Full is never
selected automatically. A developer should select Focused explicitly for risks that
cannot be inferred from file paths.

## Impact selection

The standard-library selector in `tools/validation_scope.py` includes staged,
unstaged and untracked files. An explicit BASE also includes committed changes since
that exact commit. Without BASE, dirty files define the batch; a clean checkout uses
the latest commit. Invalid bases fail, rather than silently narrowing validation.
CI supplies push.before, or the PR merge-base; initial branch pushes compare main.

- Engine source changes use the reverse Python import closure through source modules
  and test helpers, plus a small fixed regression set for deterministic combat,
  resource/payment rejection, typed effects/events, reactions, checks and delivery.
  Python parent package initialization is included. This is conservative: heavily
  shared modules can select the whole engine suite.
- Public engine contracts/resolvers/types expand to bridge/demo contract tests in
  Fast, and complete downstream runtime suites in Focused. Multiple underlying modules
  or packages also require Focused. Runtime plugin/dynamic dependencies must be treated
  as high risk by the developer; static imports alone cannot prove their full impact.
- Data/schema/canonical or package dependency/configuration changes expand according
  to `data -> engine -> bridge/demo`. Fast runs data tests plus downstream contracts;
  Focused runs all affected runtime suites. Root dependencies expand the entire workspace.
- Bridge/demo changes run their respective suites; individual test changes select that
  file (plus engine regressions when applicable). Shared test fixtures/helpers and deleted
  files fall back to package-wide tests and necessary downstream checks.
- Docs-only changes build MkDocs strictly and run no unrelated Python tests. Examples
  run only when changed, in relevant Focused plans, or in Full.
- Validation infrastructure changes run tooling tests and bounded package contracts in
  Fast, all runtime suites in Focused. Unknown paths conservatively run all workspace
  runtime tests, log why, and require Focused; nothing is silently ignored.

Paths are sorted and deduplicated; a package-wide `tests` selection replaces individual
files. Package pytest processes remain separate because their test namespaces overlap.
All package test files and assertions are preserved. Existing package `make check`
entrypoints remain available for diagnosis.

## Installation and CI

Each gate performs one `uv sync --locked`, selecting the relevant workspace members,
dev extras and docs group. Subprocesses use `--no-sync` / `UV_NO_SYNC=1`; dependencies
are not repeatedly resolved or installed. GitHub uses setup-uv's lock-keyed cache.
See [uv's CI guidance](https://docs.astral.sh/uv/guides/integration/github/) and
[sync semantics](https://docs.astral.sh/uv/concepts/projects/sync/).

CI runs on branch pushes and PRs. An open PR owns validation for its branch, so the
duplicate push validation job is visibly skipped. API lookup failures conservatively
keep the push check. An initial push before opening a PR can still have a Fast run
followed by PR validation; no expensive Full work is automatically duplicated.
New commits cancel obsolete runs within the same event/branch. The stable checks are
`Select validation`, `Fast / Focused Validation`, and manual `Full Validation`.
PR and push validation both use `make check-auto`; logs state the actual tier and plan.

Full is available from CI's workflow_dispatch tier choice; it is never automatic.
Provenance regeneration and release publishing retain their independent maintainer
workflows. No Python compatibility matrix is added to ordinary batches.

Pages is currently unconfigured (repository Pages API returned 404 during this audit).
`Deploy docs` is manual-only and builds by default. To deploy in the future, configure
Pages with GitHub Actions as its source, set repository variable `PAGES_ENABLED=true`,
and dispatch on main with `deploy=true`. Automatic documentation validation uses CI's
strict build and never calls Pages. `make docs` builds locally; `make smoke` exercises
fresh data/engine wheels against locked third-party dependencies without editable
installs, PYTHONPATH, or the Unix-only venv activation assumed by the older package script.
The B9 smoke also builds/installs the Bridge wheel, exports its locked validation
extra (HTTP TestClient dependencies), and executes actual HTTP object spell casts,
retries, cover and movement. The original B6/B7 grid-combat probes remain intact.

## Audit and measurements

Baseline: commit `3ff6143`, Windows, Python 3.14.7, warm uv cache. The preceding
unchanged-baseline root `make check` log spans approximately **503 seconds**
(file creation/last-write times, not a stopwatch measurement). Pytest reported:
data 518 passed / 1 existing skip in 172.05s; engine 6170 passed in 279.97s;
bridge 100 passed in 3.68s; demo 53 passed in 16.00s. Coverage was 97.54%, 95.67%,
and 94.81%. A separate clean-wheel smoke took approximately 33s, and docs about 6.75s.
Those separate timings are not included in the old root gate and are not a measured
combined Full duration.

The old manual CI synchronized separately for data, engine, bridge, demo, examples
and docs across two environments. It always used coverage and all packages; it did
not provide a lightweight automatic branch check. The new scoped tiers remove
coverage, clean-install and unconditional examples/docs from normal work, skip
unrelated packages, and initialize one environment per validation run. Full preserves
all those capabilities, with docs/smoke now included in the root gate.

New entrypoints are each exercised in this infrastructure batch to verify their
composition and measure the tiers. Ordinary batches choose one final gate, avoiding
that repetition. PowerShell stopwatch measurements (including make/bootstrap) were:

| Entry | Before | After | Executed scope |
| --- | --- | --- | --- |
| Fast | No separate tier; default Full approximately 503s | **45.38s** | 451 runtime tests, tooling tests, four scoped static gates, strict docs |
| Focused | No separate tier | **342.93s** | All 6,841 runtime tests, 1 existing skip, tooling, static checks, examples and docs |
| Full | Approximately **503s**, excluding docs/smoke | **368.35s** | All 6,841 runtime tests, 1 existing skip, 24 tooling tests, original coverage/security, examples, docs and wheel smoke |

Fast's measured batch is approximately **11.1x faster / 91.0% less time** than the
previous default root gate, by executing a smaller relevant scope. Focused was 1.47x
faster than that baseline, while retaining complete runtime suites. Fast was measured
when the 17 selector tests existed; seven mocked runner-composition tests and the
staged/worktree cancellation regression were subsequently included in the final Full
gate. The runtime selection stayed identical; unchanged runtime tests were not rerun
merely to update the Fast timing.

Full retained coverage at **97.54% data / 95.67% engine / 94.81% bridge**. Its isolated
wheel smoke passed in **17.97s**, importing both installed wheels from site-packages
and executing the original public grid-combat smoke. All four workflows passed
actionlint 1.7.12. No existing test was deleted, weakened or newly skipped.

The runtime suites remain the bottleneck: Engine took 204.17s without coverage and
205.43s with coverage in these runs; Data took 91.37s and 90.82s respectively. Removing
coverage alone did not materially accelerate these warm runs. The Full timing also
reflects warmer filesystem/process conditions than the historical log, and adds docs
and smoke, so its reduction cannot be attributed solely to the pipeline changes.
Machine-local timings do not predict cold GitHub runners. Shared Engine imports can
select its entire runtime suite and exceed three minutes; the 1–3 minute target is
not a guarantee for public/high-risk changes or conservative fallbacks.

Local logs for this audit are `lean-ci-fast.log`, `lean-ci-focused.log`,
`lean-ci-full.log`, and `lean-ci-tool-tests.log` under the executing user's temporary
directory. GitHub Actions evidence is reported separately after push; local success
does not establish remote success.

### Clean-checkout finding from real CI

The first branch push (`35846bb`) exercised the actual Linux/Python 3.12 workflow.
Scope selection, environment preparation and actionlint succeeded. Data pytest then
reported **10 failed / 468 passed / 41 existing optional skips**: ten lifecycle
translator regressions unconditionally opened ignored `raw_sources/foundry` files.
The populated developer checkout had hidden this existing environment dependency.

Those ten inputs are now a **47,811-byte pinned Git snapshot** under
`packages/dnd5e-srd-data/tests/fixtures/foundry`, with original URLs, SHA-256 hashes,
source attribution, upstream license and enforced LF checkout. The existing tests
use these exact inputs and additionally validate hashes and the existing PINS commit;
their full canonical-output equality assertion is retained. No failing test is
skipped or deselected, and no heavyweight upstream fetch is added to routine CI.
The other optional maintainer-source tests keep their pre-existing conditions.

Only the affected lifecycle test module and its static checks are rerun locally for
this follow-up; unchanged Engine/Bridge/Demo suites and Full are not repeated.
The module passed **34 tests in 5.11s**; Ruff check/format and LF attributes also
passed. The next real CI run verifies the fix from another clean checkout.

## Changed files in this batch

- `AGENTS.md`, `Makefile`, `README.md`, `pyproject.toml`, `mkdocs.yml`
- `.github/workflows/ci.yml`, `.github/workflows/docs.yml`
- `tools/validation_scope.py`, `tools/validate.py`, `tools/smoke_clean_install.py`
- `tools/tests/test_validation_scope.py`, `tools/tests/test_validate.py`
- `packages/dnd5e-engine/Makefile`, `packages/dnd5e-engine/scripts/smoke_clean_install.sh`
- `docs/dev/validation.md`
- Follow-up: `packages/dnd5e-srd-data/tests/test_effect_lifecycle.py` and
  `packages/dnd5e-srd-data/tests/fixtures/foundry/` (ten YAML inputs, manifest,
  attribution README, upstream license and LF attributes)

Runtime source, canonical data, existing test assertions, lockfile, coverage floors,
provenance and release workflows are unchanged. The lifecycle test's input location
is the sole change to an existing test file; its validation is strengthened.


## Architecture and contract checks

The [current cross-repository baseline](stateless-evaluation.md#cross-repository-contract-alignment-2026-10-20) separates implemented DTOs and SM adoption from target proposals. Before public-contract changes, inspect the consumer pin, required fields/statuses, Snapshot/Delta adoption and corresponding tests; then choose the existing gate above. Shared audit/query/finalize rules live in sibling Module Contracts §2.3 and §6.5–§6.6, not in a second validation policy here. Scope optimizations preserve full relevant mechanical closure and add deterministic growth counters. Documentation-only alignment uses the existing strict MkDocs path and no unrelated runtime suites.

## Runtime reuse and source research (2026-10-21)

Before design or implementation, read the project contract and relevant domain docs.
The [unified references](https://github.com/agentic-trpg/agentic-trpg/blob/docs/runtime-reuse-and-references/docs/REFERENCES.md)
organize rule originals, Package setting/adventure evidence and engineering research.
The [reuse contract in MC §2.3](https://github.com/agentic-trpg/agentic-trpg/blob/docs/runtime-reuse-and-references/docs/MODULE_CONTRACTS.md#23-运行时数据复用派生-snapshot-与版本有效性-decided)
is the single shared policy for on-demand scope, dependency validity, incremental
update prerequisites, read/transaction isolation, bounded disposable caches and checks
on hits. Persistence and instance ownership follow MC §2.4; ruleset identity follows
§4.3. These links target the accompanying unmerged documentation branch; in the logical
workspace use sibling `../agentic-trpg/docs/`. They do not claim main already contains
this update.

For rule behavior, consult relevant passages of the bound edition; the project baseline
remains PHB 2024, DMG 2024, MM 2025 and SRD 5.2.1. This does not change any dataset or
RulesetBinding. For lore, verify the Package's adopted setting, era and adventure edition;
using D&D rules does not select a setting. Distinguish official text, approved project
adaptations, engineering choices, target designs and supported implementation. Report
conflicts or inaccessible passages; memory and inference are not verified evidence.
External materials are development references, not a new runtime online lookup dependency.

**Source check, not new runtime acceptance:** starting RE main/origin/main was
`f18e0ad0d0bb6641efb050b51bf919dd41a8631f`. Its
[evaluation_ruleset.py](https://github.com/agentic-trpg/trpg-rules-engine/blob/f18e0ad0d0bb6641efb050b51bf919dd41a8631f/packages/dnd5e-engine/src/dnd5e_engine/evaluation_ruleset.py)
freezes detached effective inputs once, compares the exact FrozenRuleset identity and
publishes the default frozen dataset only after successful construction. Mutable/custom
loaders retain full digest verification. Its
[frozen.py](https://github.com/agentic-trpg/trpg-rules-engine/blob/f18e0ad0d0bb6641efb050b51bf919dd41a8631f/packages/dnd5e-srd-data/src/dnd5e_srd_data/frozen.py)
prepares the UUID index before publication and returns detached copies to callers.
This is prepared-input/index reuse, not a claim that all evaluated results are cached.
Check the [capability matrix](../capabilities.md), exact source/tests and the actual
consumer pin separately before making a behavior claim. This documentation batch runs
no business or performance tests and does not refresh earlier acceptance results.

SM's `refactor/scoped-runtime-validation@4341f69cbb560edd9c9c1b1bd907ed6c599c0ed0`
is unmerged against main `7927a382738fc97e2420967c3e1c6280c717d1fb`; its
operation-local ContainmentState.weights is not the cross-operation memory-cache target.
Its Engine/SRD pin remains `a011eba3ea2226e22d8081294ee38d3ec193b325`, distinct from
this RE checkout. The consumer and existing protections are not changed by this batch.
