# RF-RE1–RF-RE4: history-free evaluation and modular SM integration

This is the coordinated RE-HIST-01 migration, not a conversion of every Legacy
combat mechanic. RE computes a proposal; the fact owners in SM share one SQLite
transaction. A snapshot is a read-only projection, never a replacement write.
Accepted does not mean committed. No RE code may access an SM database/component.

## Baselines and serial delivery

| Repository | Branch | Starting HEAD | Initial local changes |
| --- | --- | --- | --- |
| RE | `refactor/re-rf1-rf3-history-free-evaluation` | `38fcfcd4c3a744b30f792775415e798b323d4331` | unrelated `.evaluation-worktree/`, preserved |
| SM | `refactor/sm-rf4-rules-adapter-upgrade` | `9ea750bab2c1ff6d990a02b13b10ffff1c81ed88` | existing GC2 navigation work, preserved and excluded from this batch |

Origins are respectively `agentic-trpg/trpg-rules-engine` and
`agentic-trpg/trpg-state-machine` on GitHub. The adjacent `agentic-trpg` repository
is read-only architecture reference: MODULE_CONTRACTS sections 0.3/4/5,
STATE_MACHINE_ARCHITECTURE section 3, and MVP_SCOPE recovery/authority boundaries.
SM's `docs/RF1_DOMAIN_RUNTIME.md`, fact owners, session access, event persistence,
CombatHandler and LegacyRulesAdapter were read before this audit. Neither SM nor
the architecture checkout has an AGENTS.md at this baseline; RE's root standards
and the explicit task govern RE validation and serial commits.

| Batch | Scope | Status |
| --- | --- | --- |
| RF-RE1 | Contract/field/call-chain audit; existing differential baseline | audit recorded; validation below |
| RF-RE2 | Separate candidate mechanics from this call's ordered typed events | pending RF-RE1 validation/commit/push |
| RF-RE3 | Remove full-history input and Legacy event-index requirements; version all affected envelopes | pending RF-RE2 |
| RF-RE4 | Upgrade SM using the validated, pushed RF-RE3 full SHA | pending RF-RE3 and RE review |

Each batch records its actual starting SHA, validation and commit before the next
batch begins. No merge, rebase, reset, force push or upstream Nat20 change is part
of this work. RF-RE1–3 change only RE; RF-RE4 changes only SM.

## Actual, current and target contracts

| Surface | SM actually pinned | RE baseline | RF target |
| --- | --- | --- | --- |
| Engine/SRD Git pin | both `602dcb8d448e670049427bbb751d6ed226005298` | RE HEAD above, packages 0.6.0 | both RF-RE3's pushed full SHA, not a branch/path |
| Evaluation | `/1` for combat, `/4` for complete noncombat | `engine-evaluation/22` | explicit new version in RF-RE3 |
| Snapshot | `engine-snapshot/1` or `/2` | `engine-snapshot/16` | history-free explicit new version |
| Availability | `/1` or `/2` | `engine-availability/12` | explicit new version with the same mechanical preflight |
| Evaluator binding | pinned package uses `/4` and `/8` profiles | `dnd5e-evaluation/35` | new revision, no aliases |
| Closure | flat outcome fields and optional NPC life normalization | typed `historical` report, `xp_increments`, already-consistent life/death | write only ended/XP; reports never reapplied |

RF-RE2 keeps all public versions/shapes unchanged. RF-RE3 removes history from
new stateless input and versions the nested schema and every envelope containing
it, including CombatInitializationCandidateRequest/CombatCreationCandidate.value
and ConcentrationDamageRequest. Validation and serialization JSON Schema
modes must both remain strict (including `test_lifecycle_wire_schema`). Legacy
public coroutines and HTTP Bridge continue their existing independent
runtime API. No moving dependency pin, silent legacy default, or candidate-state
output for SM to persist is acceptable.

## Fact/consumer/Delta/commit matrix

Rows group fields that have the same source and lifecycle. The complete Legacy
declaration index remains in [the field inventory](stateless-evaluation-inventory.md).
Names here describe the actual current DTO, including unsupported dependencies.
SM column describes the required controlled owner, not a claim that every RE
operation is admitted by SM. A known empty component is explicit; unread/unknown
facts must cause refusal rather than being converted to empty.

