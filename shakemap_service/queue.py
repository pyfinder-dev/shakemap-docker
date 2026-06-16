# -*- coding: utf-8 -*-
"""Durable queue discovery, claim locking, and queue-state helpers.

Phase 04 — Durable Queue Foundation.
Phase 05 — Worker Claim Locking.

This module provides:

- ``discover_queue()`` — scan filesystem for events with QUEUED status,
  return a deterministic FIFO-ordered list of queue candidates.
- ``QueueSnapshot`` — immutable snapshot of discovered queue candidates
  with ``claim_next()`` and ``claim_event()`` helpers for safe
  QUEUED → RUNNING transition with multi-process filesystem locking.
- ``list_queue_candidates()`` — convenience wrapper returning event_ids
  of current queue candidates in FIFO order.

Design constraints:

- No separate queue files — ``requeststatus.json`` is the durable source
  of truth (contract §5.2, §5.3).
- Queue is reconstructed from filesystem on every call (contract §5.2:
  "The queue state MUST be reconstructable from requeststatus.json files
  on disk").
- Ordering: deterministic FIFO by ``queued_at``, then ``submitted_at``,
  then ``event_id`` as tiebreaker.
- Malformed status files are logged/reported but never crash discovery.
- Claiming an event acquires an exclusive ``fcntl.flock`` on the status
  file, validates status is still QUEUED, then performs the QUEUED → RUNNING
  transition while holding the lock.  The lock is released after the atomic
  write completes.
- Duplicate claims within the same ``QueueSnapshot`` are prevented —
  once an event is claimed, it is removed from the snapshot's candidate
  list.
- No worker loop, no ShakeMap execution, no retry execution, no product
  publication, no CLI.
"""
from __future__ import annotations

import fcntl
import logging
from dataclasses import dataclass, field
from typing import Optional

from . import paths
from . import status as status_mod
from .status import (
    EventStatus,
    RequestStatus,
    read_status,
    scan_event_records,
    transition_to_running,
    write_status_atomic,
    _dict_to_record,
    _now_iso,
    _validate_transition,
    AttemptRecord,
)

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Queue candidate sorting key
# ------------------------------------------------------------------

def _queue_sort_key(record: RequestStatus) -> tuple[str, str, str]:
    """Return a sort key for deterministic FIFO ordering.

    Priority:
    1. ``queued_at`` timestamp (earliest first)
    2. ``submitted_at`` timestamp as fallback
    3. ``event_id`` as final tiebreaker for absolute determinism

    Missing timestamps sort after present ones (empty string < any ISO
    timestamp would sort wrong, so we use a sentinel that sorts last).
    """
    SENTINEL = "9999-12-31T23:59:59+00:00"
    queued_at = record.queued_at or SENTINEL
    submitted_at = record.submitted_at or SENTINEL
    return (queued_at, submitted_at, record.event_id)


# ------------------------------------------------------------------
# Discovery
# ------------------------------------------------------------------

@dataclass
class MalformedRecord:
    """Report entry for a malformed status file encountered during scan."""
    event_id: str
    error: str


def discover_queue() -> tuple[list[RequestStatus], list[MalformedRecord]]:
    """Discover all QUEUED events from the filesystem.

    Scans ``events/*/`` for ``requeststatus.json`` files via the
    existing ``scan_event_records()`` helper.  Filters to events with
    status ``QUEUED`` and returns them in deterministic FIFO order.

    Returns:
        A tuple of:
        - List of ``RequestStatus`` records with status QUEUED, sorted
          by ``queued_at`` / ``submitted_at`` / ``event_id``.
        - List of ``MalformedRecord`` entries for any status files that
          could not be read (logged, never crash).
    """
    malformed: list[MalformedRecord] = []

    events_root = paths.events_dir()
    all_records: list[RequestStatus] = []

    if not events_root.is_dir():
        return [], malformed

    for entry in sorted(events_root.iterdir()):
        if not entry.is_dir():
            continue

        event_id = entry.name
        try:
            record = read_status(event_id)
            if record is not None:
                all_records.append(record)
        except ValueError as exc:
            msg = str(exc)
            logger.warning(
                "Queue discovery: skipping malformed record for event '%s': %s",
                event_id, msg,
            )
            malformed.append(MalformedRecord(event_id=event_id, error=msg))
        except Exception as exc:
            msg = str(exc)
            logger.warning(
                "Queue discovery: error reading record for event '%s': %s",
                event_id, msg,
            )
            malformed.append(MalformedRecord(event_id=event_id, error=msg))

    # Filter to QUEUED status only.
    queued = [r for r in all_records if r.status == EventStatus.QUEUED.value]

    # Sort for deterministic FIFO order.
    queued.sort(key=_queue_sort_key)

    return queued, malformed


