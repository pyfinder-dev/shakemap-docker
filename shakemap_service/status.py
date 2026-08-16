# -*- coding: utf-8 -*-
"""Durable calculation records and lifecycle transition rules."""
from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from . import paths
from .config import settings
from .directory_access import (
    DirectoryHandle,
    directory_open_flags,
    open_service_directory,
)
from .request_validation import (
    validate_configuration_name,
    validate_event_id,
    validate_upload_basename,
)


STATUS_SCHEMA_VERSION = 1


class LifecycleState(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class RunningRecordRecoveryResult(str, Enum):
    MISSING = "missing"
    IDENTITY_MISMATCH = "identity_mismatch"
    BLOCKING_CHILD_PRESENT = "blocking_child_present"
    NOT_RUNNING = "not_running"
    FAILED = "failed"


TERMINAL_STATES = frozenset({LifecycleState.SUCCESS, LifecycleState.FAILED})

OPERATIONAL_PHASES = frozenset(
    {
        "preceding_tree_disposition",
        "calculation_preparation",
        "native_execution",
        "product_validation",
        "record_finalization",
    }
)

_RFC3339_UTC_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z$"
)

_ALLOWED_TRANSITIONS = {
    LifecycleState.QUEUED: frozenset({LifecycleState.RUNNING}),
    LifecycleState.RUNNING: frozenset(
        {LifecycleState.SUCCESS, LifecycleState.FAILED}
    ),
    LifecycleState.SUCCESS: frozenset(),
    LifecycleState.FAILED: frozenset(),
}


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def shared_paths_for(event_id: str) -> dict[str, Optional[str]]:
    validate_event_id(event_id)
    return {
        "input": str(paths.shared_event_input_dir(event_id)),
        "products": str(paths.shared_event_native_products_dir(event_id)),
        "provenance": None,
        "product_manifest": None,
        "service_log": None,
        "shake_log": None,
    }


@dataclass
class CalculationRecord:
    schema_version: int
    event_id: str
    internal_sequence: int
    status: str
    overwrite: bool
    warnings: list[str]
    request: dict[str, Any]
    configuration: dict[str, Any]
    progress: dict[str, Any]
    native_outcome: Optional[dict[str, Any]]
    service_outcome: Optional[dict[str, Any]]
    failure: Optional[dict[str, Any]]
    timestamps: dict[str, Optional[str]]
    shared_paths: dict[str, Optional[str]]


def new_queued_record(
    *,
    event_id: str,
    internal_sequence: int,
    selected_configuration: str,
    overwrite: bool,
    warnings: list[str],
    input_mode: str,
) -> CalculationRecord:
    submitted_at = _now_iso()
    return CalculationRecord(
        schema_version=STATUS_SCHEMA_VERSION,
        event_id=event_id,
        internal_sequence=internal_sequence,
        status=LifecycleState.QUEUED.value,
        overwrite=overwrite,
        warnings=list(warnings),
        request={
            "snapshot": "request",
            "manifest": "request-manifest.json",
            "input_mode": input_mode,
        },
        configuration={"selected": selected_configuration},
        progress={
            "phase": None,
            "phase_started_at": None,
            "module_plan": list(settings.module_plan),
            "current_module": None,
            "completed_modules": [],
        },
        native_outcome=None,
        service_outcome=None,
        failure=None,
        timestamps={
            "submitted_at": submitted_at,
            "started_at": None,
            "native_started_at": None,
            "native_finished_at": None,
            "completed_at": None,
        },
        shared_paths=shared_paths_for(event_id),
    )


def _record_to_dict(record: CalculationRecord) -> dict[str, Any]:
    return asdict(record)


def _dict_to_record(data: object) -> CalculationRecord:
    if not isinstance(data, dict):
        raise ValueError("calculation record is not a JSON object")
    try:
        record = CalculationRecord(**data)
    except TypeError as exc:
        raise ValueError(f"calculation record fields are invalid: {exc}") from exc
    _validate_record(record)
    return record


