# Bounded Openable checks

Branch feat/world-openable-support starts from the State Machine fixed dependency
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
