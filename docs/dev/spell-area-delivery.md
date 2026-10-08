# Unified area and spell delivery

Direct PC spells, monster casts and item `CastActivity` delegation share the
same delivery boundary. Direct item saves, damage and healing use the same
activity target planner. Callers own Action, Bonus Action, Reaction, slot,
charge and feature-resource payment; delivery owns target planning and activity
execution.

```text
Intent / monster plan / delegated cast
    -> immutable SpellDeliverySpec
    -> draw-free delivery preflight
    -> caller payment and existing reactions
    -> shared execution and live target revalidation
    -> typed Activity resolver
    -> authoritative damage, effects and movement
```

`spell_delivery.py` contains immutable declarations and pure planning.
`live_spell_delivery.py` supplies the live adapters and dispatches the child
spell. `activities/cast.py` remains a pure delegation resolver: it follows the
canonical UUID and calls `ctx.spell_dispatch`. It does not select spatial
targets or copy the parent's target list into the child.

## Host declaration and canonical rules

`SpellDeliverySpec` contains decisions already made by a host or AI:

| Internal field | Existing `PlayerIntent` field |
|---|---|
| `primary_target_id` | `target_id` |
| `selected_target_ids` | `target_ids` |
| `origin_cell` | `target_zone_id` |
| `direction` | `direction` |
| `excluded_target_ids` | `excluded_target_ids` |
| `source_kind` | Derived as direct spell, item cast, monster cast or reaction cast |
| `source_item_id`, `source_activity_id` | Existing invocation identity |

The declaration is frozen and rejects extra fields. A host cannot override
radius, target cap, creature-type rules, save DC or forced-movement distance
through this carrier. Geometry and restrictions come from canonical activity
data. Delegation retains the declaration, then plans against the **child**
spell's own activities and range.

`TargetBlock.area_semantics` is an optional frozen `AreaSemantics` value:

| Field | Closed values |
|---|---|
| `template_role` | `target_area`, `effect_geometry` |
| `origin_policy` | `actor`, `point_within_range` |
| `includes_origin` | Boolean |

Safe shape defaults remain available when there is no exact metadata. Reviewed
metadata overrides those defaults. Ingestion attaches metadata by exact source
and activity identity and checks the pinned source clauses and structure.
Runtime never parses descriptions, activation conditions or `affects.special`.

`delivery_activities()` provides a bounded compatibility adapter for spells
with multiple canonical activities. A declaration whose activity identity
matches a child spell activity selects that activity, together with the spell's
turn-start/turn-end and persistent payloads. Explicit manual activities remain
deferred. Without a matching selection, the default retains the first immediate
activity and other immediate activities with neither activation nor range
overrides, plus those lifecycle
payloads; if none qualify, it retains the first canonical activity. This lets
Freezing Sphere's default Cast and Fire avoid the separate Freeze Water and
Throw Held Globe alternatives without reading their prose. It preserves
existing multi-payload casts rather than implementing a complete spell
activity-selection or held-spell lifecycle.

## Template roles and placement

Slow's 40-foot Cube is a `target_area` placed within spell range. Its footprint
uses the grid's point-based Square convention: the declared cell is the
minimum-column/minimum-row corner of an eight-by-eight block on a 5-foot grid.
The origin is included and a direction is unnecessary. This differs from
Thunderwave's actor-origin, direction-selected Cube, whose face lies adjacent
to the caster and excludes the caster's cell.

Phantasmal Force's 10-foot Cube is `effect_geometry`: the largest possible
phantasm, not a creature selector. Its cast still resolves against one named
creature with normal range and visibility gates; nearby creatures receive no
save merely because they stand in that display geometry. This targeting
correction does not implement all illusion interaction or Study mechanics.

Point origins may be empty or occupied. Preflight checks canonical cell
identity, bounds, range and line of effect before resource payment or RNG.
An explicitly supplied empty or noncanonical internal origin is refused;
only an omitted origin retains the existing target/caster default.
On this grid, `blocked_cells` represent terrain filling its cell and providing
Total Cover. Reaching a chosen point inside such terrain, or a `cover_cells`
Total Cover location, from another cell fails the clear-path gate. The existing
source-cell convention remains: an origin belongs to its own area, and a caster
can choose its own covered cell. Public combat already forbids a source from
starting inside blocked terrain. Half/Three-Quarters Cover and creature
occupancy do not forbid a point. No automatic relocation to the near side
of an obstruction is inferred: the existing public contract refuses the
declaration and lets the caller choose a legal point.
Area cells are rasterized by `GridTopology`, then filtered by line of effect
from the origin. Half and Three-Quarters Cover affect applicable saves; Total
Cover and walls exclude blocked cells. Grid topology contains geometry, not
spell-origin rules.

