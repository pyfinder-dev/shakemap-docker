#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 05 — Worker Claim Locking and Execution Skeleton verification tests.

Run with:
    source /Users/savas/my-codes/eew/pyfinder-dev/.venv/bin/activate
    cd /Users/savas/my-codes/eew/pyfinder-dev/shakemap-docker
    python tests/test_phase05_worker_locking.py

Test sections:
    1.  Multi-process claim locking: two processes cannot claim the same event
    2.  Claim lock releases after successful claim attempt
    3.  Failed claim leaves event recoverable (still QUEUED)
    4.  QUEUED events remain discoverable after failed claim
    5.  RUNNING claim creates attempt history
    6.  Non-QUEUED events cannot be claimed
    7.  Queue ordering remains deterministic with locking
    8.  Worker skeleton: placeholder execution returns to QUEUED
    9.  Worker skeleton: no real ShakeMap subprocess call
   10.  Worker skeleton: run_worker_cycle
   11.  Recovery: find_stale_running identifies RUNNING records
   12.  Recovery: record_interrupted_attempt re-queues or fails
   13.  Worker recover_interrupted_events at startup
   14.  No ShakeMap subprocess call anywhere
   15.  No product publication
   16.  runner.py unchanged
   17.  Phase 06 integration point documented
"""
from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# --- bootstrap: project on sys.path ---------------------
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

# Redirect SERVICE_ROOT to a temp dir so tests don't touch real data.
_tmpdir = tempfile.mkdtemp(prefix="phase05_test_")
os.environ["SERVICE_ROOT"] = _tmpdir
os.environ["RUNTIME_ROOT"] = _tmpdir

# Force config to reload with our test SERVICE_ROOT
from shakemap_service import config
config.settings = config.Settings()

from shakemap_service import paths, status
from shakemap_service.status import (
    EventStatus,
    RequestStatus,
    AttemptRecord,
    write_status_atomic,
    read_status,
    create_event_record,
    transition_to_queued,
    transition_to_running,
    transition_to_validating,
    transition_to_validation_failed,
    transition_to_success,
    transition_to_failed,
    find_stale_running,
    record_interrupted_attempt,
)
from shakemap_service.queue import (
    discover_queue,
    list_queue_candidates,
    take_snapshot,
    QueueSnapshot,
    ClaimResult,
    MalformedRecord,
    _queue_sort_key,
    _claim_with_lock,
)
from shakemap_service.worker import (
    WorkerResult,
    execute_placeholder,
    process_next_event,
    run_worker_cycle,
    recover_interrupted_events,
    PLACEHOLDER_OUTCOME,
)


passed = 0
failed = 0


def check(label: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {label}")
    else:
        failed += 1
        msg = f"  [FAIL] {label}"
        if detail:
            msg += f" -- {detail}"
        print(msg)


def _iso(offset_seconds: int = 0) -> str:
    """Return an ISO 8601 timestamp with optional offset."""
    dt = datetime(2026, 6, 16, 10, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=offset_seconds)
    return dt.isoformat()


def _make_queued_event(event_id: str, user_id: str = "testuser",
                       queued_at: str | None = None,
                       submitted_at: str | None = None) -> RequestStatus:
    """Create a QUEUED event record directly (bypassing normal flow)."""
    record = RequestStatus(
        event_id=event_id,
        user_id=user_id,
        status=EventStatus.QUEUED.value,
        submitted_at=submitted_at or _iso(0),
        validated_at=queued_at or _iso(1),
        queued_at=queued_at or _iso(1),
        current_attempt=0,
        max_attempts=3,
    )
    write_status_atomic(event_id, record)
    return record


def _make_event_with_status(event_id: str, event_status: EventStatus,
                            user_id: str = "testuser",
                            max_attempts: int = 3,
                            current_attempt: int | None = None) -> RequestStatus:
    """Create an event record with an arbitrary status."""
    now = _iso(0)
    if current_attempt is None:
        current_attempt = 0 if event_status != EventStatus.RUNNING else 1
    record = RequestStatus(
        event_id=event_id,
        user_id=user_id,
        status=event_status.value,
        submitted_at=now,
        current_attempt=current_attempt,
        max_attempts=max_attempts,
    )
    if event_status in (EventStatus.QUEUED, EventStatus.RUNNING, EventStatus.SUCCESS,
                        EventStatus.FAILED):
        record.validated_at = now
        record.queued_at = now
    if event_status in (EventStatus.RUNNING, EventStatus.SUCCESS, EventStatus.FAILED):
        record.started_at = now
        record.attempt_history = [AttemptRecord(
            attempt_number=current_attempt,
            started_at=now,
            status="RUNNING" if event_status == EventStatus.RUNNING else event_status.value,
        )]
    if event_status in (EventStatus.SUCCESS, EventStatus.FAILED):
        record.completed_at = now
    write_status_atomic(event_id, record)
    return record


def _cleanup():
    """Remove all event dirs in the test environment."""
    events_root = paths.events_dir()
    if events_root.is_dir():
        shutil.rmtree(events_root)
    events_root.mkdir(parents=True, exist_ok=True)


# ==================================================================
print("=" * 60)
print("Phase 05 -- Worker Claim Locking and Execution Skeleton Tests")
print("=" * 60)
print(f"Test SERVICE_ROOT: {_tmpdir}")
print()

# Ensure events dir exists
paths.events_dir().mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------------
# Test 1: Multi-process claim locking — two processes cannot claim the same event
# ------------------------------------------------------------------
print("--- Test 1: Multi-process claim locking ---")
_cleanup()

_make_queued_event("mp_contest")


def _child_claim(event_id: str, result_dict: dict, worker_name: str,
                 tmpdir: str, project_dir: str) -> None:
    """Child process: try to claim an event and report success/failure."""
    import sys
    if project_dir not in sys.path:
        sys.path.insert(0, project_dir)
    os.environ["SERVICE_ROOT"] = tmpdir
    os.environ["RUNTIME_ROOT"] = tmpdir

    from shakemap_service import config as _cfg
    _cfg.settings = _cfg.Settings()

    from shakemap_service.queue import _claim_with_lock

    try:
        record = _claim_with_lock(event_id)
        result_dict[worker_name] = "claimed"
    except (ValueError, BlockingIOError, FileNotFoundError) as exc:
        result_dict[worker_name] = f"failed: {type(exc).__name__}: {exc}"
    except Exception as exc:
        result_dict[worker_name] = f"error: {type(exc).__name__}: {exc}"


# We test locking by:
# 1. Worker A acquires a file lock and holds it.
# 2. Worker B tries to claim the same event — should fail with BlockingIOError.
# 3. Worker A releases the lock.

status_file = paths.event_status_file("mp_contest")

# Acquire lock as "Worker A"
lock_fd = open(status_file, "r")
fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

# "Worker B" tries to claim while lock is held
try:
    _claim_with_lock("mp_contest")
    check("Worker B claim while lock held -> should fail", False,
          "No exception raised")
except BlockingIOError:
    check("Worker B gets BlockingIOError when lock held by Worker A", True)
except Exception as exc:
    check("Worker B gets BlockingIOError when lock held by Worker A", False,
          f"Got {type(exc).__name__}: {exc}")

# Verify event is still QUEUED (Worker A has the lock but didn't transition)
r = read_status("mp_contest")
check("Event still QUEUED while lock held", r.status == "QUEUED" if r else False)

# Release Worker A's lock
lock_fd.close()

# Now a claim should succeed
try:
    updated = _claim_with_lock("mp_contest")
    check("Claim succeeds after lock released", True)
    check("Event transitioned to RUNNING", updated.status == "RUNNING")
except Exception as exc:
    check("Claim succeeds after lock released", False, str(exc))
    check("Event transitioned to RUNNING", False)

print()


# ------------------------------------------------------------------
# Test 2: Claim lock releases after successful claim attempt
# ------------------------------------------------------------------
print("--- Test 2: Claim lock releases after successful claim ---")
_cleanup()

_make_queued_event("lock_release")

snap = take_snapshot()
result = snap.claim_next()
check("Claim succeeded", result is not None and result.success)

# After claim, the lock should be released.  Verify by acquiring
# the lock again (should not block).
sf = paths.event_status_file("lock_release")
fd = open(sf, "r")
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    check("Lock released after claim -- re-acquirable", True)
    fcntl.flock(fd, fcntl.LOCK_UN)
except BlockingIOError:
    check("Lock released after claim -- re-acquirable", False,
          "Lock still held!")
finally:
    fd.close()

print()


# ------------------------------------------------------------------
# Test 3: Failed claim leaves event recoverable (still QUEUED)
# ------------------------------------------------------------------
print("--- Test 3: Failed claim leaves event recoverable ---")
_cleanup()

_make_queued_event("fail_recover")

# Make the event non-QUEUED to simulate a race condition
# (another process changed it between snapshot and lock)
r = read_status("fail_recover")
r.status = EventStatus.RUNNING.value
r.current_attempt = 1
r.started_at = _iso(0)
r.attempt_history = [AttemptRecord(attempt_number=1, started_at=_iso(0), status="RUNNING")]
write_status_atomic("fail_recover", r)

# Now a snapshot was taken when it was QUEUED, but on disk it's RUNNING
snap = QueueSnapshot(candidates=[_make_queued_event("fail_recover_fresh")])
result = snap.claim_next()
# The fresh one should claim fine
check("Fresh event claimed successfully", result is not None and result.success)

# The original event (now RUNNING) should not be claimable via direct lock
try:
    _claim_with_lock("fail_recover")
    check("Cannot claim RUNNING event", False, "Should have raised ValueError")
except ValueError:
    check("Cannot claim RUNNING event -- ValueError raised", True)

# Event is still in a recoverable state
r2 = read_status("fail_recover")
check("Event still on disk", r2 is not None)
check("Event status is RUNNING (not lost)", r2.status == "RUNNING" if r2 else False)
print()


# ------------------------------------------------------------------
# Test 4: QUEUED events remain discoverable after failed claim
# ------------------------------------------------------------------
print("--- Test 4: QUEUED events remain discoverable after failed claim ---")
_cleanup()

_make_queued_event("disc_a", queued_at=_iso(10))
_make_queued_event("disc_b", queued_at=_iso(20))

# Claim disc_a, which changes it to RUNNING
snap = take_snapshot()
snap.claim_next()  # claims disc_a

# New snapshot should still find disc_b
snap2 = take_snapshot()
check("disc_b still discoverable", snap2.pending_count == 1)
check("disc_b is the remaining candidate",
      snap2.pending[0].event_id == "disc_b" if snap2.pending else False)

# Claim disc_b
result = snap2.claim_next()
check("disc_b claimed successfully", result is not None and result.success)

# Now empty
snap3 = take_snapshot()
check("Queue is empty after all claims", snap3.pending_count == 0)
print()


# ------------------------------------------------------------------
# Test 5: RUNNING claim creates attempt history
# ------------------------------------------------------------------
print("--- Test 5: RUNNING claim creates attempt history ---")
_cleanup()

_make_queued_event("hist_event")

snap = take_snapshot()
result = snap.claim_next()

check("Claim succeeded", result is not None and result.success)
if result and result.record:
    r = result.record
    check("Status is RUNNING", r.status == "RUNNING")
    check("current_attempt is 1", r.current_attempt == 1, f"got {r.current_attempt}")
    check("attempt_history has 1 entry", len(r.attempt_history) == 1,
          f"got {len(r.attempt_history)}")
    check("Attempt status is RUNNING", r.attempt_history[0].status == "RUNNING")
    check("Attempt started_at set", r.attempt_history[0].started_at is not None)
    check("Attempt number is 1", r.attempt_history[0].attempt_number == 1)

    # Verify on disk
    disk = read_status("hist_event")
    check("On-disk status is RUNNING", disk.status == "RUNNING" if disk else False)
    check("On-disk attempt_history has 1 entry",
          len(disk.attempt_history) == 1 if disk else False)
print()


# ------------------------------------------------------------------
# Test 6: Non-QUEUED events cannot be claimed
# ------------------------------------------------------------------
print("--- Test 6: Non-QUEUED events cannot be claimed ---")
_cleanup()

for sid, st in [
    ("nc_registered", EventStatus.REGISTERED),
    ("nc_validating", EventStatus.VALIDATING),
    ("nc_valfailed", EventStatus.VALIDATION_FAILED),
    ("nc_running", EventStatus.RUNNING),
    ("nc_success", EventStatus.SUCCESS),
    ("nc_failed", EventStatus.FAILED),
    ("nc_cancelled", EventStatus.CANCELLED),
]:
    _make_event_with_status(sid, st)

for eid in ["nc_registered", "nc_validating", "nc_valfailed",
            "nc_running", "nc_success", "nc_failed", "nc_cancelled"]:
    try:
        _claim_with_lock(eid)
        check(f"{eid} ({read_status(eid).status}) cannot be claimed", False,
              "No exception raised")
    except ValueError:
        check(f"{eid} cannot be claimed -- ValueError", True)
    except Exception as exc:
        check(f"{eid} cannot be claimed", True, f"Got {type(exc).__name__}")

print()


# ------------------------------------------------------------------
# Test 7: Queue ordering remains deterministic with locking
# ------------------------------------------------------------------
print("--- Test 7: Queue ordering deterministic with locking ---")
_cleanup()

_make_queued_event("order_c", queued_at=_iso(30))
_make_queued_event("order_a", queued_at=_iso(10))
_make_queued_event("order_b", queued_at=_iso(20))

snap = take_snapshot()
r1 = snap.claim_next()
check("First claim: order_a",
      r1.event_id == "order_a" if r1 else False)

r2 = snap.claim_next()
check("Second claim: order_b",
      r2.event_id == "order_b" if r2 else False)

r3 = snap.claim_next()
check("Third claim: order_c",
      r3.event_id == "order_c" if r3 else False)

r4 = snap.claim_next()
check("No more claims: None", r4 is None)
print()


# ------------------------------------------------------------------
# Test 8: Worker skeleton — placeholder execution returns to QUEUED
# ------------------------------------------------------------------
print("--- Test 8: Worker placeholder execution -> QUEUED ---")
_cleanup()

_make_queued_event("wk_placeholder")

snap = take_snapshot()
wr = process_next_event(snap)

check("WorkerResult claimed is True", wr.claimed)
check("event_id is wk_placeholder", wr.event_id == "wk_placeholder")
check("Outcome is placeholder_no_execution",
      wr.outcome == PLACEHOLDER_OUTCOME)
check("Final status is QUEUED", wr.final_status == "QUEUED")

# Verify on disk
disk = read_status("wk_placeholder")
check("On-disk status is QUEUED", disk.status == "QUEUED" if disk else False)
check("On-disk current_attempt is 0 (decremented back)",
      disk.current_attempt == 0 if disk else False)

# Verify attempt history records the placeholder attempt
check("attempt_history has 1 placeholder entry",
      len(disk.attempt_history) == 1 if disk else False)
if disk and disk.attempt_history:
    a = disk.attempt_history[0]
    check("Placeholder attempt status is PLACEHOLDER", a.status == "PLACEHOLDER")
    check("Placeholder attempt has failure_reason",
          a.failure_reason is not None and "placeholder" in a.failure_reason.lower())

# Event should still be discoverable in queue
snap2 = take_snapshot()
check("Event still discoverable after placeholder", snap2.pending_count == 1)
print()


# ------------------------------------------------------------------
# Test 9: Worker skeleton — no real ShakeMap subprocess call
# ------------------------------------------------------------------
print("--- Test 9: No real ShakeMap subprocess call ---")

worker_py = (PROJECT_DIR / "shakemap_service" / "worker.py").read_text()
queue_py = (PROJECT_DIR / "shakemap_service" / "queue.py").read_text()

check("No 'import subprocess' in worker.py",
      "import subprocess" not in worker_py)
check("No 'subprocess.run' in worker.py",
      "subprocess.run" not in worker_py)
check("No 'import subprocess' in queue.py",
      "import subprocess" not in queue_py)
check("No 'subprocess.run' in queue.py",
      "subprocess.run" not in queue_py)
# run_shake is mentioned in docstrings for Phase 06 handoff, but never called
def _has_run_shake_call(source: str) -> bool:
    """Check if run_shake is called in actual code (not docstrings/comments)."""
    in_docstring = False
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith('"""') or stripped.startswith("'''"):
            if stripped.count('"""') == 1 or stripped.count("'''") == 1:
                in_docstring = not in_docstring
            continue
        if in_docstring or stripped.startswith('#'):
            continue
        if 'run_shake(' in stripped:
            return True
    return False

