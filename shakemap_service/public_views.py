# -*- coding: utf-8 -*-
"""Read-only public projections of durable calculation records."""
from __future__ import annotations

import fcntl
import json
import os
import re
import stat
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import paths
from .config import Settings, settings
from .directory_access import directory_open_flags, open_service_directory
from .request_validation import validate_event_id
from .status import (
    CalculationRecord,
    LifecycleState,
    _validate_timestamp,
    read_archived_record,
    scan_current_records,
    scan_queue_records,
)


_ARCHIVE_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S.%fZ"
_ARCHIVE_TIMESTAMP_LENGTH = len("20260816T120000.000000Z")
_SERVICE_PATH_NAMES = (
    "provenance",
    "product_manifest",
    "service_log",
    "shake_log",
)


@dataclass(frozen=True)
class DurableRecordProblem:
    source: str
    entry: str
    message: str


class DurableStateError(RuntimeError):
    """Durable records cannot be projected without hiding invalid state."""

    def __init__(self, problems: Iterable[DurableRecordProblem]) -> None:
        self.problems = tuple(problems)
        super().__init__("durable calculation state is malformed")


class UnknownEventError(LookupError):
    """No durable current, queued, or retained record identifies an event."""


@dataclass(frozen=True)
class OperationalSnapshot:
    records: tuple[CalculationRecord, ...]
    current_by_event: dict[str, CalculationRecord]
    current_sequences: frozenset[int]
    queue_progress: dict[int, tuple[int, str]]
    maximum_running: int
    running: int
    queued: int


@dataclass(frozen=True)
class OperationalViews:
    events: dict[str, Any]
    queue: dict[str, Any]


@dataclass(frozen=True)
class _ArchiveTree:
    name: str
    event_id: str
    timestamp: datetime
    directory: Path
    has_products: bool
    has_service: bool


def _problem(source: str, entry: str, message: str) -> DurableStateError:
    return DurableStateError(
        [DurableRecordProblem(source=source, entry=entry, message=message)]
    )


_PRIVATE_IDENTITY_FIELDS = (
    ("immutable_image", "manifest_path"),
    ("immutable_image", "installed", "dependency_inventory_path"),
    (
        "immutable_image",
        "installed",
        "mapping_compatibility",
        "source_lock_path",
    ),
    (
        "immutable_image",
        "installed",
        "mapping_compatibility",
        "record_path",
    ),
    ("immutable_image", "support", "natural_earth", "manifest_path"),
    ("immutable_image", "support", "natural_earth", "cartopy_data_dir"),
    ("immutable_image", "support", "strec", "database_path"),
    ("immutable_image", "support", "strec", "database_link"),
    ("immutable_image", "support", "slab2", "source_archive_path"),
    ("immutable_image", "support", "slab2", "source_manifest_path"),
    (
        "immutable_image",
        "support",
        "slab2",
        "installed_files_manifest_path",
    ),
    ("immutable_image", "support", "slab2", "slabs_dir"),
)


def _remove_nested_field(
    value: dict[str, Any],
    field_path: tuple[str, ...],
) -> str | None:
    current: object = value
    for name in field_path[:-1]:
        if not isinstance(current, dict):
            return None
        current = current.get(name)
    if not isinstance(current, dict):
        return None
    removed = current.pop(field_path[-1], None)
    return removed if isinstance(removed, str) and removed else None


def _sanitize_diagnostic_text(value: str, private_paths: Iterable[str]) -> str:
    sanitized = value
    for private_path in sorted(set(private_paths), key=len, reverse=True):
        if private_path:
            sanitized = sanitized.replace(private_path, "<private-path>")
    private_roots = ("/opt", str(paths.service_root()).rstrip("/"))
    for private_root in private_roots:
        if not private_root:
            continue
        sanitized = re.sub(
            re.escape(private_root) + r"(?:/[^\s,;:'\"\)\]\}]+)*",
            "<private-path>",
            sanitized,
        )
    return sanitized


def _sanitize_diagnostic(value: Any, private_paths: Iterable[str]) -> Any:
    if isinstance(value, str):
        return _sanitize_diagnostic_text(value, private_paths)
    if isinstance(value, list):
        return [_sanitize_diagnostic(item, private_paths) for item in value]
    if isinstance(value, dict):
        return {
            key: _sanitize_diagnostic(item, private_paths)
            for key, item in value.items()
        }
    return value


