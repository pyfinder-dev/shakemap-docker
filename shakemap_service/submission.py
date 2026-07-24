# -*- coding: utf-8 -*-
"""Atomic durable submission snapshots for the internal FIFO queue."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import paths
from .status import (
    RequestStatus,
    _fsync_directory,
    _record_to_dict,
    new_queued_record,
)


REQUIRED_EVENT_FILE = "event.xml"
ACCEPTED_STATION_FILENAMES = frozenset({
    "stationlist.json",
    "stationlist.xml",
    "event_dat.xml",
})
OPTIONAL_INPUT_FILENAMES = frozenset({"rupture.json"})
ALL_ACCEPTED_FILENAMES = (
    frozenset({REQUIRED_EVENT_FILE})
    | ACCEPTED_STATION_FILENAMES
    | OPTIONAL_INPUT_FILENAMES
)
_EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass
class SubmissionResult:
    event_id: str
    sequence: int
    status: str
    status_path: str
    validation_errors: Optional[list[str]] = None


def validate_inputs(file_names: list[str]) -> list[str]:
    """Retain the current file-input boundary without implementing Milestone 4."""
    errors: list[str] = []
    names = set(file_names)
    if REQUIRED_EVENT_FILE not in names:
        errors.append(f"Required file '{REQUIRED_EVENT_FILE}' is missing.")
    if not ACCEPTED_STATION_FILENAMES & names:
        accepted = ", ".join(sorted(ACCEPTED_STATION_FILENAMES))
        errors.append(
            "The current pre-Milestone-4 file request requires one station file; "
            f"accepted filenames: {accepted}"
        )
    unsupported = sorted(names - ALL_ACCEPTED_FILENAMES)
    if unsupported:
        errors.append(f"Unsupported input filenames: {', '.join(unsupported)}")
    return errors


def _validate_submission(event_id: str, user_id: str, files: dict[str, bytes]) -> None:
    if not _EVENT_ID.fullmatch(event_id):
        raise ValueError(
            "event_id must be 1-128 characters using ASCII letters, digits, '.', '_' or '-'"
        )
    if not user_id or not user_id.strip():
        raise ValueError("user_id must be non-empty")
    if not files:
        raise ValueError("at least one request file is required")
    errors = validate_inputs(list(files))
    if errors:
        raise ValueError("; ".join(errors))
    for name, content in files.items():
        if Path(name).name != name or "/" in name or "\\" in name:
            raise ValueError(f"input filename is not a safe basename: {name!r}")
        if not isinstance(content, bytes):
            raise ValueError(f"input {name!r} must be bytes")


def _write_file_sync(target: Path, content: bytes) -> None:
    with target.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _write_json_sync(target: Path, data: dict) -> None:
    payload = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _write_file_sync(target, payload)


def _allocate_sequence() -> int:
    root = paths.events_dir()
    root.mkdir(parents=True, exist_ok=True)
    lock_path = paths.queue_sequence_lock_file()
    lock_path.touch(exist_ok=True)
    with lock_path.open("r+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = paths.queue_sequence_file()
        existing = []
        for entry in root.iterdir():
            if entry.name.startswith("."):
                continue
            try:
                existing.append(paths.parse_queue_entry_name(entry.name))
            except ValueError:
                continue
        filesystem_next = max(existing, default=0) + 1
        if state.exists():
            text = state.read_text(encoding="ascii").strip()
            if not text.isascii() or not text.isdigit() or int(text) < 1:
                raise ValueError(f"malformed queue sequence state: {state}")
            sequence = max(int(text), filesystem_next)
        else:
            sequence = filesystem_next
        temporary = state.with_name(f".next-sequence.{uuid.uuid4().hex}.tmp")
        _write_file_sync(temporary, f"{sequence + 1}\n".encode("ascii"))
        os.replace(temporary, state)
        _fsync_directory(root)
        return sequence


def submit_event(
    event_id: str,
    user_id: str,
    files: dict[str, bytes],
    *,
    kind: str = "calculation",
    requested_region: Optional[str] = None,
    module_plan: Optional[list[str]] = None,
) -> SubmissionResult:
    """Durably accept one distinct queue entry.

    A submission is acknowledged only after its complete request snapshot and
    status record become visible together through an atomic directory rename.
    Sequence gaps are permitted after interrupted acceptance; ordering never
    depends on wall-clock timestamps.
    """
    _validate_submission(event_id, user_id, files)
    sequence = _allocate_sequence()
    record = new_queued_record(
        event_id=event_id,
        sequence=sequence,
        user_id=user_id,
        kind=kind,
        requested_region=requested_region,
        module_plan=module_plan,
    )
    root = paths.events_dir()
    final = paths.queue_entry_dir(sequence)
    temporary = root / f".pending-{paths.queue_entry_name(sequence)}-{uuid.uuid4().hex}"
    try:
        request = temporary / "request"
        request.mkdir(parents=True, exist_ok=False)
        manifest_files = []
        for name in sorted(files):
            _write_file_sync(request / name, files[name])
            manifest_files.append({
                "name": name,
                "size_bytes": len(files[name]),
                "sha256": hashlib.sha256(files[name]).hexdigest(),
            })
        _fsync_directory(request)
        _write_json_sync(temporary / "request-manifest.json", {
            "schema_version": 1,
            "event_id": event_id,
            "sequence": sequence,
            "files": manifest_files,
        })
        _write_json_sync(temporary / "status.json", _record_to_dict(record))
        _write_file_sync(temporary / "claim.lock", b"")
        _fsync_directory(temporary)
        os.rename(temporary, final)
        _fsync_directory(root)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return SubmissionResult(
        event_id=event_id,
        sequence=sequence,
        status=record.status,
        status_path=f".service/events/{paths.queue_entry_name(sequence)}/status.json",
    )
