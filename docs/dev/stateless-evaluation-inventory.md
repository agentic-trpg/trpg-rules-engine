# Stateless evaluation: state inventory and regression baseline

Baseline: `64dd920507de29602908b4bea1635eedc4385370`. Architecture source:
ADR-001 Accepted (2026-10-09), Module Contracts sections 2/4/5/6/8,
State Machine Architecture sections 3/5/7, MVP Scope section 6, and Agent
Architecture combat-intent constraints, in the adjacent `agentic-trpg/docs` repository.
Accepted ownership semantics govern this migration. Candidate Python names, RNG
encoding, complex continuation and delta shapes remain local implementation choices;
this document does not freeze a cross-repository ABI.

## Ownership and lifetime

A = authoritative mechanical fact or retained rule evidence: preserve until its rule
expires, including identity, order and counters. D = derived: reconstruct from the
complete authoritative input and pinned rules, then verify consistency. E = ephemeral:
recreate per evaluation and never persist callbacks, queues or transaction objects.
The field index below includes every declared field, including nested sidecars.
Locations are representative producer/consumer anchors, not an exhaustive call graph;
attribute mutation includes container writes and model-copy replacement as well as
assignment. Search the exact field name to enumerate aliases before migrating a path.

| Domain | Producer / mutator | Consumers | Lifetime / recovery requirement |
| --- | --- | --- | --- |
| Actor HP/temp/death/attribution | start hydration; `_emit_apply_damage`, healing/temp folds, `_record_death` | context hydration, targeting, outcome | Entire encounter; restore actors and attribution together, never infer HP from proposed events |
| Action economy | turn start, `_consume_intent_budget`, action policy | funding/preflight, continuation | Current turn; preserve ordinary/extra/restricted grants and attack counters |
| Reaction | ready registration, reaction firing, expiry hooks | live reaction hooks, damage/attack/cast opportunities | Across turns; ordered declarations, effect identity and consumed budget all required |
| Effects/concentration | attachment/expiry folds, lifecycle link recording | passive projection, concentration saves, turn hooks | Until expiry; full identity, captured source metadata, child templates and condition lineage |
| Movement | hydration, step commits, forced movement, speed clamps | route preflight, range, opportunity attacks | Turn ledger plus durable position; remaining budget alone loses mode/dash/distance history |
| Limited uses | feature/item payment; monster recharge | preflight, hydration, monster planner | Rest/day/turn-specific; resource owner and max/spent semantics required |
| Areas/objects/timed activity | registration, event observers, relocation | movement/turn/visibility/delivery hooks | Captured magnitudes, geometry, ordered IDs, trigger history, source/lifetime and used-ID tombstones |
| Summons/transforms/constructs | conjuration folds and roster changes | allegiance, target lookup, expiry, turn handoff | Anchor identity, stashed original mechanics, owner, serials; cannot recover from current HP alone |
| Rule history | event folds, attack/rider observation, turn bookkeeping | Rage, damage-instance de-duplication, legendary windows | Relevant ordered evidence must survive; event log is not merely narration |
| RNG | seeded at start, all resolver draws on live.rng | shared d20/damage/check/save primitives | Explicit getstate/setstate with validated encoding; only SM commits successor |

## Transaction and transport boundaries

`_REGISTRY` owns legacy `_LiveCombat` instances. `CombatHandle` is only an opaque
lookup token, not a portable snapshot. `_ENDED` retains bounded ended instances.
`_execution_transaction` deep-copies all mechanical fields, restores the original RNG
on BaseException (including cancellation), buffers queue/listener publication, then
updates execution_serial. Normal typed refusals can publish legacy diagnostic events;
a legal miss or countered paid spell commits actual costs and RNG. Listener exceptions
after publication are post-commit errors and cannot undo external observer effects.

`event_log` contains rule evidence as well as narration; `event_queue` and
`event_listeners` are delivery objects. Stateless execution must build private queues
and never attach Bridge or Host subscribers. It must not call start/end registration
or `_keep_ended` accidentally. `_emit` performs mechanical folds; reuse it locally.
Bridge combat locks, mutation tasks and `(combat_id, request_id)` fingerprint receipts
are process-local HTTP coordination in `nat20_bridge/combat_execution.py` and `state.py`.
They are not SM atomic commit, durable receipts, or authority for a new evaluation.

## Snapshot dependency closure and first attack

| Input | Minimum full dependency | Migration policy |
| --- | --- | --- |
| Actor roster | Complete Combatant fields, stable IDs, sides, initiative, current turn/serial | Require full records; never select actor fields by intent |
| Shared character resources | Spell/Pact slots, known spells, custom counters, equipment/action sources | Explicit even when empty; preserve irrelevant resources unchanged |
| Scene | Grid geometry, walls/cover/light/obscurement/sunlight, actor positions | Rebuild topology from explicit scene; no retained runtime topology |
| Effects | Actor conditions, active effects, concentration and lifecycle/lineage state | Empty complex-state closure initially supported; nonempty unsupported before draw/payment |
| Hidden sidecars | Reaction, Help/Vex/Sap/Slow, movement, areas/objects, summons, delayed work, relevant history | Carry complete typed state or explicitly restrict input profile; never silently default omitted facts |
| Binding | Ruleset ID, evaluator revision, digest of all effective typed assets including overrides | Independent pre-execution binding error; base SRD label alone insufficient |
| RNG | Separate stream ID/version plus complete explicit PRNG state | Private restored RNG; no global random source |

First attack should reuse `PlayerIntent`, action-policy funding/gates,
`_resolve_intent_activities`, `build_activity_context`, `resolve_activity` and `_emit`
folds. Extract a context-taking internal dispatch seam only when required; do not
register a temporary combat and monkeypatch globals. Admission must reject unknown
weapon activities, riders, features, effect/reaction/environment paths before payment.
A restricted profile may represent complex components as typed known-empty values;
it must refuse nonempty input rather than discard it, and document that such snapshots
cannot represent general active combat yet. Full actor fields remain required.

Delta must cover HP, temp HP, alive/death-save/conditions, attribution, all changed
action/attack flags, damage-instance sequence and retained de-duplication evidence.
Turn advance and encounter ending are independent mechanics, not implicit conveniences:
either capture their entire transition or keep the first vertical slice turn-local.
Comparing changed state against the closed delta surface is a final fail-closed guard,
not a substitute for preflight.

## Behavior baseline and support matrix

`tests/evaluation_support.py` defines original synthetic rules/actors/scene, avoiding
private adventure data. `test_stateless_regression_baseline.py` checks hits, natural
1, natural 20, misses, temp-HP absorption, costs, deterministic events/RNG, rejection,
and post-resolution exception/cancellation rollback. Existing execution-integrity,
reaction-queue and typed PC/effect suites supply representative real effect/reaction
regressions; these remain Legacy capabilities, not evidence of stateless support.

| Capability | Batch 1 status |
| --- | --- |
| Legacy attacks, effects, reactions, Bridge | Unchanged; regression baseline only |
| Portable typed snapshots/RNG/deltas | Not implemented until Batch 2 |
| Stateless PC attack evaluation | Not implemented until Batch 3 |
| Explicit NPC evaluation / availability | Not implemented until Batch 4 |
| SQLite commit, durable receipt, event outbox, full continuation | State Machine / future work |

## Complete field index

Batch 1 validation (Windows, CPython 3.14.7): planner inspected with exact
`BASE=64dd920507de29602908b4bea1635eedc4385370`; Full passed in 691.47s.
Because GNU Make is absent locally, a temporary adapter read and executed the
checked-in package Makefile `check` recipes and their dependencies without changing
them. Tooling: 24 tests; data: 631 passed / 41 skipped (97.56% coverage);
engine: 6,879 passed (95.70%); Bridge: 139 passed (97.05%); demo: 53 passed.
Ruff, formatting, mypy, Bandit, examples, strict MkDocs, isolated three-wheel and
real Bridge HTTP smoke all passed. The 41 data skips depend on absent maintainer
Foundry source packs or optional oracle inputs; they are not executed passes.
The original checkout's normalization-only status entries were preserved in place;
development and validation ran in a separate clean worktree. No runtime code changed.

Declaration locations pin the source of each field. A fields must be serialized or
excluded by an explicit unsupported input profile. D fields need their stated source
and deterministic rebuilding. E fields are recreated and never cross the boundary.

### `_CharacterDamageInstance` — `orchestrator.py`

This follow-up scratch record exists only between typed damage emission and the
shared instance-completion callback. Both fields are E: `hp_before` captures the
pre-hit tracked HP; `event` retains the first DamageApplied for source/critical-hit
metadata. `_emit_apply_damage` creates it; `_complete_character_damage_instance`
consumes it; departure clears it. No RNG, loader lookup or retained state is added.

### `_LiveCombat` — `orchestrator.py`

