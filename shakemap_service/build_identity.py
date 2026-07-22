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
    if root.get("schema_version") != 2:
        raise BuildIdentityError("Unsupported build identity schema_version")
    image = _require_mapping(root.get("immutable_image"), "immutable_image")
    if image.get("available") is not True:
        raise BuildIdentityError("Image manifest must record available=true")
    upstream = _require_mapping(image.get("upstream"), "immutable_image.upstream")
    installed = _require_mapping(image.get("installed"), "immutable_image.installed")
    service = _require_mapping(image.get("service"), "immutable_image.service")
    support = _require_mapping(image.get("support"), "immutable_image.support")

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

    natural_earth = _require_mapping(support.get("natural_earth"), "support.natural_earth")
    strec = _require_mapping(support.get("strec"), "support.strec")
    if natural_earth.get("tag") != "v5.1.2":
        raise BuildIdentityError("unsupported Natural Earth tag")
    if _FULL_COMMIT_RE.fullmatch(_require_string(natural_earth.get("commit"), "natural_earth.commit")) is None:
        raise BuildIdentityError("natural_earth.commit is not a full commit")
    if natural_earth.get("file_count") != 20:
        raise BuildIdentityError("Natural Earth image support must contain 20 files")
    for field in ("manifest_path", "manifest_sha256", "cartopy_data_dir"):
        value = _require_string(natural_earth.get(field), f"natural_earth.{field}")
        if field.endswith("sha256") and _SHA256_RE.fullmatch(value) is None:
            raise BuildIdentityError(f"{field} is not a SHA-256 digest")
    _require_string(strec.get("distribution_version"), "strec.distribution_version")
    for field in ("database_path", "database_link", "database_sha256"):
        value = _require_string(strec.get(field), f"strec.{field}")
        if field.endswith("sha256") and _SHA256_RE.fullmatch(value) is None:
            raise BuildIdentityError(f"{field} is not a SHA-256 digest")
    if not isinstance(strec.get("database_size"), int) or strec["database_size"] <= 0:
        raise BuildIdentityError("strec.database_size must be positive")

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
            "schema_version": 2,
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
    natural_earth_manifest: Path,
    cartopy_data_dir: Path,
    strec_database_link: Path = Path("/opt/shakemap-support/strec/moment_tensors.db"),
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

    try:
        natural_earth = json.loads(natural_earth_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildIdentityError(f"Natural Earth manifest is unreadable: {exc}") from exc
    if natural_earth.get("schema_version") != 1 or len(natural_earth.get("files", [])) != 20:
        raise BuildIdentityError("Natural Earth manifest is invalid")
    for record in natural_earth["files"]:
        path = cartopy_data_dir / record["target_path"]
        if not path.is_file() or path.stat().st_size != record["size"] or _sha256(path) != record["sha256"]:
            raise BuildIdentityError(f"Natural Earth support file failed verification: {path}")

    strec_dist = importlib.metadata.distribution("usgs-strec")
    strec_database = next(
        (strec_dist.locate_file(item) for item in strec_dist.files or [] if str(item).endswith("strec/data/moment_tensors.db")),
        None,
    )
    strec_link = strec_database_link
    if strec_database is None or not Path(strec_database).is_file() or not strec_link.is_symlink():
        raise BuildIdentityError("Installed STREC moment_tensors.db or image support link is missing")
    strec_database = Path(strec_database)

    manifest = {
        "schema_version": 2,
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
            "support": {
                "natural_earth": {
                    "tag": natural_earth["tag"],
                    "commit": natural_earth["commit"],
                    "manifest_path": str(natural_earth_manifest),
                    "manifest_sha256": _sha256(natural_earth_manifest),
                    "cartopy_data_dir": str(cartopy_data_dir),
                    "file_count": len(natural_earth["files"]),
                    "layers": natural_earth["layers"],
                },
                "strec": {
                    "distribution_version": strec_dist.version,
                    "database_path": str(strec_database),
                    "database_link": str(strec_link),
                    "database_size": strec_database.stat().st_size,
                    "database_sha256": _sha256(strec_database),
                    "database_is_installed_distribution_file": True,
                },
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
    writer.add_argument("--natural-earth-manifest", type=Path, required=True)
    writer.add_argument("--cartopy-data-dir", type=Path, required=True)
    writer.add_argument(
        "--strec-database-link",
        type=Path,
        default=Path("/opt/shakemap-support/strec/moment_tensors.db"),
    )
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
            natural_earth_manifest=args.natural_earth_manifest,
            cartopy_data_dir=args.cartopy_data_dir,
            strec_database_link=args.strec_database_link,
        )
    except (BuildIdentityError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
