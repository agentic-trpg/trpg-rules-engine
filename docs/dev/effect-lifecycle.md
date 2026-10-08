# Typed effect lifecycle and conditional expiry

Lifecycle behavior belongs to the exact canonical effect binding. A failed
save, concentration flag, condition name or display duration does not grant a
repeat save. Runtime code does not interpret descriptions or activation prose.

The initial architecture audit separates ownership explicitly:

| Existing component | Responsibility after this change |
|---|---|
| Condition lineage | `conditions_by_effect` preserves full effect identity and removes a condition only when its final source ends. It does not discover repetition. |
| Concentration chain and cap | Existing cast/drop behavior remains authoritative; target lifecycle expiry prunes that target and clears the final caster token without fabricating a concentration drop. |
| Timed activities and persistent areas | Their typed producers schedule damage/saves, placement and entry independently; they reuse the shared resolver and source cleanup. |
| Shield | Its existing reaction and owner-next-turn-start expiry adapter remains intact. |
| Stunning Strike / Open Hand / Obscure | Existing typed rider boundary adapters remain intact; generic consumption preserves their unrelated clauses. |
| Typed effect lifecycle | Owns only explicitly bound repeat saves, finite caps, exact boundaries, complete-instance damage breaks and one-use state. |

## Contracts and registration

An `AppliedEffectRef.lifecycle` or attack rider's exact effect binding carries
the closed `EffectLifecycleSpec`. Its independent clauses are:

- `repeat_save`: each target turn end, using the triggering activity's captured
  ability and DC, with successful saves expiring that effect.
- `maximum_rounds`: a reviewed finite combat duration.
- `expiry_boundary`: the source's or target's next turn start or end.
- `expire_on_positive_damage`: removal after a complete positive damage instance.
- `one_use_modifiers`: typed next-save disadvantage or other-creature next-attack
  bonus consumption.
- `stacking` and `stacking_group`: an exact latest-only target group across sources.
- `next_attack_scope` and `next_attack_bonus_group`: an other-creature attacker
  qualifier and stable nonstacking bonus selection group.

`EffectLifecycleApplication` captures source actor, source kind and canonical
slug, activity ID, initial save ability/DC and magical provenance. The applied
`ActiveEffect` carries this application; registration does not consume RNG.
Conditions suppressed by immunity do not register a repeating condition effect.

`live.effect_lifecycles` maps the full identity
`(target_id, effect_id, origin)` to immutable `OngoingEffectLifecycle` state.
The state records application turn/round, expiry round or actor, the last repeat
turn serial and remaining one-use modifiers. Two sources of the same effect
retain independent registrations and condition lineage.

## Turn boundaries and duration

Each target turn end permits one repeat save, including the end of a turn in
which the effect was applied. The turn serial prevents duplicate processing of
the same boundary. Exact **next** turn start/end expiry instead requires a later
turn serial. These are separate contracts.

A finite `N`-round cap expires at the target's turn end when
`round_number >= applied_round + N`. This combat rounding convention does not
count the partial application round as a complete round. A reviewed one-minute
effect therefore uses ten rounds, even when Foundry's display effect says one
round. Repeat saves resolve before the duration cap at the same boundary.
Legacy display-duration ticking skips effects owned by this typed registry.

Turn-end event order is the phase marker, `SaveRolled`, optional
`LegendaryResistanceUsed`, `EffectExpired(reason="save_succeeded")`, then removal
of conditions whose final source ended. A failed repeat leaves the registration
and condition in place. Duration expiry uses `reason="duration"`.

The original save DC and ability remain fixed. Fresh target defenses, effects,
conditions and Legendary Resistance state are read at each repeat. Changing the
source's ability score or proficiency later cannot change the captured DC.

## Shared saving throws and one-use modifiers

The initial audit found several handwritten save implementations. Their live
entrypoints now share `activities.save_primitive.roll_save` through
`live_effect_lifecycle.roll_live_save`, where appropriate:

| Surface | Shared behavior and source provenance |
|---|---|
| Spell, item-cast and monster `SaveActivity` | Resolved per-ability modifier, proficiency, condition auto-fail/advantage/disadvantage, Exhaustion, effect bonus dice and armed Legendary Resistance. Spell provenance includes cantrips. |
| Feature `SaveActivity` and attack rider save | Same mechanics; ordinary feature saves are nonmagical. Activity kind alone never enables Magic Resistance. |
| Typed repeat save | Captured DC, ability and magical provenance; current target defenses and the same seeded draw order. |
| Grapple/Shove target save | Shared mechanics with the existing STR/DEX choice and DC; nonmagical. |
| Damage-triggered concentration check | Shared CON save mechanics and `ConcentrationCheck` event; maintaining concentration is not itself a magical hostile save. |
| Undead Fortitude | Shared CON save, effect bonuses, Exhaustion and armed Legendary Resistance; nonmagical even if incoming damage came from a spell. |
| Death saving throw | Its existing special success/failure state machine and Exhaustion remain; it consumes generic next-save disadvantage through the same callback. |
| Standalone legacy `check.resolve_check(kind="saving_throw")` | Separate pure API; no live effect-state consumption callback. Its migration remains deferred. |

Magic Resistance grants advantage only for a spell or explicit magical
provenance. Repeating an ordinary Poison/Knock Out feature effect does not become
magical because it uses `SaveActivity`. Legendary Resistance remains host-armed;
successful saves do not spend it.