def _require_exact_keys(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    if set(value) != keys:
        raise ValueError(f"{label} fields are invalid")
    return value


def _validate_optional_text(value: object, label: str) -> None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{label} must be a string or null")


def _validate_timestamp(value: object, label: str, *, required: bool) -> None:
    if value is None and not required:
        return
    if not isinstance(value, str) or _RFC3339_UTC_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be an RFC 3339 UTC timestamp ending in Z")
    try:
        datetime.fromisoformat(value[:-1])
    except ValueError as exc:
        raise ValueError(f"{label} is not a valid timestamp") from exc


def _validate_record(
    record: CalculationRecord,
    expected_sequence: Optional[int] = None,
    expected_event_id: Optional[str] = None,
) -> None:
    if record.schema_version != STATUS_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported schema_version {record.schema_version!r}; "
            f"expected {STATUS_SCHEMA_VERSION}"
        )
    validate_event_id(record.event_id)
    if (
        isinstance(record.internal_sequence, bool)
        or not isinstance(record.internal_sequence, int)
        or record.internal_sequence < 1
    ):
        raise ValueError("internal_sequence must be a positive integer")
    if expected_sequence is not None and record.internal_sequence != expected_sequence:
        raise ValueError(
            f"record sequence {record.internal_sequence} does not match "
            f"queue entry {expected_sequence}"
        )
    if expected_event_id is not None and record.event_id != expected_event_id:
        raise ValueError(
            f"current record event_id {record.event_id!r} does not match "
            f"{expected_event_id!r}"
        )
    try:
        state = LifecycleState(record.status)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid lifecycle state {record.status!r}") from exc
    if not isinstance(record.overwrite, bool):
        raise ValueError("overwrite must be a boolean")
    if not isinstance(record.warnings, list) or not all(
        isinstance(item, str) for item in record.warnings
    ):
        raise ValueError("warnings must be a list of strings")

    request = _require_exact_keys(
        record.request,
        {"snapshot", "manifest", "input_mode"},
        "request",
    )
    if request["snapshot"] != "request" or request["manifest"] != "request-manifest.json":
        raise ValueError("request snapshot paths are invalid")
    if request["input_mode"] not in {"directory", "upload", "mixed"}:
        raise ValueError("request input_mode is invalid")

    configuration = _require_exact_keys(
        record.configuration,
        {"selected"},
        "configuration",
    )
    validate_configuration_name(configuration["selected"])

    progress = _require_exact_keys(
        record.progress,
        {
            "phase",
            "phase_started_at",
            "module_plan",
            "current_module",
            "completed_modules",
        },
        "progress",
    )
    _validate_optional_text(progress["phase"], "progress.phase")
    if (
        progress["phase"] is not None
        and progress["phase"] not in OPERATIONAL_PHASES
    ):
        raise ValueError("progress.phase is not an approved operational phase")
    _validate_timestamp(
        progress["phase_started_at"],
        "progress.phase_started_at",
        required=False,
    )
    _validate_optional_text(progress["current_module"], "progress.current_module")
    if not isinstance(progress["module_plan"], list) or not all(
        isinstance(item, str) for item in progress["module_plan"]
    ):
        raise ValueError("progress.module_plan must be a list of strings")
    if not isinstance(progress["completed_modules"], list) or not all(
        isinstance(item, str) for item in progress["completed_modules"]
    ):
        raise ValueError("progress.completed_modules must be a list of strings")
    if progress["module_plan"] != list(settings.module_plan):
        raise ValueError("progress.module_plan differs from the fixed native plan")
    if (
        progress["current_module"] is not None
        and progress["current_module"] not in progress["module_plan"]
    ):
        raise ValueError("progress.current_module is not in the native plan")
    if any(
        item not in progress["module_plan"]
        for item in progress["completed_modules"]
    ):
        raise ValueError("progress.completed_modules contains an unknown module")

    timestamps = _require_exact_keys(
        record.timestamps,
        {
            "submitted_at",
            "started_at",
            "native_started_at",
            "native_finished_at",
            "completed_at",
        },
        "timestamps",
    )
    _validate_timestamp(
        timestamps["submitted_at"],
        "timestamps.submitted_at",
        required=True,
    )
    for name in (
        "started_at",
        "native_started_at",
        "native_finished_at",
        "completed_at",
    ):
        _validate_timestamp(
            timestamps[name],
            f"timestamps.{name}",
            required=False,
        )
    if state == LifecycleState.QUEUED and (
        timestamps["started_at"] is not None
        or timestamps["completed_at"] is not None
    ):
        raise ValueError("QUEUED records cannot have start or completion timestamps")
    if state != LifecycleState.QUEUED and timestamps["started_at"] is None:
        raise ValueError(f"{state.value} records require a start timestamp")
    if state in TERMINAL_STATES and timestamps["completed_at"] is None:
        raise ValueError(f"{state.value} records require a completion timestamp")
    if state == LifecycleState.RUNNING and timestamps["completed_at"] is not None:
        raise ValueError("RUNNING records cannot have a completion timestamp")

    for label, value in (
        ("native_outcome", record.native_outcome),
        ("service_outcome", record.service_outcome),
        ("failure", record.failure),
    ):
        if value is not None and not isinstance(value, dict):
            raise ValueError(f"{label} must be an object or null")
    if state == LifecycleState.SUCCESS and record.failure is not None:
        raise ValueError("SUCCESS records cannot contain a failure")
    if state == LifecycleState.FAILED and record.failure is None:
        raise ValueError("FAILED records require failure evidence")
    if state == LifecycleState.QUEUED:
        if any(
            progress[name] is not None
            for name in ("phase", "phase_started_at", "current_module")
        ) or progress["completed_modules"]:
            raise ValueError("QUEUED records cannot contain execution progress")
        if any(
            value is not None
            for value in (record.native_outcome, record.service_outcome, record.failure)
        ):
            raise ValueError("QUEUED records cannot contain execution outcomes")
    else:
        if progress["phase"] is None or progress["phase_started_at"] is None:
            raise ValueError(
                f"{state.value} records require an operational phase and start timestamp"
            )
    if state == LifecycleState.FAILED:
        failure_phase = record.failure.get("phase")
        if failure_phase != progress["phase"]:
            raise ValueError("FAILED record phase and failure.phase must match")

    shared_paths = _require_exact_keys(
        record.shared_paths,
        {
            "input",
            "products",
            "provenance",
            "product_manifest",
            "service_log",
            "shake_log",
        },
        "shared_paths",
    )
    for name, value in shared_paths.items():
        _validate_optional_text(value, f"shared_paths.{name}")
    expected_paths = shared_paths_for(record.event_id)
    if shared_paths["input"] != expected_paths["input"]:
        raise ValueError("shared_paths.input does not match event_id")
    if shared_paths["products"] != expected_paths["products"]:
        raise ValueError("shared_paths.products does not match event_id")
    service_root = paths.shared_service_root() / ".service" / "events" / record.event_id
    expected_service_paths = {
        "provenance": str(service_root / "provenance.json"),
        "product_manifest": str(service_root / "product-manifest.json"),
        "service_log": str(service_root / "logs" / "service.log"),
        "shake_log": str(service_root / "logs" / "shake.log"),
    }
    for name, expected in expected_service_paths.items():
        if shared_paths[name] not in {None, expected}:
            raise ValueError(f"shared_paths.{name} does not match event_id")


