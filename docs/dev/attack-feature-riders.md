# Attack-triggered feature riders

An optional attack rider is declared with the attack that can trigger it. The
engine checks the declaration before spending the attack's action budget or
drawing dice, then uses the attack resolver's final result to decide whether the
rider triggers. This is a separate entrypoint from standalone `use_feature`.

```python
from dnd5e_engine import AttackRiderRequest, PlayerIntent

intent = PlayerIntent(
    intent_type="attack",
    weapon_id="unarmed-strike",
    target_id="mon:foe",
    attack_riders=(
        AttackRiderRequest(
            feature_id="stunning-strike",
            activity_id="Xto99a8Zt46VLwaR",
        ),
    ),
)
```

`AttackRiderRequest` is frozen and rejects extra fields. Its fields are
`feature_id`, `activity_id`, optional `push_distance_ft`, `option_ids` and
`movement_choice`. It does not accept a
host-supplied DC, damage bonus, resource price, trigger or execution phase.
The engine derives those values from the owning character and canonical typed
metadata. A character must have the feature in its authoritative repertoire.

## Resolution contract

1. **Preflight.** Validate ownership, option identity, attack kind, weapon and
   funding requirements, resource availability, once-per-turn limits and request
   multiplicity. Invalid declarations consume no action, Bonus Action, resource,
   effect or RNG state. They produce `AttackFailed(reason="unsupported_rider")`
   before `IntentSubmitted`.
2. **Pre-roll commitment.** After the complete advantage/disadvantage sources
   are composed, Reckless declaration and Brutal qualification resolve before
   the d20. Brutal rejects any disadvantage source, then forgoes all advantage
   for the chosen roll and commits its once-per-turn choice even on a miss.
   A rejected intent restores temporary payment, effects, events and RNG.
3. **Final hit.** Roll the attack once and resolve hit-changing reactions,
   including Shield. A miss, including a hit converted to a miss by Shield,
   spends no conditional rider resource and rolls no rider save.
   Stunning Strike's `final_hit_before_damage` phase pays Focus and resolves its
   save/effect here, before weapon damage dice.
4. **Damage preparation.** Commit costs that require the real hit. Cunning-style
   options reserve and subtract their dice from the actual eligible Sneak Attack
   before any damage dice are rolled.
5. **Damage.** Resolve the weapon hit through the ordinary damage pipeline.
   Remaining Sneak Attack dice inherit the weapon's damage type and double on a
   critical hit. Commit Sneak Attack's once-per-turn state inside that damage
   resolution, before a Cleave continuation can select it again. Canonical
   Brutal and automatic Frenzy damage contributions share the triggering hit's
   `damage_instance_id`. Each contribution obeys its activity's critical policy;
   neither canonical activity doubles its extra dice on a critical hit.
6. **After damage.** Resolve the selected Open Hand, Cunning or Brutal option through
   the shared `SaveActivity`/utility resolver and apply its typed outcome through
   combat events.
   Forced movement uses the shared push primitive and persistent-area entry
   hooks. Conditions, source identity and expiry use the normal effect lifecycle.

The shared attack context carries the actual funding origin, weapon properties,
governing ability, advantage and disadvantage sources, final hit and critical
result, turn serial, and actual Sneak Attack eligibility and dice. Rider runtime
code consumes this context; it does not infer a second result from the intent or
parse feature names, descriptions, qualifiers or activation prose.

## Executable options

