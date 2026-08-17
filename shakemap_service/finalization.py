"""Exclusive deployment finalization and one-boot readiness support."""
from __future__ import annotations

import contextlib
import argparse
import fcntl
import json
import os
import shutil
import stat
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import paths, preparation, readiness, status
from .directory_access import open_service_directory

MARKER_SCHEMA_VERSION = 1
MAX_MARKER_BYTES = 4096


class FinalizationError(RuntimeError):
    """Finalization cannot proceed without risking durable work or state."""


def prepare_runtime(regional_seeds: Path | None = None) -> list[str]:
    """Create runtime paths and copy only missing regional seed directories."""
    required = [
        *paths.all_service_dirs(),
        paths.vs30_dir(),
        paths.topo_dir(),
        paths.regional_data_dir(),
        paths.test_data_dir(),
    ]
    for directory in required:
        handle = open_service_directory(directory, create=True)
        handle.close()
    destination = paths.regional_data_dir()
    seeded: list[str] = []
    if regional_seeds is None:
        return seeded
    for source in sorted(regional_seeds.iterdir(), key=lambda item: item.name):
        if source.name.startswith(".") or not source.is_dir() or source.is_symlink():
            continue
        target = destination / source.name
        if target.exists() or target.is_symlink():
            continue
        temporary = destination / f".regional-seed-{uuid.uuid4().hex}"
        try:
            shutil.copytree(source, temporary, symlinks=False)
            if target.exists() or target.is_symlink():
                continue
            os.rename(temporary, target)
            descriptor = os.open(destination, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            seeded.append(source.name)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return seeded


@contextmanager
def coordination_lock() -> Iterator[None]:
    """Serialize finalization with request publication and queue promotion."""
    service = open_service_directory(paths.service_dir(), create=True)
    descriptor = -1
    try:
        name = paths.workflow_lock_file().name
        try:
            descriptor = os.open(
                name,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=service.descriptor,
            )
            os.fsync(service.descriptor)
        except FileExistsError:
            descriptor = os.open(
                name,
                os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=service.descriptor,
            )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise FinalizationError("workflow coordination entry is not a regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        if descriptor >= 0:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        service.close()


def _unfinished_work() -> list[status.CalculationRecord]:
    queue_records, queue_problems = status.scan_queue_records()
    current_records, current_problems = status.scan_current_records()
    problems = queue_problems + current_problems
    if problems:
        entries = ", ".join(name for name, _message in problems)
        raise FinalizationError(f"durable calculation state is malformed: {entries}")
    return [
        record
        for record in queue_records + current_records
        if record.status in {
            status.LifecycleState.QUEUED.value,
            status.LifecycleState.RUNNING.value,
        }
    ]


def begin() -> None:
    """Close ordinary admission only after proving durable work is idle."""
    with coordination_lock():
        unfinished = _unfinished_work()
        if unfinished:
            details = ", ".join(
                f"{record.event_id}#{record.internal_sequence}:{record.status}"
                for record in unfinished
            )
            raise FinalizationError(
                f"accepted calculations are unfinished ({details}); wait for them to finish"
            )
        readiness._record_finalizing()


def record_failure(reason: str) -> None:
    with coordination_lock():
        readiness._set_provisional_ready(False)
        remove_bootstrap_marker()
        readiness._record_not_ready(reason)


def record_ready(service_identity: object = None) -> None:
    with coordination_lock():
        record = readiness._read_record()
        if record is None or record["state"] != "finalizing":
            raise FinalizationError("deployment is not in finalizing state")
        if service_identity is None:
            from .build_identity import service_identity as load_identity

            service_identity = load_identity()
        readiness._record_ready(service_identity)
        readiness._set_provisional_ready(False)


def _marker_payload(image_id: str) -> bytes:
    normalized = readiness._hex(image_id, 64, "sha256:")
    return (
        json.dumps(
            {
                "schema_version": MARKER_SCHEMA_VERSION,
                "image_id": normalized,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def arm_bootstrap_marker(image_id: str) -> None:
    payload = _marker_payload(image_id)
    with coordination_lock():
        record = readiness._read_record()
        if record is None or record["state"] != "finalizing":
            raise FinalizationError("bootstrap requires finalizing readiness")
        service = open_service_directory(paths.service_dir(), create=False)
        temporary = f".{paths.bootstrap_marker_file().name}.{uuid.uuid4().hex}.tmp"
        descriptor = -1
        try:
            try:
                existing = os.stat(
                    paths.bootstrap_marker_file().name,
                    dir_fd=service.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISREG(existing.st_mode):
                    raise FinalizationError("existing bootstrap marker is unsafe")
                raise FinalizationError("an unconsumed bootstrap marker already exists")
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=service.descriptor,
            )
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(
                temporary,
                paths.bootstrap_marker_file().name,
                src_dir_fd=service.descriptor,
                dst_dir_fd=service.descriptor,
            )
            os.fsync(service.descriptor)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=service.descriptor)
            service.close()


def remove_bootstrap_marker() -> None:
    try:
        service = open_service_directory(paths.service_dir(), create=False)
    except FileNotFoundError:
        return
    try:
        try:
            details = os.stat(
                paths.bootstrap_marker_file().name,
                dir_fd=service.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        if not stat.S_ISREG(details.st_mode):
            raise FinalizationError("bootstrap marker is unsafe")
        os.unlink(paths.bootstrap_marker_file().name, dir_fd=service.descriptor)
        os.fsync(service.descriptor)
    finally:
        service.close()


def _consume_marker_bytes() -> bytes | None:
    try:
        service = open_service_directory(paths.service_dir(), create=False)
    except FileNotFoundError:
        return None
    descriptor = -1
    try:
        try:
            descriptor = os.open(
                paths.bootstrap_marker_file().name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=service.descriptor,
            )
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise FinalizationError("bootstrap marker is not a regular file")
        os.unlink(paths.bootstrap_marker_file().name, dir_fd=service.descriptor)
        os.fsync(service.descriptor)
        payload = os.read(descriptor, MAX_MARKER_BYTES + 1)
        return payload
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        service.close()


def consume_bootstrap_marker(service_identity: object = None) -> bool:
    """Consume a marker before granting readiness to this process only."""
    readiness._set_provisional_ready(False)
    try:
        with coordination_lock():
            payload = _consume_marker_bytes()
            if payload is None:
                return False
            if len(payload) > MAX_MARKER_BYTES:
                return False
            try:
                marker = json.loads(payload.decode("utf-8"))
                if not isinstance(marker, dict) or set(marker) != {
                    "schema_version",
                    "image_id",
                }:
                    raise ValueError("invalid marker fields")
                if marker["schema_version"] != MARKER_SCHEMA_VERSION:
                    raise ValueError("unsupported marker schema")
                image_id = readiness._hex(marker["image_id"], 64, "sha256:")
                record = readiness._read_record()
                if record is None or record["state"] != "finalizing":
                    raise ValueError("deployment is not finalizing")
                if service_identity is None:
                    from .build_identity import service_identity as load_identity

                    service_identity = load_identity()
                current = readiness._current_identity(service_identity)
                if current["image_id"] != image_id:
                    raise ValueError("marker image does not match this deployment")
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                return False
            readiness._set_provisional_ready(True)
            return True
    except (FileNotFoundError, ValueError):
        return False


def recorded_identity_matches(
    *,
    image_id: str,
    release_tag: str,
    source_commit: str,
    shakemap_version: str,
) -> bool:
    try:
        record = readiness._read_record()
        if record is None or record["state"] != "ready":
            return False
        identity = record["identity"]
        expected_assets = {
            name: {
                key: preparation.GLOBAL_ASSETS[name][key]
                for key in ("relative", "size", "sha256")
            }
            for name in ("vs30", "topography")
        }
        return (
            identity["image_id"] == readiness._hex(image_id, 64, "sha256:")
            and identity["release_tag"] == release_tag
            and identity["source_commit"] == readiness._hex(source_commit, 40)
            and identity["shakemap_version"] == shakemap_version
            and identity["global_assets"] == expected_assets
        )
    except (OSError, UnicodeError, ValueError):
        return False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("begin")
    arm = commands.add_parser("arm")
    arm.add_argument("--image-id", required=True)
    fail = commands.add_parser("fail")
    fail.add_argument("--reason", required=True)
    commands.add_parser("ready")
    activate = commands.add_parser("activate-data")
    activate.add_argument("--data-root", type=Path, required=True)
    prepare = commands.add_parser("prepare-runtime")
    prepare.add_argument("--regional-seeds", type=Path)
    check = commands.add_parser("check-ready")
    check.add_argument("--image-id", required=True)
    check.add_argument("--release-tag", required=True)
    check.add_argument("--source-commit", required=True)
    check.add_argument("--shakemap-version", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "begin":
            begin()
        elif args.command == "arm":
            arm_bootstrap_marker(args.image_id)
        elif args.command == "fail":
            record_failure(args.reason)
        elif args.command == "ready":
            record_ready()
        elif args.command == "activate-data":
            result = preparation.activate_staged_global_replacements(args.data_root)
            print(json.dumps(result, indent=2, sort_keys=True))
        elif args.command == "prepare-runtime":
            print(json.dumps({"seeded": prepare_runtime(args.regional_seeds)}))
        elif args.command == "check-ready":
            return 0 if recorded_identity_matches(
                image_id=args.image_id,
                release_tag=args.release_tag,
                source_commit=args.source_commit,
                shakemap_version=args.shakemap_version,
            ) else 1
        return 0
    except (FinalizationError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