def _project_identity(identity: object) -> tuple[dict[str, Any], tuple[str, ...]]:
    if not isinstance(identity, dict):
        raise ValueError("service identity is not an object")
    projected = deepcopy(identity)
    private_paths = tuple(
        path
        for field_path in _PRIVATE_IDENTITY_FIELDS
        if (path := _remove_nested_field(projected, field_path)) is not None
    )
    immutable_image = projected.get("immutable_image")
    if isinstance(immutable_image, dict):
        if "built_at_utc" in immutable_image:
            _validate_timestamp(
                immutable_image["built_at_utc"],
                "identity.immutable_image.built_at_utc",
                required=True,
            )
        reason = immutable_image.get("reason")
        if isinstance(reason, str):
            immutable_image["reason"] = _sanitize_diagnostic_text(
                reason,
                private_paths,
            )
    return projected, private_paths


def public_identity_projection(identity: object) -> dict[str, Any]:
    """Return public identity facts without private filesystem evidence."""
    projected, _ = _project_identity(identity)
    return projected


def _validate_provenance_timestamps(provenance: dict[str, Any]) -> None:
    if "timestamps" in provenance:
        timestamps = provenance["timestamps"]
        if not isinstance(timestamps, dict):
            raise ValueError("provenance timestamps must be an object")
        for name, value in timestamps.items():
            _validate_timestamp(
                value,
                f"provenance.timestamps.{name}",
                required=False,
            )
    native_execution = provenance.get("native_execution")
    if native_execution is not None:
        if not isinstance(native_execution, dict):
            raise ValueError("provenance native_execution must be an object or null")
        for name in ("started_at", "completed_at"):
            if name in native_execution:
                _validate_timestamp(
                    native_execution[name],
                    f"provenance.native_execution.{name}",
                    required=False,
                )


def public_provenance_projection(provenance: object) -> dict[str, Any]:
    """Return a public-safe copy of one durable provenance object."""
    if not isinstance(provenance, dict):
        raise ValueError("provenance is not an object")
    projected = deepcopy(provenance)
    private_paths: list[str] = []

    if "software_identity" in projected:
        software_identity, identity_paths = _project_identity(
            projected["software_identity"]
        )
        projected["software_identity"] = software_identity
        private_paths.extend(identity_paths)

    materialization: dict[str, Any] | None = None
    configuration = projected.get("configuration")
    if isinstance(configuration, dict):
        candidate = configuration.get("materialization")
        if isinstance(candidate, dict):
            materialization = candidate
            source_directory = materialization.pop("source_directory", None)
            if isinstance(source_directory, str) and source_directory:
                private_paths.append(source_directory)

    if materialization is not None:
        for name in ("profile_helper", "strec_helper", "failure"):
            if name in materialization:
                materialization[name] = _sanitize_diagnostic(
                    materialization[name],
                    private_paths,
                )
    for name in ("warnings", "failure"):
        if name in projected:
            projected[name] = _sanitize_diagnostic(
                projected[name],
                private_paths,
            )

    _validate_provenance_timestamps(projected)
    return projected


def _scan_problems(
    source: str,
    errors: Iterable[tuple[str, str]],
) -> list[DurableRecordProblem]:
    return [
        DurableRecordProblem(source=source, entry=entry, message=message)
        for entry, message in errors
    ]


def _merge_records(
    queue_records: Iterable[CalculationRecord],
    current_records: Iterable[CalculationRecord],
) -> dict[int, CalculationRecord]:
    records: dict[int, CalculationRecord] = {}
    locations: dict[int, str] = {}
    for source, candidates in (
        ("queue", queue_records),
        ("current", current_records),
    ):
        for record in candidates:
            existing = records.get(record.internal_sequence)
            if existing is not None:
                if existing.event_id != record.event_id:
                    raise _problem(
                        f"{locations[record.internal_sequence]}/{source}",
                        str(record.internal_sequence),
                        "the same internal sequence has conflicting calculation identities",
                    )
                if source == "current":
                    records[record.internal_sequence] = record
                    locations[record.internal_sequence] = source
                continue
            records[record.internal_sequence] = record
            locations[record.internal_sequence] = source
    return records


