# ShakeMap v4.4.9 verification package definition

`source-manifest.json` is the tracked, checksum-pinned recipe for the external
scientific package used with `tests/fixtures/shakemap_scenario/`. It contains
no scientific payload. The preparation helper downloads or imports the listed
sources and installs them under `runtime/shakemap/data/test/v4.4.9/` by
default.

The definition intentionally selects only the release test configuration,
California Vs30/topography/selection layer, the exact STREC moment database
used by the stable image, and the four Natural Earth layers requested by the
complete native module plan. Uniform VS30 is not used. STREC slab grids are
not included because the fixed event is active crust; this package therefore
does not test subduction selection.

The prepared directory receives its own `README.md` and
`package-manifest.json` with retrieval time, exact source URLs, installed
paths, transformations, checksums, sizes, licensing, coverage, compatibility,
validation commands, and limitations.
