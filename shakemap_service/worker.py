# -*- coding: utf-8 -*-
"""Calculation-worker entry points."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from . import calculation
from .status import CalculationRecord


@dataclass(frozen=True)
class WorkerResult:
    claimed: bool = False
    event_id: Optional[str] = None
    sequence: Optional[int] = None
    outcome: str = "worker_disabled"
    final_status: Optional[str] = None


def execute_shakemap(record: CalculationRecord) -> str:
    """Delegate one supplied calculation with a private environment copy."""
    return calculation.execute_calculation(
        record,
        base_environment=dict(os.environ),
    )


def run_worker_cycle(execute_fn: object = None) -> WorkerResult:
    # Queue discovery does not authorize execution.
    return WorkerResult()
