# Dynamic environmental sources

Batch B3 adds three **bounded point operations**, exercised through public
`submit_player_intent()`: Fog Cloud, Darkness and Daylight. B8 also executes the
reviewed Darkness/Daylight object modes through a minimal typed Host carrier. The reviewed rules are
[SRD 5.2.1](https://media.dndbeyond.com/compendium-images/srd/5.2/SRD_CC_v5.2.1.pdf),
printed pp. 11 (vision), 122 (Darkness/Daylight), 133 (Fog Cloud), 177
(Area of Effect/Blindsight), 181 (Emanation), 182 (Heavily Obscured), and 190 (Truesight).
The matching official 2024 [spell descriptions](https://www.dndbeyond.com/sources/dnd/br-2024/spell-descriptions#Darkness)
and [Emanation glossary](https://www.dndbeyond.com/sources/dnd/br-2024/rules-glossary#Emanation)
were independently checked for the unattended-object requirement, origin exclusion,
movement and opaque covering.

## Authoritative ownership and data

`EnvironmentalSpec` is a closed, frozen canonical contract attached to
`PersistentAreaSpec`. It declares fog, magical darkness or light, illumination,
physical obscurement, Sunlight, Dim extension, per-slot radius increase,
cross-spell dispel thresholds and strong-wind susceptibility. Exact translator
mappings verify activity identity, geometry, range, casting time, spell level,
duration, concentration, payload and reviewed source clauses. Runtime never
parses descriptions or dispatches on spell slugs. Existing semantic admission
digests include this metadata; changing it invalidates the review.

`PersistentAreaState` remains the sole owner of geometry, duration and
concentration links. `GridTopology.environment_sources` is an immutable derived
projection; `get_live().environment_sources` exposes a stable Host snapshot with
source/area/caster identity, actual spell level, origin/radius, footprint, duration
and concentration identity. Static scene lighting/obscurement is never rewritten.
Removing one source recomputes the remaining projection.

B4 adds following sources through the same area ownership and projection:
Sunbeam's 30-ft Bright +30-ft Dim light follows its caster and marks both regions
as Sunlight. `sunlight_in_dim` is explicit; the Daylight contract below is unchanged.
Voluntary and forced movement refresh source origin and both footprints after each
committed step. See [ongoing activation](ongoing-spell-activation.md).

## Supported operations and preflight

- Fog Cloud: stationary 20-ft Sphere, +20-ft radius for each slot above 1,
  physical Heavy Obscurement, concentration up to 600 combat rounds.
- Darkness: stationary 15-ft Sphere, magical Darkness, concentration up to
  100 combat rounds, no ordinary Darkvision or nonmagical illumination.
- Daylight: stationary 60-ft Bright Sphere plus a further 60-ft Dim shell.
  The Bright Sphere is Sunlight; the additional Dim illumination is not marked
  Sunlight. No concentration; 600-round lifetime.

Declare an explicit `target_zone_id` within canonical range and line of effect.
Creature targets, target lists/exclusions, directional modes, blocked or
out-of-bounds origins, and absent point carriers are refused before Action,
Slot, concentration replacement, reaction opportunity or RNG. Canonical area
rasterization and Total Cover clipping are shared with existing area delivery.
Areas may include occupied cells and empty cells. Caster movement does not move
the source in point mode. Point modes reuse existing movement and action economy.

```python
await submit_player_intent(
    handle, actor_id="char:caster",
    intent=PlayerIntent(
        intent_type="cast_spell", spell_id="fog-cloud",
        slot_level=2, target_zone_id="8,0",
    ),
)
```

Darkness/Daylight additionally accept exactly one `target_object_id` instead of
a point; see the typed carrier below. Monster AI does not synthesize point carriers for environmental
operations; it retains uses and may choose an existing legal fallback. Delegated
Daylight supports point and typed object contracts; delegated concentration spells retain
the existing item-ownership refusal. General Dispel Magic is not implemented here.

## Composition and sight conventions

Surviving illumination combines by brightness, never insertion order. Physical
fog/foliage remains opaque regardless of light or Sunlight. Magical Darkness
excludes static/nonmagical light. A surviving magical light can illuminate its
overlap because Darkness forbids **nonmagical** light; the source itself is not
removed unless a reviewed dispel condition applies. There is no invented
"highest spell level always wins" rule.

Darkness dispels overlapping spell-created Bright/Dim sources whose **actual
cast level** is at most 2. Daylight dispels overlapping magical Darkness sources
whose actual cast level is at most 3. Those fixed thresholds do not increase
with the dispelling spell's slot. Any affected source is removed in its entirety,
with its concentration anchor if present. Both cast orders behave identically.
Darkness checks actual emitted Bright/Dim cells. Daylight checks its primary
60-ft Sphere/Emanation only, excluding its additional 60-ft Dim shell. This is a
source-based adjudication: the text defines the spell area as the 60-ft geometry
and separately says that area sheds additional Dim Light; it does not literally
state a separate Dim-dispel prohibition. Both modes/orders, tangency at 75ft
between 60-ft Daylight and 15-ft Darkness, the first 5-ft gap, and the outer Dim
boundary are pinned by reproducible grid tests. Clipped footprints govern overlap,
not merely origin distance. Higher-level sources that fail a threshold remain independently
owned and reappear when illumination ends.

The SRD calls heavy fog opaque but does not specify grid ray rasterization.
This Host convention uses the existing inclusive Bresenham sight ray: physical
Heavy Obscurement anywhere on the ray blocks ordinary sight, including when
both endpoints lie outside the cloud. Magical Darkness also blocks a ray while
unilluminated. Ordinary unlit space does not block sight to a lit distant cell.
This changes `can_see`, not geometric `has_line_of_sight` or line of effect:
fog is not Total Cover. The existing Chebyshev footprints and wall geometry stay
unchanged. Continuous geometry, vertical travel and multi-cell bodies remain out
of scope.

Blindsight within range bypasses physical fog and Darkness, subject to geometric
line of sight/Total Cover. Truesight pierces ordinary/magical Darkness and
Invisibility, **not physical fog or foliage**. Darkvision regrades ordinary
Darkness to Dim and Dim to Bright within range, but cannot see through magical
Darkness or Heavy Obscurement. Tremorsense still does not count as sight.
`light_on_cell`, `obscurement_on_cell`, `sunlight_on_cell` and `can_see` read the
same projection. Obscurement includes Dark/Dim illumination, while sensory
resolution retains the distinction between light and physical opacity.

Existing visibility consumers use that projection: Hide, unseen attacker/target,
Opportunity Attacks, Dodge, nearby ranged threats and Frightened sight gates.
Explicit sight-required checks fail without a random draw when their target is
unseen. Sight-dependent Perception checks have Disadvantage in perceived Dim
Light; hearing and non-sensory checks are unchanged. A check without a target
uses the actor's own cell as the declared surroundings convention. No narrative
perception target or sense is inferred from prose. Sunlight Sensitivity reads
the actor's projected cell for attacks and checks.

## Lifetime, wind and transactions

AreaCreated includes the typed environmental metadata. AreaExpired records
duration, concentration loss, explicit source removal, dispel, strong wind or
combat closure. Cross-spell dispel records the causing area ID; wind records its
Host source ID. Reconciliation runs after the shared concentration/result fold,
so immediate dispel cannot resurrect an orphan anchor. Concentration uses the
existing clock and event cleanup. Nonconcentration environmental duration uses
the existing round-start hook, expiring at creation round plus duration rounds;
it survives caster death. Explicit roster removal and combat closure retire
combat-local sources. These sources are not persisted into another combat or
world clock; cross-combat duration transfer remains a Host integration gap.

The Host can explicitly attest strong wind and its cells:

```python
from dnd5e_engine import StrongWind, apply_strong_wind

await apply_strong_wind(
    handle, StrongWind(source_id="weather:gust-1", cells=("8,0", "9,0")),
)
```

Contact with any susceptible fog cell disperses that entire source. Inputs
must be distinct, legal canonical cells. Wind strength, timing and coverage are
Host authority; this seam does not execute Gust of Wind or simulate weather.
No actor Action/Slot/RNG is spent. Bad inputs fail before mutation.

Public casts, object operations and wind updates reuse B1's snapshot/event-buffer transaction.
Faults after creation, payment, dispel or random draws restore authoritative
areas, derived projection, resources, effects/concentration, pending work,
event log/queue and RNG; unexpected exceptions propagate unchanged. Host
listeners receive committed events only. Counterspell preserves its existing
cost/interruption semantics and creates no environmental source.

Regressions in `test_dynamic_environment.py` exercise public casts, consumers,
overlap/levels, independent sources, durations, wind, Counterspell, admission
tampering, delegated/monster boundaries, ordering, faults and deterministic replay.
Data `test_environment.py` regenerates all three canonical spells from pinned
Foundry YAML and rejects structural/source drift. The spell capability and area
delivery audits report the new bounded operations without certifying unrelated
environmental spells.


## Typed object carrier (B8)

`CombatObjectState` owns only combat-local object records. `PersistentAreaState`
remains the sole spell-source owner: `FollowObjectEmanation` references a stable
object ID and creator inclusion choice; it does not cache an independent position
or create an ActiveEffect registry. Object records retain mutation `owner_id` and
Host introduction `source_id`, separate from spell caster/source/slot/concentration.
IDs use the `object:` namespace and cannot be reused after removal in this combat.
`get_live(handle).combat_objects` returns immutable records, resolved positions and
attached area IDs. Environmental snapshots also expose origin object ID, creator
inclusion and suppression, even while their actual footprints are empty.

```python
from dnd5e_engine import (
    CombatObject, ObjectMutation, register_combat_objects, mutate_combat_object,
)

await register_combat_objects(handle, owner_id="host:scene", objects=(
    CombatObject(id="object:stone", owner_id="host:scene",
                 source_id="scene:record-7", position="8,0"),
))
await submit_player_intent(handle, actor_id="char:caster", intent=PlayerIntent(
    intent_type="cast_spell", spell_id="darkness", slot_level=2,
    target_object_id="object:stone",
))
await mutate_combat_object(handle, owner_id="host:scene", mutation=ObjectMutation(
    object_id="object:stone", source_id="host:completed-interaction",
    operation="cover",
))
```

Registration is an atomic new-record batch. Invalid/duplicate/previously retired
IDs, mismatched mutation owners, illegal canonical ground cells and absent/dead
holders refuse before mutation/events/RNG. Unattended records have one ground
position and no holder; carried/worn records have one living holder and no ground
position. Their origin is always derived from authoritative `actor_zone`, including
voluntary and forced movement. A legal unattended object owned by another mutation
authority is still a legal spell target. Initial worn/carried or fully opaque-covered
objects refuse before spell payment, concentration replacement and Counterspell;
range and geometric Total Cover use the object's resolved position. Full opaque
cover is treated as denying a clear targeting path, independently of scene walls.

`move` is a completed Host placement of an unattended object in a legal cell.
`pickup` and `wear` require a living co-located holder; `wear` can also change that
holder's carried object to worn. No direct holder swap or held-position override is
accepted. `drop` derives the last holder cell. `cover`/`uncover` attest **complete
opaque cover**, not partial cover, a creature condition or automatically inferred
inventory state. `remove` retires the object and its attached sources. Cross-owner
mutations refuse. The owner string is a trusted in-process Host authority claim,
**not authentication for untrusted players**; a network adapter must authorize
access. These operations spend no actor Action/Slot/RNG: the Host attests completed
interaction legality and costs. They do not execute Utilize/free object interactions,
reach/capacity, equipment synchronization, theft, movement physics or a world engine.

Object casts use true 15-/60-ft Emanations. The default exclusion concerns the
originating **object identity**, not every creature in its cell. The creator can
set `object_include_origin=True`; the captured identity choice is public metadata.
The spatial light/darkness footprint includes the origin cell in both cases, so
carriers and other creatures there receive the environment. No object-target damage
mechanics are implied. Existing one-cell 2D/Chebyshev and Total Cover conventions
remain; ordinary public combat start/end occupancy prohibits two creatures sharing
an end cell. Tests isolate such co-location for geometry separately from public
carrier execution. Full object shape/size and 3D Emanations remain Host boundaries.

Opaque cover empties both primary and Dim footprints, blocking environmental
consumers and both directions of cross-spell dispelling. The source, actual cast
level, concentration and original duration/round deadline remain authoritative.
Uncover recomputes the same source without recasting, repaying or resetting clocks.
Dead holders drop objects at their last cell; independently cast Daylight survives.
Departing holders take their objects out of the combat and retire attached sources.
Caster death ends concentration Darkness but preserves non-concentration Daylight's
round clock; explicit caster roster departure retires combat-local sources under
the existing convention. Combat closure removes objects and areas; clocks do not
transfer across encounters. General Dispel Magic stays outside this contract.

Direct casts and delegated non-concentration Daylight consume the same object
preflight; delegated concentration still refuses before item costs. Unsupported
spell object modes fail closed. `PlayerIntent` remains the existing broad action
model: unrelated non-cast intents may carry unused spell fields. Host/Bridge typed
intent validation and authorization belong to the adapter parity work; this does
not authorize an object operation through a player intent.

`test_combat_objects.py` covers real public casts/payment, Counterspell, holder
movement, real lethal holder damage, actual Moonbeam Dim overlap, both area modes/
orders/thresholds, overlapping sources, mutation isolation, typed events, post-dispel
fault rollback and full-state same-seed replay. Accelerated clock tests set a final
clock boundary and use the public turn lifecycle; explicit death/departure seam
fixtures complement the natural lethal-damage path. Admission stays Bounded for
both spells and counts are unchanged; canonical exact regeneration, semantic digests
and generated inventories include only the implemented object modes.