| Field | Class | Declaration / recovery source |
| --- | --- | --- |
| `handle_id` | E | `orchestrator.py:3117`; private per-call scratch or delivery object |
| `session_id` | A | `orchestrator.py:3118`; explicit retained field; initialization and rule folds own writes |
| `initiative` | A | `orchestrator.py:3119`; explicit retained field; initialization and rule folds own writes |
| `party_ids` | A | `orchestrator.py:3120`; explicit retained field; initialization and rule folds own writes |
| `encounter_ids` | A | `orchestrator.py:3121`; explicit retained field; initialization and rule folds own writes |
| `topology` | D | `orchestrator.py:3122`; rebuild from actor/scene/rules/ordered retained evidence |
| `rng` | A | `orchestrator.py:3123`; explicit retained field; initialization and rule folds own writes |
| `event_queue` | E | `orchestrator.py:3124`; private per-call scratch or delivery object |
| `scene_location_id` | A | `orchestrator.py:3125`; explicit retained field; initialization and rule folds own writes |
| `ruleset_loader` | D | `orchestrator.py:3128`; rebuild from actor/scene/rules/ordered retained evidence |
| `execution_serial` | D | `orchestrator.py:3129`; rebuild from actor/scene/rules/ordered retained evidence |
| `transaction_active` | E | `orchestrator.py:3130`; private per-call scratch or delivery object |
| `scene_sunlight` | A | `orchestrator.py:3135`; explicit retained field; initialization and rule folds own writes |
| `current_turn_index` | A | `orchestrator.py:3136`; explicit retained field; initialization and rule folds own writes |
| `round_number` | A | `orchestrator.py:3137`; explicit retained field; initialization and rule folds own writes |
| `turn_serial` | A | `orchestrator.py:3138`; explicit retained field; initialization and rule folds own writes |
| `timed_activities` | A | `orchestrator.py:3139`; explicit retained field; initialization and rule folds own writes |
| `persistent_areas` | A | `orchestrator.py:3140`; explicit retained field; initialization and rule folds own writes |
| `combat_objects` | A | `orchestrator.py:3141`; explicit retained field; initialization and rule folds own writes |
| `ended` | A | `orchestrator.py:3142`; explicit retained field; initialization and rule folds own writes |
| `final_outcome` | D | `orchestrator.py:3143`; rebuild from actor/scene/rules/ordered retained evidence |
| `actor_zone` | A | `orchestrator.py:3145`; explicit retained field; initialization and rule folds own writes |
| `movement_ledgers` | A | `orchestrator.py:3146`; explicit retained field; initialization and rule folds own writes |
| `opportunity_attack_weapons` | A | `orchestrator.py:3155`; explicit retained field; initialization and rule folds own writes |
| `monster_slug_by_entity` | A | `orchestrator.py:3158`; explicit retained field; initialization and rule folds own writes |
| `xp_value_by_entity` | A | `orchestrator.py:3161`; Host EncounterMemberSpec overrides require explicit capture; outcome/XP consumer |
| `event_log` | A | `orchestrator.py:3168`; explicit retained field; initialization and rule folds own writes |
| `tracked_hp` | D | `orchestrator.py:3169`; rebuild from actor/scene/rules/ordered retained evidence |
| `tracked_temp_hp` | D | `orchestrator.py:3170`; rebuild from actor/scene/rules/ordered retained evidence |
| `undead_fortitude_holds` | E | `orchestrator.py:3180`; private per-call scratch or delivery object |
| `active_conditions` | D | `orchestrator.py:3182`; rebuild from actor/scene/rules/ordered retained evidence |
| `active_effects` | A | `orchestrator.py:3187`; explicit retained field; initialization and rule folds own writes |
| `deaths_recorded` | A | `orchestrator.py:3189`; explicit retained field; initialization and rule folds own writes |
| `dead_ids` | A | `orchestrator.py:3190`; explicit retained field; initialization and rule folds own writes |
| `expended_resources` | A | `orchestrator.py:3193`; explicit retained field; initialization and rule folds own writes |
| `current_actor_id` | D | `orchestrator.py:3196`; rebuild from actor/scene/rules/ordered retained evidence |
| `spell_slots_by_entity` | A | `orchestrator.py:3202`; explicit retained field; initialization and rule folds own writes |
| `pact_slots_by_entity` | A | `orchestrator.py:3206`; explicit retained field; initialization and rule folds own writes |
| `spells_known_by_entity` | A | `orchestrator.py:3207`; explicit retained field; initialization and rule folds own writes |
| `custom_counters_by_entity` | A | `orchestrator.py:3208`; explicit retained field; initialization and rule folds own writes |
| `concentration_chain` | A | `orchestrator.py:3218`; explicit retained field; initialization and rule folds own writes |
| `concentration_rounds_remaining` | A | `orchestrator.py:3228`; explicit retained field; initialization and rule folds own writes |
| `conditions_by_effect` | A | `orchestrator.py:3235`; explicit retained field; initialization and rule folds own writes |
| `effect_lifecycles` | A | `orchestrator.py:3237`; explicit retained field; initialization and rule folds own writes |
| `lifecycle_damage` | E | `orchestrator.py:3238`; private per-call scratch or delivery object |
| `character_damage_instances` | E | `orchestrator.py:3247`; captures pre-hit HP and first typed damage event until the shared whole-instance completion callback; never serialized |
| `event_listeners` | E | `orchestrator.py:3244`; private per-call scratch or delivery object |
| `pending_reactions` | A | `orchestrator.py:3248`; explicit retained field; initialization and rule folds own writes |
| `active_reaction_responses` | A | `orchestrator.py:3249`; explicit retained field; initialization and rule folds own writes |
| `reaction_resolution_depth` | E | `orchestrator.py:3250`; private per-call scratch or delivery object |
| `damage_instance_sequence` | A | `orchestrator.py:3251`; explicit retained field; initialization and rule folds own writes |
| `rider_uses` | A | `orchestrator.py:3252`; explicit retained field; initialization and rule folds own writes |
| `processed_zero_hp_damage_instances` | A | `orchestrator.py:3253`; explicit retained field; initialization and rule folds own writes |
| `reaction_effects_pending_expiry` | A | `orchestrator.py:3262`; explicit retained field; initialization and rule folds own writes |
| `help_grants` | A | `orchestrator.py:3279`; explicit retained field; initialization and rule folds own writes |
| `help_check_grants` | A | `orchestrator.py:3280`; explicit retained field; initialization and rule folds own writes |
| `hidden_entities` | A | `orchestrator.py:3292`; explicit retained field; initialization and rule folds own writes |
| `vex_grants` | A | `orchestrator.py:3310`; explicit retained field; initialization and rule folds own writes |
| `rage_bonus_extensions` | A | `orchestrator.py:3316`; explicit retained field; initialization and rule folds own writes |
| `sap_marks` | A | `orchestrator.py:3328`; explicit retained field; initialization and rule folds own writes |
| `slow_marks` | A | `orchestrator.py:3343`; explicit retained field; initialization and rule folds own writes |
| `monster_action_uses_by_entity` | A | `orchestrator.py:3349`; explicit retained field; initialization and rule folds own writes |
| `monster_turn_start_done` | A | `orchestrator.py:3359`; explicit retained field; initialization and rule folds own writes |
| `last_ended_turn` | A | `orchestrator.py:3365`; explicit retained field; initialization and rule folds own writes |
| `departed_actor_id` | A | `orchestrator.py:3371`; explicit retained field; initialization and rule folds own writes |
| `legendary_windows_used` | A | `orchestrator.py:3378`; explicit retained field; initialization and rule folds own writes |
| `legendary_resistance_armed` | A | `orchestrator.py:3391`; explicit retained field; initialization and rule folds own writes |
| `legendary_resistance_applied_event_indices` | A | `orchestrator.py:3404`; explicit retained field; initialization and rule folds own writes |
| `lifecycle` | D | `orchestrator.py:3410`; rebuild from actor/scene/rules/ordered retained evidence |
| `constructs` | A | `orchestrator.py:3414`; explicit retained field; initialization and rule folds own writes |
| `transforms` | A | `orchestrator.py:3417`; explicit retained field; initialization and rule folds own writes |
| `summons` | A | `orchestrator.py:3422`; explicit retained field; initialization and rule folds own writes |
| `summon_counts` | A | `orchestrator.py:3425`; explicit retained field; initialization and rule folds own writes |

### `_Construct` — `orchestrator.py`

| Field | Class | Declaration / recovery source |
| --- | --- | --- |
| `construct_id` | A | `orchestrator.py:3435`; explicit retained field; initialization and rule folds own writes |
| `owner_id` | A | `orchestrator.py:3436`; explicit retained field; initialization and rule folds own writes |
| `spell_id` | A | `orchestrator.py:3437`; explicit retained field; initialization and rule folds own writes |
| `cell` | A | `orchestrator.py:3438`; explicit retained field; initialization and rule folds own writes |
| `slot_level` | A | `orchestrator.py:3439`; explicit retained field; initialization and rule folds own writes |
| `anchor` | A | `orchestrator.py:3440`; explicit retained field; initialization and rule folds own writes |
| `cast_round` | A | `orchestrator.py:3441`; explicit retained field; initialization and rule folds own writes |

### `_Transform` — `orchestrator.py`

| Field | Class | Declaration / recovery source |
| --- | --- | --- |
| `entity_id` | A | `orchestrator.py:3452`; explicit retained field; initialization and rule folds own writes |
| `form_slug` | A | `orchestrator.py:3453`; explicit retained field; initialization and rule folds own writes |
| `source` | A | `orchestrator.py:3454`; explicit retained field; initialization and rule folds own writes |
| `effect_id` | A | `orchestrator.py:3455`; explicit retained field; initialization and rule folds own writes |
| `origin` | A | `orchestrator.py:3456`; explicit retained field; initialization and rule folds own writes |
| `stash` | A | `orchestrator.py:3457`; explicit retained field; initialization and rule folds own writes |
| `original_monster_slug` | A | `orchestrator.py:3458`; explicit retained field; initialization and rule folds own writes |
| `original_action_uses` | A | `orchestrator.py:3459`; explicit retained field; initialization and rule folds own writes |
| `form_proficiency_bonus` | A | `orchestrator.py:3460`; explicit retained field; initialization and rule folds own writes |
| `attacks_per_action` | A | `orchestrator.py:3461`; explicit retained field; initialization and rule folds own writes |
| `clears_temp_hp_on_end` | A | `orchestrator.py:3462`; explicit retained field; initialization and rule folds own writes |

