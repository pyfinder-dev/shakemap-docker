# 2016 Norcia verification scenario

This directory contains a pinned native-input set for the 30 October 2016
M6.6 Norcia earthquake, USGS event `us1000731j`. The same exact input bytes are
intended for separate submissions with the caller-selected `global` and
`italy` configurations. A third submission can select a syntactically valid
configuration name that does not exist to verify the recorded failure path and
the absence of configuration fallback.

The inputs come from the USGS ShakeMap Atlas v4 product with source `atlas`,
product code `us1000731j`, version `1`, and update time `1594393907171`. Pinning
that complete product identity prevents a later ComCat update from silently
changing the fixture.

## Files

| File | Role | Origin |
|---|---|---|
| `event.xml` | Required ShakeMap origin input | Deterministically rendered from the pinned `rupture.json` metadata and the product's `ACTUAL` classification. |
| `stationlist.json` | Optional instrumental and macroseismic observations | Installed byte for byte from the pinned USGS product. |
| `rupture.json` | Optional native point-rupture input | Installed byte for byte from the pinned USGS product. |
| `scenario-manifest.json` | Machine-readable sources, transformations, identities, checksums, attribution, and limitations | Project-authored metadata. |

The official immutable download URLs are:

- `stationlist.json`: `https://earthquake.usgs.gov/product/shakemap/us1000731j/atlas/1594393907171/download/stationlist.json`
- `rupture.json`: `https://earthquake.usgs.gov/product/shakemap/us1000731j/atlas/1594393907171/download/rupture.json`
- Event page: `https://earthquake.usgs.gov/earthquakes/eventpage/us1000731j`

`event.xml` preserves the official embedded identity `us1000731j` and classifies
the earthquake as `ACTUAL`. It does not repeat the optional `productcode`
attribute. `rupture.json` retains the source product code, so the effective
v4.4.9 merge and output identity must be observed during native execution
rather than assumed here. The service still treats each caller-selected public
ID as the calculation identity and does not rewrite the embedded origin. The
source `rake` value remains unchanged in `rupture.json`; it is not converted
into an undocumented event XML attribute.

## Integrity

The tracked SHA-256 values are:

- `event.xml`: `65c39659d4b36c9d6a4ed290e0d5d3216d0c6024c47a9817f1f34d0e3a9c7c7f`
- `stationlist.json`: `6bd8b01168bae31fc4e87eada4802785d72bad774aa0cd6c3885d462cc7be160`
- `rupture.json`: `10aac3ad5ee7f614d669aef0b0e3b2d0303e379afa7896ee9f238b997ec986c3`

The station list contains 383 features: 302 instrumental observations and 81
macroseismic observations. Its embedded event ID and the rupture and event XML
origins all identify `us1000731j`.

## Attribution and rights caveat

USGS states that most USGS-authored information is in the public domain in the
United States and requests attribution. The downloaded files do not carry a
file-specific license, and `stationlist.json` attributes observations to 18
contributing network codes, several of which are not USGS networks. This
directory therefore does not label the station collection CC0 or assert that
foreign or contributing-network database rights have been cleared. Preserve
the source and network attribution when using or redistributing the fixture.

USGS policy references:

- `https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits`
- `https://www.usgs.gov/information-policies-and-instructions/acknowledging-or-crediting-usgs`

## Evidence boundary

The source product reports ShakeMap version `4.0.2+335.gcbad862.dirty`, while
this project targets ShakeMap `v4.4.9`. Parsing the XML and JSON, matching
hashes, and checking metadata consistency establish only static fixture
integrity. They do not establish native compatibility, container behavior,
deployment readiness, scientific correctness, regional suitability, or
product validity.

The pinned product's processed `info.json` reports `src_mech=NM`, while the
pinned `rupture.json` origin reports `mech=ALL`. `event.xml` deliberately uses
the rupture-origin value. The fixture does not treat processed-model metadata
as authority to alter the supplied origin; the difference must remain visible
when interpreting later native evidence.

The `italy` preset and its external data are deliberately not repaired or
bundled here. If that configuration is missing, cannot be materialized, or is
rejected by native execution, the calculation must expose that exact failure
without substituting `global`. Generated products, logs, and service records do
not belong in this source fixture directory.