| Snapshot fields | Source / lifetime | Actual RE consumers | Delta / SM commit owner | SM gap at RF-RE1 |
| --- | --- | --- | --- | --- |
| session_id, world_version, schema/discriminator | SessionAccess; session/OCC | request/result verification, all projectors | version/receipt fences; SessionAccess | old schema pins |
| character_states identity, attributes, proficiencies, equipment, spell repertoire | CharacterFacts/mechanical actor records; character life | evaluation_actor, preflight, checks, shared Activity Resolver | immutable unless a named typed operation; CharacterFacts | explicit new equipment/resource/casting closure needed |
| HP, temp HP, life/death saves, conditions, attribution | CharacterFacts; current character | damage_rules, death_saves, evaluation_death, attack/check/save/turn/effects | HPDelta, TempHPSet, DeathStateUpdate, ConditionsUpdate, DamageAttributionUpdate; CharacterFacts | retain authoritative HP across exploration/combat |
| action/bonus/reaction budgets, attack counters, grants, movement_remaining and turn flags | explicit actor mechanical facts; turn | turn_rules, action_economy_rules, shared resolver | ActionBudgetUpdate; CharacterFacts (no mass field relocation) | preserve closed admitted field set |
| resource_state, actor spell_slots/pact_slots/custom_counters | owned resource component and matching actor mirrors; rest/use | evaluation_resources, payment, invocation/rest/reactions | ResourceUpdate + actual-payer expenditure ledger; controlled CharacterFacts resource interface | absent from old snapshot; unsupported pools must refuse |
| casting_state | explicit source/repertoire/build facts; character/equipment | evaluation_casting, class dependencies, spell/reaction admission | unchanged input dependency in admitted SM slice | absent; do not invent spells from prose |
| inventory_state | owned instance/quantity/charge/accessibility facts; item life | items, equipment closure, item/spell preflight | InventoryConsume and item-related typed operations; controlled CharacterFacts inventory interface | old slug array is not general instance/charge evidence |
| effect_states | controlled active effects; until expiry | concentration, Shield, Hold/Poison lifecycle, effect projection | Effect/concentration/Shield transitions; controlled effect facts | old adapter supplies empty only for its admitted empty profile |
| scene_state | WorldFacts scene/grid/obstacles; scene life | topology, range, movement, checks, lifecycle context | no generic scene replacement; WorldFacts | dynamic combat obstacles still outside SM admission |
| combat_id, initiative_ids, party_ids, encounter_ids | CombatRecords; encounter | preflight, turn, outcome, all combat operations | immutable roster except explicitly supported operations; CombatRecords | retain roster and identities |
| current_turn_index, round_number, turn_serial, ended, monster_turn_start_done, last_ended_turn, departed_actor_id | CombatRecords; current turn/closure | turn_rules, initialization, closure, reactions/lifecycle | TurnUpdate / CombatClose; CombatRecords | ended must be authoritative and legacy-consistent |
| actor_zone | actual actors positions while active; frozen terminal geometry | range, walk/opportunity, death evidence | PositionChange after spatial validation; CharacterFacts.move | preserve on-demand spatial projection; no second position authority |
| movement_ledgers | CombatRecords per actor/turn | movement, turn reset, condition speed clamps | MovementLedgerUpdate; CombatRecords | original attack reset operations already applied |
| opportunity_attack_weapons, monster_slug_by_entity, xp_value_by_entity | approved encounter bindings; encounter | opportunity/stat-block preflight, outcome | bindings immutable; XP increments to CharacterFacts | SM retains its narrower basic attack profile |
| deaths_recorded, dead_ids | retained death evidence/current death membership | death consistency, turn skipping, outcome | DeathLedgerUpdate; CombatRecords | exact closure and attribution validation required |
| expended_resources | actual-payer expenditure totals; encounter | payment and historical outcome | ExpenditureLedgerUpdate; CombatRecords | closure reports must not charge again |
| concentration_chain, concentration_rounds_remaining, conditions_by_effect, effect_lifecycles | effects + captured application clocks; effect life | concentration/effect-turn/Hold kernels | effect/concentration transitions; controlled effect facts + CombatRecords links | not broadly admitted by SM |
| pending_reactions, active_reaction_responses, reaction_effects_pending_expiry | declarations/responses and expiry owner; until trigger/expiry | Counterspell, Shield, opportunity admission and payment | PendingReactionUpdate / reviewed transitions; CombatRecords/effect facts | SM does not automatically gain RE reaction support |
| damage_instance_sequence, processed_zero_hp_damage_instances | CombatRecords scoped encounter counter/deduplication | next_damage_id, damage fold, concentration | DamageSequenceUpdate, ProcessedDamageUpdate; CombatRecords | sequence requires no RNG, no historical array index |
| rider_uses, help_grants, help_check_grants, hidden_entities, vex_grants, rage_bonus_extensions, sap_marks, slow_marks | explicit use/grant/expiry markers; rule-defined turn/effect | preflight, Sap, dependency guards; broader Legacy consumers separately | SapMarksUpdate for admitted Sap; unchanged/unsupported otherwise | no new permission to use unsupported mechanics |
| monster_action_uses_by_entity, legendary_windows_used, legendary_resistance_armed | explicit action/recharge/window markers; turn/encounter | stat-block and sidecar admission; Legacy action kernels | unchanged/unsupported in new admitted paths | no complete legendary/recharge migration |
| legendary_resistance_applied_event_indices | Legacy full-log fold deduplication; log-relative | only nonempty rejection in public stateless preflight; actual reads/writes in Legacy orchestrator | no admitted Delta | remove Legacy-only index from new DTO; reject nonempty conversion, never silently discard |
| timed_activities, persistent_areas, combat_objects | typed captured timing/geometry/sequence facts | strict sidecar guards; broader Legacy kernels | unchanged/unsupported in public stateless paths | no new broad timing/area support |
| constructs, transforms, summons, summon_counts | anchored roster/form facts; effect/encounter | strict sidecar guards; Legacy mechanics | unchanged/unsupported in admitted paths | outside this coordinated slice |
| event_log | ordered committed audit evidence; durable SM event store | baseline proposal suffix, projection prefix, closure scan; adapters | Events → EventPersistence, never state replacement | remove hot-path SELECT and candidate append after RF-RE3 |
| noncombat combat_setup | approved prospective roster/positions/initiative facts | bounded initialize_combat candidate API | initialization result uses explicit created component | not an automatic public combat.start expansion |

