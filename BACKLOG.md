# Nat20 — Backlog & Gap Inventory

Known gaps in the Nat20 libraries: the `dnd5e-engine` rules/combat engine and
the `dnd5e-srd-data` canonical SRD dataset. This is the single source of truth
for "what the engine does not yet do." It tracks **library** gaps only — host
application concerns (narrators, persistence, world state, UI) are out of scope.

**Update protocol:** when you close a gap, delete its entry in the same PR that
closes it. When you discover one, add it under the right section with a date and
a `packages/…` file anchor. Keep entries engine/data-centric — no host-app paths.

Anchors are current as of `dnd5e-engine` / `dnd5e-srd-data` **v0.6.0**
(re-verified 2026-09-27 by the C23 scrub: every anchor names a file and a
symbol, never a line number).

The user-facing summary of the same information is
[`docs/capabilities.md`](docs/capabilities.md) — the per-mechanic matrix of what
resolves today. When you close a gap here, update that page too; its published
counts are pinned by `packages/dnd5e-engine/tests/test_capability_matrix.py`.

---

# dnd5e-engine

## Spell execution admission (2026-10-08)

Paid combat spell no-ops are now gated by shared typed admission before payment,
RNG, concentration replacement and reaction opportunities. This closes the
acceptance/payment safety gap, **not** the missing mechanics below. The stable
inventory in `docs/audits/spell-execution.json` records 28 executable combat
contracts, 47 bounded contracts, 26 explicit host narrative contracts and 238
deferred spells, with all 464 canonical Activities. Unknown identities,
unreviewed Activities, missing effects and mandatory unsupported mechanisms
fail closed. PC, monster and delegated item paths share the check.

Batch B1 additionally binds runtime admission to the existing semantic digest,
restores public execution state/events/RNG on unexpected errors, and pins each
combat's opening loader. Bridge Homebrew mutations conflict while combats are
open. Public fault-injection and retry replay regressions cover direct, delegated,
monster and reaction casts. Custom loaders must remain immutable after install;
host listener errors are post-commit. These safeguards do not change the spell
support inventory or close the missing mechanics below.

Batch B2 closes the effect-choice gap for Guidance, Enhance Ability and Protection
from Energy through public casting: typed per-target canonical Effect IDs,
draw-free selection preflight, matching skill bonus dice at check resolution,
ability-check Advantage and existing damage Resistance projection. Invalid choices
preserve resources and concentration; unexpected execution errors retain B1's full
rollback. Reviewed equal-potency overlapping casts project the latest effect while
preserving older duration/concentration ownership. Other effect-choice spells stay
deferred when they need independent mechanics. Hosts must explicitly attest willing
targets; item concentration ownership and Monster AI choice declarations remain
unimplemented. See `live_spell_delivery.py`, `activities/effects.py` and
`activities/check_pipeline.py` in `packages/dnd5e-engine/src/dnd5e_engine/`.

Batch B3 adds bounded stationary point operations for Fog Cloud, Darkness and
Daylight, backed by typed EnvironmentalSpec on existing PersistentAreaState.
Shared projection feeds lighting, physical obscurement, magical Darkness,
Sunlight, vision, Hide/Attack/OA/Dodge and explicit sight-required checks.
Reviewed overlap dispels use actual spell levels and clipped footprints; source
removal restores the surviving projection without static scene mutation. Host
StrongWind attestations disperse fog; wind spells/weather simulation, Darkness /
Daylight object anchoring, Monster AI point declarations and cross-combat
world-clock persistence remain deferred. Public faults restore environment,
resources, events and RNG. See `docs/dev/dynamic-environment.md`.

Slow/Haste action rules, teleportation, general summons/enchantments/transforms,
restoration/resurrection, object/world transactions, conditional sequences and
wall geometry remain deferred. A supported target filter, save, heal or damage
Activity does not close those gaps. Finger of Death remains deferred because
Humanoid death requires next-turn Zombie creation; this is not optional.
The low-level Activity Resolver is still an unchecked building block for hosts,
not a resource-paying complete-spell API. See `docs/dev/spell-execution-admission.md`.

## B4 ongoing activation and moving sources (2026-10-08)

- Sunbeam now executes its initial and later beams through public intents. An
  area-owned canonical source captures slot/DC; later Magic actions pay only the
  base Action, retain duration/concentration, skip Counterspell cast windows and
  retain damage reactions. Failure-only Blinded expires at the caster's next
  turn start independently of concentration.
- Shared following environmental projection supplies 30-ft Bright +30-ft Dim
  Sunlight and refreshes on voluntary/forced movement. Existing area lifecycle
  removes sources on concentration expiry/replacement/drop, death, departure and
  Combat End. Static scene data stays unchanged.
- Exact canonical regeneration, semantic Admission, public prepayment refusals,
  owner isolation, Action Surge/Rage, rollback and replay regressions protect the
  contract. Sunbeam stays Bounded; counts remain 28/47/26/238.
- Moonbeam remains Deferred: active stationary-area relocation with appearance/
  movement triggers and general forced shape reversion/area-bound shape-change
  suppression are missing. Item concentration, autonomous Monster AI activation
  and out-of-combat duration clocks remain outside this batch. See
  `docs/dev/ongoing-spell-activation.md`.

## Unimplemented activity kinds (2026-08-22)

- **Most `summon`, `transform` and `enchant` activities are narrative no-ops
  (amended 2026-09-25, C21a; 2026-09-26, C21b).**
  `activities/resolver.py::resolve_activity` routes them to a logged no-op, as
  it does a `utility` activity carrying no effect riders, unless the
  orchestrator hands the resolution a conjuration carrier for an allowlisted
  source: Spiritual Weapon, Magic Weapon, Wild Shape and Polymorph resolve
  (C21a), and Summon Dragon seats its Draconic Spirit (C21b; the other
  summons are under "Roster summons"). The measured consequence: **105 of 339
  SRD spells (31%) historically loaded correctly and emitted no events**, 30 of them
  concentration spells — *Blur, Darkness, Fog Cloud,
  Wall of Force, Silent Image, Globe of Invulnerability, Expeditious
  Retreat*. These historical counts are structural, not support classifications;
  the combat admission gate now rejects unsupported spells before costs and
  preserves the caster's existing concentration. Their rules remain unimplemented.
  (`packages/dnd5e-engine/src/dnd5e_engine/activities/resolver.py`)
- **Enchantments other than Magic Weapon, and shape-shifts other than Wild
  Shape and Polymorph, stay narrative (2026-09-25, C21a).** `enchant_weapon`
  reads two item-change keys (`system.magicalBonus`, `system.properties`);
  Sacred Weapon, Shillelagh, True Strike, Contingency, Pact of the Blade and
  40 item enchantments need a general item-change interpreter (a damage die,
  an attack ability, a damage type) and a carrier naming their item. True
  Polymorph, Animal Shapes and Shapechange ship no `transform` activity.
  (`packages/dnd5e-engine/src/dnd5e_engine/activities/conjuration.py::CONJURATION_ALLOWLIST`)
- **Monster summon riders stay narrative (2026-09-25, C21a).** A monster
  attack or an item never gets a conjuration carrier, so the 16 monster
  `summon` riders resolve nothing. Monster casts of a construct or summon
  spell are deferred too (see "No monster casts a construct or summon spell"
  under Conjurations and shape-shifting).
  (`packages/dnd5e-engine/src/dnd5e_engine/activities/monster_actions.py::rank_monster_actions`)

## Monster action economy (2026-08-22)

- **Monster-side ranged-in-melee/Vex/Sap threading is wired but inert
  (2026-09-02, C15 Task 6/3).** `orchestrator.py`'s monster attack site
  passes `attacker_ranged_in_melee`, `attacker_vex_advantage`, and
  `attacker_sapped` into the activity context and pops vex grants/sap marks
  after resolution, mirroring the PC site exactly — but a monster attack
  carries its damage on the `AttackActivity` itself, not a separate typed
  `Weapon` (`resolve_activity(activity, actx, weapon=None)`), so
  `attack.py`'s weapon-gated "effectively ranged" check and mastery-proc
  fold never fire for a monster attacker. A monster can still be the
  RECEIVING end of a vex grant or sap mark from a prior PC weapon hit
  (that half is live). Needs a monster weapon-mastery/property model;
  confirmed still open after C18 (2026-09-03) — out of that cluster's scope
  per its R10 ruling.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py`)
- **Multiattack substitutions and turn-dependent conditionals remain unmodelled**
  (narrowed 2026-10-07). Typed plans support fixed sequences, referenced
  free combinations, `uses X if available` (Doppelganger) and
  `uses either X or Y if available` (Aboleth). They omit unavailable
  mandatory children without replacement and never select unreferenced
  siblings. Later-sentence replacements (Chimera's Fire Breath, dragon
  Spellcasting) and conditions such as Clay Golem's "if it used Hasten this
  turn" remain deferred; unknown branches retain only the referenced
  unconditional primary branch. Child fan-out still resolves one offensive
  activity per invocation, not arbitrary multi-activity riders.
  (`packages/dnd5e-engine/src/dnd5e_engine/activities/monster_actions.py::plan_monster_action`)
- **Utility-only and cost > 1 legendary actions are never selected by the
  built-in AI** (2026-09-03, C18). `_take_legendary_action` only considers
  entries whose `legendary_cost` is unset or `1` and that carry an
  attack/save/damage activity (or a castable spell) — a `utility`-only entry
  (e.g. Pounce) and a multi-point legendary action are skipped even when
  legal. The bundled corpus carries no multi-point legendary action today.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_take_legendary_action`)
- **Legendary Resistance is host-armed only — no AI policy decides when to
  spend it** (2026-09-03, C18). `resolve_legendary_resistance` is a pure
  seam a host calls before submitting the intent that will force a save;
  the built-in monster AI never calls it itself, so a monster never
  protects its own concentration or avoids a status condition unless a host
  makes that call on its behalf.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::resolve_legendary_resistance`)
- **Lair-variant Legendary Resistance counts are not modelled** (2026-09-23,
  noted at C18 final review). The corpus carries no "or N/Day in Lair"
  text for any monster's Legendary Resistance — e.g. the Ancient Gold
  Dragon keeps Foundry's flat 3 — so a monster fought in its own lair gets
  no extra uses.
  (`packages/dnd5e-srd-data/src/dnd5e_srd_data/canonical/monsters/`)
- **Typed effect lifecycle and shared saves (2026-10-08).** Reviewed exact
  effect bindings now register captured save ability/DC, source identity and
  magical provenance. Hold Person/Hold Monster, Cunning Poison, Devious Knock
  Out and Intimidating Presence repeat through the same save primitive as
  ordinary activities; Grapple/Shove, concentration and Undead Fortitude also
  share target modifiers, conditions, Exhaustion, effect bonus dice and armed
  Legendary Resistance. Magic Resistance applies to captured magical repeat
  saves; mundane Grapple/Shove and maintaining concentration are not magical
  saves. Next-save disadvantage consumes only its own clause, including on
  automatic failure and death saves. Standalone legacy `check.resolve_check`
  saving throws still lack live effect-state consumption. The deterministic
  [lifecycle inventory](docs/dev/effect-lifecycle-audit.json) retains 146
  deferred/candidate rows; this does not complete every ongoing spell or item.
  See [the lifecycle contract](docs/dev/effect-lifecycle.md).
- **Recharge state does not persist across combats** (2026-09-03, C18).
  `MonsterActionUses` (recharge_spent, uses_remaining) lives on
  `_LiveCombat`, discarded at `end_combat` like every other combat-scoped
  engine state (by design, per this engine's effects-are-combat-scoped
  convention) — a monster that used its recharge ability in one encounter
  always starts its next encounter fully recharged, with no cross-combat
  "still on cooldown" model.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py`)
