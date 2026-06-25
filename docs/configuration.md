# Configuration Guide

This guide covers all environment variables, VS30 data provisioning strategies, and ShakeMap profile management.

For the quick reference table, see the [Configuration](../README.md#configuration) section in the README.

---

## Environment Variables

All configuration is done through environment variables. Set them via `docker run -e`, the start script's `--env` flag, or in your orchestrator configuration.

### Core Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `RUNTIME_ROOT` | `/home/sysop/runtime` | Top-level directory for all persistent data. This is the mount target for host volumes. |
| `SERVICE_ROOT` | `/home/sysop/runtime/shakemap` | ShakeMap service working directory. Contains all event data, products, and logs. Created automatically under `RUNTIME_ROOT`. |
| `SHAKEMAP_PROFILE` | `default` | Name of the ShakeMap profile to create and use. A profile is a named configuration set that includes model parameters, data paths, and processing settings. |
| `SHAKEMAP_PORT` | `9010` | Port the REST API listens on inside the container. Map to a host port via `-p <host>:<container>`. |
| `SHAKEMAP_MODULES` | `select assemble model contour mapping stations gridxml` | Space-separated list of ShakeMap processing modules to run for each event. These are the stages of ShakeMap's processing pipeline. |
| `SHAKEMAP_REQUIRE_MOUNT` | `0` | When set to `1`, the entrypoint verifies that `SERVICE_ROOT` is a Docker volume mount (not just a container directory). This prevents accidental data loss from using ephemeral container storage. |

### Stage 2 Configuration Controls

These variables affect the `configure-shakemap.sh` script behavior:

| Variable | Default | Description |
|----------|---------|-------------|
| `SHAKEMAP_SKIP_DATA_DOWNLOAD` | `0` | When set to `1`, skips downloading VS30 and topography grids from USGS servers during configuration. Use this for testing, air-gapped environments, or when providing custom data files. |
| `SHAKEMAP_ALLOW_UNIFORM_VS30` | `0` | When set to `1`, allows the service to operate without a VS30 grid file. ShakeMap will use a uniform VS30 value of 760 m/s everywhere. **This is a development/testing override — not suitable for production accuracy.** The service reports `healthy_with_overrides` instead of `healthy` when this is active. |
| `SHAKEMAP_VS30_FILE` | _(empty)_ | Absolute path (inside the container) to a custom VS30 grid file. Takes precedence over the default download location. Use this when you want to provide your own regional VS30 data via a volume mount. |
| `SHAKEMAP_TOPO_FILE` | _(empty)_ | Absolute path (inside the container) to a custom topography grid file. Takes precedence over the default download location. |

---

## VS30 Provisioning Strategies

VS30 (time-averaged shear-wave velocity in the top 30 meters) is a geophysical parameter that determines how ShakeMap models site amplification effects. Without proper VS30 data, ShakeMap results are less accurate.

There are four strategies for providing VS30 data:

### Strategy 1 — Download from USGS (default)

Leave `SHAKEMAP_SKIP_DATA_DOWNLOAD=0` (the default). During `configure-shakemap.sh`, the script downloads:

- `global_vs30.grd` (~700 MB) — global VS30 grid from USGS
- `topo_30sec.grd` — global topography grid

The files are saved to `SERVICE_ROOT/data/vs30/` and `SERVICE_ROOT/data/topo/`, and `model.conf` is patched to reference them. This provides a usable VS30 grid for any location worldwide.

**Pros:** Works everywhere, no manual data management.
**Cons:** Large download (~1 GB total), requires network access to `apps.usgs.gov`.

### Strategy 2 — Provide Custom Grid via Volume Mount

Mount a directory containing your VS30 grid into the container and set `SHAKEMAP_VS30_FILE`:

```bash
./scripts/start-shakemap-docker.sh \
  --env SHAKEMAP_VS30_FILE=/home/sysop/runtime/custom/my_vs30.grd \
  --env SHAKEMAP_SKIP_DATA_DOWNLOAD=1
```

Place your grid file in the mounted runtime directory so it is accessible at the path you specified.

**Pros:** Use region-specific high-resolution VS30 data, no USGS download needed.
**Cons:** Requires external data management.

### Strategy 3 — Uniform VS30 Override (testing only)

```bash
./scripts/start-shakemap-docker.sh \
  --env SHAKEMAP_SKIP_DATA_DOWNLOAD=1 \
  --env SHAKEMAP_ALLOW_UNIFORM_VS30=1
```

ShakeMap uses a uniform VS30 value of 760 m/s (NEHRP B/C boundary) everywhere. No grid file is needed.

**Pros:** No data downloads, fastest setup.
**Cons:** Produces less accurate ShakeMaps. The service reports `healthy_with_overrides` to make this visible. **Not suitable for production.**

### Strategy 4 — Bake VS30 into the Docker Image

Download the VS30 grid during the Docker build by adding a `RUN` step to the Dockerfile. This makes the data always available without runtime downloads.

**Pros:** Self-contained image, no runtime data provisioning.
**Cons:** Larger Docker image (~700 MB larger), grid data may become outdated.

---

## ShakeMap Profiles

A ShakeMap **profile** is a named configuration set that tells ShakeMap where to find its configuration files, data directory, and processing parameters.

### Default Profile Structure

When `configure-shakemap.sh` creates a profile, it sets up:

```
~/shakemap_profiles/<profile_name>/
├── install/
│   ├── config/        Configuration files (model.conf, select.conf, etc.)
│   ├── data/
│   │   └── layers/    Region boundary polygons (.wkt files)
│   └── logs/          ShakeMap processing logs
└── data               Symlink → SERVICE_ROOT/work
```

The `data` symlink is critical — it bridges ShakeMap's expected directory structure to the service's working directory. When ShakeMap reads input files from `<profile>/data/<event_id>/current/`, it actually reads from `SERVICE_ROOT/work/<event_id>/current/`.

### Using a Custom Profile Name

```bash
./scripts/start-shakemap-docker.sh --env SHAKEMAP_PROFILE=my_region
docker exec shakemap /app/scripts/configure-shakemap.sh
```

The configure script uses the `SHAKEMAP_PROFILE` variable to create the profile with the specified name.

### Inspecting the Active Profile

```bash
# Via CLI
docker exec shakemap /app/scripts/inspect-shakemap-config.sh

# Via REST API
curl -s http://localhost:9010/config | python3 -m json.tool
curl -s http://localhost:9010/config/profiles | python3 -m json.tool
```

---

## ShakeMap Modules

The `SHAKEMAP_MODULES` variable controls which processing stages ShakeMap runs for each event. The default set is:

| Module | Purpose |
|--------|---------|
| `select` | Select the appropriate GMPE (Ground Motion Prediction Equation) based on tectonic region |
| `assemble` | Assemble input data, validate VS30 and station data |
| `model` | Run the ground motion model calculations |
| `contour` | Generate contour polygons for ground motion parameters |
| `mapping` | Generate map images |
| `stations` | Process and report station data |
| `gridxml` | Generate output grids in XML format |

Modules execute in the order listed. You can omit modules if you don't need their outputs (e.g., remove `mapping` if you only need numerical grids).

---

## Configuration Precedence

1. **Environment variables** (set at container start) take highest precedence.
2. **Dockerfile defaults** (defined in `ENV` block) apply when no environment variable is set.
3. **ShakeMap defaults** (from ShakeMap's built-in `modelspec.conf`) apply for ShakeMap-specific settings not overridden by the profile's `model.conf`.

The `configure-shakemap.sh` script reads environment variables and patches the ShakeMap profile configuration files accordingly. Running the script again re-applies the current environment variable values.

---

## Related Documentation

- [Quick Start](quick-start.md) — getting started with common configurations
- [Permissions Guide](permissions.md) — volume mount setup and file ownership
- [Health and Readiness](health-and-readiness.md) — how overrides affect health status
