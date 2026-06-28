#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Event Submission and Staging -- verification tests.

Standalone verification script (no pytest dependency).
Run from shakemap-docker/ with:

    python tests/test_event_submission_staging.py

Verification coverage:
    1.  Valid submission stages files correctly.
    2.  incoming/<event_id>/ contains staged files.
    3.  requeststatus.json is under events/<event_id>/.shakemap-service/.
    4.  Successful validation transitions to QUEUED.
    5.  Missing event.xml transitions to VALIDATION_FAILED.
    6.  Missing station file transitions to VALIDATION_FAILED.
    7.  Duplicate valid submission replaces authoritative incoming files atomically.
    8.  No run_id anywhere.
    9.  No worker/queue/execution/product publication added.
    10. runner.py unchanged (no import of submission).
    11. requeststatus.json never written under incoming/.
    12. Atomic staging integrity.
    13. Empty event_id/user_id rejected.
    14. Accepted station filename variants.
    15. REST endpoint structure in main.py.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# ── Bootstrap ────────────────────────────────────────────────────
# Ensure the shakemap_service package is importable.
_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root))

# Override SERVICE_ROOT before importing anything that reads settings.
_test_root = Path(tempfile.mkdtemp(prefix="phase03_test_"))
os.environ["SERVICE_ROOT"] = str(_test_root)
os.environ["RUNTIME_ROOT"] = str(_test_root.parent)

# Create required directories
for d in ("events", "incoming", "work", "products", "archive", "logs"):
    (_test_root / d).mkdir(parents=True, exist_ok=True)

# Create Stage 2 sentinel so the submit gate in main.py allows submissions
# (Two-stage refactor gates /events/submit behind Stage 2 readiness)
_sentinel_dir = Path.home() / ".shakemap"
_sentinel_dir.mkdir(parents=True, exist_ok=True)
_sentinel_file = _sentinel_dir / ".shakemap_readiness_status"
_sentinel_file.write_text("ready\n")


# ── Imports (after env override) ─────────────────────────────────
from shakemap_service import paths
from shakemap_service.status import (
    EventStatus,
    read_status,
    scan_event_records,
)
from shakemap_service.submission import (
    ALL_ACCEPTED_FILENAMES,
    ACCEPTED_STATION_FILENAMES,
    REQUIRED_EVENT_FILE,
    SubmissionResult,
    submit_event,
    validate_inputs,
)


# ── Test helpers ─────────────────────────────────────────────────

_pass_count = 0
_fail_count = 0


def _check(description: str, condition: bool) -> None:
    global _pass_count, _fail_count
    if condition:
        _pass_count += 1
        print(f"  [PASS] {description}")
    else:
        _fail_count += 1
        print(f"  [FAIL] {description}")


def _cleanup_event(event_id: str) -> None:
    """Remove all traces of an event for test isolation."""
    for d in (
        paths.event_events_dir(event_id),
        paths.event_incoming_dir(event_id),
    ):
        if d.exists():
            shutil.rmtree(d)


def _make_files(**kwargs: bytes) -> dict[str, bytes]:
    """Helper to build file payloads."""
    return kwargs


# ── Sample data ──────────────────────────────────────────────────

SAMPLE_EVENT_XML = b'<?xml version="1.0"?><earthquake id="test001" />'
SAMPLE_STATION_JSON = b'{"type": "FeatureCollection", "features": []}'
SAMPLE_STATION_XML = b'<?xml version="1.0"?><stationlist />'
SAMPLE_EVENT_DAT = b'<?xml version="1.0"?><event_dat />'
SAMPLE_RUPTURE = b'{"type": "Feature", "geometry": {}}'


# ==================================================================
# Test 1: Valid submission stages files correctly
# ==================================================================
print("\n--- Test 1: Valid submission stages files correctly ---")
_cleanup_event("test_valid_001")

result = submit_event(
    event_id="test_valid_001",
    user_id="tester",
    files={
        "event.xml": SAMPLE_EVENT_XML,
        "stationlist.json": SAMPLE_STATION_JSON,
    },
)

_check("Result is SubmissionResult", isinstance(result, SubmissionResult))
_check("event_id matches", result.event_id == "test_valid_001")
_check("Status is QUEUED", result.status == "QUEUED")
_check("status_path is correct",
       result.status_path == ".service/events/test_valid_001/requeststatus.json")
_check("replaced_previous is False", result.replaced_previous is False)
_check("No validation errors", result.validation_errors is None)


# ==================================================================
# Test 2: incoming/<event_id>/ contains staged files
# ==================================================================
print("\n--- Test 2: incoming/<event_id>/ contains staged files ---")
incoming = paths.event_incoming_dir("test_valid_001")

