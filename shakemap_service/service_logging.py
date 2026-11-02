"""Persistent service diagnostics, separate from authoritative calculation logs."""
from __future__ import annotations

import argparse
import fcntl
import logging
import os
import stat
import sys
from datetime import datetime, timezone

from . import paths
from .directory_access import open_service_directory


def append_record(name: str, message: str) -> None:
    """Append under the existing private runtime access policy without following links."""
    if name not in {"service", "operations"}:
        raise ValueError("unknown service diagnostic log")

    target = paths.logs_dir() / f"{name}.log"
    directory = None
    descriptor = -1
    try:
        directory = open_service_directory(paths.logs_dir(), create=True)
        # Nonblocking open prevents an unsafe FIFO from hanging startup. The
        # descriptor check also rejects hard links before any bytes are written.
        flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            descriptor = os.open(
                target.name,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory.descriptor,
            )
        except FileExistsError:
            descriptor = os.open(
                target.name,
                flags,
                dir_fd=directory.descriptor,
            )
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise ValueError(
                f"diagnostic log must be a regular file with one link: {target}"
            )

        # Host helpers and API threads can append concurrently. Keep a whole
        # record together, including traceback lines and any partial writes.
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        remaining = memoryview((message.rstrip("\n") + "\n").encode("utf-8"))
        while remaining:
            written = os.write(descriptor, remaining)
            if written < 1:
                raise OSError(f"zero-byte write to {target}")
            remaining = remaining[written:]

        os.fsync(descriptor)
        os.fsync(directory.descriptor)
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"cannot append diagnostic log {target}: {exc}. "
            "Check storage and service-writable runtime access for UID:GID "
            "1000:1000; follow the documented stopped-service permission recovery."
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory is not None:
            directory.close()


def record_operation(message: str, *, level: str = "INFO") -> None:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    append_record("operations", f"{timestamp} {level} {message}")


class ServiceLogHandler(logging.Handler):
    """Mirror Python records while keeping console diagnostics on write failure."""

    def __init__(self) -> None:
        super().__init__()
        # Refuse startup before serving requests if the persistent destination
        # cannot be opened; later storage failures remain visible on stderr.
        append_record("service", "--- configuring service runtime logging ---")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            append_record("service", self.format(record))
        except (OSError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    # Internal shell integration, not an additional operator command or knob.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", choices=("service", "operations"))
    parser.add_argument("level", choices=("INFO", "WARNING", "ERROR"))
    parser.add_argument("message")
    args = parser.parse_args(argv)
    timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    line = f"{timestamp} {args.level} {args.message}"
    print(line, flush=True)
    try:
        append_record(args.name, line)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
