# Bridge typed intent parity (B9)

The HTTP intent model inherits all 33 Engine `PlayerIntent` fields. Tests pin the
exact field and intent sets below, the OpenAPI schema and representative JSON
round trips. “Full” means an equivalent transport for the Engine contract;
“Partial” means the Engine itself has explicit bounded behavior. Transport parity
does not admit additional spells or promise unsupported rules.

## Every intent

All rows use `POST /v1/combat/{cid}/intent`, with `actor_id` and preferably a stable
`request_id`. Responses include ordered typed events, narration, `over` and a
receipt. Discovery is `GET /v1/combat/{cid}` → `state`, from the pinned combat.

| Intent | Parity | Required or relevant data | Authoritative result / boundary |
| --- | --- | --- | --- |
| `activate_spell` | Full | Owned discovered `source_id`, allowed `activity_id`; delivery decisions | Magic Action; Sunbeam repeats and Moonbeam destination relocation; no new slot or clock |
| `check` | Full | Nested `check`, including matching actor, ability, skill/tool, DC, context and senses | Typed check; Engine owns action cost and scope |
| `attack` | Partial | Creature target; weapon, stat-block action or existing construct spell; optional riders, grip, bonus budget, granted die | Attack/save/damage events; current Engine weapon/form/construct boundaries |
| `cast_spell` | Partial | Reviewed known spell, slot where needed, required creature/point/object/choice/willing inputs | Admission and delivery preflight; Host narrative/Deferred remain explicit |
| `use_item` | Partial | Item, selected Activity/charges and delivery | Reviewed item contract and consumption; no general inventory |
| `move` | Full | Canonical destination; movement mode | Shared weighted path, costs, reactions and area triggers |
| `dash` | Full | Optional bonus budget or discovered grant | Movement allowance and budget events |
| `dodge` | Full | Current actor | Action and Dodge state |
| `disengage` | Full | Optional bonus budget or discovered grant | Budget and opportunity-attack protection |
| `hide` | Full | Optional bonus budget or discovered grant | Authoritative visibility/check gate |
| `help` | Full | Attack target or nested `help_check` beneficiary/skill/assistance/range/possibility | Engine check grant or attack Help |
| `ready` | Partial | Existing Engine ready options: spell/weapon, target and compatible trigger | Pre-armed reaction; current Engine trigger allowlist |
| `reaction` | Partial | Existing pre-armed spell/weapon/target/trigger options | Engine reaction queue; no arbitrary trigger inference |
| `move_mark` | Partial | Existing mark spell and creature target | Engine's owned mark-transfer contract |
| `use_feature` | Partial | Feature, optional Activity, pool points/form/target | Audited feature repertoire and resources |
| `pass` | Full | Current actor | End turn |
| `drop_concentration` | Full | Current actor | Existing chains/effects/areas end synchronously |
| `grapple` | Partial | Creature target, optional eligible bonus/grant budget | Existing size/reach/save/lineage; free hands remain Host boundary |
| `shove` | Partial | Creature target, push-versus-Prone choice, budget | Existing save/forced-movement contract |
| `stand_up` | Full | Prone actor with sufficient movement | Shared movement ledger |
| `escape_grapple` | Full | Grappled actor | Existing escape action/check |

## Every input field

All fields have full JSON representation. Scalar types and unknown fields are
strictly checked with the Engine's JSON schema; JSON arrays represent tuples.
The Engine retains its Python tuple/list ergonomics. Optional union fields can
be irrelevant for a particular intent where the Engine explicitly documents that
behavior; this adapter does not invent a stricter alternative SRD dispatcher.
Missing necessary choices and illegal source/delivery/resource combinations are
refused by shared Engine preflight before payment. Invalid wire shapes return 422.

| Field | Input / consumer |
| --- | --- |
| `intent_type` | Closed 21-value Engine enum above |
| `action_grant` | `[effect_id, origin]` from restricted grant discovery |
| `source_id` | Owned ongoing-source identity |
| `check` | Full `CheckRequest`, including senses, advantage sources and granted die |
| `help_check` | Full `HelpCheckSpec` |
| `spell_id` | Canonical spell or existing construct/mark reference |
| `target_id` | Creature identity |
| `target_ids` | Array of selected creature identities; Engine count/duplicate policy |
| `effect_selections` | Array of spell/Activity/target/effect identities; canonical alternatives |
| `willing_target_ids` | Explicit Host attestation; never inferred from allegiance |
| `item_id` | Canonical item identity |
| `weapon_id` | Weapon identity |
| `feature_id` | Feature identity |
| `activity_id` | Canonical selected feature/item/spell/ongoing Activity |
| `attack_riders` | Full rider identities, option IDs, push distance and typed movement choice |
| `reckless_attack` | Strict boolean |
| `slot_level` | Integer or null; Engine determines legal levels and payment |
| `charges_to_spend` | Positive integer; Engine charge scaling |
| `pool_points` | Positive integer; Engine feature pool scaling |
| `redeem_granted_die` | Closed Engine grant identity |
| `reaction_trigger` | Deprecated compatible alias; canonical triggers remain authoritative |
| `target_zone_id` | Canonicalized `col,row` placement/destination |
| `target_object_id` | Discovered combat object identity; Engine unattended casting gate |
| `object_include_origin` | Strict boolean Emanation-origin decision |
| `movement_mode` | walk/crawl/climb/swim |
| `direction` | Nonzero two-integer JSON array |
| `excluded_target_ids` | Distinct nonempty creature IDs; Engine area-choice policy |
| `use_bonus_action` | Strict JSON boolean; Engine eligibility |
| `shove_push` | Strict JSON boolean |
| `two_handed` | Strict JSON boolean |
| `as_ritual` | Strict JSON boolean; combat ritual remains refused |
| `form_id` | Canonical Beast identity; existing transformation rules |
| `stat_block_action_id` | Existing form/monster attack action identity |

