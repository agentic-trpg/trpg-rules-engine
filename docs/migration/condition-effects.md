# Condition effect migration status

This page records the engine's migration from condition-name rules to typed
canonical clauses. Keep engine migration status here; the SRD data schema
documents the data contract and its sources.

## Current declarative clauses

| Condition | Migrated clauses |
|---|---|
| Incapacitated | `cannot_take_actions`, `breaks_concentration`, `disadvantage_initiative` |
| Charmed | `cant_attack_charmer` |
| Poisoned | `disadvantage_own_attacks`, `disadvantage_ability_checks` |
| Grappled | `speed_zero`, `disadvantage_attacks_except_grappler` |
| Invisible | `advantage_own_attacks`, `disadvantage_attacks_against`, `unseen` (observer-cannot-see-bearer gate) |
| Frightened | `disadvantage_own_attacks`, `disadvantage_ability_checks` (fear-source sight gate), `cant_move_toward_fear_source` (no visibility gate) |
| Exhaustion | `d20_test_penalty_per_level`, `speed_penalty_per_level` |
| Restrained | `disadvantage_own_attacks`, `disadvantage_save` (DEX), `advantage_attacks_against`, `speed_zero` |
| Blinded | `disadvantage_own_attacks`, `advantage_attacks_against` |
| Prone | `disadvantage_own_attacks`, `advantage_attacks_against` (within 5 ft), `disadvantage_attacks_against` (beyond 5 ft) |
| Paralyzed | `auto_fail_save` (STR, DEX), `advantage_attacks_against`, `speed_zero`, `auto_crit_within_5ft` |
| Stunned | `auto_fail_save` (STR, DEX), `advantage_attacks_against` |
| Petrified | `auto_fail_save` (STR, DEX), `advantage_attacks_against`, `speed_zero`, `resist_all_damage`, `immune_to_condition` (`poisoned`) |
| Unconscious | `auto_fail_save` (STR, DEX), `advantage_attacks_against`, `speed_zero`, `auto_crit_within_5ft` |

## Execution path

```text
canonical condition data
→ ConditionEffect
→ condition-specific clause allowlist
→ project_condition_effects()
→ ActiveEffectChange
→ existing resolver
```

`project_condition_effects()` translates supported clause kinds into active-effect
changes, all with `mode="override"`:

| Clause kind | Change key | Value |
|---|---|---|
| `ADVANTAGE_ATTACKS_AGAINST` (`value=None`) | `flags.advantage.attack` | `True` |
| `ADVANTAGE_ATTACKS_AGAINST` (integer `value`) | `flags.advantage.attack.within_ft` | threshold in feet |
| `DISADVANTAGE_ATTACKS_AGAINST` (integer `value`) | `flags.disadvantage.attack.beyond_ft` | threshold in feet |
| `AUTO_CRIT_WITHIN_5FT` (integer `value`) | `flags.auto_crit.attack.within_ft` | threshold in feet |
| `DISADVANTAGE_OWN_ATTACKS` (`gate=None`) | `flags.disadvantage.attack` | `True` |
| `DISADVANTAGE_OWN_ATTACKS` (`FEAR_SOURCE_IN_SIGHT`) | `flags.disadvantage.attack.gate.fear_source_in_sight` | `True` |
| `ADVANTAGE_OWN_ATTACKS` (`OBSERVER_CANNOT_SEE_BEARER`) | `flags.advantage.attack.gate.observer_cannot_see_bearer` | `True` |
| `DISADVANTAGE_ATTACKS_AGAINST` (`OBSERVER_CANNOT_SEE_BEARER`) | `flags.disadvantage.attack.gate.observer_cannot_see_bearer` | `True` |
| `DISADVANTAGE_ATTACKS_EXCEPT_GRAPPLER` | `flags.disadvantage.attack.except_grappler` | `True` |
| `DISADVANTAGE_ABILITY_CHECKS` (`gate=None`) | `flags.disadvantage.check` | `True` |
| `DISADVANTAGE_ABILITY_CHECKS` (`FEAR_SOURCE_IN_SIGHT`) | `flags.disadvantage.check.gate.fear_source_in_sight` | `True` |
| `CANT_ATTACK_CHARMER` (`gate=None`) | `targeting.cannot_attack_charmer` | `True` |
| `CANT_MOVE_TOWARD_FEAR_SOURCE` (`gate=None`) | `movement.cannot_move_toward_fear_source` | `True` |
| `UNSEEN` (`OBSERVER_CANNOT_SEE_BEARER`) | `visibility.unseen.gate.observer_cannot_see_bearer` | `True` |
| `DISADVANTAGE_SAVE` | `flags.disadvantage.save.<ability>` | `True` |
| `AUTO_FAIL_SAVE` | `flags.auto_fail.save.<ability>` | `True` |
| `SPEED_ZERO` | `speed.override` | `0` (integer) |
| `D20_TEST_PENALTY_PER_LEVEL` | `d20_test.penalty_per_level` | canonical integer multiplier |
| `SPEED_PENALTY_PER_LEVEL` | `speed.penalty_per_level` | canonical integer multiplier |
| `RESIST_ALL_DAMAGE` | `damage.resistance.all` | `True` |
| `IMMUNE_TO_CONDITION` | `condition.immunity` | one string per typed condition scope |
| `CANNOT_TAKE_ACTIONS` | `condition.cannot_take_actions` | `True` |
| `BREAKS_CONCENTRATION` | `condition.breaks_concentration` | `True` |
| `DISADVANTAGE_INITIATIVE` | `flags.disadvantage.initiative` | `True` |