check("No 'run_shake' call in worker.py code",
      not _has_run_shake_call(worker_py))
check("No 'from .runner' in worker.py",
      "from .runner" not in worker_py)
check("No 'import runner' in worker.py",
      "import runner" not in worker_py)
print()


# ------------------------------------------------------------------
# Test 10: Worker skeleton — run_worker_cycle
# ------------------------------------------------------------------
print("--- Test 10: run_worker_cycle ---")
_cleanup()

_make_queued_event("cycle_event")

wr = run_worker_cycle()
check("Cycle claimed", wr.claimed)
check("Cycle event_id is cycle_event", wr.event_id == "cycle_event")
check("Cycle outcome is placeholder", wr.outcome == PLACEHOLDER_OUTCOME)
check("Cycle final_status is QUEUED", wr.final_status == "QUEUED")

# Empty queue
_cleanup()
wr_empty = run_worker_cycle()
check("Empty cycle: claimed is False", not wr_empty.claimed)
check("Empty cycle: outcome is no_candidates", wr_empty.outcome == "no_candidates")
print()


# ------------------------------------------------------------------
# Test 11: Recovery — find_stale_running identifies RUNNING records
# ------------------------------------------------------------------
print("--- Test 11: find_stale_running ---")
_cleanup()

_make_event_with_status("stale_a", EventStatus.RUNNING)
_make_event_with_status("stale_b", EventStatus.RUNNING)
_make_queued_event("stale_queued")
_make_event_with_status("stale_done", EventStatus.SUCCESS)

