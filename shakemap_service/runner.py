# -*- coding: utf-8 -*-
"""Native ShakeMap subprocess execution."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .config import settings


@dataclass
class ExecutionResult:
    command: list[str]
    exit_code: Optional[int]
    signal: Optional[int]
    pid: int
    started_at: str
    completed_at: str


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def run_shake(
    event_id: str,
    *,
    log_file: Path,
    env: dict[str, str],
    on_started: Optional[Callable[[int, list[str], str], None]] = None,
) -> ExecutionResult:
    command = ["shake", event_id, *settings.module_plan]
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("w", encoding="utf-8") as output:
        started_at = _now_iso()
        process = subprocess.Popen(
            command,
            stdout=output,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
        )
        try:
            if on_started is not None:
                on_started(process.pid, command, started_at)
        except BaseException:
            try:
                process.terminate()
            except BaseException:
                pass
            try:
                process.wait()
            except BaseException:
                pass
            raise
        return_code = process.wait()
        completed_at = _now_iso()
    exit_code = return_code if return_code >= 0 else None
    terminating_signal = -return_code if return_code < 0 else None
    return ExecutionResult(
        command=command,
        exit_code=exit_code,
        signal=terminating_signal,
        pid=process.pid,
        started_at=started_at,
        completed_at=completed_at,
    )
