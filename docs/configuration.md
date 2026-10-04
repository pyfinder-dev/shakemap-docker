# Configuration and external data

The caller chooses a named configuration. Each service submission runs exactly
that selection; omission means `global`. The service never infers a country from
coordinates, substitutes another configuration, or retries the native calculation
with different scientific settings.

PyFinder owns a separate, bounded recovery policy: submit the requested region
first, retain confirmed configuration-failure evidence, then make at most one new
submission explicitly selecting `global`. It keeps the same public calculation
ID and overwrite setting, receives a new sequence, and serializes the attempts.
Uncertain acceptance, read errors, or an unrelated native failure do not authorize
this recovery. Both outcomes and the reason must remain visible in its final
alert and logs. This does not change the service's exact-selection behavior.

## Inspect the running deployment

Activate the project Python environment with `shake-in-docker` installed before
running these host commands:

```bash
# Read readiness and the operational configuration.
shake-in-docker --url http://127.0.0.1:9010 health
shake-in-docker --url http://127.0.0.1:9010 config

# Discover configuration names without starting a calculation.
shake-in-docker --url http://127.0.0.1:9010 configurations
```

The corresponding REST paths are `/healthz`, `/config`, and `/configurations`.
A configuration response contains `default: "global"` and a `configurations`
list. `global` is followed by the immediate real directory names under the
regional data root; symlink directories and regular files are excluded.
Do not put a generic asset directory directly under that root: it would also be
advertised as a configuration.

A listed name means only that its directory exists. It does not prove readable
files, installed scientific modules, usable datasets, geographic suitability,
or native success. Deployment `ready: true` records the separately verified
global path; it does not certify every listed regional configuration.

## Read-only profile checks

Use the running service to inspect the selected profile's actual references:

```bash
make check CONFIGURATION=italy
make check CONFIGURATION=switzerland
make check CONFIGURATION=global
# Equivalent host CLI; SERVICE_URL on make selects another service URL.
shake-in-docker --url http://127.0.0.1:9010 check --configuration italy
```

The separate `GET /configurations/{name}/check` endpoint returns the same JSON.
It leaves the names-only listing unchanged. An older service without this endpoint
must be updated before the command can report installed-service diagnostics; a
missing endpoint is not a successful check.

Each finding includes `configuration`, `config_file`, `section`, `key`,
`reference`, `resolved_path`, `reason`, `corrective_action`, and `evidence`.
Paths are visible to the service container. For `<INSTALL_DIR>/data/layers`,
the resolved path is the installed layer source copied by native `sm_profile`,
not a directory created for a new calculation. Missing files, unreadable files,
invalid native syntax, unresolved path macros and missing configured modules
remain distinct findings. No unrelated configuration values or secrets are dumped.

| Report status | Meaning | CLI exit |
|---|---|---|
| `NO_KNOWN_BLOCKERS` | Supported static checks completed without a known blocker. | 0 |
| `BLOCKED` | At least one required reference is missing or broken. Other unknowns remain visible. | 1 |
| `INCOMPLETE` | A required check could not be resolved statically. | 2 |

Per-finding states are `OK`, `MISSING`, `BROKEN` and `UNKNOWN`. The report always
says `scope: static` and `native_execution: not_run`. Syntax parsing is not full
native schema validation. Module lookup does not import models or validate their
classes, dependencies or constructors. Model branches are inspected without
choosing event geography; weights, coverage and scientific suitability remain
the profile owner's responsibility. No uniform site defaults are substituted.

The checker neither performs repairs nor changes submission behavior. PyFinder
still submits the selected region first and makes one explicit global submission
only after a qualifying configuration failure. Some native failures cannot be
predicted or classified from static checks. Inspect `global` separately; neither
a regional warning nor a global static pass promises successful recovery.
Service `capabilities.native_configuration_diagnostics` describes the installed
diagnostic implementation, not eligibility of every possible failure.

Use `make data DATA_ACTION=inspect` for a cheap global dataset inventory, or
`DATA_ACTION=validate` for full pinned content validation. `make verify` runs
native work and can revoke readiness or stop the service on failure; it is a
separate, mutating verification operation.

## Runtime paths and the five native files

`RUNTIME_ROOT` selects the host runtime parent; the service helpers default to
`./runtime`. It mounts as `/home/sysop/runtime`. Its scientific data subtrees are
read-only inside the service container. Configuration fields must reference paths
visible inside that container, not host paths. A combined deployment supplies its
common runtime root through the deployment settings.