def _base_job_row(
    record: CalculationRecord,
    queue_progress: dict[int, tuple[int, str]],
) -> dict[str, Any]:
    queue_position: int | None = None
    waiting_reason: str | None = None
    if record.status == LifecycleState.QUEUED.value:
        progress = queue_progress.get(record.internal_sequence)
        if progress is not None:
            queue_position, waiting_reason = progress
    terminal = record.status in {
        LifecycleState.SUCCESS.value,
        LifecycleState.FAILED.value,
    }
    return {
        "event_id": record.event_id,
        "internal_sequence": record.internal_sequence,
        "status": record.status,
        "job_completed": terminal,
        "products_ready": record.status == LifecycleState.SUCCESS.value,
        "queue_position": queue_position,
        "waiting_reason": waiting_reason,
        "phase": record.progress["phase"],
        "phase_started_at": record.progress["phase_started_at"],
        "timestamps": dict(record.timestamps),
        "configuration": dict(record.configuration),
        "overwrite": record.overwrite,
        "native_outcome": (
            None if record.native_outcome is None else dict(record.native_outcome)
        ),
        "failure": None if record.failure is None else dict(record.failure),
        "warnings": list(record.warnings),
        "shared_paths": {
            name: record.shared_paths[name]
            for name in ("products", *_SERVICE_PATH_NAMES)
        },
    }


def _event_row(
    record: CalculationRecord,
    queue_progress: dict[int, tuple[int, str]],
) -> dict[str, Any]:
    base = _base_job_row(record, queue_progress)
    timestamps = base["timestamps"]
    return {
        "event_id": base["event_id"],
        "internal_sequence": base["internal_sequence"],
        "status": base["status"],
        "job_completed": base["job_completed"],
        "products_ready": base["products_ready"],
        "queue_position": base["queue_position"],
        "waiting_reason": base["waiting_reason"],
        "phase": base["phase"],
        "submitted_at": timestamps["submitted_at"],
        "started_at": timestamps["started_at"],
        "completed_at": timestamps["completed_at"],
        "shared_products_path": base["shared_paths"]["products"],
    }


def build_operational_snapshot(
    *,
    service_settings: Settings | None = None,
) -> OperationalSnapshot:
    """Read and merge current and queued state for all public projections."""
    queue_records, queue_errors = scan_queue_records()
    current_records, current_errors = scan_current_records()
    problems = [
        *_scan_problems("queue", queue_errors),
        *_scan_problems("current", current_errors),
    ]
    if problems:
        raise DurableStateError(problems)

    running_records_by_sequence = {
        record.internal_sequence: record
        for record in (*queue_records, *current_records)
        if record.status == LifecycleState.RUNNING.value
    }
    records_by_sequence = _merge_records(queue_records, current_records)
    records = tuple(
        records_by_sequence[key] for key in sorted(records_by_sequence)
    )
    running_event_ids = {
        record.event_id for record in running_records_by_sequence.values()
    }
    queued_records = [
        record for record in records if record.status == LifecycleState.QUEUED.value
    ]

    configured = settings if service_settings is None else service_settings
    capacity_exhausted = len(running_records_by_sequence) >= configured.max_concurrent
    queue_progress: dict[int, tuple[int, str]] = {}
    for position, record in enumerate(queued_records, start=1):
        if record.event_id in running_event_ids:
            waiting_reason = "same_event_active"
        elif capacity_exhausted:
            waiting_reason = "worker_capacity"
        else:
            waiting_reason = "awaiting_scheduler"
        queue_progress[record.internal_sequence] = (position, waiting_reason)

    return OperationalSnapshot(
        records=records,
        current_by_event={record.event_id: record for record in current_records},
        current_sequences=frozenset(
            record.internal_sequence for record in current_records
        ),
        queue_progress=queue_progress,
        maximum_running=configured.max_concurrent,
        running=len(running_records_by_sequence),
        queued=len(queued_records),
    )


def build_operational_views(
    *,
    service_settings: Settings | None = None,
) -> OperationalViews:
    """Build the event collection and queue view from the same durable records."""
    snapshot = build_operational_snapshot(service_settings=service_settings)
    queue_rows: list[dict[str, Any]] = []
    for record in snapshot.records:
        if record.status != LifecycleState.QUEUED.value:
            continue
        position, waiting_reason = snapshot.queue_progress[record.internal_sequence]
        queue_rows.append(
            {
                "event_id": record.event_id,
                "internal_sequence": record.internal_sequence,
                "status": record.status,
                "queue_position": position,
                "waiting_reason": waiting_reason,
                "submitted_at": record.timestamps["submitted_at"],
            }
        )

    return OperationalViews(
        events={
            "jobs": [
                _event_row(record, snapshot.queue_progress)
                for record in snapshot.records
            ],
        },
        queue={
            "capacity": {
                "maximum_running": snapshot.maximum_running,
                "running": snapshot.running,
                "available": max(snapshot.maximum_running - snapshot.running, 0),
                "queued": snapshot.queued,
            },
            "jobs": queue_rows,
        },
    )


