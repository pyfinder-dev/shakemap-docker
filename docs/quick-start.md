# Quick start

For a combined PyFinder deployment, use
[pyfinder-deploy](../../pyfinder-deploy/README.md) and its shared runtime.
The commands below are a standalone service alternative, run from the
`shakemap-docker` repository root with an activated project Python environment.
They use `./runtime` as an example; choose one runtime root and pass it
consistently to every helper. Deployment wrappers delegate to the reusable
entry points in [the helper guide](../scripts/README.md#deployment-orchestration-boundary).

Install the host management tools and `shake-in-docker` HTTP client. This creates
the host command; USGS ShakeMap and its calculation dependencies are installed
inside Docker by the next build. The current host package also installs its
declared service libraries.

```bash
python -m pip install -e .
```

Build the service image, download missing default VS30/topography grids, then
prepare the runtime and test ShakeMap using the supplied global earthquake:

```bash
make build
make data RUNTIME_ROOT=./runtime
make finalize RUNTIME_ROOT=./runtime PORT=9010 MAX_CONCURRENT=10
```

After finalization succeeds, ShakeMap is running. Confirm that `health` reports
`ready: true` and list the available configuration names:

```bash
shake-in-docker health
shake-in-docker configurations
```

The image helper reads the fixed release from `VERSIONS.env`, verifies an
untagged candidate, and changes `shakemap-docker:latest` only after the
candidate passes. It does not install the project into the host environment.
Image success is not deployment readiness.

The VS30 grid describes ground conditions; topography provides terrain for maps.
They are stored under `runtime/shakemap/data/global/vs30/` and `.../topo/`.
For an existing or manually supplied copy, read the complete files and compare
them with the project's recorded checksums:

```bash
./scripts/manage-shakemap-data.sh validate --runtime ./runtime
```

Finalization refuses queued/running work, prepares the runtime and mounts,
checks effective write access, and runs the fixed South Napa/global calculation.
Successful finalization leaves the canonical service running. A failure must be
resolved before use; follow the diagnostic and the
[permission recovery instructions](permissions.md) when applicable.

### Optional comprehensive recheck

You do not need another verification calculation to finish successful
finalization. To deliberately repeat the full component verification, install
the extra host test dependencies first:

```bash
python -m pip install -r requirements.txt
make verify RUNTIME_ROOT=./runtime PORT=9010 MAX_CONCURRENT=10
```

This expects the canonical service to be running. It runs host tests, installed
container checks and a new calculation under the fixed verification ID. It can
mark the service unavailable or stop it on failure; follow its reported error
before retrying. These checks do not validate your regional models or datasets.

### Submit your own inputs

After successful finalization, submit native inputs through the host client.
Choose a calculation ID: a name for this calculation. `global-test-001` below is
only an example, not a reserved keyword or a generated value. Choose a name that
has not been used on your installation, and use that exact name in `submit`,
`status` and `products`. Replace the file paths with the actual full host paths
to your `event.xml` and `event_dat.xml`. Use the profile and service URL intended
for your calculation.

```bash
shake-in-docker submit global-test-001 --configuration global --overwrite true \
  --file /path/to/event.xml --file /path/to/event_dat.xml
shake-in-docker status global-test-001
shake-in-docker products global-test-001
shake-in-docker queue
```

Save the submission's `internal_sequence`, find that entry in `status`'s `jobs`
list and repeat the status request until it is `SUCCESS` or `FAILED`. Success also requires
`job_completed=true` and `products_ready=true`; on failure, inspect the reported
reason and log paths. The products response must describe the same sequence.
See [result fields and logs](../../pyfinder-deploy/docs/verification.md#check-a-regional-calculation)
for the full checks. A listed regional name alone does not establish that its
files or scientific settings work.

Select the complete runtime using `RUNTIME_ROOT`; only its scientific-data
subtrees are read-only overlays. Use `shake-in-docker --url http://localhost:9010 ...`
for an explicit service URL. Use `make stop` for graceful shutdown and
`make start` for a matching, already-finalized deployment. Changing the image
or runtime requires finalization. Keep the project environment active; do not
run finalization with `sudo`.

Read persisted setup/start phases in `runtime/shakemap/logs/operations.log` and
container startup/API diagnostics in `runtime/shakemap/logs/service.log`.
`docker logs shakemap-docker` still shows console output. See
[service diagnostics](../README.md#service-diagnostics) for exact scope,
calculation-log separation, access requirements and retention.
