# -*- coding: utf-8 -*-
"""Raw ShakeMap subprocess and disabled managed-execution boundaries."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence


class ShakeError(RuntimeError):
    pass


@dataclass
class ExecutionResult:
    command: list[str]
    exit_code: int
    pid: int
    started_at: str
    completed_at: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def materialize_calculation(record: object) -> Path:
    """Reject native-tree materialization while queue execution is disabled."""
    raise ShakeError("calculation materialization is disabled")


def run_shake(
    event_id: str,
    modules: Sequence[str],
    *,
    log_file: Path,
    env: dict[str, str],
    on_started: Optional[Callable[[int, list[str], str], None]] = None,
) -> ExecutionResult:
    command = ["shake", event_id, *modules]
    started_at = _now_iso()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            command,
            stdout=output,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
        )
        if on_started is not None:
            on_started(process.pid, command, started_at)
        exit_code = process.wait()
    completed_at = _now_iso()
    return ExecutionResult(
        command=command,
        exit_code=exit_code,
        pid=process.pid,
        started_at=started_at,
        completed_at=completed_at,
    )


def run_shake_for_event(record: object) -> str:
    """Reject native execution while the service worker is disabled."""
    raise ShakeError("native calculation execution is disabled")
