# Data-driven reactions

Reactions use canonical `ActivationBlock.reaction_conditions` as their
authoritative trigger semantics. Runtime never parses a spell description or
`activation.condition`. An activity can inherit reaction conditions while its
own `activation.type` remains `action`; populated typed conditions are what
identify the reaction.

## Host contract: pre-arm, then deterministic auto-fire

The engine has no mid-resolution host round-trip. On its own turn a creature
submits `PlayerIntent(intent_type="ready", spell_id=..., slot_level=...)` to
pre-arm a supported reaction spell. Declaration spends an Action and draws no
dice. The Reaction and spell slot are paid only when an eligible opportunity
fires. Re-arming replaces that owner's previous pending declaration.

This `ready` API is an engine pre-arm mechanism. It does not implement SRD
Ready's arbitrary trigger language or held-spell rules. A spell without
supported typed conditions is refused before the declaration's Action is
spent. The optional legacy `reaction_trigger` field is a deprecated
compatibility check: it cannot add conditions to the canonical spell, and an
incompatible value is refused before payment.

## Pending declarations and opportunities

`reactions.py` holds typed pending declarations, opportunities, matching and
target derivation. `live_reactions.py` connects them to authoritative combat
state, payment and the shared activity resolvers. The orchestrator calls these
boundaries at actual resolution points.

A pending declaration retains its owner, source kind, spell/activity identity,
slot level, canonical conditions and required predeclared resolution inputs.
Conditions have OR semantics in their stable canonical order. Shield's
`HIT_BY_ATTACK` and `TARGETED_BY_SPELL(target_spell_slug="magic-missile")`
conditions belong to the same declaration.

Opportunities carry the canonical `ReactionTriggerKind`, triggering actor,
affected creature, source spell/activity and the attack or damage context
needed by matching. They originate from rules resolution, rather than an
intent type or a host-supplied trigger string:

| Opportunity | Authoritative production point |
|---|---|
| `HIT_BY_ATTACK` | A shared attack resolver's provisional hit, after its attack roll |
| `TARGETED_BY_SPELL` | A legal spell resolution targets a creature |
| `SEES_SPELL_CAST` | Legal casting begins after casting-time payment and before source-slot payment |
| `TAKES_DAMAGE` | A creature actually takes positive damage from a rules instance |
| `CREATURE_FALLS` | No authoritative falling producer; safely deferred |

Matching reads only the typed kind, optional spell qualifier, typed range and
explicit runtime context. Sight-dependent conditions use the live visibility
rules. Targets follow closed canonical response metadata: self, triggering
actor, damage source or affected creature. Shield therefore resolves on its
reactor; Counterspell on the interrupted caster; Hellish Rebuke on the actual
creature that dealt the damage.

## Eligibility, ordering and payment

Candidates are visited in initiative order. Matching and all eligibility
checks finish before a candidate is removed: live owner and target, positive
owner HP, Reaction available, no Incapacitated restriction, spellcasting
allowed by Rage/transformation state, range/sight and an available slot at
the armed level. An ineligible candidate stays queued with its slot, Reaction
and RNG unchanged, and the scan continues to the next candidate.

An eligible firing removes its pending declaration, spends the Reaction and
slot, emits `ReactionTriggered`, and resolves the canonical activity. A slot
comes from Spellcasting first, then Pact Magic. The owner's Reaction refreshes
at its next turn start. Death and departure clear that owner's pending entry;
temporary Rage/transformation restrictions keep an otherwise valid entry
armed.

Opportunity attacks remain an automatic movement path without pre-arming.
They share reaction availability/payment primitives and the attack resolver.
Disengage, sight, reach and the existing movement-stop rules remain in force.

## Shield: one attack roll and one final adjudication

The shared attack resolver rolls once and computes a provisional hit against
the current effective AC. A provisional miss produces no hit opportunity and
does not fire Shield. On a provisional hit the live hook can resolve Shield
immediately, then returns the defender's refreshed effective AC. The same
attack total is checked against that AC. Natural 1/20 and advantage or
disadvantage retain the existing attack rules and number of draws.

