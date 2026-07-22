# ShakeMap verification scenario

This directory is the fixed client request used by the isolated native
ShakeMap integration check. Its public event identity and classification are
both `SCENARIO`. It is compatible with ShakeMap `v4.4.9` at commit
`8923f1ff6e82fc866d928a33d1e19e45f276db52`.

The source is the official release-matched USGS South Napa test event
`nc72282711`. Upstream ShakeMap declares its repository material public domain
in the United States and applies a CC0-1.0 dedication; the exact license and
source links are in `request-manifest.json`.

The immutable source files are:

- `event.xml`: `https://code.usgs.gov/ghsc/esi/shakemap/-/raw/8923f1ff6e82fc866d928a33d1e19e45f276db52/tests/data/eventdata/nc72282711/current/data/event.xml`
- `event_dat.xml` (upstream `stationlist.xml`): `https://code.usgs.gov/ghsc/esi/shakemap/-/raw/8923f1ff6e82fc866d928a33d1e19e45f276db52/tests/data/eventdata/nc72282711/current/data/stationlist.xml`
- License: `https://code.usgs.gov/ghsc/esi/shakemap/-/blob/8923f1ff6e82fc866d928a33d1e19e45f276db52/LICENSE.md`

## Files and transformations

| File | Purpose | Transformation |
|---|---|---|
| `event.xml` | Origin for the 2014 South Napa M6.0 event | Changed `id` to `SCENARIO` and added `event_type="SCENARIO"`; origin values are unchanged. |
| `event_dat.xml` | 334 instrumental stations, 999 components, and PGA, PGV, and 0.3/1.0/3.0 s PSA values from BG, BK, CE, NC, NP, and WR | Renamed upstream `stationlist.xml` because ShakeMap v4.4.9 assemble recognizes `*_dat.xml`; changed only the embedded earthquake ID to `SCENARIO`. |
| `request-manifest.json` | Machine-readable identity, provenance, transformations, sizes, checksums, licensing, compatibility, and limitations | Project-authored metadata. |

No finite rupture is supplied. This scenario deliberately exercises the
release's point-source path. No event-specific configuration is supplied;
scientific configuration and data come from the separately prepared external
verification package.

## Integrity

The installed SHA-256 values are:

- `event.xml`: `2877da340be53de959821883357759ac403d950248cc94270e282ddfca753931`
- `event_dat.xml`: `672979a942a676fecee2a7225c0acecfcde5f5abd97bec1f252673a617c98054`

Run `python scripts/prepare-shakemap-verification-data.py validate` to validate
the external package, then use its `run-native` command to test actual
ShakeMap acceptance. Host-side XML and manifest checks establish structure and
provenance only; they do not establish native acceptance.

## Scope

A successful isolated run shows that this request and its versioned external
package execute the explicit module order `select assemble model contour
mapping stations gridxml` in the exact compatible image. It does not prove
queue or REST behavior, authoritative service `SUCCESS`, final product
validation, global or named-region coverage, prediction-only support, or
production readiness. Station observations are optional in upstream ShakeMap;
their presence here is representative, not a public input requirement.