_check("incoming/<event_id>/ exists", incoming.is_dir())
_check("event.xml staged", (incoming / "event.xml").is_file())
_check("stationlist.json staged", (incoming / "stationlist.json").is_file())
_check("event.xml content correct",
       (incoming / "event.xml").read_bytes() == SAMPLE_EVENT_XML)
_check("stationlist.json content correct",
       (incoming / "stationlist.json").read_bytes() == SAMPLE_STATION_JSON)
_check("Only expected files in incoming",
       set(f.name for f in incoming.iterdir()) == {"event.xml", "stationlist.json"})


# ==================================================================
# Test 3: requeststatus.json under .service/events/<event_id>/
# ==================================================================
print("\n--- Test 3: requeststatus.json location ---")
status_file = paths.event_status_file("test_valid_001")
_check("Status file exists", status_file.is_file())
_check("Status file name is requeststatus.json",
       status_file.name == "requeststatus.json")
_check("Status file parent is event_id",
       status_file.parent.name == "test_valid_001")
_check("Status file grandparent is events/",
       status_file.parent.parent.name == "events")
_check("Status file great-grandparent is .service/",
       status_file.parent.parent.parent.name == ".service")

# Verify NO requeststatus.json under incoming/
incoming_status = paths.event_incoming_dir("test_valid_001") / "requeststatus.json"
_check("No requeststatus.json under incoming/", not incoming_status.exists())

record = read_status("test_valid_001")
_check("Record is readable", record is not None)
_check("Record event_id correct", record.event_id == "test_valid_001")
_check("Record user_id correct", record.user_id == "tester")
_check("Record status is QUEUED", record.status == "QUEUED")
_check("validated_at is set", record.validated_at is not None)
_check("queued_at is set", record.queued_at is not None)
_check("current_attempt is 0", record.current_attempt == 0)
_check("max_attempts is 3", record.max_attempts == 3)


# ==================================================================
# Test 4: Successful validation transitions to QUEUED
# ==================================================================
print("\n--- Test 4: Successful validation -> QUEUED ---")
_cleanup_event("test_queued_001")

result = submit_event(
    event_id="test_queued_001",
    user_id="tester",
    files={
        "event.xml": SAMPLE_EVENT_XML,
        "stationlist.xml": SAMPLE_STATION_XML,
    },
)
_check("Status is QUEUED", result.status == "QUEUED")
record = read_status("test_queued_001")
_check("Record status is QUEUED", record.status == "QUEUED")
_check("validated_at is set", record.validated_at is not None)
_check("queued_at is set", record.queued_at is not None)
_check("No validation errors in record", record.validation_errors is None)


# ==================================================================
# Test 5: Missing event.xml → VALIDATION_FAILED
# ==================================================================
print("\n--- Test 5: Missing event.xml -> VALIDATION_FAILED ---")
_cleanup_event("test_no_event_xml")

result = submit_event(
    event_id="test_no_event_xml",
    user_id="tester",
    files={
        "stationlist.json": SAMPLE_STATION_JSON,
    },
)
_check("Status is VALIDATION_FAILED", result.status == "VALIDATION_FAILED")
_check("Has validation errors", result.validation_errors is not None)
_check("Error mentions event.xml",
       any("event.xml" in e for e in result.validation_errors))

record = read_status("test_no_event_xml")
_check("Record status is VALIDATION_FAILED", record.status == "VALIDATION_FAILED")
_check("Record validation_errors populated",
       record.validation_errors is not None and len(record.validation_errors) > 0)

# No files should be staged for failed validation
incoming_dir = paths.event_incoming_dir("test_no_event_xml")
_check("No incoming dir for failed validation", not incoming_dir.exists())


# ==================================================================
# Test 6: Missing station file → VALIDATION_FAILED
# ==================================================================
print("\n--- Test 6: Missing station file -> VALIDATION_FAILED ---")
_cleanup_event("test_no_station")

result = submit_event(
    event_id="test_no_station",
    user_id="tester",
    files={
        "event.xml": SAMPLE_EVENT_XML,
    },
)
_check("Status is VALIDATION_FAILED", result.status == "VALIDATION_FAILED")
_check("Has validation errors", result.validation_errors is not None)
_check("Error mentions station data",
       any("station" in e.lower() for e in result.validation_errors))


# ==================================================================
# Test 7: Duplicate valid submission replaces files atomically
# ==================================================================
print("\n--- Test 7: Duplicate submission replaces files atomically ---")
_cleanup_event("test_dup_001")

