#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Event Record Foundation -- verification tests.

Demonstrates and verifies:

1.  Creating a sample event status record
2.  Reading it back
3.  Updating status atomically
4.  Status transitions through the full lifecycle
5.  Scanning existing records
6.  Malformed/missing status file handling
7.  No run_id field exists
8.  No queue/worker/submission API added
9.  No requeststatus.json under incoming/
10. Attempt history structure (without actual execution)

Runs against a temporary directory to avoid polluting the real runtime.

Usage:
    cd shakemap-docker
    source ../.venv/bin/activate
    python tests/test_event_status_records.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
from pathlib import Path

# ── Bootstrap: ensure shakemap_service is importable ──────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Override SERVICE_ROOT before importing modules so paths resolve
# to a temp directory instead of the real runtime.
_tmpdir = tempfile.mkdtemp(prefix="shakemap_phase02_test_")
os.environ["SERVICE_ROOT"] = os.path.join(_tmpdir, "shakemap")
os.environ["RUNTIME_ROOT"] = _tmpdir

# Now import — config reads env at import time.
from shakemap_service.config import Settings
# Reinstantiate settings with updated env vars.
import shakemap_service.config as _cfg
_cfg.settings = Settings()

from shakemap_service import paths
from shakemap_service.status import (
    AttemptRecord,
    EventStatus,
    RequestStatus,
    TERMINAL_STATUSES,
    create_event_record,
    read_status,
    scan_event_records,
    transition_to_archived,
    transition_to_cancelled,
    transition_to_failed,
    transition_to_queued,
    transition_to_running,
    transition_to_success,
    transition_to_validating,
    transition_to_validation_failed,
    update_status,
    write_status_atomic,
)

# ── Helpers ───────────────────────────────────────────────────────

_pass_count = 0
_fail_count = 0


def _check(label: str, condition: bool, detail: str = "") -> None:
    global _pass_count, _fail_count
    status = "PASS" if condition else "FAIL"
    if not condition:
        _fail_count += 1
    else:
        _pass_count += 1
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{status}] {label}{suffix}")


def _section(title: str) -> None:
    print(f"\n{'-' * 60}")
    print(f"  {title}")
    print(f"{'-' * 60}")


# ── Setup ─────────────────────────────────────────────────────────

def setup() -> None:
    """Create the service directory structure in the temp dir."""
    for d in paths.all_service_dirs():
        d.mkdir(parents=True, exist_ok=True)
    print(f"Test runtime root: {_tmpdir}")
    print(f"Service root:      {paths.service_root()}")


# ── Test 1: Create event record ──────────────────────────────────

def test_create_event_record() -> None:
    _section("1. Create event record")

    record = create_event_record("ev001", "pyfinder", max_attempts=3)

    _check("Record returned", record is not None)
    _check("event_id is 'ev001'", record.event_id == "ev001")
    _check("user_id is 'pyfinder'", record.user_id == "pyfinder")
    _check("status is REGISTERED", record.status == "REGISTERED")
    _check("submitted_at is set", record.submitted_at is not None and len(record.submitted_at) > 0)
    _check("current_attempt is 0", record.current_attempt == 0)
    _check("max_attempts is 3", record.max_attempts == 3)
    _check("attempt_history is empty list", record.attempt_history == [])
    _check("validation_errors is None", record.validation_errors is None)
    _check("failure_reason is None", record.failure_reason is None)

    # Verify directory was created.
    event_dir = paths.event_events_dir("ev001")
    _check("Event dir created", event_dir.is_dir(), str(event_dir))

    # Verify file exists.
    status_file = paths.event_status_file("ev001")
    _check("requeststatus.json exists", status_file.is_file(), str(status_file))

    # Verify duplicate creation raises.
    try:
        create_event_record("ev001", "pyfinder")
        _check("Duplicate creation raises", False, "No exception raised")
    except FileExistsError:
        _check("Duplicate creation raises FileExistsError", True)


# ── Test 2: Read event record ────────────────────────────────────