## Discovery and controlled Host operations

`state` includes the public `LiveCombatView`: ongoing sources with owner,
canonical Activity allowlist/cost/relocation limits and captured DC/slot; restricted
grants with owner, full effect identity, allowed classifications and unspent budget;
environmental source footprints/origins/clocks/suppression; object carriers with
mutation owner, holder/position/cover and attached areas. It also includes turn,
slots/counters, concentration, constructs, transformations and summons. Candidate
budgets are not a promise of legality: turn, conditions, target, permission and
payment still pass through Engine preflight. Set-valued footprints are sorted.
`execution_serial` advances for committed execution; a pure typed refusal keeps
gameplay state unchanged. `event_count` also identifies refusal publication and
lets the single collector verify complete publication. It is event-history
metadata, excluded from Python gameplay-view equality but included in HTTP JSON;
the execution serial and all gameplay fields still participate in equality.

`POST /v1/combat/{cid}/host/objects` accepts `{request_id, objects:[CombatObject]}`.
`POST /v1/combat/{cid}/host/object` accepts `{request_id, mutation:ObjectMutation}`.
`POST /v1/combat/{cid}/host/strong-wind` accepts `{request_id, wind:StrongWind}`.
These are the existing public Engine APIs, with no route-owned rules or costs.
Host policy is default-denied. The embedding application configures
`BridgeState(host_tokens={token: owner_id})`; callers send `Authorization: Bearer token`.
Object mutation authority comes from this binding, not a caller's owner string.
Owner mismatch is refused by the Engine. This is a minimal trusted local Host
boundary, not a general identity/authorization service. Ordinary Agent intents
cannot invoke Host operations without a configured credential. Interaction costs,
world physics and Cylinder vertical membership remain Host attestations.

## Request IDs and errors

Use a stable `request_id` for every mutation that might be retried: opening,
intent, advance-monster, end and all Host operations. Host operations require it;
legacy opening/intent/advance/end calls may omit it and have no retry guarantee.
IDs are scoped to one combat across mutation endpoints; opening has its own app
scope. Reusing an ID with a different validated payload or operation returns
409 `request_id_conflict`, without execution. The same ID/payload returns the
original status/body, including failures and closed-combat receipts. A corrected
or reconsidered command needs a new ID. Defaults/canonical cell normalization
are fingerprinted after validation.

| HTTP | Structured result |
| --- | --- |
| 200 | Committed Engine result; typed child/reaction failures remain in events |
| 422 | Wire validation failed; stable FastAPI `detail[]` type/loc/msg, no execution |
| 403 | `host_forbidden`, `not_executed` |
| 404 | `unknown_combat` or missing opening asset, `not_executed` |
| 409 | Typed Engine exception or leading actor failure, `rule_refused`; preserves reason and failure events (Invalid Target, No Action, Unsupported Activity, Unreviewed Spell, missing `spell_required`) |
| 409 | `request_id_conflict`, `not_executed` |
| 500 | `engine_unexpected_error`, `rolled_back`; state/events/RNG restored |
| 503 | `committed_response_failed`, `committed`; observer/narration/drain/serialization failed after authoritative commit; retry the same ID |

Rule refusal may commit a failure event while keeping the serial/gameplay state
unchanged and spending no resource. A countered cast can commit legal Action/reaction costs and
keeps its typed events; a later failed child is never relabeled as an unpaid
parent refusal. Receipts distinguish execution state from the rule event result.

Per-combat locks cover execution, a single event collector, response preparation,
receipt publication and reads. The whole mutation task is shielded from request
cancellation. A primitive recovery receipt is recorded before fallible response
publication. Start IDs recover a created combat even if narration fails. End
bookkeeping/sentinel commit before Host observers, and the same end ID works after
the live Bridge handle is removed. Different combats and app instances have
independent handles, loaders and RNG; opening still excludes Homebrew updates.

Receipts and locks are process-local and retained for the process lifetime; they
are not durable across restarts, shared by multiple workers, or an unbounded
production service recommendation. The embedding Host must bound process/session
lifetime. Network loss cannot be observed by the server after returning, but the
saved response remains retryable. No HTTP authentication beyond the controlled
Host routes is added to this trusted local sidecar.

## Remaining transport gaps

Direct `resolve_legendary_resistance`, `resolve_legendary_action` and arbitrary
engine state injection are not exposed. Combat opening still uses the existing
build-based party and monster-slug interface, not every Python `PartyMemberSpec`,
`EncounterMemberSpec` or arbitrary `GridScene` option. Out-of-combat ritual,
inventory/world simulation, persisted receipts and distributed execution remain
outside this batch. Spell admission remains 30 Executable / 48 Bounded /
26 Host narrative / 235 Deferred; B9 changes transport, not admission.
