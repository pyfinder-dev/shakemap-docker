# ShakeMap Docker service

This repository builds a ShakeMap v4.4.9 image and provides a calculation
service through REST and the `shake-in-docker` host client. The service accepts
caller-prepared native inputs, queues calculations, materializes caller-selected
configurations, and records products, provenance, manifests, and logs.

## Current verification status

On 2026-09-22, the writable-access and diagnostic corrections passed 445 host
tests. The most recent image execution evidence remains the 21 Linux ARM64
image checks from 2026-08-24. Global VS30, topography, and Slab2 integrity checks
also passed then.
That deployment attempt stopped at host ownership preparation before the
deployed service or either global example ran successfully.

The writable-access correction checks effective container access and provides
a bounded manual repair command when host privileges are insufficient. Its
diagnostics distinguish permission denial from missing data, invalid content,
and storage failures; unreadable datasets are retained for revalidation.
Host verification does not establish a successful deployment. South Napa
and Norcia through the deployed REST/CLI service, broader platform coverage,
and regional usability remain unverified. See the
[permission workflow](docs/permissions.md) before recovering an existing runtime.

## Supported workflow

```bash
source /path/to/project/.venv/bin/activate
python -m pip install -e .
make build
make data
make finalize RUNTIME_ROOT=./runtime PORT=9010 MAX_CONCURRENT=10
make verify RUNTIME_ROOT=./runtime PORT=9010 MAX_CONCURRENT=10
shake-in-docker health
shake-in-docker configurations
```

The build reads only `SHAKEMAP_RELEASE_TAG` from `VERSIONS.env`. It creates an
untagged candidate, runs image-internal checks against that image identity, and
assigns `shakemap-docker:latest` only after those checks pass. The active
project environment must already provide the packaged `shake-in-docker`
command; the helper never installs or changes host packages.

The supported dependency pair is `shakemap-modules[all]==1.1.18` with
`esi-shakelib==1.2.1`. Image verification checks both versions and imports all
seven native modules; `pip check` alone cannot detect incompatible APIs.
The complete dependency environment is not fully locked.

Image verification also covers the installed ShakeMap release, STREC
`moment_tensors.db`, Slab2 grids, Natural Earth mapping support, and immutable
regional configuration seeds. Global VS30 and topography remain external.
This is image-installation evidence only, not deployment or calculation
readiness.

`inspect` is cheap and read-only. It checks presence/readability and parses only
small directory entries. `validate` is also read-only, but hashes the two large
pinned global assets. `provision` reuses valid assets and may download or import
only a missing asset. An invalid, incomplete, or unexpected existing asset is
left unchanged and reported with corrective action.

`make finalize` prepares and verifies the canonical deployment and runs the
fixed global calculation before publishing readiness. `make start` only starts
an already-finalized matching deployment. `make verify` requires a running
canonical service and replaces the fixed verification calculation with a new
run. `make stop` preserves the container and runtime.

Keep the project Python environment active when running these host helpers.
Helpers never invoke `sudo`; follow the specific manual recovery command if
one is reported. Do not run the whole installation or finalization as root.

## Runtime data contract

External data lives below `runtime/shakemap/data/`:

```text
global/
  vs30/global_vs30.grd
  topo/topo_30sec.grd
regional/
test/<resolved-version>/
inputs/<event_id>/
```

Stage 2 manages only the displayed global VS30 and topography files. Other
small support data belongs to image build or finalization, not this helper.
Manual placement remains supported. Select the complete runtime with
`RUNTIME_ROOT`; `global/`, `regional/`, and `test/` are read-only container
overlays. `inputs/` and service-owned state remain writable. The service does not create or require
`.service/preparation`, `incoming`, `.service/work`, or a top-level event/work
tree.

## What API evidence means

`/healthz` reports `ready`, `reason`, and `shakemap_version`. `/config` reports
the installed identity, operational settings, and recorded readiness. Neither
endpoint runs checksum validation or a calculation. Use the data helper for
integrity checks and the matching submission's `SUCCESS`, `job_completed`, and
`products_ready` fields to determine completion. Product existence alone is
insufficient. Configuration discovery does not establish regional data coverage
or scientific suitability.

See [docs/quick-start.md](docs/quick-start.md),
[docs/runtime-layout.md](docs/runtime-layout.md), and
[docs/health-and-readiness.md](docs/health-and-readiness.md).