- **Undead Fortitude's trigger ignores temporary HP on the live combat
  path** (2026-09-23). `activities/apply.py::apply_damage` gates the trait's
  CON save on `final_amount >= target.hp_current` — the pure resolver's
  snapshot of REAL HP only. Temp HP is a separate bucket the orchestrator
  absorbs damage from AFTER this event is emitted
  (`_emit_apply_damage`/`_emit_apply_temp_hp`), so a hit that the bearer's
  temp HP would have fully absorbed (no real-HP loss at all) can still force
  a needless CON save and, on a failure, an incorrect Death. Relatedly, the
  gate compares EACH damage part's amount against the same un-decremented
  `hp_current` snapshot, so a multi-type hit whose parts each fall short of
  the bearer's HP but together exceed it never triggers the save at all and
  the bearer drops without rolling.
  (`packages/dnd5e-engine/src/dnd5e_engine/activities/apply.py`)

- **Stat-block item properties do not reach attack ability selection
  (2026-10-07).** Ordinary templates now use real scores/PB and the shared
  activity resolver, but a stat-block item has no Weapon carrier. An empty
  melee `attack.ability` therefore defaults to STR even when its original
  Foundry item has Finesse: Goblin Warrior Scimitar has `properties: [fin, …]`,
  STR 8 and DEX 14, so the activity default produces +1 rather than the
  Finesse +4. The implicit damage modifier has the same missing-property
  limitation. Preserve the structured item property/ability semantics in a
  separate data-carrier change; do not infer them from action names or bonuses.
  (`packages/dnd5e-engine/src/dnd5e_engine/activities/attack.py::_governing_ability`)

## Core combat rules not modelled (2026-08-22)

- **An opportunity attack never Cleaves, takes a versatile weapon in two hands,
  or redeems a Bardic Inspiration die (2026-10-03, C24).** SRD 5.2 Cleave
  follows any melee hit with the weapon and Bardic Inspiration any failed D20
  Test, but each is a choice the engine can only take from an intent, and an
  opportunity attack has none (the engine never pauses mid-resolution); a
  Versatile weapon rolls its one-handed die for the same reason — never the
  Versatile die, and never Great Weapon Fighting either, since that style's
  own gate for a Versatile (as opposed to strictly Two-Handed) weapon reads
  the same never-set `use_versatile_damage` flag.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_resolve_opportunity_attack`)
- **A character's `reach_ft` reaches only its opportunity attacks (2026-10-03,
  C24).** An Unarmed Strike opportunity attack threatens
  `PartyMemberSpec.reach_ft`, but the character's own on-turn Unarmed Strike
  is range-gated at the Unarmed Strike's 5 ft (SRD 5.2 Unarmed Strike: "a
  target within 5 feet of you"), so a host that sets `reach_ft=10` sees a
  10-ft opportunity attack its on-turn attack can't match.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_pc_attack_out_of_range`)
- **An `attack` or an unchosen `use_item` resolves every activity on its
  weapon or item, not the one it means to fire (2026-10-04, C26a).** A
  weapon's own non-attack activity (the Mace of Terror's Wave of Terror
  save) and an item's alternative modes (Javelin of Lightning, Rod of
  Lordly Might, the Staff of Power, the Staff of the Magi, the Staff of
  Thunder and Lightning, Thunderous Greatclub, Horn of Blasting) all resolve
  together against the intent's single target whenever nothing names which
  one to fire. C26a's area resolver guards the one visible symptom — none of
  these intents ever area-expands — but the ambiguous resolution itself
  stands: the proper fix resolves only the chosen activity, requiring
  `activity_id` when an item carries alternatives, as a feature already
  does.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_resolve_intent_activities`)
- **Grapple's free-hand gate remains unmodelled (narrowed 2026-10-08).**
  Physical movement supplies typed creature size, Grapple/Shove's target-size
  gate, dragging, release beyond reach and the Push mastery's Large-or-smaller
  gate. Grapple still needs an authoritative free-hand/equipment model.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_handle_grapple`)
- **Ritual casting is out-of-combat only (2026-09-03, C17).** `spellcasting.
  resolve_ritual_cast(spell, *, prepared, ritual_adept=False)` is the host
  seam: it validates the Ritual tag + prepared/Ritual-Adept gate and returns
  the `RitualCast` (10-minute tax, no slot expended). An in-combat
  `PlayerIntent.as_ritual=True` is rejected (`CastFailed(reason=
  "ritual_in_combat")`) before any slot logic — the turn economy has no model
  for a 10-minute action, so ritual casting only resolves between combats,
  through the host calling `resolve_ritual_cast` directly.
  (`packages/dnd5e-engine/src/dnd5e_engine/spellcasting.py`)
- **Spell components are metadata-only, not enforced (2026-09-03, C17).**
  `SpellCast` now carries `components` / `material` / `material_consumed` /
  `material_cost_gp` (via `spellcasting.spell_component_metadata`) on every
  PC cast path, but nothing gates a cast on a gagged/Silenced caster, a free
  hand, a component pouch/focus, or a costed material's gold cost — a host
  decision per spec §5 C17.
  (`packages/dnd5e-engine/src/dnd5e_engine/spellcasting.py`,
  `packages/dnd5e-engine/src/dnd5e_engine/events.py::SpellCast`)
- **Remaining rules that suppress or alter an opportunity attack
  (2026-10-03, C24; narrowed 2026-10-08).** Open Hand Technique's Addle now
  suppresses the target's opportunity attacks until its next turn starts,
  through the shared typed effect consumer. Staggering Blow suppresses OA until
  source next-turn start; Withdraw and Forceful follow movement now use an
  immediate grant with a scoped OA exemption. Unmodelled: the Agile trait
  (Deer, Rat: "doesn't provoke an
  Opportunity Attack when it moves out of an enemy's reach"); "can't make
  Opportunity Attacks" riders (Shocking Grasp, Mace of Terror); moves "without
  provoking Opportunity Attacks" (Tactical Shift, Remarkable Athlete); Disadvantage on
  opportunity attacks against a creature (Hunter's Escape the Horde, Boots of
  Speed). Flyby and Nimble Escape are recorded under "Typed traits are
  hydrated".
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_opportunity_attackers`)
- **A cast longer than a turn resolves as an Action (2026-09-25, C21a;
  amended 2026-09-26, C21b).** `_classify_action_cost` special-cases only
  Bonus Action and Reaction casts, so a 1-minute or 1-hour cast resolves
  within one turn. Of the creature summons, Find Familiar (1 hour or a
  ritual), Animate Dead and Create Undead (1 minute each) can't be cast in a
  combat turn at all, and `start_combat` has no input for a creature summoned
  before the combat that outlives it, so all three stay narrative.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_classify_action_cost`)
- **Caster-disabled concentration finalization resolved (2026-10-08).**
  Shared finalization refuses a new concentration anchor when the live caster
  is dead, at 0 Hit Points or Incapacitated. Existing final-fold cleanup still
  removes any concentration links attached before that state change. The
  0-HP and Incapacitated regression cases both verify that the resolution tail
  creates no anchor or concentration chain.
  (`packages/dnd5e-engine/tests/test_delivery_planning.py::test_concentration_finalize_cannot_create_anchor_for_disabled_caster`)
- **Item-cast concentration remains explicitly deferred (narrowed
  2026-10-08).** Recursive delivery preflight now refuses a concentration child
  before item payment. Complete caster-owned replacement timing, anchor,
  maximum duration and death/Incapacitated cleanup must land together; a
  target-held effect alone is insufficient. The CastActivity inventory records
  77 item concentration producers. Typed timing and verified persistent-area
  engines remain shared, but do not silently authorize incomplete item
  concentration. Potions retain their separate no-Concentration rules gap.
  (`packages/dnd5e-engine/src/dnd5e_engine/live_spell_delivery.py::preflight_delivery`)

- **`CombatOutcome.expended_resources` counts a concentration spell where its
  effect lands (2026-09-26, C21a).** SRD 5.2: "When you cast a spell, you
  expend a slot of that spell's level or higher" — the caster's resource,
  whatever the save. The fold charges a concentration-flagged
  `EffectApplied` to its target when that target is a party member: a PC's
  Hold Person or Polymorph that the monster saves against is counted (the
  anchor sits on the caster), one it fails is not (the effect sits on the
  monster), and a Polymorph on a PC ally charges the ally.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_emit_apply_effect_applied`)
- **"Of your choice" targeting is unmodelled on a `utility` activity or a
  templateless save (2026-10-04, C26a; narrowed 2026-10-07).** Holy Aura and
  Nature's Sanctuary carry their `affects.choice` flag on a `utility`
  activity, and Rod of Rulership's choice save carries no measured template;
  these do not resolve an area, so `PlayerIntent.excluded_target_ids`
  sent with any of them is refused with `target_invalid`. Spirit Guardians now
  persists its cast-time exclusions in a source-following Emanation.
  (`packages/dnd5e-engine/src/dnd5e_engine/areas.py::is_choice`,
  `packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_area_target_failure`)

## Movement (2026-08-22)

- **No elevation or 3-D flying/burrowing (narrowed 2026-10-08).** The grid is
  strictly 2-D. Explicit crawl/climb/swim, special-speed costs and shared turn
  accounting now resolve; fly/burrow speeds are retained and receive modifiers,
  but are not positioning modes. Water, climbable surfaces and falling are not
  inferred from cell geometry.
- **No multi-tile creature footprints (amended 2026-09-26, C21b).** Every
  creature occupies one cell regardless of size, a Large summoned Draconic
  Spirit included: its placement needs one free cell.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_summon_placement`)
- **Jumping remains deferred (narrowed 2026-10-08).** Long/high jumps, jump
  distance choices and Step of the Wind's complete jump behavior still need
  typed execution. Scoped `MovementGrant` now executes Withdraw and Brutal
  Forceful follow movement using the shared weighted step/drag/area path and an
  independent allowance. This does not implement jump rules, Tactical Shift or
  Remarkable Athlete. See [the movement contract](docs/dev/movement-positioning.md).

## Event stream observability (2026-08-22)

- **`ZoneTransit` is never emitted (2026-10-03, C25).** It stays in the closed
  `CombatEvent` union only because a host imports it. Drop it in a later
  breaking minor, once no host does.
  (`packages/dnd5e-engine/src/dnd5e_engine/events.py::ZoneTransit`)
- **A second late `narration_events` consumer on an ended combat awaits
  forever (2026-09-27, C23).** `end_combat` enqueues a `None` sentinel;
  `narration_events` takes it off the queue and returns without putting it
  back, unlike `drain_pending_events`, which re-queues it so another
  consumer still sees it. A first late consumer started after `end_combat`
  gets the sentinel and returns cleanly; a second one finds the queue empty
  and blocks on `event_queue.get()` with nothing left to ever wake it.
  Pre-existing, but this release's migration guide now advertises a late
  consumer draining `combat_ended` after close.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::narration_events`)
- **Attribution outside shared delivery remains incomplete (narrowed
  2026-10-08).**
  Shared activity damage now carries the real `source_actor_id`, including
  off-turn/reaction damage; death attribution consumes that field instead of
  the current actor. Shared spell delivery populates deterministic child
  spell/activity `DamageApplied.source_id` and retains parent invocation
  identity separately. External, environmental and manual callers still need
  their own source identity; delivery coverage does not guarantee complete
  attribution for them. `AttackRolled` / `SaveRolled` / `CheckRolled` carry `natural`,
  `modifier` and `sources`; the target's effective AC is still not reported.
  (`packages/dnd5e-engine/src/dnd5e_engine/events.py`)

## Character building (2026-08-22)

- **A multiclass caster's spellcasting ability comes from its primary class
  (amended 2026-09-24, C20).** `CharacterBuildSpec.classes` carries the build
  (C17 slot tables; C19 per-class features, HP, hit dice, proficiencies and
  non-stacking Extra Attack), and `PartyMemberSpec.classes` carries it into
  combat (C20: features, scale values and `@classes.<class>.levels` per
  class). `_resolve_caster_spellcasting_ability` still reads only
  `class_slug`, so every spell a multiclass caster casts uses that class's
  ability: a Cleric/Wizard whose `class_slug` is the Cleric casts its Wizard
  spells with Wisdom, and a non-caster `class_slug` falls back to the flat
  approximation.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_resolve_caster_spellcasting_ability`)
