# Ongoing Magic actions and moving sources

Batch B4 implements Sunbeam's initial beam, later beams and following light in
grid combat. The reviewed source is [SRD 5.2.1](https://media.dndbeyond.com/compendium-images/srd/5.2/SRD_CC_v5.2.1.pdf),
printed p. 166; B7 adds Moonbeam from printed pp. 150–151. Host-owned components and the
existing two-dimensional grid boundary remain unchanged.

## Ownership and public contract

`PersistentAreaState` owns the ongoing source. Its existing record captures owner,
source identity, original slot level, casting ability, save DC, concentration
identity and duration. `PersistentAreaSpec.ongoing_activation` supplies a closed
canonical Activity allowlist and `magic_action` cost. There is no second ongoing
state container or duplicate timed-activity registration.

`get_live(handle).ongoing_spells` exposes immutable source/owner/spell identity,
allowed Activity IDs, cost and captured slot/DC. The source ID is an area instance
ID; recasting replaces it with a new ID, invalidating old activation requests.

```python
source = get_live(handle).ongoing_spells[0]
await submit_player_intent(
    handle, actor_id=source.owner_id,
    intent=PlayerIntent(
        intent_type="activate_spell", source_id=source.source_id,
        activity_id=source.activation.activity_ids[0], direction=(1, 0),
    ),
)
```

Owner, source existence, concentration, duration, canonical content admission,
Activity membership, geometry, targets and normal action budget are checked before
payment or RNG. Malformed or unsupported inputs raise typed `IntentRejectedError`;
schema violations remain Pydantic validation failures. Cast/slot/item/feature/check
inputs cannot be smuggled into activation. Incapacitated and out-of-turn requests
use the existing public gates. Rage ends concentration and removes the source.
Action Surge's restricted action cannot fund this Magic action.

An activation uses the shared Delivery Planner and Activity Resolver with the
source's captured spell data and current target defenses/positions. Its typed
`IntentSubmitted` names source and Activity. Damage retains canonical spell/Activity
provenance and the source instance as parent. It emits no new `SpellCast`, spends
no slot/charge, and does not recreate concentration or reset duration. Ordinary
Counterspell cast opportunities do not run; damage-reaction hooks still run.
Unexpected exceptions propagate after the existing complete transaction rollback.

## Sunbeam

Both beams are 60-ft by 5-ft Lines using the existing grid geometry and Total Cover
clipping. Each creature makes a CON save against the original DC: failure takes
6d8 Radiant and Blinded, success takes half damage only. Higher slots retain 6d8;
Sunbeam has no damage-upcast clause. The effect uses the existing exact
`source_next_turn_start` lifecycle and does not require concentration: dropping
concentration removes the source/light but does not prematurely clear Blinded.

The initial Activity resolves its beam and registers one following environmental
source. `source_radius_ft` separates that source's 30-ft Sphere from the beam's
Line. Bright Light extends 30 ft, with another 30 ft of Dim Light. Sunbeam's
explicit `sunlight_in_dim` marks both regions as Sunlight. Daylight's existing
Bright-only Sunlight contract is unchanged. Shared environment projection refreshes
after every committed voluntary or forced movement step, before area-entry
payloads; static scene data is never changed. Light does not damage creatures
entering its footprint. Concentration replacement/drop/expiry, death, departure
and Combat End remove source and projection through existing area lifecycle.

Surviving magical light can illuminate Darkness; only declared level-qualified
dispel contracts remove sources. Sunbeam does not acquire Daylight's dispel rule.
Fog still obscures sight. Vision and Sunlight consumers query the same projection.
Initial casting and activation retain movement continuation for the source owner.

## Admission, limits and evidence

Exact pinned translation verifies Sunbeam's IDs, geometry, save, damage, effect,
payment flags, duration and reviewed clauses. A tracked upstream YAML fixture
reproduces canonical data without ignored maintainer inputs. Semantic digests
include ongoing, environmental and lifecycle metadata. The repeat Activity still
fails ordinary cast admission; only the ongoing carrier can satisfy its mechanism.
The capability audit records both contracts.

Sunbeam remains **Bounded**: grid combat direct casting/activation is covered;
autonomous Monster AI source activation, item concentration ownership and persistent
out-of-combat clocks are not implemented.

## Moonbeam and relocation (B7)

Moonbeam is **Bounded** within the same 2D combat and Host component boundary.
The classification totals are 30 Executable, 48 Bounded, 26 Host Narrative and
235 Deferred. The pinned YAML lacks Cylinder height; the source-reviewed mapping
supplies the SRD's 40-ft height alongside its 5-ft radius. The grid projects a
single horizontal plane: the Host must ensure participants lie within that vertical
span. Altitude, vertical motion and multi-cell bodies remain unimplemented.

`OngoingActivationSpec.relocation` declares a positive maximum distance, destination
targeting, later-owner-turn restriction and existing Magic Action cost. Owner/source
identity and geometry come from the same `PersistentArea`. Public relocation uses
the existing `activate_spell` intent with `source_id`, its allowed `activity_id`
and `target_zone_id`. Creature selections, exclusions, direction and cast/slot
options are rejected. The destination may be occupied. Distance is measured from
the current area origin, with no renewed 120-ft casting range restriction.

```python
source = get_live(handle).ongoing_spells[0]
await submit_player_intent(
    handle, actor_id=source.owner_id,
    intent=PlayerIntent(
        intent_type="activate_spell", source_id=source.source_id,
        activity_id=source.activation.activity_ids[0], target_zone_id="14,5",
    ),
)
```

The Host positioning convention requires a legal canonical destination and line
of effect from both current origin and owner to destination; Total Cover blocks
either line. This is the engine's bounded relocation contract, not a separate SRD
visibility requirement. A relocation commits one destination and emits
`AreaRelocated`; transit cells are not area arrivals. Only creatures newly covered
by the destination make the area-entry save. Existing overlap does not create an
extra save, and every trigger shares the area's per-creature combat-turn gate.
Slot, DC, source ID, concentration and duration remain captured from the original cast.

Appearance, area arrival, creature entry (voluntary or forced), and creature turn
end force CON saves, once per combat turn; turn start does not. Damage is 2d10
Radiant at level 2 plus 1d10 per higher slot, half on success. Simultaneous
appearance/relocation targets share one damage roll (SRD p.16); separate entries
are separate resolutions. Overlapping copies retain their sources/clocks but
project the highest slot, then latest equal-slot source, for a covered creature
(Combining Spell Effects, SRD p.106). A separated cylinder still owns its own gate.

On a failed save, a target shape-shifted before damage reverts through the existing
authoritative transform expiry path. This also works when damage already depleted
Polymorph Temporary HP and ended the form. The producing area then prevents further
Wild Shape or existing reviewed transformation requests until the creature or
cylinder leaves that membership. A failed ordinary target gains no lock; a success
does not force reversion. Locks belong to each area, expire with it, and never
become surrogate transformation Conditions. This adds no new Polymorph admission.

The same area projects magical Dim Light through shared environment state.
Darkness can dispel a level-2 Moonbeam on overlap in either cast order or after
relocation; level-3+ Moonbeam survives and illuminates the overlap. Shared cleanup
removes damage triggers, shape locks, light and concentration together. Initial
appearance runs after the common concentration/result fold and overlap reconciliation,
so initial self-damage can break the actual new concentration without a later
anchor resurrecting it. Counterspell only has the original cast window.

Public regressions use natural initiative, real Wild Shape, public casts/movement,
forced Thunderwave movement and the existing authoritative Polymorph carrier seam.
Fault injection and same-seed replay compare all live authority, pending appearances,
trigger records, resources, event log/queue, topology and RNG. Monster AI declaration,
delegated item concentration and out-of-combat clocks remain deferred.

Public regressions cover first cast, repeat payment/DC/slot provenance, failed and
successful saves, exact blindness expiry, duration, movement, owner isolation,
Rage/Incapacitated/Action Surge, first-cast Counterspell versus later damage
reactions, identity tampering, lifecycle removal and deterministic replay. Fault
injection after real resolution, movement projection and concentration cleanup
compares complete state, topology, event queue/listeners and RNG. Existing B1/B2/B3
and shared resolver regressions remain in the Focused Integration gate.