# First submission
result1 = submit_event(
    event_id="test_dup_001",
    user_id="tester",
    files={
        "event.xml": b"<original/>",
        "stationlist.json": b'{"original": true}',
    },
)
_check("First submission QUEUED", result1.status == "QUEUED")
_check("First submission not replaced", result1.replaced_previous is False)

# Second submission (duplicate)
result2 = submit_event(
    event_id="test_dup_001",
    user_id="tester_v2",
    files={
        "event.xml": b"<updated/>",
        "stationlist.json": b'{"updated": true}',
    },
)
_check("Second submission QUEUED", result2.status == "QUEUED")
_check("Second submission replaced_previous=True",
       result2.replaced_previous is True)

# Verify authoritative incoming files are from second submission
incoming = paths.event_incoming_dir("test_dup_001")
_check("incoming dir exists", incoming.is_dir())
_check("event.xml has updated content",
       (incoming / "event.xml").read_bytes() == b"<updated/>")
_check("stationlist.json has updated content",
       (incoming / "stationlist.json").read_bytes() == b'{"updated": true}')

# Verify status record reflects latest submission
record = read_status("test_dup_001")
_check("user_id updated to latest", record.user_id == "tester_v2")
_check("Status is QUEUED", record.status == "QUEUED")
_check("submitted_at is set", record.submitted_at is not None)

# No extra files from first submission should remain
_check("Only 2 files in incoming",
       len(list(incoming.iterdir())) == 2)


# ==================================================================
# Test 8: No run_id
# ==================================================================
print("\n--- Test 8: No run_id ---")

# Check RequestStatus dataclass
from shakemap_service.status import RequestStatus
fields = {f.name for f in RequestStatus.__dataclass_fields__.values()}
_check("No run_id in RequestStatus fields", "run_id" not in fields)

# Check SubmissionResult dataclass
sub_fields = {f.name for f in SubmissionResult.__dataclass_fields__.values()}
_check("No run_id in SubmissionResult fields", "run_id" not in sub_fields)

# Check a persisted requeststatus.json
status_file = paths.event_status_file("test_valid_001")
data = json.loads(status_file.read_text())
_check("No run_id key in persisted JSON", "run_id" not in data)


# ==================================================================
# Test 9: No worker/queue/execution/product publication
# ==================================================================
print("\n--- Test 9: No worker/queue/execution/product publication ---")

import shakemap_service

svc_dir = Path(shakemap_service.__file__).parent
py_files = sorted(f.name for f in svc_dir.iterdir() if f.suffix == ".py")

# queue.py is expected after Phase 04.
# _check("No queue.py", "queue.py" not in py_files)
# worker.py is expected after Phase 05.
# _check("No worker.py", "worker.py" not in py_files)
_check("No bridge.py", "bridge.py" not in py_files)
_check("No provenance.py", "provenance.py" not in py_files)
_check("No publisher.py", "publisher.py" not in py_files)

expected_files = sorted([
    "__init__.py", "config.py", "main.py", "paths.py",
    "queue.py", "runner.py", "status.py", "submission.py", "worker.py",
])
_check(f"Module listing: {', '.join(py_files)}", py_files == expected_files)

# Check that main.py has no execution/worker endpoints
main_text = (svc_dir / "main.py").read_text()
_check("No @app.post('/events/' + event_id + '/run') pattern",
       "/run" not in main_text)
_check("No queue_worker in main.py", "queue_worker" not in main_text)
_check("No product publication in main.py", "publish" not in main_text.lower())


# ==================================================================
# Test 10: runner.py unchanged (no submission import)
# ==================================================================
print("\n--- Test 10: runner.py unchanged ---")

runner_text = (svc_dir / "runner.py").read_text()
_check("runner.py does not import submission", "submission" not in runner_text)
# After Phase 07, runner.py imports status for execution bridge transitions.
# _check("runner.py does not import status", "from .status" not in runner_text)
_check("runner.py has ShakeError class", "class ShakeError" in runner_text)
_check("runner.py has run_shake function", "def run_shake" in runner_text)


# ==================================================================
# Test 11: No requeststatus.json under incoming/
# ==================================================================
print("\n--- Test 11: No requeststatus.json under incoming/ ---")

incoming_root = paths.incoming_dir()
found_status_in_incoming = False
if incoming_root.exists():
    for root, dirs, files in os.walk(incoming_root):
        if "requeststatus.json" in files:
            found_status_in_incoming = True
            break

_check("No requeststatus.json anywhere under incoming/",
       not found_status_in_incoming)


# ==================================================================
# Test 12: Atomic staging integrity
# ==================================================================
print("\n--- Test 12: Atomic staging integrity ---")
_cleanup_event("test_atomic_001")

