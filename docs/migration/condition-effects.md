# Condition effect migration status

This page records the engine's migration from condition-name rules to typed
canonical clauses. Keep engine migration status here; the SRD data schema
documents the data contract and its sources.

## Current declarative clauses

| Condition | Migrated clauses |
|---|---|
| Poisoned | `disadvantage_own_attacks`, `disadvantage_ability_checks` |
| Restrained | `disadvantage_own_attacks` |
| Blinded | `disadvantage_own_attacks` |
| Prone | `disadvantage_own_attacks` |

## Execution path

```text
canonical condition data
→ ConditionEffect
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

- Restrained: speed zero, advantage on attacks against the target, and DEX save
  disadvantage.
- Blinded: sight-check automatic failure and advantage on attacks against the
  target.
- Prone: crawl movement restrictions, advantage on attacks against the target
  within 5 ft, and disadvantage on attacks against the target beyond 5 ft.
  Unknown distance leaves the target attack rule inert.

This status describes which clauses have migrated; it does not claim that every
legacy clause is enforced. Existing sight-check and crawl implementation gaps
remain unchanged.

Update this page as engine migrations proceed. Engine migration status updates
do not require changes to the SRD schema docstring or canonical data.