| Feature / option | Canonical activity ID | Trigger and complete behavior |
|---|---|---|
| Stunning Strike | `Xto99a8Zt46VLwaR` | Once per turn, an Unarmed Strike or Monk-weapon hit can spend one Focus Point. The target makes a CON save against `8 + PB + WIS`. Failure imposes Stunned until the attacker's next turn starts. Success halves Speed and grants advantage on the next attack against that target, with the same source-turn expiry. The next matching attack consumes this one-use grant even if disadvantage cancels it or the roll misses. |
| Open Hand: Addle | `1jdSaWanuRrdkVs3` | A paid Flurry of Blows Unarmed Strike hit suppresses the target's opportunity attacks until that target's next turn starts. There is no save and no extra Focus charge. |
| Open Hand: Push | `XoaS0RtDCGAqrQsf` | On that paid Flurry hit, a failed STR save against `8 + PB + WIS` pushes the target away by the declared `push_distance_ft`: 0, 5, 10 or 15 feet. Forced movement spends no movement budget and provokes no opportunity attack; area-entry effects still run. |
| Open Hand: Topple | `5Qgc0K3TfuonkPIG` | On that paid Flurry hit, a failed DEX save against `8 + PB + WIS` imposes Prone through the ordinary condition/effect events. |
| Cunning Strike: Trip | `dWcCw1vTWRMx4YzD` | An eligible Sneak Attack sacrifices one d6 against a Large-or-smaller target. A failed DEX save against `8 + PB + DEX` imposes Prone. The sacrifice remains paid on a successful save; a larger target is refused before attack payment or dice. |
| Devious Strikes: Obscure | `ki4lIPVGNA0HjEzH` | An eligible Sneak Attack sacrifices three of its d6s. A failed DEX save against `8 + PB + DEX` imposes Blinded until the end of the target's next turn. The sacrifice remains paid on a successful save. |
| Cunning Strike: Poison | `n64fvJMT9fPUy7DH` | A carried canonical `poisoners-kit` and eligible Sneak Attack are required before payment. Sacrifices one d6; failed CON against `8 + PB + DEX` imposes Poisoned for ten rounds with captured end-turn repeats. Successful saves still pay the sacrifice; immunity suppresses condition/lifecycle registration. |
| Devious Strikes: Knock Out | `3eq7lcmpkJJBU2KO` | Sacrifices six eligible Sneak Attack d6s; failed CON against `8 + PB + DEX` imposes Unconscious for ten rounds with captured end-turn repeats, ending after a complete positive damage instance. Zero, immune and Shield-negated damage do not break it. |
| Cunning Strike: Withdraw | `m2bRZ1YeD3yf9nV7` | Sacrifices one eligible Sneak Attack d6; immediately after damage, optional declared walking movement uses an independent half-effective-Speed allowance in any legal direction. No opportunity attacks apply only during this grant. A miss pays no sacrifice; choosing zero movement on a hit still pays one die. |
| Brutal Strike: Forceful / Hamstring | `nN5gsB6AcSQ4uQPN` | One own-turn STR weapon or Unarmed Strike roll while Reckless is active can commit before rolling, forgoing all advantage and requiring no disadvantage sources. A final hit adds canonical scaled damage once, inheriting the base damage type, then executes selected owned options. |
| Improved Brutal Strike: Staggering | `I30qGlPDcyKwz65H` | Adds `staggering-blow` to that option pool: next save has disadvantage and opportunity attacks are suppressed until the source's next turn starts. Consuming the next-save clause preserves opportunity-attack suppression. |
| Improved Brutal Strike: Sundering | `UmRlsf4QWW98I4FS` | Adds `sundering-blow`: the next attack by another creature gains +5 until source next-turn start. Source attacks neither benefit nor consume; hit or miss consumes the applicable bonus clause. Multiple grants give at most +5 and preserve unselected grants. |
| Berserker Frenzy | `myPBq8xozti108Mc` | Automatic first qualifying own-turn STR weapon or Unarmed Strike final hit while real Rage and Reckless are active; adds `(@scale.barbarian.rage-damage)d6` of the inherited damage type in the same damage instance. Misses and nonqualifying hits preserve its use. |

Open Hand options require the owning Monk subclass and paid Flurry provenance.
A normal Attack action or Martial Arts Bonus Unarmed Strike cannot invoke them.
Only the selected Open Hand outcome executes. Improved Cunning Strike permits
the canonical option count and charges every selected option; it does not turn
deferred options into executable ones.

Sneak Attack retains the shared Finesse/Ranged weapon, advantage or adjacent
qualifying ally, no-disadvantage and once-per-turn checks. Its cap is per turn,
not per round: an eligible opportunity attack on another creature's turn can
apply it again. A miss or an ineligible hit does not consume it.
Its typed `native_damage` metadata assigns damage ownership to the existing
shared Sneak Attack resolver; the automatic contribution planner skips that
native producer, so it neither duplicates damage nor charges a second use.

## Reckless declaration and Brutal options

Set `PlayerIntent.reckless_attack=True` on the actor's first actual attack roll
of its own turn. The count includes weapon, Unarmed Strike, spell, construct,
Nick and chained attack rolls; it resets at that actor's turn start. Off-turn
rolls do not block the next own-turn declaration. A first DEX or INT roll may
declare Reckless without gaining STR-only advantage itself. The exact canonical
effect grants advantage to all bearer STR attack rolls and all incoming attack
rolls until the source's next turn starts. Saves are unaffected. The effect
persists after a miss, including a Shield-converted miss.

The typed option pool contains `forceful-blow` and `hamstring-blow` from Brutal
Strike, plus `staggering-blow` and `sundering-blow` from Improved Brutal Strike.
Base capacity is one; owning Improved Brutal Strike (2) grants two **different**
options through canonical choice-limit metadata. Two options still share one
damage contribution, and option execution follows request order. Scaling reads
the canonical Barbarian table, including 2d10 at level 17.