RNG is a separate explicit RNGContext from SessionAccess, not a Snapshot field.
Every result verifies its exact input stream/version/state. Only SessionAccess
commits the successor alongside component edits, events and the command receipt.

## History dependency call-chain audit

These are classifications and traced dependencies, not an assertion that all
search matches are public evaluator dependencies.

| Classification | Producer → consumer chain | Historical purpose / replacement |
| --- | --- | --- |
| public stateless | evaluate → execute_attack → CombatComputation.result → evaluate suffix slice | concatenates old history and local buffer solely to extract that buffer again; RF-RE2 returns mechanics and ordered events separately |
| public stateless | turn/move/item/spell/Hold/concentration → computation.result → specialized Delta → attack_delta | prefix comparison is history integrity, not mechanics; RF-RE2 compares unchanged input history while Delta still exhaustively rejects every other unexplained modification |
| public stateless | movement._execute_walk → computation.result → step_reactors; Concentration/EffectTurn/Shield result overrides | intermediate per-step mechanics and subclass effect/payment projections must also unwrap the new result, not only final evaluate callers |
| public stateless | evaluate(combat.close) → evaluate_closure → CombatEnded scan | duplicate-close fence; already has explicit ended. RF-RE3 uses ended; legacy conversion checks contradictory ended/history before removing history |
| public stateless | query_action_availability → prepare_attack → snapshot_support_failure | no log scan; Legendary event indices only cause unsupported. Remove index dependency together with the new Snapshot version, keeping other legendary guards |
| internal candidate | initialize_combat → initial_combat_state; capture_combat_snapshot/capture_evaluation_snapshot ↔ execution_context | initialization creates known fresh mechanics; parity adapters copy full Legacy history, not reached by evaluate/availability. Make history a named Legacy boundary; do not call it from new evaluation |
| Legacy runtime | _emit/_submit_live_intent → pre_event_count → recent-event slices (attack riders, Sap, damage saves, concentration/effect links) | per-operation emitted evidence; still Legacy, retain behavior and full-log delivery |
| Legacy runtime | _current_turn_events → Rage turn-end / _effect_applied_during_current_turn → timed expiry | turn history encodes current usage/grace. New admitted effects use captured lifecycle clocks / last-applied serial; general Rage/timed flags are already unsupported and stay so |
| Legacy runtime | live_spell_delivery application-{len(event_log)} → lifecycle application capture | Legacy effect identity; admitted new Hold/Poison clocks use explicit turn_serial/application facts, not this constructor |
| Legacy runtime | _sync_legendary_resistance → legendary_resistance_applied_event_indices | log-index deduplication of used-pool events; new stateless legendary execution is not supported. Nonempty old bookkeeping cannot be converted by guessing |
| Legacy runtime | live_reactions/live_effect_lifecycle/live_attack_riders/ongoing_spell_activation/timed_activities/persistent_areas | post-resolution emission windows, damage/effect outcomes; no public stateless calls to these live wrappers |
| Legacy delivery | combat_objects.observe / persistent_areas.observe → CombatEnded, Death, CombatantLeft | event subscriptions/current sidecars; no full-history Snapshot requirement in new evaluator |
| Legacy delivery | end_combat → start_index → emitted end events; views → event_count | historical handoff and presentation; independent Legacy API |
| Bridge | routes_combat → combat_execution → submit_player_intent; state retains handle/receipts | Legacy HTTP runtime, not RuleEvaluationRequest. No hidden stateless adapter exists to upgrade; downstream regression still required |
| SM hot path | CombatHandler read/OCC recheck → LegacyRulesAdapter._combat_snapshot → full ordered events SELECT | RF-RE4 removes this query from both preparation and recheck; history audit/EventPersistence remain |
| SM validation | validate_dnd_result → delta_changes → temporary candidate + proposed history | RF-RE4 validates mechanics/closure without history append; candidate never written as a whole |
| audit/tests | independent test apply helpers append proposals; capture adapters compare complete mechanics/events/RNG with Legacy | preserve independent assertions; update only versioned contract assumptions, retain ordered Legacy event comparisons |

