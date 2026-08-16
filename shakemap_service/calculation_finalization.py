"""Finalize one current calculation from already-produced evidence."""
from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Mapping

from . import paths, status
from .product_validation import ProductValidationResult
from .runner import ExecutionResult


class CalculationFinalizationError(RuntimeError):
    """Calculation evidence does not permit the requested terminal state."""


def _require_matching_current(
    record: status.CalculationRecord,
) -> status.CalculationRecord:
    if record.status != status.LifecycleState.RUNNING.value:
        raise ValueError("supplied calculation record must be RUNNING")
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
    return current


def _read_service_json(path: Path, label: str) -> dict[str, object]:
    try:
        details = path.lstat()
    except OSError as exc:
        raise CalculationFinalizationError(f"{label} is unavailable: {exc}") from exc
    if not stat.S_ISREG(details.st_mode):
        raise CalculationFinalizationError(f"{label} is not a regular file")
    if details.st_size == 0:
        raise CalculationFinalizationError(f"{label} is empty")
    try:
        with path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise CalculationFinalizationError(f"{label} is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise CalculationFinalizationError(f"{label} is not a JSON object")
    return payload


def _require_manifest(record: status.CalculationRecord) -> None:
    payload = _read_service_json(
        paths.event_manifest_file(record.event_id),
        "product manifest",
    )
    if (
        payload.get("event_id") != record.event_id
        or payload.get("internal_sequence") != record.internal_sequence
    ):
        raise CalculationFinalizationError(
            "product manifest calculation identity does not match"
        )
    if payload.get("partial") is not False:
        raise CalculationFinalizationError("product manifest is not complete")


def _require_provenance(
    record: status.CalculationRecord,
    terminal_timestamp: str,
) -> None:
    payload = _read_service_json(
        paths.event_provenance_file(record.event_id),
        "provenance",
    )
    if (
        payload.get("event_id") != record.event_id
        or payload.get("internal_sequence") != record.internal_sequence
    ):
        raise CalculationFinalizationError(
            "provenance calculation identity does not match"
        )
    timestamps = payload.get("timestamps")
    if (
        not isinstance(timestamps, dict)
        or timestamps.get("terminal_at") != terminal_timestamp
    ):
        raise CalculationFinalizationError(
            "provenance terminal timestamp does not match"
        )


def _require_nonempty_log(path: Path, label: str) -> None:
    try:
        details = path.lstat()
    except OSError as exc:
        raise CalculationFinalizationError(f"{label} is unavailable: {exc}") from exc
    if not stat.S_ISREG(details.st_mode):
        raise CalculationFinalizationError(f"{label} is not a regular file")
    if details.st_size == 0:
        raise CalculationFinalizationError(f"{label} is empty")
    try:
        with path.open("rb") as stream:
            if not stream.read(1):
                raise CalculationFinalizationError(f"{label} is empty")
    except OSError as exc:
        raise CalculationFinalizationError(f"{label} is unreadable: {exc}") from exc


def _shared_paths(event_id: str, *, require_all: bool) -> dict[str, str | None]:
    shared = status.shared_paths_for(event_id)
    service = paths.shared_service_root() / ".service" / "events" / event_id
    evidence = {
        "provenance": (
            paths.event_provenance_file(event_id),
            str(service / "provenance.json"),
        ),
        "product_manifest": (
            paths.event_manifest_file(event_id),
            str(service / "product-manifest.json"),
        ),
        "service_log": (
            paths.event_service_log_file(event_id),
            str(service / "logs" / "service.log"),
        ),
        "shake_log": (
            paths.event_log_file(event_id),
            str(service / "logs" / "shake.log"),
        ),
    }
    for name, (local_path, shared_path) in evidence.items():
        if require_all or _is_regular_readable_nonempty(local_path):
            shared[name] = shared_path
    return shared


def _is_regular_readable_nonempty(path: Path) -> bool:
    try:
        details = path.lstat()
        if not stat.S_ISREG(details.st_mode) or details.st_size == 0:
            return False
        with path.open("rb") as stream:
            return bool(stream.read(1))
    except OSError:
        return False


def _native_outcome(
    execution: ExecutionResult | None,
) -> dict[str, object] | None:
    if execution is None:
        return None
    if not isinstance(execution, ExecutionResult):
        raise TypeError("execution must be an ExecutionResult or None")
    return {
        "started": True,
        "exit_code": execution.exit_code,
        "signal": execution.signal,
    }


def finalize_success(
    record: status.CalculationRecord,
    *,
    execution: ExecutionResult,
    validation: ProductValidationResult,
    terminal_timestamp: str,
) -> status.CalculationRecord:
    """Write SUCCESS only after every required terminal artifact is ready."""
    current = _require_matching_current(record)
    if not isinstance(execution, ExecutionResult):
        raise TypeError("execution must be an ExecutionResult")
    if execution.exit_code != 0 or execution.signal is not None:
        raise CalculationFinalizationError(
            "native execution did not complete normally"
        )
    if not isinstance(validation, ProductValidationResult):
        raise TypeError("validation must be a ProductValidationResult")
    if not validation.passed:
        raise CalculationFinalizationError(
            "required-product validation did not pass"
        )

    _require_manifest(current)
    _require_provenance(current, terminal_timestamp)
    _require_nonempty_log(
        paths.event_service_log_file(current.event_id),
        "service.log",
    )
    _require_nonempty_log(paths.event_log_file(current.event_id), "shake.log")
    shared = _shared_paths(current.event_id, require_all=True)
    return status._transition_current_record_terminal(
        current.event_id,
        current.internal_sequence,
        status.LifecycleState.SUCCESS,
        terminal_timestamp=terminal_timestamp,
        native_outcome=_native_outcome(execution),
        service_outcome={"completed": True, "successful": True},
        failure=None,
        shared_paths=shared,
    )


def finalize_failure(
    record: status.CalculationRecord,
    *,
    terminal_timestamp: str,
    phase: str,
    code: str,
    message: str,
    execution: ExecutionResult | None = None,
    secondary_evidence: tuple[Mapping[str, object], ...] = (),
) -> status.CalculationRecord:
    """Write FAILED while retaining the supplied primary and secondary facts."""
    current = _require_matching_current(record)
    for label, value in (("phase", phase), ("code", code), ("message", message)):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{label} must be a nonempty string")
    secondary = [dict(item) for item in secondary_evidence]
    failure = {
        "phase": phase,
        "code": code,
        "message": message,
        "secondary_evidence": secondary,
    }
    return status._transition_current_record_terminal(
        current.event_id,
        current.internal_sequence,
        status.LifecycleState.FAILED,
        terminal_timestamp=terminal_timestamp,
        native_outcome=_native_outcome(execution),
        service_outcome={"completed": True, "successful": False},
        failure=failure,
        shared_paths=_shared_paths(current.event_id, require_all=False),
    )
