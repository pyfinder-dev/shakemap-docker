# Configuration and external data

A profile tells USGS ShakeMap which scientific models, settings and datasets to
use. Regional profiles are directories containing the five configuration files
listed below. The caller names a profile when it submits a calculation; omission
means `global`. The service runs that choice inside Docker. It does not select
another profile from earthquake coordinates or retry with different settings.

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

## Thread tuning

ShakeMap 4.5 provides `sm_tune`, a standalone maintenance command that benchmarks
BLAS/OpenMP scaling and writes `blas_threads` and `openmp_threads` under `[system]`
in a native `model.conf`. It supports `--dry-run` and an explicit `--model-conf`
target. Missing thread keys receive native defaults; tuning is not a prerequisite
for parsing an otherwise valid older configuration and does not repair broken
scientific paths or module references.

The service does not run tuning automatically. Each calculation creates a new
private profile, so tuning an unrelated default profile does not affect future
calculations. Regional profiles retain their operator-owned `model.conf`.
Persistence and precedence of deployment-wide tuning settings remain design
questions; no tuning REST endpoint or host CLI operation is currently provided.
Do not benchmark during image builds or every calculation. Any deliberate tuning
should use the intended deployment hardware and container resource limits, and
be evaluated with the configured number of concurrent calculations. A faster
native implementation does not guarantee a particular workload's speedup.

## Scientific ownership and provisioning

Use the model branches, weights, datasets, coverage and site treatment chosen for
your workflow. If someone supplies the profile, obtain its referenced files and
compatible model implementations from them. Record file versions and sources so
the calculation can be reproduced. The service prepares a private copy of the
selected files for each calculation, then USGS ShakeMap loads and uses them.
Static checks can find missing prerequisites; a real calculation is still needed
to test execution, and its scientific output must be reviewed for your use case.

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

`provision` downloads missing global VS30/topography files and reuses valid
existing ones. It leaves invalid existing files unchanged. VS30 describes ground
conditions; topography is used to draw maps. The service
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

## Regional profile file checklist

Global installation and regional scientific capability are separate. The two
global grids below are installation inputs; missing optional regional files do
not prevent global finalization or service startup. They do prevent an affected
regional calculation from completing. The service reports that failure and keeps
running; it never silently substitutes global.

Use this checklist to collect files from the profile's scientific owner. The
tables describe the shipped seeds. Existing mounted profiles are authoritative
and may already use different reviewed paths or model definitions. Inspect them
with the running-service check before deciding that an asset is missing.

### What installation already supplies

Here `<RUNTIME_ROOT>` is the host runtime parent; in a combined deployment it is
`pyfinder-deploy/runtime`. The container prefix is always `/home/sysop/runtime`.

| Item | Host location | Container location | Who supplies it |
|---|---|---|---|
| Global VS30 | `<RUNTIME_ROOT>/shakemap/data/global/vs30/global_vs30.grd` | `/home/sysop/runtime/shakemap/data/global/vs30/global_vs30.grd` | Data helper downloads or imports the pinned USGS grid and verifies its checksum. |
| Mapping topography | `<RUNTIME_ROOT>/shakemap/data/global/topo/topo_30sec.grd` | `/home/sysop/runtime/shakemap/data/global/topo/topo_30sec.grd` | Data helper downloads or imports the pinned USGS grid and verifies its checksum. Regional profiles can reference this same approved file. |
| Five native files for each region | `<RUNTIME_ROOT>/shakemap/data/regional/<name>/{gmpe_sets,model,modules,products,select}.conf` | `/home/sysop/runtime/shakemap/data/regional/<name>/` | Finalization copies a missing seed directory. Existing copies remain operator-owned and are not updated. |
| Release-matched native verification inputs | `<RUNTIME_ROOT>/shakemap/data/test/<resolved-version>/` | `/home/sysop/runtime/shakemap/data/test/<resolved-version>/` | Finalization's verification-data helper prepares the fixed package. This is test input, not regional science data. |
| Mapping, STREC and Slab2 support | No separate host input required | `/opt/shakemap-support/` and installed native package data | Image build supplies the pinned support. It does not supply every regional grid, polygon or custom model. |

The [global asset manifest](../shakemap_service/data/global-assets.json) records
the two supported downloads and content identities. `provision` does not create
the regional inputs in the next table.

### Files named by each seed

Every row also needs its five native files and mapping topography. Polygon names
below are native selection inputs: each subsection in `select.conf [layers]`
expects a file with that name plus `.wkt` in `layers.layer_dir`.

