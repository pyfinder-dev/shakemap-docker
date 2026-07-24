# -*- coding: utf-8 -*-
"""Single-entry worker and restart reconciliation without retries."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from .queue import ClaimResult, QueueSnapshot, take_snapshot
from .status import (
    RequestStatus,
    fail_interrupted,
    find_stale_running,
    read_status,
    transition_to_failed,
)

logger = logging.getLogger(__name__)


@dataclass
class WorkerResult:
    claimed: bool
    event_id: Optional[str] = None
    sequence: Optional[int] = None
    outcome: str = "no_candidates"
    claim_result: Optional[ClaimResult] = None
    final_status: Optional[str] = None


def execute_shakemap(record: RequestStatus) -> str:
    from .runner import run_shake_for_event
    return run_shake_for_event(record)


ExecuteFn = Callable[[RequestStatus], str]


def process_next_event(
    snapshot: QueueSnapshot,
    execute_fn: ExecuteFn = execute_shakemap,
) -> WorkerResult:
    claim = snapshot.claim_next()
    if claim is None:
        return WorkerResult(claimed=False)
    if not claim.success or claim.record is None:
        return WorkerResult(
            claimed=False,
            event_id=claim.event_id,
            sequence=claim.sequence,
            outcome="claim_failed",
            claim_result=claim,
        )
    record = claim.record
    try:
        outcome = execute_fn(record)
    except Exception as exc:
        logger.exception(
            "Execution raised for queue sequence %s event %s",
            record.sequence,
            record.event_id,
        )
        transition_to_failed(
            record.sequence,
            f"Unhandled execution error: {type(exc).__name__}: {exc}",
            code="unhandled_execution_error",
        )
        outcome = "failed"
    final = read_status(record.sequence)
    return WorkerResult(
        claimed=True,
        event_id=record.event_id,
        sequence=record.sequence,
        outcome=outcome,
        claim_result=claim,
        final_status=final.status if final else None,
    )


def run_worker_cycle(execute_fn: ExecuteFn = execute_shakemap) -> WorkerResult:
    return process_next_event(take_snapshot(), execute_fn=execute_fn)


def recover_interrupted_events() -> list[int]:
    """Finalize stale RUNNING entries as FAILED; never requeue or retry."""
    recovered: list[int] = []
    for record in find_stale_running():
        try:
            fail_interrupted(record.sequence)
            recovered.append(record.sequence)
            logger.warning(
                "Marked interrupted queue sequence %s event %s FAILED; no retry",
                record.sequence,
                record.event_id,
            )
        except (FileNotFoundError, ValueError):
            logger.exception(
                "Could not reconcile interrupted queue sequence %s",
                record.sequence,
            )
    return recovered
