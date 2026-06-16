# -*- coding: utf-8 -*-
"""Event submission and staging foundation.

Phase 03 — Event Submission and Staging.

This module provides:

- ``submit_event()`` — the service-layer entry point for event submission.
- Input file staging under ``incoming/<event_id>/`` with atomic replacement.
- Minimal input validation (event.xml required, station file required).
- Status flow: REGISTERED → VALIDATING → QUEUED (or VALIDATION_FAILED).
- Duplicate submission handling per contract §3.7 and §5.4.

Design constraints:

- ``event_id`` is the only public identifier — no ``run_id``.
- ``requeststatus.json`` lives under
  ``events/<event_id>/.shakemap-service/`` only — never under ``incoming/``.
- No queue worker, ShakeMap execution, product publication, or retry logic.
- Atomic staging: no partial overwrite visible to consumers.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import paths
from .status import (
    EventStatus,
    RequestStatus,
    TERMINAL_STATUSES,
    create_event_record,
    read_status,
    transition_to_queued,
    transition_to_validating,
    transition_to_validation_failed,
    write_status_atomic,
    _now_iso,
)

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Accepted input filenames (§4.2.4 MVP Input Requirements)
# ------------------------------------------------------------------

# Required: earthquake origin parameters
REQUIRED_EVENT_FILE = "event.xml"

# Required: at least one station data file in one of these formats.
# The list is extensible — add new accepted names here.
ACCEPTED_STATION_FILENAMES: frozenset[str] = frozenset({
    "stationlist.json",   # GeoJSON station observations
    "stationlist.xml",    # ShakeMap XML station observations
    "event_dat.xml",      # ShakeMap XML station observations (legacy pyfinder format)
})

# Optional inputs (accepted but not required for validation)
OPTIONAL_INPUT_FILENAMES: frozenset[str] = frozenset({
    "rupture.json",       # GeoJSON fault rupture geometry
})

# All accepted filenames for incoming submissions
ALL_ACCEPTED_FILENAMES: frozenset[str] = (
    frozenset({REQUIRED_EVENT_FILE})
    | ACCEPTED_STATION_FILENAMES
    | OPTIONAL_INPUT_FILENAMES
)


# ------------------------------------------------------------------
# Submission result
# ------------------------------------------------------------------

@dataclass
class SubmissionResult:
    """Result of a submission attempt."""

    event_id: str
    status: str
    status_path: str  # Relative path to requeststatus.json
    replaced_previous: bool = False
    validation_errors: Optional[list[str]] = None


# ------------------------------------------------------------------
# Validation
# ------------------------------------------------------------------

def validate_inputs(file_names: list[str]) -> list[str]:
    """Validate that the required input files are present.

    Returns a list of validation error messages (empty if valid).

    Per contract §4.2.4:
    - ``event.xml`` is required.
    - At least one station data file is required (one of:
      ``stationlist.json``, ``stationlist.xml``, ``event_dat.xml``).
    """
    errors: list[str] = []

    if REQUIRED_EVENT_FILE not in file_names:
        errors.append(
            f"Required file '{REQUIRED_EVENT_FILE}' is missing."
        )

    station_files_present = ACCEPTED_STATION_FILENAMES & set(file_names)
    if not station_files_present:
        accepted = ", ".join(sorted(ACCEPTED_STATION_FILENAMES))
        errors.append(
            f"At least one station data file is required. "
            f"Accepted filenames: {accepted}"
        )

    return errors


# ------------------------------------------------------------------
# Atomic staging helpers
# ------------------------------------------------------------------

def _stage_files_atomic(
    event_id: str,
    files: dict[str, bytes],
) -> Path:
    """Stage submitted files atomically under ``incoming/<event_id>/``.

    Uses write-to-temporary-then-rename to prevent partial overwrites.
    Any previous contents of ``incoming/<event_id>/`` are replaced
    atomically.

    Returns the final ``incoming/<event_id>/`` path.
    """
    target_dir = paths.event_incoming_dir(event_id)
    incoming_root = paths.incoming_dir()
    incoming_root.mkdir(parents=True, exist_ok=True)

    # Write all files to a temporary directory on the same filesystem
    # so os.rename() is atomic.
    tmp_dir = Path(tempfile.mkdtemp(
        dir=str(incoming_root),
        prefix=f".{event_id}_",
        suffix=".staging",
    ))

    try:
        for filename, content in files.items():
            file_path = tmp_dir / filename
            file_path.write_bytes(content)

        # Atomic swap: remove old target (if any), rename tmp → target.
        # On POSIX, os.rename() on directories is atomic if on same FS.
        if target_dir.exists():
            # Move old dir aside first, then rename new, then remove old.
            old_dir = Path(tempfile.mkdtemp(
                dir=str(incoming_root),
                prefix=f".{event_id}_",
                suffix=".old",
            ))
            # Remove the empty temp dir so we can rename into it.
            old_dir.rmdir()
            os.rename(str(target_dir), str(old_dir))
            try:
                os.rename(str(tmp_dir), str(target_dir))
            except BaseException:
                # Rollback: restore old dir
                os.rename(str(old_dir), str(target_dir))
                raise
            # Clean up old dir
            shutil.rmtree(str(old_dir), ignore_errors=True)
        else:
            os.rename(str(tmp_dir), str(target_dir))

    except BaseException:
        # Clean up staging temp on any failure
        shutil.rmtree(str(tmp_dir), ignore_errors=True)
        raise

    return target_dir


# ------------------------------------------------------------------
# Duplicate submission handling
# ------------------------------------------------------------------

def _handle_existing_event(
    event_id: str,
    user_id: str,
    existing: RequestStatus,
) -> RequestStatus:
    """Handle a duplicate submission for an existing event_id.

    Per contract §3.7:
    - If REGISTERED/VALIDATING/QUEUED: atomically replace inputs, reset to REGISTERED.
    - If terminal (SUCCESS/FAILED/VALIDATION_FAILED/CANCELLED/ARCHIVED):
      transition back to REGISTERED for re-processing.
    - If RUNNING: not handled in Phase 03 (no worker exists yet).
    """
    current = EventStatus(existing.status)

    if current == EventStatus.RUNNING:
        raise ValueError(
            f"Event '{event_id}' is currently RUNNING. "
            f"Duplicate submission for RUNNING events is deferred to "
            f"the queue/worker phase."
        )

    # For non-running events, reset to REGISTERED with updated submission time.
    now = _now_iso()
    existing.user_id = user_id
    existing.status = EventStatus.REGISTERED.value
    existing.submitted_at = now
    existing.validated_at = None
    existing.queued_at = None
    existing.started_at = None
    existing.completed_at = None
    existing.validation_errors = None
    existing.failure_reason = None
    existing.published_products_directory = None
    # Preserve attempt_history and max_attempts for audit trail.
    # Reset current_attempt for the new submission cycle.
    existing.current_attempt = 0

    write_status_atomic(event_id, existing)
    logger.info(
        "Duplicate submission: reset event_id=%s to REGISTERED (previous status=%s)",
        event_id, current.value,
    )
    return existing


# ------------------------------------------------------------------
# Main submission entry point
# ------------------------------------------------------------------

def submit_event(
    event_id: str,
    user_id: str,
    files: dict[str, bytes],
) -> SubmissionResult:
    """Submit an event for ShakeMap processing.

    This is the service-layer entry point for event submission.
    It performs:

    1. Creates or updates the event record (REGISTERED).
    2. Transitions to VALIDATING.
    3. Validates required inputs.
    4. If valid: stages files atomically → transitions to QUEUED.
    5. If invalid: transitions to VALIDATION_FAILED.

    Parameters
    ----------
    event_id : str
        Unique event identifier.
    user_id : str
        Identity of the requesting user/service.
    files : dict[str, bytes]
        Mapping of filename → file content bytes.

    Returns
    -------
    SubmissionResult
        Contains event_id, final status, status path reference,
        whether a previous submission was replaced, and any
        validation errors.

    Raises
    ------
    ValueError
        If event_id or user_id is empty, or if the event is RUNNING.
    """
    if not event_id or not event_id.strip():
        raise ValueError("event_id must be non-empty")
    if not user_id or not user_id.strip():
        raise ValueError("user_id must be non-empty")

    replaced_previous = False

    # Step 1: Create or update event record → REGISTERED
    existing = read_status(event_id)
    if existing is not None:
        _handle_existing_event(event_id, user_id, existing)
        replaced_previous = True
    else:
        create_event_record(event_id, user_id)

    # Step 2: Transition to VALIDATING
    transition_to_validating(event_id)

    # Step 3: Validate inputs
    file_names = list(files.keys())
    errors = validate_inputs(file_names)

    if errors:
        # Step 5 (failure): Transition to VALIDATION_FAILED
        record = transition_to_validation_failed(event_id, errors)
        return SubmissionResult(
            event_id=event_id,
            status=record.status,
            status_path=_relative_status_path(event_id),
            replaced_previous=replaced_previous,
            validation_errors=errors,
        )

    # Step 4 (success): Stage files atomically, then transition to QUEUED
    _stage_files_atomic(event_id, files)
    record = transition_to_queued(event_id)

    return SubmissionResult(
        event_id=event_id,
        status=record.status,
        status_path=_relative_status_path(event_id),
        replaced_previous=replaced_previous,
    )


def _relative_status_path(event_id: str) -> str:
    """Return the contract-relative path to requeststatus.json for an event."""
    return f"events/{event_id}/.shakemap-service/requeststatus.json"
