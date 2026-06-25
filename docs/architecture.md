# Architecture Guide

This guide documents the system architecture, module responsibilities, data flow, and internal design of the ShakeMap Docker service.

For the high-level overview, see the [How It Works](../README.md#how-it-works) section in the README.

---

## System Overview

The ShakeMap Docker service wraps USGS ShakeMap 4 in a managed container that provides:

- A REST API for event submission and health monitoring
- Filesystem-based event queuing with FIFO ordering
- Multi-process-safe claim locking for event processing
- Atomic input staging and output publication
- Structured lifecycle tracking via `requeststatus.json`
- Two-stage deployment with separate infrastructure and data validation

---

## Two-Stage Lifecycle

```
┌─────────────────────────────────────────────┐
│  Stage 1 — Build and Start                  │
│                                             │
│  Docker Build:                              │
│    Install ShakeMap + dependencies          │
│    Copy service code and scripts            │
│                                             │
│  Container Start (entrypoint.sh):           │
│    Create 6 service directories             │
│    Verify directory writability             │
│    Verify ShakeMap CLI (shake --help)       │
│    Start FastAPI service                    │
│                                             │
│  Status: not_ready                          │
│  Events: rejected (HTTP 503)               │
└──────────────────┬──────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────┐
│  Stage 2 — Configure                        │
│  (configure-shakemap.sh)                    │
│                                             │
│    Create ShakeMap profile (sm_profile -c)  │
│    Create data symlink → SERVICE_ROOT/work  │
│    Detect/download VS30 + topo grids       │
│    Patch model.conf + products.conf         │
│    Run 7 readiness probes                   │
│    Write readiness sentinel                 │
│                                             │
│  Status: healthy (or healthy_with_overrides)│
│  Events: accepted                           │
└─────────────────────────────────────────────┘
```

Stage 1 is automatic (runs when the container starts). Stage 2 is operator-initiated (`docker exec`). This separation allows infrastructure validation before committing to ShakeMap configuration, and supports re-configuration without container restart.

---

## Module Map

The service is implemented as a Python package (`shakemap_service/`) with eight modules:

| Module | Responsibility |
|--------|----------------|
| `config.py` | Settings dataclass. Reads all environment variables and provides typed defaults. Single `settings` instance used throughout. |
| `paths.py` | Pure path computation. Returns `pathlib.Path` objects for all service directories, per-event paths, profile paths, and data paths. No side effects. |
| `status.py` | Event lifecycle model. Defines the `EventStatus` enum (9 frozen values), `AttemptRecord` and `RequestStatus` dataclasses, atomic read/write/update helpers, and status transition validation. |
| `submission.py` | Event submission logic. Input file validation (filename-based), atomic staging to `incoming/<event_id>/`, status flow `REGISTERED → VALIDATING → QUEUED` (or `VALIDATION_FAILED`). |
| `queue.py` | Durable queue discovery. Scans `events/*/requeststatus.json` for `QUEUED` events, returns deterministic FIFO-ordered `QueueSnapshot`. Provides `claim_next()` with `fcntl.flock` for multi-process safety. |
| `worker.py` | Worker skeleton. Claims the next queued event, invokes the execution function, handles retry logic and interrupted-event recovery on restart. |
| `runner.py` | ShakeMap execution bridge. Copies files from `incoming/` to the ShakeMap data directory, invokes `shake --force <event_id> <modules>`, publishes products atomically on success, records failure on error. |
| `main.py` | FastAPI application. Defines all REST endpoints (`/healthz`, `/config`, `/config/profiles`, `/events/submit`). Implements the health model with Stage 1/Stage 2 checks, override reporting, and submit gate logic. |

### Dependency Flow

```
config.py           (standalone — reads environment only)
    ↓
paths.py            (depends on config)
    ↓
status.py           (depends on paths)
    ↓
submission.py       (depends on paths, status)
    ↓
queue.py            (depends on paths, status)
    ↓
worker.py           (depends on queue, status)
    ↓
runner.py           (depends on paths, config, status)
    ↓
main.py             (depends on all modules)
```

No circular dependencies. Each module depends only on modules above it in this hierarchy.

---

## Event Processing Flow

```
                   POST /events/submit
                         │
                         ▼
              ┌─── submit_event() ───┐
              │                      │
              │  1. Create record    │
              │     (REGISTERED)     │
              │  2. Validate files   │
              │     (VALIDATING)     │
              │  3. Stage files      │
              │     atomically to    │
              │     incoming/        │
              │  4. Queue event      │
              │     (QUEUED)         │
              └──────────┬───────────┘
                         │
                         ▼
              ┌─── Worker Cycle ─────┐
              │                      │
              │  1. Take queue       │
              │     snapshot         │
              │  2. Claim next event │
              │     (flock + verify) │
              │     (RUNNING)        │
              └──────────┬───────────┘
                         │
                         ▼
              ┌── run_shake_for_event() ──┐
              │                           │
              │  1. Copy incoming/ →      │
              │     work/<event>/current/ │
              │  2. Record execution      │
              │     context               │
              │  3. shake --force          │
              │     <event> <modules>     │
              │  4a. Success: publish     │
              │      products/ atomically │
              │      (SUCCESS)            │
              │  4b. Failure: record      │
              │      reason (FAILED)      │
              └───────────────────────────┘
```

---

## Data Bridge

The "data bridge" is the symlink that connects the service's working directory to ShakeMap's expected directory structure.

ShakeMap expects event data at:
```
<profile>/data/<event_id>/current/
```

The service stores working data at:
```
SERVICE_ROOT/work/<event_id>/current/
```

The configure script creates a symlink:
```
~/shakemap_profiles/<profile>/data → SERVICE_ROOT/work
```

This makes ShakeMap's expected path resolve to the service's working directory without any file duplication.

---

## Queue Design

The queue is filesystem-based with no external database:

- **Source of truth:** `requeststatus.json` files on disk
- **Discovery:** Full filesystem scan of `events/*/` on each cycle
- **Ordering:** Deterministic FIFO by `queued_at`, then `submitted_at`, then `event_id`
- **Snapshots:** Each worker cycle takes an immutable `QueueSnapshot` — new submissions during processing do not affect the current cycle
- **Claim safety:** `fcntl.flock` on the status file prevents concurrent claim of the same event by multiple workers

### Why filesystem-based?

The service is designed for single-container deployment where simplicity and durability matter more than throughput. The filesystem queue:

- Survives container restarts (data on mounted volume)
- Requires no external dependencies (no Redis, no PostgreSQL)
- Is inspectable with standard tools (`cat`, `ls`, `find`)
- Is reconstructable from disk state at any time

---

## Atomic Operations

All state mutations use atomic write patterns:

1. **Status writes:** Write to a temporary file in the same directory, then `os.rename()` to the target path. This ensures readers never see partial JSON.
2. **Input staging:** Write files to a temporary directory, then rename the directory to `incoming/<event_id>/`.
3. **Product publication:** Copy products to a temporary directory under `products/`, then rename to `products/<event_id>/`.

The `os.rename()` operation is atomic on POSIX filesystems when source and target are on the same filesystem. This is why all temporary files are created in the same directory as their target.

---

## Related Documentation

- [Execution Workflow](execution-workflow.md) — detailed event lifecycle
- [Runtime Layout](runtime-layout.md) — directory structure
- [Health and Readiness](health-and-readiness.md) — health model internals
- [Developer Guide](developer-guide.md) — code conventions and testing
