# Persistent area lifecycle

`persistent_areas.py` owns typed `PersistentArea` records and per-combat
`PersistentAreaState`. `PersistentAreaSpec` is explicit canonical data on the
payload Activity. Ingestion checks allowlisted activity IDs, save blocks,
templates and audited clauses; runtime never interprets spell names or prose.

## Geometry and placement

`StationaryArea(origin)` keeps a fixed origin. `FollowSourceEmanation(source_id)`
reads the source's current cell. Both reuse `AreaTemplate` and `area_cells`,
including line of effect. A Sphere or Cylinder uses the existing 2D grid
rasterization. A point-based Square includes its origin as the minimum-column,
minimum-row corner and extends by its side length in the positive directions;
a 10-ft square covers four 5-ft cells and a 5-ft square one cell. Cubes retain
their face-anchored, directional convention.

For point-based Sphere/Cylinder/Square intents, `target_zone_id` takes priority
over the named target's cell. Canonical cell identity, bounds, range and line
of effect are checked before any action, charge or slot is spent. Omitting the
point preserves the named-target/caster fallback. Instantaneous AoEs still
resolve at cast time and create no persistent state.

Records capture the source kind/ID, caster snapshot, Activity, template,
cast origin, placement, excluded creatures, slot/base level, ability/DC,
passive effects, duration and concentration identity. Exclusions apply to
future entrants as well as creatures initially covered. Omitted exclusions
retain the existing harmful-choice default (allies spared); an explicit empty
tuple spares nobody except the source cell excluded by Emanation geometry.

## Triggers and deterministic ordering

The trigger vocabulary is `appearance`, `enter`, `area-enters-creature`, `turn-start-inside`
and `turn-end-inside`. Only a declared appearance trigger executes on placement,
after the shared concentration/result fold. Placement creates no synthetic
movement entry. Every completed voluntary or forced movement step first writes
position and movement state, then checks creature entry and newly covered
creatures for source-following Emanations. Active areas run in creation order;
creatures newly covered by one area run in initiative order. Movement events
precede their resulting saves/damage; with active areas, movement is reported
per step. Existing movement without areas retains its event aggregation.

One `last_trigger_turn` gate per area/creature uses the combat turn serial, not
the round or the creature's own turn count. All triggers of an area share the
gate. Re-entry or an end-turn boundary after entry consumes no additional RNG
in that turn; a new actor's turn opens a fresh gate. A stable snapshot is
rechecked against live ownership before each resolution, so earlier death or
concentration loss prevents later canceled work from drawing dice.

Each trigger builds the existing `ActivityResolutionContext`, captures source
magnitudes and rehydrates current target defenses, then calls `resolve_activity`.
Save, damage, status, immunity and Legendary Resistance resolution are shared.

## Verified producers

| Source | Placement | Triggers | Payload / continuous behavior |
|---|---|---|---|
| Spirit Guardians | 15-ft source-following Emanation | enter, area-enters-creature, turn-end-inside | WIS save, 3d8 with slot scaling, half on success; cast-time exclusions; current members' Speed halved |
| Stinking Cloud | stationary 20-ft Sphere | turn-start-inside | CON save; transient Poisoned expires at target turn end; concentration anchor owns area duration |
| Ball Bearings | stationary 10-ft Square within 10 ft | enter | DC 10 DEX; failed save applies Prone |
| Caltrops | stationary 5-ft Square within 5 ft | enter | DC 15 DEX; failure deals 1 Piercing and reduces Speed to 0 until target next turn start |
| Moonbeam | relocatable 5-ft-radius, 40-ft-high Cylinder (2D projection) | appearance, area-enters-creature, enter, turn-end-inside | CON save; 2d10 Radiant, +1d10/slot above 2, half on success; failed transformed target reverts and cannot shape-shift until leaving; magical Dim Light |

All five have a once-per-turn gate. Item use places the hazard and spends its
existing action/charge once; no creature saves merely because it occupies the
square at placement. Spirit Guardians' continuous Speed reduction depends on
current geometry and exclusions, independently of a save. Entering/leaving
adjusts the remaining distance by the Speed change, retaining distance already
spent and paid Dash movement. Identical overlapping halvings do not stack.
Caltrops riders expire before the next `TurnStarted` movement-budget refresh;
leaving the square earlier does not remove that rider.

Moonbeam reuses the B4 ongoing source for a later Magic Action relocating up to
60 ft to one legal destination; no transit-cell hits or duration reset. Its height
is retained as metadata; the Host attests vertical membership on the engine's
horizontal plane. Simultaneous arrivals share damage; overlapping copies project
the strongest/latest source while preserving independent owners. Shape locks use
real `live.transforms` reversion and remain owned by the producing area. See the
[complete relocation contract](ongoing-spell-activation.md#moonbeam-and-relocation-b7).

## Lifetime and events

`AreaCreated` reports the typed area ID, source, placement, geometry/height, exclusions,
duration, concentration, slot/DC and triggers. `AreaExpired` identifies the area
and distinguishes duration, concentration loss and source removal. No host
needs to infer lifecycle state from narration.
`AreaRelocated` reports the existing source's old and new origins without implying
another cast or creation. Dim Light shares the same area lifetime and overlap dispels.

Concentration anchors use the existing chain and duration cap. Dropping or
expiring that anchor removes its area synchronously. Non-concentration finite
durations tick at the source's turn end. Source death/departure removes its
areas; one affected creature dying or leaving never owns the whole area.
Transient riders have independent effect identities and cannot expire the
producer. Stinking Cloud no longer stores footprint cells in timed pending
records; all persistent footprints live in this module.

Wall templates, 3D, multi-cell creatures, Incendiary Cloud active movement,
Tsunami, Storm of Vengeance, Delayed Blast Fireball, Forbiddance and long
casting times remain excluded. Stinking Cloud wind dispersal and obscurement,
monster AI choosing empty origins and unclassified spell producers remain
separate backlog work.