def test_read_event_record() -> None:
    _section("2. Read event record back")

    record = read_status("ev001")
    _check("Record found", record is not None)
    _check("event_id matches", record.event_id == "ev001")
    _check("user_id matches", record.user_id == "pyfinder")
    _check("status is REGISTERED", record.status == "REGISTERED")
    _check("attempt_history is list", isinstance(record.attempt_history, list))

    # Read non-existent event.
    missing = read_status("nonexistent_event")
    _check("Missing event returns None", missing is None)


# ── Test 3: Update status atomically ─────────────────────────────

def test_update_status() -> None:
    _section("3. Update status atomically")

    # Create a fresh event for this test.
    create_event_record("ev002", "tester")

    updated = update_status("ev002", max_attempts=5)
    _check("max_attempts updated to 5", updated.max_attempts == 5)

    # Read back to verify persistence.
    re_read = read_status("ev002")
    _check("Persisted max_attempts is 5", re_read.max_attempts == 5)

    # Test unknown field.
    try:
        update_status("ev002", bogus_field="value")
        _check("Unknown field raises TypeError", False)
    except TypeError:
        _check("Unknown field raises TypeError", True)

    # Test update on missing event.
    try:
        update_status("missing_event", status="QUEUED")
        _check("Update missing event raises", False)
    except FileNotFoundError:
        _check("Update missing event raises FileNotFoundError", True)


# ── Test 4: Status transitions — full lifecycle ──────────────────

def test_status_transitions() -> None:
    _section("4. Status transitions -- happy path")

    create_event_record("ev_lifecycle", "pyfinder")

    # REGISTERED → VALIDATING
    r = transition_to_validating("ev_lifecycle")
    _check("-> VALIDATING", r.status == "VALIDATING")

    # VALIDATING → QUEUED
    r = transition_to_queued("ev_lifecycle")
    _check("-> QUEUED", r.status == "QUEUED")
    _check("validated_at set", r.validated_at is not None)
    _check("queued_at set", r.queued_at is not None)

    # QUEUED → RUNNING
    r = transition_to_running("ev_lifecycle")
    _check("-> RUNNING", r.status == "RUNNING")
    _check("current_attempt is 1", r.current_attempt == 1)
    _check("attempt_history has 1 entry", len(r.attempt_history) == 1)
    _check("attempt status is RUNNING", r.attempt_history[0].status == "RUNNING")

    # RUNNING → SUCCESS
    r = transition_to_success("ev_lifecycle", products_dir="products/ev_lifecycle")
    _check("-> SUCCESS", r.status == "SUCCESS")
    _check("completed_at set", r.completed_at is not None)
    _check("published_products_directory set",
           r.published_products_directory == "products/ev_lifecycle")
    _check("attempt completed", r.attempt_history[0].completed_at is not None)
    _check("attempt status is SUCCESS", r.attempt_history[0].status == "SUCCESS")
    _check("duration_seconds >= 0",
           r.attempt_history[0].duration_seconds is not None and
           r.attempt_history[0].duration_seconds >= 0)

    # SUCCESS → ARCHIVED
    r = transition_to_archived("ev_lifecycle")
    _check("-> ARCHIVED", r.status == "ARCHIVED")

    _section("4b. Status transitions -- validation failure path")

    create_event_record("ev_valfail", "pyfinder")
    transition_to_validating("ev_valfail")
    r = transition_to_validation_failed("ev_valfail", ["Missing event.xml", "Bad format"])
    _check("-> VALIDATION_FAILED", r.status == "VALIDATION_FAILED")
    _check("validation_errors has 2 entries", len(r.validation_errors) == 2)
    _check("validated_at set", r.validated_at is not None)

    _section("4c. Status transitions -- failure path")

    create_event_record("ev_fail", "pyfinder")
    transition_to_validating("ev_fail")
    transition_to_queued("ev_fail")
    transition_to_running("ev_fail")
    r = transition_to_failed("ev_fail", "ShakeMap exit code 1")
    _check("-> FAILED", r.status == "FAILED")
    _check("failure_reason set", r.failure_reason == "ShakeMap exit code 1")
    _check("attempt failure_reason set",
           r.attempt_history[0].failure_reason == "ShakeMap exit code 1")

    _section("4d. Status transitions -- cancellation path")

    create_event_record("ev_cancel", "pyfinder")
    transition_to_validating("ev_cancel")
    transition_to_queued("ev_cancel")
    r = transition_to_cancelled("ev_cancel")
    _check("-> CANCELLED from QUEUED", r.status == "CANCELLED")
    _check("completed_at set", r.completed_at is not None)