```text
<RUNTIME_ROOT>/shakemap/data/
├── global/
│   ├── vs30/global_vs30.grd
│   └── topo/topo_30sec.grd
├── regional/
│   └── <name>/
│       ├── gmpe_sets.conf
│       ├── model.conf
│       ├── modules.conf
│       ├── products.conf
│       └── select.conf
└── test/<resolved-version>/
```

Regional grids are separate prerequisites. For example, the Italy seed names
`global_italy_vs30_clobber.grd`; the global-data helper does not install it. Put
shared regional assets in a location explicitly referenced by their native
configuration, without adding a directory that discovery would mistake for a
profile.

| File | Native responsibility |
|---|---|
| `modules.conf` | Maps scientific names to installed GMPE, IPE, GMICE and correlation classes. |
| `gmpe_sets.conf` | Defines GMPE sets, constituent models and weights. |
| `model.conf` | Supplies model settings and data references such as `data.vs30file`. |
| `select.conf` | Defines native tectonic, depth and polygon selection, including `layers.layer_dir`. |
| `products.conf` | Defines product settings, including mapping topography. |

All five files must be readable. For each calculation the service creates a
private profile using native `sm_profile`, then copies these five selected files
in full, checking byte identity. It does not merge individual regional keys with
global defaults or rewrite regional paths. Other base native files remain those
generated by `sm_profile`. Native ShakeMap still applies its own selection and
configuration rules, including `model_select.conf` produced by `select`.

`<INSTALL_DIR>` means that calculation's private native installation directory.
It does not mean the mounted regional source directory. Currently the service
copies mapping support and configures STREC, but does not copy regional `layers/`
or `GenericAmpFactors/` into the private installation. A folder placed beside
`model.conf` therefore has no effect unless the native configuration or a
supported materialization step actually connects it.

A custom Python module must be installed in the image at the path declared by
`modules.conf`; putting its source beside the five files is insufficient.
Polygons must be the intended original WKT files and must be accessible through
`layers.layer_dir`. Native generic amplification expects its data in the native
profile's `data/GenericAmpFactors` area; reproducible Swiss materialization there
remains implementation work. Do not invent replacement grids, polygons or model
classes to make a run finish.

## Scientific ownership and provisioning

The scientific owner chooses model branches, weights, datasets, geographic
coverage and site treatment. Operators install those agreed assets and preserve
their identities. ShakeMap validates and uses its native configuration during
execution. The service owns safe materialization, one complete native attempt,
and honest outcome reporting; it does not implement an independent scientific
compatibility validator or configuration-loadability preflight.

Image presets are immutable seed sources. Finalization copies only missing
regional profile directories. Existing mounted copies are operator-owned and
authoritative; rebuilding or rerunning finalization does not update their files.
Manual placement remains supported, but changing active profiles must be
coordinated with service work so a calculation cannot capture a partial update.
Preserve the original configuration and asset identities before deliberate
changes. Never replace an operator dataset merely because it differs from an
expected file.

From the deployment checkout, these commands delegate to the service's existing
data helper:

```bash
# Cheap, read-only presence and access checks for the supported global grids.
make data COMPONENT=shakemap DATA_ACTION=inspect

# Read-only full checksum validation of those pinned global grids.
make data COMPONENT=shakemap DATA_ACTION=validate
```

For a standalone service runtime, run this from the service checkout:

```bash
./scripts/manage-shakemap-data.sh inspect \
  --runtime ./runtime
```

`provision` explicitly installs missing global VS30/topography assets and reuses
valid existing ones. It leaves invalid existing assets unchanged. The service
helper also accepts `--vs30-source`, `--topo-source`, and `--no-download` for
manual imports, and `stage` validates replacements without publishing them.
These commands currently do **not** provision regional grids, custom modules,
polygons or amplification data. Container startup and API reads download nothing.

Service helpers own build/data/finalize/start/verification logic. Deployment
wrappers choose the common runtime and settings and delegate to those helpers.
Do not duplicate that logic in deployment scripts. Finalization and live service
verification are mutating operations with a fixed global calculation; failures
may stop the service or revoke readiness. They are not regional inspection
commands. See [scripts](../scripts/README.md) and
[deployment guidance](../../pyfinder-deploy/README.md).

## Global, Italy and Switzerland prerequisites

The shipped seeds describe scientific choices; they are not complete regional
installations. Inspect the selected runtime and image rather than assuming data
is present or absent from a seed directory listing.

