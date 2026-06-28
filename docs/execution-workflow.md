# Execution Workflow Guide

This guide documents the complete event processing lifecycle — from submission to output products.

For the quick overview and first-event walkthrough, see the [Running Your First ShakeMap](../README.md#running-your-first-shakemap) section in the README.

---

## Event Lifecycle

Every submitted event progresses through a series of status transitions:

```
REGISTERED → VALIDATING → QUEUED → RUNNING → SUCCESS
                ↓                      ↓
         VALIDATION_FAILED          FAILED
```

| Status | Meaning |
|--------|---------|
| `REGISTERED` | Event record created, submission acknowledged |
| `VALIDATING` | Input files are being validated |
| `VALIDATION_FAILED` | Required files are missing or invalid. Terminal state. |
| `QUEUED` | Validation passed, event is waiting for processing |
| `RUNNING` | ShakeMap is actively processing the event |
| `SUCCESS` | Processing completed, products published |
| `FAILED` | Processing failed (ShakeMap error, data error, etc.) |
| `CANCELLED` | Event cancelled by operator (manual intervention) |
| `ARCHIVED` | Event moved to archive (from any terminal state) |

The event status is tracked in `requeststatus.json` under `.service/events/<event_id>/`. This file is the authoritative record — there is no separate database.

---

## Step-by-Step Processing

### 1. Submission

When `POST /events/submit` is called:

1. A `requeststatus.json` record is created with status `REGISTERED`
2. Status transitions to `VALIDATING`
3. Input files are validated (see requirements below)
4. If validation passes, files are staged atomically to `incoming/<event_id>/` and status becomes `QUEUED`
5. If validation fails, status becomes `VALIDATION_FAILED` with error details

**Atomic staging:** Files are first written to a temporary directory on the same filesystem, then renamed to the target location. This ensures consumers never see partially written files.

### 2. Validation

The submission validates:

- `event.xml` is present (required — earthquake origin parameters)
- At least one station file is present (required — ground motion observations)
- Accepted station file names: `stationlist.json`, `stationlist.xml`, `event_dat.xml`
- `rupture.json` is accepted but not required (fault geometry)

Only filename-based validation is performed. The content of the files is not parsed or schema-validated at submission time.

### 3. Queue

Validated events are queued in FIFO (first-in, first-out) order. Queue ordering is deterministic:

1. Primary sort: `queued_at` timestamp (earliest first)
2. Secondary sort: `submitted_at` timestamp
3. Tiebreaker: `event_id` (alphabetical)

The queue is **filesystem-based** — there is no separate queue database. The worker discovers queued events by scanning `events/*/` for `requeststatus.json` files with `status: "QUEUED"`. This means the queue survives container restarts.

### 4. Claim and Execute

The worker claims the next queued event using a file-level lock (`fcntl.flock`) on the `requeststatus.json` file:

1. Acquire exclusive lock on `requeststatus.json`
2. Re-read the file (verify still `QUEUED` — another process may have claimed it)
3. Transition status to `RUNNING`
4. Record the attempt in `attempt_history`
5. Release the lock

After claiming, the execution bridge:

1. **Prepares data:** Copies files from `incoming/<event_id>/` to `work/<event_id>/current/` (the ShakeMap data directory, via the profile symlink)
2. **Records execution context:** Writes the active profile and modules to the attempt record
3. **Runs ShakeMap:** Invokes `shake --force <event_id> <modules>` with the configured modules
4. **On success:** Finds output products at `work/<event_id>/current/products/`, publishes them atomically to `products/<event_id>/`, transitions to `SUCCESS`
5. **On failure:** Records the failure reason, transitions to `FAILED`

### 5. Product Publication

Products are published atomically:

1. Create a temporary directory under `products/`
2. Copy all output files from the ShakeMap products directory
3. Rename the temporary directory to `products/<event_id>/`

This ensures consumers never see partial product sets.

---

## Input File Requirements

### event.xml (Required)

Earthquake origin parameters in ShakeMap's XML format:

```xml
<earthquake id="20240101_120000_fixture"
            netid="XX" mag="5.0"
            lat="47.60" lon="7.55" depth="8.0"
            time="2024-01-01T12:00:00Z"
            locstring="Basel Region"
            event_type="ACTUAL" />
```

Required attributes: `id`, `netid`, `mag`, `lat`, `lon`, `depth`, `time`, `locstring`.

### Station File (Required — one of three formats)

Ground motion observations from seismic stations. At least one file with an accepted name must be present:

| Accepted Name | Format | Description |
|---------------|--------|-------------|
| `stationlist.json` | GeoJSON FeatureCollection | Modern ShakeMap 4 format |
| `stationlist.xml` | ShakeMap XML `<stationlist>` | Legacy XML format |
| `event_dat.xml` | ShakeMap XML `<stationlist>` | Legacy XML format (alternative name) |

### rupture.json (Optional)

Fault rupture geometry in GeoJSON format. For a point source, use a `Point` geometry. If omitted, ShakeMap assumes a point-source approximation.

### Test Fixture

A complete set of test files is included at `tests/fixtures/shakemap_event_minimal/`. See the [fixture README](../tests/fixtures/shakemap_event_minimal/README.md) for format details.

---

## Retry and Recovery

### Attempt Tracking

Each execution attempt is recorded in the `attempt_history` array within `requeststatus.json`:

```json
{
  "attempt_history": [
    {
      "attempt_number": 1,
      "started_at": "2024-01-01T12:05:00+00:00",
      "completed_at": "2024-01-01T12:06:10+00:00",
      "status": "FAILED",
      "failure_reason": "ShakeMap execution failed: 'shake' failed with exit code 1",
      "duration_seconds": 70.635,
      "execution_context": {
        "profile": "default",
        "modules": ["select", "assemble", "model", "contour", "mapping", "stations", "gridxml"]
      }
    }
  ]
}
```

Each event allows up to `max_attempts` (default: 3) execution attempts.

### Interrupted Attempt Recovery

If the container restarts while an event is `RUNNING`:

1. On startup, the worker scans for events with status `RUNNING` (stale from the previous run)
2. The interrupted attempt is recorded as `FAILED` with reason "interrupted by restart"
3. If the event has remaining attempts, it is re-queued (`RUNNING` → `QUEUED`)
4. If no attempts remain, it transitions to `FAILED`

### Duplicate Submissions

Submitting the same `event_id` again:

- If the previous submission is in a **terminal state** (SUCCESS, FAILED, VALIDATION_FAILED, CANCELLED): the event is reset, input files are replaced atomically, and it is re-queued
- If the previous submission is **RUNNING**: the duplicate is rejected (you cannot replace an actively processing event)
- The response includes `"replaced_previous": true` when a previous submission was replaced

---

## requeststatus.json Schema

The complete schema of the event tracking file:

```json
{
  "event_id": "20240101_120000_fixture",
  "user_id": "operator",
  "status": "SUCCESS",
  "submitted_at": "2024-01-01T12:00:00+00:00",
  "validated_at": "2024-01-01T12:00:01+00:00",
  "queued_at": "2024-01-01T12:00:01+00:00",
  "started_at": "2024-01-01T12:05:00+00:00",
  "completed_at": "2024-01-01T12:06:10+00:00",
  "current_attempt": 1,
  "max_attempts": 3,
  "validation_errors": null,
  "failure_reason": null,
  "published_products_directory": "products/20240101_120000_fixture",
  "attempt_history": [ ... ]
}
```

---

## Related Documentation

- [REST API Reference](rest-api.md) — submission endpoint details
- [Runtime Layout](runtime-layout.md) — where files are stored
- [Troubleshooting](troubleshooting.md) — debugging execution failures