# ── Test 5: Invalid transitions ──────────────────────────────────

def test_invalid_transitions() -> None:
    _section("5. Invalid transitions raise ValueError")

    create_event_record("ev_invalid", "pyfinder")

    # REGISTERED → QUEUED (must go through VALIDATING first)
    try:
        transition_to_queued("ev_invalid")
        _check("REGISTERED -> QUEUED raises", False)
    except ValueError as e:
        _check("REGISTERED -> QUEUED raises ValueError", True, str(e))

    # REGISTERED → SUCCESS (invalid)
    try:
        transition_to_success("ev_invalid")
        _check("REGISTERED -> SUCCESS raises", False)
    except ValueError as e:
        _check("REGISTERED -> SUCCESS raises ValueError", True, str(e))

    # REGISTERED → FAILED (invalid)
    try:
        transition_to_failed("ev_invalid", "reason")
        _check("REGISTERED -> FAILED raises", False)
    except ValueError as e:
        _check("REGISTERED -> FAILED raises ValueError", True, str(e))


# ── Test 6: Scan existing records ────────────────────────────────

def test_scan_event_records() -> None:
    _section("6. Scan existing event records")

    records = scan_event_records()
    event_ids = {r.event_id for r in records}

    _check("Scan returns multiple records", len(records) >= 4)
    _check("Contains ev001", "ev001" in event_ids)
    _check("Contains ev_lifecycle", "ev_lifecycle" in event_ids)
    _check("Contains ev_valfail", "ev_valfail" in event_ids)
    _check("Contains ev_fail", "ev_fail" in event_ids)

    # Verify statuses are correct.
    by_id = {r.event_id: r for r in records}
    _check("ev001 is REGISTERED", by_id["ev001"].status == "REGISTERED")
    _check("ev_lifecycle is ARCHIVED", by_id["ev_lifecycle"].status == "ARCHIVED")
    _check("ev_valfail is VALIDATION_FAILED",
           by_id["ev_valfail"].status == "VALIDATION_FAILED")
    _check("ev_fail is FAILED", by_id["ev_fail"].status == "FAILED")


# ── Test 7: Malformed / missing status file handling ─────────────

def test_malformed_handling() -> None:
    _section("7. Malformed / missing status file handling")

    # Create a malformed requeststatus.json.
    malformed_dir = paths.event_events_dir("ev_malformed")
    malformed_dir.mkdir(parents=True, exist_ok=True)
    malformed_file = malformed_dir / "requeststatus.json"
    malformed_file.write_text("this is not json {{{", encoding="utf-8")

    try:
        read_status("ev_malformed")
        _check("Malformed JSON raises ValueError", False)
    except ValueError as e:
        _check("Malformed JSON raises ValueError", True, str(e)[:60])

    # Create a file missing required fields.
    incomplete_dir = paths.event_events_dir("ev_incomplete")
    incomplete_dir.mkdir(parents=True, exist_ok=True)
    incomplete_file = incomplete_dir / "requeststatus.json"
    incomplete_file.write_text('{"event_id": "ev_incomplete"}', encoding="utf-8")

    try:
        read_status("ev_incomplete")
        _check("Incomplete record raises ValueError", False)
    except ValueError as e:
        _check("Incomplete record raises ValueError", True, str(e)[:60])

    # Non-dict JSON.
    nondict_dir = paths.event_events_dir("ev_nondict")
    nondict_dir.mkdir(parents=True, exist_ok=True)
    nondict_file = nondict_dir / "requeststatus.json"
    nondict_file.write_text('["not", "a", "dict"]', encoding="utf-8")

    try:
        read_status("ev_nondict")
        _check("Non-dict JSON raises ValueError", False)
    except ValueError as e:
        _check("Non-dict JSON raises ValueError", True, str(e)[:60])

    # Scan should skip malformed records gracefully.
    records = scan_event_records()
    malformed_ids = {r.event_id for r in records}
    _check("Scan skips ev_malformed", "ev_malformed" not in malformed_ids)
    _check("Scan skips ev_incomplete", "ev_incomplete" not in malformed_ids)
    _check("Scan skips ev_nondict", "ev_nondict" not in malformed_ids)
    _check("Scan still returns valid records", len(records) >= 4)


