# -*- coding: utf-8 -*-
"""Event status record model and persistence helpers.

Phase 02 — Event Record Foundation.

This module provides:

- ``EventStatus`` enum with all 9 FROZEN lifecycle status values.
- ``AttemptRecord`` and ``RequestStatus`` dataclasses matching the
  contract §3.4 ``requeststatus.json`` schema.
- Atomic write/read/update helpers for ``requeststatus.json``.
- Status transition helpers with validation of allowed transitions.
- ``scan_event_records()`` for filesystem-based discovery of existing
  event status records.

Design constraints:

- ``event_id`` is the only public identifier — no ``run_id``.
- Atomic writes use write-to-temp-then-rename (contract §3.5).
- ``requeststatus.json`` lives under
  ``events/<event_id>/.shakemap-service/`` only — never under
  ``incoming/``.
- No queue, worker, submission, or execution logic.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from . import paths

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Status enum — FROZEN contract values (§3.3)
# ------------------------------------------------------------------

class EventStatus(str, Enum):
    """FROZEN event lifecycle status values.

    Using ``str, Enum`` so values serialise directly to JSON strings.
    """

    REGISTERED = "REGISTERED"
    VALIDATING = "VALIDATING"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ARCHIVED = "ARCHIVED"


# Terminal statuses — no automatic processing continues from these.
TERMINAL_STATUSES = frozenset({
    EventStatus.VALIDATION_FAILED,
    EventStatus.SUCCESS,
    EventStatus.FAILED,
    EventStatus.CANCELLED,
    EventStatus.ARCHIVED,
})

# Allowed status transitions: target → set of allowed source statuses.
_ALLOWED_TRANSITIONS: dict[EventStatus, frozenset[EventStatus]] = {
    EventStatus.VALIDATING: frozenset({EventStatus.REGISTERED}),
    EventStatus.VALIDATION_FAILED: frozenset({EventStatus.VALIDATING}),
    EventStatus.QUEUED: frozenset({EventStatus.VALIDATING}),
    EventStatus.RUNNING: frozenset({EventStatus.QUEUED}),
    EventStatus.SUCCESS: frozenset({EventStatus.RUNNING}),
    EventStatus.FAILED: frozenset({EventStatus.RUNNING}),
    EventStatus.CANCELLED: frozenset({
        EventStatus.REGISTERED,
        EventStatus.VALIDATING,
        EventStatus.QUEUED,
        EventStatus.RUNNING,
    }),
    EventStatus.ARCHIVED: frozenset({
        EventStatus.SUCCESS,
        EventStatus.FAILED,
        EventStatus.CANCELLED,
        EventStatus.VALIDATION_FAILED,
    }),
}


# ------------------------------------------------------------------
# Data models (§3.4)
# ------------------------------------------------------------------

@dataclass
class AttemptRecord:
    """A single execution attempt record within attempt_history."""

    attempt_number: int
    started_at: str  # ISO 8601
    completed_at: Optional[str] = None  # ISO 8601 or None
    status: str = "RUNNING"  # SUCCESS, FAILED, or RUNNING
    failure_reason: Optional[str] = None
    duration_seconds: Optional[float] = None


@dataclass
class RequestStatus:
    """Authoritative event status record — ``requeststatus.json`` schema.

    All fields per contract §3.4. No ``run_id`` field.
    """

    event_id: str
    user_id: str
    status: str  # One of EventStatus values
    submitted_at: str  # ISO 8601

    validated_at: Optional[str] = None
    queued_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

    current_attempt: int = 0
    max_attempts: int = 3

    validation_errors: Optional[list[str]] = None
    failure_reason: Optional[str] = None
    published_products_directory: Optional[str] = None

    attempt_history: list[AttemptRecord] = field(default_factory=list)


# ------------------------------------------------------------------
# Timestamp helper
# ------------------------------------------------------------------

def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------
# Serialisation helpers
# ------------------------------------------------------------------

def _record_to_dict(record: RequestStatus) -> dict:
    """Convert a RequestStatus to a JSON-serialisable dict."""
    return asdict(record)


def _dict_to_record(data: dict) -> RequestStatus:
    """Reconstruct a RequestStatus from a deserialised dict.

    Handles ``attempt_history`` entries that arrive as plain dicts.
    """
    history_raw = data.pop("attempt_history", [])
    history = [
        AttemptRecord(**entry) if isinstance(entry, dict) else entry
        for entry in history_raw
    ]
    return RequestStatus(**data, attempt_history=history)


# ------------------------------------------------------------------
# Persistence: atomic write / read / update
# ------------------------------------------------------------------

def write_status_atomic(event_id: str, record: RequestStatus) -> None:
    """Write ``requeststatus.json`` atomically (write-to-temp-then-rename).

    The temp file is created in the same directory as the target to
    guarantee an atomic ``os.replace()`` on the same filesystem.

    Contract §3.5: "Writes to requeststatus.json SHOULD be atomic
    (write-to-temp-then-rename) to prevent corruption from crashes
    mid-write."
    """
    target = paths.event_status_file(event_id)
    target.parent.mkdir(parents=True, exist_ok=True)

    data = _record_to_dict(record)

    fd, tmp_path = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=".requeststatus_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(target))
    except BaseException:
        # Clean up temp file on any failure.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_status(event_id: str) -> Optional[RequestStatus]:
    """Read and deserialise ``requeststatus.json`` for an event.

    Returns ``None`` if the file does not exist.
    Raises ``ValueError`` on malformed JSON or missing required fields.
    """
    status_file = paths.event_status_file(event_id)
    if not status_file.is_file():
        return None

    text = status_file.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Malformed requeststatus.json for event '{event_id}': {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"requeststatus.json for event '{event_id}' is not a JSON object"
        )

    # Validate required fields are present.
    for required_field in ("event_id", "user_id", "status", "submitted_at"):
        if required_field not in data:
            raise ValueError(
                f"requeststatus.json for event '{event_id}' missing "
                f"required field '{required_field}'"
            )

    return _dict_to_record(data)


def update_status(event_id: str, **kwargs) -> RequestStatus:
    """Read the current record, apply field updates, and write atomically.

    Raises ``FileNotFoundError`` if no record exists for ``event_id``.
    Raises ``TypeError`` if an unknown field name is passed.
    """
    record = read_status(event_id)
    if record is None:
        raise FileNotFoundError(
            f"No requeststatus.json found for event '{event_id}'"
        )

    valid_fields = {f.name for f in record.__dataclass_fields__.values()}
    for key, value in kwargs.items():
        if key not in valid_fields:
            raise TypeError(
                f"Unknown RequestStatus field: '{key}'"
            )
        setattr(record, key, value)

    write_status_atomic(event_id, record)
    return record


# ------------------------------------------------------------------
# Event record creation
# ------------------------------------------------------------------

def create_event_record(
    event_id: str,
    user_id: str,
    max_attempts: int = 3,
) -> RequestStatus:
    """Create a new event record directory and write initial status.

    Creates ``events/<event_id>/.shakemap-service/`` and writes
    ``requeststatus.json`` with status ``REGISTERED``.

    Contract §5.3: "Before acknowledging receipt of a submission, the
    service MUST write requeststatus.json with status REGISTERED."

    Raises ``FileExistsError`` if a status file already exists for
    this ``event_id``.
    """
    status_file = paths.event_status_file(event_id)
    if status_file.is_file():
        raise FileExistsError(
            f"Event record already exists for '{event_id}': {status_file}"
        )

    record = RequestStatus(
        event_id=event_id,
        user_id=user_id,
        status=EventStatus.REGISTERED.value,
        submitted_at=_now_iso(),
        current_attempt=0,
        max_attempts=max_attempts,
    )

    write_status_atomic(event_id, record)
    logger.info("Created event record: event_id=%s, user_id=%s", event_id, user_id)
    return record


# ------------------------------------------------------------------
# Status transition helpers
# ------------------------------------------------------------------

def _validate_transition(
    current: str,
    target: EventStatus,
) -> None:
    """Raise ``ValueError`` if the transition is not allowed."""
    try:
        current_status = EventStatus(current)
    except ValueError:
        raise ValueError(
            f"Unknown current status '{current}'; cannot transition to {target.value}"
        )

    allowed_from = _ALLOWED_TRANSITIONS.get(target)
    if allowed_from is None:
        raise ValueError(f"No transition rules defined for target '{target.value}'")

    if current_status not in allowed_from:
        allowed_names = ", ".join(s.value for s in sorted(allowed_from, key=lambda s: s.value))
        raise ValueError(
            f"Cannot transition from '{current}' to '{target.value}'. "
            f"Allowed source statuses: {allowed_names}"
        )


def transition_to_validating(event_id: str) -> RequestStatus:
    """Transition event from REGISTERED → VALIDATING."""
    record = read_status(event_id)
    if record is None:
        raise FileNotFoundError(f"No record for event '{event_id}'")

    _validate_transition(record.status, EventStatus.VALIDATING)

    record.status = EventStatus.VALIDATING.value
    write_status_atomic(event_id, record)
    return record


def transition_to_validation_failed(
    event_id: str,
    errors: list[str],
) -> RequestStatus:
    """Transition event from VALIDATING → VALIDATION_FAILED."""
    record = read_status(event_id)
    if record is None:
        raise FileNotFoundError(f"No record for event '{event_id}'")

    _validate_transition(record.status, EventStatus.VALIDATION_FAILED)

    now = _now_iso()
    record.status = EventStatus.VALIDATION_FAILED.value
    record.validated_at = now
    record.completed_at = now
    record.validation_errors = errors
    write_status_atomic(event_id, record)
    return record


def transition_to_queued(event_id: str) -> RequestStatus:
    """Transition event from VALIDATING → QUEUED."""
    record = read_status(event_id)
    if record is None:
        raise FileNotFoundError(f"No record for event '{event_id}'")

    _validate_transition(record.status, EventStatus.QUEUED)

    now = _now_iso()
    record.status = EventStatus.QUEUED.value
    record.validated_at = now
    record.queued_at = now
    write_status_atomic(event_id, record)
    return record


def transition_to_running(event_id: str) -> RequestStatus:
    """Transition event from QUEUED → RUNNING.

    Increments ``current_attempt`` and appends a new ``AttemptRecord``
    to ``attempt_history`` with status ``RUNNING``.
    """
    record = read_status(event_id)
    if record is None:
        raise FileNotFoundError(f"No record for event '{event_id}'")

    _validate_transition(record.status, EventStatus.RUNNING)

    now = _now_iso()
    record.status = EventStatus.RUNNING.value
    record.started_at = now
    record.current_attempt += 1

    attempt = AttemptRecord(
        attempt_number=record.current_attempt,
        started_at=now,
        status="RUNNING",
    )
    record.attempt_history.append(attempt)

    write_status_atomic(event_id, record)
    return record


def transition_to_success(
    event_id: str,
    products_dir: Optional[str] = None,
) -> RequestStatus:
    """Transition event from RUNNING → SUCCESS.

    Completes the current attempt in ``attempt_history``.
    """
    record = read_status(event_id)
    if record is None:
        raise FileNotFoundError(f"No record for event '{event_id}'")

    _validate_transition(record.status, EventStatus.SUCCESS)

    now = _now_iso()
    record.status = EventStatus.SUCCESS.value
    record.completed_at = now
    record.published_products_directory = products_dir

    # Complete the current attempt record.
    if record.attempt_history:
        current = record.attempt_history[-1]
        current.completed_at = now
        current.status = "SUCCESS"
        if current.started_at:
            try:
                started = datetime.fromisoformat(current.started_at)
                completed = datetime.fromisoformat(now)
                current.duration_seconds = round(
                    (completed - started).total_seconds(), 3
                )
            except (ValueError, TypeError):
                current.duration_seconds = None

    write_status_atomic(event_id, record)
    return record


def transition_to_failed(
    event_id: str,
    reason: str,
) -> RequestStatus:
    """Transition event from RUNNING → FAILED.

    Completes the current attempt in ``attempt_history``.
    Sets ``failure_reason`` as the final failure reason.
    """
    record = read_status(event_id)
    if record is None:
        raise FileNotFoundError(f"No record for event '{event_id}'")

    _validate_transition(record.status, EventStatus.FAILED)

    now = _now_iso()
    record.status = EventStatus.FAILED.value
    record.completed_at = now
    record.failure_reason = reason

    # Complete the current attempt record.
    if record.attempt_history:
        current = record.attempt_history[-1]
        current.completed_at = now
        current.status = "FAILED"
        current.failure_reason = reason
        if current.started_at:
            try:
                started = datetime.fromisoformat(current.started_at)
                completed = datetime.fromisoformat(now)
                current.duration_seconds = round(
                    (completed - started).total_seconds(), 3
                )
            except (ValueError, TypeError):
                current.duration_seconds = None

    write_status_atomic(event_id, record)
    return record


def transition_to_cancelled(event_id: str) -> RequestStatus:
    """Transition event to CANCELLED from REGISTERED/VALIDATING/QUEUED/RUNNING."""
    record = read_status(event_id)
    if record is None:
        raise FileNotFoundError(f"No record for event '{event_id}'")

    _validate_transition(record.status, EventStatus.CANCELLED)

    now = _now_iso()
    record.status = EventStatus.CANCELLED.value
    record.completed_at = now

    # If there's an active attempt, complete it.
    if record.attempt_history:
        current = record.attempt_history[-1]
        if current.status == "RUNNING":
            current.completed_at = now
            current.status = "FAILED"
            current.failure_reason = "Cancelled"
            if current.started_at:
                try:
                    started = datetime.fromisoformat(current.started_at)
                    completed = datetime.fromisoformat(now)
                    current.duration_seconds = round(
                        (completed - started).total_seconds(), 3
                    )
                except (ValueError, TypeError):
                    current.duration_seconds = None

    write_status_atomic(event_id, record)
    return record


def transition_to_archived(event_id: str) -> RequestStatus:
    """Transition event to ARCHIVED from SUCCESS/FAILED/CANCELLED/VALIDATION_FAILED."""
    record = read_status(event_id)
    if record is None:
        raise FileNotFoundError(f"No record for event '{event_id}'")

    _validate_transition(record.status, EventStatus.ARCHIVED)

    record.status = EventStatus.ARCHIVED.value
    write_status_atomic(event_id, record)
    return record


# ------------------------------------------------------------------
# Filesystem scan / discovery
# ------------------------------------------------------------------

def scan_event_records() -> list[RequestStatus]:
    """Scan ``events/*/`` for existing ``requeststatus.json`` files.

    Discovers all event status records under the contract events
    directory.  Skips malformed or unreadable files with a warning log.

    Does NOT scan ``incoming/`` — contract requires status records
    only under ``events/<event_id>/.shakemap-service/``.
    """
    events_root = paths.events_dir()
    records: list[RequestStatus] = []

    if not events_root.is_dir():
        return records

    for entry in sorted(events_root.iterdir()):
        if not entry.is_dir():
            continue

        event_id = entry.name
        try:
            record = read_status(event_id)
            if record is not None:
                records.append(record)
        except ValueError as exc:
            logger.warning(
                "Skipping malformed status record for event '%s': %s",
                event_id, exc,
            )
        except Exception as exc:
            logger.warning(
                "Error reading status record for event '%s': %s",
                event_id, exc,
            )

    return records
