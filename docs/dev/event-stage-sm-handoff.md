# R34–R37: SM handoff for local stage mechanical facts

## Fixed candidate and scope

The final full **R37 source SHA is recorded in delivery evidence after the source
gate/commit/push**. Do not integrate a moving branch or infer that this pending
record authorizes an SM change. This task changes RE only. SM's inspected reference
is `80bc1af59993d29f3281e99b06de60915dc450a9`, architecture
`8141402592946942ff5f6a69f04858ef382bd734`. Neither is modified; SM's Engine/SRD
pin remains `675552406e67470bf3456fe90e88c4f1595bf324` in the inspected RF4 adapter.

Local candidate versions: Evaluation Request/Result `engine-evaluation/24`,
evaluator `dnd5e-evaluation/37`, `EventStageFacts` `engine-stage-facts/2`.
Snapshot `/17`, Availability `/13`, distribution versions and dependencies do not
change. Old `/23`, evaluator `/36` and private StageFacts `/1` are refused.
Initialization/concentration candidates sharing EvaluationVersion are also `/24`;
their stages are explicitly uncovered. C-19/C-24 and common ABI remain OPEN.

## Consumption and actual sampling

One `evaluate(request)` returns the original Typed Delta/proposed_events/RNG plus
`stage_facts`. Accepted requires complete or uncovered coverage. Rejected,
needs_choice and unsupported require explicit null; exceptions return no proposal.
There is no second stage-evaluation API, handle, registry or cross-request cache.
After transport, strictly deserialize and call `result.verify_request(request)`.

The outer Result binds session, command, input world version, rules/data/evaluator
and RNG to the original Request. `participant_ids` references supplied actors.
`MechanicalStage.sequence` and `event_cursor` are this call's local ordinals/frontier,
never authoritative event IDs or historical log offsets. Every `EventStageReference`
names exactly one actual returned proposed_event and its post-fold sample. One
AttackRolled also references its paid execution boundary. One ordinary ActorMoved
references all actual completed step samples and its run-end event sample.

| Facts | Real sampling point |
|---|---|
| Input references | original Snapshot; no copies of whole scene/world in stages |
| Attacker budget | after actual spend, before Activity Resolver; IntentSubmitted is still unpaid |
| Roll audit | after actual AttackRolled fold, including roll count; no visibility claim |
| HP/temp/attribution | after actual DamageApplied packet, before nested NPC Death |
| Life/dead membership | after Death fold/direct append; preserved exact nested ordinal |
| Completed damage/zero-HP counters | after actual damage completion, including changes with no event |
| Save events / completed save | emitted save audits may precede assigning new counters; separate completion carries final counters/stability |
| Turn end / advance | TurnEnded is old turn; turn_advanced records index/round/last-ended changes |
| Next turn / reset | TurnStarted precedes serial, ledger and budget reset; budget_reset is separate |
| Walk position/cost/remaining | after each actually accepted step, before next step/run flush |
| Operation end | after remaining budget bookkeeping; must match covered final Typed Delta |

`StageActorFacts` bounds HP/temp, life/death saves, Conditions, attribution, cell,
AttackBudgetState and MovementLedgerState. Static equipment/traits/senses and
geometry reference the input. Sparse `StageCombatUpdate` changes only the bounded
turn/sequence/death/dedup view. Its pure resolution applies to StageCombatFacts,
never authoritative CombatState or storage. Values are nested immutable typed
models/tuples/frozensets, detached from later mutations. All references and allowed
mutation fields, checkpoint counts, turn clocks, stability/ledger invariants and
covered final Delta pre-values/unique writes/combat binding are checked.

## Coverage matrix

| Original rule path | Stage coverage |
|---|---|
| Ordinary admitted PC/NPC weapon or single StatBlock Attack, no complex graphs/Mastery | complete bounded projection: hit/miss/crit, HP/temp, death/dying/immunity and actual turn consequences |
| Ordinary Pass, including audited ordinary dying-PC save | complete bounded projection with pre/post reset and round boundaries |
| Ordinary Walk without OA/effects/areas | complete bounded projection of the actual path and supplied participants; existing terrain/topology rules reused |
| Shield, OA/reactions, concentration, Poison/Hold/other effect subclasses | uncovered `complex_effects_or_reactions`; existing rule outcome preserved |
| Sap/trained Mastery or non-implied Conditions | uncovered `complex_mechanics`; existing rule outcome preserved |
| Existing Attack input budget mirror not aligned with ledger | uncovered `input_budget_ledger_not_aligned`; no new rule rejection |
| Dash/Disengage, spells/features, items, check/save/rest, initialization, closure | uncovered `operation_not_in_stage_profile` (standalone concentration uses complex reason); original support unchanged |

Complete means all declared bounded mechanics for supplied participants. It never
means all world actors/observers, all mechanical fields, sensory Evidence, noticing,
EpisodeObservation or narrative. Missing coverage means unknown, not unchanged.
No SpellCast is split into invented chant/projectile/explosion stages.

## SM/world work still required

1. Pin both Engine/SRD to the final full candidate source SHA and explicitly adapt
   versioned result validation; do not silently fall back to old envelopes.
2. Keep final Typed Delta as the sole mechanical mutation proposal. Never recalculate
   damage/resources from events to reconstruct stages, replay the action for stages,
   or treat StageCombatUpdate/actor projections as extra write instructions.
3. World fact owners must supply Snapshot-external actors/NPCs, objects, scene
   membership, event-time positions, senses, light, obscuration, blockers and any
   other facts required by the chosen actual Signal scopes. Absence from Snapshot
   proves neither absence nor unchanged state. RE does no observer discovery.
4. Compose trustworthy input references, actual stage changes and owner-supplied
   facts under the **same version/dependency/OCC boundary**. Recheck the prepared
   world/read set, rules binding and RNG in the shared short commit transaction.
   Do not asynchronously resample current DB facts to repair past stages.
5. Determine actual observable Signals separately from mechanical audits, discover
   complete scoped candidates, and validate/persist immutable world Evidence with
   the responsible domain changes, final Delta, RNG/version, formal event identities,
   receipt and reliable Outbox at the existing atomic commit boundary. Rollback
   publishes nothing; no LLM or long evaluation waits inside the write transaction.
6. Continue existing deterministic/JEV perception and authorized OB/Episode flows
   only from committed sufficient Evidence. Mechanical stage completeness is never
   proof that an NPC noticed anything. Old events cannot acquire fabricated historical
   Evidence through this new interface.

SM adapter, database Evidence integration, candidate discovery, NPC sensing, Episode
and end-to-end acceptance are **not implemented by this RE batch**. Keep the existing
unresolved semantics until those components are separately implemented and verified.

## Reproducible local example

`examples/event_stage_request.json` is an explicit fictional canonical Rapier request,
bound to bundled data and explicit RNG. The example consumes it without Legacy runtime:

```text
uv run --locked python examples/event_stage_evaluation.py examples/event_stage_request.json --output result.json
uv run --locked python examples/event_stage_evaluation.py --schema-dir exported-schemas
```

The CLI calls evaluate once, deserializes/verifies its Result and can export actual
Request/Result JSON Schema. It is not a commit adapter or cross-repository ABI.
See [audit, validation and size/copy measurements](event-stage-facts.md).