The **common set** means `california.wkt`, `taiwan.wkt`, `japan.wkt`, `china.wkt`,
`new_zealand.wkt`, `chile.wkt` and `europe_share.wkt`. Native `sm_profile` can copy
release-bundled layers from the image; it does not collect extra regional polygons
from beside the five files. Check which originals the installed release supplies.

| Profile | `model.conf [data] vs30file`: intended grid basename | Required native WKT files | Additional model/site requirements |
|---|---|---|---|
| `albania` | `global_italy_vs30_clobber.grd` | `albania.wkt`, `scr.wkt`, `europe_share.wkt` | Release-compatible module mappings for the selected GMPE/GMICE/IPE/correlation branches. |
| `belgium` | `global_italy_vs30_clobber.grd` | `mob.wkt`, `lgz.wkt`, `rvrs.wkt`, `belgium.wkt`, `europe_share.wkt` | Review selected `Fcea10` / `FaccioliEtAl2010`; its seed module path contains `openquake.hazrdlib`, which is not the intended installed package name. |
| `croatia` | `global_italy_vs30_clobber.grd` | Common set | Release-compatible module mappings; retain this profile's scientific branches and weights. |
| `france` | `global_italy_vs30_clobber.grd` | Common set | Release-compatible module mappings; its GMICE choices differ from Croatia/Slovenia. |
| `greece` | `Hellas-geology_18-30_34-43_30s_NoNan.grd` | `greece.wkt`, `europe_share.wkt` | Compatible selected OpenQuake models, including the seed's `Bea20` / `BooreEtAl2020`, and native module mappings. |
| `italy` | `global_italy_vs30_clobber.grd` | Common set plus `volcanic_italy.wkt`, `sicily_area.wkt`, `italy.wkt` | Compatible `OFM22` GMICE implementation for the branches that select it. |
| `romania` | `global_italy_vs30_clobber.grd` | `carpathians.wkt`, `europe_share.wkt` | Release-compatible module mappings for the selected branches, including the Vrancea sets. |
| `slovenia` | `global_italy_vs30_clobber.grd` | Common set | Release-compatible module mappings; retain this profile's scientific branches and weights. |
| `switzerland` | `null_vs30.grd` | `swiss_foreland.wkt`, `swiss_alpine.wkt` | EF2013 Alpine/Foreland GMPE classes, compatible `FM11_CH`, and the intended generic amplification/site treatment described below. |

Seven seeds name the same Italy-clobbered VS30 file. Its name is
not permission to replace it with `global_vs30.grd`; the profile owner must confirm
the intended grid, coverage and site treatment for each selection. For a newly
added runtime profile, use its own five files and check report; this seed inventory
does not define its requirements.

### Where to put regional inputs and connect them

The seeds' `/home/shake/shakemap_data/...` references are legacy container paths,
not this deployment's mounts. A grid can already exist on the host while that
reference still fails. Do not redownload it merely because the old path is missing.
Use an approved file in the mounted data tree and deliberately update the named
native key to its container-visible path; the service does not rewrite regional
paths for you.

| Input | Supported host placement | Native reference to review |
|---|---|---|
| Shared regional VS30 grid | For example `<RUNTIME_ROOT>/shakemap/data/regional/global_italy_vs30_clobber.grd` | Set `model.conf [data] vs30file` to `/home/sysop/runtime/shakemap/data/regional/global_italy_vs30_clobber.grd` if this is the approved grid. Greece/Swiss grids need their own approved files and references. |
| Existing global topography | `<RUNTIME_ROOT>/shakemap/data/global/topo/topo_30sec.grd` | `products.conf [products][mapping][layers] topography` uses `/home/sysop/runtime/shakemap/data/global/topo/topo_30sec.grd` when that is the intended mapping dataset. |
| Original native polygons | `<RUNTIME_ROOT>/shakemap/data/regional/<name>/layers/<polygon>.wkt` | `select.conf [layers] layer_dir` can explicitly name `/home/sysop/runtime/shakemap/data/regional/<name>/layers`. Include every WKT named by that profile, including any common-set polygons it uses. |
| Custom model implementation | Install the compatible package/module in the image | `modules.conf` declares the exact class and importable module, for example `OFM22, shakelib.gmice.ofm22` in the Italy seed. A `.py` beside `model.conf` is not installed or imported automatically. |

Keep shared grids as regular files or under an existing profile directory. A new
generic directory directly below `regional/` would be advertised as another
profile. Preserve existing files and coordinate any deliberate profile updates
with accepted work; do not edit a profile while a calculation may copy it.

