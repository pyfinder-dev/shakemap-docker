# -*- coding: utf-8 -*-
"""Worker claim-and-execute loop for ShakeMap event processing.

This module provides:

- ``WorkerResult`` — outcome dataclass for a single worker cycle.
- ``process_next_event()`` — claim the next QUEUED event from a snapshot
  and execute it via the ShakeMap CLI.
- ``execute_shakemap()`` — production execution function that delegates
  to ``runner.run_shake_for_event()``.
- ``execute_placeholder()`` — development/debug-only function that does
  NOT run ShakeMap and returns events to QUEUED.  Must be passed
  explicitly; never used in production startup.
- ``run_worker_cycle()`` — take a queue snapshot, claim and process the
  next event, return the outcome.
- ``recover_interrupted_events()`` — find RUNNING events on disk and
  re-queue or fail them as appropriate (restart recovery).

Execution model:
    - Worker owns QUEUED → RUNNING (claim locking).
    - Runner owns RUNNING → SUCCESS/FAILED (via execute_shakemap callback).
    - Placeholder returns events to QUEUED — development/debug only.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from .queue import ClaimResult, QueueSnapshot, take_snapshot
from .status import (
    EventStatus,
    RequestStatus,
    find_stale_running,
    read_status,
    record_interrupted_attempt,
    update_status,
)

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Worker result
# ------------------------------------------------------------------

@dataclass
class WorkerResult:
    """Outcome of a single worker cycle.

    Attributes:
        claimed: True if an event was claimed.
        event_id: The event_id that was claimed (or None).
        outcome: Descriptive string: "placeholder_no_execution",
                 "no_candidates", "claim_failed", or a real outcome
                 from Phase 06+.
        claim_result: The raw ClaimResult from the queue snapshot.
        final_status: The event's status after processing.
    """
    claimed: bool
    event_id: Optional[str] = None
    outcome: str = "no_candidates"
    claim_result: Optional[ClaimResult] = None
    final_status: Optional[str] = None


# ------------------------------------------------------------------
# Placeholder execution
# ------------------------------------------------------------------

# Placeholder sentinel outcome — not SUCCESS, not FAILED.  The event
# is returned to QUEUED so it remains discoverable and processable.
# This is a DEVELOPMENT/DEBUG-ONLY code path.
PLACEHOLDER_OUTCOME = "placeholder_no_execution"


def execute_placeholder(record: RequestStatus) -> str:
    """Development/debug-only execution function — does NOT run ShakeMap.

    .. warning::
       This function is for development and debugging ONLY.  It must
       be passed explicitly to ``process_next_event()`` or
       ``run_worker_cycle()``; it is never used in production startup.

    This function:
    1. Logs that placeholder execution is active.
    2. Returns a safe outcome string ("placeholder_no_execution").
    3. Does NOT transition the event to SUCCESS or FAILED.

    The caller (``process_next_event``) is responsible for returning
    the event to a safe state (QUEUED) after this placeholder runs.

    Returns:
        ``"placeholder_no_execution"`` — always.
    """
    logger.info(
        "PLACEHOLDER execution for event '%s' -- ShakeMap NOT executed. "
        "Event will be returned to QUEUED. "
        "This is a development/debug code path.",
        record.event_id,
    )
    return PLACEHOLDER_OUTCOME


def execute_shakemap(record: RequestStatus) -> str:
    """Production execution callback — runs ShakeMap for the claimed event.

    This function:
    1. Receives the claimed RequestStatus record (already RUNNING).
    2. Delegates to ``runner.run_shake_for_event()`` which handles:
       - Data preparation (incoming → ShakeMap data layout)
       - ShakeMap CLI invocation with configured modules
       - Product collection and atomic publication
       - RUNNING → SUCCESS/FAILED status transitions

    The worker owns QUEUED → RUNNING (claim locking).
    The runner owns RUNNING → SUCCESS/FAILED (via this callback).

    This is the default execution function for production use.

    Returns:
        ``"success"`` or ``"failed"`` as outcome string.
    """
    from .runner import run_shake_for_event
    return run_shake_for_event(record)


# Type alias for execution functions.  Phase 05 placeholder and
# Phase 07 execute_shakemap both conform to this signature.
ExecuteFn = Callable[[RequestStatus], str]


# ------------------------------------------------------------------
# Process a single event
# ------------------------------------------------------------------

def process_next_event(
    snapshot: QueueSnapshot,
    execute_fn: ExecuteFn = execute_shakemap,
) -> WorkerResult:
    """Claim the next QUEUED event and execute it.

    Args:
        snapshot: A ``QueueSnapshot`` from ``take_snapshot()``.
        execute_fn: A callable that receives the claimed ``RequestStatus``
            and returns an outcome string.  Defaults to the placeholder
            which does nothing.

    Returns:
        A ``WorkerResult`` describing what happened.

    The function:
    1. Calls ``snapshot.claim_next()`` to atomically claim the next
       QUEUED event (QUEUED → RUNNING with filesystem lock).
    2. Calls ``execute_fn(record)`` with the claimed record.
    3. Based on the outcome:
       - If placeholder: returns the event to QUEUED (safe state).
       - If real execution returned "success": the execute_fn itself
         handles the transition (Phase 06).
       - If real execution returned "failed": the execute_fn itself
         handles the transition (Phase 06).

    This function never marks an event as SUCCESS because no ShakeMap
    execution has actually happened in Phase 05.
    """
    claim = snapshot.claim_next()

    if claim is None:
        return WorkerResult(
            claimed=False,
            outcome="no_candidates",
        )

    if not claim.success:
        return WorkerResult(
            claimed=False,
            event_id=claim.event_id,
            outcome="claim_failed",
            claim_result=claim,
        )

    # Claim succeeded — record is now RUNNING on disk.
    record = claim.record

    try:
        outcome = execute_fn(record)
    except Exception as exc:
        # Execution function raised — record the interrupted attempt
        # and return to a safe state.
        outcome = f"execution_error: {exc}"
        logger.error(
            "Execution function raised for event '%s': %s",
            record.event_id, exc,
        )

    # Handle placeholder outcome: return event to QUEUED.
    if outcome == PLACEHOLDER_OUTCOME:
        _return_to_queued(record.event_id)
        final = read_status(record.event_id)
        final_status = final.status if final else "UNKNOWN"
        return WorkerResult(
            claimed=True,
            event_id=record.event_id,
            outcome=PLACEHOLDER_OUTCOME,
            claim_result=claim,
            final_status=final_status,
        )

    # For real outcomes (Phase 06+), the execute_fn is expected to
    # handle status transitions itself.  We just report back.
    final = read_status(record.event_id)
    final_status = final.status if final else "UNKNOWN"
    return WorkerResult(
        claimed=True,
        event_id=record.event_id,
        outcome=outcome,
        claim_result=claim,
        final_status=final_status,
    )


def _return_to_queued(event_id: str) -> None:
    """Return a RUNNING event to QUEUED after placeholder execution.

    The event was claimed (QUEUED → RUNNING) but no real work was done.
    We mark the current attempt as "placeholder — no execution" and
    transition back to QUEUED so the event remains discoverable.

    This directly updates the record rather than using transition
    helpers since RUNNING → QUEUED is not a standard contract transition
    — it is a Phase 05 skeleton-only operation.
    """
    record = read_status(event_id)
    if record is None:
        logger.error(
            "Cannot return event '%s' to QUEUED -- record not found",
            event_id,
        )
        return

    if record.status != EventStatus.RUNNING.value:
        logger.warning(
            "Cannot return event '%s' to QUEUED -- status is '%s', not RUNNING",
            event_id, record.status,
        )
        return

    from .status import _now_iso, write_status_atomic

    now = _now_iso()

    # Complete the current attempt as a placeholder.
    if record.attempt_history:
        current = record.attempt_history[-1]
        current.completed_at = now
        current.status = "PLACEHOLDER"
        current.failure_reason = "Placeholder -- no ShakeMap execution (Phase 05 skeleton)"
        if current.started_at:
            try:
                from datetime import datetime
                started = datetime.fromisoformat(current.started_at)
                completed = datetime.fromisoformat(now)
                current.duration_seconds = round(
                    (completed - started).total_seconds(), 3
                )
            except (ValueError, TypeError):
                current.duration_seconds = None

    # Decrement current_attempt since no real work was done.
    if record.current_attempt > 0:
        record.current_attempt -= 1

    # Return to QUEUED.
    record.status = EventStatus.QUEUED.value
    record.started_at = None
    record.completed_at = None

    write_status_atomic(event_id, record)
    logger.info(
        "Returned event '%s' to QUEUED after placeholder execution",
        event_id,
    )


# ------------------------------------------------------------------
# Full worker cycle
# ------------------------------------------------------------------

def run_worker_cycle(
    execute_fn: ExecuteFn = execute_shakemap,
) -> WorkerResult:
    """Take a queue snapshot, claim and process the next event.

    This is the top-level entry point for a single worker iteration.
    The background worker loop calls this repeatedly with backoff.

    Args:
        execute_fn: The execution function to call.  Defaults to
            ``execute_shakemap`` for production use.  Pass
            ``execute_placeholder`` explicitly for development/debug.

    Returns:
        A ``WorkerResult`` describing what happened.
    """
    snapshot = take_snapshot()
    return process_next_event(snapshot, execute_fn=execute_fn)


# ------------------------------------------------------------------
# Recovery: handle interrupted RUNNING events
# ------------------------------------------------------------------

def recover_interrupted_events() -> list[str]:
    """Find and recover events stuck in RUNNING status.

    This should be called at startup after a crash/restart.  It:
    1. Scans the filesystem for events with status RUNNING.
    2. For each, records the current attempt as interrupted.
    3. Re-queues the event if attempts remain, or fails it.

    Returns:
        List of event_ids that were recovered.
    """
    stale = find_stale_running()
    recovered: list[str] = []

    for record in stale:
        try:
            result = record_interrupted_attempt(
                record.event_id,
                reason="Interrupted -- process no longer active (restart recovery)",
            )
            recovered.append(record.event_id)
            logger.info(
                "Recovered interrupted event '%s' -> %s",
                record.event_id, result.status,
            )
        except (ValueError, FileNotFoundError) as exc:
            logger.warning(
                "Could not recover event '%s': %s",
                record.event_id, exc,
            )

    return recovered
