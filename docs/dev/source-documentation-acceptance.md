# Source documentation acceptance — 2026-10-27

Baseline: fetched `origin/main@7bb02b955609f68fc78ff57dac2d51a0c2333366`.
Branch: `docs/source-docstrings-and-skills`, in an isolated parallel task worktree.
Original main and its untracked `.evaluation-worktree/` are preserved. The task changes
source explanations and development guidance, without changing rule behavior, public
signatures, database structure, dependencies or runtime architecture.

## Coverage and boundaries

Engine, SRD Data, Bridge and Demo production `src` contain 192 modules, 515 classes and
1,658 functions/methods including nested functions. Added six module, 240 class and 845
function explanations across 154 files; all nodes now have docstrings. The original
186 module, 275 class and 813 function explanations are preserved (1,274 total).
Tests/examples, offline maintainer tools, third-party/vendored/generated/temp files
were not expanded into a blanket docstring task. Installed production audit/CLI modules
remain included. Protocols, model validators and simple getters use proportionate prose.

No SM experiment summaries were reused for RE. Explanations describe actual fixed-input
evaluation and uncommitted Delta/events/RNG; Legacy/Bridge working combat, local homebrew
JSON storage and receipts are distinguished from authoritative SM persistence.
Reviewed frozen corpus capture, defensive copy getters and mutable-loader safeguards;
no evaluation-result caching or independent world lifecycle is claimed.

Same-name helpers were checked in their owning scope, including Homebrew/object removal,
speed validation/runtime initialization, concentration damage/death and rider/spell delivery
preparation. Targeted descriptions were corrected rather than using name-only semantics.
The project-wide [maintenance standard](source-documentation.md) is shared with SM;
AGENTS.md makes it mandatory independently of automatic Skill selection.

SM main `b9ae704` now has the scoped SessionMemory/instance-field consumer implementation,
as recorded in its existing evidence. New dated entries supersede the current-status use
of historical 2026-10-21 NOT IMPLEMENTED text while preserving that history. No new RE ABI,
SM dependency-pin adoption, full Legacy exit, complete restart or multiplayer acceptance
is implied by this prose batch.

## Skills and verification

Installed `.agents/skills/trpg-code-documentation/SKILL.md` and its AST verifier. The
workflow and script are byte-identical to SM's installation and refer to one authoritative
sibling standard. Metadata validates with the bundled skill-creator checker. Static
representative routing covers adding/refactoring functions and checking stale prose in
this workspace, without assuming discovery of sibling Skills. Actual automatic triggering
in a fresh Codex session was not tested.

| Check | Result |
| --- | --- |
| `uv run --locked --no-sync ruff check packages apps tools` | exit 0 |
| `uv run --locked --no-sync ruff format --check packages apps tools` | exit 0 |
| `python tools/validate.py fast --plan --base 7bb02b955609f68fc78ff57dac2d51a0c2333366` | planner inspected; source paths conservatively select runtime checks |
| `python .agents/skills/trpg-code-documentation/scripts/verify_docstrings.py --base 7bb02b955609f68fc78ff57dac2d51a0c2333366` | exit 0; all 192 files compile; no non-docstring AST differences |
| Existing docstrings | all 1,274 original values retained |
| Generated schema comparison | 718 validation/serialization schemas equal after removing description metadata |
| Frozen rules binding comparison | all three fields unchanged |
| Strict MkDocs build | exit 0 |
| Skill metadata / copy consistency / verifier checks | valid metadata, identical copies, six positive/negative invariants passed |

Schema comparison covers selected public evaluation, state, Delta, RNG, effect, stage,
Openable, resource/casting, events/intents, SRD and Bridge request models. Added docstrings
can change descriptions in generated schemas/API help; field shapes and constraints in
these 718 generated schemas are unchanged. Binding remains evaluator
`dnd5e-evaluation/37`, data revision
`sha256:f1f2b8d5e9bef154940cb14c615f2f4ac7301a4ab9d869841bc1b86e9664c44e`.
Existing audit modules use their unchanged module `__doc__` for argparse help; strict
mkdocstrings rendering is part of the documentation gate.

