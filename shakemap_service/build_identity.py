# -*- coding: utf-8 -*-
"""Validated loader for immutable image and runtime deployment identity."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import sys
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any, Sequence

from shakemap_service.release import OFFICIAL_REPOSITORY_URL

IDENTITY_PATH = Path("/opt/shakemap-build/identity.json")
_FULL_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_STABLE_TAG_RE = re.compile(r"^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REPOSITORY_DIGEST_RE = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*@sha256:[0-9a-f]{64}$"
)


class BuildIdentityError(ValueError):
    """Raised when the recorded image manifest is malformed."""


def _require_mapping(value: Any, field: str) -> dict:
    if not isinstance(value, dict):
        raise BuildIdentityError(f"{field} must be an object")
    return value


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BuildIdentityError(f"{field} must be a non-empty string")
    return value


def validate_build_identity(data: Any) -> dict:
    """Validate a manifest and return it unchanged as a plain mapping."""
    root = _require_mapping(data, "manifest")
    if root.get("schema_version") != 1:
        raise BuildIdentityError("Unsupported build identity schema_version")
    image = _require_mapping(root.get("immutable_image"), "immutable_image")
    if image.get("available") is not True:
        raise BuildIdentityError("Image manifest must record available=true")
    upstream = _require_mapping(image.get("upstream"), "immutable_image.upstream")
    installed = _require_mapping(image.get("installed"), "immutable_image.installed")
    service = _require_mapping(image.get("service"), "immutable_image.service")

    repository_url = _require_string(upstream.get("repository_url"), "repository_url")
    if repository_url != OFFICIAL_REPOSITORY_URL:
        raise BuildIdentityError("repository_url is not the official USGS ShakeMap repository")
    tag = _require_string(upstream.get("release_tag"), "release_tag")
    if _STABLE_TAG_RE.fullmatch(tag) is None:
        raise BuildIdentityError("release_tag is not a final stable tag")
    commit = _require_string(upstream.get("source_commit"), "source_commit").lower()
    if _FULL_COMMIT_RE.fullmatch(commit) is None:
        raise BuildIdentityError("source_commit is not a full commit")
    upstream["source_commit"] = commit

    for field in (
        "shakemap_distribution_version",
        "shakemap_modules_distribution_version",
        "python_version",
        "dependency_inventory_path",
    ):
        _require_string(installed.get(field), field)
    inventory_digest = _require_string(
        installed.get("dependency_inventory_sha256"), "dependency_inventory_sha256"
    ).lower()
    if _SHA256_RE.fullmatch(inventory_digest) is None:
        raise BuildIdentityError("dependency_inventory_sha256 is not a SHA-256 digest")
    installed["dependency_inventory_sha256"] = inventory_digest

    service_commit = service.get("source_commit")
    if service_commit is not None:
        service_commit = _require_string(service_commit, "service.source_commit").lower()
        if _FULL_COMMIT_RE.fullmatch(service_commit) is None:
            raise BuildIdentityError("service.source_commit is not a full commit")
        service["source_commit"] = service_commit
    if service.get("worktree_dirty_at_build") not in (True, False, None):
        raise BuildIdentityError("worktree_dirty_at_build must be true, false, or null")
    _require_string(image.get("built_at_utc"), "built_at_utc")
    return root


@lru_cache(maxsize=8)
def _load_path(path_text: str) -> dict:
    path = Path(path_text)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return validate_build_identity(data)
    except (OSError, json.JSONDecodeError, BuildIdentityError) as exc:
        return {
            "schema_version": 1,
            "immutable_image": {
                "available": False,
                "reason": f"Recorded build identity unavailable: {exc}",
                "manifest_path": str(path),
            },
        }


def clear_identity_cache() -> None:
    """Clear cached manifest reads; intended for tests and controlled reloads."""
    _load_path.cache_clear()


def load_build_identity(path: str | Path | None = None) -> dict:
    """Load immutable facts from the code-fixed path (or an explicit test path)."""
    selected = str(path if path is not None else IDENTITY_PATH)
    return deepcopy(_load_path(selected))


def deployment_identity() -> dict:
    """Return validated deployment facts supplied by the supported startup path."""
    supplied = {
        "image_id": os.getenv("SHAKEMAP_IMAGE_ID") or None,
        "image_digest": os.getenv("SHAKEMAP_IMAGE_DIGEST") or None,
    }
    validators = {
        "image_id": _IMAGE_ID_RE,
        "image_digest": _REPOSITORY_DIGEST_RE,
    }
    invalid_fields = [
        field
        for field, value in supplied.items()
        if value is not None and validators[field].fullmatch(value) is None
    ]
    trusted = {
        field: None if field in invalid_fields else value
        for field, value in supplied.items()
    }
    available = any(trusted.values())
    if invalid_fields:
        source = "runtime_environment_with_invalid_values" if available else "invalid_runtime_environment"
    else:
        source = "runtime_environment" if available else "unavailable"
    return {
        "available": available,
        "image_id": trusted["image_id"],
        "image_digest": trusted["image_digest"],
        "invalid_fields": invalid_fields,
        "source": source,
    }


def service_identity() -> dict:
    """Return the shared API/calculation identity model."""
    build = load_build_identity()
    return {
        "schema_version": 1,
        "immutable_image": build["immutable_image"],
        "deployment": deployment_identity(),
    }


def _distribution_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError as exc:
        raise BuildIdentityError(f"Required installed distribution is missing: {name}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_build_identity(
    *,
    output: Path,
    dependencies: Path,
    source_url: str,
    release_tag: str,
    release_version: str,
    source_commit: str,
    service_commit: str,
    service_worktree_dirty: str,
    build_timestamp_utc: str,
) -> dict:
    """Validate and write the build-time identity manifest."""
    shakemap_version = _distribution_version("shakemap")
    modules_version = _distribution_version("shakemap-modules")
    if shakemap_version != release_version:
        raise BuildIdentityError(
            "Installed ShakeMap distribution version does not match resolved release: "
            f"installed={shakemap_version!r}, resolved={release_version!r}"
        )
    if not dependencies.is_file() or dependencies.stat().st_size == 0:
        raise BuildIdentityError("Dependency inventory is missing or empty")

    manifest = {
        "schema_version": 1,
        "immutable_image": {
            "available": True,
            "upstream": {
                "repository_url": source_url,
                "release_tag": release_tag,
                "source_commit": source_commit,
            },
            "installed": {
                "shakemap_distribution_version": shakemap_version,
                "shakemap_modules_distribution_version": modules_version,
                "python_version": platform.python_version(),
                "dependency_inventory_path": str(dependencies),
                "dependency_inventory_sha256": _sha256(dependencies),
            },
            "service": {
                "source_commit": None if service_commit == "unavailable" else service_commit,
                "worktree_dirty_at_build": {
                    "true": True,
                    "false": False,
                    "unknown": None,
                }[service_worktree_dirty],
            },
            "built_at_utc": build_timestamp_utc,
        },
    }
    manifest = validate_build_identity(manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    writer = subparsers.add_parser("write", help="write an immutable build identity manifest")
    writer.add_argument("--output", type=Path, required=True)
    writer.add_argument("--dependencies", type=Path, required=True)
    writer.add_argument("--source-url", required=True)
    writer.add_argument("--release-tag", required=True)
    writer.add_argument("--release-version", required=True)
    writer.add_argument("--source-commit", required=True)
    writer.add_argument("--service-commit", required=True)
    writer.add_argument(
        "--service-worktree-dirty",
        choices=("true", "false", "unknown"),
        required=True,
    )
    writer.add_argument("--build-timestamp-utc", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        write_build_identity(
            output=args.output,
            dependencies=args.dependencies,
            source_url=args.source_url,
            release_tag=args.release_tag,
            release_version=args.release_version,
            source_commit=args.source_commit,
            service_commit=args.service_commit,
            service_worktree_dirty=args.service_worktree_dirty,
            build_timestamp_utc=args.build_timestamp_utc,
        )
    except (BuildIdentityError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