- **Feats other than the four Fighting Styles are almost entirely inert
  (amended 2026-09-24, C20).** The engine applies the four SRD 5.2 Fighting
  Style feats itself, by slug; of the other 13 corpus feats only Boon of the
  Night Spirit carries a mechanical activity. The dataset `Feat` schema has no
  `passive_effects`, so the effects Foundry ships on feats (Archery's and
  Defense's among them) are dropped at translation. Prerequisites are only
  partly checked (2026-09-24): a feat's free-text `requirement` (Grappler's
  "Strength or Dexterity 13+") is never validated, and the two epic boons
  whose corpus `prerequisites` carry no `level` entry at all — `boon-of-fate`
  and `boon-of-irresistible-offense` — are accepted below the SRD's Epic Boon
  floor of character level 19, unlike `boon-of-combat-prowess`, whose entry
  does carry `level: 19`.
  (`packages/dnd5e-engine/src/dnd5e_engine/build_spec.py::_asi_level_feats`,
  `packages/dnd5e-srd-data/src/dnd5e_srd_data/schema/feat.py`,
  `packages/dnd5e-srd-data/tools/translators/foundry.py`)
- **Magic weapons named by slug aren't matched to their base weapon for
  proficiency (2026-09-24).** A class granted specific weapon slugs rather
  than a whole category — Rogue and Monk get `rapier`/`scimitar`/
  `shortsword`/… individually — gets no Proficiency Bonus against a magic
  variant of one of them, such as a Scimitar of Speed: Foundry's own
  `system.type.baseItem` links a magic weapon back to its mundane base
  weapon (the translator already reads `baseItem` for another purpose), but
  the canonical `Item` schema has no equivalent field yet, so
  `_is_proficient_with_weapon` can only match a `weapon_category` or the
  item's own slug. Root fix: a dataset `base_weapon` field built from
  `baseItem`, read by `_is_proficient_with_weapon`. A host can pin
  `attack_bonus` on `CombatInstance` meanwhile.
  (`packages/dnd5e-srd-data/src/dnd5e_srd_data/schema/item.py`,
  `packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_is_proficient_with_weapon`)
- **`derive_sheet` does not apply several SRD inputs (2026-09-23, C19 scope
  cuts).** Recorded but not applied: languages, tool proficiencies, the
  background's Origin feat (`starting_feat_slug` is an unresolved Foundry
  id), ability increases from feats other than the Ability Score
  Improvement feat, and level-20 capstone increases (the Monk's Body and
  Mind is a fixed `points: 0` ASI entry up to 25, the Barbarian's Primal
  Champion a feature). Not validated at all: multiclass ability
  prerequisites (the sheet has no score history, and enforcing them would
  reject the corpus's own default-STR/DEX builds in C19-S09), skill-choice
  capacity where grants are missing (for example Skilled),
  `attunement_constraint`, untrained-armor
  penalties (`armor_training` is reported so a host can apply them), and
  magic items' own passive effects (hosts pass them as `active_effects`).
  Feat repeatability is also prose-only, so `DerivedSheet.feats` is not
  de-duplicated: a feat reachable both as a bare feature-choice-pool pick
  and as a `feat:<class>:<level>:<feat>` token lands twice.
  (`packages/dnd5e-engine/src/dnd5e_engine/build_spec.py::derive_sheet`)
- **Remaining C19 sheet consumers (narrowed 2026-10-07).** Noisy armor,
  Jack of All Trades and Reliable Talent now project into live combat and the
  shared typed ability-check pipeline. Mage Armor still does not flip derived
  `ac_calc_mode`. Divine Order Thaumaturge and Primal Order Magician now project
  structured skill bonuses into shared live checks. Primal Knowledge still needs
  a Rage-conditioned ability-substitution carrier; its prose is not interpreted.

## Architecture (2026-08-22)

- **Remaining reaction sources and opportunity producers (narrowed 2026-10-07).**
  The typed spell queue, attack/cast/damage opportunities, target derivation
  and payment live in `reactions.py` / `live_reactions.py`. The canonical
  audit inventories 55 activities: 3 executable, 1 with a typed trigger but
  no producer, and 51 explicitly deferred. Feather Fall's `CREATURE_FALLS`
  has no authoritative falling lifecycle. Feature/item/monster reaction
  sources, untyped prose and compound damage-type/save/movement qualifiers
  need complete typed execution contracts before admission. Absorb Elements
  is absent from the bundled canonical spell corpus. Nested reaction chains
  and a monster AI reaction policy remain deferred. Unsupported declarations
  are refused before the pre-arm Action is spent.
  (`packages/dnd5e-engine/src/dnd5e_engine/reaction_audit.py`,
  `docs/dev/reaction-runtime-audit.json`)
- **Engine does not yet read `canonical/conditions/`.** The dataset category
  exists (C22, `AssetLoader.get_condition`), mirroring `rules/conditions.py`;
  per campaign design D3 the engine should prefer the data when present and
  fall back to the Python registry. Still open after C12 and C18 (neither
  reads the category); unowned.
  (`packages/dnd5e-engine/src/dnd5e_engine/rules/conditions.py`)
- **`orchestrator.py` remains large**, well over a third of the engine,
  holding item/feature charge accounting, the monster
  turn, the effect lifecycle, the conjurations and the turn loop. Each is a
  coherent module; splitting them would make the combat loop readable
  without changing behaviour.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py`)

## Spatial mechanics (grid backend is in place; these are additive)

- **Threat-aware routing remains deferred (narrowed 2026-10-08).** Shared
  weighted paths minimize movement cost with stable ties and preserve the BFS
  helper's contract. They do not rank opportunity-attack exposure or creature
  footprints.

- **Unified area and spell delivery implemented (2026-10-08).** Direct PC,
  monster and item-delegated spells share immutable delivery declarations,
  draw-free preflight and activity execution. Child Fireball uses its own
  20-ft Sphere, point/range/LoE gates, DC override and cast level rather than
  the parent's named target list. Slow checks selected creatures inside its
  point-placed Cube; Phantasmal Force's Cube is display geometry. Typed origin
  and inclusion replace caller-specific assumptions. Thunderwave's canonical
  failed-save 10-ft push records the same request on PC, monster and delegated
  paths; the old spell-slug registry is deleted. Monster point origins include
  all legal grid cells, with deterministic ties and no RNG. See
  `docs/dev/spell-area-delivery.md` and its two checked-in audits.

- **Canonical Line widths implemented (2026-10-08).** Width reaches shared
  grid rasterization. The audit includes 5-ft Lines and real 10-ft ancient
  dragon breath; the 5-ft grid's lanes use a fixed perpendicular convention.
  Continuous geometry and 3-D remain outside the grid contract.
  (`packages/dnd5e-engine/src/dnd5e_engine/spatial.py::cells_in_template`)

- **Truesight versus physical Heavy Obscurement fixed (2026-10-08, B3).**
  Truesight pierces Darkness, not opaque fog/foliage; Blindsight retains its
  independent physical-obscurement bypass. Static and dynamic sources share
  the corrected consumer and explicit sight-ray convention.
- **Dynamic point environments added; portable light remains open (B3).**
  Fog Cloud, Darkness and Daylight now affect authoritative spatial consumers.
  Torches, Light, object carriers/covering, general weather and wind spell
  execution are still absent. Nonconcentration Daylight survives caster death;
  explicit roster removal and combat closure retire combat-local sources.
  Sunlight Sensitivity attacks/checks read the actor's projected cell.
  No *See Invisibility*-style effect flag
  pierces the Invisible condition either (C16b plan ruling R3) — only
  blindsight/
  truesight in range with line of sight do, via
  `orchestrator.py::_pierces_invisibility`; an effect-vocabulary carve-out is
  a future cluster's seam.
  (`packages/dnd5e-engine/src/dnd5e_engine/spatial.py::GridTopology.can_see`)
- **Unsupported area geometry remains deferred (narrowed 2026-10-08).** Wall
  templates, formula-sized areas, altitude/3-D and multi-cell creature
  footprints remain unsupported. Real unsupported target geometry now refuses
  before Action, slot, charge or RNG payment; it never quietly resolves against
  a named target alone. Monster AI skips unusable modes without spending
  Recharge or daily uses and may choose another legal action. Verified
  persistent producers retain their existing shared state.
  (`packages/dnd5e-engine/src/dnd5e_engine/spell_delivery.py`)

## Effect-change sidecars (2026-07-02)

- **Two effect-key namespaces for check/save bonuses (2026-08-26).** The public
  standalone check resolver folds `check.bonus` / `check.skill_check.bonus` /
  `check.ability_check.bonus` / `save.bonus` / `save.saving_throw.bonus` /
  `save.<long ability>.bonus` (e.g. `save.wisdom.bonus`), while the activity path
  (F1d) folds `abilities.check` / `abilities.skill` / `abilities.<ab>.save` (plus
  the Foundry-native `system.bonuses.abilities.*` / `system.abilities.<ab>.
  bonuses.save` spellings) into the `check_modifiers` / `save_modifiers`
  sidecars. An ActiveEffect authored against one key set is therefore INERT on
  the other surface. Recommended resolution: alias the standalone resolver's key
  set onto the `abilities.*` family (one normalization table, both consumers).
  Not aliased, deliberately, so F1d stayed behaviour-preserving; neither C12
  nor C19 took it up.
  (`packages/dnd5e-engine/src/dnd5e_engine/check.py::_KIND_TO_BUCKETS`,
  `packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_fold_d20_test_bonus`).
## Class / species feature mechanics

- **Feature runtime safety audit (2026-10-07).** The old 29-activity failure
  estimate is retired. The pre-change numeric resolver audit found 20 failures
  among 77 damage/heal/save activities; that count did not prove public API
  reachability. The new deterministic audit covers all 191 reachable canonical
  activities: 18 `fully_resolvable`, 44 `unsupported_preflight`, 98
  `semantic_special_case`, 14 `attack_rider_executable` and 17
  `attack_rider_deferred` (updated 2026-10-08). Executable riders still reject
  standalone invocation. Every rejected invocation is checked before action,
  resource or RNG consumption. See [the contract](docs/dev/feature-runtime.md)
  and [slug/activity/reason rows](docs/dev/feature-runtime-audit.json).
  Closed shared gaps: selected feature build-to-combat projection, owner-class
  spellcasting context, closed arithmetic and source-use carriers, cross-feature
  resource identity, Remove Poison, Patient Defense, Adrenaline Rush, Rage's
  armor/spell/concentration/duration rules, and Thaumaturge/Magician skill bonuses.
- **Heal bonus producer audit (2026-10-07).** Current canonical effects contain
  zero `system.bonuses.heal.*` producers. A generic consumer is deferred until
  there is a typed producer/contract; no feature-specific bonus shortcut was added.
- **Step of the Wind remains deferred (2026-10-07).** The corpus contains only
  the Focus variant. Its doubled jump distance has no carrier; preflight rejects
  rather than applying only Dash/Disengage. Patient Defense's free Disengage and
  one-Focus Disengage+Dodge now delegate to the real action mechanics.
- **Action Surge's extra action funds no Action-costed feature or item
  (2026-09-24, narrowed 2026-10-07).** The corpus still lacks reliable Magic-action
  classification for features and items, so Action-costed `use_feature` and
  `use_item` conservatively count as Magic actions. The restricted extra Action
  therefore cannot fund them. Attack/Magic ordering and refused casts preserve
  the appropriate budgets.
- **Remaining attack rider options (narrowed 2026-10-08).** Shared typed attack
  binding, Stunning Strike, Open Hand Addle/Push/Topple, Cunning Trip/Poison and
  Devious Obscure/Knock Out, Withdraw and the Brutal family are executable.
  Automatic Frenzy joins Sneak Attack through shared damage contributions.
  Sneak Attack commits inside damage
  resolution before Cleave or another attack can select it again. The full
  [rider inventory](docs/dev/attack-rider-audit.json) distinguishes 15
  executable riders, 17 deferred options, two executable foundations and 17
  supporting contexts; see
  [the contract](docs/dev/attack-feature-riders.md). Poison requires an actual
  carried canonical Poisoner's Kit and one Sneak die; Knock Out costs six
  dice. Both use captured CON saves and ten-round target timers; Knock Out also
  ends after a complete positive damage instance. Withdraw sacrifices one die
  for immediate half-effective-Speed walking with a move-scoped OA exemption
  and independent allowance. Daze still needs mutually exclusive
  movement/Action/Bonus Action restrictions and continues to reject before payment.
- **Closed Reckless / Brutal / Frenzy and scoped movement (2026-10-08).**
  First-own-turn actual attack-roll declaration applies typed STR-only outgoing
  and all incoming advantage until source next-turn start. Brutal commits its
  once-per-turn choice before rolling, rejects every disadvantage source and
  forgoes all advantage only for the chosen roll. Its four owned options execute
  through the shared planner: fixed Forceful push plus optional scoped follow;
  latest-only all-Speed Hamstring; next-save Staggering with independent OA
  suppression; and nonstacking Sundering for the next other attacker, consuming
  only the selected grant. Improved Brutal Strike (2) permits two distinct
  options with shared scaled damage once. Frenzy requires live Rage/Reckless
  and commits on the first qualifying own-turn final hit. Both extra damage
  contributions fold into the original damage instance with canonical critical
  policy. Other inventoried riders retain their exact missing qualifiers,
  costs, target and lifecycle clauses in the audit.
- **Breath Weapon remains deferred (narrowed 2026-10-08).** Canonical invocation
  lacks Attack replacement and ancestry-bound damage selection. Intimidating
  Presence's initial Bonus Action, selected 30-foot Emanation, STR/PB save DC,
  own one-use Long-Rest pool and ten-round repeat-save Frightened lifecycle are
  executable. Its separate Rage Recharge activity remains deferred because the
  empty canonical restore target is not an authoritative resource transfer.
- **Bardic Inspiration follow-ups (narrowed 2026-10-07).** Grant range and the
  recipient's sight/hearing now gate payment. Weapon attacks and ability checks
  retain their redemption paths. Spell-attack/save redemption and redemption
  after the granting bard departs remain deferred.
- **Persistent Rage recovery, Font of Inspiration and Superior Inspiration
  (2026-10-07).** Initiative recovery and spell-slot conversion need a shared
  resource-trigger lifecycle. Font's Short-Rest recharge upgrade also remains
  absent. These standalone activities reject before spending. Persistent Rage's
  existing no-extension/Unconscious behavior now uses the corrected 100-round cap.
- **Cunning Action has one mechanical entrypoint (2026-10-07).** Canonical
  `use_feature cunning-action` activities explicitly reject. Use dedicated
  `dash`, `disengage`, and `hide` intents with `use_bonus_action=True`; cosmetic
  canonical markers are not a second implementation.
- **Dust targeting resolved; ongoing semantics remain deferred (2026-10-08).**
  Typed Emanation metadata includes the user in the real 30-ft save targets.
  Construct, Elemental, Ooze, Plant and Undead remain targeted and emit
  successful saves with zero d20 draws and no failed-save effect. Complete
  suffocation and Lesser Restoration removal remain unsupported; target/save
  corrections do not make the whole item fully supported. Intimidating Presence
  and Spirit Guardians keep their reviewed source and exclusion semantics.
  (`packages/dnd5e-engine/src/dnd5e_engine/live_spell_delivery.py`)

- **Preserve Life's divided pool remains deferred (narrowed 2026-10-08).**
  The typed area model can express creator inclusion, but the producer still
  needs a total healing pool divided among selected targets and a per-target
  half-maximum cap. Feature preflight refuses it before spending; ordinary
  area healing must not grant the complete pool to every ally.
  (`packages/dnd5e-engine/src/dnd5e_engine/feature_runtime.py`)

- **Reviewed creature-type targeting resolved (narrowed 2026-10-08).** Closed
  typed include/exclude/automatic-save lists replace arbitrary source strings
  at runtime. Exact reviewed source normalizations include Undead for Sear
  Undead and Helm of Brilliance/Diamond Light. Unreviewed area restrictions
  carry a deferred marker and fail closed. This does not implement Turn Undead
  fleeing/source termination, Diamond Light's ongoing gem/start-turn producer,
  arbitrary description-only filters, or legacy named size/grapple/object
  restrictions. Do not promote whole features/items on filter support alone.
  (`packages/dnd5e-srd-data/tools/translators/area_delivery.py`)

### Passive-stat projection (`activities/passive_stats.py`)

The interpreter now projects always-on `dr` (damage resistance), `di`
(immunity), `dv` (vulnerability), `ci` (condition immunity), `senses`, and
`movement` (walk-speed bonus + typed non-walk modes) at combat start, plus the
activation-gated Rage `dr` fold on the active-effect path. One entry
of the passive-projection spec allowlist remains recognized-but-deferred for lack of a landing
zone + apply logic:

- **Ability scores (direct passive overrides) and languages** (amended
  2026-09-23, C19) — each still needs its own landing zone + apply logic.
  `ac.calc`, `weaponProf`, `armorProf` and HP bonuses are now read by
  `derive_sheet` — outside this interpreter's own allowlist,
  `rules/character.py` reads the same always-on `changes` list directly
  (`ac_modes_from_changes`, `armor_training_from_changes`,
  `weapon_proficiencies_from_changes`, `hit_point_bonus`). A feature that
  overrides an ability score directly (rather than through a
  `background:`/`asi:` choice token) and species/feature language grants
  are still routed to `skipped_keys`.
- **Fast Movement's heavy-armor condition and Unarmored Movement's symbolic
  `@scale` value are not modelled** (2026-09-23, C19 scope cut). Fast
  Movement's own passive change is an unconditional flat `+10` walk-speed
  add — its "doesn't function while wearing heavy armor" text lives only in
  corpus prose, not in a structured field, so the interpreter (which never
  sees worn equipment) has no way to suppress it for a character in heavy
  armor. Unarmored Movement's bonus is the symbolic value
  `@scale.monk.unarmored-movement`, which `_resolve_movement_modes` already
  defends against reaching a numeric field — it lands in `skipped_keys`
  rather than resolving the level-scaled bonus a real ScaleValue lookup would
  give.
  (`packages/dnd5e-engine/src/dnd5e_engine/activities/passive_stats.py`)

## Conjurations and shape-shifting (2026-09-25, C21a)

- **Only attack-roll actions of a stat block can be commanded, and a
  Multiattack's composition is not enforced (2026-09-25, C21a; amended
  2026-09-26, C21b).** `stat_block_action_id` refuses a save action (a Breath
  Weapon, Venomous Spew, a Roar) and the Multiattack itself; the host picks
  every swing of the Attack action, so "one Bite attack and one Claw attack"
  can be two Bites. A summoned Draconic Spirit's Breath Weapon is refused too,
  so its Multiattack ("…and it uses Breath Weapon") gives only the Rends;
  Summon Dragon's `match.saves` (the caster's spell save DC) lands with the
  first commandable save action. Commanding a save action (deferred,
  2026-10-04, C26a) needs: `_stat_block_attack_failure` to accept one; the
  whole Action as its cost, not one Multiattack swing; aim from `direction` or
  `target_id` through the area resolver; `action_unavailable` when it is spent
  or used up and `out_of_range` when its origin is beyond its range;
  `_mark_monster_action_used` when it commits; and, for a summon, the caster's
  save DC and the Draconic Spirit's damage-type choice.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_stat_block_attack_failure`)
