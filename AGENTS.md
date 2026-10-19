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

## Authoritative project architecture (mandatory)

This Rules Engine is one repository in the Agentic TRPG project. The sibling
`agentic-trpg` repository holds the authoritative cross-module architecture.
From this repository root, read these documents before designing or modifying
implementation, contracts, state handling, or validation behavior:

- `../agentic-trpg/docs/MODULE_CONTRACTS.md` (primary cross-module authority;
  especially the state ownership, runtime data reuse / snapshot principles in
  section 2.3, package/instance boundaries, and RulesetBinding in section 4.3).
- `../agentic-trpg/docs/STATE_MACHINE_ARCHITECTURE.md` (state ownership,
  authoritative commits, snapshots, OCC, transaction, RNG, and recovery rules).
- `../agentic-trpg/docs/WORLD_SUBSYSTEM_DESIGN.md` (world/geometry dependencies,
  local queries, scene snapshots, and full-audit versus local-check boundaries).

For changes touching a particular domain, also read other relevant top-level
project docs (for example, `AGENT_ARCHITECTURE.md`, `ADVENTURE_PACKAGE_SCHEMA.md`,
`MVP_SCOPE.md`, or `GAME_CORE_EVOLUTION_PLAN.md`) and the local RE docs/tests.
The current authoritative docs take precedence over older progress notes or
historical reviews; preserve their `[DECIDED]`, `[PROPOSED]`, `[OPEN]`, and
`[NOT IMPLEMENTED]` distinctions. Do not assume a documented target is already
implemented or invent a new cross-module contract from an unapproved proposal.

If the sibling repository or a required document is unavailable in this Codex
workspace, say exactly what could not be read and request access before any
architecture-sensitive change. Do not claim to have read inaccessible docs.
Do not modify the sibling repository unless the current task explicitly asks.

### Global integrity audits versus normal evaluation

- Full-corpus / full-world / full-session integrity audits belong at explicit
  initialization or frozen-dataset admission, save-load/recovery integrity
  verification, migration verification, or explicitly requested diagnostics.
  Do not introduce such audits into ordinary queries, action preflight,
  evaluation, or commit paths merely for defensive convenience.
- Normal RE requests **must** still validate their typed request/snapshot,
  ruleset identity, relevant dependency closure, state delta, RNG transition,
  and result invariants. Checking the **complete relevant mechanical snapshot**
  is not a forbidden whole-world audit; never weaken required validation.
- Use the proven immutable `FrozenRuleset` identity for inexpensive runtime
  ruleset checks. Full effective-corpus capture, duplicate-identity checks,
  and digest computation belong to freezing/admission. Mutable/custom loaders
  require their existing correctness safeguards unless an equivalent safe
  freeze/version protocol is implemented and tested.
- For a bounded lookup or operation, inspect the full call chain: an apparently
  local wrapper must not enumerate unrelated rules assets, actors, scenes, or
  objects unnecessarily. Preserve fail-closed behavior, version consistency,
  deterministic replay, and authorization. Replace any inappropriate global
  audit with an equivalent scoped check before removing it; add regression tests
  for both valid and invalid states.

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
