# Health and Readiness Guide

This guide covers the complete health model, readiness semantics, override reporting, and Kubernetes probe configuration.

For the quick overview, see the [Health and Readiness](../README.md#health-and-readiness) section in the README.

---

## Health Statuses

The `/healthz` endpoint returns one of three statuses:

| Status | Meaning | Event Submissions |
|--------|---------|-------------------|
| `healthy` | Both Stage 1 and Stage 2 passed. ShakeMap is fully configured with proper VS30 data. | Accepted |
| `healthy_with_overrides` | Current code accepted an explicit development/emergency override. This does not establish production or deployment readiness. | Accepted by current code |
| `not_ready` | One or both stages have not passed. The response includes `blocking_reasons` and `next_action`. | Rejected with HTTP 503 |

There is no `degraded` status. The service is either ready to process events or it is not.

---

## Two-Stage Health Model

### Stage 1 — Infrastructure

Checked automatically when the container starts. Verifies:

- All six service directories exist (`events/`, `incoming/`, `work/`, `products/`, `archive/`, `logs/`)
- All directories are writable by the `sysop` user
- The ShakeMap CLI (`shake`) is on PATH
- The ShakeMap CLI responds to `shake --help` within 15 seconds

Stage 1 passes if all infrastructure checks succeed. If Stage 1 fails, the container typically exits at startup with an actionable error message.

### Stage 2 — Configuration and Data

Checked via a readiness sentinel file written by `configure-shakemap.sh`. Validates:

- ShakeMap profile exists and is registered in `profiles.conf`
- Profile config directory contains `model.conf`
- Profile data directory is symlinked to `SERVICE_ROOT/work`
- VS30 grid file exists and is non-empty (or uniform VS30 override is active)
- `model.conf` does not reference stale placeholder paths (e.g., `CA_vs30.grd`)
- The readiness sentinel file says `ready`

Stage 2 passes only when the sentinel file indicates readiness. The sentinel is written last by `configure-shakemap.sh`, so it reflects the outcome of all preceding checks.

---

## Response Schema

A full `/healthz` response:

```json
{
  "status": "healthy",
  "blocking_reasons": [],
  "next_action": "",
  "overrides": [],
  "override_warnings": [],
  "stage1": {
    "passed": true,
    "checks": {
      "directories_exist": true,
      "directories_writable": true,
      "shake_cli_available": true,
      "shake_cli_responsive": true
    }
  },
  "stage2": {
    "passed": true,
    "checks": {
      "vs30_file": "/home/sysop/runtime/shakemap/data/vs30/global_vs30.grd",
      "vs30_file_exists": true,
      "vs30_file_non_empty": true,
      "model_conf_valid": true,
      "topo_file": "/home/sysop/runtime/shakemap/data/topo/topo_30sec.grd",
      "topo_file_exists": true,
      "topo_file_non_empty": true,
      "profiles_conf_readable": true,
      "profile_exists": true,
      "profile_config_valid": true,
      "profile_data_bridge_ok": true
    },
    "active_profile": "default",
    "available_profiles": ["default"]
  },
  "infrastructure": {
    "service_root": "/home/sysop/runtime/shakemap",
    "directories": {
      "events": {"exists": true, "writable": true},
      "incoming": {"exists": true, "writable": true},
      "work": {"exists": true, "writable": true},
      "products": {"exists": true, "writable": true},
      "archive": {"exists": true, "writable": true},
      "logs": {"exists": true, "writable": true}
    }
  },
  "shakemap": {
    "shake_cli_available": true,
    "shake_cli_responsive": true
  },
  "configuration": {
    "modules": "select assemble model contour mapping stations gridxml",
    "service_root": "/home/sysop/runtime/shakemap"
  }
}
```

### Key Fields

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | `healthy`, `healthy_with_overrides`, or `not_ready` |
| `blocking_reasons` | list[string] | Human-readable reasons why the service is not ready. Empty when healthy. |
| `next_action` | string | Recommended fix. Empty when healthy. Example: `"Run: docker exec <container> /app/scripts/configure-shakemap.sh"` |
| `overrides` | list[string] | Active override flags. Example: `["uniform_vs30_override"]` |
| `override_warnings` | list[string] | Warning messages about active overrides. |
| `stage1.passed` | boolean | Whether infrastructure checks passed |
| `stage2.passed` | boolean | Whether configuration/data checks passed |

---

## Override Model

When `SHAKEMAP_ALLOW_UNIFORM_VS30=1` is set and no VS30 grid file is present:

- `configure-shakemap.sh` writes the sentinel as `ready|uniform_vs30_override` instead of plain `ready`
- `/healthz` returns `"status": "healthy_with_overrides"` instead of `"healthy"`
- The `overrides` field contains `["uniform_vs30_override"]`
- The `override_warnings` field contains a warning explaining the implications

This exposes the override, but it does not make that deployment production-ready
or replace real calculation verification.

---

## Readiness Sentinel

The readiness sentinel is a file at `~/.shakemap/.shakemap_readiness_status` inside the container. It is the primary signal for Stage 2 readiness.

**Sentinel values:**

| Content | Meaning |
|---------|---------|
| `ready` | Stage 2 passed, no overrides |
| `ready\|uniform_vs30_override` | Stage 2 passed with uniform VS30 override |
| `not_ready\|<reason>` | Stage 2 failed with the given reason |

The sentinel is:

- **Written last** by `configure-shakemap.sh`, after all checks pass
- **Read on every `/healthz` request** (fast file read, no subprocess)
- **Ephemeral** — it lives inside the container filesystem, not on the mounted volume. Re-run `configure-shakemap.sh` after container recreation.
- **Idempotent** — re-running `configure-shakemap.sh` overwrites the sentinel correctly

---

## Blocking Reasons and Next Action

When the service is `not_ready`, the response includes actionable guidance:

```json
{
  "status": "not_ready",
  "blocking_reasons": [
    "Stage 2 configuration has not been run"
  ],
  "next_action": "Run: docker exec <container> /app/scripts/configure-shakemap.sh"
}
```

Common blocking reasons:

| Reason | Fix |
|--------|-----|
| `Stage 2 configuration has not been run` | Run `configure-shakemap.sh` |
| `Directory <name>/ is not writable` | `chown -R 1000:1000 <host-runtime-dir>` |
| `ShakeMap CLI (shake) not found on PATH` | Rebuild the Docker image |
| `VS30 grid missing` | Provide a compatible VS30 grid. Uniform VS30 is development/emergency only. |
| `model.conf validation failed` | Re-run `configure-shakemap.sh` |
| `Profile data symlink not correct` | Re-run `configure-shakemap.sh` |

---

## Kubernetes Probe Configuration

For Kubernetes deployments, map the two-stage health model to probes:

```yaml
# Startup probe — Stage 1 (infrastructure ready)
startupProbe:
  httpGet:
    path: /healthz
    port: 9010
  initialDelaySeconds: 5
  periodSeconds: 5
  failureThreshold: 12   # Allow 60s for ShakeMap CLI warm-up

# Readiness probe — Stage 2 (configuration complete, accepting events)
readinessProbe:
  httpGet:
    path: /healthz
    port: 9010
  periodSeconds: 10
  failureThreshold: 3

# Liveness probe — service is responsive
livenessProbe:
  httpGet:
    path: /healthz
    port: 9010
  periodSeconds: 30
  failureThreshold: 3
```

**Current-code note:** both `healthy` and `healthy_with_overrides` accept event
submissions. Do not route production traffic solely from that behavior:
`healthy_with_overrides` does not establish production/deployment readiness,
and the current HTTP 200 response alone is insufficient for that decision.

---

## Related Documentation

- [Configuration Guide](configuration.md) — override variables and VS30 strategies
- [REST API Reference](rest-api.md) — full endpoint documentation
- [Troubleshooting](troubleshooting.md) — interpreting health status issues
