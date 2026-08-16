#!/usr/bin/env python3
"""Prepare and validate release-matched ShakeMap verification data."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFINITION_ROOT = PROJECT_ROOT / "tests" / "verification_packages"
MODULE_PLAN = [
    "select",
    "assemble",
    "model",
    "contour",
    "mapping",
    "stations",
    "gridxml",
]


class VerificationDataError(RuntimeError):
    """Base error for package preparation and validation."""


class MissingSourceError(VerificationDataError):
    """A manual or remote source is unavailable."""


class IntegrityError(VerificationDataError):
    """A source or installed payload does not match its definition."""


class DestinationError(VerificationDataError):
    """A destination is unsafe to create or replace."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MissingSourceError(f"missing JSON file: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"unreadable JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise IntegrityError(f"JSON root must be an object: {path}")
    return value


def default_definition() -> Path:
    manifests = sorted(DEFINITION_ROOT.glob("v*/source-manifest.json"))
    if not manifests:
        raise MissingSourceError(f"no verification definitions under {DEFINITION_ROOT}")
    return manifests[-1]


def load_definition(path: Path) -> dict[str, Any]:
    definition = load_json(path)
    required = {
        "schema_version",
        "package_id",
        "compatibility",
        "module_plan",
        "default_destination",
        "coverage",
        "licenses",
        "image_dependencies",
        "sources",
        "limitations",
    }
    missing = sorted(required - definition.keys())
    if missing:
        raise IntegrityError(f"definition missing fields: {', '.join(missing)}")
    if definition.get("schema_version") != 2:
        raise IntegrityError("unsupported verification definition schema")
    if definition["module_plan"] != MODULE_PLAN:
        raise IntegrityError("definition does not contain the required module plan")
    return definition


def default_destination(definition: dict[str, Any]) -> Path:
    return PROJECT_ROOT / str(definition["default_destination"])


def exact_url(source: dict[str, Any], source_path: str | None = None) -> str:
    if source.get("kind") == "raw-files":
        if not source_path:
            raise IntegrityError(f"raw source {source.get('id')} has no source path")
        return str(source["url_prefix"]) + source_path
    return str(source["url"])


def manual_source_path(
    source_dir: Path, source: dict[str, Any], source_path: str | None = None
) -> Path:
    base = source_dir / str(source["id"])
    if source.get("kind") == "raw-files":
        if not source_path:
            raise IntegrityError(f"raw source {source.get('id')} has no source path")
        return base / source_path
    return base / str(source["source_filename"])


def verify_file(path: Path, size: int, sha256: str, label: str) -> None:
    if not path.is_file():
        raise MissingSourceError(f"missing {label}: {path}")
    actual_size = path.stat().st_size
    if actual_size != size:
        raise IntegrityError(
            f"corrupt {label}: {path} has {actual_size} bytes, expected {size}"
        )
    actual_digest = sha256_path(path)
    if actual_digest != sha256:
        raise IntegrityError(
            f"corrupt {label}: {path} SHA-256 {actual_digest}, expected {sha256}"
        )


def download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        url, headers={"User-Agent": "shakemap-docker-verification-data/1"}
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            with destination.open("wb") as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
    except Exception as exc:
        raise MissingSourceError(f"download failed for {url}: {exc}") from exc


def source_inventory(definition: dict[str, Any]) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    for source in definition["sources"]:
        if source["kind"] == "raw-files":
            for item in source["files"]:
                inventory.append(
                    {
                        "source_id": source["id"],
                        "url": exact_url(source, item["source_path"]),
                        "manual_path": str(
                            Path(source["id"]) / str(item["source_path"])
                        ),
                        "size": item["size"],
                        "sha256": item["sha256"],
                    }
                )
        else:
            inventory.append(
                {
                    "source_id": source["id"],
                    "url": exact_url(source),
                    "manual_path": str(
                        Path(source["id"]) / str(source["source_filename"])
                    ),
                    "size": source["source_size"],
                    "sha256": source["source_sha256"],
                }
            )
    return inventory


