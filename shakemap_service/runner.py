# -*- coding: utf-8 -*-
"""Native ShakeMap subprocess execution."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
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
    service_terminated: bool = False


class ServiceShutdownError(RuntimeError):
    """Raised when native execution is requested after shutdown begins."""


@dataclass
class _TrackedProcess:
    service_terminated: bool = False
    owner_active: bool = True


_process_lock = Lock()
_launch_open = True
_active_processes: dict[subprocess.Popen, _TrackedProcess] = {}


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _prune_completed_locked() -> set[subprocess.Popen]:
    completed_processes: set[subprocess.Popen] = set()
    for process, tracked in tuple(_active_processes.items()):
        try:
            completed = process.poll() is not None
        except Exception:
            continue
        if completed:
            completed_processes.add(process)
        if completed and not tracked.owner_active:
            # Uncertain children remain owned until polling confirms completion.
            _active_processes.pop(process, None)
    return completed_processes


def open_launch_gate() -> None:
    global _launch_open
    with _process_lock:
        _prune_completed_locked()
        if _active_processes:
            _launch_open = False
            raise ServiceShutdownError(
                "native execution cannot reopen while a prior child is unresolved"
            )
        _launch_open = True


def close_and_terminate_active() -> int:
    global _launch_open
    with _process_lock:
        _launch_open = False
        completed = _prune_completed_locked()
        terminated = 0
        for process, tracked in tuple(_active_processes.items()):
            if process in completed:
                continue
            try:
                process.terminate()
            except Exception:
                continue
            tracked.service_terminated = True
            terminated += 1
        return terminated


def force_kill_active() -> int:
    with _process_lock:
        completed = _prune_completed_locked()
        killed = 0
        for process, tracked in tuple(_active_processes.items()):
            if process in completed:
                continue
            try:
                process.kill()
            except Exception:
                continue
            tracked.service_terminated = True
            killed += 1
        return killed


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
        # Spawning and registration share the shutdown lock so no child can be missed.
        with _process_lock:
            if not _launch_open:
                raise ServiceShutdownError(
                    "native execution is unavailable while the service is shutting down"
                )
            process = subprocess.Popen(
                command,
                stdout=output,
                stderr=subprocess.STDOUT,
                env=env,
                text=True,
            )
            _active_processes[process] = _TrackedProcess()
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
                with _process_lock:
                    tracked = _active_processes.get(process)
                    if tracked is not None:
                        tracked.owner_active = False
            else:
                with _process_lock:
                    _active_processes.pop(process, None)
            raise
        try:
            return_code = process.wait()
        except BaseException:
            with _process_lock:
                tracked = _active_processes.get(process)
                if tracked is not None:
                    tracked.owner_active = False
            raise
        with _process_lock:
            tracked = _active_processes.pop(process, None)
            service_terminated = (
                tracked.service_terminated if tracked is not None else False
            )
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
        service_terminated=service_terminated,
    )