| Selection | Required configuration and assets | Installation limitation |
|---|---|---|
| `global` | Pinned global VS30 and topography, plus image support and a finalized native profile. | Image installation and file presence alone do not prove deployment readiness. Run finalization against the intended runtime. |
| `italy` | The intended `global_italy_vs30_clobber.grd`, topography, complete selection polygons, and the OFM22 custom GMICE implementation. | The seed contains legacy absolute data paths and `<INSTALL_DIR>/data/layers`. The global helper does not provision its grid, polygons or custom module; those paths need explicit wiring and the module needs current-release compatibility checks. |
| `switzerland` | EF2013 model branches, FM11_CH, Swiss selection polygons, and the intended generic amplification data and site treatment. | The seed references `null_vs30.grd` and enables generic amplification. The image does not supply FM11_CH and the service does not materialize regional amplification data. Site treatment remains unresolved; do not substitute a grid or constant to make the run finish. |

The upstream INGV sources provide
[OFM22](https://github.com/INGV/shakemap/blob/f3632031a46e487f72b96dcb0df3657f4acdc2ea/ext/ofm22.py),
[FM11_CH](https://github.com/INGV/shakemap/blob/f3632031a46e487f72b96dcb0df3657f4acdc2ea/ext/fm11_ch.py),
and Italian [Sicily](https://github.com/INGV/shakemap/blob/f3632031a46e487f72b96dcb0df3657f4acdc2ea/data/shakemap_profiles/italy/install/data/layers/sicily_area.wkt)
and [volcanic](https://github.com/INGV/shakemap/blob/f3632031a46e487f72b96dcb0df3657f4acdc2ea/data/shakemap_profiles/italy/install/data/layers/volcanic_italy.wkt)
polygons. They identify implementation sources, not a tested installation recipe
for this release. Preserve scientific coefficients, units and asset identities
while establishing compatibility. Regional provisioning must record source and
integrity evidence rather than silently accepting an arbitrary replacement.

Swiss site treatment and amplification coverage, units and supported intensity
measures require a bounded compatibility experiment. No uniform VS30 value is
prescribed here, and uniform VS30 must not be used as proof of normal readiness.
Successful global execution does not resolve these regional requirements.

## Submission, evidence and corrective actions

The caller's `PYFINDER_SHAKEMAP_CONFIGURATION` setting selects the initial profile;
it defaults to `global`. It does not enable the listener. The deployment example
keeps `PYFINDER_SHAKEMAP_ENABLED=false` as a separate activation decision.
The caller container uses its configured reachable service URL and canonical
input path; the host CLI uses the host URL. See the
[PyFinder adapter](../../pyfinder/docs/shakemap-adapter.md).

For reviewed inputs and an explicitly chosen configuration, the CLI form is
`shake-in-docker --url URL submit CALCULATION_ID --configuration NAME --file FILE`.
Repeat `--file` for each native input. No ready-to-run Italy or Swiss scientific
recipe is supplied here. A reused ID means recalculation: default
`--overwrite true` discards both preceding service and product trees;
`--overwrite false` archives both. Keep required earlier evidence before replacement.

Retain the submission's `internal_sequence`. Poll `status CALCULATION_ID` for
that exact attempt, not simply the latest record or an existing product path.
The complete native plan is `select assemble model contour mapping stations
gridxml`. Require `SUCCESS`, `job_completed=true`, `products_ready=true`, and
validated core products, manifest, provenance and logs. The status response
exposes evidence paths; the CLI does not have a separate `logs` command or a
sequence-selection option. `products CALCULATION_ID` describes current products.

| Failure evidence | Corrective action |
|---|---|
| Required regional file missing/unreadable during `regional_sources` | Restore the intended five-file profile or correct access at the reported source path. |
| `native_configuration_failed` with `configuration_error` | Inspect its typed native origin and reference. Repair the selected module or invalid native fields; preserve the failed attempt. This code is produced only by the supported execution-time diagnostic launcher. |
| Generic `native_exit`, signal or product-validation failure | Investigate the native logs and exact sequence. This alone is not permission for caller global recovery. |
| Old VS30/topography path | Wire the verified intended asset using its container-visible path; do not substitute another grid just because it exists. |
| Pinned global checksum mismatch | Preserve the existing asset, stage/review the intended replacement, and take an explicit replacement action. |
| Permission error | Follow the helper's targeted recovery command; avoid broad recursive ownership changes. |
| Uncertain submission or status read failure | Retain the attempt identity and reconcile observations; do not issue a speculative new submission. |

A regional failure does not invalidate independently verified global readiness.
Conversely, a successful global recovery does not make the requested regional
configuration ready or erase its failure.