def validate_manual_sources(
    definition: dict[str, Any], source_dir: Path
) -> None:
    problems: list[str] = []
    for entry in source_inventory(definition):
        path = source_dir / entry["manual_path"]
        try:
            verify_file(path, entry["size"], entry["sha256"], "manual source")
        except VerificationDataError as exc:
            problems.append(str(exc))
    if problems:
        raise MissingSourceError(
            "manual source set is missing, corrupt, or partial:\n- "
            + "\n- ".join(problems)
        )


def expected_payload(definition: dict[str, Any]) -> dict[str, dict[str, Any]]:
    payload: dict[str, dict[str, Any]] = {}
    for source in definition["sources"]:
        for item in source["files"]:
            payload[item["target_path"]] = {
                "size": item["size"],
                "sha256": item["sha256"],
            }
    return payload


def expected_installed_records(
    definition: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    records = {
        item["target_path"]: installed_record(source, item)
        for source in definition["sources"]
        for item in source["files"]
    }
    return records


def validate_package(
    definition: dict[str, Any],
    destination: Path,
    recorded_destination: Path | None = None,
) -> dict[str, Any]:
    if not destination.is_dir():
        raise MissingSourceError(f"verification package is missing: {destination}")
    readme = destination / "README.md"
    manifest_path = destination / "package-manifest.json"
    if not readme.is_file() or not manifest_path.is_file():
        raise IntegrityError(
            f"partial package at {destination}: README.md and package-manifest.json are required"
        )
    readme_text = readme.read_text(encoding="utf-8")
    required_readme_text = [
        "# Release-matched ShakeMap verification package",
        "## Contents and size",
        "## Exact sources",
        "## Prepare or import",
        "## Validate",
        "## Licensing",
        "## Limitations",
        "package-manifest.json",
        definition["compatibility"]["shakemap_release_tag"],
        definition["compatibility"]["shakemap_source_commit"],
    ]
    missing_readme = [item for item in required_readme_text if item not in readme_text]
    if missing_readme:
        raise IntegrityError(
            f"prepared README is incomplete; missing required text: {missing_readme}"
        )
    manifest = load_json(manifest_path)
    if manifest.get("schema_version") != 2:
        raise IntegrityError("unsupported prepared package manifest schema")
    if manifest.get("package_id") != definition["package_id"]:
        raise IntegrityError("prepared package identifier is incompatible")
    if manifest.get("compatibility") != definition["compatibility"]:
        raise IntegrityError("prepared package release identity is incompatible")
    if manifest.get("module_plan") != MODULE_PLAN:
        raise IntegrityError("prepared package module plan is incompatible")
    required_manifest_fields = {
        "prepared_at_utc",
        "prepared_destination",
        "expected_destination",
        "coverage",
        "licenses",
        "image_dependencies",
        "source_download_bytes",
        "compressed_archive_bytes",
        "installed_payload_bytes",
        "validation_command",
        "limitations",
    }
    missing_manifest = sorted(required_manifest_fields - manifest.keys())
    if missing_manifest:
        raise IntegrityError(
            f"prepared package manifest missing fields: {missing_manifest}"
        )
    if manifest.get("coverage") != definition["coverage"]:
        raise IntegrityError("prepared package coverage metadata is incompatible")
    if manifest.get("licenses") != definition["licenses"]:
        raise IntegrityError("prepared package license metadata is incompatible")
    if manifest.get("image_dependencies") != definition["image_dependencies"]:
        raise IntegrityError("prepared package image dependency metadata is incompatible")
    if manifest.get("limitations") != definition["limitations"]:
        raise IntegrityError("prepared package limitations are incompatible")
    expected_destination_metadata = str(
        (recorded_destination or destination).resolve()
    )
    if manifest.get("prepared_destination") != expected_destination_metadata:
        raise IntegrityError("prepared package destination metadata is incompatible")

    expected = expected_payload(definition)
    expected_records = expected_installed_records(definition)
    expected_download_bytes = sum(
        entry["size"] for entry in source_inventory(definition)
    )
    expected_archive_bytes = sum(
        source["source_size"]
        for source in definition["sources"]
        if source["kind"] == "zip-member"
    )
    expected_installed_bytes = sum(item["size"] for item in expected.values())
    recorded_sizes = {
        "source_download_bytes": expected_download_bytes,
        "compressed_archive_bytes": expected_archive_bytes,
        "installed_payload_bytes": expected_installed_bytes,
    }
    for field, expected_size in recorded_sizes.items():
        if manifest.get(field) != expected_size:
            raise IntegrityError(
                f"prepared package manifest has wrong {field}: "
                f"{manifest.get(field)!r}, expected {expected_size}"
            )
    installed = manifest.get("files")
    if not isinstance(installed, list):
        raise IntegrityError("prepared package manifest has no file inventory")
    installed_by_path = {
        entry.get("installed_path"): entry
        for entry in installed
        if isinstance(entry, dict)
    }
    if set(installed_by_path) != set(expected):
        missing = sorted(set(expected) - set(installed_by_path))
        unexpected = sorted(set(installed_by_path) - set(expected))
        raise IntegrityError(
            f"prepared manifest file set differs; missing={missing}, unexpected={unexpected}"
        )
    for relative, facts in expected.items():
        entry = installed_by_path[relative]
        required_entry_fields = {
            "source_id",
            "source_url",
            "source_filename",
            "source_member",
            "installed_path",
            "installed_size",
            "installed_sha256",
            "transformation",
            "license_id",
        }
        missing_entry = sorted(required_entry_fields - entry.keys())
        if missing_entry:
            raise IntegrityError(
                f"prepared manifest entry for {relative} missing fields: {missing_entry}"
            )
        if entry.get("installed_size") != facts["size"]:
            raise IntegrityError(f"prepared manifest has wrong size for {relative}")
        if entry.get("installed_sha256") != facts["sha256"]:
            raise IntegrityError(f"prepared manifest has wrong SHA-256 for {relative}")
        for field, expected_value in expected_records[relative].items():
            if entry.get(field) != expected_value:
                raise IntegrityError(
                    f"prepared manifest has wrong {field} for {relative}"
                )
        verify_file(
            destination / relative,
            facts["size"],
            facts["sha256"],
            f"installed file {relative}",
        )

    allowed = set(expected) | {"README.md", "package-manifest.json"}
    actual = {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    }
    extras = sorted(actual - allowed)
    missing_files = sorted(allowed - actual)
    if extras or missing_files:
        raise IntegrityError(
            f"package contents are partial or unexpected; missing={missing_files}, unexpected={extras}"
        )
    return manifest


def installed_record(
    source: dict[str, Any], item: dict[str, Any]
) -> dict[str, Any]:
    source_path = str(item["source_path"])
    record = {
        "source_id": source["id"],
        "source_url": exact_url(
            source, source_path if source["kind"] == "raw-files" else None
        ),
        "source_filename": (
            source_path
            if source["kind"] == "raw-files"
            else source["source_filename"]
        ),
        "source_member": source_path if source["kind"] == "zip-member" else None,
        "installed_path": item["target_path"],
        "installed_size": item["size"],
        "installed_sha256": item["sha256"],
        "transformation": item["transformation"],
        "license_id": source["license_id"],
    }
    if source["kind"] == "raw-files":
        record["source_size"] = item["size"]
        record["source_sha256"] = item["sha256"]
    else:
        record["source_size"] = source["source_size"]
        record["source_sha256"] = source["source_sha256"]
    return record


def write_prepared_readme(
    definition: dict[str, Any],
    package_root: Path,
    destination: Path,
    retrieval_time: str,
    download_bytes: int,
    compressed_archive_bytes: int,
    installed_bytes: int,
) -> None:
    compatibility = definition["compatibility"]
    limitations = "\n".join(f"- {item}" for item in definition["limitations"])
    exact_sources = "\n".join(
        f"- `{entry['url']}` ({entry['size']} bytes; SHA-256 "
        f"`{entry['sha256']}`)"
        for entry in source_inventory(definition)
    )
    text = f"""# Release-matched ShakeMap verification package

This external package supports only the tracked `SCENARIO` request with
ShakeMap `{compatibility['shakemap_release_tag']}` at commit
`{compatibility['shakemap_source_commit']}`. It was prepared at
`{retrieval_time}` for `{destination}`.

The complete explicit native plan is:

```text
{' '.join(MODULE_PLAN)}
```

## Contents and size

The checksum-verified download total is {download_bytes} bytes. The package
contains no compressed generic-support archive. The installed payload is
{installed_bytes} bytes, excluding this README and the generated manifest.
`package-manifest.json` lists every source URL, source and installed filename,
transformation, size, SHA-256, license, and compatibility fact.

- `config/`: release-matched ShakeMap configuration used by the plan.
- `data/vs30/CA_vs30.grd`: measured California Vs30 grid; this is not uniform
  VS30.
- `data/mapping/CA_topo.grd`: California test topography.
- `data/layers/california.wkt`: selection override polygon.
- Natural Earth mapping layers are supplied read-only by the image.
- The STREC 2.3.14 `moment_tensors.db` installed with the Python distribution
  is supplied read-only by the image; this package does not duplicate it.

The event at ({definition['coverage']['event_latitude']},
{definition['coverage']['event_longitude']}) is within the recorded Vs30 and
topography bounds and California selection layer. This active-crust test does
not prove subduction behavior or worldwide scientific validity.

## Exact sources

{exact_sources}

The exact per-file URLs, source and installed filenames, transformations,
sizes, and SHA-256 values are recorded in `package-manifest.json`.

## Prepare or import

From the repository root with Python 3.10 or newer:

```bash
python3 scripts/prepare-shakemap-verification-data.py prepare
```

For manual placement, arrange the checksum-pinned source files exactly as
shown by `list-sources`, then run:

```bash
python3 scripts/prepare-shakemap-verification-data.py prepare --source-dir /path/to/source-mirror
```

The helper never overwrites an existing valid package and never deletes an
invalid operator directory. Use `--destination` for an isolated alternate
location.

## Validate

```bash
python scripts/prepare-shakemap-verification-data.py validate
```

Validation checks the package metadata and every checksum-pinned installed
file. It does not run ShakeMap.

## Licensing

ShakeMap states US public-domain terms and the upstream CC0-1.0 declaration
recorded in `package-manifest.json`. Generic STREC and Natural Earth licenses
belong to the immutable image-support inventory, not this scenario package.

## Limitations

{limitations}
"""
    (package_root / "README.md").write_text(text, encoding="utf-8")


def prepare_package(
    definition: dict[str, Any], destination: Path,
    source_dir: Path | None = None
) -> tuple[str, dict[str, Any]]:
    destination = destination.resolve()
    if destination.exists():
        try:
            manifest = validate_package(definition, destination)
        except VerificationDataError as exc:
            raise DestinationError(
                f"destination already exists but is incomplete, corrupt, or incompatible: "
                f"{destination}\n{exc}\nMove it aside or choose --destination; it was not modified."
            ) from exc
        return "already-valid", manifest
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    if source_dir is not None:
        source_dir = source_dir.resolve()
        validate_manual_sources(definition, source_dir)

    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.prepare-", dir=parent)
    )
    retrieval_time = utc_now()
    records: list[dict[str, Any]] = []
    source_download_bytes = 0
    compressed_archive_bytes = 0
    installed_bytes = 0
    try:
        source_cache = temporary / ".sources"
        for source in definition["sources"]:
            if source["kind"] == "raw-files":
                for item in source["files"]:
                    target = temporary / str(item["target_path"])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if source_dir is None:
                        download(exact_url(source, item["source_path"]), target)
                    else:
                        shutil.copyfile(
                            manual_source_path(
                                source_dir, source, str(item["source_path"])
                            ),
                            target,
                        )
                    verify_file(
                        target, item["size"], item["sha256"], "downloaded source"
                    )
                    source_download_bytes += item["size"]
                    installed_bytes += item["size"]
                    records.append(installed_record(source, item))
            elif source["kind"] == "zip-member":
                archive = source_cache / str(source["id"]) / str(
                    source["source_filename"]
                )
                archive.parent.mkdir(parents=True, exist_ok=True)
                if source_dir is None:
                    download(exact_url(source), archive)
                else:
                    shutil.copyfile(manual_source_path(source_dir, source), archive)
                verify_file(
                    archive,
                    source["source_size"],
                    source["source_sha256"],
                    "source archive",
                )
                source_download_bytes += source["source_size"]
                compressed_archive_bytes += source["source_size"]
                try:
                    with zipfile.ZipFile(archive) as bundle:
                        names = set(bundle.namelist())
                        for item in source["files"]:
                            if item["source_path"] not in names:
                                raise MissingSourceError(
                                    f"archive missing {item['source_path']}: {archive}"
                                )
                            data = bundle.read(item["source_path"])
                            if len(data) != item["size"]:
                                raise IntegrityError(
                                    f"archive member has wrong size: {item['source_path']}"
                                )
                            if sha256_bytes(data) != item["sha256"]:
                                raise IntegrityError(
                                    f"archive member has wrong SHA-256: {item['source_path']}"
                                )
                            target = temporary / str(item["target_path"])
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_bytes(data)
                            installed_bytes += len(data)
                            records.append(installed_record(source, item))
                except zipfile.BadZipFile as exc:
                    raise IntegrityError(f"invalid source archive: {archive}") from exc
            else:
                raise IntegrityError(f"unsupported source kind: {source['kind']}")

        shutil.rmtree(source_cache, ignore_errors=True)
        manifest = {
            "schema_version": 2,
            "package_id": definition["package_id"],
            "prepared_at_utc": retrieval_time,
            "prepared_destination": str(destination),
            "expected_destination": definition["default_destination"],
            "compatibility": definition["compatibility"],
            "module_plan": MODULE_PLAN,
            "coverage": definition["coverage"],
            "licenses": definition["licenses"],
            "image_dependencies": definition["image_dependencies"],
            "source_download_bytes": source_download_bytes,
            "compressed_archive_bytes": compressed_archive_bytes,
            "installed_payload_bytes": installed_bytes,
            "files": sorted(records, key=lambda item: item["installed_path"]),
            "validation_command": (
                "python scripts/prepare-shakemap-verification-data.py validate "
                f"--destination {destination}"
            ),
            "limitations": definition["limitations"],
        }
        (temporary / "package-manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        write_prepared_readme(
            definition,
            temporary,
            destination,
            retrieval_time,
            source_download_bytes,
            compressed_archive_bytes,
            installed_bytes,
        )
        validate_package(definition, temporary, destination)
        if destination.exists():
            raise DestinationError(
                f"destination appeared during preparation and was not modified: {destination}"
            )
        os.replace(temporary, destination)
        return "prepared", validate_package(definition, destination)
    except Exception:
        if temporary.exists() and temporary.parent == parent:
            shutil.rmtree(temporary)
        raise


