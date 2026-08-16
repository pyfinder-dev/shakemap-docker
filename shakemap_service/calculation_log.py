# -*- coding: utf-8 -*-
"""Append durable wrapper events to one calculation's service log."""
from __future__ import annotations

import json
import os
import stat
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import paths, status
from .directory_access import open_service_directory


SERVICE_LOG_NAME = "service.log"
PRIVATE_FILE_MODE = 0o600


@dataclass(frozen=True)
class CalculationLogEvent:
    recorded_at: str
    event_id: str
    internal_sequence: int
    phase: str
    severity: str
    message: str
    log_file: Path


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _require_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _require_current_running(record: status.CalculationRecord) -> None:
    if record.status != status.LifecycleState.RUNNING.value:
        raise ValueError("calculation record must be RUNNING")
    current = status.read_current_record(record.event_id)
    if current is None:
        raise FileNotFoundError(
            f"current calculation record for {record.event_id!r} does not exist"
        )
    if (
        current.event_id != record.event_id
        or current.internal_sequence != record.internal_sequence
    ):
        raise ValueError("current calculation record identity does not match")
    if current.status != status.LifecycleState.RUNNING.value:
        raise ValueError("current calculation record must be RUNNING")


def _open_log_file(directory_descriptor: int) -> tuple[int, bool]:
    flags = (
        os.O_WRONLY
        | os.O_APPEND
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(
            SERVICE_LOG_NAME,
            flags | os.O_CREAT | os.O_EXCL,
            PRIVATE_FILE_MODE,
            dir_fd=directory_descriptor,
        )
        created = True
    except FileExistsError:
        descriptor = os.open(
            SERVICE_LOG_NAME,
            flags,
            dir_fd=directory_descriptor,
        )
        created = False
    details = os.fstat(descriptor)
    if not stat.S_ISREG(details.st_mode):
        os.close(descriptor)
        raise ValueError("service.log is not a regular file")
    return descriptor, created


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written < 1:
            raise OSError("zero-byte write while appending service.log")
        remaining = remaining[written:]


def append_service_log(
    record: status.CalculationRecord,
    *,
    phase: str,
    severity: str,
    message: str,
) -> CalculationLogEvent:
    """Append and persist one event for the matching running calculation."""
    phase = _require_text(phase, "phase")
    severity = _require_text(severity, "severity")
    message = _require_text(message, "message")
    _require_current_running(record)

    log_file = paths.event_service_log_file(record.event_id)
    event = CalculationLogEvent(
        recorded_at=_now_iso(),
        event_id=record.event_id,
        internal_sequence=record.internal_sequence,
        phase=phase,
        severity=severity,
        message=message,
        log_file=log_file,
    )
    payload_fields = asdict(event)
    payload_fields.pop("log_file")
    payload = (
        json.dumps(
            payload_fields,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")

    logs = open_service_directory(paths.event_logs_dir(record.event_id), create=True)
    descriptor = -1
    created = False
    try:
        descriptor, created = _open_log_file(logs.descriptor)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        if created:
            os.fsync(logs.descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        logs.close()
    return event
