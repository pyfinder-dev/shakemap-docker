# ShakeMap Docker Service

A Docker-based deployment of [USGS ShakeMap 4](https://code.usgs.gov/ghsc/esi/shakemap) with a REST API for earthquake event submission, processing, and health monitoring. The service wraps the official ShakeMap software in a managed container with filesystem-based event queuing, automatic retry, and structured output publication. It is designed for seismic early warning and rapid earthquake assessment pipelines.

## Prerequisites

- **Docker** (version 20.10 or later)
- **~4 GB disk space** for the Docker image build
- **Network access** to `code.usgs.gov` during build (to clone the ShakeMap repository)
- **Network access** to `apps.usgs.gov` during configuration (optional — for downloading VS30 and topography grids)
- **Host directory** for persistent runtime data (volume mount)
- Basic familiarity with Docker commands (`docker run`, `docker exec`)

## Quick Start

Four commands take you from zero to a running, configured ShakeMap service:

```bash
# 1. Build the Docker image
./scripts/build-shakemap-docker.sh

# 2. Start the container
./scripts/start-shakemap-docker.sh

# 3. Configure ShakeMap inside the running container
docker exec shakemap /app/scripts/configure-shakemap.sh

# 4. Verify the deployment
./scripts/verify-shakemap-deployment.sh shakemap --expect ready
```

Or use the Makefile:

```bash
make build && make start && make configure && make verify
```

**What each step does:**

1. **Build** — compiles the Docker image, installs ShakeMap and its dependencies from USGS source.
2. **Start** — launches the container, creates runtime directories, starts the REST API on port 9010. The service reports `not_ready` until configured.
3. **Configure** — creates a ShakeMap profile, provisions data files (VS30 grids, topography), patches configuration, and writes the readiness sentinel. The service transitions to `healthy`.
4. **Verify** — runs automated checks against the running container to confirm health, directory structure, and API availability.

> **Note:** If you want to skip the USGS data download (for testing or air-gapped environments), pass environment variables at start time. See the [Configuration](#configuration) section below.

For a full walkthrough with expected terminal output and common variations, see the [Quick Start Guide](docs/quick-start.md).

## How It Works

The service uses a **two-stage lifecycle**:

**Stage 1 — Build and Start.** The Docker image is built and the container starts. The entrypoint creates the runtime directory structure, verifies that the ShakeMap CLI is installed, and starts the FastAPI REST API. At this point the service is running but reports `not_ready` because ShakeMap is not yet configured.

**Stage 2 — Configure.** The operator runs `configure-shakemap.sh` inside the container. This script creates a ShakeMap profile, sets up the data directory symlink, detects or downloads VS30 and topography grids, patches `model.conf` and `products.conf`, runs readiness probes, and writes a readiness sentinel file. Once Stage 2 succeeds, the service reports `healthy` and accepts event submissions.

**Why two stages?** ShakeMap profile configuration requires a running container with access to the ShakeMap CLI. Data downloads may be skipped or customized. Separating the stages allows operators to validate infrastructure (Stage 1) before committing to configuration (Stage 2), and to re-run configuration without restarting the container.

For the detailed system architecture including module responsibilities and data flow, see the [Architecture Guide](docs/architecture.md).

## Configuration

Environment variables control the service behavior. Set them via `docker run -e`, the start script's `--env` flag, or in your orchestrator.

| Variable | Default | Purpose |
|----------|---------|---------|
| `RUNTIME_ROOT` | `/home/sysop/runtime` | Top-level runtime directory inside the container |
| `SERVICE_ROOT` | `/home/sysop/runtime/shakemap` | ShakeMap service working directory |
| `SHAKEMAP_PROFILE` | `default` | ShakeMap profile name |
| `SHAKEMAP_PORT` | `9010` | REST API port |
| `SHAKEMAP_MODULES` | `select assemble model contour mapping stations gridxml` | ShakeMap processing modules (space-separated) |
| `SHAKEMAP_REQUIRE_MOUNT` | `0` | Set to `1` to require SERVICE_ROOT to be a Docker volume mount |
| `SHAKEMAP_SKIP_DATA_DOWNLOAD` | `0` | Set to `1` to skip USGS grid downloads during configuration |
| `SHAKEMAP_ALLOW_UNIFORM_VS30` | `0` | Set to `1` to allow operation without a VS30 grid file (uses uniform 760 m/s) |
| `SHAKEMAP_VS30_FILE` | _(empty)_ | Path to a custom VS30 grid file inside the container |
| `SHAKEMAP_TOPO_FILE` | _(empty)_ | Path to a custom topography grid file inside the container |

**VS30** (time-averaged shear-wave velocity in the top 30 meters) is a geophysical parameter that ShakeMap uses to estimate how local soil conditions amplify ground shaking. Without a VS30 grid file, ShakeMap can use a uniform default value (760 m/s), but this produces less accurate results and is **not suitable for production use**.

**Example — testing without data downloads:**

```bash
./scripts/start-shakemap-docker.sh \
  --env SHAKEMAP_SKIP_DATA_DOWNLOAD=1 \
  --env SHAKEMAP_ALLOW_UNIFORM_VS30=1
```

For detailed explanations of each variable, VS30 provisioning strategies, and profile management, see the [Configuration Guide](docs/configuration.md).

## Runtime Directory Layout

Inside the container, the service root at `SERVICE_ROOT` (`/home/sysop/runtime/shakemap/` by default) contains:

```
/home/sysop/runtime/shakemap/    (SERVICE_ROOT)
├── events/        Event tracking — per-event status in requeststatus.json
├── incoming/      Submitted input files, staged atomically per event
├── work/          ShakeMap private processing directory
├── products/      Published outputs — completed ShakeMap results
├── archive/       Completed-run archive
├── logs/          Service logs
└── data/          Shared data files
    ├── vs30/      VS30 grid files
    └── topo/      Topography grid files
```

When you mount a host directory (e.g., `./runtime:/home/sysop/runtime`), all event data, products, and logs persist across container restarts.

For the complete directory tree including per-event structure, ShakeMap profile layout, and volume mount guidance, see the [Runtime Layout Guide](docs/runtime-layout.md).

## Health and Readiness

Check the service status at any time:

```bash
curl -s http://localhost:9010/healthz | python3 -m json.tool
```

The `/healthz` endpoint returns one of three statuses:

| Status | Meaning |
|--------|---------|
| `healthy` | Both stages passed. Service is fully operational with proper data files. |
| `healthy_with_overrides` | Both stages passed, but with operator overrides active (e.g., uniform VS30 instead of a real grid file). Functional but **not fully provisioned for production accuracy**. |
| `not_ready` | One or both stages have not passed. The response includes `blocking_reasons` (what is wrong) and `next_action` (what to do). |

When the service is `not_ready`, event submissions return HTTP 503 with the reason.

For the full health response schema, override model, and Kubernetes probe configuration, see the [Health and Readiness Guide](docs/health-and-readiness.md).

## REST API

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `GET` | `/healthz` | Health and readiness status |
| `GET` | `/config` | Active configuration inspection |
| `GET` | `/config/profiles` | List ShakeMap profiles with validation status |
| `POST` | `/events/submit` | Submit an earthquake event for processing |
| `GET` | `/docs` | Interactive API documentation (Swagger UI) |
| `GET` | `/redoc` | Alternative API documentation |
| `GET` | `/openapi.json` | OpenAPI schema |

For request/response schemas, examples, and error codes, see the [REST API Reference](docs/rest-api.md).

## Running Your First ShakeMap

The repository includes a minimal test fixture at `tests/fixtures/shakemap_event_minimal/` with synthetic earthquake data (M5.0, Basel Region). After completing the [Quick Start](#quick-start):

```bash
# Submit the test event
curl -s -X POST http://localhost:9010/events/submit \
  -F "event_id=20240101_120000_fixture" \
  -F "user_id=test" \
  -F "files=@tests/fixtures/shakemap_event_minimal/event.xml" \
  -F "files=@tests/fixtures/shakemap_event_minimal/event_dat.xml" \
  -F "files=@tests/fixtures/shakemap_event_minimal/rupture.json"
```

The response includes the event status and a path to the tracking file. After processing completes:

- **Input files** are staged in `SERVICE_ROOT/incoming/<event_id>/`
- **Output products** are published to `SERVICE_ROOT/products/<event_id>/`
- **Event status** is tracked in `SERVICE_ROOT/events/<event_id>/.shakemap-service/requeststatus.json`

> **Important:** ShakeMap execution requires a properly configured profile with valid VS30 data for the target region. The test fixture uses a synthetic event in the Basel Region (Switzerland). Running it against a container configured with `SHAKEMAP_ALLOW_UNIFORM_VS30=1` (no real VS30 grid) will produce a ShakeMap using uniform site conditions. Running against a container with only the default California VS30 data will fail because the data does not cover the Basel region. Full end-to-end execution with downloaded USGS data has not yet been verified in automated testing.

For the complete event lifecycle, input file requirements, and output format, see the [Execution Workflow Guide](docs/execution-workflow.md).

## Volume Mounts and Permissions

**Default volume mount:**

```bash
-v ./runtime:/home/sysop/runtime
```

The container runs as user `sysop` with **UID 1000** and **GID 1000**. On Linux, the host directory must be writable by this user:

```bash
# Fix permissions on the host before starting
chown -R 1000:1000 ./runtime
```

The entrypoint detects permission problems at startup and fails with an actionable error message including the required `chown` command. It **cannot fix** host-side permissions from inside the container.

For platform-specific behavior on macOS Docker Desktop and Windows, see the [Permissions Guide](docs/permissions.md).

## Scripts Reference

| Script | Run From | Purpose |
|--------|----------|---------|
| `build-shakemap-docker.sh` | Host | Build the Docker image |
| `start-shakemap-docker.sh` | Host | Start the service container with defaults |
| `configure-shakemap.sh` | Container | Configure ShakeMap profile and data (Stage 2) |
| `verify-shakemap-deployment.sh` | Host | Verify a running deployment |
| `inspect-shakemap-config.sh` | Container | Print the active ShakeMap configuration |
| `verify-shakemap-build.sh` | Container | _(Dev)_ Verify build infrastructure |
| `verify-shakemap-config.sh` | Container | _(Dev)_ Verify configuration state |
| `run-shakemap-ci-tests.sh` | Host | _(Dev)_ Full CI test suite |

For full flag documentation and usage examples for each script, see the [Scripts Reference](scripts/README.md).

## Troubleshooting

**Container starts but `/healthz` returns `not_ready`:**
Run Stage 2 configuration: `docker exec <container> /app/scripts/configure-shakemap.sh`

**Entrypoint fails with "not writable" error:**
Fix host directory permissions: `chown -R 1000:1000 ./runtime`

**ShakeMap fails with `CA_vs30.grd` or `vs30file` error:**
The VS30 grid file is not provisioned. Either download USGS data (default), provide a custom grid via `SHAKEMAP_VS30_FILE`, or set `SHAKEMAP_ALLOW_UNIFORM_VS30=1` for testing.

**Port 9010 already in use:**
Start with a different port: `./scripts/start-shakemap-docker.sh --port 8080`

**`/healthz` returns `healthy_with_overrides`:**
The service is functional but using the uniform VS30 override (760 m/s everywhere). This is acceptable for testing but not for production accuracy. Provide a real VS30 grid to achieve `healthy` status.

**`configure-shakemap.sh` fails or the sentinel says `not_ready`:**
Check the script output for the specific failure reason. Re-running the script is safe (it is idempotent).

For extended troubleshooting including log analysis, ShakeMap-specific errors, and `requeststatus.json` debugging, see the [Troubleshooting Guide](docs/troubleshooting.md).

## Development

**Run the Python test suite** (from the host, with the virtualenv activated):

```bash
python tests/test_event_status_records.py
python tests/test_event_submission_staging.py
python tests/test_durable_queue.py
python tests/test_worker_claim_locking.py
python tests/test_shakemap_fixtures.py
python tests/test_execution_bridge.py
```

**Run the full CI test suite** (builds image, starts container, runs integration checks):

```bash
./scripts/run-shakemap-ci-tests.sh
```

For codebase structure, module descriptions, test strategy, and contribution guidelines, see the [Developer Guide](docs/developer-guide.md).

## License

See [LICENSE](LICENSE) for full license terms.