def validate_known_legacy_package(destination: Path) -> dict[str, Any]:
    """Prove that a persistent package is the known generated v1 package."""
    manifest = load_json(destination / "package-manifest.json")
    if manifest.get("schema_version") != 1 or manifest.get("package_id") != "shakemap-verification-v4.4.9":
        raise DestinationError("existing destination is not the known legacy verification package")
    if manifest.get("compatibility", {}).get("shakemap_source_commit") != "8923f1ff6e82fc866d928a33d1e19e45f276db52":
        raise DestinationError("legacy package has an unexpected ShakeMap identity")
    records = manifest.get("files")
    if not isinstance(records, list) or len(records) != 33:
        raise DestinationError("legacy package does not contain its expected 33-file payload")
    required = {
        "data/strec/moment_tensors.db",
        "data/cartopy/shapefiles/natural_earth/cultural/ne_10m_admin_0_countries.shp",
        "data/vs30/CA_vs30.grd",
        "data/mapping/CA_topo.grd",
    }
    if not required.issubset({record.get("installed_path") for record in records}):
        raise DestinationError("legacy package is missing its identifying support payload")
    for record in records:
        verify_file(
            destination / str(record.get("installed_path", "")),
            int(record.get("installed_size", -1)),
            str(record.get("installed_sha256", "")),
            "known legacy installed file",
        )
    return manifest