# ── Test 8: No run_id ────────────────────────────────────────────

def test_no_run_id() -> None:
    _section("8. No run_id exists")

    # Check the RequestStatus dataclass fields.
    field_names = {f.name for f in RequestStatus.__dataclass_fields__.values()}
    _check("No 'run_id' in RequestStatus fields", "run_id" not in field_names)

    # Check serialised JSON on disk.
    status_file = paths.event_status_file("ev001")
    data = json.loads(status_file.read_text(encoding="utf-8"))
    _check("No 'run_id' key in JSON", "run_id" not in data)

    # Check AttemptRecord fields.
    attempt_fields = {f.name for f in AttemptRecord.__dataclass_fields__.values()}
    _check("No 'run_id' in AttemptRecord fields", "run_id" not in attempt_fields)


# ── Test 9: No queue/worker/submission API ────────────────────────

def test_no_queue_worker_api() -> None:
    _section("9. No queue/worker/execution modules added")

    svc_dir = Path(__file__).resolve().parent.parent / "shakemap_service"

    # queue.py is expected after Phase 04.
    # _check("No queue.py", not (svc_dir / "queue.py").exists())
    # worker.py is expected after Phase 05.
    # _check("No worker.py", not (svc_dir / "worker.py").exists())
    # submission.py is expected after Phase 03.
    _check("No bridge.py", not (svc_dir / "bridge.py").exists())
    _check("No provenance.py", not (svc_dir / "provenance.py").exists())
    _check("No publisher.py", not (svc_dir / "publisher.py").exists())

    # Check runner.py — after Phase 07, runner.py imports status for
    # execution bridge transitions (RUNNING -> SUCCESS/FAILED).
    # runner_text = (svc_dir / "runner.py").read_text(encoding="utf-8")
    # _check("runner.py does not import status", "from .status" not in runner_text)
    # _check("runner.py does not reference RequestStatus", "RequestStatus" not in runner_text)

    # @app.post for /events/submit is expected after Phase 03.
    # Verify no execution/mutation endpoints beyond submission exist.
    main_text = (svc_dir / "main.py").read_text(encoding="utf-8")
    _check("No @app.put in main.py", "@app.put" not in main_text)
    _check("No @app.delete in main.py", "@app.delete" not in main_text)


# ── Test 10: No requeststatus.json under incoming/ ────────────────

def test_no_status_under_incoming() -> None:
    _section("10. No requeststatus.json under incoming/")

    incoming = paths.incoming_dir()
    incoming.mkdir(parents=True, exist_ok=True)

    # Walk incoming/ and verify no requeststatus.json.
    found = []
    for root, dirs, files in os.walk(str(incoming)):
        for f in files:
            if f == "requeststatus.json":
                found.append(os.path.join(root, f))

    _check("No requeststatus.json under incoming/", len(found) == 0,
           f"found: {found}" if found else "clean")


# ── Test 11: Attempt history structure ────────────────────────────

def test_attempt_history() -> None:
    _section("11. Attempt history structure (no actual execution)")

    create_event_record("ev_attempts", "pyfinder", max_attempts=3)
    transition_to_validating("ev_attempts")
    transition_to_queued("ev_attempts")

    # First attempt — transition to RUNNING.
    r = transition_to_running("ev_attempts")
    _check("After 1st RUNNING: current_attempt=1", r.current_attempt == 1)
    _check("attempt_history length=1", len(r.attempt_history) == 1)

    a1 = r.attempt_history[0]
    _check("attempt_number is 1", a1.attempt_number == 1)
    _check("attempt started_at set", a1.started_at is not None)
    _check("attempt status is RUNNING", a1.status == "RUNNING")
    _check("attempt completed_at is None", a1.completed_at is None)
    _check("attempt duration_seconds is None", a1.duration_seconds is None)

    # Complete as FAILED.
    r = transition_to_failed("ev_attempts", "exit code 2")
    _check("After FAILED: status=FAILED", r.status == "FAILED")
    _check("attempt completed_at set", r.attempt_history[0].completed_at is not None)
    _check("attempt status=FAILED", r.attempt_history[0].status == "FAILED")
    _check("attempt failure_reason set",
           r.attempt_history[0].failure_reason == "exit code 2")
    _check("attempt duration_seconds >= 0",
           r.attempt_history[0].duration_seconds is not None and
           r.attempt_history[0].duration_seconds >= 0)

    # Verify all fields in attempt record are contract-compliant.
    attempt_field_names = sorted(f.name for f in AttemptRecord.__dataclass_fields__.values())
    expected_fields = sorted([
        "attempt_number", "started_at", "completed_at",
        "status", "failure_reason", "duration_seconds",
        "execution_context",
    ])
    _check("AttemptRecord has exactly contract fields",
           attempt_field_names == expected_fields,
           f"got: {attempt_field_names}")