- **A form's melee reach is 5 feet (2026-09-25, C21a; amended 2026-09-26,
  C21b; amended 2026-10-03, C24).** The corpus carries no melee reach for a
  monster, so a transformed creature swings at 5 feet whatever its form — its
  opportunity attack too, now that one resolves through the stat block
  (`_stat_block_opportunity_attack` falls back to the same
  `Combatant.melee_reach_ft` the on-turn path does). A summoned Draconic
  Spirit's `melee_reach_ft` is 5 too, though its Rend reaches 10 feet; a
  commanded Rend reads the action's own range, so only a reader of the field
  sees 5.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_physical_stat_fields`,
  `::_stat_block_opportunity_attack`)
- **A shape-shifted creature keeps its items and class features
  (2026-09-25, C21a).** Casting, readying a spell and weapon attacks are
  refused, but `use_item` is not — SRD 5.2 Wild Shape: "Your ability to
  handle objects is determined by the form's limbs rather than your own";
  Polymorph: "The creature can't use or otherwise benefit from any of that
  equipment" — and a polymorphed creature can still `use_feature`, though
  Polymorph replaces its game statistics (only its Wild Shape is refused).
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_shape_shifted_failure`)
- **Beast Spells (Druid 18) is not modelled (2026-09-25, C21a).** SRD 5.2:
  "While using Wild Shape, you can cast spells in Beast form, except for any
  spell that has a Material component with a cost specified or that consumes
  its Material component." A Druid 18 in a form is refused every spell.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_shape_shifted_failure`)
- **A form can't be carried into a combat (2026-09-25, C21a).** An effect
  flagged `transform_form` passed to `start_combat(active_effects=...)` sits
  on the creature without swapping its statistics, so a Wild Shape or
  Polymorph begun before the combat is lost.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_seed_active_effects`)
- **Temporary Hit Points are one bucket (2026-09-25, C21a).** SRD 5.2: "If
  you have Temporary Hit Points and receive more of them, you decide whether
  to keep the ones you have or to gain the new ones." The engine keeps the
  larger and can't tell sources apart: Polymorph's end empties the bucket
  only when its grant raised it; otherwise the creature keeps the older
  Temporary Hit Points, and their running out ends the spell.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_revert_transform_on_expiry`)
- **No monster casts a construct or summon spell (2026-09-26, C21a; amended
  2026-09-26, C21b).** The monster AI skips a spell with no attack, save or
  damage activity of its own, so no monster casts Spiritual Weapon or Summon
  Dragon, whose only activity is a `summon`. Counting Spiritual Weapon as
  offensive waits until the Priest's data slip is fixed (see "The Priest's
  Spiritual Weapon is a data slip" under Conjuration and monster data): with
  the uuid followed, the bundled Priest opens with a Spiritual Weapon the SRD
  5.2 Priest doesn't have, where its SRD Multiattack belongs. The SRD 5.2
  caster, the Cultist Fanatic ("Spiritual Weapon (2/Day)"), then also needs a
  cell for the force (the AI picks none), its uses cap and the Bonus-Action
  move-and-repeat on later turns. A monster's Summon Dragon would also need a
  conjuration carrier (`_resolve_monster_cast` builds none, so the cast would
  stay narrative), and a foe the host drives can't cast it either: an
  `EncounterMemberSpec` carries no spell slots. A summon's allegiance already
  resolves through its caster, on either side.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_monster_cast_candidate`)
- **Spiritual Weapon's force moves through walls (2026-09-25, C21a).** Its
  Bonus-Action move is checked as a distance (the force floats), with no
  pathing, so it can cross a wall to a cell 20 feet away.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_construct_attack_failure`)