### `_Summon` — `orchestrator.py`

| Field | Class | Declaration / recovery source |
| --- | --- | --- |
| `entity_id` | A | `orchestrator.py:3475`; explicit retained field; initialization and rule folds own writes |
| `owner_id` | A | `orchestrator.py:3476`; explicit retained field; initialization and rule folds own writes |
| `spell_id` | A | `orchestrator.py:3477`; explicit retained field; initialization and rule folds own writes |
| `stat_block_slug` | A | `orchestrator.py:3478`; explicit retained field; initialization and rule folds own writes |
| `slot_level` | A | `orchestrator.py:3479`; explicit retained field; initialization and rule folds own writes |
| `anchor` | A | `orchestrator.py:3480`; explicit retained field; initialization and rule folds own writes |
| `magnitudes` | A | `orchestrator.py:3481`; explicit retained field; initialization and rule folds own writes |
| `attacks_per_action` | A | `orchestrator.py:3482`; explicit retained field; initialization and rule folds own writes |

### `MonsterActionUses` — `types/combat.py`

| Field | Class | Declaration / recovery source |
| --- | --- | --- |
| `recharge_spent` | A | `types/combat.py:53`; explicit retained field; initialization and rule folds own writes |
| `uses_remaining` | A | `types/combat.py:54`; explicit retained field; initialization and rule folds own writes |
| `action_uses_remaining` | A | `types/combat.py:55`; explicit retained field; initialization and rule folds own writes |

### `Combatant` — `types/combat.py`

| Field | Class | Declaration / recovery source |
| --- | --- | --- |
| `entity_id` | A | `types/combat.py:59`; explicit retained field; initialization and rule folds own writes |
| `entity_type` | A | `types/combat.py:60`; explicit retained field; initialization and rule folds own writes |
| `name` | A | `types/combat.py:61`; explicit retained field; initialization and rule folds own writes |
| `initiative` | A | `types/combat.py:62`; explicit retained field; initialization and rule folds own writes |
| `hp_current` | A | `types/combat.py:63`; explicit retained field; initialization and rule folds own writes |
| `temp_hp` | A | `types/combat.py:65`; explicit retained field; initialization and rule folds own writes |
| `is_alive` | A | `types/combat.py:66`; explicit retained field; initialization and rule folds own writes |
| `conditions` | A | `types/combat.py:67`; explicit retained field; initialization and rule folds own writes |
| `hp_max` | A | `types/combat.py:69`; explicit retained field; initialization and rule folds own writes |
| `ac` | A | `types/combat.py:70`; explicit retained field; initialization and rule folds own writes |
| `attack_bonus` | A | `types/combat.py:83`; explicit retained field; initialization and rule folds own writes |
| `attack_rolls_made_this_turn` | A | `types/combat.py:84`; explicit retained field; initialization and rule folds own writes |
| `damage_dice` | A | `types/combat.py:85`; explicit retained field; initialization and rule folds own writes |
| `damage_type` | A | `types/combat.py:86`; explicit retained field; initialization and rule folds own writes |
| `behavior_profile` | A | `types/combat.py:87`; explicit retained field; initialization and rule folds own writes |
| `strength` | A | `types/combat.py:88`; explicit retained field; initialization and rule folds own writes |
| `dexterity` | A | `types/combat.py:89`; explicit retained field; initialization and rule folds own writes |
| `constitution` | A | `types/combat.py:90`; explicit retained field; initialization and rule folds own writes |
| `intelligence` | A | `types/combat.py:91`; explicit retained field; initialization and rule folds own writes |
| `wisdom` | A | `types/combat.py:92`; explicit retained field; initialization and rule folds own writes |
| `charisma` | A | `types/combat.py:93`; explicit retained field; initialization and rule folds own writes |
| `proficiency_bonus_override` | A | `types/combat.py:96`; explicit retained field; initialization and rule folds own writes |
| `save_proficiencies` | A | `types/combat.py:97`; explicit retained field; initialization and rule folds own writes |
| `skill_proficiencies` | A | `types/combat.py:98`; explicit retained field; initialization and rule folds own writes |
| `skill_expertise` | A | `types/combat.py:99`; explicit retained field; initialization and rule folds own writes |
| `skill_check_bonuses` | A | `types/combat.py:100`; explicit retained field; initialization and rule folds own writes |
| `jack_of_all_trades` | A | `types/combat.py:101`; explicit retained field; initialization and rule folds own writes |
| `reliable_talent` | A | `types/combat.py:102`; explicit retained field; initialization and rule folds own writes |
| `stealth_disadvantage` | A | `types/combat.py:103`; explicit retained field; initialization and rule folds own writes |
| `tool_proficiencies` | A | `types/combat.py:104`; explicit retained field; initialization and rule folds own writes |
| `weapon_proficiencies` | A | `types/combat.py:116`; explicit retained field; initialization and rule folds own writes |
| `death_saves` | A | `types/combat.py:117`; explicit retained field; initialization and rule folds own writes |
| `creature_type` | A | `types/combat.py:124`; explicit retained field; initialization and rule folds own writes |
| `creature_size` | A | `types/combat.py:125`; explicit retained field; initialization and rule folds own writes |
| `damage_resistances` | A | `types/combat.py:135`; explicit retained field; initialization and rule folds own writes |
| `damage_immunities` | A | `types/combat.py:136`; explicit retained field; initialization and rule folds own writes |
| `damage_vulnerabilities` | A | `types/combat.py:143`; explicit retained field; initialization and rule folds own writes |
| `condition_immunities` | A | `types/combat.py:154`; explicit retained field; initialization and rule folds own writes |
| `senses` | A | `types/combat.py:160`; explicit retained field; initialization and rule folds own writes |
| `concentration_effect_id` | A | `types/combat.py:165`; explicit retained field; initialization and rule folds own writes |
| `character_level` | A | `types/combat.py:170`; explicit retained field; initialization and rule folds own writes |
| `action_available` | A | `types/combat.py:175`; explicit retained field; initialization and rule folds own writes |
| `bonus_action_available` | A | `types/combat.py:176`; explicit retained field; initialization and rule folds own writes |
| `reaction_available` | A | `types/combat.py:177`; explicit retained field; initialization and rule folds own writes |
| `action_taken_this_turn` | A | `types/combat.py:178`; explicit retained field; initialization and rule folds own writes |
| `bonus_action_taken_this_turn` | A | `types/combat.py:179`; explicit retained field; initialization and rule folds own writes |
| `attack_action_attacks_made` | A | `types/combat.py:180`; explicit retained field; initialization and rule folds own writes |
| `action_grants_spent` | A | `types/combat.py:181`; explicit retained field; initialization and rule folds own writes |
| `action_grant_groups_spent` | A | `types/combat.py:182`; explicit retained field; initialization and rule folds own writes |
| `base_speed` | A | `types/combat.py:189`; explicit retained field; initialization and rule folds own writes |
| `movement_remaining` | A | `types/combat.py:190`; explicit retained field; initialization and rule folds own writes |
| `movement_modes` | A | `types/combat.py:197`; explicit retained field; initialization and rule folds own writes |
| `melee_reach_ft` | A | `types/combat.py:206`; explicit retained field; initialization and rule folds own writes |
| `granted_features` | A | `types/combat.py:214`; explicit retained field; initialization and rule folds own writes |
| `class_slug` | A | `types/combat.py:215`; explicit retained field; initialization and rule folds own writes |
| `classes` | A | `types/combat.py:218`; explicit retained field; initialization and rule folds own writes |
| `fighting_styles` | A | `types/combat.py:221`; explicit retained field; initialization and rule folds own writes |
| `carried_item_slugs` | A | `types/combat.py:224`; explicit retained field; initialization and rule folds own writes |
| `worn_armor` | A | `types/combat.py:227`; explicit retained field; initialization and rule folds own writes |
| `shield_equipped` | A | `types/combat.py:228`; explicit retained field; initialization and rule folds own writes |
| `subclass_slug` | A | `types/combat.py:233`; explicit retained field; initialization and rule folds own writes |
| `species_slug` | A | `types/combat.py:240`; explicit retained field; initialization and rule folds own writes |
| `last_damaged_by` | A | `types/combat.py:248`; explicit retained field; initialization and rule folds own writes |
| `disengaging_this_turn` | A | `types/combat.py:255`; explicit retained field; initialization and rule folds own writes |
| `sneak_attack_spent_this_turn` | A | `types/combat.py:263`; explicit retained field; initialization and rule folds own writes |
| `attacks_remaining` | A | `types/combat.py:271`; explicit retained field; initialization and rule folds own writes |
| `attack_action_engaged` | A | `types/combat.py:277`; explicit retained field; initialization and rule folds own writes |
| `light_weapon_swing_slug` | A | `types/combat.py:283`; explicit retained field; initialization and rule folds own writes |
| `restricted_light_weapon_swing_slug` | A | `types/combat.py:284`; explicit retained field; initialization and rule folds own writes |
| `offhand_attack_spent` | A | `types/combat.py:289`; explicit retained field; initialization and rule folds own writes |
| `dodging` | A | `types/combat.py:299`; explicit retained field; initialization and rule folds own writes |
| `trait_mechanics` | A | `types/combat.py:305`; explicit retained field; initialization and rule folds own writes |
| `physical_resistances_nonmagical_only` | A | `types/combat.py:315`; explicit retained field; initialization and rule folds own writes |
| `legendary_actions_max` | A | `types/combat.py:323`; explicit retained field; initialization and rule folds own writes |
| `legendary_actions_remaining` | A | `types/combat.py:324`; explicit retained field; initialization and rule folds own writes |
| `legendary_resistances_max` | A | `types/combat.py:331`; explicit retained field; initialization and rule folds own writes |
| `legendary_resistances_remaining` | A | `types/combat.py:332`; explicit retained field; initialization and rule folds own writes |
| `has_fled` | A | `types/combat.py:336`; explicit retained field; initialization and rule folds own writes |
| `spellcasting_ability` | A | `types/combat.py:341`; explicit retained field; initialization and rule folds own writes |
| `loading_weapon_fired_this_action` | A | `types/combat.py:348`; explicit retained field; initialization and rule folds own writes |
| `cleave_spent_this_turn` | A | `types/combat.py:355`; explicit retained field; initialization and rule folds own writes |
| `flurry_strikes_remaining` | A | `types/combat.py:361`; explicit retained field; initialization and rule folds own writes |
| `extra_actions_remaining` | A | `types/combat.py:366`; explicit retained field; initialization and rule folds own writes |
| `action_surge_used_this_turn` | A | `types/combat.py:367`; explicit retained field; initialization and rule folds own writes |