```python
from dnd5e_engine.movement import MovementChoice

intent = PlayerIntent(
    intent_type="attack",
    weapon_id="greataxe",
    target_id="mon:foe",
    reckless_attack=True,
    attack_riders=(AttackRiderRequest(
        feature_id="brutal-strike",
        activity_id="nN5gsB6AcSQ4uQPN",
        option_ids=("forceful-blow",),
        movement_choice=MovementChoice(distance_ft=10),
    ),),
)
```

Forceful pushes a fixed 15 feet straight away through the shared push path,
then may grant walking movement straight toward the target's actual post-push
cell. Hamstring reduces every effective movement speed by 15 feet; the latest
typed target-group application replaces the previous one across all sources.
Hamstring uses source-next-turn-start lifecycle metadata. Sundering selects
the oldest applicable grant in canonical insertion order, gives only its +5,
and consumes only that grant's attack-bonus clause. Staggering's next-save
disadvantage is consumed even by an automatic failure.

Withdraw and Forceful share the scoped [movement executor](movement-positioning.md).
The grant cap is half current effective walking Speed, independent of remaining
ordinary movement and Dash. Terrain and grapple drag charge the grant; it does
not pay or replenish the turn's ordinary movement ledger. A later normal move
can provoke opportunity attacks.

## Deferred options

Each row remains fail-closed before attack payment. A supported effect fragment
does not make the complete option executable.

| Feature / option | Missing contract |
|---|---|
| Cunning Strike: standalone Sneak Attack damage activity | This is a data carrier for dice sacrifice, not permission to deal Sneak Attack damage without an eligible attack hit. |
| Devious Strikes: Daze | The target's next-turn choice of movement, Action or Bonus Action, allowing only one of those. An inert duration marker is insufficient. |

The full inventory also keeps Divine Strike's own-turn/choice scaling,
Primal Strike's weapon-or-Wild-Shape and damage choice, Eldritch Smite's pact
weapon/slot requirements, Lifedrinker's pact weapon/damage/Hit Die choices,
Colossus Slayer's validated choice and missing-HP gate, and Repelling Blast's selected cantrip binding
deferred. Fire's Burn, Frost's Chill and Hill's Tumble need positive triggering
damage and their linked species resources. Shared size qualification alone does
not complete these riders: typed size metadata records Large-or-smaller for
Hill's Tumble and Repelling Blast, and Huge-or-smaller for Eldritch Smite.
Quivering Palm needs vibration binding and its later release lifecycle.
Hurl Through Hell needs banishment/return and the Fiend damage exclusion.
Each activity's exact missing clauses remain in the linked audit.

Stunning Strike and the listed Open Hand/Cunning/Devious/Brutal/Frenzy activities still reject
standalone `use_feature`; their executable entrypoint is the bound attack.
The standalone [feature audit](feature-runtime.md) therefore distinguishes
standalone rejection from attack-rider execution.

## Audit and verification

[The deterministic rider audit](attack-rider-audit.json) inventories canonical
feature/activity options, typed triggers, qualifiers, costs, target roles,
phases, executable status and specific deferred reasons. Ingestion owns the
exact canonical mappings. Unknown declarations do not gain semantics by
matching human-readable text.

The inventory has 51 rows from 36 feature documents: 15 executable riders,
17 deferred rider options, two executable foundations and 17 supporting contexts.
The executable riders include automatic Sneak Attack and Frenzy. Reckless and
Improved Brutal Strike (2) are executable foundations with no standalone activity.
Funding producers, defensive reactions and passive contexts remain separate.

`AttackRiderTriggered` records attacker, target, feature and option identities,
the triggering attack activity, phase, paid resource identities/prices,
sacrificed Sneak Attack dice, selected option IDs, shared damage activity/formula
and save outcome. Its event order follows the
phases above; the selected option's effect remains an ordinary `ActiveEffect`.
If an unexpected resolver `ValueError` occurs after conditional payment, the
public intent transaction restores resources, budgets, effects, RNG and the
event stream before propagating the error. Partially resolved events are not
delivered to host listeners.

[`test_attack_rider_runtime.py`](https://github.com/tapestria/nat20/blob/main/packages/dnd5e-engine/tests/test_attack_rider_runtime.py)
exercises the public attack declaration, authoritative class/subclass grants,
preflight state preservation, final-hit reaction ordering, save outcomes,
resource and Sneak Attack commits, effect expiry, forced movement and replay
determinism. The capability matrix probes the same typed support boundary.

Poison and Knock Out use the shared [typed effect lifecycle](effect-lifecycle.md),
which captures initial save ability/DC and source provenance, preserves full
effect identities and owns repetition, finite duration and damage expiry.