Sphere, Cylinder and Square retain their point-placement conventions. Cylinder
geometry is its 2-D footprint; height does not establish elevation support.
Actor-origin Cones and Lines retain the eight grid directions. Line width now
reaches rasterization: the canonical corpus includes real 10-foot-wide ancient
dragon breath Lines, as well as the common 5-foot Lines. See
[spatial geometry](spatial-geometry.md) for lane conventions.

## Target selection contract

Targets use stable initiative order. The planner applies geometry, current
live/alive eligibility, typed creature filters and allegiance restrictions
before choice and count. Dead or departed creatures do not save or take damage.
Non-Combatant constructs are not silently treated as creatures.

For a counted target area such as Slow, explicit `target_ids` name the final
selected creatures **inside the computed area**. Names must be unique, live,
eligible and within the count cap. A seventh Slow target, a duplicate, or a
creature outside the Cube is refused before payment and RNG. With no explicit
selection, the existing deterministic default chooses from legal area members.

For an uncounted area, `target_ids` do not replace area expansion. A Wand of
Fireballs invocation therefore cannot narrow the child Fireball to one named
target. For `effect_geometry`, named targets follow the ordinary non-area
contract instead.

`excluded_target_ids` names creatures actually inside a choice area. Unknown
or outside-area exclusions are refused during preflight. Exclusions never
change geometry or add targets. With explicit counted selection, they can only
remove selected creatures; the selected list remains authoritative. For
choice areas without explicit selection, an omitted exclusions field retains
the existing harmful/enemy or healing/ally default, while an explicit empty
exclusions tuple selects all otherwise eligible creatures.

An actor already excluded by its actor-origin template may also be named as a
redundant exclusion, preserving Intimidating Presence's existing declarations.
This does not include the actor in the area or permit unrelated outside IDs.

Persistent choice areas preserve their existing cast-time exemption contract:
Spirit Guardians may record a known live creature outside its current area
so that creature remains spared if it later enters. Unknown IDs still fail
preflight; the stored exemption does not make an outside creature an immediate
area target or change the area geometry.

Execution replans against current live positions and defenses after reactions.
A target that dies or departs between preflight and resolution is omitted;
successful Counterspell prevents spell delivery entirely. The planner itself
emits no events, mutates no state and draws no RNG.

Save damage policy is closed to `half`, `none` and `full`. The canonical
inventory contains 492 SaveActivities: 323 half, 162 none and seven full.
Schema ingestion rejects unknown values. Shared preflight also guards
unchecked/custom carriers with `CastFailed(reason="unsupported_activity")`
before Action/slot/charge payment. The save resolver independently validates
before DC, damage or save draws; an unknown policy never becomes full damage,
including when the save fails or there are no targets.

## Creature filters and automatic saves

`TargetCreatureFilter` contains closed standard D&D creature-type tuples:
`include_creature_types`, `exclude_creature_types` and
`auto_success_creature_types`. It can also carry a typed `deferred_reason` for
an unreviewed restriction. `Combatant.creature_type` supplies the target value.

Ingestion currently normalizes six exact reviewed source values: `Undead`,
`Undead of your choice`, `Elemental`, `Fiend or Undead`, the five-type
Celestial/Elemental/Fey/Fiend/Undead list, and `target is Humanoid`. There is no
general natural-language parser. Other nonempty area restrictions receive an
explicit deferred marker. Legacy non-area size, grapple, object and visibility
restrictions are outside this type-filter migration.

Sear Undead and the Helm of Brilliance's Diamond Light now carry an Undead-only
predicate. This closes target filtering, not every feature/item clause: Turn
Undead fleeing and source-dependent termination, and Diamond Light's ongoing
start-turn/gem mechanics still require their complete producers.

Dust of Sneezing and Choking has actor-origin metadata with
`includes_origin=True`. Its user is a real save target, alongside nearby
creatures. Construct, Elemental, Ooze, Plant and Undead targets remain targeted
and emit successful `SaveRolled` events with no natural d20 and zero d20 RNG.
They receive no failed-save effect. Ordinary targets use the shared save
primitive. Dust's complete suffocation and Lesser Restoration removal
semantics remain deferred. Intimidating Presence and Spirit Guardians retain
their source exclusion and existing choice/exclusion behavior.

## Delegated casts and provenance

Preflight resolves each `CastActivity.spell.uuid` and recursively validates the
child's geometry before the outer item's charge payment. Unresolved UUIDs,
cycles and unsupported child geometry fail closed. A valid delegated spell
keeps its fixed save DC/attack override and cast-level scaling. Wand of
Fireballs uses DC 15 and a true 20-foot Sphere centered on its declared point;
all legal creatures receive their individual saves in stable order.

`AreaTargeted` reports the child area, origin, direction, affected/spared IDs,
spell/activity identity and parent invocation identity. Activity damage has a
deterministic child source such as `spell:fireball:<activity-id>`, with parent
item identity retained separately. The cycle guard is not used as an
observability store. Existing typed timed activities continue through the
shared scheduler, rather than resolving every child activity immediately.