`<INSTALL_DIR>/data/layers` instead resolves within each calculation's private
native installation. Placing WKT files beside a seed does not populate that
directory. Use an explicit mounted `layer_dir` when appropriate, or establish a
supported materialization change before relying on private-install assets.
Likewise, Swiss `model.conf [modeling] apply_generic_amp_factors=true` expects
the intended native amplification files under that private installation's
`data/GenericAmpFactors`. The current service does not copy them there;
placing files in a mounted directory alone does not enable that feature. This
limitation applies to profiles that enable generic amplification, including this
Swiss seed; it is not a requirement for every operator using a Swiss region.
The seeds do not specify a complete amplification-file inventory or a validated
replacement for `null_vs30.grd`: obtain the scientific owner's site-treatment,
coverage, units and intensity-measure requirements before implementing that step.
Do not manufacture a null/uniform grid or disable amplification just to pass.
The Swiss seed also leaves `model.conf [modeling] gmice` unspecified; a static
check reports that default/selection behavior as unknown even though
`select.conf` names `FM11_CH`. Review the intended precedence with the profile
owner rather than guessing a value to remove the finding.

### Model compatibility and provenance

The five native files, original polygons and regional datasets belong to the
profile's scientific owner. Collect the intended release/source and integrity
evidence; regional seeds do not contain a complete regional download/checksum
manifest. The global-data helper cannot supply missing regional science assets.

Older `shakelib.*` module references must match the installed native release.
The current 4.5 image exposes standard modules through `esi_shakelib`, for example
`esi_shakelib.gmice.ak07`, `esi_shakelib.gmice.wgrw12`,
`esi_shakelib.correlation.loth_baker_2013` and `esi_shakelib.virtualipe`.
Review the declared class/module pairs with the profile owner and rerun the
installed-service check; namespace substitution alone does not establish class
compatibility, scientific equivalence or a usable custom model. Unused custom
aliases in a file do not establish that an event actually selects them.

The upstream INGV sources provide
[OFM22](https://github.com/INGV/shakemap/blob/f3632031a46e487f72b96dcb0df3657f4acdc2ea/ext/ofm22.py),
[FM11_CH](https://github.com/INGV/shakemap/blob/f3632031a46e487f72b96dcb0df3657f4acdc2ea/ext/fm11_ch.py),
and Italian [Sicily](https://github.com/INGV/shakemap/blob/f3632031a46e487f72b96dcb0df3657f4acdc2ea/data/shakemap_profiles/italy/install/data/layers/sicily_area.wkt)
and [volcanic](https://github.com/INGV/shakemap/blob/f3632031a46e487f72b96dcb0df3657f4acdc2ea/data/shakemap_profiles/italy/install/data/layers/volcanic_italy.wkt)
polygons. They identify implementation sources, not a tested installation recipe
for this release. Preserve scientific coefficients, units and asset identities
while establishing compatibility. Regional provisioning must record source and
integrity evidence rather than silently accepting an arbitrary replacement.

Swiss site treatment and amplification still require a bounded compatibility
experiment. Successful global execution does not resolve regional requirements.

### Check the selected profile and follow its findings

From the **service checkout**, with the host client installed and service running:

```bash
# Replace italy with the exact profile you intend to use.
make check CONFIGURATION=italy
```

From the **deployment checkout**, use the host CLI for one exact profile, or
`make check` for the accumulated caller-routing assessment:

```bash
shake-in-docker --url http://127.0.0.1:9010 check --configuration italy
make check
```

Use your configured host URL/port instead of the example when different. A
`MISSING` path means check its exact namespace, mount and existing file before
collecting another copy. A missing module means review the class/module mapping
and installed package. `UNKNOWN`, including unmaterialized amplification, needs
the stated compatibility or implementation decision. Rerun static checks after
an explicit correction, then verify the intended regional native calculation
separately. A static pass does not qualify its coverage or scientific output.

## Submission, evidence and corrective actions

PyFinder's installed `regional-configurations` WKT catalog selects the initial
profile from the selected solution's location. Its coverage WKT resources are
separate from the native selection polygons listed above. Uncovered or unusable
optional coverage uses global with diagnostics. The obsolete
`PYFINDER_SHAKEMAP_CONFIGURATION` setting warns and is ignored; it does not force
a service profile. The deployment example keeps `PYFINDER_SHAKEMAP_ENABLED=false`
as a separate activation decision.
Caller coverage resources come from the PyFinder checkout's
`pyfinder/extern/shakemap_regional_wkt/` and are packaged in its image; changing
them requires caller rebuild/redeployment, not placement in the service's
`regional/<name>/layers/`. See [coverage authoring](../../pyfinder/docs/shakemap-region-coverage.md).
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