# Submit, then verify no temp/staging dirs remain
result = submit_event(
    event_id="test_atomic_001",
    user_id="tester",
    files={
        "event.xml": SAMPLE_EVENT_XML,
        "stationlist.json": SAMPLE_STATION_JSON,
    },
)

_check("Submission succeeded", result.status == "QUEUED")

# Check no temp staging dirs remain under incoming/
incoming_root = paths.incoming_dir()
temp_dirs = [
    d for d in incoming_root.iterdir()
    if d.is_dir() and (d.name.startswith(".") or d.name.endswith(".staging") or d.name.endswith(".old"))
]
_check("No temporary staging dirs remain", len(temp_dirs) == 0)

# Verify final incoming dir is a real directory (not symlink)
incoming = paths.event_incoming_dir("test_atomic_001")
_check("incoming/<event_id> is a real directory", incoming.is_dir() and not incoming.is_symlink())


# ==================================================================
# Test 13: Empty event_id / user_id rejected
# ==================================================================
print("\n--- Test 13: Empty event_id / user_id rejected ---")

try:
    submit_event(event_id="", user_id="tester", files={"event.xml": b"x"})
    _check("Empty event_id raises ValueError", False)
except ValueError:
    _check("Empty event_id raises ValueError", True)

try:
    submit_event(event_id="test", user_id="", files={"event.xml": b"x"})
    _check("Empty user_id raises ValueError", False)
except ValueError:
    _check("Empty user_id raises ValueError", True)

try:
    submit_event(event_id="   ", user_id="tester", files={"event.xml": b"x"})
    _check("Whitespace-only event_id raises ValueError", False)
except ValueError:
    _check("Whitespace-only event_id raises ValueError", True)


# ==================================================================
# Test 14: Accepted station filename variants
# ==================================================================
print("\n--- Test 14: Accepted station filename variants ---")

# stationlist.json
_cleanup_event("test_station_json")
r = submit_event("test_station_json", "tester",
                  {"event.xml": SAMPLE_EVENT_XML, "stationlist.json": SAMPLE_STATION_JSON})
_check("stationlist.json accepted -> QUEUED", r.status == "QUEUED")

# stationlist.xml
_cleanup_event("test_station_xml")
r = submit_event("test_station_xml", "tester",
                  {"event.xml": SAMPLE_EVENT_XML, "stationlist.xml": SAMPLE_STATION_XML})
_check("stationlist.xml accepted -> QUEUED", r.status == "QUEUED")

# event_dat.xml
_cleanup_event("test_event_dat")
r = submit_event("test_event_dat", "tester",
                  {"event.xml": SAMPLE_EVENT_XML, "event_dat.xml": SAMPLE_EVENT_DAT})
_check("event_dat.xml accepted -> QUEUED", r.status == "QUEUED")

# Optional rupture.json alongside required files
_cleanup_event("test_with_rupture")
r = submit_event("test_with_rupture", "tester", {
    "event.xml": SAMPLE_EVENT_XML,
    "stationlist.json": SAMPLE_STATION_JSON,
    "rupture.json": SAMPLE_RUPTURE,
})
_check("rupture.json + required files -> QUEUED", r.status == "QUEUED")
incoming = paths.event_incoming_dir("test_with_rupture")
_check("rupture.json staged", (incoming / "rupture.json").is_file())

# Accepted station filenames set matches contract
_check("ACCEPTED_STATION_FILENAMES has stationlist.json",
       "stationlist.json" in ACCEPTED_STATION_FILENAMES)
_check("ACCEPTED_STATION_FILENAMES has stationlist.xml",
       "stationlist.xml" in ACCEPTED_STATION_FILENAMES)
_check("ACCEPTED_STATION_FILENAMES has event_dat.xml",
       "event_dat.xml" in ACCEPTED_STATION_FILENAMES)
_check("ACCEPTED_STATION_FILENAMES has exactly 3 entries",
       len(ACCEPTED_STATION_FILENAMES) == 3)


# ==================================================================
# Test 15: REST endpoint structure in main.py
# ==================================================================
print("\n--- Test 15: REST endpoint structure ---")

from shakemap_service.main import app as fastapi_app

routes = [r.path for r in fastapi_app.routes if hasattr(r, "path")]
_check("POST /events/submit route exists", "/events/submit" in routes)
_check("GET /healthz route exists", "/healthz" in routes)

# Verify no execution-related routes
_check("No /events/{event_id}/run route",
       not any("/run" in r for r in routes))
_check("No /queue route",
       not any("/queue" in r for r in routes))


