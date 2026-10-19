# Bounded Openable checks

## Integration with current main (2026-10-20)

`feat/integrate-world-openable` starts at refreshed `origin/main`
`3718f7c3d35424cc7910bcc19673083a357afdb7` and merges
`origin/feat/world-openable-support@74d9e0800c22a740614af72930dd77dd3c064561`.
Their common ancestor is `675552406e67470bf3456fe90e88c4f1595bf324`.
The three source-only commits are `cf80827` (bounded checks), `d596161`
(crowbar correction) and `74d9e08` (historical evidence). Main-only commits are
`63a59f3`, `aa8997d`, `9846e35`, `e5c7a28`, `d7ee80c`, `19671fb`, `e5254a2`,
`f02ab4a`, `7c1eab5` and `3718f7c`. History-free evaluation, audited stages,
frozen effective corpora and conflicting spell UUID rejection remain intact.
No R38–R41 effect-composition branch, dependency downgrade or SM edit is included.

Actual retained capabilities:

| Entry / rule | Behavior |
| --- | --- |
| `pick_lock` | Explicit Dexterity / Sleight of Hand, configured DC and actual Thieves' Tools instance |
| `force_open`, `clear_jam` | Explicit host-adjudicated ability/skill/DC; optional required tool, including Smith's Tools; qualified Strength crowbar leverage |
| Tool admission | Exact instance ID, actor ownership, accessibility, positive quantity and matching item slug, before restoring RNG |
| Shared modifiers | Tool or skill proficiency adds PB once; expertise retains double PB; tool proficiency replaces Jack's half PB; skill plus tool proficiency grants advantage; shared Reliable Talent applies only with eligible proficiency |
| Crowbar | Equipment leverage grants advantage independently of proficiency; a spurious crowbar proficiency label grants neither PB nor tool-based Reliable Talent |
| Result | A failed legal roll is `accepted` with `succeeded=false`; rejected/unsupported requests have no mechanical proposal or RNG transition |

The public entry is `dnd5e_engine.evaluation_openable.evaluate_openable(spec,
loader=assets)` with `OpenableCheckRequest` (`engine-openable/1`). Its nested
request and returned result use **`engine-evaluation/24`**, Snapshot
**`engine-snapshot/17`**, evaluator **`dnd5e-evaluation/37`**, and stage facts
**`engine-stage-facts/2`**. Availability remains `/13`. No public protocol
revision is needed: the additive outer API is retained and now constructs main's
current Result. Generic `evaluate(request)` retains its existing behavior and
call signature; it does not infer Openable intent from an ordinary check.

The original merge passed textually but failed seven of its eight original tests:
the old result constructor omitted required `stage_facts`. The integrated entry
uses main's shared result coverage helper: accepted checks carry explicit
`uncovered("operation_not_in_stage_profile")`; nonaccepted results carry null.
No object stages, sensory Evidence or additional state authority are invented.
Read fencing remains the whole-session world version, the successful check Delta
has zero operations, and ordered `CheckRolled` plus the exact RNG transition are
verified by `result.verify_request(spec.request)`, including after JSON transport.

Like `evaluate`, the entry revalidates a detached request and checks the current
three-field `RulesetBinding` **before** mechanical admission or RNG restoration.
Pass one engine-created `freeze_ruleset(source)` for the final effective custom
corpus, or omit `loader` to share `get_default_ruleset()`'s bundled dataset.
It never freezes per request, constructs a new bundled loader per call, or adds
a parallel rule identity. Raw mutable loaders retain main's explicit compatibility
path (full verification each call); hosts needing reuse must retain a frozen one.

```python
from dnd5e_engine.evaluation_openable import evaluate_openable
from dnd5e_engine.evaluation_ruleset import get_default_ruleset, ruleset_binding

assets = get_default_ruleset()  # or freeze_ruleset(final_effective_loader), once
binding = ruleset_binding(assets)  # persist all three fields on the SM Session
# Build spec.request with this binding and the complete current Snapshot/RNG.
# result = await evaluate_openable(spec, loader=assets)
# result.verify_request(spec.request)
```