Item-cast concentration remains explicitly deferred before payment. This
preserves the need for complete replacement timing, caster-held anchor,
maximum duration and caster death/Incapacitated cleanup. The delivery audit
records these sources; structural UUID resolution does not claim complete
concentration or item support. Verified direct and monster persistent areas
continue through `PersistentAreaState`; unsupported delegated persistent
producers cannot silently fall back to immediate target-only execution.

Direct spell finalization checks the caster's current state before creating
a concentration anchor. A caster who reaches 0 Hit Points or becomes
Incapacitated during resolution cannot restart concentration at the tail.

## Typed forced movement

Canonical activities can carry frozen `ForcedMovementSpec` values containing
`trigger` (`failed_save`, `successful_save`, `hit`), distance, direction and
target role. Thunderwave's reviewed activity carries a failed-save, 10-foot
push away from its source. There is no authoritative spell-slug registry in
the engine.

The pure SaveActivity resolver records `ForcedMovementRequest` after the
target's save and damage. The live delivery fold calls the existing shared
push seam. PC, monster and delegated spell execution therefore share event
order: `SaveRolled`, `DamageApplied`, then completed forced-movement events.
A successful Thunderwave save causes half damage and no push. A target killed
or removed by damage is not moved.

Forced movement spends no target allowance and provokes no target opportunity
attack. Wall/occupancy blocking, completed-step persistence, area crossing and
grapple range reconciliation reuse authoritative movement behavior.

## Monster planning

Monster plans build the same delivery declaration. Actor-origin directional
areas evaluate the eight fixed directions. Point-origin areas evaluate all
legal grid cells within range and line of effect, including empty cells;
occupied cells remain legal unless a specific rule forbids them.

Scoring uses legal affected targets, enemy count, actual friendly fire,
existing target priority, caster-to-origin distance and numeric cell order.
It is deterministic and draws no RNG. An unsupported or unusable area mode
spends no Recharge or daily uses and does not prevent trying another legal
action.

Non-area monster activities validate the AI's existing chosen target through
the same pure `plan_delivery` eligibility rules, including typed filters and
live/departed state. Candidate ranking checks eligibility without range so
the existing approach gambit can still move toward a legal target. Execution
rechecks each child's established reach/LoS before context construction or
RNG, and again before resolution. An empty target plan does not count as an
invocation or spend Recharge/daily/activity uses; legendary targeting is
validated before spending its separate pool. Target priority is unchanged;
an ineligible mode may fall through to another legal action.

`tests/test_delivery_hardening.py` adds rejection, resource and purity
regressions and compares event bytes, final RNG state and public final combat
state plus timed/persistent owners across seeded reruns. It covers the six
primary acceptances without promoting their deferred rule clauses.

## Audit and unsupported boundaries

The checked-in inventories are deterministic JSON:

- [Area delivery audit](area-delivery-audit.json): spells, items, features and
  all monster activity buckets, with role, origin, inclusion, count, filters,
  movement, line width and delivery status.
- [Delegated spell delivery audit](spell-delivery-audit.json): UUID resolution,
  child geometry, fixed challenges, concentration, persistent/timed activities
  and explicit unsupported reasons.

The audit describes delivery capability. It does not promote an entire spell,
feature or item merely because its target filtering or template is supported.
Non-resolving templates remain distinct from explicitly reviewed
`effect_geometry` metadata. Area rows inventory every canonical activity,
including separately invoked alternatives; they do not claim that a default
cast executes every row. Delegated child summaries conservatively inventory
all of that child's area activities, while actual preflight validates the
activity family selected for the invocation.

The area inventory has **348 rows**: 219 executable target areas, one explicit
effect-geometry template, four verified persistent areas, 74 non-resolving
templates, 19 deferred target restrictions, 17 unsupported Walls, 12
nonconstant/missing sizes and two manual activities. These counts include all
monster activity buckets. The 39 Line records include eight width-10 records,
26 width-5 records, one width-1 record, one width-20 record and three with the
5-foot default.

The CastActivity inventory has **560 rows**: 160 item, 399 monster and one
feature invocation. It resolves 557 referenced UUIDs; 181 rows have a child
template, 41 have a fixed save DC and one has a fixed attack bonus. It records
254 concentration children, including 77 item invocations. Current structural
delivery classification is 470 resolvable and 90 deferred, with 22 unsupported
child-geometry records; some deferred reasons overlap concentration, so these
producer counts are not partitions.

Wall templates, formula-sized areas, 3-D/elevation and multi-cell creature
footprints remain unsupported. A real unsupported target area is refused
before payment rather than resolving against a named target alone. Preserve
Life remains deferred because it needs a divided total pool and a per-target
half-maximum cap. Darkness/light sources, falling, arbitrary prose inference,
full long-cast lifecycle, complete Dust suffocation and new reaction mechanics
are outside this change.
