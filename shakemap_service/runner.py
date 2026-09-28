# -*- coding: utf-8 -*-
"""Native ShakeMap subprocess execution."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Callable, Optional

from .config import settings
from .native_launcher import REPORT_ENV, TOKEN_ENV


@dataclass
class ExecutionResult:
    command: list[str]
    exit_code: Optional[int]
    signal: Optional[int]
    pid: int
    started_at: str
    completed_at: str
    service_terminated: bool = False
    configuration_error: Optional[dict[str, object]] = None


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
    # Use an explicit launcher rather than shadowing the installed shake
    # executable. Execution evidence records the command that actually ran;
    # the launcher delegates these same event/module arguments to official main.
    command = [
        sys.executable,
        "-m",
        "shakemap_service.native_launcher",
        event_id,
        *settings.module_plan,
    ]
    report_path = log_file.with_name("native_failure.json")
    if os.path.lexists(report_path):
        raise FileExistsError(f"native failure evidence already exists: {report_path}")
    token = uuid.uuid4().hex
    child_environment = dict(env)
    child_environment.update({REPORT_ENV: str(report_path), TOKEN_ENV: token})
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
                env=child_environment,
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
    configuration_error = None
    if exit_code is not None and exit_code != 0 and not service_terminated:
        try:
            # The report is private to this invocation. Missing, malformed or
            # mismatched evidence leaves native_exit generic and cannot cause
            # caller recovery. The original log and exit status remain intact.
            if report_path.is_symlink() or report_path.stat().st_size > 16384:
                raise ValueError("invalid native failure report")
            report = json.loads(report_path.read_text(encoding="utf-8"))
            fact = report.get("configuration_error")
            if (
                report.get("event_id") == event_id
                and report.get("token") == token
                and isinstance(fact, dict)
                and fact.get("origin") in {
                    "native_config_validation",
                    "native_model_reference",
                    "native_configured_module",
                }
                and isinstance(fact.get("reference"), str)
                and isinstance(fact.get("exception_type"), str)
            ):
                configuration_error = fact
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    return ExecutionResult(
        command=command,
        exit_code=exit_code,
        signal=terminating_signal,
        pid=process.pid,
        started_at=started_at,
        completed_at=completed_at,
        service_terminated=service_terminated,
        configuration_error=configuration_error,
    )
