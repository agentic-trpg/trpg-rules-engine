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
and current evaluator `dnd5e-evaluation/5`. Required nullable fields must still be present;
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

| Capability | Current support through Batch 4 |
| --- | --- |
| Typed request/result, Combat/NonCombat snapshot, closed attack Delta | Implemented and serialized |
| Explicit RNG capture/restore/transition and effective corpus binding | Implemented |
| Stateless evaluate / PC attack | Executable bounded basic weapon attack |
| NPC explicit attack | Executable bounded carried-weapon or exact stat-block action attack |
| Availability | Independent read-only available / unavailable / unknown query |
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

## Executable attack boundary (Batch 3)

`await dnd5e_engine.evaluation.evaluate(request, loader=immutable_loader)` returns
the validated proposal. Omitted loader uses a fresh bundled corpus; overlays must
be passed explicitly and match the request pin. The entry never calls start_combat,
reads/registers `_REGISTRY`, invokes a Bridge or commits/publishes external state.
It creates one disposable `_LiveCombat`, a local queue and canonical turn hooks.
Legacy keeps its registry and transaction wrapper and calls the same supplied-context
executor. Gate ordering, Action Policy, Activity Resolver, damage folding and turn
continuation are shared. No attack/damage rule algorithm was copied.

Batch 3 admission allowed one PC weapon Attack on one opposing living target on a
plain bright grid, with a known carried weapon, explicit range, ordinary typed attack
activity and known damage types/formulas. Properties, mastery, active effects,
conditions, classes/features, extended budgets, reactions, timing, areas, objects,
transforms, summons and template-driven NPC mechanics return unsupported before
restoring execution RNG or paying. Standard static R/I/V and numeric magical bonus
use the existing resolver. Missing weapon identity can request a preflight choice;
invalid identity/target, turn, range or action economy returns rejected. Legal misses
and immune zero damage remain accepted with actual action payment and RNG.

The adapter captures every resulting mechanical field, projects the closed typed
operations and rejects unexpected unrepresented changes with EvaluationInvariantError.
This guard is an internal failure, not a late unsupported partial result. The ordered
history suffix is returned only as ProposedEvents. An exhaustive differential test
consumer applies the Delta with expected-value checks and compares the complete final
snapshot, events and PRNG state against Legacy. Faults after damage and cancellation
cannot publish to registered listeners or change the input. Independent thread tests
exercise different PRNG states and effective loaders.

read_set currently contains a conservative whole-session world-version fence. It
does not invent per-entity versions; SM must check this fence plus the pinned rules
and RNG stream/version/input state atomically. Engine does not advance world/RNG
versions, authenticate actors, retain results, or supply persistent idempotency.

Batch 3 validation from `f726d554690ea36ac067dbf02c101bd814039ea4`: 105 targeted
contract/baseline/attack tests passed. The requested local Full gate passed in
472.73 seconds: tooling 24, data 631 passed / 41 unavailable-input skips, Engine
6,976 (95.61% coverage), Bridge 139, Demo 53; all static/security/example/docs and
isolated installation/HTTP checks passed using the verified Windows adapter.
XP outcome hydration remains outside this attack slice; the NPC batch must retain
Host-supplied XP values rather than assume all values are derivable from a template.

## Explicit NPC and availability boundary (Batch 4)

The same evaluate entry now admits NPC/Monster actors on their actual initiative
turn. A carried ordinary weapon uses the same PC path. A stat_block_action_id must
be a nonempty strict string matching exactly one action on that actor's supplied,
rules-bound stat block. Names and descriptions are never used. One action-kind
AttackActivity with explicit weapon classification, range in feet, ordinary damage
parts and numeric bonuses is supported, including flat attack bonuses. Reserved
Legacy Multiattack, multiple activities, recharge/limited uses, passive template
traits, legendary/spellcasting mechanics and unreviewed activity state remain
unsupported before execution RNG restoration. This path never drives Monster AI.
Neutral canonical instant-duration encodings, single-target encodings and UI prompt
metadata are admitted without inferring mechanics. Canonical Bandit scimitar and
light-crossbow attacks pass complete Legacy/event/Delta/RNG differential tests.
The broader corpus scan identifies 23 NPC action shapes under the current admission;
that static count is not 23 independently executed acceptance tests. PC weapon
properties/mastery remain unsupported; ordinary canonical PC equipment therefore
needs further admission/delta work before a general MVP combat loop can use it.

