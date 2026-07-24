# -*- coding: utf-8 -*-
"""Versioned durable queue status records and lifecycle transitions."""
from __future__ import annotations

import json
import hashlib
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from . import paths


STATUS_SCHEMA_VERSION = 1
STATUS_KINDS = frozenset({"calculation", "operation"})


class EventStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATUSES = frozenset({
    EventStatus.SUCCESS,
    EventStatus.FAILED,
    EventStatus.CANCELLED,
})

_ALLOWED_TRANSITIONS = {
    EventStatus.QUEUED: frozenset(),
    EventStatus.RUNNING: frozenset({EventStatus.QUEUED}),
    EventStatus.SUCCESS: frozenset({EventStatus.RUNNING}),
    EventStatus.FAILED: frozenset({EventStatus.QUEUED, EventStatus.RUNNING}),
    EventStatus.CANCELLED: frozenset({EventStatus.QUEUED, EventStatus.RUNNING}),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def mounted_paths_for(event_id: str, sequence: int) -> dict[str, str]:
    return {
        "service_root": str(paths.service_root()),
        "queue_entry": str(paths.queue_entry_dir(sequence)),
        "queued_request": str(paths.queue_request_dir(sequence)),
        "calculation": str(paths.event_products_dir(event_id)),
        "request": str(paths.event_request_dir(event_id)),
        "effective": str(paths.event_effective_dir(event_id)),
        "profile": str(paths.event_profile_dir(event_id)),
        "current": str(paths.event_current_dir(event_id)),
        "native_products": str(paths.event_native_products_dir(event_id)),
        "logs": str(paths.event_logs_dir(event_id)),
        "status": str(paths.event_status_file(event_id)),
        "metadata": str(paths.event_metadata_file(event_id)),
        "product_manifest": str(paths.event_manifest_file(event_id)),
    }


@dataclass
class RequestStatus:
    schema_version: int
    kind: str
    event_id: str
    sequence: int
    user_id: str
    status: str
    submitted_at: str
    queued_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    requested_region: Optional[str] = None
    effective_region: Optional[str] = None
    module_plan: Optional[list[str]] = None
    current_child: Optional[dict[str, Any]] = None
    failure: Optional[dict[str, Any]] = None
    interruption: Optional[dict[str, Any]] = None
    mounted_paths: dict[str, str] = field(default_factory=dict)


def new_queued_record(
    *,
    event_id: str,
    sequence: int,
    user_id: str,
    kind: str = "calculation",
    requested_region: Optional[str] = None,
    module_plan: Optional[list[str]] = None,
) -> RequestStatus:
    now = _now_iso()
    return RequestStatus(
        schema_version=STATUS_SCHEMA_VERSION,
        kind=kind,
        event_id=event_id,
        sequence=sequence,
        user_id=user_id,
        status=EventStatus.QUEUED.value,
        submitted_at=now,
        queued_at=now,
        requested_region=requested_region,
        module_plan=list(module_plan) if module_plan is not None else None,
        mounted_paths=mounted_paths_for(event_id, sequence),
    )


def _record_to_dict(record: RequestStatus) -> dict[str, Any]:
    return asdict(record)


def _dict_to_record(data: dict[str, Any]) -> RequestStatus:
    if not isinstance(data, dict):
        raise ValueError("status record is not a JSON object")
    try:
        record = RequestStatus(**data)
    except TypeError as exc:
        raise ValueError(f"status record fields are invalid: {exc}") from exc
    _validate_record(record)
    return record


def _validate_record(record: RequestStatus, expected_sequence: Optional[int] = None) -> None:
    if record.schema_version != STATUS_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported status schema_version {record.schema_version!r}; "
            f"expected {STATUS_SCHEMA_VERSION}"
        )
    if record.kind not in STATUS_KINDS:
        raise ValueError(f"invalid status kind {record.kind!r}")
    if not record.event_id or not isinstance(record.event_id, str):
        raise ValueError("event_id must be a non-empty string")
    if isinstance(record.sequence, bool) or not isinstance(record.sequence, int) or record.sequence < 1:
        raise ValueError("sequence must be a positive integer")
    if expected_sequence is not None and record.sequence != expected_sequence:
        raise ValueError(
            f"status sequence {record.sequence} does not match queue entry {expected_sequence}"
        )
    try:
        EventStatus(record.status)
    except ValueError as exc:
        raise ValueError(f"invalid lifecycle status {record.status!r}") from exc
    if not isinstance(record.mounted_paths, dict):
        raise ValueError("mounted_paths must be an object")


