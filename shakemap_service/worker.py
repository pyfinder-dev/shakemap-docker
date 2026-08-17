# -*- coding: utf-8 -*-
"""Calculation-worker entry points."""
from __future__ import annotations

import os

from . import calculation
from .status import CalculationRecord


def execute_shakemap(record: CalculationRecord) -> str:
    """Delegate one supplied calculation with a private environment copy."""
    return calculation.execute_calculation(
        record,
        base_environment=dict(os.environ),
    )