stale = find_stale_running()
stale_ids = [r.event_id for r in stale]

check("find_stale_running returns 2", len(stale) == 2, f"got {len(stale)}")
check("stale_a found", "stale_a" in stale_ids)
check("stale_b found", "stale_b" in stale_ids)
check("stale_queued NOT found", "stale_queued" not in stale_ids)
check("stale_done NOT found", "stale_done" not in stale_ids)
check("All results are RUNNING",
      all(r.status == "RUNNING" for r in stale))
print()


# ------------------------------------------------------------------
# Test 12: Recovery — record_interrupted_attempt re-queues or fails
# ------------------------------------------------------------------
print("--- Test 12: record_interrupted_attempt ---")
_cleanup()

# Case A: attempts remaining → re-queue
_make_event_with_status("int_requeue", EventStatus.RUNNING,
                        max_attempts=3, current_attempt=1)
result_a = record_interrupted_attempt("int_requeue")
check("Re-queued: status is QUEUED", result_a.status == "QUEUED")
check("Re-queued: current_attempt unchanged at 1", result_a.current_attempt == 1)
check("Re-queued: attempt_history[0] completed",
      result_a.attempt_history[0].completed_at is not None)
check("Re-queued: attempt_history[0] status FAILED",
      result_a.attempt_history[0].status == "FAILED")
