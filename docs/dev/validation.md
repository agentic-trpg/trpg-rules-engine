# Validation and CI

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

## Changed files in this batch

- `AGENTS.md`, `Makefile`, `README.md`, `pyproject.toml`, `mkdocs.yml`
- `.github/workflows/ci.yml`, `.github/workflows/docs.yml`
- `tools/validation_scope.py`, `tools/validate.py`, `tools/smoke_clean_install.py`
- `tools/tests/test_validation_scope.py`, `tools/tests/test_validate.py`
- `packages/dnd5e-engine/Makefile`, `packages/dnd5e-engine/scripts/smoke_clean_install.sh`
- `docs/dev/validation.md`

Runtime source, canonical data, existing test files, lockfile, coverage floors,
provenance and release workflows are unchanged.