def fsync_directory(directory: Path) -> None:
    """Persist directory-entry changes or propagate the durability failure."""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass
class _RecordDirectoryAccess:
    path: Path
    descriptor: int

    def close(self) -> None:
        fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        os.close(self.descriptor)


def _open_record_directory(
    record_directory: Path,
    *,
    exclusive: bool,
    parent_descriptor: Optional[int] = None,
    entry_name: Optional[str] = None,
) -> Optional[_RecordDirectoryAccess]:
    try:
        if parent_descriptor is None:
            descriptor = open_service_directory(
                record_directory,
                create=False,
            ).descriptor
        else:
            if entry_name is None:
                raise ValueError("record entry name is required with a parent descriptor")
            descriptor = os.open(
                entry_name,
                directory_open_flags(),
                dir_fd=parent_descriptor,
            )
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"record directory is missing or unsafe: {record_directory}: {exc}"
        ) from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISDIR(details.st_mode):
            raise ValueError(f"record path is not a directory: {record_directory}")
        fcntl.flock(
            descriptor,
            fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
        )
        return _RecordDirectoryAccess(
            path=record_directory,
            descriptor=descriptor,
        )
    except BaseException:
        os.close(descriptor)
        raise


def _load_record_from_directory(
    access: _RecordDirectoryAccess,
    *,
    expected_sequence: Optional[int],
    expected_event_id: Optional[str] = None,
) -> CalculationRecord:
    descriptor, _ = _open_regular_child(
        access.descriptor,
        "status.json",
        "status.json",
    )
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            data = json.load(stream)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"malformed calculation record in {access.path}: {exc}"
        ) from exc
    record = _dict_to_record(data)
    _validate_record(
        record,
        expected_sequence=expected_sequence,
        expected_event_id=expected_event_id,
    )
    return record