- **A monster-turn attacker's ally-enchanted weapon grants no Magic Weapon
  bonus (2026-09-25, C21a; amended 2026-10-03, C24).** Magic Weapon's
  `enchanted_weapon` flag and `weapon_enchantment_to_hit` reach only the
  on-turn `submit_player_intent` attack path; `_monster_context_kwargs` never
  threads either one, and a monster attack carries no `Weapon` object to
  enchant in the first place, so an ally's Magic Weapon on a monster
  combatant's weapon gives no bonus to hit or damage on that monster's own
  driven turn, its legendary action, or (since C24) its opportunity attack —
  all three share `_resolve_monster_attack_activities`.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_monster_context_kwargs`)
- **A Multiattack that "uses" an action counts it as a swing (2026-09-26,
  C21a).** SRD 5.2 Giant Constrictor Snake: "The snake makes one Bite attack
  and uses Constrict." `multiattack_count` sums every item the clause names,
  so the form's Attack action admits two swings and a creature polymorphed
  into the snake can Bite twice, where the SRD gives one Bite plus Constrict
  (a save action a host can't command yet). The other 17 corpus Beast
  Multiattacks count right.
  (`packages/dnd5e-engine/src/dnd5e_engine/activities/monster_actions.py::multiattack_count`)
- **A form's Multiattack count outlives the form within its Attack action
  (2026-09-26, C21a).** SRD 5.2 Wild Shape: "Your game statistics are replaced
  by the Beast's stat block" — only while in the form. The revert doesn't
  re-clamp `attacks_remaining`, so a Druid 4 who Rends once as a Black Bear,
  takes the Bonus-Action leave and then swings a Scimitar still gets the
  form's second swing. A monster's own turn has the same shape: a Giant
  Constrictor Snake form whose Bite breaks the Polymorph's concentration still
  resolves its Constrict, at the form's DC, after the revert.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_revert_transform_on_expiry`)
- **A second caster's Polymorph ends the first caster's concentration
  (2026-09-26, C21a).** SRD 5.2 Combining Spell Effects: "The most recent
  effect applies if the castings are equally potent and their durations
  overlap" — the first casting is overridden while both run, and its caster
  keeps concentrating. Polymorphing an already-polymorphed creature ends the
  first form with `remove_ieffect`, which drops the first caster's
  concentration.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_apply_transform`)
- **A form's skill modifiers lose its stat block's Expertise (2026-09-26,
  C21a).** SRD 5.2 Polymorph: "The target's game statistics are replaced by
  the stat block of the chosen Beast"; Wild Shape: "If a skill or saving throw
  modifier in the Beast's stat block is higher than yours, use the one in the
  stat block." A form adds only its skill proficiencies, so the 25 corpus
  Beast skills printed above the ability modifier plus the Proficiency Bonus
  come out low: a polymorphed Wolf's Perception is +3 against the printed +5,
  a Giant Spider's Stealth +5 (+6 wild-shaped at Druid 8) against +7.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_form_stat_fields`)

## Roster summons (2026-09-26, C21b)

- **Only Summon Dragon seats a creature (2026-09-26, C21b).** `SUMMONS` maps
  one spell to its stat block. Giant Insect's SRD stat block is variant-gated
  (its profiles point at excluded variant actors). Animate Objects needs
  objects as targets, count formulas and its Slam's
  `@flags.dnd5e.summon.mod`, and with it the `activities/formula.py` carrier
  for summon roll data (`@flags.dnd5e.summon.*` and `@item.level` still raise
  there). Finger of Death's Zombie ("A Humanoid killed by this spell rises at
  the start of your next turn as a Zombie") joins a turn later, with no
  concentration to end it. The remaining spell summons — illusions, sensors,
  lights, Mage Hand, Unseen Servant, Floating Disk, Secret Chest, Arcane Eye,
  Arcane Hand, Arcane Sword, Flaming Sphere, Guardian of Faith, Conjure
  Animals, Conjure Elemental, Conjure Fey, Faithful Hound — stay narrative.
  The spell-to-stat-block mapping is engine data; `Monster.foundry_uuid` with
  an `AssetLoader.get_monster_by_uuid` would move it into the dataset.
  (`packages/dnd5e-engine/src/dnd5e_engine/activities/conjuration.py::SUMMONS`)
- **An uncommanded summon doesn't move (2026-09-26, C21b).** SRD 5.2 Summon
  Dragon: "If you don't issue any, it takes the Dodge action and uses its
  movement to avoid danger." The engine plays the Dodge and leaves the
  creature where it stands.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_run_uncommanded_summon_turn`)
- **Summons take no reactions (2026-09-26, C21b).** A summon is in neither
  side set, and the opportunity-attack trigger takes reactors only from
  those, so it makes no opportunity attack, whoever leaves its reach.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_opportunity_attackers`)
- **The Draconic Spirit's chosen damage type is not modelled (2026-09-26,
  C21b).** SRD 5.2 Shared Resistances: "When you summon the spirit, choose one
  of its Resistances. You have Resistance to the chosen damage type until the
  spell ends." The caster gains no Resistance, and Breath Weapon's "2d6 damage
  of a type this spirit has Resistance to (your choice when you cast the
  spell)" has no choice to read. The spell's `match.saves` (Breath Weapon's
  "DC equals your spell save DC") is not carried either; it lands with the
  first commandable save action (see "Only attack-roll actions of a stat
  block can be commanded").
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_seat_summon`)
- **Summons end with the combat (2026-09-26, C21b).** Summon Dragon lasts up
  to an hour, but `EndCombatResult` and `CombatOutcome` report no surviving
  summon, and
  `start_combat` has no input for one: a concentration anchor seeded through
  `start_combat(active_effects=...)` concentrates with no creature.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_project_outcome`)
- **A departed creature can still be named (2026-09-26, C21b).** Another
  caster's concentration on a creature that left keeps running (SRD 5.2 is
  silent on a spell whose target vanishes), so its later `EffectExpired` or
  `ConditionRemoved` names the departed id. The rest of the resolution that
  drops a summon — a multiattack's later swing, an on-hit rider's save or
  condition, a weapon-mastery mark — still names it after its `CombatantLeft`
  and may leave a stale entry keyed by its id, read by nothing that walks the
  roster.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_purge_entity_state`)
- **Summon Dragon's "space that you can see" ignores light (2026-09-27,
  C21b).** SRD 5.2: "It manifests in an unoccupied space that you can see
  within range." Placement reads range, line of sight, total cover and a
  Blinded caster (with its Blindsight), but not the light or
  obscurement at the space against the caster's senses: a caster without
  Darkvision can place the spirit in Darkness or a Heavily Obscured cell.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_summon_placement`)

## Rest & recovery

- **Non-literal feature-recovery formulas are unhandled** (2026-07-04).
  `rest.recover_feature_uses` honours each feature's typed `uses.recovery`
  rules: `recoverAll` fully recharges, a literal-integer
  `formula` regains that many uses, and a period-miss (with recovery data
  supplied) correctly preserves `spent` (lr-only features do not recharge on a
  Short Rest). The residual: a NON-literal recovery formula (e.g. an
  `@abilities.*` expression) is not evaluated — the counter is left unchanged
  rather than guessed. Zero corpus occurrences today (a structural scan of
  `canonical/features` shows all 5 `formula` recovery entries are the literal
  `"1"`); thread roll-data evaluation through if such data ever lands
  (`packages/dnd5e-engine/src/dnd5e_engine/rest.py`).

## Audit 2026-08-26 — rolls & modifiers

- **Standalone out-of-combat `resolve_check` has no exhaustion seam**
  (2026-08-27) — the in-combat activity path folds `ctx.d20_test_penalty`, but
  the host-facing `CheckSpec` carries no conditions/exhaustion field, so a
  library consumer rolling a check outside combat cannot express the SRD 5.2
  `-2 x level` D20 Test penalty. Additive fix: an `exhaustion_level: int = 0`
  (or projected `modifier`) on `CheckSpec`.
  (`packages/dnd5e-engine/src/dnd5e_engine/check.py::CheckSpec`)
- **`ammunition` is parsed and never read (2026-09-02, narrowed by C15).**
  C15 wired `finesse`, `reach`, `loading` (one-shot-per-Action cap),
  `thrown` (thrown-at-range attacks), `light` (Nick's off-hand-swing
  exemption), `two_handed`/`versatile` (grip selection via
  `PlayerIntent.two_handed`; `versatile_damage` is now chosen when
  two-handed), and `heavy` (the raw-Strength-13 disadvantage gate) into the
  attack-resolution path. `ammunition` — tracking how many pieces of
  ammunition a combatant carries, and blocking an attack when the supply
  runs out — is deliberately out of scope: it is host inventory-tracking
  state, not a rules computation, and the engine models no inventory.
  (`packages/dnd5e-engine/src/dnd5e_engine/activities/attack.py`)
- **Two-Handed weapon equip legality is out of engine scope (2026-09-02).**
  SRD 5.2 requires both hands free to wield a Two-Handed weapon (and bars
  it alongside a shield); the engine has no equipped-item/hand-occupancy
  model, so `PlayerIntent.two_handed` is accepted at face value with no
  legality check, even beside a Shield the combatant has equipped
  (`Combatant.shield_equipped`, which only Great Weapon Fighting and Martial
  Arts read). Equip-slot bookkeeping is a host concern.
  (`packages/dnd5e-engine/src/dnd5e_engine/activities/attack.py`)

## Audit 2026-08-26 — action economy & turn structure

- **Search / Study / Influence now use typed check Action wrappers (2026-10-07).**
  `PlayerIntent.check.context` selects audited ability/skill combinations and
  requires a DM-supplied DC. DM/Agent still selects the check and semantics;
  the engine performs no narrative, NPC attitude or DC judgment. `utilize`,
  exploration and opposed searches remain outside this seam.

- **Help ability-check flavor resolved (2026-10-07).** Typed beneficiary/skill,
  helper proficiency, DM-declared assistance possibility/range, one-use matching
  consumption and helper-next-turn expiry share the existing Help lifecycle.
  Attack Help behavior is unchanged.

- **Help has no "target is an enemy of the helper" gate.** SRD 5.2 Help's
  attack-roll flavor requires an enemy within 5 feet; its `help_grants` bookkeeping
  accepts any `target_id`, including the helper's own ally or self, with no
  validation.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_dispatch_simple_turn_ending_intent`)
- **A redundant second Grapple attempt appends an orphaned, inert
  `ActiveEffect`.** `_handle_grapple` never checks whether the target is
  already Grappled by the same attacker before rolling a fresh save and
  appending another `"Grappled"` effect; the pre-existing condition's
  `source_effect_id` still wins, so the second effect sits in
  `live.active_effects` doing nothing until combat ends. Compounding this:
  `escape_grapple`/`_handle_escape_grapple` clears the Grappled condition
  outright on a successful escape check rather than decrementing a
  grappler-count, so a victim held by TWO grapplers (the original plus this
  redundant-attempt orphan) is freed from BOTH by a single successful escape
  — a one-check-escapes-two leniency — while the second, never-consulted
  `ActiveEffect` remains orphaned in `live.active_effects` regardless.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_handle_grapple`,
  `::_handle_escape_grapple`)
