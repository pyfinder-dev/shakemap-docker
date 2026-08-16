"""Publish observed provenance for one current calculation."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Mapping

from . import build_identity, paths, status
from .config import settings
from .directory_access import open_service_directory
from .native_profile import BASE_CONFIGURATION_FILES
from .required_products import RequiredProductResolution


PROVENANCE_NAME = "provenance.json"
PRIVATE_FILE_MODE = 0o600
HASH_CHUNK_SIZE = 64 * 1024
_PROFILE_FILES = (
    "home/.shakemap/profiles.conf",
    *(f"install/config/{name}" for name in BASE_CONFIGURATION_FILES),
    "home/.strec/config.ini",
)


class ProvenanceError(RuntimeError):
    """Calculation provenance could not be assembled or published."""


@dataclass(frozen=True)
class ProvenanceFacts:
    """Observed calculation facts supplied by execution finalization."""

    configuration_materialization: Mapping[str, object]
    native_execution: Mapping[str, object] | None
    required_products: RequiredProductResolution | None
    native_outcome: Mapping[str, object] | None
    service_outcome: Mapping[str, object] | None
    warnings: tuple[str, ...]
    failure: Mapping[str, object] | None
    timestamps: Mapping[str, str | None]


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


def _read_request_manifest(
    record: status.CalculationRecord,
) -> list[dict[str, object]]:
    manifest_file = paths.event_service_dir(record.event_id) / "request-manifest.json"
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProvenanceError(
            f"retained request manifest could not be read: {exc}"
        ) from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("event_id") != record.event_id
        or manifest.get("internal_sequence") != record.internal_sequence
        or not isinstance(manifest.get("files"), list)
    ):
        raise ProvenanceError("retained request manifest identity is invalid")
    files: list[dict[str, object]] = []
    for entry in manifest["files"]:
        if not isinstance(entry, dict) or set(entry) != {
            "basename",
            "size_bytes",
            "sha256",
        }:
            raise ProvenanceError("retained request file identity is invalid")
        files.append(dict(entry))
    return files


def _hash_regular_file(path: Path) -> dict[str, object] | None:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ProvenanceError(
            f"retained profile file is inaccessible: {path}: {exc}"
        ) from exc
    if not stat.S_ISREG(details.st_mode):
        raise ProvenanceError(f"retained profile path is not a regular file: {path}")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(HASH_CHUNK_SIZE), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ProvenanceError(
            f"retained profile file is unreadable: {path}: {exc}"
        ) from exc
    return {
        "size": details.st_size,
        "sha256": digest.hexdigest(),
    }


def _profile_file_identities(event_id: str) -> list[dict[str, object]]:
    profile_directory = paths.event_profile_dir(event_id)
    identities: list[dict[str, object]] = []
    for relative in _PROFILE_FILES:
        identities.append(
            {
                "path": relative,
                "identity": _hash_regular_file(profile_directory / relative),
            }
        )
    return identities


def _required_product_facts(
    resolution: RequiredProductResolution | None,
) -> dict[str, object] | None:
    if resolution is None:
        return None
    return {
        "paths": list(resolution.paths),
        "source": resolution.source,
    }


def _large_dataset_declarations(
    selected_configuration: str,
) -> dict[str, object]:
    resource = files("shakemap_service").joinpath("data/global-assets.json")
    manifest = json.loads(resource.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise RuntimeError("unsupported global scientific-data manifest schema")
    assets = manifest.get("assets")
    if not isinstance(assets, dict) or set(assets) != {"vs30", "topography"}:
        raise RuntimeError(
            "global scientific-data manifest must define Vs30 and topography"
        )
    shared_data_directory = paths.shared_service_root() / "data"
    global_assets: dict[str, object] = {}
    for name in ("vs30", "topography"):
        declaration = assets[name]
        global_assets[name] = {
            "path": str(shared_data_directory / str(declaration["relative"])),
            "manifest_identity": {
                "size": declaration["size"],
                "sha256": declaration["sha256"],
                "source_url": declaration["url"],
                "checksum_authority": declaration["checksum_authority"],
            },
            "calculation_validation": "not_observed",
        }

    regional_path = None
    regional_state = "not_applicable"
    if selected_configuration != "global":
        regional_path = str(
            shared_data_directory / "regional" / selected_configuration
        )
        regional_state = "manifest_identity_unavailable"
    return {
        "global": global_assets,
        "regional": {
            "path": regional_path,
            "manifest_identity": None,
            "identity_state": regional_state,
        },
    }


def _shared_locations(event_id: str) -> dict[str, str]:
    service_directory = (
        paths.shared_service_root() / ".service" / "events" / event_id
    )
    return {
        "input": str(paths.shared_event_input_dir(event_id)),
        "native_products": str(paths.shared_event_native_products_dir(event_id)),
        "status": str(service_directory / "status.json"),
        "profile": str(service_directory / "profile"),
        "provenance": str(service_directory / PROVENANCE_NAME),
        "product_manifest": str(service_directory / "product-manifest.json"),
        "service_log": str(service_directory / "logs" / "service.log"),
        "shake_log": str(service_directory / "logs" / "shake.log"),
    }


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written < 1:
            raise OSError("zero-byte write while publishing provenance")
        remaining = remaining[written:]


def _publish(record: status.CalculationRecord, payload: bytes) -> Path:
    provenance_file = paths.event_provenance_file(record.event_id)
    service = open_service_directory(
        paths.event_service_dir(record.event_id),
        create=False,
    )
    temporary_name = f".provenance-{uuid.uuid4().hex}.tmp"
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
            PROVENANCE_NAME,
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
    return provenance_file


def publish_provenance(
    record: status.CalculationRecord,
    facts: ProvenanceFacts,
) -> Path:
    """Assemble and durably publish observed calculation provenance."""
    if not isinstance(facts, ProvenanceFacts):
        raise TypeError("facts must be ProvenanceFacts")
    current = _require_matching_current(record)
    selected_configuration = current.configuration["selected"]
    payload = {
        "event_id": current.event_id,
        "internal_sequence": current.internal_sequence,
        "request": {
            "configuration": selected_configuration,
            "overwrite": current.overwrite,
            "input_mode": current.request["input_mode"],
            "files": _read_request_manifest(current),
        },
        "configuration": {
            "selected": selected_configuration,
            "materialization": dict(facts.configuration_materialization),
            "profile_files": _profile_file_identities(current.event_id),
        },
        "software_identity": build_identity.service_identity(),
        "module_plan": list(settings.module_plan),
        "native_execution": None
        if facts.native_execution is None
        else dict(facts.native_execution),
        "outcomes": {
            "native": None
            if facts.native_outcome is None
            else dict(facts.native_outcome),
            "service": None
            if facts.service_outcome is None
            else dict(facts.service_outcome),
        },
        "warnings": list(facts.warnings),
        "failure": None if facts.failure is None else dict(facts.failure),
        "timestamps": dict(facts.timestamps),
        "required_products": _required_product_facts(facts.required_products),
        "locations": _shared_locations(current.event_id),
        "large_datasets": _large_dataset_declarations(selected_configuration),
    }
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    return _publish(current, encoded)