def migrate_known_legacy_package(
    definition: dict[str, Any], destination: Path, source_dir: Path | None
) -> tuple[str, dict[str, Any]]:
    try:
        return prepare_package(definition, destination, source_dir)
    except DestinationError:
        validate_known_legacy_package(destination)

    staging = destination.with_name(f".{destination.name}.corrected-{uuid.uuid4().hex}")
    preserved = destination.with_name(
        f"{destination.name}.legacy-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    )
    prepare_package(definition, staging, source_dir)
    validate_package(definition, staging)
    moved_old = False
    try:
        os.replace(destination, preserved)
        moved_old = True
        os.replace(staging, destination)
        manifest_path = destination / "package-manifest.json"
        manifest = load_json(manifest_path)
        manifest["prepared_destination"] = str(destination.resolve())
        manifest["migration"] = {
            "migrated_at_utc": utc_now(),
            "known_legacy_package_preserved_at": str(preserved.resolve()),
            "legacy_package_id": "shakemap-verification-v4.4.9",
        }
        temporary = manifest_path.with_name(f".{manifest_path.name}.{uuid.uuid4().hex}")
        temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, manifest_path)
        return "migrated-known-legacy", validate_package(definition, destination)
    except Exception:
        if moved_old and not destination.exists() and preserved.exists():
            os.replace(preserved, destination)
        raise


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--definition",
        type=Path,
        default=None,
        help="Tracked source-manifest.json (default: latest tracked definition)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser(
        "list-sources", help="Print URLs, manual mirror paths, sizes, and checksums"
    )
    add_common(list_parser)

    prepare_parser = subparsers.add_parser(
        "prepare", help="Download or manually import and validate the package"
    )
    add_common(prepare_parser)
    prepare_parser.add_argument("--destination", type=Path)
    prepare_parser.add_argument(
        "--source-dir",
        type=Path,
        help="Offline source mirror using paths printed by list-sources",
    )
    prepare_parser.add_argument(
        "--migrate-known-legacy",
        action="store_true",
        help="Preserve and replace only the checksum-verified generated v1 package",
    )

    validate_parser = subparsers.add_parser(
        "validate", help="Validate an existing prepared package"
    )
    add_common(validate_parser)
    validate_parser.add_argument("--destination", type=Path)

    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    definition_path = (args.definition or default_definition()).resolve()
    try:
        definition = load_definition(definition_path)
        destination = (
            args.destination.resolve()
            if getattr(args, "destination", None)
            else default_destination(definition).resolve()
        )
        if args.command == "list-sources":
            print(json.dumps(source_inventory(definition), indent=2, sort_keys=True))
            return 0
        if args.command == "prepare":
            if args.migrate_known_legacy:
                state, manifest = migrate_known_legacy_package(
                    definition, destination, args.source_dir
                )
            else:
                state, manifest = prepare_package(
                    definition, destination, args.source_dir
                )
            print(f"{state}: {destination}")
            print(
                f"payload: {manifest['installed_payload_bytes']} bytes; "
                f"sources: {manifest['source_download_bytes']} bytes; "
                f"compressed archives: {manifest['compressed_archive_bytes']} bytes"
            )
            return 0
        if args.command == "validate":
            manifest = validate_package(definition, destination)
            print(f"valid: {destination}")
            print(
                f"release: {manifest['compatibility']['shakemap_release_tag']} "
                f"commit: {manifest['compatibility']['shakemap_source_commit']}"
            )
            return 0
        raise VerificationDataError(f"unsupported command: {args.command}")
    except VerificationDataError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
