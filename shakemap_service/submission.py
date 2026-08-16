# -*- coding: utf-8 -*-
"""Streamed caller-input publication and durable request acceptance."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable, Optional

from . import paths
from .directory_access import (
    DirectoryHandle as _DirectoryGuard,
    directory_open_flags as _directory_open_flags,
    open_service_directory as _open_service_directory,
)
from .request_validation import (
    validate_configuration_name,
    validate_event_id,
    validate_overwrite,
    validate_upload_basename,
)
from .status import (
    CalculationRecord,
    _record_to_dict,
    fsync_directory,
    new_queued_record,
)


REQUIRED_EVENT_FILE = "event.xml"
COPY_CHUNK_SIZE = 64 * 1024
UPLOAD_PRIVATE_DIRECTORY = ".uploads"
UPLOAD_STREAM_PREFIX = "stream-"
UPLOAD_PRECEDING_PREFIX = "preceding-"
UPLOAD_PRIVATE_SUFFIX = ".tmp"


@dataclass(frozen=True)
class Upload:
    basename: str
    stream: BinaryIO


@dataclass(frozen=True)
class SubmissionResult:
    event_id: str
    internal_sequence: int
    status: str
    warnings: tuple[str, ...]
    requested_configuration: str
    overwrite: bool
    status_path: str
    shared_input_path: str
    shared_products_path: str


class PrivateRequestCleanupError(RuntimeError):
    """The unpublished request tree could not be completely removed."""


class InputValidationError(ValueError):
    """A request or caller input failed validation before acceptance."""


class InputPublicationError(RuntimeError):
    """A caller upload could not be safely published before acceptance."""

    def __init__(self, message: str, *, retain_private_tree: bool = False) -> None:
        super().__init__(message)
        self._retain_private_tree = retain_private_tree


class InputSnapshotError(RuntimeError):
    """Caller input could not be read into a complete private snapshot."""


def _write_bytes_sync(
    target: Path,
    payload: bytes,
    directory_descriptor: Optional[int] = None,
) -> None:
    open_target: object = target if directory_descriptor is None else target.name
    descriptor = os.open(
        open_target,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=directory_descriptor,
    )
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise OSError(f"zero-byte write while publishing {target}")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_json_sync(
    target: Path,
    data: dict[str, object],
    directory_descriptor: Optional[int] = None,
) -> None:
    payload = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _write_bytes_sync(target, payload, directory_descriptor)


def _make_private_queue_directory(
    guard: _DirectoryGuard,
) -> tuple[Path, _DirectoryGuard]:
    for _ in range(100):
        name = f".accept-{uuid.uuid4().hex}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=guard.descriptor)
        except FileExistsError:
            continue
        path = guard.path / name
        try:
            os.fsync(guard.descriptor)
            descriptor = os.open(
                name,
                _directory_open_flags(),
                dir_fd=guard.descriptor,
            )
            return path, _DirectoryGuard(
                path=path,
                descriptor=descriptor,
            )
        except BaseException as creation_error:
            _cleanup_private_queue_directory(path, guard, creation_error)
            raise
    raise FileExistsError(
        f"could not allocate a private queue directory in {guard.path}"
    )


def _private_queue_directory_exists(
    guard: _DirectoryGuard,
    name: str,
) -> bool:
    try:
        os.stat(name, dir_fd=guard.descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _sync_private_queue_cleanup(guard: _DirectoryGuard) -> None:
    os.fsync(guard.descriptor)


def _cleanup_private_queue_directory(
    temporary: Path,
    guard: _DirectoryGuard,
    acceptance_error: BaseException,
) -> None:
    removal_error: Optional[BaseException] = None
    try:
        shutil.rmtree(temporary.name, dir_fd=guard.descriptor)
    except BaseException as exc:
        removal_error = exc

    retained = _private_queue_directory_exists(guard, temporary.name)
    sync_error: Optional[OSError] = None
    try:
        _sync_private_queue_cleanup(guard)
    except OSError as exc:
        sync_error = exc

    if retained:
        durability = sync_error is None
        detail = (
            f"removal failed: {removal_error}"
            if removal_error
            else "removal was incomplete"
        )
        if sync_error is not None:
            detail += f"; cleanup sync failed: {sync_error}"
        raise PrivateRequestCleanupError(
            f"request acceptance failed: {acceptance_error}; incomplete private "
            f"material remains at retained path {temporary}; {detail}; cleanup "
            f"directory sync completed={durability}",
        ) from acceptance_error
    if sync_error is not None:
        raise PrivateRequestCleanupError(
            f"request acceptance failed: {acceptance_error}; private cleanup path "
            f"{temporary} is absent from the live namespace, but cleanup sync "
            f"failed: {sync_error}",
        ) from acceptance_error


def _safe_regular_names(
    directory: Path,
    guard: Optional[_DirectoryGuard] = None,
) -> list[str]:
    names: list[str] = []
    scan_target: object = directory if guard is None else guard.descriptor
    with os.scandir(scan_target) as entries:
        for entry in entries:
            if not entry.is_file(follow_symlinks=False):
                # Nested content and links remain caller-owned but are not inputs.
                continue
            try:
                validate_upload_basename(entry.name)
            except ValueError:
                continue
            names.append(entry.name)
    return sorted(names)


def _validated_uploads(uploads: Iterable[Upload]) -> list[Upload]:
    prepared = list(uploads)
    basenames: set[str] = set()
    duplicates: set[str] = set()
    for upload in prepared:
        if not isinstance(upload, Upload):
            raise ValueError("each upload must be an Upload")
        basename = validate_upload_basename(upload.basename)
        if basename in basenames:
            duplicates.add(basename)
        basenames.add(basename)
        if not callable(getattr(upload.stream, "read", None)):
            raise ValueError(f"upload {upload.basename!r} is not a readable stream")
    if duplicates:
        raise ValueError(
            "duplicate upload basenames are not allowed: "
            + ", ".join(sorted(duplicates))
        )
    return prepared


def _remove_private_upload(
    private_file: Path,
    upload_guard: _DirectoryGuard,
    *,
    missing_ok: bool,
) -> None:
    try:
        os.unlink(private_file.name, dir_fd=upload_guard.descriptor)
    except FileNotFoundError:
        if not missing_ok:
            raise


def _sync_private_uploads(upload_guard: _DirectoryGuard) -> None:
    os.fsync(upload_guard.descriptor)


def _private_cleanup_problem(
    private_file: Path,
    upload_guard: _DirectoryGuard,
) -> Optional[str]:
    problems: list[str] = []
    try:
        _remove_private_upload(private_file, upload_guard, missing_ok=True)
    except OSError as exc:
        problems.append(f"removing {private_file} failed: {exc}")
    try:
        _sync_private_uploads(upload_guard)
    except OSError as exc:
        problems.append(f"syncing {upload_guard.path} failed: {exc}")
    return "; ".join(problems) or None


def _raise_input_publication_error(
    message: str,
    cause: BaseException,
    *,
    cleanup_problem: Optional[str] = None,
    retain_private_tree: bool = False,
) -> None:
    if cleanup_problem is not None:
        message = f"{message}; secondary private cleanup failure: {cleanup_problem}"
        retain_private_tree = True
    raise InputPublicationError(
        message,
        retain_private_tree=retain_private_tree,
    ) from cause


def _verify_atomic_upload_filesystem(
    input_guard: _DirectoryGuard,
    upload_guard: _DirectoryGuard,
) -> None:
    if os.fstat(input_guard.descriptor).st_dev == os.fstat(
        upload_guard.descriptor
    ).st_dev:
        return
    cause = OSError(
        f"caller input directory {input_guard.path} and private upload directory "
        f"{upload_guard.path} are on different filesystems"
    )
    raise InputPublicationError(
        "upload publication requires caller input and private upload storage "
        "on the same filesystem"
    ) from cause


def _stream_upload(
    directory: Path,
    upload: Upload,
    guard: _DirectoryGuard,
) -> Path:
    private_file = directory / (
        f"{UPLOAD_STREAM_PREFIX}{uuid.uuid4().hex}{UPLOAD_PRIVATE_SUFFIX}"
    )
    descriptor = os.open(
        private_file.name,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=guard.descriptor,
    )
    try:
        with os.fdopen(descriptor, "wb") as destination:
            descriptor = -1
            while True:
                chunk = upload.stream.read(COPY_CHUNK_SIZE)
                if chunk == b"":
                    break
                if chunk is None:
                    raise ValueError(
                        f"upload {upload.basename!r} ended without a byte-stream EOF"
                    )
                if not isinstance(chunk, bytes):
                    raise ValueError(
                        f"upload {upload.basename!r} returned non-byte content"
                    )
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        return private_file
    except Exception as stream_error:
        cleanup_problem = _private_cleanup_problem(private_file, guard)
        _raise_input_publication_error(
            f"could not stream upload {upload.basename!r} into private file "
            f"{private_file}: {stream_error}",
            stream_error,
            cleanup_problem=cleanup_problem,
        )
        raise AssertionError("upload failure reporting unexpectedly returned")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _existing_regular_file(
    target: Path,
    guard: Optional[_DirectoryGuard] = None,
) -> bool:
    try:
        if guard is None:
            details = target.lstat()
        else:
            details = os.stat(
                target.name,
                dir_fd=guard.descriptor,
                follow_symlinks=False,
            )
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(details.st_mode):
        raise ValueError(
            f"upload destination {target.name!r} exists but is not a safe regular file"
        )
    return True


def _replace_input_name(
    source: Path,
    destination: Path,
    source_guard: _DirectoryGuard,
    destination_guard: _DirectoryGuard,
) -> None:
    os.replace(
        source.name,
        destination.name,
        src_dir_fd=source_guard.descriptor,
        dst_dir_fd=destination_guard.descriptor,
    )


def _sync_input_storage(
    directory: Path,
    guard: Optional[_DirectoryGuard],
) -> None:
    if guard is None:
        fsync_directory(directory)
    else:
        os.fsync(guard.descriptor)


def _sync_input_directory(
    directory: Path,
    guard: Optional[_DirectoryGuard],
) -> None:
    _sync_input_storage(directory, guard)


def _replacement_warning(basename: str) -> str:
    return f"Uploaded {basename!r} replaced the existing caller input file"


def _rollback_replacement(
    private_file: Path,
    target: Path,
    backup: Path,
    input_guard: _DirectoryGuard,
    upload_guard: _DirectoryGuard,
    publication_error: BaseException,
) -> None:
    try:
        # One rollback is the boundary because another recovery rewrite could
        # destroy the only remaining name for the caller's preceding bytes.
        _replace_input_name(backup, target, upload_guard, input_guard)
    except Exception as rollback_error:
        raise InputPublicationError(
            f"upload replacement failed for {target}: {publication_error}; "
            f"one rollback from {backup} also failed: {rollback_error}; "
            f"unpublished request tree retained at {upload_guard.path.parent}",
            retain_private_tree=True,
        ) from publication_error

    secondary: list[str] = []
    try:
        _sync_input_directory(target.parent, input_guard)
    except Exception as sync_error:
        secondary.append(
            f"syncing restored caller input directory {target.parent} failed: "
            f"{sync_error}"
        )
    cleanup_problem = _private_cleanup_problem(private_file, upload_guard)
    if cleanup_problem is not None:
        secondary.append(cleanup_problem)

    message = (
        f"upload replacement failed for {target}: {publication_error}; "
        f"the preceding bytes were restored from {backup}"
    )
    if secondary:
        message += "; secondary rollback cleanup failure: " + "; ".join(secondary)
    raise InputPublicationError(
        message,
        retain_private_tree=cleanup_problem is not None,
    ) from publication_error


def _replace_existing_upload(
    private_file: Path,
    target: Path,
    input_guard: _DirectoryGuard,
    upload_guard: _DirectoryGuard,
) -> None:
    backup = upload_guard.path / (
        f"{UPLOAD_PRECEDING_PREFIX}{uuid.uuid4().hex}{UPLOAD_PRIVATE_SUFFIX}"
    )
    try:
        # The hard link keeps the preceding caller bytes available if the
        # replacement or its directory sync fails after the atomic rename.
        os.link(
            target.name,
            backup.name,
            src_dir_fd=input_guard.descriptor,
            dst_dir_fd=upload_guard.descriptor,
            follow_symlinks=False,
        )
    except Exception as backup_error:
        cleanup_problem = _private_cleanup_problem(private_file, upload_guard)
        _raise_input_publication_error(
            f"could not create replacement backup {backup} for {target}: "
            f"{backup_error}",
            backup_error,
            cleanup_problem=cleanup_problem,
        )
        raise AssertionError("backup failure reporting unexpectedly returned")

    try:
        _replace_input_name(private_file, target, upload_guard, input_guard)
    except Exception as publication_error:
        _rollback_replacement(
            private_file,
            target,
            backup,
            input_guard,
            upload_guard,
            publication_error,
        )
        raise AssertionError("replacement rollback unexpectedly returned")

    try:
        _sync_input_directory(target.parent, input_guard)
    except Exception as publication_error:
        _rollback_replacement(
            private_file,
            target,
            backup,
            input_guard,
            upload_guard,
            publication_error,
        )
        raise AssertionError("replacement rollback unexpectedly returned")

    try:
        _remove_private_upload(backup, upload_guard, missing_ok=False)
    except Exception as cleanup_error:
        _rollback_replacement(
            private_file,
            target,
            backup,
            input_guard,
            upload_guard,
            cleanup_error,
        )
        raise AssertionError("replacement rollback unexpectedly returned")

    try:
        _sync_private_uploads(upload_guard)
    except Exception as cleanup_error:
        raise InputPublicationError(
            f"replacement was published at {target}, but syncing private upload "
            f"cleanup directory {upload_guard.path} failed: {cleanup_error}"
        ) from cleanup_error


def _publish_new_upload(
    private_file: Path,
    target: Path,
    input_guard: _DirectoryGuard,
    upload_guard: _DirectoryGuard,
) -> None:
    try:
        os.link(
            private_file.name,
            target.name,
            src_dir_fd=upload_guard.descriptor,
            dst_dir_fd=input_guard.descriptor,
            follow_symlinks=False,
        )
    except Exception as publication_error:
        cleanup_problem = _private_cleanup_problem(private_file, upload_guard)
        _raise_input_publication_error(
            f"could not publish new caller input {target} without clobbering an "
            f"existing name: {publication_error}",
            publication_error,
            cleanup_problem=cleanup_problem,
        )
        raise AssertionError("new upload failure reporting unexpectedly returned")

    try:
        _sync_input_directory(target.parent, input_guard)
    except Exception as publication_error:
        cleanup_problem = _private_cleanup_problem(private_file, upload_guard)
        _raise_input_publication_error(
            f"new caller input {target} was linked, but syncing input directory "
            f"{target.parent} failed: {publication_error}",
            publication_error,
            cleanup_problem=cleanup_problem,
        )
        raise AssertionError("new upload sync reporting unexpectedly returned")

    try:
        _remove_private_upload(private_file, upload_guard, missing_ok=False)
    except Exception as cleanup_error:
        raise InputPublicationError(
            f"new caller input {target} was published, but removing private "
            f"upload file {private_file} failed: {cleanup_error}; unpublished "
            f"request tree retained at {upload_guard.path.parent}",
            retain_private_tree=True,
        ) from cleanup_error

    try:
        _sync_private_uploads(upload_guard)
    except Exception as cleanup_error:
        raise InputPublicationError(
            f"new caller input {target} was published, but syncing private upload "
            f"cleanup directory {upload_guard.path} failed: {cleanup_error}"
        ) from cleanup_error


def _publish_upload(
    private_file: Path,
    target: Path,
    input_guard: _DirectoryGuard,
    upload_guard: _DirectoryGuard,
    *,
    replace_existing: bool,
) -> bool:
    if replace_existing:
        _replace_existing_upload(
            private_file,
            target,
            input_guard,
            upload_guard,
        )
        return True
    _publish_new_upload(
        private_file,
        target,
        input_guard,
        upload_guard,
    )
    return False


def _copy_regular_file(
    source: Path,
    destination: Path,
    source_guard: Optional[_DirectoryGuard] = None,
    destination_directory_descriptor: Optional[int] = None,
) -> tuple[int, str]:
    if source_guard is None:
        source_details = source.lstat()
    else:
        source_details = os.stat(
            source.name,
            dir_fd=source_guard.descriptor,
            follow_symlinks=False,
        )
    if not stat.S_ISREG(source_details.st_mode):
        raise ValueError(f"input is no longer a safe regular file: {source.name}")
    if source_guard is None:
        source_descriptor = os.open(
            source,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
    else:
        source_descriptor = os.open(
            source.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=source_guard.descriptor,
        )
    opened_destination_descriptor: Optional[int] = None
    try:
        opened_details = os.fstat(source_descriptor)
        # Comparing identity as well as type closes the lstat/open symlink race
        # on platforms where O_NOFOLLOW is unavailable.
        if (
            not stat.S_ISREG(opened_details.st_mode)
            or opened_details.st_dev != source_details.st_dev
            or opened_details.st_ino != source_details.st_ino
        ):
            raise ValueError(f"input changed while it was opened: {source.name}")
        destination_target: object = (
            destination
            if destination_directory_descriptor is None
            else destination.name
        )
        opened_destination_descriptor = os.open(
            destination_target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=destination_directory_descriptor,
        )
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(source_descriptor, COPY_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(opened_destination_descriptor, view)
                if written < 1:
                    raise OSError(
                        f"zero-byte write while snapshotting {source.name}"
                    )
                view = view[written:]
        os.fsync(opened_destination_descriptor)
        return size, digest.hexdigest()
    finally:
        os.close(source_descriptor)
        if opened_destination_descriptor is not None:
            os.close(opened_destination_descriptor)


def _snapshot_inputs(
    input_directory: Path,
    request_directory: Path,
    input_guard: Optional[_DirectoryGuard] = None,
    private_guard: Optional[_DirectoryGuard] = None,
) -> list[dict[str, object]]:
    names = _safe_regular_names(input_directory, input_guard)
    if REQUIRED_EVENT_FILE not in names:
        raise ValueError(
            f"a readable regular {REQUIRED_EVENT_FILE} is required in the "
            "canonical event input directory"
        )
    request_descriptor: Optional[int] = None
    if private_guard is None:
        request_directory.mkdir(parents=False, exist_ok=False)
    else:
        os.mkdir("request", mode=0o700, dir_fd=private_guard.descriptor)
        request_descriptor = os.open(
            "request",
            _directory_open_flags(),
            dir_fd=private_guard.descriptor,
        )
    manifest_files: list[dict[str, object]] = []
    try:
        for basename in names:
            size, digest = _copy_regular_file(
                input_directory / basename,
                request_directory / basename,
                input_guard,
                request_descriptor,
            )
            manifest_files.append(
                {"basename": basename, "size_bytes": size, "sha256": digest}
            )
        # The snapshot directory is synced only after every child file is synced.
        if request_descriptor is None:
            fsync_directory(request_directory)
        else:
            os.fsync(request_descriptor)
            os.fsync(private_guard.descriptor)
    finally:
        if request_descriptor is not None:
            os.close(request_descriptor)
    return manifest_files


def _queue_sequence_from_disk(
    root: Path,
    guard: Optional[_DirectoryGuard] = None,
) -> int:
    existing_sequences: list[int] = []
    entry_names = (
        [entry.name for entry in root.iterdir()]
        if guard is None
        else os.listdir(guard.descriptor)
    )
    for entry_name in entry_names:
        if entry_name.startswith("."):
            continue
        try:
            existing_sequences.append(paths.parse_queue_entry_name(entry_name))
        except ValueError:
            continue
    # A stale counter must never move behind a visible numeric directory,
    # because doing so could assign an earlier request's sequence again.
    filesystem_next = max(existing_sequences, default=0) + 1
    state = paths.queue_sequence_file()
    try:
        if guard is None:
            state_details = state.lstat()
        else:
            state_details = os.stat(
                state.name,
                dir_fd=guard.descriptor,
                follow_symlinks=False,
            )
    except FileNotFoundError:
        return filesystem_next
    if not stat.S_ISREG(state_details.st_mode):
        raise ValueError(f"queue sequence state is not a safe regular file: {state}")
    state_target: object = state if guard is None else state.name
    descriptor = os.open(
        state_target,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=None if guard is None else guard.descriptor,
    )
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ValueError(
                f"queue sequence state is not a safe regular file: {state}"
            )
        with os.fdopen(descriptor, "r", encoding="ascii") as stream:
            descriptor = -1
            text = stream.read().strip()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not text.isascii() or not text.isdigit() or int(text) < 1:
        raise ValueError(f"malformed queue sequence state: {state}")
    return max(int(text), filesystem_next)


def _store_next_sequence(
    root: Path,
    next_sequence: int,
    guard: Optional[_DirectoryGuard] = None,
) -> None:
    state = paths.queue_sequence_file()
    temporary = root / f".next-sequence-{uuid.uuid4().hex}.tmp"
    try:
        _write_bytes_sync(
            temporary,
            f"{next_sequence}\n".encode("ascii"),
            None if guard is None else guard.descriptor,
        )
        if guard is None:
            os.replace(temporary, state)
        else:
            os.replace(
                temporary.name,
                state.name,
                src_dir_fd=guard.descriptor,
                dst_dir_fd=guard.descriptor,
            )
        # Advancing the counter before queue publication permits gaps after a
        # crash, but it prevents any accepted sequence from ever being reused.
        if guard is None:
            fsync_directory(root)
        else:
            os.fsync(guard.descriptor)
    finally:
        if guard is None:
            temporary.unlink(missing_ok=True)
        else:
            try:
                os.unlink(temporary.name, dir_fd=guard.descriptor)
            except FileNotFoundError:
                pass


def _sync_published_queue_parent(
    root: Path,
    queue_guard: Optional[_DirectoryGuard],
) -> None:
    if queue_guard is None:
        fsync_directory(root)
    else:
        os.fsync(queue_guard.descriptor)


def _publish_queue_entry(
    temporary: Path,
    *,
    event_id: str,
    configuration: str,
    overwrite: bool,
    warnings: list[str],
    input_mode: str,
    manifest_files: list[dict[str, object]],
    queue_guard: Optional[_DirectoryGuard] = None,
    private_guard: Optional[_DirectoryGuard] = None,
) -> CalculationRecord:
    root = paths.queue_dir()
    coordination_descriptor = (
        os.open(root, _directory_open_flags())
        if queue_guard is None
        else queue_guard.descriptor
    )
    fcntl.flock(coordination_descriptor, fcntl.LOCK_EX)
    try:
        return _publish_queue_entry_locked(
            temporary,
            event_id=event_id,
            configuration=configuration,
            overwrite=overwrite,
            warnings=warnings,
            input_mode=input_mode,
            manifest_files=manifest_files,
            root=root,
            queue_guard=queue_guard,
            private_guard=private_guard,
        )
    finally:
        fcntl.flock(coordination_descriptor, fcntl.LOCK_UN)
        if queue_guard is None:
            os.close(coordination_descriptor)


def _publish_queue_entry_locked(
    temporary: Path,
    *,
    event_id: str,
    configuration: str,
    overwrite: bool,
    warnings: list[str],
    input_mode: str,
    manifest_files: list[dict[str, object]],
    root: Path,
    queue_guard: Optional[_DirectoryGuard],
    private_guard: Optional[_DirectoryGuard],
) -> CalculationRecord:
    internal_sequence = _queue_sequence_from_disk(root, queue_guard)
    record = new_queued_record(
        event_id=event_id,
        internal_sequence=internal_sequence,
        selected_configuration=configuration,
        overwrite=overwrite,
        warnings=warnings,
        input_mode=input_mode,
    )
    _write_json_sync(
        temporary / "request-manifest.json",
        {
            "schema_version": 1,
            "event_id": event_id,
            "internal_sequence": internal_sequence,
            "files": manifest_files,
        },
        None if private_guard is None else private_guard.descriptor,
    )
    _write_json_sync(
        temporary / "status.json",
        _record_to_dict(record),
        None if private_guard is None else private_guard.descriptor,
    )
    # Sync the complete private entry before its single publication rename.
    if private_guard is None:
        fsync_directory(temporary)
    else:
        os.fsync(private_guard.descriptor)
    _store_next_sequence(root, internal_sequence + 1, queue_guard)

    final = paths.queue_entry_dir(internal_sequence)
    if queue_guard is None:
        final_exists = os.path.lexists(final)
    else:
        try:
            os.stat(
                final.name,
                dir_fd=queue_guard.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            final_exists = False
        else:
            final_exists = True
    if final_exists:
        raise FileExistsError(f"queue entry already exists: {final}")
    if queue_guard is None:
        os.rename(temporary, final)
    else:
        os.rename(
            temporary.name,
            final.name,
            src_dir_fd=queue_guard.descriptor,
            dst_dir_fd=queue_guard.descriptor,
        )
    # Sync the queue directory before acknowledging the published entry.
    _sync_published_queue_parent(root, queue_guard)
    return record


def accept_request(
    event_id: str,
    uploads: Iterable[Upload] = (),
    *,
    configuration: str = "global",
    overwrite: bool = True,
) -> SubmissionResult:
    """Publish caller uploads and durably accept one immutable queue snapshot."""
    try:
        event_id = validate_event_id(event_id)
        configuration = validate_configuration_name(configuration)
        overwrite = validate_overwrite(overwrite)
        prepared_uploads = _validated_uploads(uploads)
    except ValueError as exc:
        raise InputValidationError(str(exc)) from exc

    input_directory = paths.event_input_dir(event_id)
    try:
        input_guard = _open_service_directory(
            input_directory,
            create=bool(prepared_uploads),
        )
    except FileNotFoundError as exc:
        raise InputValidationError(
            "canonical event input directory does not exist and no uploads were supplied"
        ) from exc
    except OSError as exc:
        raise InputSnapshotError(
            "canonical event input directory could not be opened safely"
        ) from exc
    input_locked = False
    try:
        # Hold this event's input stable from upload publication through its
        # immutable snapshot; other event directories use independent locks.
        fcntl.flock(input_guard.descriptor, fcntl.LOCK_EX)
        input_locked = True
        try:
            existing_names = _safe_regular_names(input_directory, input_guard)
        except OSError as exc:
            raise InputSnapshotError(
                "canonical event input directory could not be read safely"
            ) from exc
        try:
            replacement_by_name = {
                upload.basename: _existing_regular_file(
                    input_directory / upload.basename,
                    input_guard,
                )
                for upload in prepared_uploads
            }
        except ValueError as exc:
            raise InputValidationError(str(exc)) from exc
        except OSError as exc:
            raise InputValidationError(
                "caller upload destinations could not be inspected safely"
            ) from exc
        warnings: list[str] = []
        queue_root = paths.queue_dir()
        queue_guard = _open_service_directory(queue_root, create=True)
        try:
            temporary, private_guard = _make_private_queue_directory(queue_guard)
            try:
                try:
                    if prepared_uploads:
                        upload_directory = temporary / UPLOAD_PRIVATE_DIRECTORY
                        os.mkdir(
                            upload_directory.name,
                            mode=0o700,
                            dir_fd=private_guard.descriptor,
                        )
                        upload_descriptor = os.open(
                            upload_directory.name,
                            _directory_open_flags(),
                            dir_fd=private_guard.descriptor,
                        )
                        upload_guard = _DirectoryGuard(
                            path=upload_directory,
                            descriptor=upload_descriptor,
                        )
                        try:
                            os.fsync(private_guard.descriptor)
                            _verify_atomic_upload_filesystem(
                                input_guard,
                                upload_guard,
                            )
                            private_uploads: list[tuple[Upload, Path]] = []
                            for upload in prepared_uploads:
                                private_uploads.append(
                                    (
                                        upload,
                                        _stream_upload(
                                            upload_directory,
                                            upload,
                                            upload_guard,
                                        ),
                                    )
                                )
                            for upload, private_file in private_uploads:
                                replaced = _publish_upload(
                                    private_file,
                                    input_directory / upload.basename,
                                    input_guard,
                                    upload_guard,
                                    replace_existing=replacement_by_name[
                                        upload.basename
                                    ],
                                )
                                if replaced:
                                    warnings.append(
                                        _replacement_warning(upload.basename)
                                    )
                        finally:
                            upload_guard.close()
                        upload_directory_removed = False
                        try:
                            os.rmdir(
                                upload_directory.name,
                                dir_fd=private_guard.descriptor,
                            )
                            upload_directory_removed = True
                            os.fsync(private_guard.descriptor)
                        except OSError as cleanup_error:
                            raise InputPublicationError(
                                f"caller uploads were published, but removing or "
                                f"syncing private upload directory {upload_directory} "
                                f"failed: {cleanup_error}",
                                retain_private_tree=not upload_directory_removed,
                            ) from cleanup_error

                    input_mode = "directory"
                    if prepared_uploads and existing_names:
                        input_mode = "mixed"
                    elif prepared_uploads:
                        input_mode = "upload"

                    try:
                        manifest_files = _snapshot_inputs(
                            input_directory,
                            temporary / "request",
                            input_guard,
                            private_guard,
                        )
                    except ValueError as exc:
                        raise InputValidationError(str(exc)) from exc
                    except OSError as exc:
                        raise InputSnapshotError(
                            "caller input files could not be snapshotted completely"
                        ) from exc
                    fcntl.flock(input_guard.descriptor, fcntl.LOCK_UN)
                    input_locked = False
                    record = _publish_queue_entry(
                        temporary,
                        event_id=event_id,
                        configuration=configuration,
                        overwrite=overwrite,
                        warnings=warnings,
                        input_mode=input_mode,
                        manifest_files=manifest_files,
                        queue_guard=queue_guard,
                        private_guard=private_guard,
                    )
                except BaseException as acceptance_error:
                    retain_private_tree = bool(
                        isinstance(acceptance_error, InputPublicationError)
                        and acceptance_error._retain_private_tree
                    )
                    if not retain_private_tree:
                        try:
                            _cleanup_private_queue_directory(
                                temporary,
                                queue_guard,
                                acceptance_error,
                            )
                        except PrivateRequestCleanupError as cleanup_error:
                            if not isinstance(
                                acceptance_error,
                                InputPublicationError,
                            ):
                                raise
                            original_cause = (
                                acceptance_error.__cause__ or acceptance_error
                            )
                            raise InputPublicationError(
                                f"{acceptance_error}; secondary unpublished request "
                                f"cleanup failure: {cleanup_error}",
                                retain_private_tree=True,
                            ) from original_cause
                    raise
            finally:
                private_guard.close()
        finally:
            queue_guard.close()
    finally:
        if input_locked:
            fcntl.flock(input_guard.descriptor, fcntl.LOCK_UN)
        input_guard.close()

    return SubmissionResult(
        event_id=event_id,
        internal_sequence=record.internal_sequence,
        status=record.status,
        warnings=tuple(warnings),
        requested_configuration=configuration,
        overwrite=overwrite,
        status_path=(
            f".service/queue/{paths.queue_entry_name(record.internal_sequence)}/status.json"
        ),
        shared_input_path=str(paths.shared_event_input_dir(event_id)),
        shared_products_path=str(paths.shared_event_native_products_dir(event_id)),
    )
