# Feature runtime boundary

`use_feature` now builds a priced invocation before it changes the Action,
Bonus Action, Reaction, feature counters, spell slots, conditions, effects,
position or RNG. Missing carriers and deferred semantics emit
`CastFailed(reason="unsupported_feature")`. Existing range, target, economy
and exhausted-pool failures retain their more specific reasons.

## Reproducible corpus audit

Run from the repository root:

```console
uv run python -m dnd5e_engine.feature_audit --output docs/dev/feature-runtime-audit.json
uv run pytest packages/dnd5e-engine/tests/test_feature_runtime_safety.py
```

The [checked-in audit](feature-runtime-audit.json) enumerates **191 reachable
activities across the 261 canonical feature documents**. It chooses an actual
granting class, subclass or species at level 20, and includes reached feature
choice pools. The classifications are:

| Classification | Activities | Live behavior |
|---|---:|---|
| `fully_resolvable` | 17 | The supported invocation passes pure preflight; normal live target/economy gates still apply. |
| `unsupported_preflight` | 44 | Refused before payment or draws: unsupported operation, formula/carrier or resource shape. |
| `semantic_special_case` | 99 | Refused before payment or draws: missing triggering event, lifecycle or action semantics. |
| `attack_rider_executable` | 6 | Standalone invocation refuses; the authoritative attack path supports the rider. |
| `attack_rider_deferred` | 25 | Standalone invocation refuses; the attack option is explicitly deferred with its missing clauses. |

Each row records the feature slug, activity ID, actual granting owner, reason,
and independent formula/resource diagnostics, plus `execution_path`,
`standalone_executable` and `rider_executable`. The latter diagnostics are collected even for
semantically rejected activities. The audit is a level-20 coverage fixture, not
a proof of every possible actor/target state. Live tests run **all 191 rows**;
additional tests vary class levels, multiclass ownership, selected choices,
targets, conditions, malformed data and resource balances.

The old backlog number of 29 was stale. Before this change, a replay of the
existing numeric resolver over the 77 damage/heal/save feature activities at
`95e0157b6afdc85080125d2cf9e02e030e0b7517` found **20 numeric failures**.
That was a resolver-level count, not 20 demonstrated public API exceptions:
some choice features could not yet reach the live gate. Ability-code DCs,
class levels and scales already worked. Remaining failures involved missing
owner spellcasting context, source uses, trigger-dependent tokens and arithmetic.
The new full audit also examines utility/check payloads and type/resource
semantics, so its diagnostic totals are not the same metric.

## Build and ownership contract

`build_party_member` copies `DerivedSheet.features` into
`PartyMemberSpec.granted_features`, then combat hydration preserves the tuple.
`None` retains the legacy fixed-grant behavior; `()` explicitly grants nothing.
A supplied tuple is the complete projection: it can omit grants, but cannot
add an unreached feature or exceed typed choice-pool capacity. Fixed grants
keep owner order, selected choices keep their order, and duplicates collapse.
Prerequisites absent from canonical data are not inferred from descriptions.

`FeatureOwner` comes from granting documents. A Cleric feature in a
Wizard/Cleric build uses the Cleric's Wisdom and class level. A subclass uses
its actual `class_identifier`, not its source directory or the primary class.
Selected features also contribute their own scale tables at their owner's level.

## Preflight and commit

`feature_runtime.py` owns immutable invocation/payment plans and pure
validation. `live_features.py` owns live target checks and action delegation.
Preflight validates selection, activation, resource identity/cost/cap,
spellcasting context, scalar save/check DCs, every executed damage/heal formula
and referenced effect. Live bonus expressions are checked before payment too.
Its RNG object raises if validation tries to draw.

The activity layer still uses `resolve_roll_data` for substitution. Its shared
integer/dice parser accepts `+`, `-`, `*`, parentheses, unary signs, dice, and
`min`/`max`; it does not evaluate Python or arbitrary functions. It parses the
whole expression before drawing, and evaluates dice left to right. `SourceUses`
supplies precommit spent/current/maximum values for `@item.uses.*`; formula code
does not import live state. Trigger-dependent `@scaling`, Hit Die and pact-slot
values stay unsupported when the invocation has no corresponding carrier.

