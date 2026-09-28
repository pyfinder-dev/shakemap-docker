# ShakeMap Docker service

Build and run ShakeMap v4.4.9 behind a REST API and the `shake-in-docker` host
client. Callers supply native inputs, a calculation ID and a configuration name.
The service queues work, runs the complete native module sequence, and retains
products, provenance, manifests and logs.

## Install and start

For a combined PyFinder deployment, start with the
[deployment guide](../pyfinder-deploy/README.md) and its shared runtime. The
commands below are a standalone service alternative. They use `./runtime` as
an example; choose one runtime root and pass it consistently to every helper.

You need Docker and an activated project Python environment with Python 3.10 or
newer. Run these commands from this repository's root:

```bash
# Install the host client and helpers into the active environment.
python -m pip install -e .

# Build and verify the canonical image.
make build

# Provision the required external global grids.
make data RUNTIME_ROOT=./runtime

# Prepare the runtime and verify a native global calculation.
make finalize RUNTIME_ROOT=./runtime PORT=9010 MAX_CONCURRENT=10
```

Finalization leaves the canonical `shakemap-docker` service running when its
checks succeed. Read its state before submitting work:

```bash
shake-in-docker health
shake-in-docker configurations
```

`make start` starts an already-finalized deployment with matching image, mounts
and settings. `make stop` stops it gracefully and preserves its runtime. Use the
same `RUNTIME_ROOT`, `PORT` and `MAX_CONCURRENT` values throughout installation
and startup. The image is `shakemap-docker:latest`; the container is
`shakemap-docker`.

For a combined PyFinder deployment, use the
[deployment guide](../pyfinder-deploy/README.md). Its wrappers delegate to this
repository's helpers; they do not replace their data or lifecycle logic.
See [quick start](docs/quick-start.md) and [helper interfaces](scripts/README.md)
for manual imports and detailed operation.

## Data and configurations

Global VS30 and topography remain external, operator-owned files:

```text
runtime/shakemap/data/
├── global/
│   ├── vs30/global_vs30.grd
│   └── topo/topo_30sec.grd
├── regional/
├── test/<resolved-version>/
└── inputs/<event_id>/
```

The data helper manages only the two global grids. `inspect` checks presence and
readability; `validate` performs full pinned checksums. Both are read-only.
`provision` reuses valid files or installs missing ones. Invalid or unexpected
existing files are retained with an explanation and corrective action. Manual
placement and explicit replacement staging are supported.

The image includes small STREC, Slab2 and mapping support, and regional
configuration seeds. Seeding preserves existing operator profile directories.
Regional modules, data paths and scientific assets must be usable before the
selected profile can calculate. A name in `configurations` is not evidence of
regional readiness. See the [configuration runbook](docs/configuration.md) for
the five native files and Italy/Switzerland prerequisites.

Each service request runs exactly its caller-selected configuration; omission
means `global`. The service never substitutes a different configuration. PyFinder
may make a separate explicit global recovery submission after a confirmed
regional configuration failure, retaining both attempt outcomes. That caller
policy does not change service selection or replacement semantics.

## Verification and outcome evidence

Verification has three separate levels: host tests, installed image/module
checks, and running-service checks. A successful build does not establish
mounted-data readiness. Finalization runs a fixed global calculation before
publishing readiness. To verify an already-running deployment:

```bash
# This submits a new run of the fixed verification calculation.
make verify RUNTIME_ROOT=./runtime PORT=9010 MAX_CONCURRENT=10
```

Finalization and verification are mutating operations. Follow their diagnostics
if they fail; they may stop the service or revoke readiness. Keep the project
environment active and do not run the entire workflow with `sudo`. Targeted
[permission recovery](docs/permissions.md) is available when needed.

`/healthz` reports readiness, reason and installed ShakeMap version. `/config`
reports operational settings and identity. Neither performs large checksums or
scientific validation. For a submitted calculation, follow its exact returned
sequence and require `SUCCESS`, `job_completed=true`, `products_ready=true`,
validated core products, provenance, a manifest and logs. Product existence
alone is insufficient.

A successful verification covers its recorded inputs and selected configuration;
it does not establish all regional branches, geographic coverage or scientific
accuracy. The supported native dependency pair is
`shakemap-modules[all]==1.1.18` with `esi-shakelib==1.2.1`; the complete dependency
environment is not fully locked.

See the [REST and CLI guide](docs/rest-api.md),
[runtime layout](docs/runtime-layout.md),
[health and readiness](docs/health-and-readiness.md), and
[troubleshooting](docs/troubleshooting.md).
