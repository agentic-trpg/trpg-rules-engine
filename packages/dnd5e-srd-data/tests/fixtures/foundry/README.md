# Pinned lifecycle translator inputs

These ten unmodified YAML files are copied from Git blobs in
[foundryvtt/dnd5e at 965ad2d0cf5d063dac675ba078b5bd3c3c0dd449](https://github.com/foundryvtt/dnd5e/tree/965ad2d0cf5d063dac675ba078b5bd3c3c0dd449).
`snapshot.json` records each original URL and SHA-256. The commit must match the
existing `raw_sources/PINS.json` Foundry pin. Tests validate both the source identity
and byte hashes before reproducing and comparing the complete canonical document.

They make the existing lifecycle translator regressions runnable from a clean
checkout without downloading the maintainer-only upstream history and assets.
They are test inputs, never runtime data. Keep the `packs/_source` hierarchy because
the translator derives provenance and class-scoped names from that hierarchy.
Updating the upstream pin requires an explicit review and refresh of this snapshot.

Upstream software attribution and license are preserved in `UPSTREAM-LICENSE.txt`
(Copyright 2021 Andrew Clayton, MIT). The selected SRD 5.2 rules content is credited
to Wizards of the Coast LLC under CC-BY-4.0, as specified by the source documents;
see the package's `LICENSE` and the repository's SRD attribution. No content was changed.