- **Monster AI never selects Dodge, Hide, Help, Grapple, or Shove.**
  `advance_monster_turn` has no branch that chooses any of the five C14
  actions; a monster only ever attacks, casts, moves, or flees.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::advance_monster_turn`)
- **Unmapped ongoing damage and persistent-area producers remain partial.**
  (2026-08-26, narrowed 2026-10-07) Typed spell timed activities now produce
  recurring start/end and next-turn-end work through `turn_lifecycle.py` for
  six verified canonical activities, including Acid Arrow's next-turn-end
  damage. PersistentAreaState now owns Spirit Guardians, Stinking Cloud,
  Ball Bearings and Caltrops, including step entry, source-follow coverage,
  boundary triggers, exclusions, per-turn gates and lifetime cleanup. Other
  unmapped effects, actively moved clouds and numbered rounds remain deferred. Monster
  regeneration/recharge remain on the driven-monster path (C18).
  See `docs/dev/spell-timed-activities.md` for the canonical audit and scope.
  (`packages/dnd5e-engine/src/dnd5e_engine/timed_activities.py`)
- **Initiative has no "Delay" option.** C14 Task 8 (2026-09-01) added the
  engine-rolled `d20 + DEX modifier` path (`initiative=None`) with Surprise
  and Incapacitated Disadvantage; the SRD "Delay" combat option (holding
  your Initiative count to act later) is still absent.
  (`packages/dnd5e-engine/src/dnd5e_engine/specs.py`)
- **Remaining movement geometry (narrowed 2026-10-08).** Crawling, explicit
  climb/swim, additive movement costs, special-speed modifiers, one shared
  turn ledger, size-aware creature-space passage and grapple dragging now
  resolve. Jumping, elevation and flying/burrowing positioning, footprints,
  and a forced shared-space turn-end consequence remain deferred. Voluntary
  starts and endpoints cannot overlap; Tiny stacking is not introduced.
  Multiple dragged victims use fixed deterministic packing; alternate follower
  formations and joint positional path searches remain outside that convention.
  (`packages/dnd5e-engine/src/dnd5e_engine/live_movement.py`)

## Audit 2026-08-26 — spellcasting & concentration

- **Complex spell activity sequences remain manual.** (2026-10-04 C26a,
  narrowed 2026-10-07) Canonical typed timing now schedules Weird, Vitriolic
  Sphere, Stinking Cloud, Acid Arrow, Ensnaring Strike and Searing Smite at
  their verified boundaries, with source cleanup and stable ordering. The
  14 audited activities for Incendiary Cloud, Tsunami, Earthquake, Delayed
  Blast Fireball, Storm of Vengeance, Forbiddance, Wall of Ice and Wall of
  Thorns are explicitly manual and no longer resolve automatically at cast
  time. Their moving areas, entry events, numbered stages or detonation state
  require further typed producers. Verified persistent producers are documented
  in `docs/dev/persistent-areas.md`; this does not enable other area spells.
  (`packages/dnd5e-srd-data/tools/translators/spell_timing.py`,
  `packages/dnd5e-engine/src/dnd5e_engine/timed_activities.py`)
- **Persistent area lifecycle implemented for four verified producers
  (2026-10-07).** Spirit Guardians follows its caster with a 15-ft Emanation,
  persists exclusions, halves Speed by current membership and shares one
  per-turn gate across entry, Emanation entry and turn end. Ball Bearings and
  Caltrops place stationary 10-ft/5-ft squares and save on first entry each
  turn (DC 10/15); Caltrops' failed-save Speed 0 ends at the target's next
  turn start. Stinking Cloud uses the same state for its stationary footprint
  and turn-start saves. AreaCreated/AreaExpired are typed events. Wall geometry,
  3D, multi-cell creatures, Incendiary Cloud active movement, Tsunami, Storm
  of Vengeance, Delayed Blast Fireball, Forbiddance and long casting times
  remain excluded. Wind dispersal and Stinking Cloud obscurement are deferred.
  (`packages/dnd5e-engine/src/dnd5e_engine/persistent_areas.py`)
- **Empty `scaling.mode` is treated as whole-mode dice scaling on upcast
  (2026-09-03, C17).** `activities/dice.py::_scaling_steps` scales dice for
  any leveled spell whose damage part carries the corpus-default
  `scaling {number: 1, mode: ""}`; Foundry treats `mode: ""` as no scaling.
  C17 added a narrow guard in `activities/damage.py` (count-bearing
  activities — those whose `target.affects.count` contains `@item.level`,
  per `spellcasting.count_scales_with_cast_level` — roll at base level so
  Magic Missile darts stay 1d4+1), but every OTHER leveled spell with an
  empty mode still gains dice per slot above base. Needs a corpus scan and
  a decision on whether `""` should mean "none" (would shift results for
  hosts upcasting such spells).
  (`packages/dnd5e-engine/src/dnd5e_engine/activities/dice.py`)
- **Magic Missile darts share ONE damage roll applied N times, not N
  independent rolls (2026-09-03, C17, ruling R5).** SRD RAW rolls each
  dart's `1d4+1` separately; `resolve_damage` rolls the part once per
  activity and applies that single result to every target in the
  count-scaled fan-out, so all darts in one cast always deal identical
  damage. This is a documented deviation kept deliberately for draw-count
  determinism (a seeded replay's RNG draw count does not grow with slot
  level) rather than a gap to close.
  (`packages/dnd5e-engine/src/dnd5e_engine/activities/damage.py::resolve_damage`)
- **Pool choice for a multiclass Warlock cast is Spellcasting-first
  (2026-09-03, C17 R3); no per-class spell-list gate or `use_pact_slot`
  flag.** `_slot_available`/`_take_spell_slot` always try the regular
  Spellcasting pool before Pact Magic; SRD §Multiclassing lets either pool
  cast either prepared spell, so this is correct for slot AVAILABILITY, but
  a caller cannot force a specific pool to be spent (e.g. to bank
  Spellcasting slots and burn Pact Magic first, or vice versa).
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_take_spell_slot`)
- **Attack-kind repeat instances (Scorching Ray's rays) are not
  count-scaled (2026-09-03, C17).** R5's target-count scaling
  (`resolve_target_count` / `target.affects.count`) only expands a
  `damage`-kind activity's target list; the corpus encodes an
  `attack`-kind spell's multiple-instances count (Scorching Ray: "three
  rays", one attack roll each) only in description prose, not in a typed
  field the resolver reads, so a single Scorching Ray cast still resolves
  one attack roll regardless of slot level.
  (`packages/dnd5e-engine/src/dnd5e_engine/activities/attack.py`)
- **Long Rest 16-hour cooldown / rest interruption are host time-model
  concerns (2026-09-03, C17).** SRD 5.2 §Long Rest: "After you finish a
  Long Rest, you must wait at least 16 hours before starting another one."
  It also lists interrupting activity (walking, fighting, casting a spell)
  as voiding progress; `resolve_long_rest`/
  `resolve_short_rest` are pure functions with no calendar/clock input, so
  neither the cooldown nor interruption tracking exists — a host wanting
  either must gate its OWN call to these resolvers.
  (`packages/dnd5e-engine/src/dnd5e_engine/rest.py`)
- **Unsupported activity formulas and empty saves need earlier validation
  (narrowed 2026-10-08).** Delayed Blast Fireball reads `@item.uses.value`,
  Spider Climb reads `@attributes.movement.walk`, and Phantasmal Killer's save
  names no ability; the formula resolver has no handler for either token and
  the save resolver refuses an empty ability. These clauses remain unsupported
  and require validation before payment wherever they can reach resolution.
  Tsunami's unsupported wall geometry is now refused by shared delivery
  preflight before slot payment; it is no longer evidence of a late wall cast.
  (`packages/dnd5e-engine/src/dnd5e_engine/activities/formula.py::_resolve_token`)
- **Arbitrary Ready spell targeting and held-spell rules remain unsupported
  (narrowed 2026-10-07).** The engine-specific pre-arm API accepts only
  supported canonical reaction conditions and typed target roles. Shield
  targets self, Counterspell the triggering caster and Hellish Rebuke the
  damage source. Sunbeam, Wall of Ice and other non-reaction spells cannot
  become readied reactions through a host trigger string; they are refused
  before Action payment. SRD arbitrary Ready and its held concentration/
  casting-time semantics need a separate contract.
  (`packages/dnd5e-engine/src/dnd5e_engine/live_reactions.py::prearm_failure`)
- **A potion's spell concentrates (2026-09-26, pre-existing).** SRD 5.2
  potions give a spell's effect "(no Concentration required)" — Potion of
  Speed: "the effect of the *Haste* spell for 1 minute (no Concentration
  required) without suffering the wave of lethargy". A potion's effects keep
  the spell's concentration flag and join the drinker's concentration chain,
  and both options land at once: Potion of Speed applies `effect:hasted` and
  `effect:lethargy`, Potion of Growth `effect:enlarged` and `effect:reduced`.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_writeback_concentration`)

## Audit 2026-08-26 — character derivation

`derive_sheet` (C19) closed most of this audit's findings: HP, AC, hit dice
and skill/save proficiencies are now computed engine-side from
`CharacterBuildSpec`, and `build_party_member` folds them into
`PartyMemberSpec` for any value a host leaves unset. The bridge's `sheet.py`
now calls the engine rather than standing in for it. Residual gaps:

- **Group checks and tool proficiencies are absent.** Neither is derived from
  class/background/equipment, nor accepted as an explicit build input.
  (`packages/dnd5e-engine/src/dnd5e_engine/build_spec.py`)
- **Class features that are prose-only in the corpus (amended 2026-09-24,
  C20):** Divine Smite, Metamagic / Sorcery Points and Eldritch Invocations.
  Nothing ties a Divine Smite cast to the Melee weapon or Unarmed Strike hit
  it rides, and Paladin's Smite's free cast ("you can cast it without
  expending a spell slot, but you must finish a Long Rest before you can cast
  it in this way again") has no activity. `metamagic.json`, its option
  features, `eldritch-invocations.json` and the invocation features carry no
  activities. Font of Magic's two conversions price Sorcery Points with
  formulas (`1 + @scaling + floor(@scaling / 3)`, `0 - @scaling`) the engine
  doesn't evaluate, so each spends one point. None of these has an approved
  scenario.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_feature_activity_cost`,
  `packages/dnd5e-srd-data/src/dnd5e_srd_data/canonical/features/paladins-smite.json`)
- **Equipment:** ammunition decrement, shield don/doff and encumbrance are
  absent (C19 validates `attuned_items` against the attunement limit and
  `requires_attunement`; C15 picks versatile damage from
  `PlayerIntent.two_handed`). Magic-item charges are the one other equipment
  mechanic that is real.
  (`packages/dnd5e-engine/src/dnd5e_engine/build_spec.py`)
- **No Heroic Inspiration** (reroll) anywhere; XP is summed at `end_combat`
  but no threshold table / level-up path exists (host concern; noting the hook).

## Audit 2026-08-26 — monsters

- **Typed traits are hydrated; only Flyby, Nimble Escape, Keen Senses and
  Aggressive are still unconsumed (amended 2026-09-03, C18).**
  `Combatant.trait_mechanics` carries the 14 `MonsterTraitMechanic` values
  (C22). Magic Resistance grants save advantage against spells and explicitly
  magical sources, including captured typed repeat saves (2026-10-08). Ordinary
  feature saves, Grapple/Shove and concentration maintenance are nonmagical.
  Unmapped magical producers remain deferred. C18 landed
  Pack Tactics (attack advantage), Sunlight Sensitivity (attack and all ability-check
  disadvantage in sunlight; typed check pipeline 2026-10-07), Undead Fortitude
  (CON save to hold at 1 HP), Swarm (no HP/temp-HP gain) and Legendary
  Resistance. Flyby (no flying-movement tracking) and Nimble Escape
  (untyped bonus action; the monster AI takes no bonus actions) are not
  modelled; Keen Senses and Aggressive are absent from the SRD 5.2 corpus
  entirely.
  (`packages/dnd5e-engine/src/dnd5e_engine/activities/save_primitive.py`)