Incapacitated opts in only these three clauses. Paralyzed, Petrified, Stunned
and Unconscious reach them through `CONDITION_IMPLIES`, rather than separate
action, concentration or initiative opt-ins. The consumers accept only exact
keys, `mode="override"`, and the boolean `True`; integer 1, strings, other keys
and other modes are inert. Qualifier prose does not govern any of them.
`conditions_block_actions()` remains the shared action/reaction eligibility
predicate, with the existing pass, move and drop-concentration exceptions.

`BREAKS_CONCENTRATION` controls only concentration teardown. Grapple release,
Rage expiry (including Persistent Rage's Unconscious exception), and Wild Shape
expiry still use their legacy Incapacitated implication semantics independently.
Projection is pure; the existing runtime teardown functions own state and
typed events. Effect-applied teardown retains its order between `EffectApplied`
and the following `ConditionApplied`.

Combat initialization hydrates every seed and concentration chain before
reconciling actual attached conditions, after 0-HP condition hydration. Stable
entity, condition and concentration-identity traversal makes lifecycle cleanup
independent of effect input ordering. Suppressed statuses do not own conditions
or trigger teardown. Reconciliation emits existing teardown events only for
state it ends, without fabricating `ConditionApplied` transitions or adding RNG
draws. Real Wild Shape forms still cannot be seeded across combats; that remains
the existing transform-hydration follow-up.

Initiative now consumes the projected initiative boolean. Fixed initiative,
party/encounter spec traversal, Surprise OR condition disadvantage, die calls and
tie-breaking are unchanged. It still reads raw seed statuses before Combatant
immunity filtering, an explicitly deferred initialization issue in `BACKLOG.md`.
`CANNOT_SPEAK` remains canonical metadata without opt-in or consumer; spell
components remain metadata and this batch adds no spell-component legality.

Petrified's damage defense follows `project_passive_damage_modifiers()` →
hydration payload → `ActivityResolutionContext.passive_damage_modifiers` →
the existing `apply_damage()` and `_apply_modifiers()` path. Its exact boolean
override grants `resistances=["all"]`, without any damage immunity. Poison
damage is halved, just like every other damage type; magic does not bypass the
all-damage resistance. Duplicate clauses halve only once. Existing vulnerability
×2 → resistance //2 → immunity 0 ordering and floor rounding are unchanged.

Condition immunity uses `ConditionEffect.condition_slugs: list[str]`, defaulting
to an empty list for old JSON. Qualifier prose is never parsed. The projector
normalizes scope slugs and preserves declaration order; the consumer accepts
only exact string overrides and returns stable unique scopes. The shared
`is_condition_immune()` checks static `Combatant.condition_immunities` unioned
with `project_condition_immunities()` from current conditions. Runtime effect
statuses, direct condition events/folds, combat seeds and mastery riders reuse
this check. An immune `ConditionApplied` is suppressed before observation;
`EffectApplied` still survives with its other riders, while its immune status
does not attach to either condition store. Acquiring Petrified does not remove
Poisoned already present. Removing an opt-in or canonical clause disables only
that defense, and changing typed scope changes immunity even when prose stays
the same. Generic projection never opts in unrelated conditions.

Save scopes come exclusively from `ConditionEffect.abilities`. The projector
normalizes ability codes by trimming whitespace and lowercasing, then uses the
existing full-name suffix vocabulary (`str` → `strength`, `dex` → `dexterity`,
and likewise for CON/INT/WIS/CHA). Duplicate scopes are emitted once; empty or
unknown scopes produce no flags. The key mapping and its sidecar consumer are
centralized in `rules/effects.py`.

Saving throws continue through the existing resolver pipeline:

```text
ActiveEffectChange
→ project_passive_save_modifiers()
→ passive_save_dis / passive_save_auto_fail sidecars
→ existing activity, grapple/shove, and end-of-turn save handlers
```

The sidecar keeps unique uppercase ability codes. Auto-fail also contributes
disadvantage as the existing defensive fallback, after explicit disadvantage
entries. The resolver still draws two d20s for disadvantage, one when advantage
and disadvantage cancel (or a save is otherwise normal), and none for auto-fail.
Event modes, source attribution, and RNG draw order are unchanged.

Poisoned, Restrained, Blinded, and Prone share the same projector for their
own-attack disadvantage. Projection is deterministic, performs no I/O or RNG
draws, and does not mutate the canonical input. The engine's temporary migration
registry uses clause-level explicit opt-in: each condition has an allowlist of
`ConditionEffectKind` values, and only those canonical clauses reach the
projector. Adding generic support for another kind does not migrate it for a
condition without a separate allowlist opt-in. The canonical clauses supply
their mechanical meaning.

The six previously migrated `ADVANTAGE_ATTACKS_AGAINST` clauses are unconditional. The
attack helper consumes `flags.advantage.attack` from the target's projection
and `flags.disadvantage.attack` from the attacker's projection, then feeds the
existing attack resolver and `roll_d20_test()`. Sources remain
`condition:target` and `condition:attacker`; multiple conditions never stack
extra dice. Advantage alone draws two d20s and keeps the higher, disadvantage
alone keeps the lower, and cancellation draws exactly one d20.

Distance-qualified attack mechanics use `ConditionEffect.kind` plus its integer
`value`; `qualifier` prose is never parsed. `attack_distance_flag_applies()`
validates the exact key, override mode, and integer type (excluding booleans and
strings), then compares the per-target `target_distance_ft` against that value.
Unknown distance leaves scoped flags inert. Prone's target advantage applies at
or below the threshold; its target disadvantage applies strictly above it.

Context-qualified attacks use the optional, enum-backed `ConditionEffect.gate`.
`OBSERVER_CANNOT_SEE_BEARER` observes the attacker from its target for own-attack
advantage, and observes the target from the attacker for attacks-against
disadvantage. `FEAR_SOURCE_IN_SIGHT` uses the bearer's existing fear-source
context. Grappled's specific except-grappler kind supplies identity semantics;
an unknown grappler stays inert. Consumers accept exact boolean override flags
and reuse the existing pierced, fear-sight, grappler and target inputs. Their
public signature is unchanged. Qualifier prose is explanatory and never parsed.
Unknown/unsupported gates and mixed integer-distance/visibility clauses project
nothing, without falling back to unconditional flags. Ungated own-attack
disadvantage and Prone's integer distance projection retain their meanings.

These four attack clauses have explicit condition/kind opt-ins. Removing either
the canonical clause or its opt-in disables only that mechanic. Generic support
does not opt in unrelated conditions. The same `_attack_roll_sources()` consumer
serves ordinary, Cleave, opportunity, monster and spell attacks; `condition:attacker`
and `condition:target` remain separate from generic geometry's `unseen` source.
Cancellation still draws one d20; duplicate conditions never stack extra dice.

### Source/context-relative consumers

Charmed's `cant_attack_charmer` opts in independently of its social-check clause.
`conditions_cannot_attack_charmer()` consumes the exact projected boolean override.
Player attack and harmful spell rejection, monster target filtering (including
legendary actions), and opportunity-attack filtering all consult this helper
before using `_condition_source_entity(..., "charmed")` for identity. Unknown
charmers impose no restriction. Utility/beneficial spells and the existing
Grapple/Shove boundary are unchanged. Rejected actions keep their existing
`target_is_charmer` failure, turn and action economy, with zero RNG draws.

Frightened's ability-check clause projects a distinct contextual key; it never
uses Poisoned's ungated `flags.disadvantage.check`. Both
`conditions_grant_disadvantage_on_ability_checks()` and
`project_passive_check_modifiers()` accept keyword `fear_source_in_sight=True`.
That default preserves callers without context and the existing unknown-source
convention. Live hydration supplies `_fear_source_in_sight(live, combatant)`.
Its existing Frightened presence read selects lineage context only; projected
check/attack flags still decide whether either disadvantage mechanic exists.
Known living sources out of sight remove only Frightened's check disadvantage;
Poisoned still imposes disadvantage in the same scene. The existing attack-roll
gate is unchanged.

Frightened's no-approach clause has **no visibility gate**. Its exact projected
boolean authorizes the restriction, lineage identifies the source, and the
existing spatial consumer tests each consecutive path step's distance. A known,
living, tracked source still blocks approach through darkness, walls or Invisible,
even when the check/attack disadvantage gate is false. Unknown/missing/dead
sources, unknown source positions and unresolvable distance pairs stay inert.
`MoveFailed(reason="frightened")` occurs before movement budget or opportunity
attacks, without RNG draws or partial movement.

Invisible's `unseen` flag is independent of its two attack flags.
`conditions_unseen()` accepts the observer's runtime ability to see the bearer
through the existing special-sense seam. `_combatant_can_see()` retains the
untracked-position convention, Blinded viewer handling, Blindsight/Truesight
reach, and final topology/cover/light test. Dodge, opportunity attacks, Hide,
Frightened sight and ranged-in-melee consumers reuse this composite predicate.
The generic geometry `unseen` attack source still uses its existing separate
visibility maps. Blinded's legacy inability to see is not projected from its
sight-check auto-failure clause.
Hide's `ConditionApplied`/`ConditionRemoved` reads still name Invisible as the
condition attached by that action; those lifecycle events do not authorize
concealment without an opted-in `unseen` clause.

All four consumers require exact keys, `mode="override"`, and `value is True`.
Integer 1, strings, wrong modes and other keys stay inert. Unsupported gates
and mixed numeric/context clauses project nothing; `UNSEEN` requires its exact
observer gate, and the two source-restriction clauses reject visibility gates.
Ungated ability-check disadvantage remains a supported typed clause for Poisoned.
Projection reads no source, visibility, live state, RNG or I/O. Qualifier prose
does not govern mechanics. Duplicate flags do not stack. Removing a canonical
clause or condition/kind opt-in genuinely disables only that mechanic, without
a condition-name fallback; removing Invisible's attack clauses leaves `unseen`
active and vice versa. Existing JSON without `gate` loads as `None`.
Regeneration follows the existing serializer's explicit-null convention.

No RNG is added. Existing legal/rejected Charmed actions, visibility consumers,
and visible-source Frightened checks retain their draw order and event behavior.
The two intentional Frightened corrections change results for affected inputs:
an out-of-sight source alone now produces a normal check (one d20 instead of
two); an approach previously permitted through loss of sight is now rejected
before any opportunity-attack draws. Advantage/disadvantage cancellation still
uses one d20, and disadvantage from Poisoned still uses two. Same corrected
initial state, intent sequence and seed remain deterministic.

`conditions_auto_crit_within_5ft()` is retained as a public consumer seam. Its
historical one-argument form queries at 5 ft; attack resolvers always supply the
actual distance, including unknown distance. It consumes projected clauses,
without a condition-name registry. Ordinary attacks and Cleave feed its boolean
into the existing `_resolve_hit_outcome(auto_crit_on_hit=...)`; opportunity and
monster attacks share that activity resolver. A miss or natural 1 stays a miss;
only a hit receives the automatic crit. Natural 20 and the existing critical
damage path are unchanged: dice double, flat modifiers and critical bonuses
apply once, and condition projection adds no RNG draws.

Unconscious still implies Prone. Its unconditional advantage remains independent
of both the scoped Prone clauses and auto-crit:

| Distance | Attack mode | D20 draws | Automatic crit on hit |
|---|---|---|---|
| Unknown | Advantage | 2 | No |
| 0 or 5 ft | Advantage | 2 | Yes |
| 6 or 30 ft | Normal (advantage and disadvantage cancel) | 1 | No |

Multiple or duplicate conditions contribute booleans and never stack extra dice
or crit multipliers. Paralyzed retains unconditional advantage at every distance,
with automatic crit only on hits within its threshold. The implied Prone
own-attack disadvantage is consumed only when that creature attacks.

The five migrated `SPEED_ZERO` clauses are unconditional. `project_speed()`
consumes the projected zero-speed scalar override instead of a condition-name
set, then `_effective_speed()` feeds the existing movement budget, voluntary
movement, player and monster Dash, stand-up, and Dodge consumers. A zero-speed
override takes precedence over the unchanged Exhaustion penalty; otherwise
speed remains `max(0, base_speed - projected_multiplier * exhaustion_level)`. Projection mutates
no movement state and consumes no RNG.

Forced movement keeps its separate existing path: `push_combatant()` can push a
Speed-0 creature without spending its movement budget or provoking opportunity
attacks. Unconscious's Speed 0 comes from its own canonical clause; its implied
Prone and Incapacitated conditions retain their existing semantics. Prone alone
does not zero Speed, and SRD 5.2 Stunned has no `SPEED_ZERO` clause.

Exhaustion's two numeric clauses use the same explicit allowlist and generic
projector. The projector copies `ConditionEffect.value` into an integer override
without reading runtime state or parsing `qualifier` prose. The small shared
`projected_scalar_value()` consumer reads the first matching integer override
in declaration order, rejects booleans, strings, wrong modes and other keys,
and returns no value when the clause is absent. Duplicate clauses never stack.

`exhaustion_level_of()` still reads the highest `ActiveCondition.exhaustion_level`
among active Exhaustion entries, or zero when absent. `d20_test_penalty()` applies
`-projected_multiplier * runtime_level`; the existing hydration sidecar and D20
resolvers consume that flat penalty. Attack rolls, saves, ability/skill checks,
death saves, and the existing grapple, Hide, concentration and repeat-save
paths keep their draw counts, modes and source attribution. Auto-fail saves
still draw no d20. Exhaustion does not impose ability-check disadvantage.
The standalone out-of-combat `CheckSpec` has no condition/Exhaustion input;
that existing API limitation is unchanged by this engine migration.

`project_speed()` gives `speed.override=0` precedence, otherwise subtracts the
projected Speed multiplier times the supplied level and floors at zero. Its
historical explicit-level API also works when the condition-name list omits
Exhaustion. The orchestrator supplies the level through `exhaustion_level_of()`;
movement budgets, player/monster Dash and monster approach/flee reuse the
existing `_effective_speed()` path. Missing canonical clauses or allowlist
opt-ins remove only that clause's penalty; there is no constant fallback.

Canonical values remain 2 for D20 Tests and 5 ft for Speed. The engine's duplicate
`EXHAUSTION_*_PENALTY_PER_LEVEL` constants and exports have been removed.
`DEATH_AT_LEVEL(value=6)` is not opted in or projected: no complete runtime
Exhaustion-level death event/state transition exists, and this migration adds
no level-6 death behavior.

## Legacy boundary

Clauses outside the migration table remain on their existing legacy paths. In
particular:

- Grappled: `speed_zero` and attack disadvantage except against the grappler are
  migrated. Source-identity tracking, drag/carry, escape and grapple removal
  retain their existing paths and implementation boundaries.
- Blinded: the viewer's legacy inability to see remains in the composite
  visibility predicate. `auto_fail_sight_checks` is not opted in and still
  lacks a per-check sense requirement. Deafened's `auto_fail_hearing_checks`
  likewise remains outside the migration and unimplemented.
- Prone: crawl movement restrictions. Both target-side distance attack clauses
  are migrated; unknown distance leaves those scoped clauses inert.
- Charmed: the attack/harmful-target restriction is migrated; charmer identity
  remains runtime lineage. `charmer_social_advantage` remains unimplemented.
- Invisible: both attack clauses and `unseen` are migrated with separate typed
  observer gates. `advantage_initiative` remains outside the migration.
- Frightened: attack/check disadvantage and no-approach are migrated; source
  identity and spatial/visibility context remain runtime responsibilities.
  Monster AI's deferred no-approach enforcement remains outside this batch.
- Exhaustion: both numeric penalty clauses are migrated; `death_at_level`
  remains outside the migration and has no complete live execution path.
- Incapacitated: `cannot_speak` remains unimplemented gameplay; verbal spell
  components, Silence and other component gates are not enforced.
- Paralyzed, Stunned, Petrified, and Unconscious: action, concentration and
  initiative mechanics come from implied Incapacitated's canonical clauses;
  other clauses outside the migration table keep their existing paths. Nearby
  automatic critical hits for Paralyzed and Unconscious are now migrated.
  Petrified's defensive clauses are migrated; poison condition immunity grants
  no poison damage immunity.
  Stunned has no SRD 5.2 speed-zero clause.
  Unconscious still implies Prone, whose own-attack disadvantage was already
  migrated.

This status describes which clauses have migrated; it does not claim that every
legacy clause is enforced. Existing sight-check and crawl implementation gaps
remain unchanged.

Update this page as engine migrations proceed. Engine migration status updates
do not require changes to the SRD schema docstring or canonical data.