### `MovementLedger` — `movement.py`

| Field | Class | Declaration / recovery source |
| --- | --- | --- |
| `spent_ft` | A | `movement.py:29`; explicit retained field; initialization and rule folds own writes |
| `distance_ft` | A | `movement.py:30`; explicit retained field; initialization and rule folds own writes |
| `active_mode` | A | `movement.py:31`; explicit retained field; initialization and rule folds own writes |
| `dash_count` | A | `movement.py:32`; explicit retained field; initialization and rule folds own writes |

### `PendingReaction` — `reactions.py`

| Field | Class | Declaration / recovery source |
| --- | --- | --- |
| `owner_id` | A | `reactions.py:38`; explicit retained field; initialization and rule folds own writes |
| `spell_id` | A | `reactions.py:39`; explicit retained field; initialization and rule folds own writes |
| `activity_id` | A | `reactions.py:40`; explicit retained field; initialization and rule folds own writes |
| `slot_level` | A | `reactions.py:41`; explicit retained field; initialization and rule folds own writes |
| `conditions` | A | `reactions.py:42`; explicit retained field; initialization and rule folds own writes |
| `semantics` | A | `reactions.py:43`; explicit retained field; initialization and rule folds own writes |
| `source_kind` | A | `reactions.py:44`; explicit retained field; initialization and rule folds own writes |

### `ActiveReactionResponse` — `reactions.py`

| Field | Class | Declaration / recovery source |
| --- | --- | --- |
| `owner_id` | A | `reactions.py:69`; explicit retained field; initialization and rule folds own writes |
| `effect_identity` | A | `reactions.py:70`; explicit retained field; initialization and rule folds own writes |
| `conditions` | A | `reactions.py:71`; explicit retained field; initialization and rule folds own writes |
| `responses` | A | `reactions.py:72`; explicit retained field; initialization and rule folds own writes |

### `OngoingEffectLifecycle` — `effect_lifecycle.py`

| Field | Class | Declaration / recovery source |
| --- | --- | --- |
| `identity` | A | `effect_lifecycle.py:49`; explicit retained field; initialization and rule folds own writes |
| `application` | A | `effect_lifecycle.py:50`; explicit retained field; initialization and rule folds own writes |
| `applied_turn_serial` | A | `effect_lifecycle.py:51`; explicit retained field; initialization and rule folds own writes |
| `applied_round` | A | `effect_lifecycle.py:52`; explicit retained field; initialization and rule folds own writes |
| `expires_round` | A | `effect_lifecycle.py:53`; explicit retained field; initialization and rule folds own writes |
| `expiry_actor_id` | A | `effect_lifecycle.py:54`; explicit retained field; initialization and rule folds own writes |
| `last_repeat_turn_serial` | A | `effect_lifecycle.py:55`; explicit retained field; initialization and rule folds own writes |
| `remaining_one_use_modifiers` | A | `effect_lifecycle.py:56`; explicit retained field; initialization and rule folds own writes |

### `PendingTimedActivity` — `timed_activities.py`

| Field | Class | Declaration / recovery source |
| --- | --- | --- |
| `sequence` | A | `timed_activities.py:46`; explicit retained field; initialization and rule folds own writes |
| `spell` | A | `timed_activities.py:47`; explicit retained field; initialization and rule folds own writes |
| `activity` | A | `timed_activities.py:48`; explicit retained field; initialization and rule folds own writes |
| `caster` | A | `timed_activities.py:49`; explicit retained field; initialization and rule folds own writes |
| `target_id` | A | `timed_activities.py:50`; explicit retained field; initialization and rule folds own writes |
| `slot_level` | A | `timed_activities.py:51`; explicit retained field; initialization and rule folds own writes |
| `spellcasting_ability` | A | `timed_activities.py:52`; explicit retained field; initialization and rule folds own writes |
| `save_dc_override` | A | `timed_activities.py:53`; explicit retained field; initialization and rule folds own writes |
| `timing` | A | `timed_activities.py:54`; explicit retained field; initialization and rule folds own writes |
| `not_before_turn` | A | `timed_activities.py:55`; explicit retained field; initialization and rule folds own writes |
| `effect_identity` | A | `timed_activities.py:56`; explicit retained field; initialization and rule folds own writes |
| `duration` | A | `timed_activities.py:57`; explicit retained field; initialization and rule folds own writes |
| `concentration` | A | `timed_activities.py:58`; explicit retained field; initialization and rule folds own writes |
| `concentration_identity` | A | `timed_activities.py:59`; explicit retained field; initialization and rule folds own writes |
| `activity_source_id` | A | `timed_activities.py:60`; explicit retained field; initialization and rule folds own writes |
| `source_parent_id` | A | `timed_activities.py:61`; explicit retained field; initialization and rule folds own writes |
| `attack_bonus_override` | A | `timed_activities.py:62`; explicit retained field; initialization and rule folds own writes |

### `TimedActivityState` — `timed_activities.py`

| Field | Class | Declaration / recovery source |
| --- | --- | --- |
| `pending` | A | `timed_activities.py:69`; explicit retained field; initialization and rule folds own writes |
| `next_sequence` | A | `timed_activities.py:70`; explicit retained field; initialization and rule folds own writes |

### `StationaryArea` — `persistent_areas.py`

| Field | Class | Declaration / recovery source |
| --- | --- | --- |
| `origin` | A | `persistent_areas.py:49`; explicit retained field; initialization and rule folds own writes |

### `FollowSourceEmanation` — `persistent_areas.py`

| Field | Class | Declaration / recovery source |
| --- | --- | --- |
| `source_entity_id` | A | `persistent_areas.py:54`; explicit retained field; initialization and rule folds own writes |

### `FollowObjectEmanation` — `persistent_areas.py`

| Field | Class | Declaration / recovery source |
| --- | --- | --- |
| `object_id` | A | `persistent_areas.py:59`; explicit retained field; initialization and rule folds own writes |
| `includes_origin_object` | A | `persistent_areas.py:60`; explicit retained field; initialization and rule folds own writes |

### `PersistentArea` — `persistent_areas.py`

| Field | Class | Declaration / recovery source |
| --- | --- | --- |
| `id` | A | `persistent_areas.py:65`; explicit retained field; initialization and rule folds own writes |
| `sequence` | A | `persistent_areas.py:66`; explicit retained field; initialization and rule folds own writes |
| `source_id` | A | `persistent_areas.py:67`; explicit retained field; initialization and rule folds own writes |
| `source_kind` | A | `persistent_areas.py:68`; explicit retained field; initialization and rule folds own writes |
| `caster` | A | `persistent_areas.py:69`; explicit retained field; initialization and rule folds own writes |
| `activity` | A | `persistent_areas.py:70`; explicit retained field; initialization and rule folds own writes |
| `spec` | A | `persistent_areas.py:71`; explicit retained field; initialization and rule folds own writes |
| `geometry` | A | `persistent_areas.py:72`; explicit retained field; initialization and rule folds own writes |
| `template` | A | `persistent_areas.py:73`; explicit retained field; initialization and rule folds own writes |
| `cast_origin` | A | `persistent_areas.py:74`; explicit retained field; initialization and rule folds own writes |
| `excluded_ids` | A | `persistent_areas.py:75`; explicit retained field; initialization and rule folds own writes |
| `slot_level` | A | `persistent_areas.py:76`; explicit retained field; initialization and rule folds own writes |
| `base_spell_level` | A | `persistent_areas.py:77`; explicit retained field; initialization and rule folds own writes |
| `spellcasting_ability` | A | `persistent_areas.py:78`; explicit retained field; initialization and rule folds own writes |
| `save_dc` | A | `persistent_areas.py:79`; explicit retained field; initialization and rule folds own writes |
| `passive_effects` | A | `persistent_areas.py:80`; explicit retained field; initialization and rule folds own writes |
| `duration` | A | `persistent_areas.py:81`; explicit retained field; initialization and rule folds own writes |
| `rounds_remaining` | A | `persistent_areas.py:82`; explicit retained field; initialization and rule folds own writes |
| `concentration_identity` | A | `persistent_areas.py:83`; explicit retained field; initialization and rule folds own writes |
| `not_before_turn` | A | `persistent_areas.py:84`; explicit retained field; initialization and rule folds own writes |
| `activity_source_id` | A | `persistent_areas.py:85`; explicit retained field; initialization and rule folds own writes |
| `source_parent_id` | A | `persistent_areas.py:86`; explicit retained field; initialization and rule folds own writes |
| `environment_expires_round` | A | `persistent_areas.py:87`; explicit retained field; initialization and rule folds own writes |
| `last_trigger_turn` | A | `persistent_areas.py:88`; explicit retained field; initialization and rule folds own writes |
| `shape_locked_ids` | A | `persistent_areas.py:89`; explicit retained field; initialization and rule folds own writes |
| `appearance_pending` | A | `persistent_areas.py:90`; explicit retained field; initialization and rule folds own writes |