`ResourcePayment` resolves own-pool, `feat:<slug>` and unambiguous full Foundry
feature references. All named pools must belong to the actor. Duplicate costs
are summed, every pool is checked before commit, and each pool is spent once.
Non-feature resource types, recovery/conversion activities and activity-local
pools require separate typed semantics and currently reject.

Unexpected resolver `ValueError` is logged, rolled back and re-raised. Feature
events are buffered until the invocation returns; the fallback restores the
live state and the original RNG's state, so subscribers see no partial events.
It does not turn programmer defects into rules refusals or catch all exceptions.

## Data bindings and supported shared fixes

`Feature.runtime_operations` and `Feature.target_rules` are closed typed
carriers. `tools/translators/feature_runtime.py` supplies exact audited
slug/activity-ID mappings; runtime does not parse descriptions. Empty fields
are omitted from serialization to keep unrelated canonical documents stable.

| Operation | Result |
|---|---|
| Lay on Hands Remove Poison | A touch-range Bonus Action spends five pool points and emits authoritative `ConditionRemoved(all_sources=True)` when Poisoned is present. It clears every Poisoned source while preserving each effect's other statuses and changes. Heal remains amount-scaled. |
| Patient Defense | The free variant performs real Disengage; the one-Focus variant performs real Disengage and Dodge. Both use the shared action handlers. |
| Adrenaline Rush | The same shared Dash mechanic adds movement, then the typed heal activity grants temporary HP. |
| Cunning Action | Canonical `use_feature` activities reject. Use the existing `dash`, `disengage` and `hide` intents with `use_bonus_action=True`. |
| Rage | Heavy armor prevents entry and ends an active Rage on armor changes; entry drops concentration, casts/readied spells and queued spell reactions are blocked, and the duration cap is 100 rounds. Existing extension/incapacitation mechanics remain authoritative. |
| Bardic Inspiration grant | Range and the recipient's ability to see or hear the bard are checked before payment. Existing redemption limits remain. |
| Divine Spark | Uses Cleric spellcasting context and requires another visible creature in range. |
| Wholeness of Body | The canonical formula now includes its minimum of one HP, evaluated by the shared `max` grammar without extra draws. |

Exact ingestion corrections also change Lay on Hands' inherited `self` range
to touch and Rage's conflicting 10-round/600-second effect to 100 rounds.

Divine Order Thaumaturge and Primal Order Magician's transfer effects project
their structured skill bonuses into `DerivedSheet.skill_check_bonuses` and the
shared live check modifier. The bonus applies to the skill's normal governing
ability. Primal Knowledge needs a Rage-conditioned ability substitution carrier
and remains deferred. A scan of current canonical effects found **zero
`system.bonuses.heal.*` producers**; no speculative heal-bonus consumer was added.

## Deliberate remaining limits

- **Attack-bound execution:** standalone Stunning Strike, Sneak Attack, Open Hand
  Technique and Devious Strikes Obscure continue to reject. Their typed attack
  path is described in [attack feature riders](attack-feature-riders.md) and the
  [option audit](attack-rider-audit.json). This distinction prevents standalone
  damage, saves or resource payments from bypassing an actual qualifying hit.
- **Step of the Wind:** the corpus includes only the Focus variant. Combined
  Dash/Disengage plus doubled jump distance lacks a full carrier, so it rejects;
  no free variant was invented.
- **Remaining optional riders:** Cunning Strike Poison/Trip/Withdraw, Devious
  Strikes Daze/Knock Out, Reckless/Brutal Strike and other corpus riders remain
  deferred per option. Obscure is a Dexterity save ending at the end of the
  target's next turn; one supported option does not imply a complete feature.
- **Preserve Life:** formula support does not provide pool division, self
  inclusion or a half-maximum HP cap; the invocation rejects.
- **Breath Weapon / Intimidating Presence:** Attack replacement and ancestry-bound
  damage, or repeated saves/condition duration, respectively, remain deferred.
- **Persistent Rage recovery and Font/Superior Inspiration:** initiative/rest
  triggers remain deferred. The separate [reaction subsystem](reaction-queue.md)
  provides typed pre-arm auto-fire. Bardic Inspiration's save
  and spell-attack redemption also remain deferred.

The corpus test asserts byte-equivalent serialized rules state and RNG for
refusals, allowing only the rejection event. Targeted tests cover actual build
choices, multiclass DC and draw order, real action state, authoritative removal,
resource exhaustion and injected post-payment resolver failures.
