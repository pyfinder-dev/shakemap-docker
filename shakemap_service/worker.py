# -*- coding: utf-8 -*-
"""Disabled calculation-worker boundary."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class WorkerResult:
    claimed: bool = False
    event_id: Optional[str] = None
    sequence: Optional[int] = None
    outcome: str = "worker_disabled"
    final_status: Optional[str] = None


def execute_shakemap(record: object) -> str:
    raise RuntimeError("native calculation execution is disabled")


def run_worker_cycle(execute_fn: object = None) -> WorkerResult:
    # Queue discovery does not authorize execution.
    return WorkerResult()