### `PersistentAreaState` — `persistent_areas.py`

| Field | Class | Declaration / recovery source |
| --- | --- | --- |
| `areas` | A | `persistent_areas.py:127`; explicit retained field; initialization and rule folds own writes |
| `next_sequence` | A | `persistent_areas.py:128`; explicit retained field; initialization and rule folds own writes |
| `next_turn_start_effects` | A | `persistent_areas.py:131`; explicit retained field; initialization and rule folds own writes |

### `CombatObjectState` — `combat_objects.py`

| Field | Class | Declaration / recovery source |
| --- | --- | --- |
| `objects` | A | `combat_objects.py:31`; explicit retained field; initialization and rule folds own writes |
| `used_ids` | A | `combat_objects.py:33`; explicit retained field; initialization and rule folds own writes |

## Per-field access anchors

This source scan distinguishes direct/container mutation from attribute reads. Each
row lists representative actual call sites in addition to initialization at the
declaration above. Model-copy dictionary updates, hydration constructors and aliases
must also be followed through the domain mutators listed earlier. A dash means no
direct attribute access was found; it does not prove a field is unused. Fields sharing
a name can show a same-named DTO consumer; verify the receiver at the linked location.

| Field | Mutation anchors | Consumer anchors |
| --- | --- | --- |
| `handle_id` | - | `orchestrator.py:12768 (_advance_monster_turn)`; `orchestrator.py:13092 (_project_outcome)`; `orchestrator.py:13156 (_keep_ended)` |
| `session_id` | - | - |
| `initiative` | `orchestrator.py:10539 (_consume_action_budget)`; `orchestrator.py:10660 (_consume_attack_budget)`; `orchestrator.py:10678 (_consume_attack_budget)` | `build_party.py:36 (build_party_member)`; `combat_objects.py:150 (_holder_valid)`; `live_checks.py:52 (project_check_states)` |
| `party_ids` | - | `orchestrator.py:1164 (_side_of)`; `orchestrator.py:1165 (_side_of)`; `orchestrator.py:12464 (_opportunity_attackers)` |
| `encounter_ids` | - | `orchestrator.py:1166 (_side_of)`; `orchestrator.py:1167 (_side_of)`; `orchestrator.py:12464 (_opportunity_attackers)` |
| `topology` | `environment.py:95 (refresh_environment)` | `combat_objects.py:140 (_position_valid)`; `environment.py:103 (reconcile_environment)`; `environment.py:152 (apply_strong_wind)` |
| `rng` | - | `action_policy.py:291 (fail_somatic_attempt)`; `activities/attack.py:1002 (_resolve_flat_bonus)`; `activities/attack.py:1202 (_apply_on_hit_damage)` |
| `event_queue` | `orchestrator.py:11773 (_execution_transaction)`; `orchestrator.py:11785 (_execution_transaction)` | `orchestrator.py:11759 (_execution_transaction)`; `orchestrator.py:12995 (drain_pending_events)`; `orchestrator.py:13001 (drain_pending_events)` |
| `scene_location_id` | - | `orchestrator.py:5597 (_record_death)` |
| `ruleset_loader` | - | `ongoing_spell_activation.py:69 (activate_spell)`; `orchestrator.py:11769 (_execution_transaction)`; `orchestrator.py:11777 (_execution_transaction)` |
| `execution_serial` | `orchestrator.py:11798 (_execution_transaction)` | `views.py:176 (from_live)` |
| `transaction_active` | `orchestrator.py:11775 (_execution_transaction)`; `orchestrator.py:11787 (_execution_transaction)` | `orchestrator.py:11756 (_execution_transaction)` |
| `scene_sunlight` | - | `live_checks.py:50 (project_check_states)` |
| `current_turn_index` | `orchestrator.py:8760 (_open_turn_at_current_index)`; `orchestrator.py:8803 (_end_turn_and_advance)`; `orchestrator.py:8885 (_insert_into_roster)` | `orchestrator.py:3579 (_current_actor)`; `orchestrator.py:8758 (_open_turn_at_current_index)`; `orchestrator.py:8884 (_insert_into_roster)` |
| `round_number` | `orchestrator.py:8761 (_open_turn_at_current_index)` | `environment.py:179 (expire_environment_round)`; `live_effect_lifecycle.py:236 (expire_at_boundary)`; `live_effect_lifecycle.py:40 (register_effect)` |
| `turn_serial` | `orchestrator.py:4929 (_emit_apply_turn_started)` | `activities/attack.py:1130 (_attack_rider_plan)`; `activities/attack.py:463 (_prepare_attack_roll)`; `live_attack_riders.py:154 (preflight_attack_riders)` |
| `timed_activities` | `timed_activities.py:290 (run_timed_activities)`; `timed_activities.py:293 (run_timed_activities)`; `timed_activities.py:367 (_execute)` | `orchestrator.py:4866 (_emit)`; `orchestrator.py:5588 (_record_death)`; `orchestrator.py:7132 (_expire_timed_effects_at_turn_end)` |
| `persistent_areas` | `persistent_areas.py:584 (_execute)` | `combat_objects.py:44 (views)`; `combat_objects.py:61 (remove)`; `combat_objects.py:66 (remove)` |
| `combat_objects` | `combat_objects.py:190 (register_combat_objects)`; `combat_objects.py:191 (register_combat_objects)`; `combat_objects.py:252 (mutate_combat_object)` | `combat_objects.py:175 (register_combat_objects)`; `combat_objects.py:210 (mutate_combat_object)`; `environment.py:51 (refresh_environment)` |
| `ended` | `orchestrator.py:13139 (_end_combat)` | `combat_objects.py:167 (register_combat_objects)`; `combat_objects.py:207 (mutate_combat_object)`; `environment.py:148 (apply_strong_wind)` |
| `final_outcome` | `orchestrator.py:13140 (_end_combat)` | `orchestrator.py:11799 (_execution_transaction)`; `orchestrator.py:13106 (end_combat)`; `orchestrator.py:13125 (_end_combat)` |
| `actor_zone` | `live_movement.py:261 (position_step)`; `orchestrator.py:8886 (_insert_into_roster)` | `combat_objects.py:152 (_holder_valid)`; `combat_objects.py:176 (register_combat_objects)`; `combat_objects.py:223 (mutate_combat_object)` |
| `movement_ledgers` | `live_movement.py:100 (add_dash)`; `live_movement.py:107 (pay_movement)`; `orchestrator.py:3150 (__post_init__)` | `live_movement.py:79 (ledger_for)` |
| `opportunity_attack_weapons` | - | `orchestrator.py:12635 (_opportunity_attack_of)` |
| `monster_slug_by_entity` | `orchestrator.py:10012 (_apply_transform)`; `orchestrator.py:10058 (_revert_transform_on_expiry)`; `orchestrator.py:10060 (_revert_transform_on_expiry)` | `orchestrator.py:10148 (_challenge_rating_of)`; `orchestrator.py:10228 (_stat_block_magnitudes_of)`; `orchestrator.py:10256 (_current_stat_block_slug)` |
| `xp_value_by_entity` | - | `orchestrator.py:13080 (_project_outcome)`; `orchestrator.py:8906 (_purge_entity_state)` |
| `event_log` | `orchestrator.py:4865 (_emit)`; `orchestrator.py:5180 (_emit_apply_damage)`; `orchestrator.py:5210 (_apply_zero_hp_to_character)` | `live_attack_riders.py:410 (_resolve)`; `live_attack_riders.py:418 (_resolve)`; `live_effect_lifecycle.py:224 (run_repeats)` |
| `tracked_hp` | `orchestrator.py:5091 (_emit_apply_damage)`; `orchestrator.py:5263 (_emit_apply_healing)`; `orchestrator.py:5554 (_maybe_roll_death_save)` | `orchestrator.py:13067 (_project_outcome)`; `orchestrator.py:5064 (_emit_apply_damage)`; `orchestrator.py:5258 (_emit_apply_healing)` |
| `tracked_temp_hp` | `orchestrator.py:10067 (_revert_transform_on_expiry)`; `orchestrator.py:5072 (_emit_apply_damage)`; `orchestrator.py:5309 (_emit_apply_temp_hp)` | `orchestrator.py:10007 (_apply_transform)`; `orchestrator.py:10211 (_end_polymorph_on_depletion)`; `orchestrator.py:13074 (_project_outcome)` |
| `undead_fortitude_holds` | `activities/apply.py:239 (apply_damage)`; `orchestrator.py:5088 (_emit_apply_damage)` | `live_reactions.py:149 (_context)`; `orchestrator.py:11693 (_pc_attack_context_kwargs)`; `orchestrator.py:2515 (_monster_context_kwargs)` |
| `active_conditions` | `orchestrator.py:3786 (_fold_condition_onto_combatant)`; `orchestrator.py:3867 (_strip_condition_from_combatant)`; `orchestrator.py:4901 (_emit)` | `live_features.py:133 (commit_feature_operation)`; `orchestrator.py:3931 (_drop_concentration)`; `orchestrator.py:5488 (_emit_apply_effect_expired)` |
| `active_effects` | `orchestrator.py:4058 (_release_one_grapple_effect)`; `orchestrator.py:4174 (_handle_grapple)`; `orchestrator.py:5365 (_remove_condition_sources)` | `action_policy.py:122 (validate_grant)`; `action_policy.py:158 (grant_payment)`; `action_policy.py:22 (policies)` |
| `deaths_recorded` | `orchestrator.py:5593 (_record_death)` | `orchestrator.py:13094 (_project_outcome)` |
| `dead_ids` | `orchestrator.py:5589 (_record_death)` | `combat_objects.py:149 (_holder_valid)`; `live_checks.py:162 (validate_check_surface)`; `live_checks.py:189 (validate_live_request)` |
| `expended_resources` | `orchestrator.py:5418 (_emit_apply_effect_applied)` | `orchestrator.py:13099 (_project_outcome)`; `orchestrator.py:8912 (_purge_entity_state)` |
| `current_actor_id` | `orchestrator.py:4928 (_emit_apply_turn_started)`; `orchestrator.py:8756 (_open_turn_at_current_index)` | `action_policy.py:35 (denial)`; `attack_declarations.py:139 (observe_attack_roll)`; `attack_declarations.py:59 (validate_declaration)` |
| `spell_slots_by_entity` | - | `orchestrator.py:11002 (_slot_available)`; `orchestrator.py:11011 (_take_spell_slot)`; `orchestrator.py:6058 (_project_caster_pools)` |
| `pact_slots_by_entity` | - | `orchestrator.py:11003 (_slot_available)`; `orchestrator.py:11012 (_take_spell_slot)`; `orchestrator.py:8914 (_purge_entity_state)` |
| `spells_known_by_entity` | - | `orchestrator.py:6070 (_project_caster_pools)`; `orchestrator.py:8915 (_purge_entity_state)`; `views.py:223 (from_live)` |
| `custom_counters_by_entity` | `orchestrator.py:8085 (_increment_feature_use)`; `orchestrator.py:8497 (_record_item_charge_spend)` | `orchestrator.py:11724 (_prepare_feature_invocation)`; `orchestrator.py:6082 (_project_caster_pools)`; `orchestrator.py:8075 (_feature_use_spent)` |
| `concentration_chain` | `live_effect_lifecycle.py:76 (forget_effect)`; `live_effect_lifecycle.py:78 (forget_effect)`; `orchestrator.py:3943 (_drop_concentration)` | `live_effect_lifecycle.py:71 (forget_effect)`; `ongoing_spell_activation.py:45 (activate_spell)`; `orchestrator.py:10028 (_end_transform)` |
| `concentration_rounds_remaining` | `live_effect_lifecycle.py:79 (forget_effect)`; `orchestrator.py:3944 (_drop_concentration)`; `orchestrator.py:6563 (_record_effect_lifecycle_links)` | `orchestrator.py:6734 (_hook_concentration_expiry)`; `orchestrator.py:8918 (_purge_entity_state)` |
| `conditions_by_effect` | `orchestrator.py:4061 (_release_one_grapple_effect)`; `orchestrator.py:5330 (_attach_effect_statuses)`; `orchestrator.py:5371 (_remove_condition_sources)` | `live_effect_lifecycle.py:35 (register_effect)`; `orchestrator.py:3906 (_drop_concentration)`; `orchestrator.py:5369 (_remove_condition_sources)` |
| `effect_lifecycles` | `live_effect_lifecycle.py:123 (observe_modifier_consumption)`; `live_effect_lifecycle.py:199 (run_repeats)`; `live_effect_lifecycle.py:39 (register_effect)` | `live_effect_lifecycle.py:116 (observe_modifier_consumption)`; `live_effect_lifecycle.py:136 (expire_effect)`; `live_effect_lifecycle.py:192 (run_repeats)` |
| `lifecycle_damage` | `live_effect_lifecycle.py:247 (record_damage)`; `live_effect_lifecycle.py:253 (damage_instance_completed)`; `live_effect_lifecycle.py:278 (actor_departed)` | `live_effect_lifecycle.py:247 (record_damage)`; `live_effect_lifecycle.py:253 (damage_instance_completed)` |
| `character_damage_instances` | `orchestrator.py:5079 (_emit_apply_damage)`; `orchestrator.py:5206 (_complete_character_damage_instance)`; `live_effect_lifecycle.py:280 (actor_departed)` | `orchestrator.py:5206 (_complete_character_damage_instance)`; `evaluation_snapshot.py (in-flight capture guard)` |
| `event_listeners` | `orchestrator.py:11774 (_execution_transaction)`; `orchestrator.py:11786 (_execution_transaction)`; `orchestrator.py:7889 (_start_combat)` | `orchestrator.py:11760 (_execution_transaction)`; `orchestrator.py:4868 (_emit)` |
| `pending_reactions` | `live_reactions.py:242 (register_pending_reaction)`; `live_reactions.py:243 (register_pending_reaction)`; `live_reactions.py:357 (fire_reaction)` | `live_reactions.py:242 (register_pending_reaction)`; `live_reactions.py:350 (fire_reaction)`; `live_reactions.py:543 (observe_reaction_lifecycle)` |
| `active_reaction_responses` | `live_reactions.py:470 (record_reaction_effects)`; `live_reactions.py:536 (observe_reaction_lifecycle)`; `live_reactions.py:544 (observe_reaction_lifecycle)` | `live_reactions.py:468 (record_reaction_effects)`; `live_reactions.py:507 (targeted_spell_opportunities)`; `live_reactions.py:538 (observe_reaction_lifecycle)` |
| `reaction_resolution_depth` | `live_reactions.py:388 (fire_reaction)`; `live_reactions.py:408 (fire_reaction)` | `live_reactions.py:347 (fire_reaction)` |
| `damage_instance_sequence` | `live_reactions.py:630 (next_instance)` | `live_reactions.py:631 (next_instance)` |
| `rider_uses` | `live_attack_riders.py:390 (_pay)`; `orchestrator.py:4930 (_emit_apply_turn_started)` | `live_attack_riders.py:153 (preflight_attack_riders)`; `live_attack_riders.py:380 (_pay)` |
| `processed_zero_hp_damage_instances` | `orchestrator.py:5219 (_apply_zero_hp_to_character)`; `orchestrator.py:5233 (_apply_zero_hp_to_character)` | `orchestrator.py:5231 (_apply_zero_hp_to_character)` |
| `reaction_effects_pending_expiry` | `live_reactions.py:479 (record_reaction_effects)`; `orchestrator.py:5040 (_hook_expire_reaction_effects)` | `orchestrator.py:8919 (_purge_entity_state)` |
| `help_grants` | `orchestrator.py:1256 (_pop_help_grant)`; `orchestrator.py:4310 (_dispatch_simple_turn_ending_intent)`; `orchestrator.py:5004 (_emit_apply_turn_started)` | `activities/check_pipeline.py:261 (resolve_check_request)`; `activities/check_pipeline.py:268 (resolve_check_request)`; `activities/check_pipeline.py:83 (_sources)` |
| `help_check_grants` | `live_checks.py:235 (observe_check)`; `orchestrator.py:4305 (_dispatch_simple_turn_ending_intent)`; `orchestrator.py:5000 (_emit_apply_turn_started)` | `live_checks.py:229 (observe_check)`; `live_checks.py:64 (project_check_states)`; `orchestrator.py:5000 (_emit_apply_turn_started)` |
| `hidden_entities` | `orchestrator.py:4529 (_handle_hide)`; `orchestrator.py:4607 (_break_hide)`; `orchestrator.py:8931 (_purge_entity_state)` | `orchestrator.py:4589 (_break_hide_on_attack_or_verbal_cast)`; `orchestrator.py:4604 (_break_hide)` |
| `vex_grants` | `orchestrator.py:1301 (_pop_vex_grants)`; `orchestrator.py:1382 (_fold_mastery_procs)`; `orchestrator.py:6762 (_hook_expire_vex_grants)` | `orchestrator.py:1269 (_attacker_vex_advantage_map)`; `orchestrator.py:1285 (_pop_vex_grants)`; `orchestrator.py:6754 (_hook_expire_vex_grants)` |
| `rage_bonus_extensions` | `orchestrator.py:6850 (_hook_rage_extension)`; `orchestrator.py:8148 (_record_rage_extension)`; `orchestrator.py:8932 (_purge_entity_state)` | `orchestrator.py:6849 (_hook_rage_extension)` |
| `sap_marks` | `orchestrator.py:1316 (_pop_sap_mark)`; `orchestrator.py:1386 (_fold_mastery_procs)`; `orchestrator.py:5012 (_emit_apply_turn_started)` | `orchestrator.py:11682 (_pc_attack_context_kwargs)`; `orchestrator.py:1311 (_pop_sap_mark)`; `orchestrator.py:2489 (_monster_context_kwargs)` |
| `slow_marks` | `orchestrator.py:1388 (_fold_mastery_procs)`; `orchestrator.py:5022 (_emit_apply_turn_started)`; `orchestrator.py:8966 (_purge_references_to)` | `live_movement.py:47 (effective_speed)`; `orchestrator.py:5018 (_emit_apply_turn_started)`; `orchestrator.py:5019 (_emit_apply_turn_started)` |
| `monster_action_uses_by_entity` | `orchestrator.py:10013 (_apply_transform)`; `orchestrator.py:10062 (_revert_transform_on_expiry)`; `orchestrator.py:10064 (_revert_transform_on_expiry)` | `orchestrator.py:10000 (_apply_transform)`; `orchestrator.py:2205 (_monster_cast_candidate)`; `orchestrator.py:2253 (_monster_limited_cast_remaining)` |
| `monster_turn_start_done` | `orchestrator.py:915 (_run_monster_turn_start)` | `orchestrator.py:913 (_run_monster_turn_start)` |
| `last_ended_turn` | `orchestrator.py:8819 (_close_turn)` | `orchestrator.py:2727 (_eligible_legendary_actor)`; `orchestrator.py:2729 (_eligible_legendary_actor)`; `orchestrator.py:2769 (_spend_legendary_use)` |
| `departed_actor_id` | `orchestrator.py:8801 (_end_turn_and_advance)`; `orchestrator.py:8841 (_hand_off_departed_turn)`; `orchestrator.py:9022 (_leave_roster)` | `orchestrator.py:8740 (_begin_turn)`; `orchestrator.py:8800 (_end_turn_and_advance)`; `orchestrator.py:8836 (_hand_off_departed_turn)` |
| `legendary_windows_used` | `orchestrator.py:2771 (_spend_legendary_use)` | `orchestrator.py:2744 (qualifies)` |
| `legendary_resistance_armed` | `activities/save_primitive.py:213 (_convert_if_legendary_resistance_armed)`; `orchestrator.py:3555 (resolve_legendary_resistance)`; `orchestrator.py:835 (_sync_legendary_resistance)` | `activities/save_primitive.py:209 (_convert_if_legendary_resistance_armed)`; `orchestrator.py:3546 (resolve_legendary_resistance)`; `orchestrator.py:6188 (_build_hydration_payload)` |
| `legendary_resistance_applied_event_indices` | `orchestrator.py:832 (_sync_legendary_resistance)`; `orchestrator.py:879 (_emit_legendary_resistance_used)` | `orchestrator.py:830 (_sync_legendary_resistance)` |
| `lifecycle` | - | `action_policy.py:110 (grant_group)`; `activities/effects.py:357 (apply_activity_effects)`; `activities/effects.py:366 (apply_activity_effects)` |
| `constructs` | `orchestrator.py:8942 (_purge_entity_state)`; `orchestrator.py:9471 (_apply_construct_requests)`; `orchestrator.py:9568 (_end_anchor_dependents)` | `orchestrator.py:8941 (_purge_entity_state)`; `orchestrator.py:9265 (_repeated_construct)`; `orchestrator.py:9567 (_end_anchor_dependents)` |
| `transforms` | `orchestrator.py:10055 (_revert_transform_on_expiry)`; `orchestrator.py:9991 (_apply_transform)` | `live_reactions.py:68 (can_take_reaction)`; `orchestrator.py:10023 (_end_transform)`; `orchestrator.py:10049 (_revert_transform_on_expiry)` |
| `summons` | `orchestrator.py:9803 (_seat_summon)` | `orchestrator.py:10140 (_challenge_rating_of)`; `orchestrator.py:10225 (_stat_block_magnitudes_of)`; `orchestrator.py:1161 (_side_of)` |
| `summon_counts` | `orchestrator.py:9711 (_summon_id)` | `orchestrator.py:9710 (_summon_id)` |
| `entity_id` | `activities/apply.py:239 (apply_damage)`; `live_movement.py:100 (add_dash)`; `live_movement.py:107 (pay_movement)` | `action_policy.py:122 (validate_grant)`; `action_policy.py:158 (grant_payment)`; `action_policy.py:234 (needs_new_attack_action)` |
| `entity_type` | - | `activities/build_context.py:124 (_attack_bonus_override)`; `activities/build_context.py:329 (build_activity_context)`; `activities/build_context.py:445 (build_activity_context)` |
| `name` | `orchestrator.py:5419 (_emit_apply_effect_applied)` | `activities/effects.py:233 (passive_effect_to_active_effect)`; `activities/effects.py:234 (passive_effect_to_active_effect)`; `activities/effects.py:235 (passive_effect_to_active_effect)` |
| `hp_current` | `activities/apply.py:228 (apply_damage)` | `activities/apply.py:170 (apply_damage)`; `activities/apply.py:183 (apply_damage)`; `build_party.py:37 (build_party_member)` |
| `temp_hp` | - | `orchestrator.py:6124 (_build_hydration_payload)` |
| `is_alive` | - | `combat_objects.py:149 (_holder_valid)`; `live_checks.py:161 (validate_check_surface)`; `live_checks.py:189 (validate_live_request)` |
| `conditions` | - | `activities/attack.py:364 (_resolve_cleave_chain)`; `activities/build_context.py:565 (build_activity_context)`; `activities/build_context.py:566 (build_activity_context)` |
| `hp_max` | - | `build_party.py:31 (build_party_member)`; `orchestrator.py:1004 (_monster_is_fleeing)`; `orchestrator.py:5562 (_hp_max_for)` |
| `ac` | - | `activities/attack.py:219 (resolve_attack)`; `activities/attack.py:384 (_resolve_cleave_chain)`; `build_party.py:39 (build_party_member)` |
| `attack_bonus` | - | `activities/build_context.py:102 (_save_dc)`; `activities/build_context.py:128 (_attack_bonus_override)`; `activities/build_context.py:452 (build_activity_context)` |
| `attack_rolls_made_this_turn` | - | `attack_declarations.py:144 (observe_attack_roll)`; `attack_declarations.py:60 (validate_declaration)` |
| `damage_dice` | - | `orchestrator.py:2149 (_synthesize_attack_from_legacy_fields)`; `orchestrator.py:7474 (_build_foe_combatants)` |
| `damage_type` | - | `activities/attack.py:1095 (_attack_rider_plan)`; `activities/attack.py:1383 (observe_damage)`; `activities/attack.py:1559 (_roll_base_weapon_damage)` |
| `behavior_profile` | - | `orchestrator.py:1001 (_monster_is_fleeing)`; `orchestrator.py:2418 (_resolve_monster_activities)`; `orchestrator.py:7476 (_build_foe_combatants)` |
| `strength` | - | `activities/attack.py:921 (_weapon_heavy_disadvantage)`; `activities/build_context.py:333 (build_activity_context)`; `build_party.py:40 (build_party_member)` |
| `dexterity` | - | `activities/attack.py:921 (_weapon_heavy_disadvantage)`; `activities/build_context.py:334 (build_activity_context)`; `build_party.py:41 (build_party_member)` |
| `constitution` | - | `activities/build_context.py:335 (build_activity_context)`; `build_party.py:42 (build_party_member)`; `live_spell_delivery.py:67 (_validate_spell_inputs)` |
| `intelligence` | - | `activities/build_context.py:336 (build_activity_context)`; `build_party.py:43 (build_party_member)`; `live_spell_delivery.py:68 (_validate_spell_inputs)` |
| `wisdom` | - | `activities/build_context.py:337 (build_activity_context)`; `build_party.py:44 (build_party_member)`; `live_spell_delivery.py:69 (_validate_spell_inputs)` |
| `charisma` | - | `activities/build_context.py:338 (build_activity_context)`; `build_party.py:45 (build_party_member)`; `live_spell_delivery.py:70 (_validate_spell_inputs)` |
| `proficiency_bonus_override` | - | `activities/actor_stats.py:50 (proficiency_bonus_of)`; `activities/actor_stats.py:51 (proficiency_bonus_of)` |
| `save_proficiencies` | - | `activities/actor_stats.py:57 (save_modifier)`; `build_party.py:66 (build_party_member)`; `orchestrator.py:7325 (_build_pc_combatants)` |
| `skill_proficiencies` | - | `activities/actor_stats.py:65 (check_modifier)`; `activities/check_pipeline.py:224 (resolve_check_request)`; `build_party.py:67 (build_party_member)` |
| `skill_expertise` | - | `activities/actor_stats.py:66 (check_modifier)`; `build_party.py:68 (build_party_member)`; `orchestrator.py:7327 (_build_pc_combatants)` |
| `skill_check_bonuses` | - | `activities/actor_stats.py:70 (check_modifier)`; `build_party.py:69 (build_party_member)`; `orchestrator.py:7328 (_build_pc_combatants)` |
| `jack_of_all_trades` | - | `activities/actor_stats.py:68 (check_modifier)`; `build_party.py:70 (build_party_member)`; `check.py:212 (resolve_check)` |
| `reliable_talent` | - | `activities/check_pipeline.py:227 (resolve_check_request)`; `build_party.py:71 (build_party_member)`; `check.py:214 (resolve_check)` |
| `stealth_disadvantage` | - | `activities/check_pipeline.py:68 (_sources)`; `build_party.py:72 (build_party_member)`; `orchestrator.py:7331 (_build_pc_combatants)` |
| `tool_proficiencies` | - | `activities/check_pipeline.py:185 (resolve_check_request)`; `activities/check_pipeline.py:225 (resolve_check_request)`; `orchestrator.py:7332 (_build_pc_combatants)` |
| `weapon_proficiencies` | - | `build_party.py:73 (build_party_member)`; `orchestrator.py:7256 (_is_proficient_with_weapon)`; `orchestrator.py:7259 (_is_proficient_with_weapon)` |
| `death_saves` | - | `death_saves.py:189 (roll_death_save)`; `death_saves.py:190 (roll_death_save)`; `orchestrator.py:5234 (_apply_zero_hp_to_character)` |
| `creature_type` | - | `live_checks.py:137 (validate_check_surface)`; `live_spell_delivery.py:556 (prepare_activity_delivery)`; `orchestrator.py:10079 (_validated_beast_form)` |
| `creature_size` | - | `activities/attack.py:1137 (_attack_rider_plan)`; `activities/mastery.py:140 (apply_mastery_on_hit)`; `build_party.py:56 (build_party_member)` |
| `damage_resistances` | - | `activities/apply.py:101 (_effective_resistances)`; `build_party.py:61 (build_party_member)`; `orchestrator.py:5968 (_project_target_modifiers)` |
| `damage_immunities` | - | `activities/apply.py:60 (_damage_immunities)`; `build_party.py:62 (build_party_member)`; `orchestrator.py:5974 (_project_target_modifiers)` |
| `damage_vulnerabilities` | - | `orchestrator.py:5984 (_project_target_modifiers)`; `orchestrator.py:5986 (_project_target_modifiers)`; `orchestrator.py:7309 (_build_pc_combatants)` |
| `condition_immunities` | - | `activities/effects.py:130 (applicable_effect_statuses)`; `activities/effects.py:83 (is_condition_immune)`; `build_party.py:63 (build_party_member)` |
| `senses` | - | `build_party.py:64 (build_party_member)`; `build_spec.py:723 (derive_sheet)`; `build_spec.py:740 (derive_sheet)` |
| `concentration_effect_id` | - | `build_party.py:50 (build_party_member)`; `orchestrator.py:3952 (_drop_concentration)`; `orchestrator.py:6165 (_build_hydration_payload)` |
| `character_level` | - | `activities/actor_stats.py:52 (proficiency_bonus_of)`; `activities/build_context.py:148 (spell_attack_magnitudes)`; `activities/build_context.py:340 (build_activity_context)` |
| `action_available` | - | `action_policy.py:201 (preflight_intent_policy)`; `action_policy.py:268 (record_budget_changes)`; `action_policy.py:52 (denial)` |
| `bonus_action_available` | - | `action_policy.py:271 (record_budget_changes)`; `orchestrator.py:10586 (_action_economy_gate_failure)`; `orchestrator.py:10831 (_classify_attack_funding)` |
| `reaction_available` | - | `live_reactions.py:62 (can_take_reaction)`; `orchestrator.py:10594 (_action_economy_gate_failure)` |
| `action_taken_this_turn` | - | `action_policy.py:40 (denial)` |
| `bonus_action_taken_this_turn` | - | `action_policy.py:45 (denial)` |
| `attack_action_attacks_made` | - | `action_policy.py:235 (needs_new_attack_action)`; `action_policy.py:51 (denial)`; `orchestrator.py:10677 (_consume_attack_budget)` |
| `action_grants_spent` | - | `action_policy.py:132 (validate_grant)`; `action_policy.py:161 (grant_payment)`; `action_policy.py:252 (has_action_grant)` |
| `action_grant_groups_spent` | - | `action_policy.py:133 (validate_grant)`; `action_policy.py:162 (grant_payment)`; `action_policy.py:164 (grant_payment)` |
| `base_speed` | - | `build_party.py:57 (build_party_member)`; `live_movement.py:40 (effective_speed)`; `orchestrator.py:7313 (_build_pc_combatants)` |
| `movement_remaining` | - | `live_movement.py:83 (ledger_for)`; `orchestrator.py:4568 (_handle_stand_up)`; `orchestrator.py:7996 (_turn_can_continue)` |
| `movement_modes` | - | `build_party.py:65 (build_party_member)`; `build_spec.py:739 (derive_sheet)`; `live_movement.py:172 (movement_cost)` |
| `melee_reach_ft` | - | `live_movement.py:214 (drag_positions)`; `live_movement.py:240 (reconcile_grapple_range)`; `orchestrator.py:10281 (_stat_block_target_out_of_range)` |
| `granted_features` | - | `feature_repertoire.py:69 (feature_repertoire)`; `feature_repertoire.py:72 (feature_repertoire)`; `feature_repertoire.py:77 (feature_repertoire)` |
| `class_slug` | - | `build_party.py:52 (build_party_member)`; `build_spec.py:522 (_ability_scores)`; `build_spec.py:526 (_ability_scores)` |
| `classes` | - | `build_party.py:53 (build_party_member)`; `build_spec.py:368 (_subclass)`; `build_spec.py:370 (_subclass)` |
| `fighting_styles` | - | `orchestrator.py:12265 (_submit_player_intent)` |
| `carried_item_slugs` | - | `attack_riders.py:309 (_validate_required_items)` |
| `worn_armor` | - | `orchestrator.py:3992 (_martial_arts_active)`; `orchestrator.py:6799 (_reconcile_rage_state)` |
| `shield_equipped` | - | `orchestrator.py:3993 (_martial_arts_active)` |
| `subclass_slug` | - | `build_party.py:54 (build_party_member)`; `build_spec.py:362 (_subclass)`; `build_spec.py:364 (_subclass)` |
| `species_slug` | - | `build_party.py:55 (build_party_member)`; `build_spec.py:673 (derive_sheet)`; `feature_audit.py:110 (audit_context)` |
| `last_damaged_by` | - | - |
| `disengaging_this_turn` | - | `orchestrator.py:12458 (_opportunity_attackers)` |
| `sneak_attack_spent_this_turn` | - | `attack_riders.py:458 (plan_attack_riders)`; `orchestrator.py:11660 (_pc_attack_context_kwargs)`; `orchestrator.py:4993 (_emit_apply_turn_started)` |
| `attacks_remaining` | - | `orchestrator.py:10606 (_action_economy_gate_failure)`; `orchestrator.py:10676 (_consume_attack_budget)`; `orchestrator.py:10725 (_is_offhand_attack_swing)` |
| `attack_action_engaged` | - | `orchestrator.py:10618 (_action_economy_gate_failure)`; `orchestrator.py:10722 (_is_offhand_attack_swing)`; `orchestrator.py:7949 (_attack_action_is_spent)` |
| `light_weapon_swing_slug` | - | `orchestrator.py:10724 (_is_offhand_attack_swing)`; `orchestrator.py:7935 (_offhand_window_open)` |
| `restricted_light_weapon_swing_slug` | - | `orchestrator.py:10830 (_classify_attack_funding)`; `orchestrator.py:10835 (_classify_attack_funding)`; `orchestrator.py:7983 (_turn_can_continue)` |
| `offhand_attack_spent` | - | `orchestrator.py:10832 (_classify_attack_funding)`; `orchestrator.py:7935 (_offhand_window_open)`; `orchestrator.py:7984 (_turn_can_continue)` |
| `dodging` | - | `orchestrator.py:3721 (_dodge_benefit_active)` |
| `trait_mechanics` | - | `activities/apply.py:184 (apply_damage)`; `activities/check_pipeline.py:70 (_sources)`; `activities/heal.py:70 (resolve_heal)` |
| `physical_resistances_nonmagical_only` | - | `activities/apply.py:104 (_effective_resistances)`; `orchestrator.py:7387 (_build_foe_combatants)` |
| `legendary_actions_max` | - | `orchestrator.py:2734 (qualifies)`; `orchestrator.py:918 (_run_monster_turn_start)`; `orchestrator.py:919 (_run_monster_turn_start)` |
| `legendary_actions_remaining` | `orchestrator.py:2770 (_spend_legendary_use)`; `orchestrator.py:919 (_run_monster_turn_start)` | `orchestrator.py:2740 (qualifies)`; `orchestrator.py:2777 (_spend_legendary_use)`; `views.py:246 (from_live)` |
| `legendary_resistances_max` | - | `views.py:253 (from_live)` |
| `legendary_resistances_remaining` | `orchestrator.py:838 (_sync_legendary_resistance)`; `orchestrator.py:865 (_consume_armed_legendary_resistance)` | `orchestrator.py:3547 (resolve_legendary_resistance)`; `orchestrator.py:3551 (resolve_legendary_resistance)`; `orchestrator.py:6190 (_build_hydration_payload)` |
| `has_fled` | - | `orchestrator.py:13043 (_derive_ended_reason)` |
| `spellcasting_ability` | - | `activities/attack.py:856 (_governing_ability)`; `activities/cast.py:111 (resolve_cast)`; `activities/check.py:128 (_resolve_dc)` |
| `loading_weapon_fired_this_action` | - | `orchestrator.py:10908 (_consume_intent_budget)`; `orchestrator.py:2052 (_loading_weapon_already_fired_failure)` |
| `cleave_spent_this_turn` | - | `orchestrator.py:1465 (_cleave_available)` |
| `flurry_strikes_remaining` | - | `orchestrator.py:10847 (_classify_attack_funding)`; `orchestrator.py:10923 (_consume_intent_budget)`; `orchestrator.py:7980 (_turn_can_continue)` |
| `extra_actions_remaining` | - | `action_policy.py:201 (preflight_intent_policy)`; `action_policy.py:269 (record_budget_changes)`; `action_policy.py:53 (denial)` |
| `action_surge_used_this_turn` | - | `orchestrator.py:10950 (_action_surge_failure)`; `orchestrator.py:7954 (_action_surge_opportunity)` |
