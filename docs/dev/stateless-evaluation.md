# Stateless rule evaluation (local implementation contract)

ADR-001 Accepted establishes State Machine ownership. Module Contracts sections
2/4/5/6/8 establish the semantic boundary; their candidate ABI, PRNG encoding,
complex deltas and continuation remain OPEN. The types here are a versioned local
implementation, subject to cross-repository review, rather than a new formal decision.
See [the complete state inventory](stateless-evaluation-inventory.md).

## Contract and snapshot boundaries

Import contracts from `dnd5e_engine.evaluation_contracts`, snapshot types from
`evaluation_state`, operations from `evaluation_delta`, and RNG types from
`evaluation_rng`. Local versions are `engine-evaluation/1`, `engine-snapshot/1`
and evaluator `dnd5e-evaluation/1`. Required nullable fields must still be present;
empty collections explicitly attest empty state. Unknown envelope/operation fields
and unknown discriminators fail schema validation. Legacy models are not changed.

`RuleEvaluationRequest` has separate `operation_kind` and typed `payload`:
`combat.intent` requires a CombatIntentPayload and CombatSnapshot; `rules.check`
requires a matching typed CheckRequest. Both admitted schema branches represent
Actor operations and require real actor_id. System effects/combat.start/source_ref
are not admitted schema branches because their source authority details remain OPEN.
The engine does not authenticate principals; SM authorizes the Actor and supplies
the trusted snapshot and session pin. Schema acceptance does not imply execution support.

CombatSnapshot and NonCombatSnapshot form a strict discriminator union. Both share
complete CharacterState records (mapped from every existing Combatant mechanical
field plus spell/Pact/known-spell/counter resources), explicit effects and scene.
CombatSnapshot additionally carries initiative, sides, positions, movement ledgers,
reactions, concentration, lifecycle/lineage, areas, objects, timed work, summons,
transform original mechanics, resource expenditures and retained rule history.
No snapshot contains RNG, callbacks, queue, loader object or CombatHandle.
Derived HP/temp/condition indexes, topology, current Actor and canonical hooks are
reconstructed. An in-flight execution context cannot be captured as a boundary.

`capture_combat_snapshot` is an internal read-only migration/parity helper over a
provided context. It never looks up the registry and requires the actual static
scene and world version. SM builds the same DTO from its own authoritative records;
evaluate must not invoke a Legacy capture to fill missing input dependencies.

Retained event_log is mechanical rule evidence, supplied by SM from committed
events. ProposedEvents have neither committed IDs nor sequence allocation. SM must
persist the returned ordered proposals together with the Delta/RNG in its own
transaction before rebuilding future committed rule history. It must not recompute
HP or resource payment from event text.

## Failure and commit semantics

| Result status | Delta/events/RNG | Required branch information |
| --- | --- | --- |
| accepted | Complete typed Delta, ordered proposals, explicit transition | choice/error null; accepted means evaluated |
| rejected | null / empty / null | structured rule error |
| needs_choice | null / empty / null | structured preflight choice |
| unsupported | null / empty / null | structured missing capability |

Schema errors, RulesetBindingError, unexpected exceptions/cancellation and transport
errors remain outside these four statuses. RuleEvaluationResult.verify_request
checks identity/world/binding correlation and input RNG association. SM still
validates authorization, write scope, version, expected operation values and combined
invariants in its own atomic transaction; Engine returns no CommandReceipt.

StateDeltaOperation is closed: HP delta, temp HP, death state, conditions, damage
attribution, action/attack budgets, turn state, movement ledger, damage sequence,
processed zero-HP instances and death ledger. Expected values are explicit. Component
updates are restricted to named mechanical components, never arbitrary object paths,
JSON Patch, SQL, complete Actor replacement or complete Snapshot replacement.
Complex effects, resources/inventory consumption and general continuation need
additional independently reviewed operations before those paths can be admitted.

## Rules and RNG binding

RulesetBinding pins ruleset_id, data_revision and evaluator_version. The current
digest is SHA-256 over all enumerated effective typed categories/assets, canonical
model fields, sorted mapping keys and sorted sets, preserving ordered sequences.
This includes applicable homebrew overrides, not just the SRD base name. Listed
assets must exist, identities be unique, and typed item accessors agree. Custom
loaders must enumerate the entire effective corpus and remain immutable during an
operation; the protocol cannot prove undisclosed content in an arbitrary loader.
No digest cache permits stale pins after mutation. This encoding is a local choice.

RNGContext is a separate stream_id/version/state input. `stdlib-mt19937/1` encodes
624 unsigned words, index 0..624 and nullable finite Gaussian cache from stdlib
Random.getstate version 3. Restore uses Random(0) plus setstate, never pickle or
global random draws. RNGTransition includes exact input state/version and successor,
with validated state_changed. Unchanged state is legal when no draws occur. SM owns
RNG stream version advancement; this implementation does not freeze the OPEN policy
for no-consumption commits or promise replay across evaluator/data revisions.

## Supported operation matrix

| Capability | Batch 2 |
| --- | --- |
| Typed request/result, Combat/NonCombat snapshot, closed attack Delta | Implemented and serialized |
| Explicit RNG capture/restore/transition and effective corpus binding | Implemented |
| Stateless evaluate / PC or NPC attack | Not implemented until subsequent batches |
| Availability | Not implemented until Batch 4 |
| Noncombat checks execution, Spell/Effect/Reaction migration | Not migrated |
| SQLite commit, receipt, Outbox, LLM/controller authorization | SM/Host responsibility; outside this repository batch |

## Batch 2 verification

From Batch 1 `980b893369b8ea663037519fe7513b1a5f9893e6`, the impact planner required
integration. The explicitly requested Full gate passed locally in 484.48 seconds
on Python 3.14.7: tooling 24, data 631 passed / 41 skipped, Engine 6,926, Bridge
139 and Demo 53 passed, plus Ruff, formatting, strict mypy, Bandit, examples,
strict MkDocs and isolated three-wheel / real Bridge HTTP installation smoke.
The 41 skips require absent maintainer-only raw Foundry/oracle inputs. GNU make
is unavailable on this Windows host; the temporary adapter executed the exact
checked-in package Makefile dependency recipes through tools/validate.py,
preserving thresholds. These are local results, not a claim of native CI Full.