`ActionAvailabilityRequest` and `query_action_availability` live in
`dnd5e_engine.evaluation_availability`. This distinct schema has no command_id or
RNGContext. It calls the identical prepare_attack and checks context/RNG invariance;
available means preflight succeeded on the returned snapshot version, unavailable
means a known rule refusal, unknown means unsupported or an unresolved preflight
choice. Schema/binding/internal errors stay exceptions. The result has a structured
reason, actor/session identity, snapshot version and rules pin, with no delta,
events, RNG transition or receipt. It does not authorize control of the actor.

Snapshots now retain explicit Host XP for every encounter actor, including zero
from Legacy's documented sparse-map default. This corrects the inventory's earlier
Derived classification: a Host XP override cannot be reconstructed from CR. Actor,
stat-block, movement and XP closure are validated. Entry points revalidate copied
typed requests, including mutated payload source identities, before rule evaluation.

This is sufficient to start **limited in-memory SM consumer tests**: validate the
local schema, authorize actor and operation, obtain a complete snapshot plus rules
and RNG pin, evaluate outside the transaction, then compare world/rules/RNG versions
and every expected delta value before applying the entire result atomically. Tests
already apply the full typed delta to PC and NPC snapshots and compare Legacy.
SQLite atomicity, durable idempotency, receipts, event IDs/sequences and Outbox remain
SM work. Cross-repository ABI adoption requires review. The Batch 4 evaluator revision
was bumped to `/4` for this executable slice; future semantic changes must change the
revision. Wire schema versions and RNG stream advancement are independent of it.

Batch 4 validation from `32d10d6d80c93e62fed7440f938e1b756e8812a7`: 136 targeted
contract/baseline/attack/NPC tests passed; strict mypy checked 99 Engine source files.
The final local Full gate passed in 476.77 seconds: tooling 24, data 631 passed /
41 unavailable-input skips, Engine 7,007 (95.65% coverage), Bridge 139 (97.05%),
Demo 53, all static/security/example/docs checks and isolated three-wheel / real
Bridge HTTP smoke. The same verified Windows Makefile adapter preserved all checks
and thresholds. Two earlier Full attempts were cancelled during development and
are not passing evidence. Missing-weapon choices now include only admitted weapons;
when all carried weapons require unsupported mechanics the result is unsupported,
and availability is unknown without execution RNG restoration.

## Independent review corrections

The follow-up evaluator revision `/5` fixes two shared damage rules, so both Legacy
and stateless execution change together. SRD 5.2.1 applies resistance before
vulnerability: seven damage with both becomes six after rounding. Character zero-HP
handling now waits for the existing typed whole-instance callback and uses the
sum after Temporary Hit Point absorption. An eight Slashing plus five Fire hit
against one remaining HP and a ten-HP maximum causes instant death, without an
intermediate Unconscious event or a spurious next-turn death-save RNG draw.

The new `character_damage_instances` map is execution scratch, not retained state.
It captures pre-hit HP and the representative typed event, is cleared at completion
or actor departure, participates in Legacy transaction rollback, and prevents
snapshot capture while a hit is incomplete. Separate hits retain separate IDs.

Effects now use the explicit `evaluation_effects.EffectState` DTO family instead
of Legacy authoring models. Every effect, duration, change, action policy, captured
lifecycle and child-effect field must be supplied, including nulls and empty values.
Nested values are strict and instances are revalidated; status serialization is
deterministic. Movement ledgers also reject coercible numeric strings. The internal
capture helper materializes existing Legacy values; evaluate never fills SM gaps.
The wire versions remain `/1`, enforcing their documented explicit-field boundary.

This correction does not admit complex Effect, Spell, Reaction, Area, Object or
timed execution. Other retained complex Legacy records still need explicit nested
DTO review before those capabilities can be admitted. Their presence continues to
return unsupported before execution RNG or payment; schema acceptance alone is not
evidence of a complete complex-state execution contract.
