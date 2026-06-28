# Quick Start Guide

This guide walks through deploying the ShakeMap Docker service from scratch, with expected output at each step and common variations.

For the minimal 4-command version, see the [Quick Start](../README.md#quick-start) section in the README.

---

## Step 1 — Build the Docker Image

```bash
./scripts/build-shakemap-docker.sh
```

This clones the USGS ShakeMap repository, installs all Python dependencies, and bundles the service code into a Docker image tagged `shakemap-service:latest`.

**Expected output (last lines):**

```
[4/4] Build complete
Image: shakemap-service:latest
```

**Variations:**

```bash
# Custom tag
./scripts/build-shakemap-docker.sh --tag shakemap-service:v1.0

# Cross-platform build (e.g., for deployment on linux/amd64)
./scripts/build-shakemap-docker.sh --platform linux/amd64

# Clean build without Docker layer cache
./scripts/build-shakemap-docker.sh --no-cache
```

Build time is typically 5–10 minutes depending on network speed (ShakeMap dependencies are large).

---

## Step 2 — Start the Container

```bash
./scripts/start-shakemap-docker.sh
```

This starts the container with sensible defaults: container name `shakemap`, port 9010, runtime directory at `./runtime`.

**Expected output:**

```
Container 'shakemap' started (detached)
  Port:    9010
  Runtime: ./runtime
  Image:   shakemap-service:latest
```

**Variations:**

```bash
# Custom port and runtime directory
./scripts/start-shakemap-docker.sh --port 8080 --runtime /data/shakemap

# Custom container name
./scripts/start-shakemap-docker.sh --name my-shakemap

# Pass environment variables (e.g., skip data download for testing)
./scripts/start-shakemap-docker.sh \
  --env SHAKEMAP_SKIP_DATA_DOWNLOAD=1 \
  --env SHAKEMAP_ALLOW_UNIFORM_VS30=1

# Run in foreground (useful for debugging)
./scripts/start-shakemap-docker.sh --foreground
```

**Verify Stage 1 is up:**

```bash
curl -s http://localhost:9010/healthz | python3 -m json.tool
```

You should see `"status": "not_ready"` with a blocking reason indicating that Stage 2 configuration has not been run. This is expected.

---

## Step 3 — Configure ShakeMap

```bash
docker exec shakemap /app/scripts/configure-shakemap.sh
```

This runs inside the container and:

1. Creates the ShakeMap profile (default name: `default`)
2. Sets up the data directory symlink (`profile/data` → `SERVICE_ROOT/work`)
3. Detects or downloads VS30 and topography grid files
4. Patches `model.conf` and `products.conf` to reference actual data paths
5. Runs readiness probes (profile structure, config validity, data availability)
6. Writes the readiness sentinel file

**Expected output (last lines):**

```
[9/9] Writing readiness sentinel
READY
```

The script is idempotent — running it again is safe and will re-validate everything.

### Configuration without data downloads

For testing or air-gapped environments:

```bash
# Start with download skip + uniform VS30
./scripts/start-shakemap-docker.sh \
  --env SHAKEMAP_SKIP_DATA_DOWNLOAD=1 \
  --env SHAKEMAP_ALLOW_UNIFORM_VS30=1

# Then configure
docker exec shakemap /app/scripts/configure-shakemap.sh
```

This skips the ~1 GB USGS grid downloads and uses a uniform VS30 value (760 m/s). The service will report `healthy_with_overrides` instead of `healthy`, indicating that it is operational but not fully provisioned for production accuracy.

For details on VS30 provisioning strategies, see the [Configuration Guide](configuration.md).

---

## Step 4 — Verify the Deployment

```bash
./scripts/verify-shakemap-deployment.sh shakemap --expect ready
```

This runs automated checks against the running container:

- Container is running
- User identity is correct (sysop, UID 1000)
- Runtime directories exist and are writable
- ShakeMap CLI is available
- `/healthz` returns the expected status
- Readiness sentinel is present

**Expected output:**

```
All checks passed
```

**Pre-configure verification** (to validate Stage 1 before running configure):

```bash
./scripts/verify-shakemap-deployment.sh shakemap --expect not-ready
```

---

## Step 5 — Submit a Test Event

The repository includes a minimal test fixture with synthetic earthquake data:

```bash
curl -s -X POST http://localhost:9010/events/submit \
  -F "event_id=20240101_120000_fixture" \
  -F "user_id=test" \
  -F "files=@tests/fixtures/shakemap_event_minimal/event.xml" \
  -F "files=@tests/fixtures/shakemap_event_minimal/event_dat.xml" \
  -F "files=@tests/fixtures/shakemap_event_minimal/rupture.json" \
  | python3 -m json.tool
```

**Expected response:**

```json
{
    "event_id": "20240101_120000_fixture",
    "status": "QUEUED",
    "status_path": ".service/events/20240101_120000_fixture/requeststatus.json",
    "replaced_previous": false,
    "validation_errors": null
}
```

### Where to find outputs

After processing:

- **Input files:** `./runtime/shakemap/incoming/20240101_120000_fixture/`
- **Products:** `./runtime/shakemap/products/20240101_120000_fixture/` (if ShakeMap succeeded)
- **Status tracking:** `./runtime/shakemap/.service/events/20240101_120000_fixture/requeststatus.json`

> **Important:** ShakeMap execution success depends on having a VS30 grid that covers the target region. The test fixture is a synthetic event in the Basel Region (Switzerland). See the [Execution Workflow](execution-workflow.md) for details on what determines success or failure.

---

## Makefile Shortcuts

All commands are also available via the Makefile:

```bash
make build        # Build the Docker image
make start        # Start the container
make configure    # Configure ShakeMap
make verify       # Verify the deployment
make inspect      # Print active configuration
make ci           # Run full CI test suite
```

Override the container name: `make configure CONTAINER=my-shakemap`

---

## Next Steps

- [Configuration Guide](configuration.md) — environment variables, VS30 strategies, profiles
- [Runtime Layout](runtime-layout.md) — where files live, what to mount
- [Health and Readiness](health-and-readiness.md) — understanding health statuses
- [Troubleshooting](troubleshooting.md) — common problems and fixes