def _parse_archive_name(name: str) -> tuple[str, datetime]:
    separator = len(name) - _ARCHIVE_TIMESTAMP_LENGTH - 1
    if separator < 1 or name[separator] != "-":
        raise ValueError("archive name does not contain the fixed UTC timestamp suffix")
    event_id = name[:separator]
    timestamp_text = name[separator + 1 :]
    validate_event_id(event_id)
    try:
        timestamp = datetime.strptime(
            timestamp_text,
            _ARCHIVE_TIMESTAMP_FORMAT,
        ).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError("archive timestamp is invalid") from exc
    return event_id, timestamp


def _archive_trees() -> tuple[_ArchiveTree, ...]:
    try:
        archive_root = open_service_directory(paths.archive_dir(), create=False)
    except FileNotFoundError:
        return ()
    except (OSError, ValueError) as exc:
        raise _problem("archive", "<archive>", str(exc)) from exc

    trees: list[_ArchiveTree] = []
    problems: list[DurableRecordProblem] = []
    try:
        fcntl.flock(archive_root.descriptor, fcntl.LOCK_SH)
        try:
            names = os.listdir(archive_root.descriptor)
            for name in names:
                try:
                    event_id, timestamp = _parse_archive_name(name)
                    archive_descriptor = os.open(
                        name,
                        directory_open_flags(),
                        dir_fd=archive_root.descriptor,
                    )
                    try:
                        component_names = set(os.listdir(archive_descriptor))
                        if not component_names or not component_names <= {
                            "products",
                            "service",
                        }:
                            raise ValueError("archive components are invalid")
                        for component in component_names:
                            child = os.open(
                                component,
                                directory_open_flags(),
                                dir_fd=archive_descriptor,
                            )
                            os.close(child)
                    finally:
                        os.close(archive_descriptor)
                    trees.append(
                        _ArchiveTree(
                            name=name,
                            event_id=event_id,
                            timestamp=timestamp,
                            directory=archive_root.path / name,
                            has_products="products" in component_names,
                            has_service="service" in component_names,
                        )
                    )
                except (OSError, TypeError, ValueError) as exc:
                    problems.append(
                        DurableRecordProblem(
                            source="archive",
                            entry=name,
                            message=str(exc),
                        )
                    )
        except OSError as exc:
            problems.append(
                DurableRecordProblem(
                    source="archive",
                    entry="<archive>",
                    message=str(exc),
                )
            )
        finally:
            fcntl.flock(archive_root.descriptor, fcntl.LOCK_UN)
    finally:
        archive_root.close()
    if problems:
        raise DurableStateError(problems)
    return tuple(trees)


