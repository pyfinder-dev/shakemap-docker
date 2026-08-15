# -*- coding: utf-8 -*-
"""Durable replacement of one event calculation tree."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import stat
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from typing import Any, Optional

from . import paths, status
from .directory_access import DirectoryHandle, directory_open_flags, open_service_directory
from .request_validation import validate_event_id, validate_upload_basename


TRANSACTION_FILE = "transaction.json"
STAGING_DIRECTORY = ".recalculation-stage"
NATIVE_CANDIDATE_DIRECTORY = ".native-event-candidate"
COPY_CHUNK_SIZE = 64 * 1024
JOURNAL_SCHEMA_VERSION = 1

_CHECKPOINTS = (
    "products_staged",
    "service_staged",
    "preceding_disposed",
    "record_promoted",
    "candidate_ready",
    "native_tree_published",
)
_INTENT_NAMES = ("stage_removal_started", "candidate_removal_started")
_ARCHIVE_TIMESTAMP = re.compile(r"[0-9]{8}T[0-9]{6}\.[0-9]{6}Z")


class RecalculationError(RuntimeError):
    """A calculation tree could not be prepared or reconciled safely."""


@dataclass(frozen=True)
class PreparationResult:
    record: status.CalculationRecord
    native_event_directory: Path
    archive_directory: Optional[Path]
    preceding_products: bool
    preceding_service: bool


@dataclass
class _Transaction:
    record: status.CalculationRecord
    record_handle: DirectoryHandle
    queue_handle: DirectoryHandle
    events_handle: DirectoryHandle
    archive_handle: DirectoryHandle
    products_handle: DirectoryHandle
    journal: dict[str, Any]
    location: str


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _operation_boundary(position: str, kind: str, name: str) -> None:
    pass


def _named_rename(
    source: str,
    destination: str,
    *,
    source_descriptor: int,
    destination_descriptor: int,
    name: str,
) -> None:
    _operation_boundary("before", "rename", name)
    os.rename(
        source,
        destination,
        src_dir_fd=source_descriptor,
        dst_dir_fd=destination_descriptor,
    )
    _operation_boundary("after", "rename", name)


def _named_fsync(descriptor: int, name: str) -> None:
    _operation_boundary("before", "fsync", name)
    os.fsync(descriptor)
    _operation_boundary("after", "fsync", name)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written < 1:
            raise OSError("zero-byte write")
        view = view[written:]


def _write_journal(record_handle: DirectoryHandle, journal: dict[str, Any]) -> None:
    temporary_name = f".transaction-{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=record_handle.descriptor,
    )
    replaced = False
    try:
        payload = (json.dumps(journal, indent=2, sort_keys=True) + "\n").encode("utf-8")
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary_name,
            TRANSACTION_FILE,
            src_dir_fd=record_handle.descriptor,
            dst_dir_fd=record_handle.descriptor,
        )
        replaced = True
        os.fsync(record_handle.descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not replaced:
            try:
                os.unlink(temporary_name, dir_fd=record_handle.descriptor)
            except FileNotFoundError:
                pass


def _named_checkpoint(transaction: _Transaction, checkpoint: str) -> None:
    if checkpoint not in _CHECKPOINTS:
        raise ValueError(f"unknown transaction checkpoint: {checkpoint}")
    completed = transaction.journal["completed"]
    if checkpoint in completed:
        return
    _operation_boundary("before", "checkpoint", checkpoint)
    completed.append(checkpoint)
    try:
        _write_journal(transaction.record_handle, transaction.journal)
    except BaseException:
        completed.pop()
        raise
    _operation_boundary("after", "checkpoint", checkpoint)


def _named_intent(transaction: _Transaction, intent: str) -> None:
    if intent not in _INTENT_NAMES:
        raise ValueError(f"unknown transaction intent: {intent}")
    if transaction.journal["intents"][intent]:
        return
    _operation_boundary("before", "intent", intent)
    transaction.journal["intents"][intent] = True
    try:
        _write_journal(transaction.record_handle, transaction.journal)
    except BaseException:
        transaction.journal["intents"][intent] = False
        raise
    _operation_boundary("after", "intent", intent)


def _entry_details(parent: DirectoryHandle, name: str) -> Optional[os.stat_result]:
    try:
        return os.stat(name, dir_fd=parent.descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _safe_directory_entry(
    parent: DirectoryHandle,
    name: str,
    label: str,
) -> bool:
    details = _entry_details(parent, name)
    if details is None:
        return False
    if not stat.S_ISDIR(details.st_mode):
        raise RecalculationError(f"{label} is not a safe directory: {parent.path / name}")
    return True


def _open_child_directory(parent: DirectoryHandle, name: str) -> DirectoryHandle:
    descriptor = os.open(name, directory_open_flags(), dir_fd=parent.descriptor)
    return DirectoryHandle(path=parent.path / name, descriptor=descriptor)


def _revalidate_parent_entry(
    parent: DirectoryHandle,
    name: str,
    child: DirectoryHandle,
    label: str,
) -> None:
    try:
        parent_details = os.stat(
            name,
            dir_fd=parent.descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise RecalculationError(f"{label} parent entry changed: {exc}") from exc
    child_details = os.fstat(child.descriptor)
    if (
        not stat.S_ISDIR(parent_details.st_mode)
        or parent_details.st_dev != child_details.st_dev
        or parent_details.st_ino != child_details.st_ino
    ):
        raise RecalculationError(f"{label} parent entry changed after it was opened")


def _revalidate_service_directory(
    opened: DirectoryHandle,
    path: Path,
    label: str,
) -> None:
    try:
        authoritative = open_service_directory(path, create=False)
    except (FileNotFoundError, ValueError) as exc:
        raise RecalculationError(f"{label} changed after it was opened: {exc}") from exc
    try:
        opened_details = os.fstat(opened.descriptor)
        authoritative_details = os.fstat(authoritative.descriptor)
        if (
            not stat.S_ISDIR(opened_details.st_mode)
            or not stat.S_ISDIR(authoritative_details.st_mode)
            or opened_details.st_dev != authoritative_details.st_dev
            or opened_details.st_ino != authoritative_details.st_ino
        ):
            raise RecalculationError(f"{label} changed after it was opened")
    finally:
        authoritative.close()


def _revalidate_status(
    record_handle: DirectoryHandle,
    record: status.CalculationRecord,
) -> None:
    stored = _read_json_regular(
        record_handle.descriptor,
        "status.json",
        "status.json",
    )
    if stored != asdict(record):
        raise RecalculationError("calculation status changed after it was read")


def _open_regular(parent_descriptor: int, name: str, label: str) -> int:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
    except OSError as exc:
        raise RecalculationError(f"{label} is missing or unsafe: {exc}") from exc
    details = os.fstat(descriptor)
    if not stat.S_ISREG(details.st_mode):
        os.close(descriptor)
        raise RecalculationError(f"{label} is not a regular file")
    return descriptor


def _read_json_regular(parent_descriptor: int, name: str, label: str) -> object:
    descriptor = _open_regular(parent_descriptor, name, label)
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            try:
                return json.load(stream)
            except json.JSONDecodeError as exc:
                raise RecalculationError(f"{label} is malformed: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validate_manifest(
    record_handle: DirectoryHandle,
    record: status.CalculationRecord,
) -> tuple[dict[str, tuple[int, str]], DirectoryHandle]:
    manifest = _read_json_regular(
        record_handle.descriptor,
        "request-manifest.json",
        "request-manifest.json",
    )
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "event_id",
        "internal_sequence",
        "files",
    }:
        raise RecalculationError("request manifest fields are invalid")
    if (
        type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or manifest["event_id"] != record.event_id
        or type(manifest["internal_sequence"]) is not int
        or manifest["internal_sequence"] != record.internal_sequence
        or not isinstance(manifest["files"], list)
    ):
        raise RecalculationError("request manifest identity or schema is invalid")

    expected: dict[str, tuple[int, str]] = {}
    for item in manifest["files"]:
        if not isinstance(item, dict) or set(item) != {
            "basename",
            "size_bytes",
            "sha256",
        }:
            raise RecalculationError("request manifest file entry is invalid")
        try:
            basename = validate_upload_basename(item["basename"])
        except (TypeError, ValueError) as exc:
            raise RecalculationError(f"request manifest basename is invalid: {exc}") from exc
        if basename in expected:
            raise RecalculationError(
                f"request manifest contains duplicate basename: {basename}"
            )
        size = item["size_bytes"]
        digest = item["sha256"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise RecalculationError(f"request snapshot size is invalid: {basename}")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise RecalculationError(f"request snapshot checksum is invalid: {basename}")
        expected[basename] = (size, digest)
    if "event.xml" not in expected:
        raise RecalculationError("request manifest must contain event.xml")

    try:
        request_handle = _open_child_directory(record_handle, "request")
    except OSError as exc:
        raise RecalculationError(f"request snapshot is missing or unsafe: {exc}") from exc
    try:
        actual_names = os.listdir(request_handle.descriptor)
        if set(actual_names) != set(expected):
            missing = sorted(set(expected) - set(actual_names))
            extra = sorted(set(actual_names) - set(expected))
            details: list[str] = []
            if missing:
                details.append("missing " + ", ".join(missing))
            if extra:
                details.append("unmanifested " + ", ".join(extra))
            raise RecalculationError(
                "request snapshot differs from manifest: " + "; ".join(details)
            )
        for basename, (expected_size, expected_digest) in expected.items():
            descriptor = _open_regular(
                request_handle.descriptor,
                basename,
                f"request snapshot file {basename}",
            )
            try:
                details = os.fstat(descriptor)
                digest = hashlib.sha256()
                size = 0
                while True:
                    chunk = os.read(descriptor, COPY_CHUNK_SIZE)
                    if not chunk:
                        break
                    size += len(chunk)
                    digest.update(chunk)
            finally:
                os.close(descriptor)
            if size != expected_size:
                raise RecalculationError(f"request snapshot size differs: {basename}")
            if digest.hexdigest() != expected_digest:
                raise RecalculationError(f"request snapshot checksum differs: {basename}")
        return expected, request_handle
    except BaseException:
        request_handle.close()
        raise


def _load_running_record(sequence: int) -> status.CalculationRecord:
    record = status.read_status(sequence)
    if record is None:
        raise RecalculationError(f"claimed queue record does not exist: {sequence}")
    if record.status != status.LifecycleState.RUNNING.value:
        raise RecalculationError(
            f"calculation {sequence} must be durably RUNNING before preparation"
        )
    return record


def _timestamp(value: Optional[datetime] = None) -> datetime:
    selected = datetime.now(timezone.utc) if value is None else value
    if selected.tzinfo is None or selected.utcoffset() != timedelta(0):
        raise ValueError("archive time must be timezone-aware UTC")
    return selected.astimezone(timezone.utc)


def _archive_path(
    archive_handle: DirectoryHandle,
    event_id: str,
    value: Optional[datetime],
) -> Path:
    selected = _timestamp(value)
    for offset in range(1_000_000):
        candidate_time = selected + timedelta(microseconds=offset)
        timestamp = candidate_time.strftime("%Y%m%dT%H%M%S.%fZ")
        candidate = paths.event_archive_dir(event_id, timestamp)
        if _entry_details(archive_handle, candidate.name) is None:
            return candidate
    raise RecalculationError(f"could not allocate a collision-free archive path for {event_id}")


def _validate_archive_path(event_id: str, value: object) -> Path:
    if not isinstance(value, str):
        raise RecalculationError("transaction archive path is invalid")
    archive_path = Path(value)
    if archive_path.parent != paths.archive_dir():
        raise RecalculationError("transaction archive path is outside the archive directory")
    prefix = f"{event_id}-"
    if not archive_path.name.startswith(prefix):
        raise RecalculationError("transaction archive name does not match event_id")
    timestamp = archive_path.name[len(prefix) :]
    if _ARCHIVE_TIMESTAMP.fullmatch(timestamp) is None:
        raise RecalculationError("transaction archive timestamp is invalid")
    try:
        datetime.strptime(timestamp, "%Y%m%dT%H%M%S.%fZ")
    except ValueError as exc:
        raise RecalculationError("transaction archive timestamp is invalid") from exc
    return archive_path


def _missing_counterpart_warning(
    preceding_products: bool,
    preceding_service: bool,
) -> Optional[str]:
    if preceding_products and preceding_service:
        return None
    if not preceding_products and not preceding_service:
        return "no preceding products or service tree existed"
    if not preceding_products:
        return "preceding products tree was missing"
    return "preceding service tree was missing"


def _persist_missing_counterpart_warning(
    record_handle: DirectoryHandle,
    queue_handle: DirectoryHandle,
    record: status.CalculationRecord,
    warning: Optional[str],
) -> status.CalculationRecord:
    if warning is None or warning in record.warnings:
        return record
    fcntl.flock(record_handle.descriptor, fcntl.LOCK_UN)
    try:
        updated = status.update_status(
            record.internal_sequence,
            warnings=[*record.warnings, warning],
        )
    finally:
        fcntl.flock(record_handle.descriptor, fcntl.LOCK_EX)
    _revalidate_parent_entry(
        queue_handle,
        paths.queue_entry_name(record.internal_sequence),
        record_handle,
        "claimed queue record",
    )
    _revalidate_status(record_handle, updated)
    return updated


def _open_parents(
    *,
    create: bool,
) -> tuple[DirectoryHandle, DirectoryHandle, DirectoryHandle, DirectoryHandle]:
    opened: list[DirectoryHandle] = []
    try:
        for path in (
            paths.queue_dir(),
            paths.events_dir(),
            paths.archive_dir(),
            paths.products_dir(),
        ):
            opened.append(open_service_directory(path, create=create))
        return opened[0], opened[1], opened[2], opened[3]
    except BaseException:
        for handle in reversed(opened):
            handle.close()
        raise


def _close_transaction(transaction: _Transaction) -> None:
    fcntl.flock(transaction.record_handle.descriptor, fcntl.LOCK_UN)
    transaction.record_handle.close()
    transaction.products_handle.close()
    fcntl.flock(transaction.archive_handle.descriptor, fcntl.LOCK_UN)
    transaction.archive_handle.close()
    transaction.events_handle.close()
    transaction.queue_handle.close()


def _verify_one_filesystem(handles: list[DirectoryHandle]) -> None:
    devices = {os.fstat(handle.descriptor).st_dev for handle in handles}
    if len(devices) != 1:
        locations = ", ".join(str(handle.path) for handle in handles)
        raise RecalculationError(
            f"recalculation directories must be safe directories on one filesystem: {locations}"
        )


def _new_transaction(
    sequence: int,
    archive_time: Optional[datetime],
) -> tuple[_Transaction, dict[str, tuple[int, str]], DirectoryHandle]:
    queue_handle, events_handle, archive_handle, products_handle = _open_parents(create=True)
    record_handle: Optional[DirectoryHandle] = None
    request_handle: Optional[DirectoryHandle] = None
    archive_locked = False
    record_locked = False
    try:
        try:
            record_handle = _open_child_directory(queue_handle, paths.queue_entry_name(sequence))
        except OSError as exc:
            raise RecalculationError(f"claimed queue record is missing or unsafe: {exc}") from exc
        record = _load_running_record(sequence)
        fcntl.flock(record_handle.descriptor, fcntl.LOCK_EX)
        record_locked = True
        fcntl.flock(archive_handle.descriptor, fcntl.LOCK_EX)
        archive_locked = True
        _revalidate_parent_entry(
            queue_handle,
            paths.queue_entry_name(sequence),
            record_handle,
            "claimed queue record",
        )
        _revalidate_status(record_handle, record)
        manifest, request_handle = _validate_manifest(record_handle, record)
        _verify_one_filesystem(
            [
                queue_handle,
                events_handle,
                archive_handle,
                products_handle,
                record_handle,
                request_handle,
            ]
        )
        if _entry_details(record_handle, TRANSACTION_FILE) is not None:
            raise RecalculationError(f"calculation {sequence} already has a transaction journal")
        if _entry_details(record_handle, STAGING_DIRECTORY) is not None:
            raise RecalculationError(f"calculation {sequence} has unexpected private staging state")
        if _entry_details(record_handle, NATIVE_CANDIDATE_DIRECTORY) is not None:
            raise RecalculationError(
                f"calculation {sequence} has unexpected native candidate state"
            )

        preceding_products = _safe_directory_entry(
            products_handle,
            record.event_id,
            "preceding products tree",
        )
        preceding_service = _safe_directory_entry(
            events_handle,
            record.event_id,
            "preceding service tree",
        )
        record = _persist_missing_counterpart_warning(
            record_handle,
            queue_handle,
            record,
            _missing_counterpart_warning(preceding_products, preceding_service),
        )
        archive_path = (
            _archive_path(archive_handle, record.event_id, archive_time)
            if not record.overwrite and (preceding_products or preceding_service)
            else None
        )
        journal: dict[str, Any] = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "event_id": record.event_id,
            "internal_sequence": record.internal_sequence,
            "overwrite": record.overwrite,
            "preceding": {
                "products": preceding_products,
                "service": preceding_service,
            },
            "archive_path": None if archive_path is None else str(archive_path),
            "completed": [],
            "intents": {
                "stage_removal_started": False,
                "candidate_removal_started": False,
            },
            "recovery_failure": None,
        }
        _write_journal(record_handle, journal)
        transaction = _Transaction(
            record=record,
            record_handle=record_handle,
            queue_handle=queue_handle,
            events_handle=events_handle,
            archive_handle=archive_handle,
            products_handle=products_handle,
            journal=journal,
            location="queue",
        )
        return transaction, manifest, request_handle
    except BaseException:
        if request_handle is not None:
            request_handle.close()
        if record_handle is not None:
            if record_locked:
                fcntl.flock(record_handle.descriptor, fcntl.LOCK_UN)
            record_handle.close()
        products_handle.close()
        if archive_locked:
            fcntl.flock(archive_handle.descriptor, fcntl.LOCK_UN)
        archive_handle.close()
        events_handle.close()
        queue_handle.close()
        raise


def _stage_preceding(transaction: _Transaction) -> DirectoryHandle:
    os.mkdir(STAGING_DIRECTORY, mode=0o700, dir_fd=transaction.record_handle.descriptor)
    _named_fsync(transaction.record_handle.descriptor, "stage_created")
    stage = _open_child_directory(transaction.record_handle, STAGING_DIRECTORY)
    try:
        for checkpoint, source_parent, source_name, destination_name in (
            (
                "products_staged",
                transaction.products_handle,
                transaction.record.event_id,
                "products",
            ),
            (
                "service_staged",
                transaction.events_handle,
                transaction.record.event_id,
                "service",
            ),
        ):
            kind = destination_name
            if transaction.journal["preceding"][kind]:
                _named_rename(
                    source_name,
                    destination_name,
                    source_descriptor=source_parent.descriptor,
                    destination_descriptor=stage.descriptor,
                    name=checkpoint,
                )
                _named_fsync(source_parent.descriptor, f"{checkpoint}_source")
                _named_fsync(stage.descriptor, f"{checkpoint}_destination")
            _named_checkpoint(transaction, checkpoint)
        return stage
    except BaseException:
        stage.close()
        raise


def _dispose_preceding(transaction: _Transaction, stage: DirectoryHandle) -> None:
    preceding = transaction.journal["preceding"]
    has_preceding = preceding["products"] or preceding["service"]
    if not transaction.record.overwrite and has_preceding:
        archive_path = Path(transaction.journal["archive_path"])
        _named_rename(
            STAGING_DIRECTORY,
            archive_path.name,
            source_descriptor=transaction.record_handle.descriptor,
            destination_descriptor=transaction.archive_handle.descriptor,
            name="archive_published",
        )
        stage.close()
        _named_fsync(transaction.record_handle.descriptor, "archive_source")
        _named_fsync(transaction.archive_handle.descriptor, "archive_destination")
    else:
        _named_intent(transaction, "stage_removal_started")
        stage.close()
        shutil.rmtree(STAGING_DIRECTORY, dir_fd=transaction.record_handle.descriptor)
        _named_fsync(transaction.record_handle.descriptor, "stage_removed")
    _named_checkpoint(transaction, "preceding_disposed")


def _promote_record(transaction: _Transaction) -> None:
    _operation_boundary("before", "rename", "record_promoted")
    _revalidate_parent_entry(
        transaction.queue_handle,
        paths.queue_entry_name(transaction.record.internal_sequence),
        transaction.record_handle,
        "claimed queue record",
    )
    _revalidate_status(transaction.record_handle, transaction.record)
    os.rename(
        paths.queue_entry_name(transaction.record.internal_sequence),
        transaction.record.event_id,
        src_dir_fd=transaction.queue_handle.descriptor,
        dst_dir_fd=transaction.events_handle.descriptor,
    )
    _operation_boundary("after", "rename", "record_promoted")
    transaction.location = "events"
    transaction.record_handle.path = paths.event_service_dir(transaction.record.event_id)
    _named_fsync(transaction.queue_handle.descriptor, "record_promoted_source")
    _named_fsync(transaction.events_handle.descriptor, "record_promoted_destination")
    _named_checkpoint(transaction, "record_promoted")


def _copy_manifested_request(
    transaction: _Transaction,
    request_handle: DirectoryHandle,
    manifest: dict[str, tuple[int, str]],
) -> None:
    os.mkdir(
        NATIVE_CANDIDATE_DIRECTORY,
        mode=0o700,
        dir_fd=transaction.record_handle.descriptor,
    )
    candidate = _open_child_directory(transaction.record_handle, NATIVE_CANDIDATE_DIRECTORY)
    current: Optional[DirectoryHandle] = None
    try:
        os.mkdir("current", mode=0o700, dir_fd=candidate.descriptor)
        current = _open_child_directory(candidate, "current")
        for basename, (expected_size, expected_digest) in sorted(manifest.items()):
            source = _open_regular(
                request_handle.descriptor,
                basename,
                f"request snapshot file {basename}",
            )
            destination = os.open(
                basename,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=current.descriptor,
            )
            try:
                digest = hashlib.sha256()
                size = 0
                while True:
                    chunk = os.read(source, COPY_CHUNK_SIZE)
                    if not chunk:
                        break
                    _write_all(destination, chunk)
                    size += len(chunk)
                    digest.update(chunk)
                if size != expected_size or digest.hexdigest() != expected_digest:
                    raise RecalculationError(
                        f"request snapshot changed during native-tree copy: {basename}"
                    )
                os.fsync(destination)
            finally:
                os.close(destination)
                os.close(source)
        _named_fsync(current.descriptor, "candidate_current")
        _named_fsync(candidate.descriptor, "candidate_tree")
        _named_fsync(transaction.record_handle.descriptor, "candidate_parent")
        _named_checkpoint(transaction, "candidate_ready")
    finally:
        if current is not None:
            current.close()
        candidate.close()


def _publish_native_tree(transaction: _Transaction) -> None:
    _named_rename(
        NATIVE_CANDIDATE_DIRECTORY,
        transaction.record.event_id,
        source_descriptor=transaction.record_handle.descriptor,
        destination_descriptor=transaction.products_handle.descriptor,
        name="native_tree_published",
    )
    _named_fsync(transaction.record_handle.descriptor, "native_tree_source")
    _named_fsync(transaction.products_handle.descriptor, "native_tree_destination")
    _named_checkpoint(transaction, "native_tree_published")


def prepare_calculation(
    sequence: int,
    *,
    archive_time: Optional[datetime] = None,
) -> PreparationResult:
    """Replace preceding trees and publish a clean native event directory."""
    transaction, manifest, request_handle = _new_transaction(sequence, archive_time)
    try:
        stage = _stage_preceding(transaction)
        _dispose_preceding(transaction, stage)
        _promote_record(transaction)
        _copy_manifested_request(transaction, request_handle, manifest)
        _publish_native_tree(transaction)
        archive_path = transaction.journal["archive_path"]
        return PreparationResult(
            record=transaction.record,
            native_event_directory=paths.event_products_dir(transaction.record.event_id),
            archive_directory=None if archive_path is None else Path(archive_path),
            preceding_products=transaction.journal["preceding"]["products"],
            preceding_service=transaction.journal["preceding"]["service"],
        )
    finally:
        request_handle.close()
        _close_transaction(transaction)


def _load_journal(record_handle: DirectoryHandle) -> dict[str, Any]:
    journal = _read_json_regular(
        record_handle.descriptor,
        TRANSACTION_FILE,
        TRANSACTION_FILE,
    )
    if not isinstance(journal, dict) or set(journal) != {
        "schema_version",
        "event_id",
        "internal_sequence",
        "overwrite",
        "preceding",
        "archive_path",
        "completed",
        "intents",
        "recovery_failure",
    }:
        raise RecalculationError("transaction journal fields are invalid")
    if (
        type(journal["schema_version"]) is not int
        or journal["schema_version"] != JOURNAL_SCHEMA_VERSION
    ):
        raise RecalculationError("transaction journal schema is unsupported")
    if not isinstance(journal["event_id"], str):
        raise RecalculationError("transaction event_id is invalid")
    validate_event_id(journal["event_id"])
    if (
        isinstance(journal["internal_sequence"], bool)
        or not isinstance(journal["internal_sequence"], int)
        or journal["internal_sequence"] < 1
        or not isinstance(journal["overwrite"], bool)
    ):
        raise RecalculationError("transaction identity is invalid")
    preceding = journal["preceding"]
    if (
        not isinstance(preceding, dict)
        or set(preceding) != {"products", "service"}
        or not all(isinstance(value, bool) for value in preceding.values())
    ):
        raise RecalculationError("transaction preceding-tree evidence is invalid")
    archive_path = journal["archive_path"]
    has_preceding = preceding["products"] or preceding["service"]
    archive_required = not journal["overwrite"] and has_preceding
    if (archive_path is not None) != archive_required:
        raise RecalculationError("transaction archive intent is inconsistent")
    if archive_path is not None:
        _validate_archive_path(journal["event_id"], archive_path)
    completed = journal["completed"]
    if (
        not isinstance(completed, list)
        or not all(isinstance(item, str) for item in completed)
        or len(completed) != len(set(completed))
        or any(item not in _CHECKPOINTS for item in completed)
        or completed != list(_CHECKPOINTS[: len(completed)])
    ):
        raise RecalculationError("transaction checkpoints are invalid")
    intents = journal["intents"]
    if (
        not isinstance(intents, dict)
        or set(intents) != set(_INTENT_NAMES)
        or not all(isinstance(value, bool) for value in intents.values())
    ):
        raise RecalculationError("transaction intents are invalid")
    if intents["stage_removal_started"] and "service_staged" not in completed:
        raise RecalculationError("stage-removal intent precedes staging")
    if intents["candidate_removal_started"] and "record_promoted" not in completed:
        raise RecalculationError("candidate-removal intent precedes promotion")
    recovery_failure = journal["recovery_failure"]
    if recovery_failure is not None:
        if (
            not isinstance(recovery_failure, dict)
            or set(recovery_failure) != {"type", "message", "recorded_at"}
            or not isinstance(recovery_failure["type"], str)
            or not isinstance(recovery_failure["message"], str)
            or not isinstance(recovery_failure["recorded_at"], str)
            or not recovery_failure["recorded_at"].endswith("Z")
        ):
            raise RecalculationError("transaction recovery failure is invalid")
        try:
            datetime.fromisoformat(
                recovery_failure["recorded_at"][:-1] + "+00:00"
            )
        except ValueError as exc:
            raise RecalculationError(
                "transaction recovery failure timestamp is invalid"
            ) from exc
    return journal


def _validate_journal_identity(
    journal: dict[str, Any],
    record: status.CalculationRecord,
) -> None:
    if (
        journal["event_id"] != record.event_id
        or journal["internal_sequence"] != record.internal_sequence
        or journal["overwrite"] != record.overwrite
    ):
        raise RecalculationError(
            "transaction journal identity does not match its calculation record"
        )


def _journal_candidate_present(event_id: str, sequence: int) -> bool:
    for parent_path, entry_name in (
        (paths.queue_dir(), paths.queue_entry_name(sequence)),
        (paths.events_dir(), event_id),
    ):
        try:
            parent = open_service_directory(parent_path, create=False)
        except FileNotFoundError:
            continue
        try:
            if not _safe_directory_entry(parent, entry_name, "calculation record"):
                continue
            record_handle = _open_child_directory(parent, entry_name)
            try:
                if _entry_details(record_handle, TRANSACTION_FILE) is not None:
                    return True
            finally:
                record_handle.close()
        finally:
            parent.close()
    return False


def current_transaction_present(event_id: str) -> bool:
    """Return whether the current service record retains transaction evidence."""
    event_id = validate_event_id(event_id)
    try:
        events_handle = open_service_directory(paths.events_dir(), create=False)
    except FileNotFoundError:
        return False
    try:
        entry_details = _entry_details(events_handle, event_id)
        if entry_details is None:
            if _entry_details(events_handle, event_id) is not None:
                raise RecalculationError(
                    "current calculation record changed while it was inspected"
                )
            _revalidate_service_directory(
                events_handle,
                paths.events_dir(),
                "current-record directory",
            )
            return False
        if not stat.S_ISDIR(entry_details.st_mode):
            raise RecalculationError(
                "current calculation record is not a safe directory: "
                f"{events_handle.path / event_id}"
            )
        record_handle = _open_child_directory(events_handle, event_id)
        try:
            opened_details = os.fstat(record_handle.descriptor)
            if (
                stat.S_IFMT(entry_details.st_mode)
                != stat.S_IFMT(opened_details.st_mode)
                or entry_details.st_dev != opened_details.st_dev
                or entry_details.st_ino != opened_details.st_ino
            ):
                raise RecalculationError(
                    "current calculation record changed while it was opened"
                )
            fcntl.flock(record_handle.descriptor, fcntl.LOCK_SH)
            try:
                present = _entry_details(record_handle, TRANSACTION_FILE) is not None
                _revalidate_parent_entry(
                    events_handle,
                    event_id,
                    record_handle,
                    "current calculation record",
                )
                _revalidate_service_directory(
                    events_handle,
                    paths.events_dir(),
                    "current-record directory",
                )
                return present
            finally:
                fcntl.flock(record_handle.descriptor, fcntl.LOCK_UN)
        finally:
            record_handle.close()
    finally:
        events_handle.close()


def _locate_transaction(event_id: str, sequence: int) -> Optional[_Transaction]:
    if not _journal_candidate_present(event_id, sequence):
        return None
    queue_handle, events_handle, archive_handle, products_handle = _open_parents(create=False)
    record_handle: Optional[DirectoryHandle] = None
    archive_locked = False
    record_locked = False
    try:
        locations: list[tuple[str, DirectoryHandle, str]] = [
            ("queue", queue_handle, paths.queue_entry_name(sequence)),
            ("events", events_handle, event_id),
        ]
        selected: Optional[tuple[str, DirectoryHandle]] = None
        for location, parent, name in locations:
            if not _safe_directory_entry(parent, name, f"{location} calculation record"):
                continue
            candidate = _open_child_directory(parent, name)
            if _entry_details(candidate, TRANSACTION_FILE) is None:
                candidate.close()
                continue
            if selected is not None:
                candidate.close()
                selected[1].close()
                raise RecalculationError(
                    "transaction journal exists in both queue and current state"
                )
            selected = (location, candidate)
        if selected is None:
            products_handle.close()
            archive_handle.close()
            events_handle.close()
            queue_handle.close()
            return None
        location, record_handle = selected
        record = (
            status.read_status(sequence)
            if location == "queue"
            else status.read_current_record(event_id)
        )
        if record is None or record.internal_sequence != sequence:
            raise RecalculationError("transaction record identity does not match its journal")
        fcntl.flock(record_handle.descriptor, fcntl.LOCK_EX)
        record_locked = True
        fcntl.flock(archive_handle.descriptor, fcntl.LOCK_EX)
        archive_locked = True
        selected_parent = queue_handle if location == "queue" else events_handle
        selected_name = paths.queue_entry_name(sequence) if location == "queue" else event_id
        _revalidate_parent_entry(
            selected_parent,
            selected_name,
            record_handle,
            "transaction record",
        )
        _revalidate_status(record_handle, record)
        journal = _load_journal(record_handle)
        if journal["event_id"] != event_id or journal["internal_sequence"] != sequence:
            raise RecalculationError("requested transaction identity does not match journal")
        _validate_journal_identity(journal, record)
        _verify_one_filesystem(
            [queue_handle, events_handle, archive_handle, products_handle, record_handle]
        )
        return _Transaction(
            record=record,
            record_handle=record_handle,
            queue_handle=queue_handle,
            events_handle=events_handle,
            archive_handle=archive_handle,
            products_handle=products_handle,
            journal=journal,
            location=location,
        )
    except BaseException:
        if record_handle is not None:
            if record_locked:
                fcntl.flock(record_handle.descriptor, fcntl.LOCK_UN)
            record_handle.close()
        products_handle.close()
        if archive_locked:
            fcntl.flock(archive_handle.descriptor, fcntl.LOCK_UN)
        archive_handle.close()
        events_handle.close()
        queue_handle.close()
        raise


def _ensure_stage(transaction: _Transaction) -> DirectoryHandle:
    if not _safe_directory_entry(
        transaction.record_handle,
        STAGING_DIRECTORY,
        "private recalculation stage",
    ):
        os.mkdir(STAGING_DIRECTORY, mode=0o700, dir_fd=transaction.record_handle.descriptor)
        _named_fsync(transaction.record_handle.descriptor, "recovery_stage_created")
    return _open_child_directory(transaction.record_handle, STAGING_DIRECTORY)


def _recover_staging(transaction: _Transaction) -> None:
    archive_path = (
        None
        if transaction.journal["archive_path"] is None
        else Path(transaction.journal["archive_path"])
    )
    archive_exists = False
    if archive_path is not None:
        if archive_path.parent != paths.archive_dir():
            raise RecalculationError("transaction archive path is outside the archive directory")
        archive_exists = _safe_directory_entry(
            transaction.archive_handle,
            archive_path.name,
            "transaction archive",
        )
    if transaction.journal["intents"]["stage_removal_started"]:
        if archive_path is not None:
            raise RecalculationError("stage-removal intent conflicts with archive intent")
        if _safe_directory_entry(
            transaction.products_handle,
            transaction.record.event_id,
            "preceding products tree",
        ) or _safe_directory_entry(
            transaction.events_handle,
            transaction.record.event_id,
            "preceding service tree",
        ):
            raise RecalculationError(
                "preceding tree reappeared after stage removal started"
            )
        if _safe_directory_entry(
            transaction.record_handle,
            STAGING_DIRECTORY,
            "private recalculation stage",
        ):
            shutil.rmtree(
                STAGING_DIRECTORY,
                dir_fd=transaction.record_handle.descriptor,
            )
        _named_fsync(transaction.record_handle.descriptor, "recovery_stage_removed")
        _named_checkpoint(transaction, "preceding_disposed")
        return
    if archive_exists:
        if transaction.record.overwrite:
            raise RecalculationError("overwrite transaction unexpectedly has an archive")
        if _entry_details(transaction.record_handle, STAGING_DIRECTORY) is not None:
            raise RecalculationError("published archive and private stage both exist")
        archive = _open_child_directory(transaction.archive_handle, archive_path.name)
        try:
            expected_names = {
                name
                for name in ("products", "service")
                if transaction.journal["preceding"][name]
            }
            if set(os.listdir(archive.descriptor)) != expected_names:
                raise RecalculationError(
                    "published archive contents differ from transaction evidence"
                )
        finally:
            archive.close()
        _named_checkpoint(transaction, "preceding_disposed")
        return

    stage_exists = _safe_directory_entry(
        transaction.record_handle,
        STAGING_DIRECTORY,
        "private recalculation stage",
    )
    completed = transaction.journal["completed"]
    sources_absent = not _safe_directory_entry(
        transaction.products_handle,
        transaction.record.event_id,
        "preceding products tree",
    ) and not _safe_directory_entry(
        transaction.events_handle,
        transaction.record.event_id,
        "preceding service tree",
    )
    preceding = transaction.journal["preceding"]
    has_preceding = preceding["products"] or preceding["service"]
    if (
        not stage_exists
        and sources_absent
        and "products_staged" in completed
        and "service_staged" in completed
        and (transaction.record.overwrite or not has_preceding)
    ):
        _named_checkpoint(transaction, "preceding_disposed")
        return

    stage = _ensure_stage(transaction)
    try:
        for checkpoint, source_parent, source_name, destination_name in (
            (
                "products_staged",
                transaction.products_handle,
                transaction.record.event_id,
                "products",
            ),
            (
                "service_staged",
                transaction.events_handle,
                transaction.record.event_id,
                "service",
            ),
        ):
            source_exists = _safe_directory_entry(
                source_parent,
                source_name,
                f"preceding {destination_name} tree",
            )
            staged_exists = _safe_directory_entry(
                stage,
                destination_name,
                f"staged {destination_name} tree",
            )
            expected = transaction.journal["preceding"][destination_name]
            if not expected and (source_exists or staged_exists):
                raise RecalculationError(
                    f"unexpected {destination_name} tree appeared during reconciliation"
                )
            if expected and source_exists and staged_exists:
                raise RecalculationError(
                    f"preceding and staged {destination_name} trees both exist"
                )
            if expected and source_exists:
                _named_rename(
                    source_name,
                    destination_name,
                    source_descriptor=source_parent.descriptor,
                    destination_descriptor=stage.descriptor,
                    name=f"recovery_{checkpoint}",
                )
                _named_fsync(source_parent.descriptor, f"recovery_{checkpoint}_source")
                _named_fsync(stage.descriptor, f"recovery_{checkpoint}_destination")
                staged_exists = True
            if expected and not staged_exists:
                raise RecalculationError(f"preceding {destination_name} tree is unaccounted for")
            _named_checkpoint(transaction, checkpoint)

        if not transaction.record.overwrite and has_preceding:
            if archive_path is None:
                raise RecalculationError("archive path is missing for retained preceding trees")
            _named_rename(
                STAGING_DIRECTORY,
                archive_path.name,
                source_descriptor=transaction.record_handle.descriptor,
                destination_descriptor=transaction.archive_handle.descriptor,
                name="recovery_archive_published",
            )
            stage.close()
            stage = None
            _named_fsync(transaction.record_handle.descriptor, "recovery_archive_source")
            _named_fsync(transaction.archive_handle.descriptor, "recovery_archive_destination")
        else:
            _named_intent(transaction, "stage_removal_started")
            stage.close()
            stage = None
            shutil.rmtree(STAGING_DIRECTORY, dir_fd=transaction.record_handle.descriptor)
            _named_fsync(transaction.record_handle.descriptor, "recovery_stage_removed")
        _named_checkpoint(transaction, "preceding_disposed")
    finally:
        if stage is not None:
            stage.close()


def _recover_promotion(transaction: _Transaction) -> None:
    if transaction.location == "events":
        _named_checkpoint(transaction, "record_promoted")
        return
    if _safe_directory_entry(
        transaction.events_handle,
        transaction.record.event_id,
        "current service record",
    ):
        raise RecalculationError("current service destination is occupied before promotion")
    _operation_boundary("before", "rename", "recovery_record_promoted")
    _revalidate_parent_entry(
        transaction.queue_handle,
        paths.queue_entry_name(transaction.record.internal_sequence),
        transaction.record_handle,
        "transaction queue record",
    )
    _revalidate_status(transaction.record_handle, transaction.record)
    os.rename(
        paths.queue_entry_name(transaction.record.internal_sequence),
        transaction.record.event_id,
        src_dir_fd=transaction.queue_handle.descriptor,
        dst_dir_fd=transaction.events_handle.descriptor,
    )
    _operation_boundary("after", "rename", "recovery_record_promoted")
    transaction.location = "events"
    transaction.record_handle.path = paths.event_service_dir(transaction.record.event_id)
    _named_fsync(transaction.queue_handle.descriptor, "recovery_record_source")
    _named_fsync(transaction.events_handle.descriptor, "recovery_record_destination")
    _named_checkpoint(transaction, "record_promoted")


def _verify_disposition_after_promotion(transaction: _Transaction) -> None:
    if _entry_details(transaction.record_handle, STAGING_DIRECTORY) is not None:
        raise RecalculationError("private recalculation stage remains after promotion")
    archive_path = transaction.journal["archive_path"]
    if archive_path is None:
        return
    archive_name = Path(archive_path).name
    if not _safe_directory_entry(
        transaction.archive_handle,
        archive_name,
        "transaction archive",
    ):
        raise RecalculationError("transaction archive is missing after promotion")
    archive = _open_child_directory(transaction.archive_handle, archive_name)
    try:
        expected_names = {
            name
            for name in ("products", "service")
            if transaction.journal["preceding"][name]
        }
        if set(os.listdir(archive.descriptor)) != expected_names:
            raise RecalculationError(
                "published archive contents differ from transaction evidence"
            )
    finally:
        archive.close()


def _remove_private_candidate(transaction: _Transaction) -> bool:
    candidate_exists = _safe_directory_entry(
        transaction.record_handle,
        NATIVE_CANDIDATE_DIRECTORY,
        "private native candidate",
    )
    public_exists = _safe_directory_entry(
        transaction.products_handle,
        transaction.record.event_id,
        "published native event tree",
    )
    removal_started = transaction.journal["intents"][
        "candidate_removal_started"
    ]
    if removal_started:
        if public_exists:
            raise RecalculationError(
                "candidate-removal intent conflicts with a published native tree"
            )
        if candidate_exists:
            shutil.rmtree(
                NATIVE_CANDIDATE_DIRECTORY,
                dir_fd=transaction.record_handle.descriptor,
            )
        _named_fsync(
            transaction.record_handle.descriptor,
            "recovery_candidate_removed",
        )
        return False
    if candidate_exists and public_exists:
        raise RecalculationError("private and published native event trees both exist")
    completed = transaction.journal["completed"]
    if public_exists and "candidate_ready" not in completed:
        raise RecalculationError("published native event tree has no ready checkpoint")
    if not public_exists and "native_tree_published" in completed:
        raise RecalculationError("published native event tree is missing")
    if (
        not candidate_exists
        and not public_exists
        and "candidate_ready" in completed
    ):
        raise RecalculationError("ready native event-tree candidate is missing")
    if candidate_exists:
        _named_intent(transaction, "candidate_removal_started")
        shutil.rmtree(
            NATIVE_CANDIDATE_DIRECTORY,
            dir_fd=transaction.record_handle.descriptor,
        )
        _named_fsync(transaction.record_handle.descriptor, "recovery_candidate_removed")
    return public_exists


def _mark_failed(transaction: _Transaction, message: str) -> None:
    if transaction.record.status == status.LifecycleState.FAILED.value:
        return
    if transaction.record.status != status.LifecycleState.RUNNING.value:
        raise RecalculationError(
            f"interrupted calculation has unexpected state {transaction.record.status}"
        )
    fcntl.flock(transaction.record_handle.descriptor, fcntl.LOCK_UN)
    try:
        transaction.record = status.transition_current_record(
            transaction.record.event_id,
            status.LifecycleState.FAILED,
            failure={
                "code": "interrupted_recalculation",
                "message": message,
            },
            service_outcome={"completed": True, "successful": False},
        )
    finally:
        fcntl.flock(transaction.record_handle.descriptor, fcntl.LOCK_EX)
    _revalidate_parent_entry(
        transaction.events_handle,
        transaction.record.event_id,
        transaction.record_handle,
        "current calculation record",
    )
    _revalidate_status(transaction.record_handle, transaction.record)


def _remove_journal(record_handle: DirectoryHandle) -> None:
    try:
        os.unlink(TRANSACTION_FILE, dir_fd=record_handle.descriptor)
    except FileNotFoundError:
        return
    os.fsync(record_handle.descriptor)


def _record_recovery_failure(transaction: _Transaction, error: BaseException) -> None:
    transaction.journal["recovery_failure"] = {
        "type": type(error).__name__,
        "message": str(error),
        "recorded_at": _now_iso(),
    }
    try:
        _write_journal(transaction.record_handle, transaction.journal)
    except BaseException as journal_error:
        raise RecalculationError(
            f"reconciliation failed: {error}; recording failure evidence also "
            f"failed: {journal_error}"
        ) from error


def reconcile_transaction(event_id: str, sequence: int) -> bool:
    """Finish durable disposition, fail interrupted work, and never execute it."""
    event_id = validate_event_id(event_id)
    transaction = _locate_transaction(event_id, sequence)
    if transaction is None:
        return False
    try:
        try:
            if transaction.location == "queue":
                _recover_staging(transaction)
            _recover_promotion(transaction)
            _verify_disposition_after_promotion(transaction)
            published = _remove_private_candidate(transaction)
            message = (
                "calculation was interrupted after native event-tree publication; "
                "published files were preserved and native execution was not retried"
                if published
                else "calculation was interrupted before native event-tree publication; "
                "private candidate files were removed and native execution was not started"
            )
            _mark_failed(transaction, message)
            _remove_journal(transaction.record_handle)
            return True
        except BaseException as exc:
            _record_recovery_failure(transaction, exc)
            raise RecalculationError(f"could not reconcile calculation {sequence}: {exc}") from exc
    finally:
        _close_transaction(transaction)


def recover_stale_running_calculation(event_id: str, sequence: int) -> bool:
    """Fail one interrupted RUNNING record without retrying native work."""
    event_id = validate_event_id(event_id)
    paths.queue_entry_name(sequence)
    failure = {
        "code": "interrupted_without_transaction",
        "message": (
            "calculation was found RUNNING without a transaction journal; "
            "native execution was not retried"
        ),
    }
    for record_directory in (
        paths.queue_entry_dir(sequence),
        paths.event_service_dir(event_id),
    ):
        result = status.fail_matching_running_record_if_child_absent(
            record_directory,
            expected_event_id=event_id,
            expected_sequence=sequence,
            blocking_child=TRANSACTION_FILE,
            failure=failure,
        )
        if result == status.RunningRecordRecoveryResult.BLOCKING_CHILD_PRESENT:
            return reconcile_transaction(event_id, sequence)
        if result == status.RunningRecordRecoveryResult.FAILED:
            return True
        if result == status.RunningRecordRecoveryResult.NOT_RUNNING:
            return False
    return False


def finalize_transaction(event_id: str) -> bool:
    """Remove a journal only after its current record is durably terminal."""
    event_id = validate_event_id(event_id)
    record = status.read_current_record(event_id)
    if record is None:
        raise RecalculationError(f"current calculation record does not exist: {event_id}")
    if record.status not in {
        status.LifecycleState.SUCCESS.value,
        status.LifecycleState.FAILED.value,
    }:
        raise RecalculationError(
            f"calculation {event_id!r} is not terminal: {record.status}"
        )
    try:
        events_handle = open_service_directory(paths.events_dir(), create=False)
    except FileNotFoundError as exc:
        raise RecalculationError(
            f"current calculation record does not exist: {event_id}"
        ) from exc
    try:
        record_handle = _open_child_directory(events_handle, event_id)
    except OSError as exc:
        events_handle.close()
        raise RecalculationError(
            f"current calculation record does not exist: {event_id}"
        ) from exc
    fcntl.flock(record_handle.descriptor, fcntl.LOCK_EX)
    try:
        _revalidate_parent_entry(
            events_handle,
            event_id,
            record_handle,
            "current calculation record",
        )
        _revalidate_status(record_handle, record)
        if _entry_details(record_handle, TRANSACTION_FILE) is None:
            return False
        journal = _load_journal(record_handle)
        _validate_journal_identity(journal, record)
        if journal["completed"] != list(_CHECKPOINTS):
            raise RecalculationError("transaction journal is not fully completed")
        if journal["recovery_failure"] is not None:
            raise RecalculationError("transaction journal contains recovery failure evidence")
        if journal["intents"]["candidate_removal_started"]:
            raise RecalculationError("transaction journal contains recovery removal intent")
        _remove_journal(record_handle)
        return True
    finally:
        fcntl.flock(record_handle.descriptor, fcntl.LOCK_UN)
        record_handle.close()
        events_handle.close()