# ------------------------------------------------------------------
# Convenience: list queue candidate event IDs
# ------------------------------------------------------------------

def list_queue_candidates() -> list[str]:
    """Return event_ids of current queue candidates in FIFO order.

    Convenience wrapper around ``discover_queue()``.  Malformed records
    are logged but not returned.
    """
    queued, _malformed = discover_queue()
    return [r.event_id for r in queued]


# ------------------------------------------------------------------
# Filesystem claim lock
# ------------------------------------------------------------------

def _claim_with_lock(event_id: str) -> RequestStatus:
    """Acquire an exclusive lock on requeststatus.json, validate the
    event is still QUEUED, and perform the QUEUED → RUNNING transition.

    The lock uses ``fcntl.flock(LOCK_EX | LOCK_NB)`` — a non-blocking
    exclusive lock on the status file.  This is appropriate for
    Unix/Linux containers and prevents two workers/processes from
    claiming the same event simultaneously.

    Locking strategy:
    1. Open ``requeststatus.json`` for reading.
    2. Acquire exclusive non-blocking flock.
    3. Re-read the file contents while holding the lock (the file may
       have been updated between the snapshot and the lock acquisition).
    4. Validate the status is still QUEUED.
    5. Perform the QUEUED → RUNNING transition (increment attempt,
       append AttemptRecord, update timestamps).
    6. Write the updated record atomically (temp + rename).
    7. Release the lock (close the file descriptor).

    Raises:
        ``BlockingIOError``: if another process holds the lock.
        ``ValueError``: if the event is no longer QUEUED.
        ``FileNotFoundError``: if the status file does not exist.
    """
    status_file = paths.event_status_file(event_id)

    if not status_file.is_file():
        raise FileNotFoundError(
            f"No requeststatus.json found for event '{event_id}'"
        )

    # Open the file and acquire exclusive non-blocking lock.
    fd = open(status_file, "r")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        # Re-read while holding lock — authoritative state.
        import json
        text = fd.read()
        data = json.loads(text)
        record = _dict_to_record(data)

        # Validate the event is still QUEUED.
        _validate_transition(record.status, EventStatus.RUNNING)

        # Perform transition.
        now = _now_iso()
        record.status = EventStatus.RUNNING.value
        record.started_at = now
        record.current_attempt += 1

        attempt = AttemptRecord(
            attempt_number=record.current_attempt,
            started_at=now,
            status="RUNNING",
        )
        record.attempt_history.append(attempt)

        # Write atomically while still holding the lock.
        write_status_atomic(event_id, record)

        return record
    finally:
        # Releasing the file descriptor releases the flock.
        fd.close()


# ------------------------------------------------------------------
# Queue snapshot with claim tracking
# ------------------------------------------------------------------

@dataclass
class ClaimResult:
    """Result of a claim attempt."""
    success: bool
    event_id: str
    record: Optional[RequestStatus] = None
    error: Optional[str] = None


