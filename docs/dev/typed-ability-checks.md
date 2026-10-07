# Typed ability checks

The DM/Agent decides why a check is needed, its ability and optional skill/tool,
DC, sensory requirement and social-interaction context. The engine makes no
narrative or DC judgment and never parses activity descriptions for semantics.

## Audit and ownership

| Entry | Payment owner | Resolution |
|---|---|---|
| `PlayerIntent(intent_type="check", check=CheckRequest(...))` | Rules-owned Action | Shared check pipeline |
| Search / Study / Influence | Rules-owned Action, selected by request context | Audited ability/skill combinations; explicit DM DC |
| Hide | Action or granted Cunning Action Bonus Action | Existing geometry/visibility gate, then DC 15 DEX (Stealth) through shared pipeline |
| Escape Grapple | Existing Action | Existing stored DC and Athletics/Acrobatics choice, then shared pipeline |
| Grapple / Shove | Existing Unarmed Strike payment | Saving throws, not ability checks; unchanged |
| Feature/item/spell `CheckActivity` | Canonical activation | Pure adapter resolves canonical actor/ability/DC, then shared pipeline |
| Initiative | Dedicated combat-start entry | Existing `roll_d20_test` primitive; dedicated initiative rules remain |
| Internal checks | Engine caller | Shared resolver with explicit internal provenance, never a host `free` flag |
| Standalone `check.resolve_check(CheckSpec)` | Outside combat | Existing standalone skill/ability/save API retained |

`CheckRequest` and `HelpCheckSpec` are frozen, extra-forbidden Pydantic objects,
exported at package level. A minimal host request is:

```python
PlayerIntent(
    intent_type="check",
    check=CheckRequest(
        actor_id="char:hero", ability="wis", skill="perception", dc=16,
        context="search", required_sense="hearing",
    ),
)
```

There is no host-controlled free payment. Generic checks spend an Action,
including informational checks without a DC and sense-based automatic failures.
Search accepts Wisdom with Insight/Medicine/Perception/Survival; Study accepts
Intelligence with Arcana/History/Investigation/Nature/Religion. Influence accepts
Charisma with Deception/Intimidation/Performance/Persuasion, or Wisdom (Animal
Handling) for a Beast/Monstrosity. The host supplies its target, social semantics
and DC; attitudes, communication policy and NPC behavior are outside this seam.
Generic checks can explicitly pair another ability with a skill.

An optional request on a single canonical CheckActivity adds semantics or
redemption, but cannot change its rolling actor, ability, skill/tool or DC.
Multiple-check invocations cannot accept an ambiguous override. All host and
canonical-check legality runs before action, resource, slot or RNG consumption.
The activity retains ownership of its original activation cost.

## Projection and shared core

`live_checks.py` projects typed `CheckActorState` values from real combatants,
active effects, fear-source visibility, sunlight, Help and condition source
lineage. `activities/check_pipeline.py::resolve_check_request` is pure of live
state and calls `roll_d20_test`. `activities/check.py::resolve_check` adapts the
canonical activity and retains standalone contexts and explicit legacy modifier
sidecars. Neither Hide nor Escape reconstructs rolls or emits CheckRolled itself.

Modifiers use live ability scores and level/CR Proficiency Bonus, skill
proficiency, Expertise and Exhaustion's declarative penalty. Audited active-effect
aliases share existing effect arithmetic. Alternate ability/skill pairings keep
the requested ability. DerivedSheet's noisy armor, Jack of All Trades and
Reliable Talent project through PartyMemberSpec to Combatant.

Jack of All Trades adds floor(PB/2) only to unproficient skill checks that do not
otherwise use PB; raw ability checks and Initiative do not qualify. Reliable
Talent floors the **kept** d20 to 10 only for a proficient skill/tool check.
Explicit tool proficiency labels are supported; inventory, tool grants and a
complete tool system remain outside scope. Neither feature changes attacks or
saving throws.

AdvantageSources compose declared grants, Poisoned/Frightened's declarative
projection, noisy armor, sunlight, scoped Help and charmer social advantage.
Fear uses the existing sight predicate. Charmed checks actual source lineage,
including imposing effect origins, and requires `social_interaction=True`.
Blinded/Deafened automatic failure uses the separately migrated canonical
sight/hearing clauses and the request's required sense. A skill name never
implies a sense or social interaction.

Help uses the existing Help action and helper-next-turn lifecycle, with a typed
beneficiary/skill grant alongside attack Help. The helper must be proficient and
assist another living ally. SRD leaves whether assistance is possible and 'near
enough' to the GM: `assistance_possible`, `assistance` and `range_ft` record that
decision; the engine checks actual range, physical line of effect and hearing for
verbal assistance. The next matching check consumes one grant, including a
check whose Advantage cancels or which auto-fails. Other skills leave it banked.
Sequential CheckActivity resolutions sharing one context also consume Help
once; their projected snapshots are updated after the authoritative event.

## Events and RNG

CheckRolled remains authoritative. Additive fields report context, target,
required sense, social interaction, payment owner, activity ID, directional
advantage sources, automatic failure, original kept die, Reliable Talent and
Inspiration bonus. Existing event fields and canonical skill labels remain.

Order is legality -> payment -> modifier/source projection -> automatic failure
or d20 -> Reliable Talent -> effect bonus dice -> optional Inspiration ->
CheckRolled -> Help/Inspiration writeback -> activity riders/continuation.
Automatic failure draws nothing, normal draws one d20, advantage/disadvantage
draw two, and both kinds cancel to one. Reliable Talent changes no draws.
Explicit Bardic redemption rolls the held die only after a failed DC-bearing
D20 Test; success, no DC and sense-based auto-failure never consume it. The
existing attack/save Inspiration system is unchanged.

The same state, typed intents and seed produce byte-equivalent events, final
state and RNG state. Projection is draw-free and grant order is stable.

Rules audit: [SRD 5.2](https://media.dndbeyond.com/compendium-images/srd/5.2/SRD_CC_v5.2.pdf)
pp. 6–10, 183–184 and 189; pinned canonical `jack-of-all-trades`,
`reliable-talent`, conditions and `sunlight-sensitivity` source data.
Opposed hidden-creature searches, exploration, skill challenges, tool inventory,
NPC attitude policy, monster AI, components and reaction rewrites remain excluded.
