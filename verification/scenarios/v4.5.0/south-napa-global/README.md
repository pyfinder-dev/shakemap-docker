# South Napa global verification scenario

This directory is the `south-napa-global` scenario used by
`scripts/finalize-shakemap.sh` and `scripts/verify-shakemap-deployment.sh`.
Its intended configuration is `global`. Its embedded native identity and
classification are both `SCENARIO`; the helpers use the public calculation ID
`shakemap-docker-verification`. The scenario is compatible
with ShakeMap `v4.5.0` at commit
`2b270c626efff8e54bfbe2d7dac5da9444d08c51`.

The source is the official release-matched USGS South Napa test event
`nc72282711`. Upstream ShakeMap declares its repository material public domain
in the United States and applies a CC0-1.0 dedication; the exact license and
source links are in `scenario-manifest.json`.

The immutable source files are:

- `event.xml`: `https://code.usgs.gov/ghsc/esi/shakemap/-/raw/2b270c626efff8e54bfbe2d7dac5da9444d08c51/tests/data/eventdata/nc72282711/current/data/event.xml`
- `event_dat.xml` (upstream `stationlist.xml`): `https://code.usgs.gov/ghsc/esi/shakemap/-/raw/2b270c626efff8e54bfbe2d7dac5da9444d08c51/tests/data/eventdata/nc72282711/current/data/stationlist.xml`
- License: `https://code.usgs.gov/ghsc/esi/shakemap/-/blob/2b270c626efff8e54bfbe2d7dac5da9444d08c51/LICENSE.md`

## Files and transformations

| File | Purpose | Transformation |
|---|---|---|
| `event.xml` | Origin for the 2014 South Napa M6.0 event | Changed `id` to `SCENARIO` and added `event_type="SCENARIO"`; origin values are unchanged. |
| `event_dat.xml` | 334 instrumental stations, 999 components, and PGA, PGV, and 0.3/1.0/3.0 s PSA values from BG, BK, CE, NC, NP, and WR | Renamed upstream `stationlist.xml` because ShakeMap v4.5.0 assemble recognizes `*_dat.xml`; changed only the embedded earthquake ID to `SCENARIO`. |
| `scenario-manifest.json` | Machine-readable identity, provenance, transformations, sizes, checksums, licensing, compatibility, and limitations | Project-authored metadata. |

No finite rupture is supplied. This scenario deliberately exercises the
release's point-source path. No event-specific configuration is supplied;
the helpers select `global` and use mounted global VS30 and topography
plus image support. It does not use the separate California verification
package.

## Integrity

The installed SHA-256 values are:

- `event.xml`: `2877da340be53de959821883357759ac403d950248cc94270e282ddfca753931`
- `event_dat.xml`: `672979a942a676fecee2a7225c0acecfcde5f5abd97bec1f252673a617c98054`

The California verification package can be validated separately with
`python scripts/prepare-shakemap-verification-data.py validate`; it is not an
input to the helpers' native calculation. Host-side XML and manifest checks
establish structure and provenance only. They do not establish native acceptance.

## Scope

Both helpers submit one fixed global calculation using the native module order
`select assemble model contour mapping stations gridxml`. They require service
`SUCCESS`, completed work and ready products after real native execution and
the service's required-product, manifest, provenance and log gates.

Finalization runs that calculation in an isolated, network-disabled service,
then checks the retained result through REST and the host CLI before publishing
readiness. The deployment verifier requires an already-running deployment and
compares REST/CLI health, configuration, configuration names, event collection,
queue, and the new calculation's status and products. These checks do not
exercise native different-ID concurrency or same-ID serialization.

Successful verification is bounded to the recorded image, inputs, selected
global configuration and mounted data. It does not establish scientific
correctness, equivalence to another release, or general global or regional
coverage. Station observations are optional in upstream ShakeMap; their
presence here is representative, not a public input requirement.
