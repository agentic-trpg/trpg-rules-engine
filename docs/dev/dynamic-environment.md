# Dynamic environmental sources

Batch B3 adds three **bounded point operations**, exercised through public
`submit_player_intent()`: Fog Cloud, Darkness and Daylight. This is not complete
support for every mode of those spells. The reviewed rules are
[SRD 5.2.1](https://media.dndbeyond.com/compendium-images/srd/5.2/SRD_CC_v5.2.1.pdf),
printed pp. 11 (vision), 122 (Darkness/Daylight), 133 (Fog Cloud), 177
(Blindsight), 182 (Heavily Obscured), and 190 (Truesight).

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
Creature/object targets, target lists/exclusions, directional modes, blocked or
out-of-bounds origins, and absent point carriers are refused before Action,
Slot, concentration replacement, reaction opportunity or RNG. Canonical area
rasterization and Total Cover clipping are shared with existing area delivery.
Areas may include occupied cells and empty cells. Caster movement does not move
the source. No new object, movement or action-economy subsystem is introduced.

```python
await submit_player_intent(
    handle, actor_id="char:caster",
    intent=PlayerIntent(
        intent_type="cast_spell", spell_id="fog-cloud",
        slot_level=2, target_zone_id="8,0",
    ),
)
```

Darkness/Daylight object anchoring, object movement and opaque covering remain
Deferred. Monster AI does not synthesize point carriers for environmental
operations; it retains uses and may choose an existing legal fallback. Delegated
Daylight supports the same point contract; delegated concentration spells retain
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
Overlap includes emitted Dim cells and respects clipped footprints, not merely
origin distance. Higher-level sources that fail a threshold remain independently
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

Public casts and wind updates reuse B1's snapshot/event-buffer transaction.
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
