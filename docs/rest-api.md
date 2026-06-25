# REST API Reference

This guide documents all REST API endpoints with request/response schemas, examples, and error codes.

For the endpoint summary table, see the [REST API](../README.md#rest-api) section in the README.

---

## Base URL

```
http://localhost:9010
```

The port is configurable via `SHAKEMAP_PORT` (default: `9010`).

---

## Endpoints

### GET /healthz

Health and readiness status. Returns the current state of both Stage 1 (infrastructure) and Stage 2 (configuration) checks.

**Request:**

```bash
curl -s http://localhost:9010/healthz | python3 -m json.tool
```

**Response (healthy):**

```json
{
  "status": "healthy",
  "blocking_reasons": [],
  "next_action": "",
  "overrides": [],
  "override_warnings": [],
  "stage1": { "passed": true, "checks": { ... } },
  "stage2": { "passed": true, "checks": { ... }, "active_profile": "default", "available_profiles": ["default"] },
  "infrastructure": { "service_root": "...", "directories": { ... } },
  "shakemap": { "shake_cli_available": true, "shake_cli_responsive": true },
  "configuration": { "modules": "...", "service_root": "..." }
}
```

**Response (not ready):**

```json
{
  "status": "not_ready",
  "blocking_reasons": ["Stage 2 configuration has not been run"],
  "next_action": "Run: docker exec <container> /app/scripts/configure-shakemap.sh",
  "overrides": [],
  "override_warnings": [],
  "stage1": { "passed": true, "checks": { ... } },
  "stage2": { "passed": false, "reason": "Stage 2 configuration has not been run", "checks": { ... } }
}
```

**Status codes:** Always returns HTTP 200. Check the `status` field in the response body.

For the full response schema details, see the [Health and Readiness Guide](health-and-readiness.md).

---

### GET /config

Active configuration inspection. Returns the current ShakeMap configuration including profile, data file paths, readiness state, and any active overrides.

**Request:**

```bash
curl -s http://localhost:9010/config | python3 -m json.tool
```

**Response:**

```json
{
  "active_profile": "default",
  "available_profiles": ["default"],
  "profiles_conf_path": "/home/sysop/.shakemap/profiles.conf",
  "profiles_conf_exists": true,
  "model_conf_path": "/home/sysop/shakemap_profiles/default/install/config/model.conf",
  "model_conf_exists": true,
  "products_conf_path": "/home/sysop/shakemap_profiles/default/install/config/products.conf",
  "products_conf_exists": false,
  "products_conf_required": false,
  "vs30_file": "/home/sysop/runtime/shakemap/data/vs30/global_vs30.grd",
  "vs30_file_exists": true,
  "topo_file": "/home/sysop/runtime/shakemap/data/topo/topo_30sec.grd",
  "topo_file_exists": true,
  "readiness_state": "ready",
  "readiness_reason": "",
  "overrides": [],
  "override_warnings": [],
  "service_root": "/home/sysop/runtime/shakemap",
  "shakemap_modules": "select assemble model contour mapping stations gridxml"
}
```

**Key fields:**

| Field | Description |
|-------|-------------|
| `readiness_state` | `ready`, `ready_with_overrides`, or `not_ready` |
| `products_conf_required` | Always `false` — ShakeMap uses built-in defaults when `products.conf` is absent |
| `overrides` | List of active override flags (e.g., `["uniform_vs30_override"]`) |
| `override_warnings` | Human-readable warnings about overrides |

---

### GET /config/profiles

List existing ShakeMap profiles with validation status.

**Request:**

```bash
curl -s http://localhost:9010/config/profiles | python3 -m json.tool
```

**Response:**

```json
{
  "active_profile": "default",
  "profile_count": 1,
  "profiles": [
    {
      "name": "default",
      "is_active": true,
      "profile_root": "/home/sysop/shakemap_profiles/default",
      "config_dir_exists": true,
      "model_conf_exists": true,
      "data_dir_is_symlink": true,
      "valid": true
    }
  ]
}
```

A profile is `valid` when its config directory exists and contains `model.conf`.

---

### POST /events/submit

Submit an earthquake event for ShakeMap processing. Accepts multipart form data with event metadata and input files.

**Request:**

```bash
curl -s -X POST http://localhost:9010/events/submit \
  -F "event_id=my_event_001" \
  -F "user_id=operator" \
  -F "files=@event.xml" \
  -F "files=@event_dat.xml" \
  -F "files=@rupture.json" \
  | python3 -m json.tool
```

**Form fields:**

| Field | Required | Description |
|-------|----------|-------------|
| `event_id` | Yes | Unique event identifier |
| `user_id` | Yes | Identifier of the submitting user or system |
| `files` | Yes | One or more input files (multipart file upload, repeatable) |

**Required input files:**

| File | Required | Description |
|------|----------|-------------|
| `event.xml` | Yes | Earthquake origin parameters (magnitude, location, depth, time) |
| Station file (one of below) | Yes | Ground motion observations from seismic stations |
| `rupture.json` | No | Fault rupture geometry (GeoJSON) |

**Accepted station file names:** `stationlist.json`, `stationlist.xml`, `event_dat.xml`

**Response (success — HTTP 200):**

```json
{
  "event_id": "my_event_001",
  "status": "QUEUED",
  "status_path": "events/my_event_001/.shakemap-service/requeststatus.json",
  "replaced_previous": false,
  "validation_errors": null
}
```

**Response (validation failed — HTTP 422):**

```json
{
  "event_id": "my_event_001",
  "status": "VALIDATION_FAILED",
  "status_path": "events/my_event_001/.shakemap-service/requeststatus.json",
  "replaced_previous": false,
  "validation_errors": ["Missing required file: event.xml"]
}
```

**Status codes:**

| Code | Condition |
|------|-----------|
| 200 | Event accepted and queued |
| 400 | Invalid request (empty event_id, no files, etc.) |
| 422 | Validation failed (missing required files) |
| 503 | Service not ready (Stage 2 not complete) |

**Duplicate submissions:** If an event with the same `event_id` already exists in a non-running state, the input files are atomically replaced and the event is re-queued. The response includes `"replaced_previous": true`.

---

### Auto-Generated Documentation

FastAPI automatically provides interactive documentation:

| Endpoint | Format |
|----------|--------|
| `GET /docs` | Swagger UI — interactive API explorer |
| `GET /redoc` | ReDoc — alternative API documentation |
| `GET /openapi.json` | OpenAPI 3.x schema (machine-readable) |

---

## Related Documentation

- [Health and Readiness](health-and-readiness.md) — full health response schema
- [Execution Workflow](execution-workflow.md) — what happens after submission
- [Configuration Guide](configuration.md) — environment variables and settings
