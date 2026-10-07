# Physical movement and positioning

Physical movement has one typed route from an intent through path selection,
cost payment and positional events. `movement.py` owns pure rules and value
types; `live_movement.py` adapts them to combatants, conditions, occupancy and
the existing reaction and persistent-area lifecycle. The orchestrator retains
intent dispatch and turn advancement.

## Positions and creature size

Grid cell input is parsed and stored in canonical `"col,row"` form. Whitespace
aliases therefore identify the same space in starts, destinations, constructs,
summons and area origins. A blocked, out-of-bounds or already occupied starting
cell fails before combat begins. The engine never chooses a replacement start.

`CreatureSize` is shared with the dataset: Tiny, Small, Medium, Large, Huge and
Gargantuan. It travels through character derivation, party and encounter specs,
live combatants, forms and summons. A species with multiple allowed sizes
requires an explicit legal build choice. Size qualifies Grapple/Shove, the Push
mastery, Cunning Trip and passage through occupied spaces. Every creature still
occupies one cell; this does not implement footprints or Tiny stacking.

A mover can pass through an ally, an Incapacitated creature, a Tiny creature,
or a creature at least two size categories larger or smaller. A passable space
is difficult terrain unless its occupant is an ally or Tiny. A voluntary move
cannot end in an occupied space, including one it could legally pass through.
PC movement, monster closing and flee use these same rules.

## Modes, costs and the turn ledger

`PlayerIntent.movement_mode` is `walk` by default and also accepts `crawl`,
`climb` and `swim`. The caller declares the mode; the grid does not infer water,
surfaces or elevation. Climbing and swimming remain possible without a special
speed, at the extra-foot cost below. Prone permits only crawling or standing up.

For each foot traveled, movement costs are additive:

| Entry | Walk | Crawl | Climb/swim without that speed | Climb/swim with that speed |
|---|---:|---:|---:|---:|
| Ordinary space | 1 | 2 | 2 | 1 |
| Difficult terrain or difficult creature space | 2 | 3 | 3 | 2 |
| The same entry while dragging a non-exempt creature | +1 | +1 | +1 | +1 |

Terrain and creature-space difficulty are one difficulty term. A special speed
removes only the climb/swim term. A Tiny victim or one at least two size
categories smaller than the grappler removes the drag term. Multiple victims
do not multiply it.

Each actor has one `MovementLedger` for the turn: movement cost spent, physical
distance traveled, active mode and Dash count. Switching modes subtracts all
movement already spent from the new mode's allowance. It never grants another
independent pool. Standing up charges half current Speed without adding travel
distance. Normal Dash, Cunning Action Dash and Adrenaline Rush add allowance
through the same ledger. Only turn start resets it. `movement_remaining` is the
compatibility projection of this state.

Speed-zero conditions stop every speed. Exhaustion, Slow, typed speed reductions
and multipliers, and verified persistent-area penalties apply to the relevant
special speeds as well as walking. Monster templates hydrate their special
speeds; ordinary AI movement still chooses walking.

## Weighted paths and transaction boundaries

`GridTopology.shortest_path` retains its fewest-cells BFS contract. Live movement
uses weighted search with a shared step oracle for walls, bounds, occupants,
mode, terrain and drag. Routes minimize total cost, then step count, then a
stable numeric cell/neighbor order. Equal inputs choose the same path without
drawing dice. Threat avoidance is not part of path ranking.

Before the first mutation, a voluntary route validates its canonical
destination, endpoint occupancy, complete route, initial affordability and
Frightened restrictions. Rejection leaves positions, resources, movement,
effects and RNG unchanged apart from the failure event.

For an accepted route, each step resolves opportunity attacks before leaving
the current cell, then reads live state again. Death, a new movement restriction,
reduced allowance or displacement can stop the remaining route. Already
completed steps remain authoritative. Cost is paid before the new position is
reported; the step then moves the grappler and any dragged victims and runs
persistent-area and grapple-range hooks. Events expose completed movement
before any resulting area save or damage.

`ActorMoved` preserves its actor/from/to/distance fields and adds mode, actual
movement cost and path. An uninterrupted run may remain aggregated; reactions
and step-sensitive area hooks can split it. `CombatantMoved` preserves forced
movement fields and adds mode, movement cost and optional `dragged_by` source.
Forced movement and drag spend no victim allowance and provoke no victim
opportunity attack.

## Grapple dragging and release

The existing source-effect lineage identifies a grappler's victims. On a step,
the first victim in initiative order enters the grappler's vacated cell. For
additional victims, the engine chooses an empty cell within one step of that
victim's previous position and within the grappler's new reach. Candidates rank
by distance from the grappler's old cell, then numeric cell order. This packing
convention never stacks creatures or carries a victim more than one step. If a
complete placement is impossible, the step is illegal; route preflight simulates
these placements before committing. This convention does not search alternate
follower formations or a joint graph of all participants' positions.

Dragging uses authoritative forced-movement events and the normal per-step area
entry path. A forced separation beyond reach releases Grappled and removes its
source effect and `conditions_by_effect` lineage. Incapacitation and either
participant leaving combat use the existing shared cleanup path.

## Scoped immediate movement

`MovementGrant` records a maximum cost allowance, source, direction,
opportunity-attack behavior and immediate/current-turn lifetime.
`live_movement.execute_movement_grant` executes immediate grants through the
ordinary step path. Cunning Strike's Withdraw uses any legal walking direction;
Brutal Forceful uses a straight path toward the target after its fixed push.
Both cap the grant at half current effective walking Speed. Dash and ordinary
movement remaining do not increase this cap. Terrain and grapple drag consume
the independent grant allowance without charging or replenishing the ordinary
`MovementLedger`. Effects that change Speed still update its remaining-movement
projection normally.

Declare `AttackRiderRequest.movement_choice=MovementChoice(destination_cell=...)`
or a directed `distance_ft` before attack resolution. Extra mechanics are
forbidden; the host chooses no Speed fraction, OA policy or direction rule.
Omitting the choice, or selecting zero distance, declines optional movement.
Withdraw still sacrifices its one Sneak Attack die on a qualifying hit when
zero movement is chosen; misses pay no sacrifice and grant no movement.

Unrestricted preflight validates the complete weighted route, destination,
occupancy, drag and initial affordability before attack dice. Directed preflight
checks the declared cap and grid bounds; Forceful computes the actual path
after the push, using the target's actual final cell. The grid supports straight
axis or 45-degree rays without detours. A noncollinear ray permits no follow
movement. Occupied cells cannot be entered as a final destination, and only
complete affordable cell steps execute.

Each grant step reuses normal occupancy, drag, persistent-area entry and
grapple-range reconciliation. Death, zero Speed or forced relocation stops the
remaining steps and preserves completed movement. A dynamic post-push obstacle
does not undo the completed attack. OA exemption is scoped to this execution;
the grant creates no persistent Disengage flag, and later ordinary movement
can provoke normally.

## Remaining scope

Jump distance, Step of the Wind's complete jump behavior, elevation, 3-D flying
or burrowing, creature footprints and Grapple's free-hand requirement remain
deferred. Size support alone does not complete Hill Tumble or Eldritch Smite.
The [capability matrix](../capabilities.md) and [rider audit](attack-rider-audit.json)
distinguish those feature boundaries from the shared movement foundation.

Runtime regression coverage lives in
`packages/dnd5e-engine/tests/test_physical_movement_runtime.py`, alongside the
scoped grant, opportunity-attack, persistent-area, Grapple/Shove and spatial suites.