def _fsync_directory(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_json_atomic(target: Path, data: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def write_status_atomic(sequence: int, record: RequestStatus) -> None:
    _validate_record(record, expected_sequence=sequence)
    write_json_atomic(paths.queue_status_file(sequence), _record_to_dict(record))


def write_calculation_status(record: RequestStatus) -> None:
    """Mirror status into the calculation only when this entry owns it."""
    metadata = paths.event_metadata_file(record.event_id)
    if not metadata.is_file():
        return
    try:
        owner = json.loads(metadata.read_text(encoding="utf-8")).get("queue_sequence")
    except (OSError, json.JSONDecodeError):
        return
    if owner == record.sequence:
        write_json_atomic(paths.event_status_file(record.event_id), _record_to_dict(record))


def read_status(sequence: int) -> Optional[RequestStatus]:
    target = paths.queue_status_file(sequence)
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed status record for sequence {sequence}: {exc}") from exc
    record = _dict_to_record(data)
    _validate_record(record, expected_sequence=sequence)
    return record


def update_status(sequence: int, **changes: Any) -> RequestStatus:
    record = read_status(sequence)
    if record is None:
        raise FileNotFoundError(f"queue status for sequence {sequence} does not exist")
    for name, value in changes.items():
        if not hasattr(record, name):
            raise ValueError(f"unknown RequestStatus field {name!r}")
        setattr(record, name, value)
    _validate_record(record, expected_sequence=sequence)
    write_status_atomic(sequence, record)
    write_calculation_status(record)
    return record


def transition_status(
    sequence: int,
    target: EventStatus,
    *,
    failure: Optional[dict[str, Any]] = None,
    interruption: Optional[dict[str, Any]] = None,
    current_child: Optional[dict[str, Any]] = None,
) -> RequestStatus:
    record = read_status(sequence)
    if record is None:
        raise FileNotFoundError(f"queue status for sequence {sequence} does not exist")
    source = EventStatus(record.status)
    if source not in _ALLOWED_TRANSITIONS[target]:
        raise ValueError(f"invalid status transition {source.value} -> {target.value}")
    now = _now_iso()
    record.status = target.value
    if target == EventStatus.RUNNING:
        record.started_at = now
        record.current_child = current_child
    if target in TERMINAL_STATUSES:
        record.completed_at = now
        record.current_child = current_child
    record.failure = failure
    record.interruption = interruption
    write_status_atomic(sequence, record)
    write_calculation_status(record)
    return record


def transition_to_running(sequence: int) -> RequestStatus:
    return transition_status(sequence, EventStatus.RUNNING)


def transition_to_success(sequence: int) -> RequestStatus:
    return transition_status(sequence, EventStatus.SUCCESS)


def transition_to_failed(
    sequence: int,
    reason: str,
    *,
    code: str = "execution_failed",
    interruption: Optional[dict[str, Any]] = None,
    current_child: Optional[dict[str, Any]] = None,
) -> RequestStatus:
    return transition_status(
        sequence,
        EventStatus.FAILED,
        failure={"code": code, "message": reason},
        interruption=interruption,
        current_child=current_child,
    )


def transition_to_cancelled(sequence: int, reason: str) -> RequestStatus:
    return transition_status(
        sequence,
        EventStatus.CANCELLED,
        failure={"code": "cancelled", "message": reason},
    )


def scan_event_records() -> tuple[list[RequestStatus], list[tuple[str, str]]]:
    records: list[RequestStatus] = []
    malformed: list[tuple[str, str]] = []
    root = paths.events_dir()
    if not root.is_dir():
        return records, malformed
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if entry.name.startswith("."):
            continue
        if not entry.is_dir():
            malformed.append((entry.name, "queue entry is not a directory"))
            continue
        try:
            sequence = paths.parse_queue_entry_name(entry.name)
            record = read_status(sequence)
            if record is None:
                raise ValueError("status.json is missing")
            request = paths.queue_request_dir(sequence)
            if not request.is_dir():
                raise ValueError("request snapshot directory is missing")
            manifest_path = entry / "request-manifest.json"
            if not manifest_path.is_file():
                raise ValueError("request-manifest.json is missing")
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"request manifest is malformed: {exc}") from exc
            if (
                manifest.get("schema_version") != 1
                or manifest.get("event_id") != record.event_id
                or manifest.get("sequence") != sequence
                or not isinstance(manifest.get("files"), list)
            ):
                raise ValueError("request manifest identity or schema is invalid")
            expected_names = []
            for item in manifest["files"]:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                    raise ValueError("request manifest file entry is invalid")
                file_path = request / item["name"]
                if not file_path.is_file():
                    raise ValueError(f"request snapshot file is missing: {item['name']}")
                content = file_path.read_bytes()
                if len(content) != item.get("size_bytes"):
                    raise ValueError(f"request snapshot size differs: {item['name']}")
                if hashlib.sha256(content).hexdigest() != item.get("sha256"):
                    raise ValueError(f"request snapshot checksum differs: {item['name']}")
                expected_names.append(item["name"])
            actual_names = sorted(item.name for item in request.iterdir() if item.is_file())
            if sorted(expected_names) != actual_names:
                raise ValueError("request snapshot contains unmanifested files")
            records.append(record)
        except (OSError, ValueError) as exc:
            malformed.append((entry.name, str(exc)))
    records.sort(key=lambda item: item.sequence)
    return records, malformed


def records_for_event(event_id: str) -> list[RequestStatus]:
    records, _ = scan_event_records()
    return [record for record in records if record.event_id == event_id]


def latest_status_for_event(event_id: str) -> Optional[RequestStatus]:
    records = records_for_event(event_id)
    return records[-1] if records else None


def find_stale_running() -> list[RequestStatus]:
    records, _ = scan_event_records()
    return [record for record in records if record.status == EventStatus.RUNNING.value]


def fail_interrupted(sequence: int) -> RequestStatus:
    record = read_status(sequence)
    if record is None:
        raise FileNotFoundError(f"queue status for sequence {sequence} does not exist")
    evidence = {
        "detected_at": _now_iso(),
        "reason": "service restarted while the queue entry was RUNNING",
        "previous_child": record.current_child,
        "automatic_retry": False,
    }
    return transition_to_failed(
        sequence,
        "Interrupted by service restart; automatic retry is disabled",
        code="interrupted_on_restart",
        interruption=evidence,
        current_child=record.current_child,
    )
