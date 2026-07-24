# -*- coding: utf-8 -*-
"""Durable FIFO queue discovery and single-entry claiming."""
from __future__ import annotations

import fcntl
import logging
from dataclasses import dataclass, field
from typing import Optional

from . import paths
from .status import (
    EventStatus,
    RequestStatus,
    read_status,
    scan_event_records,
    transition_to_running,
)

logger = logging.getLogger(__name__)


@dataclass
class MalformedRecord:
    entry: str
    error: str


def discover_queue() -> tuple[list[RequestStatus], list[MalformedRecord]]:
    records, errors = scan_event_records()
    queued = [
        record for record in records
        if record.status == EventStatus.QUEUED.value
    ]
    queued.sort(key=lambda record: record.sequence)
    malformed = [MalformedRecord(entry=entry, error=error) for entry, error in errors]
    for item in malformed:
        logger.error("Malformed queue record %s: %s", item.entry, item.error)
    return queued, malformed


def list_queue_candidates() -> list[int]:
    queued, _ = discover_queue()
    return [record.sequence for record in queued]


def _claim_with_lock(sequence: int) -> RequestStatus:
    lock_path = paths.queue_claim_lock_file(sequence)
    if not paths.queue_status_file(sequence).is_file():
        raise FileNotFoundError(f"queue status for sequence {sequence} does not exist")
    lock_path.touch(exist_ok=True)
    with lock_path.open("r+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        record = read_status(sequence)
        if record is None:
            raise FileNotFoundError(f"queue status for sequence {sequence} does not exist")
        if record.status != EventStatus.QUEUED.value:
            raise ValueError(
                f"queue sequence {sequence} is {record.status}, not QUEUED"
            )
        return transition_to_running(sequence)


@dataclass
class ClaimResult:
    success: bool
    sequence: int
    event_id: str
    record: Optional[RequestStatus] = None
    error: Optional[str] = None


@dataclass
class QueueSnapshot:
    candidates: list[RequestStatus] = field(default_factory=list)
    malformed: list[MalformedRecord] = field(default_factory=list)
    _claimed: set[int] = field(default_factory=set, repr=False)

    @property
    def pending(self) -> list[RequestStatus]:
        return [
            candidate for candidate in self.candidates
            if candidate.sequence not in self._claimed
        ]

    @property
    def pending_count(self) -> int:
        return len(self.pending)

    def claim_next(self) -> Optional[ClaimResult]:
        if not self.pending:
            return None
        return self.claim_sequence(self.pending[0].sequence)

    def claim_sequence(self, sequence: int) -> ClaimResult:
        candidates = {candidate.sequence: candidate for candidate in self.candidates}
        candidate = candidates.get(sequence)
        if candidate is None:
            return ClaimResult(
                success=False,
                sequence=sequence,
                event_id="",
                error=f"queue sequence {sequence} is not a candidate",
            )
        if sequence in self._claimed:
            return ClaimResult(
                success=False,
                sequence=sequence,
                event_id=candidate.event_id,
                error=f"queue sequence {sequence} was already claimed in this snapshot",
            )
        self._claimed.add(sequence)
        try:
            record = _claim_with_lock(sequence)
            return ClaimResult(
                success=True,
                sequence=sequence,
                event_id=record.event_id,
                record=record,
            )
        except (BlockingIOError, FileNotFoundError, ValueError) as exc:
            logger.warning("Could not claim queue sequence %s: %s", sequence, exc)
            return ClaimResult(
                success=False,
                sequence=sequence,
                event_id=candidate.event_id,
                error=str(exc),
            )


def take_snapshot() -> QueueSnapshot:
    candidates, malformed = discover_queue()
    return QueueSnapshot(candidates=candidates, malformed=malformed)
