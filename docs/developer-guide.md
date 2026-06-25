# Developer Guide

This guide covers the codebase structure, testing strategy, code conventions, and how to extend the service.

For the quick test commands, see the [Development](../README.md#development) section in the README.

---

## Codebase Structure

```
shakemap-docker/
├── Dockerfile                    Container image definition
├── entrypoint.sh                 Container entrypoint (Stage 1)
├── Makefile                      Convenience targets
├── README.md                     Project documentation
├── docs/                         Detailed documentation
├── scripts/
│   ├── build-shakemap-docker.sh  Build the Docker image
│   ├── start-shakemap-docker.sh  Start the container
│   ├── configure-shakemap.sh     Stage 2 configuration
│   ├── verify-shakemap-deployment.sh  Deployment verification
│   ├── inspect-shakemap-config.sh     Configuration inspection
│   ├── verify-shakemap-build.sh       (Dev) Build checks
│   ├── verify-shakemap-config.sh      (Dev) Config checks
│   └── run-shakemap-ci-tests.sh       (Dev) CI test suite
├── shakemap_service/
│   ├── __init__.py
│   ├── config.py                 Settings (env vars → typed fields)
│   ├── paths.py                  Pure path computation
│   ├── status.py                 Event lifecycle model
│   ├── submission.py             Event submission and staging
│   ├── queue.py                  Filesystem-based queue
│   ├── worker.py                 Worker claim loop
│   ├── runner.py                 ShakeMap execution bridge
│   └── main.py                   FastAPI application
└── tests/
    ├── fixtures/
    │   └── shakemap_event_minimal/
    │       ├── event.xml
    │       ├── event_dat.xml
    │       ├── rupture.json
    │       └── README.md
    ├── test_event_status_records.py       Status model tests
    ├── test_event_submission_staging.py   Submission tests
    ├── test_durable_queue.py             Queue tests
    ├── test_worker_claim_locking.py      Worker tests
    ├── test_shakemap_fixtures.py         Fixture validation
    └── test_execution_bridge.py          Integration tests
```

---

## Module Descriptions

| Module | Lines | Test File | Description |
|--------|-------|-----------|-------------|
| `config.py` | ~30 | — | Reads environment variables into a `Settings` dataclass. Single module-level `settings` instance. All defaults are defined here. |
| `paths.py` | ~240 | — | Pure path computation using `pathlib.Path`. No filesystem side effects (except `list_profiles()` which reads directory entries). All path logic is centralized here. |
| `status.py` | ~680 | `test_event_status_records.py` | Event lifecycle model. `EventStatus` enum with 9 frozen values, `AttemptRecord` and `RequestStatus` dataclasses, atomic JSON read/write, status transition validation with allowed-transition matrix. |
| `submission.py` | ~330 | `test_event_submission_staging.py` | Submission handling. Input file validation (filename-based), atomic staging to `incoming/`, duplicate submission handling, status flow REGISTERED → VALIDATING → QUEUED (or VALIDATION_FAILED). |
| `queue.py` | ~400 | `test_durable_queue.py` | Filesystem queue. Scans `events/*/requeststatus.json` for QUEUED events, returns deterministic FIFO-ordered `QueueSnapshot`. Claim locking via `fcntl.flock`. |
| `worker.py` | ~350 | `test_worker_claim_locking.py` | Worker skeleton. Claims next queued event, invokes execution function, handles retry (max 3 attempts), recovers interrupted events on restart. |
| `runner.py` | ~280 | `test_execution_bridge.py` | ShakeMap CLI bridge. Copies files from `incoming/` to `work/<event>/current/`, invokes `shake --force`, publishes products atomically, records execution context. |
| `main.py` | ~400 | `test_execution_bridge.py` | FastAPI app. `/healthz`, `/config`, `/config/profiles`, `/events/submit`. Two-stage health model, submit gate (503 when not ready), override reporting. |

---

## Testing

### Unit Tests (Host-Side)

Unit tests run on the host machine without Docker. They use temporary directories to simulate the service's filesystem layout.

```bash
# Run individual test files
python tests/test_event_status_records.py
python tests/test_event_submission_staging.py
python tests/test_durable_queue.py
python tests/test_worker_claim_locking.py
python tests/test_shakemap_fixtures.py

# Run all at once
python -m pytest tests/ -v
```

Each test file creates its own temporary directory, populates it with the expected directory structure, and cleans up after itself.

### Integration Tests (Docker-Based)

Integration tests build and run a Docker container, then verify behavior:

```bash
# Full CI suite (build → start → stage 1 verify → configure → stage 2 verify → cleanup)
./scripts/run-shakemap-ci-tests.sh

# Execution bridge tests (build, start, test API, run ShakeMap)
python tests/test_execution_bridge.py
```

The `test_execution_bridge.py` tests require Docker and build a fresh image for testing.

### Test Conventions

- Each test file is self-contained (imports the module under test, creates fixtures, cleans up)
- Test files use `unittest.TestCase` with the standard `setUp`/`tearDown` pattern
- Temporary directories are created via `tempfile.mkdtemp()` and removed in `tearDown`
- Tests that modify `settings` restore the original values in `tearDown`
- All assertions include descriptive error messages

---

## Code Conventions

### Atomic Writes

All state mutations use the write-to-temp-then-rename pattern:

```python
# Write status atomically
with tempfile.NamedTemporaryFile(
    mode='w', dir=target_dir, suffix='.tmp', delete=False
) as f:
    json.dump(data, f, indent=2)
    tmpname = f.name
os.rename(tmpname, target_path)
```

Never write directly to `requeststatus.json`. Always use the helpers in `status.py`.

### Status Transitions

Status transitions are validated against an allowed-transition matrix:

```python
_ALLOWED_TRANSITIONS = {
    EventStatus.VALIDATING: frozenset({EventStatus.REGISTERED}),
    EventStatus.QUEUED: frozenset({EventStatus.VALIDATING}),
    EventStatus.RUNNING: frozenset({EventStatus.QUEUED}),
    # ...
}
```

Invalid transitions raise `ValueError`. Never set `record.status` directly — use the transition helpers (`transition_to_queued()`, `transition_to_failed()`, etc.).

### Path Functions

All filesystem paths are computed by `paths.py`. Never construct paths manually:

```python
# Correct
from shakemap_service import paths
status_path = paths.event_status_file(event_id)

# Incorrect
status_path = Path(settings.service_root) / "events" / event_id / ".shakemap-service" / "requeststatus.json"
```

---

## Extending the Service

### Adding a New Environment Variable

1. Add the field to `Settings` in `config.py` with an `os.getenv()` default
2. Use `settings.<field_name>` in the module that needs it
3. Document in `docs/configuration.md` and the README configuration table

### Adding a New REST Endpoint

1. Define the route in `main.py` with the appropriate HTTP method decorator
2. Add request/response schemas if needed
3. Update `docs/rest-api.md` with the full schema
4. Add the endpoint to the README API table

### Adding a New Event Status

Event statuses are **frozen** — the 9 values in `EventStatus` are part of the contract and should not change without a major version bump. If you need a new status:

1. Add the value to the `EventStatus` enum in `status.py`
2. Update `TERMINAL_STATUSES` if the new status is terminal
3. Add allowed transitions to `_ALLOWED_TRANSITIONS`
4. Create a transition helper function
5. Update `docs/execution-workflow.md`

### Adding a New Service Directory

1. Add a path function in `paths.py`
2. Add the directory to `all_service_dirs()` in `paths.py`
3. The entrypoint will automatically create it (it iterates `events incoming work products archive logs`)
4. Update `docs/runtime-layout.md`

---

## Dependencies

The service depends on:

- **Python 3.10+** (installed in the Docker image)
- **FastAPI** — REST API framework
- **uvicorn** — ASGI server
- **ShakeMap 4** — USGS earthquake ground motion software (installed from source during Docker build)

Python dependencies are installed via `pip` during the Docker build. There is no `requirements.txt` for the service itself — dependencies are specified directly in the Dockerfile.

---

## Related Documentation

- [Architecture Guide](architecture.md) — module dependencies and data flow
- [Execution Workflow](execution-workflow.md) — event processing internals
- [REST API Reference](rest-api.md) — endpoint documentation
