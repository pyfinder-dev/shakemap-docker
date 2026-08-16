# -*- coding: utf-8 -*-
"""Host-side JSON REST client for the ShakeMap service."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import stat
import sys
import unicodedata
import uuid
from typing import BinaryIO, Iterable, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


DEFAULT_SERVICE_URL = "http://localhost:9010"
MULTIPART_CHUNK_SIZE = 64 * 1024


def parser() -> argparse.ArgumentParser:
    command_parser = argparse.ArgumentParser(
        prog="shake-in-docker",
        description="Host REST client for shakemap-docker.",
    )
    command_parser.add_argument(
        "--url",
        default=DEFAULT_SERVICE_URL,
        help=f"service base URL (default: {DEFAULT_SERVICE_URL})",
    )
    subcommands = command_parser.add_subparsers(dest="command")
    subcommands.add_parser("health", help="show service readiness")
    subcommands.add_parser("config", help="show operational service configuration")
    subcommands.add_parser("configurations", help="list configuration names")
    submit_parser = subcommands.add_parser(
        "submit",
        help="submit caller-prepared native files for a calculation",
        description=(
            "Submit an event using files uploaded to or already present in the "
            "canonical event input directory. A readable event.xml is required."
        ),
        epilog=(
            "The service runs the fixed sequence: select assemble model contour "
            "mapping stations gridxml. Replacing an uploaded basename is reported "
            "as a warning in the JSON response."
        ),
    )
    submit_parser.add_argument("event_id")
    submit_parser.add_argument(
        "--configuration",
        default="global",
        help="caller-selected configuration name (default: global)",
    )
    submit_parser.add_argument(
        "--overwrite",
        choices=("true", "false"),
        default="true",
        help="discard or retain a preceding calculation (default: true)",
    )
    submit_parser.add_argument(
        "--file",
        action="append",
        default=[],
        metavar="PATH",
        help="native input file to upload; repeat for multiple files",
    )
    subcommands.add_parser("list", help="list current and queued calculations")
    subcommands.add_parser("queue", help="show queued calculations and capacity")
    status_parser = subcommands.add_parser(
        "status",
        help="show current, waiting, and retained records for an event",
    )
    status_parser.add_argument("event_id")
    products_parser = subcommands.add_parser(
        "products",
        help="show the current product-manifest summary for an event",
    )
    products_parser.add_argument("event_id")
    return command_parser


def _get_json(service_url: str, endpoint: str) -> object:
    request = Request(
        service_url.rstrip("/") + endpoint,
        headers={"Accept": "application/json"},
        method="GET",
    )
    with urlopen(request, timeout=30) as response:
        payload = response.read()
    return json.loads(payload.decode("utf-8"))


def _post_json(
    service_url: str,
    endpoint: str,
    body: Iterable[bytes],
    boundary: str,
) -> object:
    request = Request(
        service_url.rstrip("/") + endpoint,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        payload = response.read()
    return json.loads(payload.decode("utf-8"))


def _destination_basename(source_path: str) -> str:
    basename = os.path.basename(source_path)
    if not basename or basename in {".", ".."}:
        raise ValueError(f"upload path has no safe destination basename: {source_path!r}")
    if "/" in basename or any(
        unicodedata.category(character) in {"Cc", "Cs"} for character in basename
    ):
        raise ValueError(f"upload path has an unsafe destination basename: {source_path!r}")
    try:
        encoded = os.fsencode(basename)
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"upload basename cannot be represented unchanged: {basename!r}"
        ) from exc
    if len(encoded) > 255:
        raise ValueError(f"upload basename exceeds the native limit: {basename!r}")
    return basename


def _multipart_parameter(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _multipart_chunks(
    boundary: str,
    fields: Sequence[tuple[str, str]],
    files: Sequence[tuple[str, BinaryIO]],
) -> Iterable[bytes]:
    delimiter = f"--{boundary}\r\n".encode("ascii")
    for name, value in fields:
        yield delimiter
        yield (
            f'Content-Disposition: form-data; name="{_multipart_parameter(name)}"'
            "\r\n\r\n"
        ).encode("utf-8")
        yield value.encode("utf-8")
        yield b"\r\n"
    for basename, stream in files:
        yield delimiter
        yield (
            'Content-Disposition: form-data; name="files"; '
            f'filename="{_multipart_parameter(basename)}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        while True:
            chunk = stream.read(MULTIPART_CHUNK_SIZE)
            if chunk == b"":
                break
            if not isinstance(chunk, bytes):
                raise ValueError(f"upload {basename!r} did not produce byte content")
            yield chunk
        yield b"\r\n"
    yield f"--{boundary}--\r\n".encode("ascii")


def _open_submission_files(
    source_paths: Sequence[str],
    resources: contextlib.ExitStack,
) -> list[tuple[str, BinaryIO]]:
    basenames = [_destination_basename(source_path) for source_path in source_paths]
    seen: set[str] = set()
    duplicates: set[str] = set()
    for basename in basenames:
        if basename in seen:
            duplicates.add(basename)
        seen.add(basename)
    if duplicates:
        raise ValueError(
            "duplicate upload basenames are not allowed: "
            + ", ".join(sorted(duplicates))
        )

    opened: list[tuple[str, BinaryIO]] = []
    for source_path, basename in zip(source_paths, basenames):
        descriptor = os.open(
            source_path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError(
                    f"upload source is not a regular file: {source_path!r}"
                )
            stream = resources.enter_context(os.fdopen(descriptor, "rb"))
            descriptor = -1
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        opened.append((basename, stream))
    return opened


def _http_error_message(error: HTTPError) -> str:
    try:
        body = error.read().decode("utf-8")
    except Exception:
        body = ""
    suffix = f": {body}" if body else ""
    return f"HTTP {error.code} {error.reason}{suffix}"


def main(argv: Sequence[str] | None = None) -> int:
    command_parser = parser()
    arguments = command_parser.parse_args(argv)
    endpoints = {
        "health": "/healthz",
        "config": "/config",
        "configurations": "/configurations",
        "list": "/events",
        "queue": "/queue",
    }
    endpoint = endpoints.get(arguments.command)
    if arguments.command in {"status", "products"}:
        encoded_event_id = quote(arguments.event_id, safe="")
        endpoint = f"/events/{encoded_event_id}"
        if arguments.command == "products":
            endpoint += "/products"
    if endpoint is None and arguments.command != "submit":
        command_parser.print_usage(file=sys.stderr)
        return 2
    try:
        if arguments.command == "submit":
            boundary = f"shakemap-{uuid.uuid4().hex}"
            fields = (
                ("event_id", arguments.event_id),
                ("configuration", arguments.configuration),
                ("overwrite", arguments.overwrite),
            )
            with contextlib.ExitStack() as resources:
                files = _open_submission_files(arguments.file, resources)
                body = _multipart_chunks(boundary, fields, files)
                payload = _post_json(arguments.url, "/events", body, boundary)
        else:
            payload = _get_json(arguments.url, endpoint)
    except HTTPError as exc:
        print(f"ERROR: {_http_error_message(exc)}", file=sys.stderr)
        return 1
    except (OSError, URLError, UnicodeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
