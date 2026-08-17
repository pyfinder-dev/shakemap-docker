# -*- coding: utf-8 -*-
"""Read-only data inspection and explicit provisioning."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import urllib.request
import uuid
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_global_assets() -> dict[str, dict[str, Any]]:
    resource = files("shakemap_service").joinpath("data/global-assets.json")
    manifest = json.loads(resource.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise RuntimeError("unsupported global scientific-data manifest schema")
    assets = manifest.get("assets")
    if not isinstance(assets, dict) or set(assets) != {"vs30", "topography"}:
        raise RuntimeError("global scientific-data manifest must define Vs30 and topography")
    return assets


GLOBAL_ASSETS = load_global_assets()
STAGED_SUFFIX = ".staged"

class DataProvisioningError(RuntimeError):
    """Raised when an explicit data operation cannot complete safely."""


def inspect_data_assets(data_root: Path) -> dict[str, Any]:
    """Return cheap, read-only presence evidence for contracted data paths."""
    root = Path(data_root)
    assets: dict[str, dict[str, Any]] = {}
    for name, path, expected_kind in (
        ("global_vs30", root / "global/vs30/global_vs30.grd", "file"),
        ("global_topography", root / "global/topo/topo_30sec.grd", "file"),
    ):
        present = path.is_file() if expected_kind == "file" else path.is_dir()
        assets[name] = {
            "path": str(path),
            "present": present,
            "readable": present and os.access(path, os.R_OK),
        }

    global_root = root / "global"
    configurations = [
        {
            "name": "global",
            "path": str(global_root),
            "present": global_root.is_dir(),
            "readable": global_root.is_dir() and os.access(global_root, os.R_OK),
            "validation_state": "not_evaluated",
        }
    ]
    regional_root = root / "regional"
    if regional_root.is_dir() and os.access(regional_root, os.R_OK):
        try:
            configurations.extend(
                {
                    "name": path.name,
                    "path": str(path),
                    "present": True,
                    "readable": os.access(path, os.R_OK),
                    "validation_state": "not_evaluated",
                }
                for path in sorted(regional_root.iterdir(), key=lambda item: item.name)
                if path.is_dir() and not path.name.startswith(".")
            )
        except OSError:
            pass

    return {
        "assets": assets,
        "configurations": configurations,
        "summary": {
            "validation_state": "not_evaluated",
            "compatibility_state": "not_evaluated",
            "coverage_state": "not_evaluated",
            "actual_use_state": "not_evaluated",
        },
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_record(
    path: Path, *, source: dict[str, Any] | None = None
) -> dict[str, Any]:
    record = {
        "path": str(path),
        "size": path.stat().st_size,
        "sha256": sha256(path),
    }
    if source:
        record.update(
            {
                "source_url": source["url"],
                "checksum_authority": source["checksum_authority"],
                "expected_size": source["size"],
                "expected_sha256": source["sha256"],
            }
        )
    return record


def validate_pinned_file(
    path: Path, spec: dict[str, Any]
) -> tuple[bool, str]:
    try:
        if path.is_symlink():
            return False, "unexpected symbolic link"
        if path.exists() and not path.is_file():
            return False, "unexpected non-file path"
        if not path.is_file():
            return False, "missing"
        if path.stat().st_size != spec["size"]:
            return False, "size mismatch"
        with path.open("rb") as stream:
            signature = stream.read(8)
        if signature != b"\x89HDF\r\n\x1a\n":
            return False, "not an HDF5/netCDF4 grid"
        if sha256(path) != spec["sha256"]:
            return False, "checksum mismatch"
    except OSError as exc:
        return False, f"unreadable: {exc}"
    return True, "valid pinned file"


def download(url: str, destination: Path) -> None:
    request = urllib.request.Request(
        url, headers={"User-Agent": "shakemap-docker-data-provisioning/1"}
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        with destination.open("wb") as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)


def provision_file(
    target: Path,
    spec: dict[str, Any],
    source: Path | None,
    allow_download: bool,
) -> dict[str, Any]:
    """Validate or install one missing pinned file without replacing a target."""
    label = str(spec.get("label", target.name))
    valid, reason = validate_pinned_file(target, spec)
    if valid:
        return {
            "action": "reused",
            "validation": reason,
            **file_record(target, source=spec),
        }

    if target.exists() or target.is_symlink():
        raise DataProvisioningError(
            f"{label}: existing asset at {target} failed validation ({reason}); "
            "it was left unchanged. Move or remove it explicitly after review, "
            "then rerun provisioning"
        )

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DataProvisioningError(
            f"{label}: could not prepare the parent of missing asset {target}: "
            f"{exc}; correct the target path and directory permissions, then retry"
        ) from exc
    temporary = target.with_name(f".{target.name}.install-{uuid.uuid4().hex}")
    try:
        if source is not None:
            if not source.is_file():
                raise DataProvisioningError(
                    f"{label}: manual source is missing at {source}; supply a "
                    "readable repository-pinned source file, or omit the "
                    "source option to allow download"
                )
            try:
                shutil.copyfile(source, temporary)
            except OSError as exc:
                raise DataProvisioningError(
                    f"{label}: could not import {source} to target {target}: "
                    f"{exc}; correct source permissions or choose another file"
                ) from exc
            action = "imported"
        elif allow_download:
            try:
                download(spec["url"], temporary)
            except OSError as exc:
                raise DataProvisioningError(
                    f"{label}: download for target {target} failed from "
                    f"{spec['url']}: {exc}; retry or supply a manual source"
                ) from exc
            action = "downloaded"
        else:
            raise DataProvisioningError(
                f"{label}: asset is missing at {target}; supply the "
                "repository-pinned manual source or rerun with download enabled"
            )

        candidate_valid, candidate_reason = validate_pinned_file(temporary, spec)
        if not candidate_valid:
            raise DataProvisioningError(
                f"{label}: candidate for missing asset at {target} failed "
                f"identity validation ({candidate_reason}); obtain the "
                "repository-pinned asset and retry"
            )

        try:
            os.link(temporary, target)
        except FileExistsError as exc:
            raise DataProvisioningError(
                f"{label}: an asset appeared at {target} during provisioning; "
                "it was left unchanged. Inspect or validate it, then rerun"
            ) from exc
        except OSError as exc:
            raise DataProvisioningError(
                f"{label}: could not install the validated missing asset at "
                f"{target}: {exc}; correct the target directory permissions or "
                "place the pinned file manually, then retry"
            ) from exc
        temporary.unlink()
        return {
            "action": action,
            "validation": "valid pinned file",
            **file_record(target, source=spec),
        }
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def validate_pinned_global_assets(data_root: Path) -> dict[str, Any]:
    """Check only pinned byte identity at the contracted global-data paths."""
    assets = {}
    for name, spec in GLOBAL_ASSETS.items():
        path = data_root / spec["relative"]
        valid, reason = validate_pinned_file(path, spec)
        assets[name] = {
            "path": str(path),
            "valid": valid,
            "reason": reason,
            "corrective_action": (
                None
                if valid
                else (
                    "provision the missing pinned asset or place a valid manual "
                    "copy at this path"
                    if reason == "missing"
                    else "review the existing asset, then move or remove it "
                    "explicitly before provisioning the pinned asset"
                )
            ),
        }

    return {
        "validation_scope": "pinned_content_integrity",
        "global_assets": assets,
        "pinned_integrity_valid": all(item["valid"] for item in assets.values()),
    }


def provision_global_data(
    data_root: Path,
    *,
    vs30_source: Path | None,
    topo_source: Path | None,
    allow_download: bool,
) -> dict[str, Any]:
    """Explicitly provision pinned global data at contracted destinations."""
    manual = {"vs30": vs30_source, "topography": topo_source}
    assets = {
        name: provision_file(
            data_root / spec["relative"],
            spec,
            manual[name],
            allow_download,
        )
        for name, spec in GLOBAL_ASSETS.items()
    }
    return {"global_assets": assets}


def staged_asset_path(target: Path) -> Path:
    return target.with_name(f".{target.name}{STAGED_SUFFIX}")


def stage_replacement_file(
    target: Path,
    spec: dict[str, Any],
    source: Path | None,
    allow_download: bool,
) -> dict[str, Any]:
    """Validate a replacement beside its target without changing active data."""
    label = str(spec.get("label", target.name))
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = staged_asset_path(target)
    if staged.exists() and (staged.is_symlink() or not staged.is_file()):
        raise DataProvisioningError(
            f"{label}: staged replacement path is unsafe at {staged}; move it aside and retry"
        )
    temporary = target.parent / f".{target.name}.stage-{uuid.uuid4().hex}"
    try:
        if source is not None:
            if not source.is_file() or source.is_symlink():
                raise DataProvisioningError(
                    f"{label}: replacement source is not a readable regular file: {source}"
                )
            shutil.copyfile(source, temporary)
            action = "staged-import"
        elif allow_download:
            download(spec["url"], temporary)
            action = "staged-download"
        else:
            raise DataProvisioningError(
                f"{label}: replacement requires a source or enabled download"
            )
        valid, reason = validate_pinned_file(temporary, spec)
        if not valid:
            raise DataProvisioningError(
                f"{label}: replacement candidate failed identity validation ({reason}); "
                "the active asset was left unchanged"
            )
        os.replace(temporary, staged)
        fsync_directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(fsync_directory)
        finally:
            os.close(fsync_directory)
        return {
            "action": action,
            "active_path": str(target),
            **file_record(staged, source=spec),
        }
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def stage_global_replacements(
    data_root: Path,
    *,
    vs30_source: Path | None,
    topo_source: Path | None,
    allow_download: bool,
) -> dict[str, Any]:
    manual = {"vs30": vs30_source, "topography": topo_source}
    selected = (
        set(GLOBAL_ASSETS)
        if vs30_source is None and topo_source is None
        else {name for name, source in manual.items() if source is not None}
    )
    return {
        "global_assets": {
            name: stage_replacement_file(
                data_root / spec["relative"],
                spec,
                manual[name],
                allow_download,
            )
            for name, spec in GLOBAL_ASSETS.items()
            if name in selected
        }
    }


def activate_staged_global_replacements(data_root: Path) -> dict[str, Any]:
    """Atomically activate each fully validated staged global asset."""
    candidates: list[tuple[str, dict[str, Any], Path, Path]] = []
    for name, spec in GLOBAL_ASSETS.items():
        target = data_root / spec["relative"]
        staged = staged_asset_path(target)
        if not staged.exists():
            continue
        valid, reason = validate_pinned_file(staged, spec)
        if not valid:
            raise DataProvisioningError(
                f"{spec['label']}: staged replacement at {staged} failed validation "
                f"({reason}); active data was left unchanged"
            )
        if target.exists() and (target.is_symlink() or not target.is_file()):
            raise DataProvisioningError(
                f"{spec['label']}: active path is unsafe at {target}; it was left unchanged"
            )
        candidates.append((name, spec, target, staged))

    activated: dict[str, Any] = {}
    for name, spec, target, staged in candidates:
        os.replace(staged, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        activated[name] = {
            "action": "activated",
            **file_record(target, source=spec),
        }
    return {"global_assets": activated}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    default_data = PROJECT_ROOT / "runtime/shakemap/data"

    inspect = commands.add_parser("inspect")
    inspect.add_argument("--data-root", type=Path, default=default_data)

    validate = commands.add_parser("validate")
    validate.add_argument("--data-root", type=Path, default=default_data)

    provision = commands.add_parser("provision")
    provision.add_argument("--data-root", type=Path, default=default_data)
    provision.add_argument("--vs30-source", type=Path)
    provision.add_argument("--topo-source", type=Path)
    provision.add_argument("--no-download", action="store_true")
    stage = commands.add_parser("stage")
    stage.add_argument("--data-root", type=Path, default=default_data)
    stage.add_argument("--vs30-source", type=Path)
    stage.add_argument("--topo-source", type=Path)
    stage.add_argument("--no-download", action="store_true")
    return root


def main(argv: Iterable[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "inspect":
            value = inspect_data_assets(args.data_root)
            result = 0
        elif args.command == "validate":
            value = validate_pinned_global_assets(args.data_root)
            result = 0 if value["pinned_integrity_valid"] else 1
        elif args.command == "provision":
            value = provision_global_data(
                args.data_root,
                vs30_source=args.vs30_source,
                topo_source=args.topo_source,
                allow_download=not args.no_download,
            )
            result = 0
        elif args.command == "stage":
            value = stage_global_replacements(
                args.data_root,
                vs30_source=args.vs30_source,
                topo_source=args.topo_source,
                allow_download=not args.no_download,
            )
            result = 0
        else:
            return 2
        print(json.dumps(value, indent=2, sort_keys=True))
        return result
    except (OSError, DataProvisioningError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