# ==================================================================
# Test 16: Duplicate submission on terminal status
# ==================================================================
print("\n--- Test 16: Duplicate submission on terminal status ---")
_cleanup_event("test_terminal_resubmit")

# First submission → QUEUED
r1 = submit_event("test_terminal_resubmit", "tester", {
    "event.xml": SAMPLE_EVENT_XML,
    "stationlist.json": SAMPLE_STATION_JSON,
})
_check("First submission QUEUED", r1.status == "QUEUED")

# Manually mark as VALIDATION_FAILED (simulate terminal state)
from shakemap_service.status import write_status_atomic
record = read_status("test_terminal_resubmit")
record.status = EventStatus.VALIDATION_FAILED.value
record.validation_errors = ["simulated failure"]
write_status_atomic("test_terminal_resubmit", record)

# Resubmit
r2 = submit_event("test_terminal_resubmit", "tester_v2", {
    "event.xml": b"<resubmitted/>",
    "stationlist.json": b'{"resubmitted": true}',
})
_check("Resubmission after terminal -> QUEUED", r2.status == "QUEUED")
_check("Resubmission marked as replaced", r2.replaced_previous is True)

record2 = read_status("test_terminal_resubmit")
_check("Record user_id updated", record2.user_id == "tester_v2")
_check("Record has QUEUED status", record2.status == "QUEUED")


# ==================================================================
# Test 17: validate_inputs function standalone
# ==================================================================
print("\n--- Test 17: validate_inputs standalone ---")

errors = validate_inputs(["event.xml", "stationlist.json"])
_check("Valid input set -> no errors", len(errors) == 0)

errors = validate_inputs(["stationlist.json"])
_check("Missing event.xml -> error", len(errors) == 1)
_check("Error mentions event.xml", "event.xml" in errors[0])

errors = validate_inputs(["event.xml"])
_check("Missing station -> error", len(errors) == 1)
_check("Error mentions station", "station" in errors[0].lower())

errors = validate_inputs([])
_check("Empty input -> two errors", len(errors) == 2)

errors = validate_inputs(["event.xml", "stationlist.json", "rupture.json"])
_check("With optional rupture -> no errors", len(errors) == 0)

# ==================================================================
# Test 18: HTTP 422 for validation failure, 200 for valid submission
# ==================================================================
print("\n--- Test 18: HTTP status codes via TestClient ---")

from fastapi.testclient import TestClient
from shakemap_service.main import app as fastapi_app

client = TestClient(fastapi_app)

# Valid submission → HTTP 200
_cleanup_event("test_http_valid")
resp = client.post(
    "/events/submit",
    data={"event_id": "test_http_valid", "user_id": "tester"},
    files=[
        ("files", ("event.xml", SAMPLE_EVENT_XML, "application/xml")),
        ("files", ("stationlist.json", SAMPLE_STATION_JSON, "application/json")),
    ],
)
_check("Valid submission -> HTTP 200", resp.status_code == 200)
body = resp.json()
_check("Response has event_id", body["event_id"] == "test_http_valid")
_check("Response status is QUEUED", body["status"] == "QUEUED")
_check("Response has status_path", "status_path" in body)
_check("Response validation_errors is None", body["validation_errors"] is None)

# Invalid submission (missing station file) → HTTP 422
_cleanup_event("test_http_invalid")
resp2 = client.post(
    "/events/submit",
    data={"event_id": "test_http_invalid", "user_id": "tester"},
    files=[
        ("files", ("event.xml", SAMPLE_EVENT_XML, "application/xml")),
    ],
)
_check("Missing station -> HTTP 422", resp2.status_code == 422)
body2 = resp2.json()
_check("422 response has event_id", body2["event_id"] == "test_http_invalid")
_check("422 response status is VALIDATION_FAILED", body2["status"] == "VALIDATION_FAILED")
_check("422 response has status_path", "status_path" in body2)
_check("422 response has validation_errors", body2["validation_errors"] is not None)
_check("422 response validation_errors non-empty", len(body2["validation_errors"]) > 0)

# Verify requeststatus.json was created even for failed validation
record_http = read_status("test_http_invalid")
_check("Status record created for 422 response", record_http is not None)
_check("Status record is VALIDATION_FAILED", record_http.status == "VALIDATION_FAILED")


# ==================================================================
# Cleanup and summary
# ==================================================================
print("\n--- Cleanup ---")
shutil.rmtree(str(_test_root), ignore_errors=True)

print(f"\n{'='*60}")
print(f"Event Submission Staging: {_pass_count} passed, {_fail_count} failed")
print(f"{'='*60}")

if _fail_count > 0:
    sys.exit(1)