- **Target selection is hard-coded lowest-HP living enemy** (a PC or a
  party-side summon since C21b), with no reach/LoS/threat
  consideration — the monster AI never consults
  `_combatant_can_see` or a Frightened monster's own line-of-sight/
  no-approach rule when choosing a target or walking; confirmed still open
  after C18 (2026-09-03) — a rule card scoped it out as not an SRD rule, so
  this stands as a deliberate scope cut, not an oversight.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_select_monster_targets`)
- **`PartyMemberSpec` has no `physical_resistances_nonmagical_only`
  counterpart** (2026-08-30). PCs are pinned to the nonmagical-only reading of
  host-authored B/P/S resistances; a PC whose resistance should be
  unconditional cannot express it.
  (`packages/dnd5e-engine/src/dnd5e_engine/specs.py`)
- **Bridge does not serve the `conditions` / `traits` categories**
  (2026-08-27) — `routes_content._CATEGORIES` predates C22.
  (`packages/nat20-bridge/src/nat20_bridge/routes_content.py`)

## Not modelled by design (recorded so nobody re-audits them)

Hiding vs passive
Perception, falling, suffocation/drowning, underwater, extreme weather,
hazards/traps, objects as targets, mounted combat, elevation, multi-tile
footprints. Exploration-tier; revisit only if a host asks. Mounted combat
covers the steeds Find Steed and Phantom Steed summon (SRD 5.2 Find Steed:
the steed "functions as a controlled mount while you ride it"), so both
spells stay narrative (2026-09-26, C21b;
`packages/dnd5e-engine/src/dnd5e_engine/activities/conjuration.py::SUMMONS`).

## Foundations follow-ups (2026-08-26)

Reviewed and deliberately deferred during the F1–F3 foundations pass (actor
stat projection, unified d20, turn lifecycle). Each is additive and none blocks
a cluster; they are consolidated here so they are not re-discovered.

- **`AdvantageMode` and `TurnPhase` are not top-level exports.** Both live on
  `dnd5e_engine.events` and are reachable there, and the package exports no
  other event class or roll-mode alias from `__init__.py`, so the asymmetry is
  consistent rather than an omission. Revisit only if the whole event surface is
  re-exported. (`packages/dnd5e-engine/src/dnd5e_engine/__init__.py`,
  `packages/dnd5e-engine/src/dnd5e_engine/events.py`)
- **Roll events cannot report bonus DICE.** `roll_total == natural + modifier`
  only when no Bless/Bane-style bonus die applied; `modifier` deliberately
  excludes them (they are rolled after the d20 to keep the seeded stream
  stable), so the printed breakdown does not add up in that case. Fix shape: an
  additive `bonus_dice_total: int | None` on `AttackRolled` / `SaveRolled` /
  `CheckRolled` / `ConcentrationCheck`.
  (`packages/dnd5e-engine/src/dnd5e_engine/events.py`)
- **`TurnLifecycle` has no public registry introspection.** The registration
  ORDER of turn hooks is a determinism contract, and the only way to assert it
  is reading the private `_hooks` list (`tests/test_turn_lifecycle.py` does).
  A `registered_keys(phase) -> tuple[str, ...]` accessor is the clean fix.
  (`packages/dnd5e-engine/src/dnd5e_engine/turn_lifecycle.py`)
- **The turn-start log index is recomputed per candidate effect.**
  `_effect_applied_during_current_turn` rescans the event log for each
  until-end-of-next-turn effect at a turn boundary. Bounded and immaterial at
  today's scale (C18 added no such hook — its monster turn-start mechanics
  run from `_run_monster_turn_start`); if a *second* log-reading turn hook is
  ever added, hoist a
  single `current_turn_start_index` computed once in `_begin_turn` and have
  both hooks read it.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_begin_turn`)
- **A turn-start or round-start hook that removes a creature would strand
  the turn (2026-09-27, C23).** A current actor that leaves the initiative
  order hands its turn on through `_hand_off_departed_turn` once the intent
  or legendary action that removed it has resolved. `_begin_turn` runs the
  `round_start` and `turn_start` hooks and the pending death save with no
  such hand-off, so a removal there would leave `departed_actor_id` set and
  no `TurnStarted`. No registered hook can remove a creature today (the
  turn-start one expires reaction effects); whoever adds one needs the
  hand-off too.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_begin_turn`)

## Damage at 0 Hit Points (2026-08-29)

Surfaced while landing C12-S06 (Characters fall Unconscious at 0 HP).

C15 added `DamageApplied.is_crit`; the reaction architecture now also adds a
deterministic `damage_instance_id`. The live fold counts one failure for one
multi-type hit at 0 HP, or two for a critical hit, while separate hits remain
separate instances.
- **A death-save failure from damage at 0 HP surfaces no event.** The failure is
  written straight onto `Combatant.death_saves`; hosts narrating from the event
  stream see the `DamageApplied` but never learn a failure landed (only
  `DeathSaveRolled`, from the turn-start roll, carries counters). The fix needs
  a new `CombatEvent` member or a counters payload on an existing one.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py::_apply_zero_hp_to_character`)

## Conditions — SRD 5.2 rows not enforced (2026-08-27)

C12 gave all 15 conditions teeth on the live combat path (see
`docs/capabilities.md`). These rows are what is left; each needs a seam another
cluster owns.

Resolved on 2026-10-06 by the source/context-relative condition migration:
Frightened's ability-check LOS gap (recorded 2026-09-02) now consumes its
canonical sight-gated clause with live fear-source visibility. The erroneous
no-approach LOS gate (recorded 2026-09-03) is removed: a known, living, tracked
source still blocks a distance-reducing path while unseen. Regression tests
isolate both behaviors in the same wall/darkness/invisibility scenes.
Monster movement now uses the same per-step Frightened approach check as PC
movement through the shared physical movement adapter.

- **Blinded/Deafened typed check auto-fail resolved (2026-10-07).** Explicit
  sight/hearing requirements consume canonical clauses without d20 draws.
  Skills never imply senses. See `docs/dev/typed-ability-checks.md`.

- **Charmed social check Advantage resolved (2026-10-07).** Explicit typed
  social-interaction checks require actual charmer lineage, including imposing
  effect origins; ordinary Charisma checks receive no social bonus.

## C12 deferred minors (2026-08-27)

Small, real and deliberately not worth their own task; recorded so they are not
re-discovered.

- **`_check_modifier` skill penalty regression resolved (2026-10-07).** A direct
  skill-branch test pins Exhaustion; live checks also combine the declarative
  penalty with active-effect bonus dice in deterministic order.

- **The `enumerate(live.initiative)` → `model_copy` → slot-replace loop is
  still open-coded 38 times**, although `_update_combatant(live, entity_id,
  **fields)` now exists (C21) and 9 call sites use it.
  (`packages/dnd5e-engine/src/dnd5e_engine/orchestrator.py`)

## Blocked

