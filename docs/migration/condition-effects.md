# Condition effect migration status

This page records the engine's migration from condition-name rules to typed
canonical clauses. Keep engine migration status here; the SRD data schema
documents the data contract and its sources.

## Current declarative clauses

| Condition | Migrated clauses |
|---|---|
| Poisoned | `disadvantage_own_attacks`, `disadvantage_ability_checks` |
| Restrained | `disadvantage_own_attacks`, `disadvantage_save` (DEX) |
| Blinded | `disadvantage_own_attacks` |
| Prone | `disadvantage_own_attacks` |
| Paralyzed | `auto_fail_save` (STR, DEX) |
| Stunned | `auto_fail_save` (STR, DEX) |
| Petrified | `auto_fail_save` (STR, DEX) |
| Unconscious | `auto_fail_save` (STR, DEX) |

## Execution path

```text
canonical condition data
→ ConditionEffect
→ condition-specific clause allowlist
→ project_condition_effects()
→ ActiveEffectChange
→ existing resolver
```

`project_condition_effects()` translates supported clause kinds into the existing
override flags, with `mode="override"` and `value=True`:

| Clause kind | Change key |
|---|---|
| `DISADVANTAGE_OWN_ATTACKS` | `flags.disadvantage.attack` |
| `DISADVANTAGE_ABILITY_CHECKS` | `flags.disadvantage.check` |
| `DISADVANTAGE_SAVE` | `flags.disadvantage.save.<ability>` |
| `AUTO_FAIL_SAVE` | `flags.auto_fail.save.<ability>` |

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

## Legacy boundary

Clauses outside the migration table remain on their existing legacy paths. In
particular:

- Restrained: speed zero and advantage on attacks against the target.
- Blinded: sight-check automatic failure and advantage on attacks against the
  target.
- Prone: crawl movement restrictions, advantage on attacks against the target
  within 5 ft, and disadvantage on attacks against the target beyond 5 ft.
  Unknown distance leaves the target attack rule inert.
- Paralyzed, Stunned, Petrified, and Unconscious: all non-save clauses, including
  action restrictions, movement, target-side attack advantage, nearby automatic
  critical hits, damage resistance, and immunity, keep their existing paths.
  Unconscious still implies Prone, whose own-attack disadvantage was already
  migrated.

This status describes which clauses have migrated; it does not claim that every
legacy clause is enforced. Existing sight-check and crawl implementation gaps
remain unchanged.

Update this page as engine migrations proceed. Engine migration status updates
do not require changes to the SRD schema docstring or canonical data.
