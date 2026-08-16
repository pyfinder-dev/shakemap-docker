"""Publish the native product inventory for one current calculation."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from pathlib import Path

from . import paths, status
from .directory_access import open_service_directory
from .product_validation import ProductValidationResult


MANIFEST_NAME = "product-manifest.json"
PRIVATE_FILE_MODE = 0o600
HASH_CHUNK_SIZE = 64 * 1024


class ProductManifestError(RuntimeError):
    """A product inventory or manifest publication could not be completed."""


def _error_text(exc: BaseException) -> str:
    detail = str(exc)
    if detail:
        return f"{type(exc).__name__}: {detail}"
    return type(exc).__name__


def _require_matching_current(record: status.CalculationRecord) -> None:
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


def _relative_failure_path(products_directory: Path, exc: OSError) -> str:
    filename = exc.filename
    if filename is None:
        return "."
    try:
        return Path(filename).relative_to(products_directory).as_posix()
    except ValueError:
        return "."


def _inventory_products(
    products_directory: Path,
    *,
    best_effort: bool,
) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    inventory: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []

    try:
        root_details = products_directory.lstat()
    except OSError as exc:
        if not best_effort:
            raise ProductManifestError(
                f"native products directory could not be inspected: "
                f"{_error_text(exc)}"
            ) from exc
        failures.append({"path": ".", "reason": _error_text(exc)})
        return inventory, failures
    if not stat.S_ISDIR(root_details.st_mode):
        error = NotADirectoryError(
            f"native products path is not a directory: {products_directory}"
        )
        if not best_effort:
            raise ProductManifestError(str(error)) from error
        failures.append({"path": ".", "reason": _error_text(error)})
        return inventory, failures

    def walk_error(exc: OSError) -> None:
        if not best_effort:
            raise ProductManifestError(
                f"native product directory could not be read: {_error_text(exc)}"
            ) from exc
        failures.append(
            {
                "path": _relative_failure_path(products_directory, exc),
                "reason": _error_text(exc),
            }
        )

    for directory, directory_names, file_names in os.walk(
        products_directory,
        topdown=True,
        onerror=walk_error,
        followlinks=False,
    ):
        directory_names.sort()
        file_names.sort()
        directory_path = Path(directory)
        for filename in file_names:
            product = directory_path / filename
            relative = product.relative_to(products_directory).as_posix()
            try:
                details = product.lstat()
            except OSError as exc:
                if not best_effort:
                    raise ProductManifestError(
                        f"native product {relative!r} could not be inspected: "
                        f"{_error_text(exc)}"
                    ) from exc
                failures.append(
                    {"path": relative, "reason": _error_text(exc)}
                )
                continue
            if not stat.S_ISREG(details.st_mode):
                continue

            digest = hashlib.sha256()
            try:
                with product.open("rb") as stream:
                    while True:
                        chunk = stream.read(HASH_CHUNK_SIZE)
                        if not chunk:
                            break
                        digest.update(chunk)
            except OSError as exc:
                if not best_effort:
                    raise ProductManifestError(
                        f"native product {relative!r} could not be read: "
                        f"{_error_text(exc)}"
                    ) from exc
                failures.append(
                    {"path": relative, "reason": _error_text(exc)}
                )
                continue
            inventory.append(
                {
                    "path": relative,
                    "size": details.st_size,
                    "sha256": digest.hexdigest(),
                }
            )

    inventory.sort(key=lambda entry: str(entry["path"]))
    failures.sort(key=lambda entry: (entry["path"], entry["reason"]))
    return inventory, failures


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written < 1:
            raise OSError("zero-byte write while publishing product manifest")
        remaining = remaining[written:]


def _publish_manifest(record: status.CalculationRecord, payload: bytes) -> Path:
    manifest_file = paths.event_manifest_file(record.event_id)
    service = open_service_directory(
        paths.event_service_dir(record.event_id),
        create=False,
    )
    temporary_name = f".product-manifest-{uuid.uuid4().hex}.tmp"
    descriptor = -1
    published = False
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            PRIVATE_FILE_MODE,
            dir_fd=service.descriptor,
        )
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary_name,
            MANIFEST_NAME,
            src_dir_fd=service.descriptor,
            dst_dir_fd=service.descriptor,
        )
        published = True
        os.fsync(service.descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not published:
            try:
                os.unlink(temporary_name, dir_fd=service.descriptor)
            except OSError:
                pass
        service.close()
    return manifest_file


def publish_product_manifest(
    record: status.CalculationRecord,
    validation: ProductValidationResult | None,
    *,
    primary_reason: str | None = None,
) -> Path:
    """Inventory products and durably publish a current calculation manifest."""
    if primary_reason is not None and (
        not isinstance(primary_reason, str) or not primary_reason
    ):
        raise ValueError("primary_reason must be a nonempty string or None")
    if primary_reason is None and (
        validation is None or not validation.passed
    ):
        raise ValueError(
            "a complete manifest requires an all-pass required-product result"
        )
    _require_matching_current(record)

    inventory, inventory_failures = _inventory_products(
        paths.event_native_products_dir(record.event_id),
        best_effort=primary_reason is not None,
    )
    payload = {
        "event_id": record.event_id,
        "internal_sequence": record.internal_sequence,
        "partial": primary_reason is not None,
        "primary_reason": primary_reason,
        "inventory_failures": inventory_failures,
        "required_products": None
        if validation is None
        else {
            "paths": list(validation.required_paths),
            "source": validation.source,
            "passed": validation.passed,
            "checks": [
                {
                    "path": check.path,
                    "size": check.size,
                    "passed": check.passed,
                    "reason": check.reason,
                }
                for check in validation.checks
            ],
        },
        "products": inventory,
    }
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    return _publish_manifest(record, encoded)
