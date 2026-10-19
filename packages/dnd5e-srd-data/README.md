# dnd5e-srd-data

Canonical D&D 5e SRD 5.2 asset dataset shipped as a Python package. Consumed by
[dnd5e-engine](../dnd5e-engine/) for combat resolution.

## Usage

```python
from dnd5e_srd_data import BundledAssetLoader

loader = BundledAssetLoader()
aboleth = loader.get_monster("aboleth")
print(aboleth.hp)  # 135
```

`BundledAssetLoader` is a live, lazy source: ordinary getters read and parse the
current JSON, while `MemoryAssetLoader` retains mutable input models. Neither is
trusted as a fixed corpus by the engine.

For a reusable detached data-only snapshot, use
`dnd5e_srd_data.frozen.FrozenAssetLoader(source)`. It enumerates every effective
category, checks duplicate/missing/mismatched identities and typed item accessors,
and isolates nested models, dictionaries and lists. Its getters return deep
copies of private parsed models; they perform no JSON reads or parsing. Item,
Weapon and Armor accessors use the same stored item, and UUID lookup uses an index
built from the captured spells. Bundled materialization temporarily caches parsed
models so typed item validation reads/parses each JSON only once. The ordinary live
loader's behavior is unchanged.

For engine execution, prefer `dnd5e_engine.evaluation_ruleset.freeze_ruleset(source)`:
it adds the verified three-field `RulesetBinding` and constant-work verification.
Keep the returned loader and pass that same instance to execution; hashing a
snapshot and then executing against its mutable source does not establish a pin.
The source must be stable during materialization; callers synchronize concurrent
edits. A failed construction returns no dataset. Updates require a new snapshot;
dropping references releases custom snapshots without an unbounded global cache.

## License

Dataset content is CC-BY-4.0. See `NOTICE` for attribution chain.
Schema and tooling code is MIT-aligned and lives in the same package
for distribution convenience.

## Regenerating canonical data

```bash
make refresh-upstream    # pulls vendored Foundry + open5e snapshots
make regen               # translates + audits + writes canonical/*.json
```

The pinned upstream sources are recorded in `raw_sources/PINS.json` and
`raw_sources/README.md`; the raw snapshots are regenerated locally, not distributed.
