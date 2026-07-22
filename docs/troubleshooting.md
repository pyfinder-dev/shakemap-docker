# Troubleshooting Guide

This guide covers common problems, ShakeMap-specific errors, log analysis, and debugging techniques.

For the top-5 quick fixes, see the [Troubleshooting](../README.md#troubleshooting) section in the README.

---

## Startup Problems

### Container exits immediately with "not writable" error

**Cause:** The host directory mounted as the runtime volume is not owned by UID 1000.

**Fix:**

```bash
chown -R 1000:1000 ./runtime
```

The entrypoint error message includes the specific directory, current owner, and required owner.

### Container starts but ShakeMap CLI is not found

**Cause:** The Docker image did not build correctly, or ShakeMap installation failed during build.

**Fix:** Rebuild the image with `--no-cache`:

```bash
./scripts/build-shakemap-docker.sh --no-cache
```

If the build fails, check network connectivity to `code.usgs.gov` (ShakeMap source repository).

### Port already in use

**Cause:** Another process is using port 9010 on the host.

**Fix:** Use a different port:

```bash
./scripts/start-shakemap-docker.sh --port 8080
```

---

## Configuration Problems

### `/healthz` returns `not_ready` after container start

**This is expected.** Stage 2 configuration has not been run yet.

Check `/config` and `/healthz`, then provide compatible scientific data and
configuration under the mounted runtime. The internal configuration helper may
assist with legacy profiles, but it does not guarantee readiness.

### `configure-shakemap.sh` fails

Check the script output for the specific step that failed. Common causes:

- **Network error during data download:** The script downloads VS30 and topo grids from USGS servers. If the download fails, re-run the script (it uses download-once logic and skips existing files).
- **Profile creation fails:** Check that the ShakeMap CLI is functional: `docker exec <container> shake --help`
- **VS30 missing:** Provide a compatible VS30 grid under the mounted runtime.

The script is idempotent — re-running it is always safe.

### `/healthz` returns `healthy_with_overrides`

This is a development/emergency state, not production or deployment readiness.
The most common override is `uniform_vs30_override`, which means:

- No VS30 grid file is present
- `SHAKEMAP_ALLOW_UNIFORM_VS30=1` was set
- ShakeMap uses a uniform VS30 of 760 m/s everywhere
- Ground motion estimates will be less accurate

To achieve `healthy` (no overrides), provide a real VS30 grid:

1. Remove `SHAKEMAP_SKIP_DATA_DOWNLOAD=1` to allow USGS downloads, or
2. Provide a custom VS30 grid via `SHAKEMAP_VS30_FILE`

Then re-run `configure-shakemap.sh`.

---

## ShakeMap Execution Errors

### "CA_vs30.grd is not a valid file"

**Cause:** The ShakeMap `model.conf` references a California VS30 grid (`CA_vs30.grd`) that does not exist. This is a stale template reference from the default ShakeMap profile.

**Fix:** Provide a compatible VS30 grid and configure `model.conf` to reference
it. Do not use uniform VS30 to establish readiness.

### "The 'vs30file' key in the section 'data' failed validation"

**Cause:** Same root cause as above — `model.conf` references a VS30 file that does not exist.

**Fix:** Same as above.

### ShakeMap succeeds at `select` but fails at `assemble`

**Cause:** The `assemble` module validates VS30 data availability. If the VS30 grid does not cover the event's geographic region, `assemble` fails.

**This is expected behavior** when using the wrong VS30 data for the target region. The default `global_vs30.grd` from USGS covers all regions. Regional grids (like `CA_vs30.grd`) only cover their specific area.

### Event status is FAILED with "Data preparation failed"

**Cause:** The incoming files for the event could not be found or copied to the ShakeMap data directory.

**Diagnosis:**

```bash
# Check if input files exist
ls ./runtime/shakemap/incoming/<event_id>/

# Check the status file for the failure reason
cat ./runtime/shakemap/.service/events/<event_id>/requeststatus.json | python -m json.tool
```

---

## Debugging with requeststatus.json

The `requeststatus.json` file contains the complete event history:

```bash
cat ./runtime/shakemap/.service/events/<event_id>/requeststatus.json \
  | python -m json.tool
```

Key fields to check:

| Field | What it tells you |
|-------|-------------------|
| `status` | Current lifecycle state |
| `failure_reason` | Why the event failed (if applicable) |
| `attempt_history` | All execution attempts with timestamps and durations |
| `attempt_history[N].execution_context` | Which profile and modules were used |
| `attempt_history[N].failure_reason` | Per-attempt failure reason |
| `current_attempt` / `max_attempts` | How many attempts used vs. available |

---

## Inspecting Active Configuration

Use the inspect script or REST API to see the current configuration:

```bash
# CLI mode (inside container)
docker exec <container> /app/scripts/inspect-shakemap-config.sh

# REST API mode (inside container)
docker exec <container> /app/scripts/inspect-shakemap-config.sh --rest

# Direct REST API call (from host)
curl -s http://localhost:9010/config | python3 -m json.tool
```

---

## Container Restart Behavior

When a container is stopped and a new one is created from the same image with the same volume mount:

| What persists (on mounted volume) | What resets (inside container) |
|-----------------------------------|-------------------------------|
| Event data (`events/`, `incoming/`, `products/`) | ShakeMap profile (`~/shakemap_profiles/`) |
| VS30 and topo grids (`data/`) | Readiness sentinel (`~/.shakemap/`) |
| Processing workspace (`work/`) | `profiles.conf` |
| Service logs (`logs/`) | — |

**After container recreation, you must re-run `configure-shakemap.sh`.** This is fast when data files already exist on the mounted volume — the script detects existing files and skips downloads.

### Interrupted Events

If the container stops while an event is `RUNNING`:

- On the next container start, the worker detects stale `RUNNING` events
- The interrupted attempt is marked as `FAILED` with reason "interrupted by restart"
- If attempts remain, the event is re-queued automatically
- If max attempts are exhausted, the event transitions to `FAILED`

---

## Log Locations

| Log | Location | Access |
|-----|----------|--------|
| Entrypoint output | Docker container logs | `docker logs <container>` |
| Configure script output | Docker exec output | Displayed during execution |
| FastAPI/uvicorn logs | Docker container logs | `docker logs <container>` |
| ShakeMap processing logs | `~/shakemap_profiles/<profile>/install/logs/` | Inside container (not on mounted volume) |

---

## Getting Help

If you encounter a problem not covered here:

1. Check `/healthz` for `blocking_reasons` and `next_action`
2. Check `requeststatus.json` for the specific event
3. Check `docker logs <container>` for entrypoint and service logs
4. Run `inspect-shakemap-config.sh` to verify the active configuration

---

## Related Documentation

- [Health and Readiness](health-and-readiness.md) — interpreting health statuses
- [Execution Workflow](execution-workflow.md) — understanding event processing
- [Permissions Guide](permissions.md) — platform-specific permission issues
- [Configuration Guide](configuration.md) — environment variables and VS30 strategies
