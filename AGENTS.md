# Development standards

These instructions apply throughout this repository. Follow more explicit instructions
in the current user task when they differ. Keep changes scoped to the requested batch.

## Start and architecture

- Check `git branch --show-current`, `git status --short --branch`, and record the
  starting HEAD before editing. Inspect relevant code, tests, and development docs first.
- Work on a feature/chore branch. Never commit directly to `main`. If the current
  checkout is `main`, create an appropriate feature branch before making changes,
  unless the user explicitly prohibits branch creation (then clarify that conflict).
- Preserve user modifications. Do not overwrite, stage, or commit unrelated changes.
- Preserve deterministic replay (events, final authoritative state, and RNG state),
  fail-closed validation before payment/RNG, typed events and effects, and the shared
  Activity Resolver architecture. Never infer runtime rules from natural-language prose.
- Do not change unrelated modules, remove tests, weaken assertions, or lower coverage
  thresholds to obtain a passing result.

## Choose validation once

Run relevant unit tests while developing, especially regression tests that first
demonstrate a confirmed defect. At the final gate, inspect `make check-plan BASE=<start-SHA>`.
The planner includes committed changes since that SHA AND staged/unstaged/untracked files.
Without BASE it uses dirty files, or the latest commit when the checkout is clean.

- Ordinary batch: run `make check-fast BASE=<start-SHA>` once after the final edits.
- Public APIs, schema/canonical data/dependencies, shared resolvers, multiple underlying
  modules, or high-risk mechanisms: run `make check-integration BASE=<start-SHA>` instead.
  The planner's `integration_required` is a minimum, not permission to ignore known risk.
  Focused includes Fast checks once; do not run both gates on unchanged code.
- `make check-auto BASE=<start-SHA>` automatically escalates the planner's high-risk
  categories and is the CI entrypoint. Use Focused explicitly for risks only you can infer.
- Full `make check` retains complete pytest, original coverage thresholds, security,
  examples, strict docs, and clean-wheel installation. Use it only when requested,
  before release/freeze, or when a concrete risk requires it. It is NOT a normal batch default.
- Docs-only batches build strict MkDocs without unrelated Python tests. Unknown paths
  conservatively broaden checks. Never bypass a reported missing/invalid comparison base.
- Do not repeatedly rerun completed unchanged checks. Broaden or repeat only after new
  changes, a failure, or a newly identified risk. Record commands and actual outcomes.
- Fix relevant failures before completion. If blocked or unable to execute a check,
  report the exact limitation and distinguish unexecuted checks from passing checks.

See [validation design and measurements](docs/dev/validation.md). The selector is an
optimization; explicitly include extra tests when the task's behavior demands them.

## Commit, push, review

After scoped changes and necessary validation, automatically stage the batch's files,
commit with a descriptive message, and push the current feature branch to `origin`.
Normal commit/push needs no additional confirmation. Explicit task restrictions override
this default. Check the staged diff before committing and verify the resulting commit.

Never automatically merge into `main`. Never rebase, force push, commit directly to
`main`, modify the upstream Nat20 repository, or overwrite user changes without explicit
authorization. If validation cannot pass, report and leave the limitation visible;
do not claim completion or green checks that did not run.

After commit/push report the branch, commit SHA, executed validation and timings,
push result, and remaining issues. Distinguish local evidence from GitHub Actions results.
The user and ChatGPT review the branch; the user decides whether and when to merge.