@dataclass
class QueueSnapshot:
    """Immutable snapshot of discovered queue candidates.

    Provides ``claim_next()`` for safe QUEUED → RUNNING transition.
    Tracks claimed event_ids to prevent duplicate claims within the
    same snapshot.

    Multi-process safety:
        Claim operations use ``fcntl.flock`` on the status file to
        provide multi-process-safe claim locking.  Two workers cannot
        both claim the same QUEUED event — the second will receive a
        failed ``ClaimResult``.
    """

    candidates: list[RequestStatus] = field(default_factory=list)
    malformed: list[MalformedRecord] = field(default_factory=list)
    _claimed: set[str] = field(default_factory=set, repr=False)

    @property
    def pending(self) -> list[RequestStatus]:
        """Return unclaimed candidates in FIFO order."""
        return [c for c in self.candidates if c.event_id not in self._claimed]

    @property
    def pending_count(self) -> int:
        """Return number of unclaimed candidates."""
        return len(self.pending)

    def claim_next(self) -> Optional[ClaimResult]:
        """Claim the next unclaimed candidate (QUEUED → RUNNING).

        Uses filesystem locking (``fcntl.flock``) to ensure multi-process
        safety.  Returns ``None`` if no unclaimed candidates remain.
        Returns a ``ClaimResult`` with ``success=True`` and the updated
        record on success, or ``success=False`` with an error message
        if the transition fails (e.g. event was already claimed by
        another process or is no longer QUEUED).

        The claim is tracked in ``_claimed`` to prevent duplicate claims
        within this snapshot instance.
        """
        unclaimed = self.pending
        if not unclaimed:
            return None

        candidate = unclaimed[0]
        event_id = candidate.event_id

        # Guard: already claimed in this snapshot (should not happen
        # given the pending filter, but belt-and-suspenders).
        if event_id in self._claimed:
            return ClaimResult(
                success=False,
                event_id=event_id,
                error="Already claimed in this snapshot",
            )

        # Mark as claimed BEFORE the transition to prevent re-entry.
        self._claimed.add(event_id)

        try:
            updated = _claim_with_lock(event_id)
            logger.info(
                "Claimed event '%s': QUEUED -> RUNNING (attempt %d)",
                event_id, updated.current_attempt,
            )
            return ClaimResult(
                success=True,
                event_id=event_id,
                record=updated,
            )
        except (ValueError, FileNotFoundError, BlockingIOError) as exc:
            # Transition failed — event may have been claimed by another
            # process, the lock is held, or status changed.
            msg = str(exc)
            logger.warning(
                "Failed to claim event '%s': %s",
                event_id, msg,
            )
            return ClaimResult(
                success=False,
                event_id=event_id,
                error=msg,
            )

    def claim_event(self, event_id: str) -> ClaimResult:
        """Claim a specific event by event_id.

        Returns a ``ClaimResult``.  If the event_id is not in the
        candidate list or is already claimed, returns a failure result.
        Uses filesystem locking for multi-process safety.
        """
        # Check if in candidates.
        candidate_ids = {c.event_id for c in self.candidates}
        if event_id not in candidate_ids:
            return ClaimResult(
                success=False,
                event_id=event_id,
                error=f"Event '{event_id}' is not a queue candidate",
            )

        if event_id in self._claimed:
            return ClaimResult(
                success=False,
                event_id=event_id,
                error=f"Event '{event_id}' already claimed in this snapshot",
            )

        self._claimed.add(event_id)

        try:
            updated = _claim_with_lock(event_id)
            logger.info(
                "Claimed event '%s': QUEUED -> RUNNING (attempt %d)",
                event_id, updated.current_attempt,
            )
            return ClaimResult(
                success=True,
                event_id=event_id,
                record=updated,
            )
        except (ValueError, FileNotFoundError, BlockingIOError) as exc:
            msg = str(exc)
            logger.warning(
                "Failed to claim event '%s': %s",
                event_id, msg,
            )
            return ClaimResult(
                success=False,
                event_id=event_id,
                error=msg,
            )


def take_snapshot() -> QueueSnapshot:
    """Discover the queue and return an immutable snapshot.

    This is the primary entry point for queue consumers.  The snapshot
    provides deterministic ordering and claim-tracking.

    Example::

        snap = take_snapshot()
        while snap.pending_count > 0:
            result = snap.claim_next()
            if result and result.success:
                # execute event ...
                pass
    """
    candidates, malformed = discover_queue()
    return QueueSnapshot(candidates=candidates, malformed=malformed)