def _read_optional_json_object(
    directory: Path,
    filename: str,
    *,
    source: str,
    entry: str,
    event_id: str,
    internal_sequence: int,
) -> dict[str, Any] | None:
    try:
        handle = open_service_directory(directory, create=False)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise _problem(source, entry, str(exc)) from exc
    descriptor = -1
    try:
        try:
            descriptor = os.open(
                filename,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=handle.descriptor,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _problem(source, entry, f"{filename} is unsafe: {exc}") from exc
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise _problem(source, entry, f"{filename} is not a regular file")
        try:
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = -1
                payload = json.load(stream)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise _problem(source, entry, f"{filename} is malformed: {exc}") from exc
        if not isinstance(payload, dict):
            raise _problem(source, entry, f"{filename} is not a JSON object")
        if (
            payload.get("event_id") != event_id
            or type(payload.get("internal_sequence")) is not int
            or payload.get("internal_sequence") != internal_sequence
        ):
            raise _problem(
                source,
                entry,
                f"{filename} calculation identity does not match",
            )
        return payload
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        handle.close()


def _optional_regular_file(
    directory: Path,
    filename: str,
    *,
    source: str,
    entry: str,
) -> bool:
    try:
        handle = open_service_directory(directory, create=False)
    except FileNotFoundError:
        return False
    except (OSError, ValueError) as exc:
        raise _problem(source, entry, str(exc)) from exc
    descriptor = -1
    try:
        try:
            descriptor = os.open(
                filename,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=handle.descriptor,
            )
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise _problem(source, entry, f"{filename} is unsafe: {exc}") from exc
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise _problem(source, entry, f"{filename} is not a regular file")
        return True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        handle.close()


def _archive_rows(
    event_id: str,
    archive_trees: Iterable[_ArchiveTree],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for tree in archive_trees:
        if tree.event_id != event_id:
            continue
        record: CalculationRecord | None = None
        provenance: dict[str, Any] | None = None
        service_paths = {name: None for name in _SERVICE_PATH_NAMES}
        if tree.has_service:
            try:
                record = read_archived_record(
                    tree.directory / "service",
                    expected_event_id=tree.event_id,
                )
            except (OSError, TypeError, ValueError) as exc:
                raise _problem("archive", tree.name, str(exc)) from exc
            if record is None:
                raise _problem("archive", tree.name, "service record is missing")
            if record.status not in {
                LifecycleState.SUCCESS.value,
                LifecycleState.FAILED.value,
            }:
                raise _problem("archive", tree.name, "service record is not terminal")
            service_directory = tree.directory / "service"
            provenance = _read_optional_json_object(
                service_directory,
                "provenance.json",
                source="archive",
                entry=tree.name,
                event_id=record.event_id,
                internal_sequence=record.internal_sequence,
            )
            if provenance is not None:
                try:
                    provenance = public_provenance_projection(provenance)
                except (TypeError, ValueError) as exc:
                    raise _problem("archive", tree.name, str(exc)) from exc
            shared_service = (
                paths.shared_service_root() / ".service" / "archive" / tree.name / "service"
            )
            if provenance is not None:
                service_paths["provenance"] = str(shared_service / "provenance.json")
            for key, relative_directory, filename in (
                ("product_manifest", Path(), "product-manifest.json"),
                ("service_log", Path("logs"), "service.log"),
                ("shake_log", Path("logs"), "shake.log"),
            ):
                if _optional_regular_file(
                    service_directory / relative_directory,
                    filename,
                    source="archive",
                    entry=tree.name,
                ):
                    service_paths[key] = str(
                        shared_service / relative_directory / filename
                    )

        shared_archive = paths.shared_service_root() / ".service" / "archive" / tree.name
        rows.append(
            {
                "archived_at": tree.timestamp.isoformat(
                    timespec="microseconds"
                ).replace("+00:00", "Z"),
                "internal_sequence": (
                    None if record is None else record.internal_sequence
                ),
                "status": None if record is None else record.status,
                "products_ready": bool(
                    tree.has_products
                    and record is not None
                    and record.status == LifecycleState.SUCCESS.value
                ),
                "shared_paths": {
                    "archive": str(shared_archive),
                    "products": (
                        str(shared_archive / "products" / "current" / "products")
                        if tree.has_products
                        else None
                    ),
                    **service_paths,
                },
                "provenance": provenance,
            }
        )
    rows.sort(key=lambda row: row["archived_at"], reverse=True)
    return rows


def _detailed_job_row(
    record: CalculationRecord,
    snapshot: OperationalSnapshot,
) -> dict[str, Any]:
    row = _base_job_row(record, snapshot.queue_progress)
    row.pop("event_id")
    row["provenance"] = None
    if record.internal_sequence in snapshot.current_sequences:
        service_directory = paths.event_service_dir(record.event_id)
        row["provenance"] = _read_optional_json_object(
            service_directory,
            "provenance.json",
            source="current",
            entry=record.event_id,
            event_id=record.event_id,
            internal_sequence=record.internal_sequence,
        )
        if row["provenance"] is not None:
            try:
                row["provenance"] = public_provenance_projection(
                    row["provenance"]
                )
            except (TypeError, ValueError) as exc:
                raise _problem("current", record.event_id, str(exc)) from exc
        shared_service = (
            paths.shared_service_root() / ".service" / "events" / record.event_id
        )
        if row["provenance"] is not None:
            row["shared_paths"]["provenance"] = str(
                shared_service / "provenance.json"
            )
        for key, relative_directory, filename in (
            ("product_manifest", Path(), "product-manifest.json"),
            ("service_log", Path("logs"), "service.log"),
            ("shake_log", Path("logs"), "shake.log"),
        ):
            if _optional_regular_file(
                service_directory / relative_directory,
                filename,
                source="current",
                entry=record.event_id,
            ):
                row["shared_paths"][key] = str(
                    shared_service / relative_directory / filename
                )
    return row


def build_event_detail(event_id: str) -> dict[str, Any]:
    """Build one event's operational rows and retained archive history."""
    snapshot = build_operational_snapshot()
    archive_trees = _archive_trees()
    matching = [record for record in snapshot.records if record.event_id == event_id]
    archives = _archive_rows(event_id, archive_trees)
    if not matching and not archives:
        raise UnknownEventError(event_id)
    return {
        "event_id": event_id,
        "jobs": [_detailed_job_row(record, snapshot) for record in matching],
        "archives": archives,
    }


def _validate_relative_product_path(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value.startswith("/"):
        raise ValueError(f"{label} is not a relative path")
    if any(component in {"", ".", ".."} for component in value.split("/")):
        raise ValueError(f"{label} is not a canonical relative path")
    return value


def _manifest_summary(
    payload: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    partial = payload.get("partial")
    if not isinstance(partial, bool):
        raise ValueError("product manifest partial flag is invalid")
    required = payload.get("required_products")
    required_rows: list[dict[str, Any]] = []
    if required is None:
        if not partial:
            raise ValueError("complete product manifest lacks required-product checks")
    else:
        if not isinstance(required, dict) or not isinstance(required.get("checks"), list):
            raise ValueError("product manifest required-product checks are invalid")
        for index, check in enumerate(required["checks"]):
            if not isinstance(check, dict):
                raise ValueError(f"required-product check {index} is invalid")
            path = _validate_relative_product_path(
                check.get("path"),
                f"required-product check {index} path",
            )
            passed = check.get("passed")
            reason = check.get("reason")
            if not isinstance(passed, bool) or (
                reason is not None and not isinstance(reason, str)
            ):
                raise ValueError(f"required-product check {index} result is invalid")
            required_rows.append(
                {"path": path, "passed": passed, "reason": reason}
            )

    products = payload.get("products")
    if not isinstance(products, list):
        raise ValueError("product manifest inventory is invalid")
    product_rows: list[dict[str, Any]] = []
    for index, product in enumerate(products):
        if not isinstance(product, dict):
            raise ValueError(f"product inventory entry {index} is invalid")
        path = _validate_relative_product_path(
            product.get("path"),
            f"product inventory entry {index} path",
        )
        size = product.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError(f"product inventory entry {index} size is invalid")
        product_rows.append({"path": path, "size_bytes": size})
    return (
        "partial" if partial else "complete",
        required_rows,
        product_rows,
    )


def build_current_product_summary(event_id: str) -> dict[str, Any]:
    """Summarize the current service-owned product manifest without native I/O."""
    snapshot = build_operational_snapshot()
    archive_trees = _archive_trees()
    current = snapshot.current_by_event.get(event_id)
    known = (
        current is not None
        or any(record.event_id == event_id for record in snapshot.records)
        or any(tree.event_id == event_id for tree in archive_trees)
    )
    if not known:
        raise UnknownEventError(event_id)
    if current is None:
        return {"event_id": event_id, "current": None}

    manifest = _read_optional_json_object(
        paths.event_service_dir(current.event_id),
        "product-manifest.json",
        source="current",
        entry=current.event_id,
        event_id=current.event_id,
        internal_sequence=current.internal_sequence,
    )
    manifest_path: str | None = None
    if manifest is None:
        manifest_state = "unavailable"
        required_products: list[dict[str, Any]] = []
        products: list[dict[str, Any]] = []
    else:
        try:
            manifest_state, required_products, products = _manifest_summary(manifest)
        except (TypeError, ValueError) as exc:
            raise _problem("current", current.event_id, str(exc)) from exc
        manifest_path = str(
            paths.shared_service_root()
            / ".service"
            / "events"
            / current.event_id
            / "product-manifest.json"
        )
    return {
        "event_id": event_id,
        "current": {
            "internal_sequence": current.internal_sequence,
            "status": current.status,
            "products_ready": current.status == LifecycleState.SUCCESS.value,
            "manifest_state": manifest_state,
            "required_products": required_products,
            "products": products,
            "shared_paths": {
                "products": current.shared_paths["products"],
                "product_manifest": manifest_path,
            },
        },
    }