- **Lair actions are blocked on translator support** (2026-09-03, C18 R10).
  All 341 bundled monsters ship `lair_actions == []` (schema field exists,
  the translator never populates it from a Foundry source), so there is
  nothing for the engine to spend. The initiative-20 pseudo-turn a lair
  action needs (a scene-owned action outside any creature's own turn) is
  designed but unimplemented; blocked on dataset/translator work to source
  lair-action content before an engine seam is worth building.
  (`packages/dnd5e-srd-data/src/dnd5e_srd_data/schema/monster.py`,
  `packages/dnd5e-engine/src/dnd5e_engine/activities/monster_actions.py`)
- **`custom` `ActiveEffectChange` mode — needs a product decision** (2026-07-02).
  No Foundry-core semantics to port: Foundry itself delegates `custom` to
  host-registered `applyChangeCustom` callbacks, so there is no SRD or
  in-repo ground truth for what it should do in this engine
  (`packages/dnd5e-engine/src/dnd5e_engine/rules/effects.py`,
  `apply_changes_to_check` — `multiply`/`upgrade`/`downgrade` are
  implemented; `custom` is left a documented no-op). Downstream data
  carries none today — verified: `grep -rn '"mode": "custom"'
  packages/dnd5e-srd-data/src/dnd5e_srd_data/canonical/` returns zero
  matches. Blocked on a maintainer decision for what (if anything) `custom`
  should mean in a host-agnostic engine with no callback registry.

---

# nat20-bridge

The SillyTavern sidecar (`packages/nat20-bridge`) is a thin FastAPI routing
layer over the engine — see `docs/bridge.md`. Gaps found while shipping it:

- **`BuildRequest` lacks the C19 build-spec fields (2026-09-23).** The
  bridge's HTTP model still exposes only `species_slug`, `class_slug`,
  `subclass_slug`, `level`, `ability_scores` and `equipment` — a caller
  cannot reach `classes` (multiclass), `background_slug`,
  `selected_choices`, `hp_mode`/`hp_rolls`, `ac_calc_mode`, `attuned_items`
  or `ability_score_method` over `/v1/party/validate` or `/v1/combat`, even
  though `sheet.py` now derives from all of them.
  (`packages/nat20-bridge/src/nat20_bridge/models.py::BuildRequest`)
- **Global-`random` seeding is not safe under concurrent requests**
  (2026-08-21). `_start_route` (and `app.py`'s `_do_roll`/`_do_check`) seed the
  stdlib global `random` module to make the engine's legacy dice seam
  (`roll_dice_str`, `rules/effects.py`) reproducible per request, since that
  seam reads the global module rather than an injectable RNG. Two `/v1/roll`,
  `/v1/check`, or `/v1/combat` requests racing concurrently (different seeds)
  can have one request's reseed clobber the other's before its dice resolve —
  fine for the bridge's current single-connection, same-machine ST usage, not
  safe for concurrent multi-client load. Real fix: thread an injectable
  `random.Random` through the legacy dice paths instead of reading the global
  module (`packages/nat20-bridge/src/nat20_bridge/routes_combat.py`).
- **Collector tasks + event logs leak for combats never `/end`ed**
  (2026-08-21). `BridgeState.combats`/`events_log`/`names`/`seeds`/`collectors`
  are only cleaned up by `_end_route` (`state.combats.pop`, `_stop_collector`)
  — a combat a client abandons without calling `POST /v1/combat/{cid}/end`
  keeps its background collector task running and its event log growing for
  the life of the bridge process. Needs either a TTL/idle-reap sweep or an
  explicit cap on live combats (`packages/nat20-bridge/src/nat20_bridge/state.py`).
- **`attack_bonus` derivation ignores Dexterity / finesse weapons**
  (2026-08-21). `derive_sheet`'s `attack_bonus = proficiency + str_mod`
  (`sheet.py`) always uses the Strength modifier, regardless of whether the
  character's weapon is finesse (SRD 5.2: finesse lets the wielder use either
  Strength or Dexterity, typically Dexterity for a Rogue/ranged-leaning build)
  or a ranged weapon (which SRD-legally uses Dexterity, not Strength, absent a
  feat). A Dex-based Rogue or ranged character gets an under- or over-stated
  attack bonus in `/v1/party/validate` and `/v1/combat` party derivation.
  Needs the weapon's `properties`/`weapon_kind` consulted to pick
  `max(str_mod, dex_mod)` for finesse or `dex_mod` for ranged
  (`packages/nat20-bridge/src/nat20_bridge/sheet.py`).
- **The combat intent carries no class-feature or form field (2026-09-24, C20
  scope cut; amended 2026-09-25, C21a, 2026-09-26, C21b, and 2026-10-04,
  C26a).** `_IntentRequest` forwards only
  `intent_type`, `spell_id`, `target_id`, `item_id`, `weapon_id`,
  `feature_id` and `target_zone_id`, so a bridge client can't aim or shape an
  area (`direction`, `target_ids`, `excluded_target_ids`), pick an
  `activity_id` (Flurry of Blows, Lay on Hands, Channel Divinity), set
  `use_bonus_action` (Cunning Action, the Bonus Unarmed Strike) or
  `two_handed`, draw `pool_points`, redeem a Bardic die
  (`redeem_granted_die`), name a Wild Shape or Polymorph form (`form_id`:
  both are refused with `invalid_form`) or command a stat-block attack
  (`stat_block_action_id`) — so a summoned Draconic Spirit, whose only
  commandable attack is its Rend, can't be made to attack at all.
  `_view_route` doesn't expose `LiveCombatView.turn`, `constructs`,
  `transformations` or `summons` either, so `extra_actions_remaining`, a
  Spiritual Weapon force, a creature's form and a summon's caster aren't
  visible over HTTP.
  (`packages/nat20-bridge/src/nat20_bridge/routes_combat.py::_IntentRequest`)
- **The narration names a summon by its id (2026-09-26, C21b).** The bridge
  builds its name map when `/v1/combat` starts, so a creature that joins later
  narrates as `summon:<caster>:<stat block>:<n>` ("…'s turn begins"), and its
  `combatant_joined` / `combatant_left` events fall back to generic lines;
  the `name` a `CombatantJoined` carries never reaches the map.
  (`packages/nat20-bridge/src/nat20_bridge/routes_combat.py::_intent_route`)

---

# dnd5e-srd-data

## Oracle coverage (2026-08-22)

- **The monster oracle covers 3 of 341 monsters (0.9%).** `tests/oracle/
  srd_monster_oracle.json` holds three entries; `test_canonical_against_oracle.py`
  compares only slugs the oracle contains, so **338 monsters have no fidelity
  check at all** and the suite still reports green. Spells (93%) and items (75%)
  are well covered by comparison. `tests/test_oracle_coverage_floor.py` now pins
  the current ratios so they cannot regress — raise the `monsters` floor as
  entries are added. Subclasses are similarly thin at 4 of 12 (33%).
  (`packages/dnd5e-srd-data/tests/oracle/srd_monster_oracle.json`)

## Shipped prose quality (2026-08-22)

- **All 341 monsters ship `description: ""`.** A host has nothing to render for
  any creature in the corpus. The translator populates per-action descriptions
  but never the top-level one.
  (`packages/dnd5e-srd-data/tools/translators/`)
- **Residual Foundry enricher markup and HTML entities in shipped prose.**
  Roughly 1,000 of 1,545 canonical files carry unresolved markup: 728
  `&Reference[…]`, 2,162 `[[lookup …]]`, 717 `&amp;` double-escapes, 1,018 HTML
  tags. **268 of the 931 monster action descriptions (28%) are essentially raw
  macros** — goblin-warrior's Scimitar reads
  `"[[/attack extended]]. [[/damage average extended]]…"`. A `cleanup_prose`
  translator exists and is unit-tested but is evidently not applied to these
  fields. Note that `[[/item …]]` tokens are load-bearing — the engine's
  multiattack fan-out parses them — so cleanup must preserve them while
  resolving `[[lookup]]`, `&Reference[]` and entity escapes.
  (`packages/dnd5e-srd-data/tools/translators/prose_cleanup.py`)
- **`monsters/ancient-gold-dragon.json` ships an unfilled template.** Its
  multiattack description is literally
  `"makes {count} [[/item]] attacks and uses [[/item]]"`, so the action cannot
  fan out. Every sibling ancient dragon names its Rend attack correctly, so the
  defect appears to be upstream rather than in our translator. Registered in
  `tests/oracle/known_prose_defects.json` and gated by
  `tests/test_corpus_prose_integrity.py`; re-check on the next
  `make refresh-upstream` and de-register if upstream has fixed it.
- **Inherited activation `type` is not resolved** (2026-08-27). An activity
  with `activation.override: false` inherits the item-level activation in
  Foundry (Shield's utility activity is a Reaction, but canonical stores the
  activity's own `type: action`). C22 derives `reaction_conditions` from the
  effective block but leaves `type` as shipped. Resolving it changes bytes on
  every inheriting activity — do it as its own regen PR.
  (`packages/dnd5e-srd-data/tools/translators/foundry.py::_effective_activation`)

## Monster spellcasting ability gaps (2026-09-04)

- **`mummy-lord.spellcasting_ability` is `None` despite the monster casting
  spells.** Its Foundry source (`actors24/undead/mummy-lord.yml`) carries no
  usable signal: the top-level `attributes.spellcasting` is the `"str"`
  non-caster placeholder and every one of its cast activities' own
  `spell.ability` is empty — the ability is stated only in trait prose. It is
  the sole cast-bearing monster (of 65) the translator cannot resolve.
  (`packages/dnd5e-srd-data/tools/translators/foundry.py::_spellcasting_ability`)
- **Coven-shared casting can outvote a monster's own spellcasting ability.**
  `sea-hag.spellcasting_ability` resolves to `"int"` because its shared
  "Coven Magic" trait contributes 6 cast activities at `int` versus 1 at its
  true personal ability (`con`, per `attributes.spellcasting: con` and its
  own "Illusory Appearance" cast activity) — majority-vote-by-activity-count
  picks the coven ability over the personal one. `green-hag` has the
  equivalent issue (resolves `"int"` via Coven Magic instead of its personal
  `"wis"`). A future pass could special-case or exclude coven/shared-trait
  cast activities from the vote.
  (`packages/dnd5e-srd-data/tools/translators/foundry.py::_spellcasting_ability`)

## Magic-armor category and template defects (2026-09-23, C19)

`derive_sheet`'s AC formula reads each worn armor's `armor_category` and
`dex_bonus_max`. Several corpus items disagree with real armor math:
`dwarven-plate` ships `armor_category: "light"` with `dex_bonus_max: null`
(uncapped) on a base AC of 18, so a derived sheet adds the wearer's FULL
Dexterity modifier on top of 18 instead of a capped or ignored one;
`elven-chain`, `demon-armor` and `plate-armor-of-etherealness` carry the
same class of category/cap defect. The `armor-1-2-or-3`, `shield-1-2-or-3`,
`adamantine-armor` and `mithral-armor` crafting templates all ship
`base_ac: 10` regardless of the real armor they are meant to modify —
placeholders for a translator rule that never resolves the underlying base
item. None of this is a `derive_sheet` bug; the fix belongs in the
translator/dataset.
(`packages/dnd5e-srd-data/src/dnd5e_srd_data/canonical/items/dwarven-plate.json`,
`elven-chain.json`, `demon-armor.json`, `plate-armor-of-etherealness.json`,
`armor-1-2-or-3.json`, `adamantine-armor.json`, `mithral-armor.json`)

## Feature-choice pools (2026-09-24, C20)

The Champion's level-7 Fighting Style `ItemChoice` pool
(`subclasses/champion.json`) copies Foundry's full list. Besides the four SRD
5.2 styles it names Blind Fighting, Dueling, Interception, Protection, Thrown
Weapon Fighting and Unarmed Fighting, whose UUIDs resolve to nothing in the
corpus. Those six are not SRD 5.2 content and must not be implemented;
`FightingStyle` accepts only the four. The translator could drop unresolvable
pool entries.
(`packages/dnd5e-srd-data/src/dnd5e_srd_data/canonical/subclasses/champion.json`)

## Class scale-value defects (2026-09-25)

- **A Barbarian 16 has 6 Rages; SRD 5.2 gives 5.** The SRD 5.2 Barbarian
  Features table lists 5 Rages from level 12 through 16 and 6 from level 17,
  but the Rages `ScaleValue` in `classes/barbarian.json` steps to 6 at level
  16 (`"16": 6`), so the Rage use cap lets a Barbarian 16 rage once too often.
  This predates C20. The fix belongs in the translator, as a correction like
  `_WEAPON_BASE_DAMAGE_CORRECTIONS`.
  (`packages/dnd5e-srd-data/src/dnd5e_srd_data/canonical/classes/barbarian.json`,
  `packages/dnd5e-srd-data/tools/translators/foundry.py`)

## Conjuration and monster data (2026-09-25, C21a)

- **26 conjuration actors and 7 magic-item actors are quarantined
  (2026-09-25, C21a).** 24 conjuration and companion stat blocks and the 7
  items fail on `'custom' is not a valid CreatureType`, and 2 stat blocks on
  all-zero ability scores, so none ships. Spiritual Weapon's upstream attack
  lives on one of them; the engine carries that attack in a registry instead
  (`CONSTRUCTS`). Shipping them needs Foundry's `custom` type mapped, and
  belongs with the spells that need them.
  (`packages/dnd5e-srd-data/tools/translators/foundry.py`)
- **The Priest's Spiritual Weapon is a data slip, and the Cultist Fanatic's
  has no uses cap (2026-09-25, C21a).** `actors24/humanoid/priest.yml` points
  the Priest's 1/Day Spellcasting at Spiritual Weapon's uuid, while the
  entry's own text, like the SRD 5.2 Priest, says "1/Day Each: *Spirit
  Guardians*"; the engine follows the uuid. The SRD 5.2 caster is the Cultist
  Fanatic ("Spiritual Weapon (2/Day)", a Bonus Action), whose corpus entry
  carries no uses cap. Monster casts of Spiritual Weapon wait on this row (see
  "No monster casts a construct or summon spell" under Conjurations and
  shape-shifting).
  (`packages/dnd5e-srd-data/tools/translators/foundry.py`)
- **A monster attack's range inherited from its item is not resolved
  (2026-09-25, C21a).** An activity with `range.override: false` takes its
  item's range in Foundry, but canonical stores the activity's own
  (`units: "self"`), so the Ape's Rock (SRD 5.2: range 25/50 ft.) reads as a
  5-foot melee reach, for the monster AI and a commanded stat-block swing
  alike. It is the same inheritance regen as the activation row under
  "Shipped prose quality".
  (`packages/dnd5e-srd-data/tools/translators/foundry.py`)
- **Two monster Armor Classes disagree with SRD 5.2 (2026-09-25, C21a).**
  `elk` ships Foundry's flat natural armor 11 against the SRD's 10 (flat
  values are kept as shipped), and `flying-snake` derives 12 against 14 (a
  recorded divergence in `tests/oracle/known_oracle_divergence.json`).
  (`packages/dnd5e-srd-data/tools/translators/foundry.py::_monster_ac`)
- **Five Beast attacks differ from SRD 5.2 (2026-09-26, C21a).** Foundry data
  that Wild Shape and Polymorph now put in a creature's hands: Giant Frog Bite
  1d6 + 1 (SRD "5 (1d6 + 2) Piercing"), Giant Crab Claw and Giant Octopus
  Tentacles Slashing (SRD Bludgeoning), Swarm of Insects Bites Piercing (SRD
  "6 (2d4 + 1) Poison"), and Swarm of Venomous Snakes Bites with no Poison
  rider (SRD "plus 10 (3d6) Poison damage"). Canonical content flows only
  through the translator, so the fix is a correction there or a recorded
  divergence per monster.
  (`packages/dnd5e-srd-data/tools/translators/foundry.py`)
- **Three monster recharges disagree between the pinned Foundry pack and the
  SRD 5.2 creature text (2026-10-04, C26a).** The Ghost's Possession ships no
  recharge (open5e's SRD 5.2 text: Recharge 6), the Minotaur of Baphomet's Gore
  recharges on a 6 (5–6), and the Succubus's Charm on 5–6 (no recharge). None
  is an area action, so C26a's correction table left them; each needs checking
  against the SRD 5.2 document before it gets a correction row.
  (`packages/dnd5e-srd-data/tools/translators/foundry.py::_MONSTER_ACTION_CORRECTIONS`)

---

# Test & fidelity

- **Real-Foundry parity fixtures.** Engine activity-resolution tests run against
  author-derived expected event streams, not byte-for-byte Foundry ground truth.
  Capturing ~12 parity fixtures (concentration cascades, multi-target ordering,
  forward/delayed activity composition) behind a Foundry license would replace
  the author-derived expectations. The fixture schema is already capture-ready
  (`{scenario_id, inputs, expected_events}`), so the swap is drop-in.