def _read_record_directory(
    record_directory: Path,
    *,
    expected_sequence: Optional[int],
    expected_event_id: Optional[str] = None,
    parent_descriptor: Optional[int] = None,
    entry_name: Optional[str] = None,
) -> Optional[CalculationRecord]:
    access = _open_record_directory(
        record_directory,
        exclusive=False,
        parent_descriptor=parent_descriptor,
        entry_name=entry_name,
    )
    if access is None:
        return None
    try:
        return _load_record_from_directory(
            access,
            expected_sequence=expected_sequence,
            expected_event_id=expected_event_id,
        )
    finally:
        access.close()


def _replace_record_in_directory(
    access: _RecordDirectoryAccess,
    record: CalculationRecord,
    *,
    expected_sequence: Optional[int],
    expected_event_id: Optional[str] = None,
) -> None:
    _validate_record(
        record,
        expected_sequence=expected_sequence,
        expected_event_id=expected_event_id,
    )
    temporary_name = f".status-{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary_name,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=access.descriptor,
    )
    replaced = False
    try:
        payload = (
            json.dumps(_record_to_dict(record), indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise OSError("zero-byte write while publishing status.json")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary_name,
            "status.json",
            src_dir_fd=access.descriptor,
            dst_dir_fd=access.descriptor,
        )
        replaced = True
        # Fsync after replace makes the status.json directory entry durable.
        os.fsync(access.descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not replaced:
            try:
                os.unlink(temporary_name, dir_fd=access.descriptor)
            except OSError:
                pass


def read_status(sequence: int) -> Optional[CalculationRecord]:
    return _read_record_directory(
        paths.queue_entry_dir(sequence),
        expected_sequence=sequence,
    )


def read_current_record(event_id: str) -> Optional[CalculationRecord]:
    validate_event_id(event_id)
    return _read_record_directory(
        paths.event_service_dir(event_id),
        expected_sequence=None,
        expected_event_id=event_id,
    )


def read_archived_record(
    service_directory: Path,
    *,
    expected_event_id: str,
) -> Optional[CalculationRecord]:
    """Read one retained service record with its calculation identity checked."""
    validate_event_id(expected_event_id)
    service_directory = Path(service_directory)
    if (
        service_directory.name != "service"
        or service_directory.parent.parent != paths.archive_dir()
    ):
        raise ValueError("archived service record path is invalid")
    access = _open_record_directory(
        service_directory,
        exclusive=False,
    )
    if access is None:
        return None
    try:
        record = _load_record_from_directory(
            access,
            expected_sequence=None,
            expected_event_id=expected_event_id,
        )
        _load_request_manifest_identity(access, record)
        return record
    finally:
        access.close()


_UNSET = object()


def _update_record_directory(
    record_directory: Path,
    *,
    expected_sequence: Optional[int],
    expected_event_id: Optional[str],
    missing_message: str,
    changes: dict[str, Any],
) -> CalculationRecord:
    forbidden = {"schema_version", "event_id", "internal_sequence", "status"}
    if forbidden & set(changes):
        raise ValueError("identity and lifecycle fields require their dedicated operations")
    access = _open_record_directory(
        record_directory,
        exclusive=True,
    )
    if access is None:
        raise FileNotFoundError(missing_message)
    try:
        record = _load_record_from_directory(
            access,
            expected_sequence=expected_sequence,
            expected_event_id=expected_event_id,
        )
        for name, value in changes.items():
            if not hasattr(record, name):
                raise ValueError(f"unknown calculation-record field {name!r}")
            setattr(record, name, value)
        _replace_record_in_directory(
            access,
            record,
            expected_sequence=expected_sequence,
            expected_event_id=expected_event_id,
        )
        return record
    finally:
        access.close()


def update_status(sequence: int, **changes: Any) -> CalculationRecord:
    return _update_record_directory(
        paths.queue_entry_dir(sequence),
        expected_sequence=sequence,
        expected_event_id=None,
        missing_message=f"calculation record for sequence {sequence} does not exist",
        changes=changes,
    )


def update_current_record(event_id: str, **changes: Any) -> CalculationRecord:
    validate_event_id(event_id)
    return _update_record_directory(
        paths.event_service_dir(event_id),
        expected_sequence=None,
        expected_event_id=event_id,
        missing_message=f"current calculation record for {event_id!r} does not exist",
        changes=changes,
    )


def _transition_record_directory(
    record_directory: Path,
    target: LifecycleState,
    *,
    expected_sequence: Optional[int],
    expected_event_id: Optional[str],
    missing_message: str,
    progress: object = _UNSET,
    native_outcome: object = _UNSET,
    service_outcome: object = _UNSET,
    failure: object = _UNSET,
    terminal_timestamp: object = _UNSET,
    shared_paths: object = _UNSET,
) -> CalculationRecord:
    if not isinstance(target, LifecycleState):
        raise ValueError("target must be a LifecycleState")
    access = _open_record_directory(
        record_directory,
        exclusive=True,
    )
    if access is None:
        raise FileNotFoundError(missing_message)
    try:
        record = _load_record_from_directory(
            access,
            expected_sequence=expected_sequence,
            expected_event_id=expected_event_id,
        )
        source = LifecycleState(record.status)
        # The directory lock covers transition read/check/write for service writers.
        if target not in _ALLOWED_TRANSITIONS[source]:
            raise ValueError(
                f"invalid lifecycle transition {source.value} -> {target.value}"
            )
        record.status = target.value
        if progress is not _UNSET:
            record.progress = progress  # type: ignore[assignment]
        if target == LifecycleState.RUNNING:
            started_at = _now_iso()
            record.timestamps["started_at"] = started_at
            if not isinstance(record.progress, dict):
                raise ValueError("progress must be an object")
            initial_progress = dict(record.progress)
            initial_progress.update(
                {
                    "phase": "preceding_tree_disposition",
                    "phase_started_at": started_at,
                    "current_module": None,
                    "completed_modules": [],
                }
            )
            record.progress = initial_progress
        completed_at: object = _UNSET
        if target in TERMINAL_STATES:
            completed_at = (
                _now_iso()
                if terminal_timestamp is _UNSET
                else terminal_timestamp
            )
            _validate_timestamp(
                completed_at,
                "timestamps.completed_at",
                required=True,
            )
            record.timestamps["completed_at"] = completed_at
        if native_outcome is not _UNSET:
            record.native_outcome = native_outcome  # type: ignore[assignment]
        if service_outcome is not _UNSET:
            record.service_outcome = service_outcome  # type: ignore[assignment]
        if failure is not _UNSET:
            record.failure = failure  # type: ignore[assignment]
        if target == LifecycleState.FAILED and isinstance(record.failure, dict):
            normalized_failure = dict(record.failure)
            failed_phase = normalized_failure.get("phase")
            recorded_phase = record.progress.get("phase")
            if failed_phase is None:
                normalized_failure["phase"] = recorded_phase
            elif failed_phase != recorded_phase:
                failed_progress = dict(record.progress)
                failed_progress["phase"] = failed_phase
                failed_progress["phase_started_at"] = completed_at
                record.progress = failed_progress
            record.failure = normalized_failure
        if shared_paths is not _UNSET:
            record.shared_paths = shared_paths  # type: ignore[assignment]
        _replace_record_in_directory(
            access,
            record,
            expected_sequence=expected_sequence,
            expected_event_id=expected_event_id,
        )
        return record
    finally:
        access.close()


def transition_status(
    sequence: int,
    target: LifecycleState,
    *,
    progress: object = _UNSET,
    native_outcome: object = _UNSET,
    service_outcome: object = _UNSET,
    failure: object = _UNSET,
) -> CalculationRecord:
    if target == LifecycleState.SUCCESS:
        raise ValueError("SUCCESS requires calculation finalization")
    return _transition_record_directory(
        paths.queue_entry_dir(sequence),
        target,
        expected_sequence=sequence,
        expected_event_id=None,
        missing_message=f"calculation record for sequence {sequence} does not exist",
        progress=progress,
        native_outcome=native_outcome,
        service_outcome=service_outcome,
        failure=failure,
    )


def transition_current_record(
    event_id: str,
    target: LifecycleState,
    *,
    progress: object = _UNSET,
    native_outcome: object = _UNSET,
    service_outcome: object = _UNSET,
    failure: object = _UNSET,
) -> CalculationRecord:
    if target == LifecycleState.SUCCESS:
        raise ValueError("SUCCESS requires calculation finalization")
    validate_event_id(event_id)
    return _transition_record_directory(
        paths.event_service_dir(event_id),
        target,
        expected_sequence=None,
        expected_event_id=event_id,
        missing_message=f"current calculation record for {event_id!r} does not exist",
        progress=progress,
        native_outcome=native_outcome,
        service_outcome=service_outcome,
        failure=failure,
    )


def _transition_current_record_terminal(
    event_id: str,
    internal_sequence: int,
    target: LifecycleState,
    *,
    terminal_timestamp: str,
    native_outcome: Optional[dict[str, Any]],
    service_outcome: dict[str, Any],
    failure: Optional[dict[str, Any]],
    shared_paths: dict[str, Optional[str]],
) -> CalculationRecord:
    """Write a matching current calculation's preassembled terminal record."""
    validate_event_id(event_id)
    if (
        isinstance(internal_sequence, bool)
        or not isinstance(internal_sequence, int)
        or internal_sequence < 1
    ):
        raise ValueError("internal_sequence must be a positive integer")
    if target not in TERMINAL_STATES:
        raise ValueError("target must be a terminal LifecycleState")
    return _transition_record_directory(
        paths.event_service_dir(event_id),
        target,
        expected_sequence=internal_sequence,
        expected_event_id=event_id,
        missing_message=(
            f"current calculation record for {event_id!r} does not exist"
        ),
        native_outcome=native_outcome,
        service_outcome=service_outcome,
        failure=failure,
        terminal_timestamp=terminal_timestamp,
        shared_paths=shared_paths,
    )


def fail_matching_running_record_if_child_absent(
    record_directory: Path,
    *,
    expected_event_id: str,
    expected_sequence: int,
    blocking_child: str,
    failure: dict[str, Any],
) -> RunningRecordRecoveryResult:
    validate_event_id(expected_event_id)
    if (
        isinstance(expected_sequence, bool)
        or not isinstance(expected_sequence, int)
        or expected_sequence < 1
    ):
        raise ValueError("expected_sequence must be a positive integer")
    validate_upload_basename(blocking_child)
    try:
        parent = open_service_directory(record_directory.parent, create=False)
    except FileNotFoundError:
        return RunningRecordRecoveryResult.MISSING
    try:
        access = _open_record_directory(
            record_directory,
            exclusive=True,
            parent_descriptor=parent.descriptor,
            entry_name=record_directory.name,
        )
        if access is None:
            return RunningRecordRecoveryResult.MISSING
        try:
            record = _load_record_from_directory(
                access,
                expected_sequence=None,
                expected_event_id=None,
            )
            if (
                record.event_id != expected_event_id
                or record.internal_sequence != expected_sequence
            ):
                return RunningRecordRecoveryResult.IDENTITY_MISMATCH
            try:
                os.stat(
                    blocking_child,
                    dir_fd=access.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                return RunningRecordRecoveryResult.BLOCKING_CHILD_PRESENT
            if record.status != LifecycleState.RUNNING.value:
                return RunningRecordRecoveryResult.NOT_RUNNING

            parent_details = os.stat(
                record_directory.name,
                dir_fd=parent.descriptor,
                follow_symlinks=False,
            )
            record_details = os.fstat(access.descriptor)
            if (
                not stat.S_ISDIR(parent_details.st_mode)
                or parent_details.st_dev != record_details.st_dev
                or parent_details.st_ino != record_details.st_ino
            ):
                raise ValueError(
                    "record directory parent entry changed while recovery was locked"
                )
            try:
                os.stat(
                    blocking_child,
                    dir_fd=access.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                return RunningRecordRecoveryResult.BLOCKING_CHILD_PRESENT

            completed_at = _now_iso()
            record.status = LifecycleState.FAILED.value
            record.failure = dict(failure)
            failed_phase = record.failure.get("phase")
            if failed_phase is None:
                record.failure["phase"] = record.progress["phase"]
            elif failed_phase != record.progress["phase"]:
                failed_progress = dict(record.progress)
                failed_progress["phase"] = failed_phase
                failed_progress["phase_started_at"] = completed_at
                record.progress = failed_progress
            record.service_outcome = {"completed": True, "successful": False}
            record.timestamps["completed_at"] = completed_at
            _replace_record_in_directory(
                access,
                record,
                expected_sequence=expected_sequence,
                expected_event_id=expected_event_id,
            )
            return RunningRecordRecoveryResult.FAILED
        finally:
            access.close()
    finally:
        parent.close()


def transition_to_running(sequence: int) -> CalculationRecord:
    return transition_status(sequence, LifecycleState.RUNNING)


def transition_to_failed(
    sequence: int,
    reason: str,
    *,
    code: str = "execution_failed",
    native_outcome: Optional[dict[str, Any]] = None,
) -> CalculationRecord:
    return transition_status(
        sequence,
        LifecycleState.FAILED,
        failure={"code": code, "message": reason},
        native_outcome=native_outcome,
        service_outcome={"completed": True, "successful": False},
    )


def _open_regular_child(
    parent_descriptor: int,
    name: str,
    label: str,
) -> tuple[int, os.stat_result]:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
    except OSError as exc:
        raise ValueError(f"{label} is missing or unsafe: {exc}") from exc
    details = os.fstat(descriptor)
    if not stat.S_ISREG(details.st_mode):
        os.close(descriptor)
        raise ValueError(f"{label} is not a regular file")
    return descriptor, details


def _load_request_manifest_identity(
    access: _RecordDirectoryAccess,
    record: CalculationRecord,
) -> dict[str, Any]:
    manifest_descriptor, _ = _open_regular_child(
        access.descriptor,
        "request-manifest.json",
        "request-manifest.json",
    )
    try:
        with os.fdopen(
            manifest_descriptor,
            "r",
            encoding="utf-8",
        ) as stream:
            manifest = json.load(stream)
    except json.JSONDecodeError as exc:
        raise ValueError(f"request manifest is malformed: {exc}") from exc
    manifest = _require_exact_keys(
        manifest,
        {"schema_version", "event_id", "internal_sequence", "files"},
        "request manifest",
    )
    if (
        type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or manifest["event_id"] != record.event_id
        or type(manifest["internal_sequence"]) is not int
        or manifest["internal_sequence"] != record.internal_sequence
        or not isinstance(manifest["files"], list)
    ):
        raise ValueError("request manifest identity or schema is invalid")
    return manifest


def _validate_queue_entry(
    sequence: int,
    entry_name: str,
    queue_handle: DirectoryHandle,
) -> CalculationRecord:
    access = _open_record_directory(
        queue_handle.path / entry_name,
        exclusive=False,
        parent_descriptor=queue_handle.descriptor,
        entry_name=entry_name,
    )
    if access is None:
        raise ValueError(f"queue entry is missing: {entry_name}")
    try:
        record = _load_record_from_directory(
            access,
            expected_sequence=sequence,
        )
        try:
            request_descriptor = os.open(
                "request",
                directory_open_flags(),
                dir_fd=access.descriptor,
            )
        except OSError as exc:
            raise ValueError(
                f"request snapshot directory is missing or unsafe: {exc}"
            ) from exc
        try:
            manifest = _load_request_manifest_identity(access, record)
            expected_sizes: dict[str, int] = {}
            for item in manifest["files"]:
                if not isinstance(item, dict) or set(item) != {
                    "basename",
                    "size_bytes",
                    "sha256",
                }:
                    raise ValueError("request manifest file entry is invalid")
                basename = validate_upload_basename(item["basename"])
                if basename in expected_sizes:
                    raise ValueError(
                        f"request manifest contains duplicate basename: {basename}"
                    )
                if (
                    isinstance(item["size_bytes"], bool)
                    or not isinstance(item["size_bytes"], int)
                    or item["size_bytes"] < 0
                ):
                    raise ValueError(f"request snapshot size is invalid: {basename}")
                if (
                    not isinstance(item["sha256"], str)
                    or len(item["sha256"]) != 64
                    or any(
                        character not in "0123456789abcdef"
                        for character in item["sha256"]
                    )
                ):
                    raise ValueError(
                        f"request snapshot checksum is invalid: {basename}"
                    )
                expected_sizes[basename] = item["size_bytes"]
            if "event.xml" not in expected_sizes:
                raise ValueError("request manifest must contain exactly one event.xml")

            actual_sizes: dict[str, int] = {}
            # Shape/metadata keeps scans proportional; full-byte checks belong at promotion.
            for name in os.listdir(request_descriptor):
                try:
                    details = os.stat(
                        name,
                        dir_fd=request_descriptor,
                        follow_symlinks=False,
                    )
                except OSError as exc:
                    raise ValueError(
                        f"request snapshot entry is inaccessible: {name}: {exc}"
                    ) from exc
                if not stat.S_ISREG(details.st_mode):
                    raise ValueError(
                        f"request snapshot contains a non-regular entry: {name}"
                    )
                actual_sizes[name] = details.st_size
            missing_names = set(expected_sizes) - set(actual_sizes)
            if missing_names:
                raise ValueError(
                    "request snapshot is missing manifest entries: "
                    + ", ".join(sorted(missing_names))
                )
            unmanifested_names = set(actual_sizes) - set(expected_sizes)
            if unmanifested_names:
                raise ValueError(
                    "request snapshot contains unmanifested entries: "
                    + ", ".join(sorted(unmanifested_names))
                )
            for basename, expected_size in expected_sizes.items():
                if actual_sizes[basename] != expected_size:
                    raise ValueError(f"request snapshot size differs: {basename}")
        finally:
            os.close(request_descriptor)
        return record
    finally:
        access.close()


def _scan_queue_records_unlocked(
    queue_handle: DirectoryHandle,
) -> tuple[list[CalculationRecord], list[tuple[str, str]]]:
    records: list[CalculationRecord] = []
    malformed: list[tuple[str, str]] = []
    for entry_name in sorted(os.listdir(queue_handle.descriptor)):
        if entry_name.startswith("."):
            continue
        try:
            sequence = paths.parse_queue_entry_name(entry_name)
            records.append(_validate_queue_entry(sequence, entry_name, queue_handle))
        except (OSError, TypeError, ValueError) as exc:
            malformed.append((entry_name, str(exc)))
    records.sort(key=lambda item: item.internal_sequence)
    return records, malformed


def scan_queue_records() -> tuple[list[CalculationRecord], list[tuple[str, str]]]:
    try:
        queue_handle = open_service_directory(paths.queue_dir(), create=False)
    except FileNotFoundError:
        return [], []
    except (OSError, ValueError) as exc:
        return [], [("<queue>", str(exc))]
    try:
        fcntl.flock(queue_handle.descriptor, fcntl.LOCK_SH)
        try:
            return _scan_queue_records_unlocked(queue_handle)
        except (OSError, ValueError) as exc:
            return [], [("<queue>", str(exc))]
        finally:
            fcntl.flock(queue_handle.descriptor, fcntl.LOCK_UN)
    finally:
        queue_handle.close()


def _scan_current_records_unlocked(
    events_handle: DirectoryHandle,
) -> tuple[list[CalculationRecord], list[tuple[str, str]]]:
    records: list[CalculationRecord] = []
    malformed: list[tuple[str, str]] = []
    for event_id in sorted(os.listdir(events_handle.descriptor)):
        try:
            validate_event_id(event_id)
            access = _open_record_directory(
                events_handle.path / event_id,
                exclusive=False,
                parent_descriptor=events_handle.descriptor,
                entry_name=event_id,
            )
            if access is None:
                raise ValueError(f"current record is missing: {event_id}")
            try:
                record = _load_record_from_directory(
                    access,
                    expected_sequence=None,
                    expected_event_id=event_id,
                )
                _load_request_manifest_identity(access, record)
                records.append(record)
            finally:
                access.close()
        except (OSError, TypeError, ValueError) as exc:
            malformed.append((event_id, str(exc)))
    records.sort(key=lambda item: item.internal_sequence)
    return records, malformed


def scan_current_records() -> tuple[list[CalculationRecord], list[tuple[str, str]]]:
    try:
        events_handle = open_service_directory(paths.events_dir(), create=False)
    except FileNotFoundError:
        return [], []
    except (OSError, ValueError) as exc:
        return [], [("<events>", str(exc))]
    try:
        fcntl.flock(events_handle.descriptor, fcntl.LOCK_SH)
        try:
            return _scan_current_records_unlocked(events_handle)
        except (OSError, ValueError) as exc:
            return [], [("<events>", str(exc))]
        finally:
            fcntl.flock(events_handle.descriptor, fcntl.LOCK_UN)
    finally:
        events_handle.close()