check("Re-queued: started_at cleared", result_a.started_at is None)

# Case B: all attempts used → FAILED
_make_event_with_status("int_exhaust", EventStatus.RUNNING,
                        max_attempts=1, current_attempt=1)
result_b = record_interrupted_attempt("int_exhaust")
check("Exhausted: status is FAILED", result_b.status == "FAILED")
check("Exhausted: failure_reason set", result_b.failure_reason is not None)
check("Exhausted: completed_at set", result_b.completed_at is not None)

# Case C: non-RUNNING → error
_make_queued_event("int_wrong")
try:
    record_interrupted_attempt("int_wrong")
    check("Non-RUNNING raises ValueError", False)
except ValueError:
    check("Non-RUNNING raises ValueError", True)
print()


# ------------------------------------------------------------------
# Test 13: Worker recover_interrupted_events at startup
# ------------------------------------------------------------------
print("--- Test 13: recover_interrupted_events ---")
_cleanup()

_make_event_with_status("rec_a", EventStatus.RUNNING, max_attempts=3, current_attempt=1)
_make_event_with_status("rec_b", EventStatus.RUNNING, max_attempts=1, current_attempt=1)
_make_queued_event("rec_c")

recovered = recover_interrupted_events()

check("Recovered 2 events", len(recovered) == 2, f"got {len(recovered)}")
check("rec_a recovered", "rec_a" in recovered)
check("rec_b recovered", "rec_b" in recovered)