Specific Legacy scan sites also include orchestrator's spell/feature resolution,
legendary windows, payment/event folds and form/effect linkage; their callers are
Legacy submit/turn hooks, not the new evaluation dispatch. The admitted new
computation subclasses use their own local `events` and explicit current fields.
There is no justification for adding a second summary log or additional RNG draws.
No necessary missing current fact was found for the already-admitted paths: ended,
turn_serial, damage_instance_sequence, usage markers and lifecycle clocks exist.
The Legacy-only event-index set is not a current rule fact in this subset.

## Behavior baseline and required regression evidence

Existing independent comparisons are the pre-migration oracle, not regenerated
golden fixtures: attack/core/weapons and class attacks, turn/movement/opportunity,
item consumption, spell/feature/Counterspell, concentration, Shield, Hold/Poison,
closure, checks/save/rest and chained workflows. They compare independent Delta
application to Legacy capture, ordered typed events and full RNG successor;
exception/cancellation tests prove input/Legacy state unchanged on failures.
The bounded baseline run and the final per-batch gate are recorded below.

RF-RE2 adds direct envelope tests with nonempty committed history, checks that
candidate mechanics retain it unchanged and proposals contain only new events,
and keeps the unrepresented-change guard. RF-RE3 adds history-free schema and
serialization/fresh-call/interleaved-session checks, missing/extra/wrong-reference
refusals, ended fences, old-format contradiction rejection, and guards forbidding
Legacy history adapters on evaluate/availability. Full-history audit events remain
outside requests. No performance or complexity claim is made without measurement.

RF-RE4 must run real pinned RE against real SQLite, including retry/fingerprint,
authorization/handoff/OCC, illegal Delta/pre-value/RNG, injected rollback, no double
XP and existing evidence/outbox/queue/watermarks. Evidence without intermediate
stage facts remains Unresolved; final state is not event-time evidence. Active
combat cross-process resume and a new Evidence protocol are excluded.

## Executed validation and delivery

RF-RE1 baseline: `uv run --locked --package dnd5e-engine --extra dev pytest`
with `--no-cov -q` on `test_stateless_regression_baseline`, `attack_core`, `turn`,
`movement`, `spell_feature`, `concentration`, `shield` and `closure`: **244 passed**,
two existing Lark deprecations, 22.30 s pytest / 71.50 s including dev-environment
bootstrap. Initial sandbox interpreter execution was denied; the first elevated
`--no-sync` attempt found no pytest, so the locked dev extra was initialized.

GNU Make is unavailable on this host. Its checked-in command is executed directly:
`uv run --no-project python tools/validate.py fast --plan --base
38fcfcd4c3a744b30f792775415e798b323d4331` inspected the exact base and existing
untracked worktree. It conservatively requires Integration for that unknown path.
The final gate uses `integration --base` with the same SHA, including strict
MkDocs, without modifying or excluding that unrelated worktree. The log is
`%TEMP%/rf-re1-integration.log`. Local results and GitHub Actions results are
separate evidence. Later batches append final gate/commit/push outcomes here.

An independent read-only agent review found no blocking omission in the public
history dependency audit. Its two clarifications (movement intermediate results
and nested candidate/schema envelopes) are included above. This is an audit
review; it is not review or validation of the not-yet-implemented later batches.