SM must separately upgrade both Engine and SRD dependency pins to the final
integration commit and adapt the nested current DTOs, required stage coverage and
three-field binding verification. Retain one selected frozen dataset reference per
effective content version and share it with evaluation, availability and
initialization; compare the persisted binding on restore and before execution.
SM owns object identity/reach, DC, leverage qualification and configured World
consequences, supplies real inventory instance facts, and atomically commits any
authorized consequences with events/RNG/version/receipt. `accepted` does not mean
success or commit. RE performs no World/database writes. Combat Openable and new
combat resource costs remain outside scope; general effects/features retain the
existing explicit capability refusals. SM frozen-dataset integration and its
end-to-end validation remain separate work.

Integration validation is recorded in [migration progress](stateless-migration-progress.md).
`test_openable_integration.py` exercises original pipeline parity, accepted failure,
strict Python/JSON transport, complete deterministic results, pre-RNG refusal and
sequential requests against real bundled frozen tools. It counts file reads,
canonical model construction, source/frozen enumeration and whole-corpus digest
work separately for construction and runtime; repeated explicit/default calls
must add **zero** of each runtime operation.

## Historical source-branch evidence (2026-10-17)

Branch `feat/world-openable-support` started from the State Machine fixed dependency
675552406e67470bf3456fe90e88c4f1595bf324, not current RE main.

engine-openable/1 wraps the existing immutable evaluation request/result with an
explicit operation, actual tool instance and scenario leverage qualification.
The existing generic rules.check API and binding version are unchanged.
The host admits reach, object identity and configured DC/consequences. RE verifies
owned accessible tool facts before RNG, and the shared resolver owns modifiers,
tool/skill proficiency (not counted twice), advantage and reliable talent.

Official SRD 5.2.1: Equipment Tools pp.93-94, Crowbar p.97, Lock p.98.
https://media.dndbeyond.com/compendium-images/srd/5.2/SRD_CC_v5.2.1.pdf

This first support increment is noncombat only; no spell, damage, or action-budget
support is claimed. Generic evaluator restrictions on effects/class features
remain explicit. Failure is a valid accepted check with succeeded=false, not a
rejected action; only the host's configured success consequences may change World.


Validation evidence (2026-10-17): integration planner selected tooling + all four
packages due to the pre-existing untracked .evaluation-worktree. Tooling 24 tests,
data 671 passed / 1 skipped; bridge 139 passed; demo 54 passed; package lint/types,
three executable examples, and strict MkDocs passed. Initial engine run: 8858
passed / 1 failed, caused by passing new optional kwargs to the generic fault
injection seam. The generic call signature was restored; the complete affected
check suites then passed 474 tests. A frozen full engine rerun is pending; this
note does not claim that it has passed. No source assertion was weakened.


Final bounded API review: crowbars grant leverage advantage, never tool proficiency
bonus even if a supplied proficiency label incorrectly names the equipment. The
actual-resolver edge regression passes; Openable tests now total 8. Strict types
pass on this revision. Full engine rerun covers the generic-seam repair; the final
crowbar-only correction is covered by the focused API tests, not claimed as a new
whole-suite frozen run.


Final resumed integration evidence: full engine rerun after the generic signature
repair passed 8,859 tests in 636.67s (exit 0). Final crowbar-only API correction:
8 focused tests passed in 1.75s, plus strict types. This is combined integration
plan evidence, not a single final all-package gate invocation or a new frozen
whole-engine run after the last bounded API correction. Data 671 passed / 1 skip,
Bridge 139 passed, Demo 54 passed, tooling 24 passed; lint, format, types, examples
and strict documentation all passed. Runtime pin is d596161060555630019163ab301e1d25fd816549.
