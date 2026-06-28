#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Durable Queue Foundation -- verification tests.

Run with:
    source /Users/savas/my-codes/eew/pyfinder-dev/.venv/bin/activate
    cd /Users/savas/my-codes/eew/pyfinder-dev/shakemap-docker
    python tests/test_durable_queue.py

Test sections:
    1.  discover_queue with multiple QUEUED events — deterministic FIFO
    2.  Non-QUEUED events are ignored by discover_queue
    3.  Malformed status files are reported without crashing
    4.  list_queue_candidates returns ordered event_ids
    5.  take_snapshot returns QueueSnapshot with correct candidates
    6.  claim_next transitions QUEUED → RUNNING with attempt history
    7.  No event is claimed twice in one snapshot
    8.  claim_event by specific event_id
    9.  Restart-style discovery: scan filesystem records
   10.  Empty queue: no candidates, no crash
   11.  Mixed statuses: only QUEUED is picked
   12.  Ordering tiebreaker: submitted_at, then event_id
   13.  No queue/worker/execution/product publication introduced
   14.  runner.py unchanged
"""
from __future__ import annotations

import inspect
import json
import os
import sys
import tempfile
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path

# --- bootstrap: project on sys.path ---------------------
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

# Redirect SERVICE_ROOT to a temp dir so tests don't touch real data.
_tmpdir = tempfile.mkdtemp(prefix="phase04_test_")
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
)
from shakemap_service.queue import (
    discover_queue,
    list_queue_candidates,
    take_snapshot,
    QueueSnapshot,
    ClaimResult,
    MalformedRecord,
    _queue_sort_key,
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
                            user_id: str = "testuser") -> RequestStatus:
    """Create an event record with an arbitrary status."""
    now = _iso(0)
    record = RequestStatus(
        event_id=event_id,
        user_id=user_id,
        status=event_status.value,
        submitted_at=now,
        current_attempt=0 if event_status != EventStatus.RUNNING else 1,
        max_attempts=3,
    )
    if event_status in (EventStatus.QUEUED, EventStatus.RUNNING, EventStatus.SUCCESS,
                        EventStatus.FAILED):
        record.validated_at = now
        record.queued_at = now
    if event_status in (EventStatus.RUNNING, EventStatus.SUCCESS, EventStatus.FAILED):
        record.started_at = now
        record.attempt_history = [AttemptRecord(
            attempt_number=1,
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
print("Durable Queue Foundation -- Verification Tests")
print("=" * 60)
print(f"Test SERVICE_ROOT: {_tmpdir}")
print()

# Ensure events dir exists
paths.events_dir().mkdir(parents=True, exist_ok=True)

# ------------------------------------------------------------------
# Test 1: discover_queue with multiple QUEUED events — deterministic FIFO
# ------------------------------------------------------------------
print("--- Test 1: discover_queue with multiple QUEUED events -- deterministic FIFO ---")
_cleanup()

# Create 4 QUEUED events with different queued_at timestamps (out of order)
_make_queued_event("evt_c", queued_at=_iso(30), submitted_at=_iso(5))
_make_queued_event("evt_a", queued_at=_iso(10), submitted_at=_iso(1))
_make_queued_event("evt_d", queued_at=_iso(40), submitted_at=_iso(2))
_make_queued_event("evt_b", queued_at=_iso(20), submitted_at=_iso(3))

queued, malformed = discover_queue()

check("discover_queue returns 4 candidates", len(queued) == 4,
      f"got {len(queued)}")
check("FIFO order by queued_at: evt_a first", queued[0].event_id == "evt_a" if queued else False)
check("FIFO order by queued_at: evt_b second", queued[1].event_id == "evt_b" if len(queued) > 1 else False)
check("FIFO order by queued_at: evt_c third", queued[2].event_id == "evt_c" if len(queued) > 2 else False)
check("FIFO order by queued_at: evt_d fourth", queued[3].event_id == "evt_d" if len(queued) > 3 else False)
check("No malformed records", len(malformed) == 0, f"got {len(malformed)}")
check("All candidates have QUEUED status",
      all(r.status == "QUEUED" for r in queued))
print()

# ------------------------------------------------------------------
# Test 2: Non-QUEUED events are ignored by discover_queue
# ------------------------------------------------------------------
print("--- Test 2: Non-QUEUED events are ignored ---")
_cleanup()

_make_queued_event("evt_queued_1")
_make_event_with_status("evt_registered", EventStatus.REGISTERED)
_make_event_with_status("evt_validating", EventStatus.VALIDATING)
_make_event_with_status("evt_valfailed", EventStatus.VALIDATION_FAILED)
_make_event_with_status("evt_running", EventStatus.RUNNING)
_make_event_with_status("evt_success", EventStatus.SUCCESS)
_make_event_with_status("evt_failed", EventStatus.FAILED)
_make_event_with_status("evt_cancelled", EventStatus.CANCELLED)

queued, malformed = discover_queue()

check("Only 1 QUEUED event found", len(queued) == 1, f"got {len(queued)}")
check("QUEUED event is evt_queued_1",
      queued[0].event_id == "evt_queued_1" if queued else False)
check("REGISTERED ignored", "evt_registered" not in [r.event_id for r in queued])
check("VALIDATING ignored", "evt_validating" not in [r.event_id for r in queued])
check("VALIDATION_FAILED ignored", "evt_valfailed" not in [r.event_id for r in queued])
check("RUNNING ignored", "evt_running" not in [r.event_id for r in queued])
check("SUCCESS ignored", "evt_success" not in [r.event_id for r in queued])
check("FAILED ignored", "evt_failed" not in [r.event_id for r in queued])
check("CANCELLED ignored", "evt_cancelled" not in [r.event_id for r in queued])
print()

# ------------------------------------------------------------------
# Test 3: Malformed status files are reported without crashing
# ------------------------------------------------------------------
print("--- Test 3: Malformed status files reported without crash ---")
_cleanup()

_make_queued_event("evt_good")

# Create a malformed JSON file
malformed_dir = paths.event_events_dir("evt_bad_json")
malformed_dir.mkdir(parents=True, exist_ok=True)
(malformed_dir / "requeststatus.json").write_text("NOT VALID JSON {{{")

# Create a JSON file missing required fields
incomplete_dir = paths.event_events_dir("evt_incomplete")
incomplete_dir.mkdir(parents=True, exist_ok=True)
(incomplete_dir / "requeststatus.json").write_text(json.dumps({"event_id": "evt_incomplete"}))

# Create a JSON file that's not an object
notobj_dir = paths.event_events_dir("evt_notobj")
notobj_dir.mkdir(parents=True, exist_ok=True)
(notobj_dir / "requeststatus.json").write_text(json.dumps([1, 2, 3]))

queued, malformed = discover_queue()

check("Good event still discovered", len(queued) == 1)
check("Good event is evt_good",
      queued[0].event_id == "evt_good" if queued else False)
check("3 malformed records reported", len(malformed) == 3, f"got {len(malformed)}")
check("Malformed records have event_id fields",
      all(m.event_id for m in malformed))
check("Malformed records have error messages",
      all(m.error for m in malformed))
malformed_ids = {m.event_id for m in malformed}
check("evt_bad_json in malformed", "evt_bad_json" in malformed_ids)
check("evt_incomplete in malformed", "evt_incomplete" in malformed_ids)
check("evt_notobj in malformed", "evt_notobj" in malformed_ids)
print()

# ------------------------------------------------------------------
# Test 4: list_queue_candidates returns ordered event_ids
# ------------------------------------------------------------------
print("--- Test 4: list_queue_candidates returns ordered event_ids ---")
_cleanup()

_make_queued_event("q_third", queued_at=_iso(30))
_make_queued_event("q_first", queued_at=_iso(10))
_make_queued_event("q_second", queued_at=_iso(20))
_make_event_with_status("not_queued", EventStatus.SUCCESS)

candidates = list_queue_candidates()

check("list_queue_candidates returns 3 ids", len(candidates) == 3,
      f"got {len(candidates)}")
check("Order: q_first first", candidates[0] == "q_first" if candidates else False)
check("Order: q_second second", candidates[1] == "q_second" if len(candidates) > 1 else False)
check("Order: q_third third", candidates[2] == "q_third" if len(candidates) > 2 else False)
check("Returns strings", all(isinstance(c, str) for c in candidates))
print()

# ------------------------------------------------------------------
# Test 5: take_snapshot returns QueueSnapshot with correct candidates
# ------------------------------------------------------------------
print("--- Test 5: take_snapshot returns QueueSnapshot ---")
_cleanup()

_make_queued_event("snap_a", queued_at=_iso(10))
_make_queued_event("snap_b", queued_at=_iso(20))
_make_event_with_status("snap_done", EventStatus.SUCCESS)

snap = take_snapshot()

check("Snapshot is QueueSnapshot", isinstance(snap, QueueSnapshot))
check("Snapshot has 2 candidates", len(snap.candidates) == 2,
      f"got {len(snap.candidates)}")
check("pending_count is 2", snap.pending_count == 2)
check("pending returns list of RequestStatus",
      all(isinstance(r, RequestStatus) for r in snap.pending))
check("No malformed records", len(snap.malformed) == 0)
print()

# ------------------------------------------------------------------
# Test 6: claim_next transitions QUEUED → RUNNING with attempt history
# ------------------------------------------------------------------
print("--- Test 6: claim_next transitions QUEUED -> RUNNING ---")
_cleanup()

_make_queued_event("claim_a", queued_at=_iso(10))
_make_queued_event("claim_b", queued_at=_iso(20))

snap = take_snapshot()
result = snap.claim_next()

check("claim_next returns ClaimResult", isinstance(result, ClaimResult))
check("claim_next success", result.success if result else False)
check("Claimed event is claim_a (first in FIFO)",
      result.event_id == "claim_a" if result else False)
check("Result has updated record", result.record is not None if result else False)

if result and result.record:
    r = result.record
    check("Status is RUNNING after claim", r.status == "RUNNING")
    check("current_attempt is 1", r.current_attempt == 1,
          f"got {r.current_attempt}")
    check("attempt_history has 1 entry", len(r.attempt_history) == 1,
          f"got {len(r.attempt_history)}")
    check("Attempt status is RUNNING",
          r.attempt_history[0].status == "RUNNING")
    check("Attempt started_at is set",
          r.attempt_history[0].started_at is not None)

# Verify on-disk state
disk_record = read_status("claim_a")
check("On-disk status is RUNNING",
      disk_record.status == "RUNNING" if disk_record else False)

# Check pending count decreased
check("pending_count decreased to 1", snap.pending_count == 1)

# Claim second
result2 = snap.claim_next()
check("Second claim is claim_b",
      result2.event_id == "claim_b" if result2 else False)
check("Second claim success", result2.success if result2 else False)

# Nothing left
result3 = snap.claim_next()
check("No more candidates: returns None", result3 is None)
print()

# ------------------------------------------------------------------
# Test 7: No event is claimed twice in one snapshot
# ------------------------------------------------------------------
print("--- Test 7: No duplicate claim in one snapshot ---")
_cleanup()

_make_queued_event("dupclaim")
snap = take_snapshot()

r1 = snap.claim_next()
check("First claim succeeds", r1 is not None and r1.success)
check("pending_count is 0 after claim", snap.pending_count == 0)

# Try to claim the same event explicitly
r2 = snap.claim_event("dupclaim")
check("Duplicate claim_event fails", not r2.success)
check("Duplicate claim error mentions 'already claimed'",
      "already claimed" in (r2.error or "").lower())

# Try claim_next again — no candidates
r3 = snap.claim_next()
check("claim_next returns None when empty", r3 is None)
print()

# ------------------------------------------------------------------
# Test 8: claim_event by specific event_id
# ------------------------------------------------------------------
print("--- Test 8: claim_event by specific event_id ---")
_cleanup()

_make_queued_event("specific_a", queued_at=_iso(10))
_make_queued_event("specific_b", queued_at=_iso(20))
_make_queued_event("specific_c", queued_at=_iso(30))

snap = take_snapshot()

# Claim the THIRD event (not FIFO order)
r = snap.claim_event("specific_c")
check("claim_event specific_c succeeds", r.success)
check("Claimed event_id is specific_c", r.event_id == "specific_c")
check("pending_count is 2", snap.pending_count == 2)

# Claim non-existent event
r_bad = snap.claim_event("nonexistent")
check("claim_event nonexistent fails", not r_bad.success)
check("Error mentions 'not a queue candidate'",
      "not a queue candidate" in (r_bad.error or "").lower())
print()

# ------------------------------------------------------------------
# Test 9: Restart-style discovery — scan filesystem records
# ------------------------------------------------------------------
print("--- Test 9: Restart-style discovery -- filesystem scan ---")
_cleanup()

# Simulate a "previous run" left behind QUEUED events on disk
for i in range(5):
    _make_queued_event(f"restart_{i:03d}", queued_at=_iso(i * 10))

# "Restart": create a fresh snapshot from filesystem
snap = take_snapshot()
check("Restart discovery finds 5 events", snap.pending_count == 5,
      f"got {snap.pending_count}")
check("Order preserved: restart_000 first",
      snap.pending[0].event_id == "restart_000" if snap.pending else False)
check("Order preserved: restart_004 last",
      snap.pending[-1].event_id == "restart_004" if snap.pending else False)

# Claim one and verify state
r = snap.claim_next()
check("Claim after restart succeeds", r.success if r else False)
check("Claimed restart_000", r.event_id == "restart_000" if r else False)

# New snapshot shows 4 remaining (restart_000 is now RUNNING)
snap2 = take_snapshot()
check("New snapshot after claim has 4 candidates", snap2.pending_count == 4,
      f"got {snap2.pending_count}")
print()

# ------------------------------------------------------------------
# Test 10: Empty queue — no candidates, no crash
# ------------------------------------------------------------------
print("--- Test 10: Empty queue ---")
_cleanup()

queued, malformed = discover_queue()
check("Empty discover_queue returns empty list", len(queued) == 0)
check("Empty discover_queue no malformed", len(malformed) == 0)

candidates = list_queue_candidates()
check("Empty list_queue_candidates returns empty list", len(candidates) == 0)

snap = take_snapshot()
check("Empty snapshot pending_count is 0", snap.pending_count == 0)
check("Empty snapshot claim_next returns None", snap.claim_next() is None)
print()

# ------------------------------------------------------------------
# Test 11: Mixed statuses — only QUEUED is picked
# ------------------------------------------------------------------
print("--- Test 11: Mixed statuses -- only QUEUED is picked ---")
_cleanup()

all_statuses = [
    ("s_registered", EventStatus.REGISTERED),
    ("s_validating", EventStatus.VALIDATING),
    ("s_valfail", EventStatus.VALIDATION_FAILED),
    ("s_queued_1", EventStatus.QUEUED),
    ("s_running", EventStatus.RUNNING),
    ("s_success", EventStatus.SUCCESS),
    ("s_failed", EventStatus.FAILED),
    ("s_cancelled", EventStatus.CANCELLED),
    ("s_queued_2", EventStatus.QUEUED),
]

for eid, st in all_statuses:
    if st == EventStatus.QUEUED:
        _make_queued_event(eid)
    else:
        _make_event_with_status(eid, st)

queued, _ = discover_queue()
queued_ids = [r.event_id for r in queued]

check("Exactly 2 QUEUED events found", len(queued) == 2, f"got {len(queued)}")
check("s_queued_1 in results", "s_queued_1" in queued_ids)
check("s_queued_2 in results", "s_queued_2" in queued_ids)
check("No non-QUEUED event leaked in",
      all(r.status == "QUEUED" for r in queued))
print()

# ------------------------------------------------------------------
# Test 12: Ordering tiebreaker — submitted_at, then event_id
# ------------------------------------------------------------------
print("--- Test 12: Ordering tiebreaker ---")
_cleanup()

# Same queued_at, different submitted_at
_make_queued_event("tie_b", queued_at=_iso(10), submitted_at=_iso(5))
_make_queued_event("tie_a", queued_at=_iso(10), submitted_at=_iso(2))
_make_queued_event("tie_c", queued_at=_iso(10), submitted_at=_iso(8))

queued, _ = discover_queue()
check("Tiebreaker: 3 events", len(queued) == 3)
check("Same queued_at -> sort by submitted_at: tie_a first",
      queued[0].event_id == "tie_a" if queued else False)
check("Same queued_at -> sort by submitted_at: tie_b second",
      queued[1].event_id == "tie_b" if len(queued) > 1 else False)
check("Same queued_at -> sort by submitted_at: tie_c third",
      queued[2].event_id == "tie_c" if len(queued) > 2 else False)

# Same queued_at AND submitted_at → event_id tiebreaker
_cleanup()
_make_queued_event("zz_event", queued_at=_iso(10), submitted_at=_iso(5))
_make_queued_event("aa_event", queued_at=_iso(10), submitted_at=_iso(5))
_make_queued_event("mm_event", queued_at=_iso(10), submitted_at=_iso(5))

queued, _ = discover_queue()
check("event_id tiebreaker: aa_event first",
      queued[0].event_id == "aa_event" if queued else False)
check("event_id tiebreaker: mm_event second",
      queued[1].event_id == "mm_event" if len(queued) > 1 else False)
check("event_id tiebreaker: zz_event third",
      queued[2].event_id == "zz_event" if len(queued) > 2 else False)
print()

# ------------------------------------------------------------------
# Test 13: No queue/worker/execution/product publication introduced
# ------------------------------------------------------------------
print("--- Test 13: No worker/execution/product publication ---")

queue_py = (PROJECT_DIR / "shakemap_service" / "queue.py").read_text()

check("No 'subprocess' import in queue.py",
      "import subprocess" not in queue_py)
check("No 'worker' class/function in queue.py",
      "def worker" not in queue_py.lower() and "class worker" not in queue_py.lower())
check("No 'run_shake' in queue.py",
      "run_shake" not in queue_py)
check("No 'ShakeError' in queue.py",
      "ShakeError" not in queue_py)
check("No 'publish' in queue.py",
      "publish" not in queue_py.lower())
check("No 'product' handling (write/copy) in queue.py",
      "shutil.copy" not in queue_py and "shutil.move" not in queue_py)
check("No asyncio.Queue or threading in queue.py",
      "asyncio.Queue" not in queue_py and "import threading" not in queue_py)
check("No '@app.post' in queue.py",
      "@app.post" not in queue_py)
check("No '@app.get' in queue.py",
      "@app.get" not in queue_py)
check("No 'run_id' in queue.py (except docstrings/comments)",
      queue_py.count("run_id") == 0)

# worker.py is expected after Phase 05.
# check("No worker.py exists",
#       not (PROJECT_DIR / "shakemap_service" / "worker.py").exists())

# Check no bridge.py was created
check("No bridge.py exists",
      not (PROJECT_DIR / "shakemap_service" / "bridge.py").exists())

# Check no publisher.py was created
check("No publisher.py exists",
      not (PROJECT_DIR / "shakemap_service" / "publisher.py").exists())

# Check no provenance.py was created
check("No provenance.py exists",
      not (PROJECT_DIR / "shakemap_service" / "provenance.py").exists())
print()

# ------------------------------------------------------------------
# Test 14: runner.py unchanged
# ------------------------------------------------------------------
print("--- Test 14: runner.py unchanged ---")

runner_path = PROJECT_DIR / "shakemap_service" / "runner.py"
runner_text = runner_path.read_text()

check("runner.py exists", runner_path.is_file())
check("runner.py contains ShakeError class", "class ShakeError" in runner_text)
check("runner.py contains run_shake function", "def run_shake" in runner_text)
check("runner.py has no queue imports",
      "from .queue" not in runner_text and "import queue" not in runner_text)

# Verify main.py was NOT changed (no queue imports)
main_path = PROJECT_DIR / "shakemap_service" / "main.py"
main_text = main_path.read_text()
check("main.py has no queue imports",
      "from .queue" not in main_text and "import queue" not in main_text)
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
print(f"Durable Queue results: {passed} passed, {failed} failed (of {total} total)")
if failed == 0:
    print("ALL TESTS PASSED")
else:
    print("SOME TESTS FAILED")
print("=" * 60)

sys.exit(0 if failed == 0 else 1)
