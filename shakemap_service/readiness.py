"""Durable readiness state and lightweight deployment-identity comparison."""
from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import uuid

from . import paths
from .directory_access import open_service_directory

RECORD_NAME = "readiness.json"
MAX_RECORD_BYTES = 16 * 1024
NOT_RECORDED = "deployment readiness has not been recorded"
UNAVAILABLE = "recorded readiness is unavailable"
MISMATCH = "recorded readiness does not match this deployment"
_IDENTITY_KEYS = {
    "image_id",
    "release_tag",
    "source_commit",
    "shakemap_version",
    "global_assets",
}
_FINAL_RELEASE_TAG_RE = re.compile(
    r"^v?(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)$"
)


def _mapping(value: object, keys: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("invalid readiness fields")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("readiness text must be nonempty")
    return value


def _hex(value: object, size: int, prefix: str = "") -> str:
    text = _text(value)
    body = text[len(prefix) :] if text.startswith(prefix) else ""
    if len(body) != size or any(
        character not in "0123456789abcdef" for character in body
    ):
        raise ValueError("readiness hexadecimal identity is invalid")
    return text


def _relative(value: object) -> str:
    text = _text(value)
    components = text.split("/")
    if (
        text.startswith("/")
        or "\\" in text
        or any(part in {"", ".", ".."} for part in components)
    ):
        raise ValueError("readiness asset path is unsafe")
    return text


def _normalize_identity(value: object) -> dict[str, object]:
    identity = _mapping(value, _IDENTITY_KEYS)
    assets = _mapping(identity["global_assets"], {"vs30", "topography"})
    normalized: dict[str, object] = {}
    for name in ("vs30", "topography"):
        asset = _mapping(assets[name], {"relative", "size", "sha256"})
        if type(asset["size"]) is not int or asset["size"] < 1:
            raise ValueError("asset size must be a positive integer")
        normalized[name] = {
            "relative": _relative(asset["relative"]),
            "size": asset["size"],
            "sha256": _hex(asset["sha256"], 64),
        }
    # Normalize only deployment-binding fields so comparisons are structural and exact.
    release_tag = _text(identity["release_tag"])
    if _FINAL_RELEASE_TAG_RE.fullmatch(release_tag) is None:
        raise ValueError("readiness release tag is not a final stable release")
    result = {
        "release_tag": release_tag,
        "shakemap_version": _text(identity["shakemap_version"]),
    }
    result["image_id"] = _hex(identity["image_id"], 64, "sha256:")
    result["source_commit"] = _hex(identity["source_commit"], 40)
    result["global_assets"] = normalized
    return result


def _current_identity(service_identity: object) -> dict[str, object]:
    root = service_identity if isinstance(service_identity, dict) else {}
    image, deployment = root.get("immutable_image"), root.get("deployment")
    if (
        not isinstance(image, dict)
        or image.get("available") is not True
        or not isinstance(deployment, dict)
        or deployment.get("available") is not True
    ):
        raise ValueError("image identity is unavailable")
    upstream, installed = image.get("upstream"), image.get("installed")
    if not isinstance(upstream, dict) or not isinstance(installed, dict):
        raise ValueError("release identity is unavailable")
    # Mounted bytes are validated before readiness is recorded, never during reads.
    from .preparation import GLOBAL_ASSETS

    assets = {
        name: {
            key: GLOBAL_ASSETS[name][key]
            for key in ("relative", "size", "sha256")
        }
        for name in ("vs30", "topography")
    }
    return _normalize_identity(
        {
            "image_id": deployment.get("image_id"),
            "release_tag": upstream.get("release_tag"),
            "source_commit": upstream.get("source_commit"),
            "shakemap_version": installed.get("shakemap_distribution_version"),
            "global_assets": assets,
        }
    )


def _validate_record(value: object) -> dict:
    record = _mapping(value, {"schema_version", "state", "reason", "identity"})
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        raise ValueError("unsupported readiness schema")
    if record["state"] == "not_ready":
        if record["identity"] is not None:
            raise ValueError("not-ready identity must be null")
        _text(record["reason"])
    elif record["state"] == "ready":
        if record["reason"] is not None:
            raise ValueError("ready reason must be null")
        record["identity"] = _normalize_identity(record["identity"])
    else:
        raise ValueError("invalid readiness state")
    return record


def _read_record() -> dict | None:
    try:
        service = open_service_directory(paths.service_dir(), create=False)
    except FileNotFoundError:
        return None
    descriptor = -1
    try:
        try:
            # Nonblocking open lets special-file entries be rejected without waiting.
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(RECORD_NAME, flags, dir_fd=service.descriptor)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("readiness record is not a bounded regular file")
        stream = os.fdopen(descriptor, "rb")
        descriptor = -1
        with stream:
            payload = stream.read(MAX_RECORD_BYTES + 1)
        if len(payload) > MAX_RECORD_BYTES:
            raise ValueError("readiness record is oversized")
        return _validate_record(json.loads(payload.decode("utf-8")))
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        service.close()


def _require_replaceable(parent: int) -> None:
    try:
        details = os.stat(RECORD_NAME, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(details.st_mode):
        raise ValueError("existing readiness entry is unsafe")


def _publish(record: dict) -> None:
    payload = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode()
    if len(payload) > MAX_RECORD_BYTES:
        raise ValueError("readiness record is oversized")
    temporary = f".{RECORD_NAME}.{uuid.uuid4().hex}.tmp"
    descriptor = -1
    service = open_service_directory(paths.service_dir(), create=True)
    try:
        _require_replaceable(service.descriptor)
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=service.descriptor,
        )
        stream = os.fdopen(descriptor, "wb")
        descriptor = -1
        # The complete temporary record is durable before its name is published.
        with stream:
            if stream.write(payload) != len(payload):
                raise OSError("incomplete readiness write")
            stream.flush()
            os.fsync(stream.fileno())
        _require_replaceable(service.descriptor)
        os.replace(
            temporary,
            RECORD_NAME,
            src_dir_fd=service.descriptor,
            dst_dir_fd=service.descriptor,
        )
        # Persist the replacement directory entry before reporting success.
        os.fsync(service.descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=service.descriptor)
        service.close()


def _record_ready(service_identity: object) -> None:
    _publish(
        {
            "schema_version": 1,
            "state": "ready",
            "reason": None,
            "identity": _current_identity(service_identity),
        }
    )


def _record_not_ready(reason: str) -> None:
    _publish(
        {
            "schema_version": 1,
            "state": "not_ready",
            "reason": _text(reason),
            "identity": None,
        }
    )


def read_readiness(service_identity: object = None) -> dict[str, object]:
    try:
        record = _read_record()
    except (OSError, UnicodeError, ValueError):
        return {"ready": False, "reason": UNAVAILABLE}
    if record is None:
        return {"ready": False, "reason": NOT_RECORDED}
    if record["state"] == "not_ready":
        return {"ready": False, "reason": record["reason"]}
    try:
        if service_identity is None:
            # Load current identity only after the durable record claims readiness.
            from .build_identity import service_identity as load_identity

            service_identity = load_identity()
        current = _current_identity(service_identity)
    except Exception:
        return {"ready": False, "reason": MISMATCH}
    if record["identity"] != current:
        return {"ready": False, "reason": MISMATCH}
    return {"ready": True, "reason": None}
