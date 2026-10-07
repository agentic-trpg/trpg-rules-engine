# Spell timed activities

The canonical audit covers 339 spells and 464 activities in the pinned Foundry
corpus (`965ad2d0cf5d063dac675ba078b5bd3c3c0dd449`). Timing is translated into
`ActivityTiming`; the combat runtime never interprets spell descriptions or
activity names. This infrastructure addresses timing, not every rule of the
six affected spells.

| Timing | Activities | Audited sources |
| --- | ---: | --- |
| Immediate/default | 444 | Cast-time attacks, saves, damage, healing and utility; existing behavior for unclassified activities |
| Recurring turn start | 3 | Stinking Cloud, Ensnaring Strike, Searing Smite |
| Recurring turn end | 1 | Weird |
| One-shot next turn end | 2 | Vitriolic Sphere, Acid Arrow |
| Manual/event or sequence | 14 | Complex area hazards, moving areas, numbered rounds and delayed detonation |

The immediate count includes unclassified/manual-invocation activities already
present in the corpus; it does not claim that every spell is mechanically
complete. Attack riders such as Hex and Hunter's Mark refer to turns but are
not autonomous boundary activities. Recasting, releasing and other player
actions are likewise distinct from scheduled work.

## Verified mappings

`tools/translators/spell_timing.py` requires an exact spell/activity id, kind,
name, activation condition and verified source clause for each supported
mapping. Missing ids and changed fields fail translation. The canonical JSON
stores non-default timing; omitted timing validates to `immediate`, preserving
existing documents. Hermetic raw-source fixtures cover the three acceptance
spells, and tests verify every mapping and effect reference.

| Spell | Activity id | Trigger and linkage |
| --- | --- | --- |
| Weird | `nuStSySOEkwOUnXf` | Target turn end, recurring; Frightened effect `6bCIQhmzYmSR5Ck1`; success ends that target's effect |
| Vitriolic Sphere | `dnd5eactivity200` | Target next turn end, once; failed-save effect `WHhf4PFHQBDkCAyb`; lingering 5d4 does not scale with slot |
| Stinking Cloud | `dnd5eactivity000` | Each actor's turn start inside the stationary footprint; concentration anchor; transient Poisoned expires at that actor's turn end |
| Acid Arrow | `uAyE5DrJp0YpElcw` | Target next turn end, once; hit effect `NgglkyjzTbDh5Loa`; slot scaling retained |
| Ensnaring Strike | `ZWId9mbOE9zFnP6f` | Target turn start, recurring; Restrained effect `tFGMG3cjQTEeAhv2` |
| Searing Smite | `dnd5eactivity000` | Target turn start, recurring; effect `A0tTvLeRetrC708K`; success ends the source effect |

The 14 manual activities are Incendiary Cloud's Per Turn Save; Tsunami's Start
of Turn Effects; Earthquake's End of Turn Fissures and Collapsing Structure
Save; Delayed Blast Fireball's Touch Bead, Turn End Damage Increase and Trigger
Explosion; Storm of Vengeance's four turn-2/3/4/5+ activities; Forbiddance's
Damage Forbidden Creature; Wall of Ice's Frigid Air; and Wall of Thorns'
Traversal Save. They no longer execute automatically at cast time. Entry
hazards, moving areas, numbered stages and spell-end detonation still need
their own typed producers.

## State and execution

`timed_activities.py` owns typed `PendingTimedActivity` records and their
per-combat ordered state. Each record captures source spell/activity, caster,
target, slot, ability/DC, timing, earliest turn,
duration, source-effect identity and concentration identity. Source magnitudes
are captured when cast; defenses and positions are read at the boundary.
Persistent footprints, including Stinking Cloud, live in `PersistentAreaState`;
see [Persistent area lifecycle](persistent-areas.md).

On-turn, monster and readied spell dispatch resolve immediate activities in
canonical order, then schedule eligible deferred activities. Delegated item
casts use the same dispatch callback; standalone Activity resolution, which
has no combat lifecycle, resolves only immediate child activities. Item
concentration anchors/replacement remain a separate backlog item.

An attached source effect, not an assumed failed save, funds linked work.
Successful saves, misses and condition immunity cannot schedule that work.
At a boundary the scheduler builds an `ActivityResolutionContext` and calls
the existing `resolve_activity()`, retaining its damage/save/defense and
Legendary Resistance behavior. It does not copy those resolvers.

Order is cast registration, canonical activity order, then target order.
Boundary execution snapshots this order and rechecks cancellation before each
resolution, so an earlier death or concentration drop prevents later RNG
draws. Turn-start reaction effects expire first. Timed work runs before
end-of-turn repeat saves, source-duration expiry and concentration-cap expiry.
One-shot work is removed after execution. Source expiry/removal,
concentration drop, and target death/departure cancel related work without
RNG. Non-concentration work can outlive its caster; an area record survives
an individual creature's death or departure.

The monotonically increasing turn serial prevents next-turn work from expiring
on the casting turn. Typed effect repeat saves instead run at **each** target
turn end, including the applying turn's end; their captured save and expiry
contracts are described in [typed effect lifecycle](effect-lifecycle.md).
Turn-start removal hands off to the next initiative slot, including a round
wrap, without ending a departed actor's turn twice.

A legal new concentration cast drops the prior chain when casting begins,
before Counterspell and new resolution. A refused cast never reaches this
step. The existing typed spell duration also caps monster concentration.

## Boundaries

Stinking Cloud now uses shared persistent-area state and tests current
membership at turn start. Spirit Guardians, Ball Bearings and Caltrops use the
same state for movement and boundary triggers. Wind dispersal and cloud
vision/obscurement remain deferred. Wall geometry, long casting times, spell components, a reaction
rewrite and 3D/elevation remain outside this infrastructure. Existing
unclassified spell mechanics and corpus payload divergences remain in
`BACKLOG.md`.