The existing locked environment is reused without syncing dependencies. The task's
explicit prose-only validation scope, AST proof and generated-consumer checks determine
the final gate; the path-based planner does not recognize docstring-only source changes.
No entire game suite is run merely because many source paths changed. No runtime/fault/
performance acceptance, new retrieval metrics or GitHub Actions result is claimed.
Natural-language semantics are not proved by AST equality; dynamic consumers are not
exhaustively verified. No business defect was opportunistically fixed.

Focused existing consumer tests also passed: Engine public API/lifecycle wire/frozen ruleset
111, SRD frozen loader 5, Bridge overlay 10 and CLI 2 (128 total). The first Bridge run
had two fixture setup errors because the task-local basetemp parent did not exist;
creating that parent and rerunning only the CLI file passed. No business code changed
for this correction. Existing dependency deprecation warnings remain.

Two inherited R23/R24 links now use explicit stable HTML anchors so the same source
works in GitHub and MkDocs despite slash-containing heading slug differences.

Final static checks also include the installed verifier's Ruff lint/format. All 23
changed/new Markdown files across the coordinated batch pass local path/anchor, table
and fence checks. Working/staged `git diff --check` is the commit gate. These are local
results; actual automatic Skill triggering and remote CI remain unverified.

## 2026-10-27 follow-up: system and interface overview

Starting HEAD: `f1a3235b2c636156e90a7a7192d980a62f949070`, same existing
`docs/source-docstrings-and-skills` worktree. Fetch confirmed the remote task branch
matches, without later commits; origin/main remains `7bb02b9`. Started clean; no new
branch, merge, unrelated changes, experiment edits, Pages or paid model API calls.

Added [docs/SYSTEM_OVERVIEW.md](../SYSTEM_OVERVIEW.md), 143 lines. Existing concept/API
pages remain detailed references; none supplied the requested compact repository-wide
Codex navigation, so this is the single new structure/interface entrypoint.
It covers Stateless evaluate and availability, explicit Snapshot/binding/RNG, admission,
candidate computation and uncommitted Delta/events/RNG, SRD/FrozenRuleset, rule module
collaboration and the separate Legacy/Bridge/Demo local-state boundaries. Supporting
candidate APIs are distinguished from evaluate dispatch; schema values remain linked
to current definitions rather than hard-coded as permanent constraints.

Changed files: `AGENTS.md`, `README.md`, `docs/index.md`, `mkdocs.yml`,
`docs/SYSTEM_OVERVIEW.md`, `docs/dev/source-documentation.md`, this acceptance record,
and `.agents/skills/trpg-code-documentation/SKILL.md`.
The overview is in MkDocs navigation. AGENTS requires it for structure/ownership/interface
changes, followed by relevant authority/source; small local function edits do not require
reading all architecture docs. The shared documentation workflow matches SM byte-for-byte
and separates function detail, module background and repository overview maintenance.
No production Python source, public contract, rule, dependency or database changed here.

Static source navigation verifies 107 file/symbol references across the three overviews;
20 changed Markdown files pass local path/anchor, table and fence checks. Six Skill
installations validate with the bundled quick checker in UTF-8 mode, and the shared copies
match. `uv run --locked --no-sync python -m mkdocs build --strict` passes (exit 0) in the
existing locked environment; working/staged `git diff --check` gates also pass (exit 0).
The read-only validation planner is inspected against the exact start SHA and selects
documentation only: no runtime packages/tests and no integration escalation.

No Mermaid/images are included, so no diagram rendering verification is applicable.
Complete Stateless migration, general SM integration, full recovery, multiplayer or a
complete Agent Host/UI remain outside the claimed acceptance. No game tests, new runtime
measurements, automatic Skill trigger checks or remote CI checks ran in this continuation;
the earlier 128 tests and Schema comparison remain the initial batch's evidence.
