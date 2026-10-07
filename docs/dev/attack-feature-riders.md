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
`feature_id`, `activity_id`, and optional `push_distance_ft`. It does not accept a
host-supplied DC, damage bonus, resource price, trigger or execution phase.
The engine derives those values from the owning character and canonical typed
metadata. A character must have the feature in its authoritative repertoire.

## Resolution contract

1. **Preflight.** Validate ownership, option identity, attack kind, weapon and
   funding requirements, resource availability, once-per-turn limits and request
   multiplicity. Invalid declarations consume no action, Bonus Action, resource,
   effect or RNG state. They produce `AttackFailed(reason="unsupported_rider")`
   before `IntentSubmitted`.
2. **Final hit.** Roll the attack once and resolve hit-changing reactions,
   including Shield. A miss, including a hit converted to a miss by Shield,
   spends no conditional rider resource and rolls no rider save.
   Stunning Strike's `final_hit_before_damage` phase pays Focus and resolves its
   save/effect here, before weapon damage dice.
3. **Damage preparation.** Commit costs that require the real hit. Cunning-style
   options reserve and subtract their dice from the actual eligible Sneak Attack
   before any damage dice are rolled.
4. **Damage.** Resolve the weapon hit through the ordinary damage pipeline.
   Remaining Sneak Attack dice inherit the weapon's damage type and double on a
   critical hit. Commit Sneak Attack's once-per-turn state inside that damage
   resolution, before a Cleave continuation can select it again.
5. **After damage.** Resolve the selected Open Hand, Trip or Obscure option through
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

Open Hand options require the owning Monk subclass and paid Flurry provenance.
A normal Attack action or Martial Arts Bonus Unarmed Strike cannot invoke them.
Only the selected Open Hand outcome executes. Improved Cunning Strike permits
the canonical option count and charges every selected option; it does not turn
deferred options into executable ones.

Sneak Attack retains the shared Finesse/Ranged weapon, advantage or adjacent
qualifying ally, no-disadvantage and once-per-turn checks. Its cap is per turn,
not per round: an eligible opportunity attack on another creature's turn can
apply it again. A miss or an ineligible hit does not consume it.

## Deferred options

Each row remains fail-closed before attack payment. A supported effect fragment
does not make the complete option executable.

| Feature / option | Missing contract |
|---|---|
| Cunning Strike: Poison | An authoritative carried Poisoner's Kit requirement and the repeated end-of-turn save lifecycle. Applying Poisoned alone is insufficient. |
| Cunning Strike: Withdraw | Complete immediate movement up to half Speed and an opportunity-attack exemption scoped to that movement. The typed `MovementGrant` is a foundation; a persistent Disengage marker is insufficient. |
| Cunning Strike: standalone Sneak Attack damage activity | This is a data carrier for dice sacrifice, not permission to deal Sneak Attack damage without an eligible attack hit. |
| Devious Strikes: Daze | The target's next-turn choice of movement, Action or Bonus Action, allowing only one of those. An inert duration marker is insufficient. |
| Devious Strikes: Knock Out | One-minute Unconscious, removal on any damage, and a repeated end-of-turn save. A one-turn condition is insufficient. |
| Reckless Attack | The own-turn first-attack STR declaration, ongoing attack advantage, and advantage for incoming attacks until the source's next turn. |
| Brutal Strike: damage / Hamstring Blow | The complete Reckless Attack prerequisite, forgoing its advantage before rolling, no remaining disadvantage, qualifying STR attack, one selected hit and source-turn restriction. Hamstring's typed Speed reduction alone is insufficient. |
| Brutal Strike: Forceful Blow | Those Brutal Strike prerequisites plus the immediate half-Speed follow movement. A push alone is insufficient. |
| Improved Brutal Strike: Staggering Blow | Those Brutal Strike prerequisites plus a one-use next-save disadvantage and opportunity-attack suppression. |
| Improved Brutal Strike: Sundering Blow | Those Brutal Strike prerequisites plus a one-use `+5` attack bonus for the next **other** attacker. |

The full inventory also keeps Divine Strike's own-turn/choice scaling,
Primal Strike's weapon-or-Wild-Shape and damage choice, Eldritch Smite's pact
weapon/slot requirements, Lifedrinker's pact weapon/damage/Hit Die choices,
Frenzy's Reckless/Rage/first-target foundation, Colossus Slayer's validated
choice and missing-HP gate, and Repelling Blast's selected cantrip binding
deferred. Fire's Burn, Frost's Chill and Hill's Tumble need positive triggering
damage and their linked species resources. Shared size qualification alone does
not complete these riders: typed size metadata records Large-or-smaller for
Hill's Tumble and Repelling Blast, and Huge-or-smaller for Eldritch Smite.
Quivering Palm needs vibration binding and its later release lifecycle.
Hurl Through Hell needs banishment/return and the Fiend damage exclusion.
Each activity's exact missing clauses remain in the linked audit.

Stunning Strike and the listed Open Hand/Trip/Obscure activities still reject
standalone `use_feature`; their executable entrypoint is the bound attack.
The standalone [feature audit](feature-runtime.md) therefore distinguishes
standalone rejection from attack-rider execution.

## Audit and verification

[The deterministic rider audit](attack-rider-audit.json) inventories canonical
feature/activity options, typed triggers, qualifiers, costs, target roles,
phases, executable status and specific deferred reasons. Ingestion owns the
exact canonical mappings. Unknown declarations do not gain semantics by
matching human-readable text.

The inventory has 51 rows from 36 feature documents: seven executable riders,
25 deferred rider options and 19 supporting contexts. The seven executable rows
include automatic Sneak Attack. Funding producers, defensive reactions and
passive/foundation records are inventoried separately and are not counted as
newly executable outgoing riders.

`AttackRiderTriggered` records attacker, target, feature and option identities,
the triggering attack activity, phase, paid resource identities/prices,
sacrificed Sneak Attack dice and save outcome. Its event order follows the
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