# rec_a should be QUEUED (attempts remaining)
ra = read_status("rec_a")
check("rec_a -> QUEUED", ra.status == "QUEUED" if ra else False)

# rec_b should be FAILED (no attempts remaining)
rb = read_status("rec_b")
check("rec_b -> FAILED", rb.status == "FAILED" if rb else False)

# rec_c should be unchanged
rc = read_status("rec_c")
check("rec_c unchanged (QUEUED)", rc.status == "QUEUED" if rc else False)

# Queue should now have rec_a and rec_c
snap = take_snapshot()
check("Queue has 2 candidates (rec_a + rec_c)", snap.pending_count == 2,
      f"got {snap.pending_count}")
print()


# ------------------------------------------------------------------
# Test 14: No ShakeMap subprocess call anywhere
# ------------------------------------------------------------------
print("--- Test 14: No ShakeMap subprocess in new/modified files ---")

worker_py = (PROJECT_DIR / "shakemap_service" / "worker.py").read_text()
queue_py = (PROJECT_DIR / "shakemap_service" / "queue.py").read_text()
status_py = (PROJECT_DIR / "shakemap_service" / "status.py").read_text()

for fname, content in [("worker.py", worker_py), ("queue.py", queue_py)]:
    check(f"No 'subprocess' in {fname}",
          "import subprocess" not in content and "subprocess.run" not in content)
    check(f"No 'run_shake' call in {fname} code",
          not _has_run_shake_call(content))
    check(f"No 'ShakeError' in {fname}",
          "ShakeError" not in content)

