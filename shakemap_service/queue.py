# -*- coding: utf-8 -*-
"""Discovery of durably accepted waiting calculations."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .status import CalculationRecord, LifecycleState, scan_queue_records


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MalformedRecord:
    entry: str
    error: str


def discover_queue() -> tuple[list[CalculationRecord], list[MalformedRecord]]:
    records, errors = scan_queue_records()
    queued = [
        record
        for record in records
        if record.status == LifecycleState.QUEUED.value
    ]
    queued.sort(key=lambda record: record.internal_sequence)
    malformed = [MalformedRecord(entry=entry, error=error) for entry, error in errors]
    for item in malformed:
        logger.error("Malformed queue record %s: %s", item.entry, item.error)
    return queued, malformed
