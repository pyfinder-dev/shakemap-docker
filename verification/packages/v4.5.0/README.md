# ShakeMap v4.5.0 verification package definition

`source-manifest.json` is the tracked, checksum-pinned recipe for the external
scientific package used with
`verification/scenarios/v4.5.0/south-napa-global/`. It contains no scientific
payload. The preparation helper downloads or imports the listed sources and
installs them under `runtime/shakemap/data/test/v4.5.0/` by default.

The definition intentionally selects only the release test configuration,
California Vs30/topography/selection layer. The installed STREC moment database
and Natural Earth layers are supplied separately by the image for the
complete native module plan. Uniform VS30 is not used. STREC slab grids are
not included because the fixed event is active crust; this package therefore
does not test subduction selection.

The prepared directory receives its own `README.md` and
`package-manifest.json` with retrieval time, exact source URLs, installed
paths, transformations, checksums, sizes, licensing, coverage, compatibility,
the validation command, and limitations.