`flags.save.next_disadvantage` applies to the saving creature. The live callback
consumes it once when the actual saving throw occurs, before any d20 draw.
Advantage cancellation, success, failure and automatic failure all consume it.
Automatic failure draws no dice and reports no rolled advantage mode.
Disabled effects and false or incorrectly modeled clauses are inert.

`EffectModifiersConsumed` names the full effect identity and consumed key.
Only that one-use clause is removed: opportunity-attack suppression, Speed
changes, conditions and timers remain. Consumption adds the `"effect"` save
provenance token. Staggering Blow is a reviewed producer of this clause and
retains its opportunity-attack suppression after the next save.

Sundering uses `next_attack_bonus_other_creature` with the exact effect source
as the excluded attacker and a typed nonstacking bonus group. Source attacks
neither gain nor consume it. Another creature's next attack gains +5, then
consumes only the selected clause even on a miss. When several grants apply,
stable insertion order selects one; the others remain available.

## Verified producers

| Source | Lifecycle and complete entrypoint |
|---|---|
| Hold Person / Hold Monster | Their failed initial WIS save attaches typed repetition at each target turn end, with captured spell provenance and concentration cleanup. |
| Cunning Strike: Poison | An eligible attack with a carried canonical `poisoners-kit` sacrifices one Sneak Attack die. Failed CON against `8 + PB + DEX` imposes Poisoned for ten rounds with end-turn repeats; success still pays the sacrifice. |
| Devious Strikes: Knock Out | An eligible attack sacrifices six Sneak Attack dice. Failed CON against `8 + PB + DEX` imposes Unconscious for ten rounds with end-turn repeats and complete-instance break-on-positive-damage. |
| Intimidating Presence | Bonus Action, selected targets in a 30-foot Emanation, WIS against `8 + PB + STR`, Frightened for ten rounds with end-turn repeats; its own once-per-Long-Rest use is authoritative. The separate Rage Recharge activity remains deferred. |
| Reckless Attack | The typed attack declaration applies STR-only outgoing and all incoming attack advantage until the source's next turn starts. |
| Hamstring Blow | All effective speeds lose 15 feet until source next-turn start; the latest application replaces the target's older typed stacking-group effect across all sources. |
| Staggering Blow | Next-save disadvantage is consumed independently of OA suppression; both expire at source next-turn start. |
| Sundering Blow | Next other-creature attack gets a single +5 from the stable selected grant; its bonus clause consumes independently, with source-next-turn-start expiry. |

Carried item slugs are stable, deduplicated canonical identities from build
equipment through derived/live state. This narrow carrier provides Poison's
kit prerequisite; it does not implement inventory quantities, poison crafting
or doses. Misses and reaction-converted misses pay no rider sacrifice. Immunity
can suppress the condition and registration after the actual save and cost.

## Damage and cleanup

Break-on-damage uses a complete damage instance, after its typed damage parts
have folded. Zero damage, immunity and Shield-negated damage do not break an
effect. A multipart instance produces one expiry and one sourced condition
cleanup; the attack still sees Unconscious for hit/critical determination before
the damage removes that condition. `EffectExpired(reason="damaged")` records
this cause.

All expiry routes forget the same full identity idempotently. Expiring one
target prunes only that target from a concentration chain. Other affected
targets keep the chain; ending its final entry clears timers without inventing
a `ConcentrationDropped` event and clears the caster's concentration token.
Concentration loss still uses its existing
cascade. Source/target boundary effects, Shield and timed activities retain
their own explicit contracts.

Target death or departure removes its lifecycle state. A source departure only
ends a lifecycle with an explicit source-bound next-turn boundary, or effects
ended by the existing concentration cascade;
non-concentration effects such as Poison keep their independent target timer.

## Audit and verification

Run from `packages/dnd5e-srd-data`:

```console
uv run python -m tools.effect_lifecycle_audit --output ../../docs/dev/effect-lifecycle-audit.json
```

The [deterministic inventory](effect-lifecycle-audit.json) has 188 rows:
133 spell, 17 feature and 38 item rows. Seven bindings have executable typed
lifecycles; 37 typed duration/overlap bindings are supported, including B4's two
Sunbeam source-next-turn-start bindings. The other 144 rows
are explicit deferred or candidate records: 39 repeat-save candidates,
23 damage-break candidates and 84 other deferred rows. Candidate discovery during ingestion does not
authorize runtime execution. Dominate variants' damage-triggered escape saves
do not become end-of-turn repeats.

There are five typed repeat producers, one typed positive-damage break producer,
31 reviewed finite-duration producers and two canonical one-use producers:
Staggering and Sundering. Reckless and Hamstring carry exact next-turn boundaries;
Hamstring additionally declares its latest-only stacking group.

Batch B2 adds 28 canonical effect-choice bindings across Guidance, Enhance Ability
and Protection from Energy. Their `latest_applies` groups project only the latest
equal-potency spell on a target while retaining older casts and their independent
concentration/duration ownership. An older effect resumes when the newer one ends.
The finite caps are 10 rounds for Guidance and 600 for the other two spells;
direct casts also retain the existing caster-owned concentration cap. Public
selection and consumer acceptance tests cover these bindings; this inventory
alone does not establish whole-spell execution support.

`test_typed_effect_lifecycle.py` covers registration, exact source identity,
repeat success/failure, captured spell DC and magic, Legendary Resistance,
duration, complete damage instances, departure and deterministic replay.
`test_one_use_save_modifiers.py` covers the shared primitive and real spell,
monster repeat, Grapple/Shove, concentration and dying-turn boundaries, plus
Undead Fortitude. Five older repeat-state suites now construct typed
registrations rather than the removed list-of-dictionaries sidecar.