Only the final `AttackRolled` is emitted. If Shield turns the hit into a miss,
there is no hit damage or hit rider. If the attack still hits, its normal
damage follows. The reaction and Shield effect remain committed in either
case, with the effect expiring at the reactor's next turn start.

Because the hook belongs to `activities/attack.py`, weapon and spell attacks,
monster attacks, opportunity attacks, legendary attacks and construct attacks
share the same timing. A `SaveActivity` or `DamageActivity` never produces a
hit opportunity.

Magic Missile uses the canonical targeted-spell qualifier. The exact typed
response stays attached to the active Shield effect, including when an attack
originally triggered it. Each matching spell resolution receives its own
damage negation without another Reaction or slot payment. The response expires
with Shield; it does not add permanent force immunity or require a spell-name
branch in the orchestrator.

## Counterspell: legal casting before source-slot payment

A refused cast produces no casting opportunity. Target, range, slot and
casting-time legality must pass first. A legal cast pays its Action, Bonus
Action or Reaction, then presents `SEES_SPELL_CAST` while its spell slot is
still unspent. Both PC and monster spell casts reach this boundary.

Counterspell's own canonical `SaveActivity` makes the caster's Constitution
save against the reactor's spell save DC through `resolve_activity()`.
Counterspell always spends its own Reaction and slot when it fires. A failed
save dissipates the triggering spell, emits `CastFailed(reason="countered")`,
preserves that spell's slot and produces none of its effects or RNG draws.
A successful save lets the triggering spell spend its own slot and resolve.
Its casting-time budget remains spent in both cases; an unused Action Surge
restricted extra Action remains available for a legal non-Magic action.

The queue resolves the first eligible reactor in initiative order. Nested
Counterspell chains are deferred; firing a queued spell does not recursively
open an unbounded spell-cast reaction stack.

## Damage-triggered reactions and attribution

`DamageApplied.source_actor_id` identifies the real source creature whenever
activity resolution knows it. Environmental or unknown sources remain
`None`; the live fold does not guess from the current turn actor. Death
attribution consumes the same field, so an opportunity attack or reaction
kill credits the attacker who actually dealt the damage.

`damage_instance_id` groups the typed damage parts of one rules instance.
Its identity is generated from deterministic resolution sequencing, never a
random UUID. A multi-type hit produces one damage-trigger opportunity per
affected creature, after its damage parts land, and one damage-at-zero-HP
death-save failure (two for a critical hit). Separate attacks remain separate
instances.

Hellish Rebuke requires positive actual damage and a living damage source the
reactor can see within the typed range. It resolves its ordinary spell
save/damage activities on that source. Zero damage, an unknown source, or a
reactor left dead, at 0 HP or Incapacitated produces no firing. Eligibility
uses the immediate damage context, rather than `last_damaged_by`.

## Events and deterministic replay

`ReactionTriggered` is authoritative. Its typed context identifies the
trigger kind, triggering actor, affected creature, source spell/activity and,
when applicable, damage instance. The legacy event-UUID field does not stand
in for those identities. A host can explain who reacted and why from the
event stream alone.

The same state, declarations, trigger intents and seed produce equivalent
events, pending queue, resources, HP/effects and RNG state. Eligibility and
declaration checks draw no RNG. A Shield reaction adds no dice to the
triggering attack.

## Canonical audit and remaining gaps

The deterministic [reaction runtime audit](reaction-runtime-audit.json)
records every canonical reaction activity's conditions, producer coverage,
target derivation, executable classification and explicit deferred reason.
Regenerate it from the repository root:

```console
uv run python -X utf8 -m dnd5e_engine.reaction_audit --output docs/dev/reaction-runtime-audit.json
```

The inventory contains 55 activities: 3 executable, 1 with a typed trigger but
no producer, and 51 explicitly deferred. Ten activities carry 11 typed
conditions. Feather Fall has typed `CREATURE_FALLS` semantics but no
authoritative falling lifecycle, so pre-arming is refused. Absorb Elements is
absent from the bundled canonical spell corpus; adding it requires complete
damage-type trigger/response semantics before admission. Untyped or unsupported
reaction declarations fail closed.
Arbitrary prose Ready, interactive prompts, falling physics, nested reaction
stacks and a new monster AI reaction policy remain outside this subsystem.