# ── Test 12: EventStatus enum ────────────────────────────────────

def test_event_status_enum() -> None:
    _section("12. EventStatus enum covers all 9 FROZEN values")

    expected = {
        "REGISTERED", "VALIDATING", "VALIDATION_FAILED",
        "QUEUED", "RUNNING", "SUCCESS",
        "FAILED", "CANCELLED", "ARCHIVED",
    }
    actual = {s.value for s in EventStatus}
    _check("All 9 statuses present", actual == expected,
           f"missing: {expected - actual}, extra: {actual - expected}")
    _check("EventStatus is str subclass", isinstance(EventStatus.REGISTERED, str))

    _check("TERMINAL_STATUSES correct", TERMINAL_STATUSES == frozenset({
        EventStatus.VALIDATION_FAILED,
        EventStatus.SUCCESS,
        EventStatus.FAILED,
        EventStatus.CANCELLED,
        EventStatus.ARCHIVED,
    }))


# ── Test 13: Atomic write integrity ──────────────────────────────

def test_atomic_write_integrity() -> None:
    _section("13. Atomic write integrity")

    # Verify the file on disk is valid JSON after every write.
    create_event_record("ev_atomic", "pyfinder")

    # Read raw file content.
    raw = paths.event_status_file("ev_atomic").read_text(encoding="utf-8")
    data = json.loads(raw)
    _check("File is valid JSON", isinstance(data, dict))
    _check("JSON is pretty-printed", "\n" in raw)

    # Update and verify.
    update_status("ev_atomic", max_attempts=10)
    raw2 = paths.event_status_file("ev_atomic").read_text(encoding="utf-8")
    data2 = json.loads(raw2)
    _check("Updated file is valid JSON", isinstance(data2, dict))
    _check("Updated max_attempts=10", data2["max_attempts"] == 10)

    # Verify no temp files left behind.
    svc_dir = paths.event_events_dir("ev_atomic")
    tmp_files = [f for f in svc_dir.iterdir() if f.suffix == ".tmp"]
    _check("No temp files left behind", len(tmp_files) == 0,
           f"found: {tmp_files}" if tmp_files else "clean")


# ── Test 14: paths.py new function ────────────────────────────────

def test_paths_provenance_file() -> None:
    _section("14. paths.event_provenance_file()")

    p = paths.event_provenance_file("ev001")
    _check("Returns Path", isinstance(p, Path))
    _check("Ends with provenance.json", p.name == "provenance.json")
    _check("Parent is event dir under .service/events/",
           p.parent == paths.event_events_dir("ev001"))


# ── Main ──────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 60)
    print("  Event Record Foundation -- Verification")
    print("=" * 60)

    setup()

    test_create_event_record()
    test_read_event_record()
    test_update_status()
    test_status_transitions()
    test_invalid_transitions()
    test_scan_event_records()
    test_malformed_handling()
    test_no_run_id()
    test_no_queue_worker_api()
    test_no_status_under_incoming()
    test_attempt_history()
    test_event_status_enum()
    test_atomic_write_integrity()
    test_paths_provenance_file()

    print(f"\n{'=' * 60}")
    print(f"  Results: {_pass_count} passed, {_fail_count} failed")
    print(f"{'=' * 60}")

    # Cleanup.
    import shutil
    shutil.rmtree(_tmpdir, ignore_errors=True)

    return 0 if _fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