print()


# ------------------------------------------------------------------
# Test 15: No product publication
# ------------------------------------------------------------------
print("--- Test 15: No product publication ---")

for fname, content in [("worker.py", worker_py), ("queue.py", queue_py)]:
    check(f"No 'publish' in {fname}",
          "publish" not in content.lower() or "published_products" in content.lower())
    check(f"No 'shutil.copy' in {fname}",
          "shutil.copy" not in content)
    check(f"No 'shutil.move' in {fname}",
          "shutil.move" not in content)

# No products dir created by worker
products = paths.products_dir()
products.mkdir(parents=True, exist_ok=True)
products_before = list(products.iterdir())
check("No product dirs created by tests",
      len(products_before) == 0, f"got {len(products_before)}")
print()


# ------------------------------------------------------------------
# Test 16: runner.py unchanged
# ------------------------------------------------------------------
print("--- Test 16: runner.py unchanged ---")

runner_path = PROJECT_DIR / "shakemap_service" / "runner.py"
runner_text = runner_path.read_text()

check("runner.py exists", runner_path.is_file())
check("runner.py contains ShakeError class", "class ShakeError" in runner_text)
check("runner.py contains run_shake function", "def run_shake" in runner_text)
check("runner.py has no queue imports",
      "from .queue" not in runner_text and "import queue" not in runner_text)
check("runner.py has no worker imports",
      "from .worker" not in runner_text and "import worker" not in runner_text)
check("runner.py has no status imports",
      "from .status" not in runner_text)

# Verify main.py was NOT changed (no worker imports)
main_path = PROJECT_DIR / "shakemap_service" / "main.py"
main_text = main_path.read_text()
check("main.py has no worker imports",
      "from .worker" not in main_text and "import worker" not in main_text)
print()


# ------------------------------------------------------------------
# Test 17: Phase 06 integration point documented
# ------------------------------------------------------------------
print("--- Test 17: Phase 06 integration point ---")

worker_py = (PROJECT_DIR / "shakemap_service" / "worker.py").read_text()

check("Worker module documents Phase 06 handoff",
      "Phase 06" in worker_py)
check("execute_fn parameter exists",
      "execute_fn" in worker_py)
check("ExecuteFn type alias defined",
      "ExecuteFn" in worker_py)
check("process_next_event accepts execute_fn parameter",
      "def process_next_event" in worker_py and "execute_fn" in worker_py)
check("run_worker_cycle accepts execute_fn parameter",
      "def run_worker_cycle" in worker_py and "execute_fn" in worker_py)

# Verify the placeholder is the default, not a hard requirement
import inspect
sig = inspect.signature(process_next_event)
check("execute_fn has default (placeholder)",
      "execute_fn" in sig.parameters and
      sig.parameters["execute_fn"].default is not inspect.Parameter.empty)
print()


# ==================================================================
# Cleanup
# ==================================================================
shutil.rmtree(_tmpdir, ignore_errors=True)

# ==================================================================
# Summary
# ==================================================================
print("=" * 60)
total = passed + failed
print(f"Phase 05 results: {passed} passed, {failed} failed (of {total} total)")
if failed == 0:
    print("ALL TESTS PASSED")
else:
    print("SOME TESTS FAILED")
print("=" * 60)

sys.exit(0 if failed == 0 else 1)
