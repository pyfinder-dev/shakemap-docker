# -*- coding: utf-8 -*-
"""Read-only data inspection, explicit provisioning, and product readers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request
import uuid
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]

GLOBAL_ASSETS = {
    "vs30": {
        "label": "global Vs30 grid",
        "relative": "global/vs30/global_vs30.grd",
        "url": "https://apps.usgs.gov/shakemap_geodata/vs30/global_vs30.grd",
        "size": 610189275,
        "sha256": "b07944c5be332c5a261777d23b3390fe8d5638f25b388b82f5dc1e98c6356011",
        "checksum_authority": (
            "project-verified download pin; USGS publishes no checksum "
            "alongside the file"
        ),
    },
    "topography": {
        "label": "global topography grid",
        "relative": "global/topo/topo_30sec.grd",
        "url": "https://apps.usgs.gov/shakemap_geodata/topo/topo_30sec.grd",
        "size": 249661705,
        "sha256": "3aa02a77d56d656deae9bf4539afdb3ce1dd1b7057a67a5c7bdd0573fc97bd4c",
        "checksum_authority": (
            "project-verified download pin; USGS publishes no checksum "
            "alongside the file"
        ),
    },
}

SLAB2 = {
    "label": "Slab2 archive",
    "url": "https://apps.usgs.gov/shakemap_geodata/slabs/slab2.zip",
    "size": 12028579,
    "sha256": "2258004fd3d8467e894a1bdb3cd4224a40bd3c876b4ec2e35617f265c7047360",
    "checksum_authority": (
        "project-verified download pin; USGS publishes no checksum alongside "
        "the file"
    ),
    "file_count": 108,
}


class DataProvisioningError(RuntimeError):
    """Raised when an explicit data operation cannot complete safely."""


def inspect_data_assets(data_root: Path) -> dict[str, Any]:
    """Return cheap, read-only presence evidence for contracted data paths."""
    root = Path(data_root)
    assets: dict[str, dict[str, Any]] = {}
    for name, path, expected_kind in (
        ("global_vs30", root / "global/vs30/global_vs30.grd", "file"),
        ("global_topography", root / "global/topo/topo_30sec.grd", "file"),
        ("strec_slabs", root / "global/strec/slabs", "directory"),
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


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_text = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_text)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


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
    if not path.is_file():
        return False, "missing"
    try:
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
    """Validate then atomically install one pinned file at its target."""
    label = str(spec.get("label", target.name))
    valid, reason = validate_pinned_file(target, spec)
    if valid:
        return {
            "action": "reused",
            "validation": reason,
            **file_record(target, source=spec),
        }

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.install-{uuid.uuid4().hex}")
    try:
        if source is not None:
            if not source.is_file():
                raise DataProvisioningError(
                    f"{label}: manual source is missing at {source}; supply a "
                    "readable source file or allow download"
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
                f"{label}: target {target} is {reason}; supply a valid manual "
                "source or rerun with download enabled"
            )

        replacement_valid, replacement_reason = validate_pinned_file(
            temporary, spec
        )
        if not replacement_valid:
            raise DataProvisioningError(
                f"{label}: replacement for {target} is invalid "
                f"({replacement_reason}); obtain the pinned asset and retry"
            )

        preserved = None
        if target.exists():
            preserved = target.with_name(
                f"{target.name}.invalid-"
                f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
            )
            if preserved.exists():
                preserved = target.with_name(
                    f"{preserved.name}-{uuid.uuid4().hex[:8]}"
                )
            shutil.copyfile(target, preserved)
        os.replace(temporary, target)
        return {
            "action": action,
            "previous_validation": reason,
            "preserved_invalid_path": str(preserved) if preserved else None,
            **file_record(target, source=spec),
        }
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def validate_slab_directory(
    root: Path,
) -> tuple[bool, str, dict[str, Any] | None]:
    manifest_path = root.parent / "slab2-manifest.json"
    if not root.is_dir() or not manifest_path.is_file():
        return False, "missing slab directory or manifest", None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("source", {}).get("sha256") != SLAB2["sha256"]:
            return False, "slab source identity differs", manifest
        records = manifest["files"]
        if len(records) != SLAB2["file_count"]:
            return False, "slab file count differs", manifest
        for record in records:
            relative = Path(record["path"])
            if relative.name != record["path"]:
                return False, f"unsafe slab manifest path: {record['path']}", manifest
            path = root / relative
            if (
                not path.is_file()
                or path.stat().st_size != record["size"]
                or sha256(path) != record["sha256"]
            ):
                return False, f"slab file invalid: {record['path']}", manifest
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return False, f"slab manifest unreadable: {exc}", None
    return True, "valid extracted Slab2 package", manifest


def provision_slabs(
    data_root: Path, source: Path | None, allow_download: bool
) -> dict[str, Any]:
    """Validate and install Slab2 below ``global/strec/slabs``."""
    strec_root = data_root / "global/strec"
    target = strec_root / "slabs"
    valid, reason, manifest = validate_slab_directory(target)
    if valid:
        return {"action": "reused", "validation": reason, "manifest": manifest}

    strec_root.mkdir(parents=True, exist_ok=True)
    archive = strec_root / f".slab2-{uuid.uuid4().hex}.zip"
    temporary = strec_root / f".slabs-install-{uuid.uuid4().hex}"
    manifest_temporary: Path | None = None
    try:
        if source is not None:
            if not source.is_file():
                raise DataProvisioningError(
                    f"Slab2 archive: manual source is missing at {source}; "
                    "supply a readable slab2.zip or allow download"
                )
            try:
                shutil.copyfile(source, archive)
            except OSError as exc:
                raise DataProvisioningError(
                    f"Slab2 archive: could not import {source} for target "
                    f"{target}: {exc}; correct source permissions or choose "
                    "another archive"
                ) from exc
            action = "imported"
        elif allow_download:
            try:
                download(SLAB2["url"], archive)
            except OSError as exc:
                raise DataProvisioningError(
                    f"Slab2 archive: download for target {target} failed from "
                    f"{SLAB2['url']}: {exc}; retry or supply a manual archive"
                ) from exc
            action = "downloaded"
        else:
            raise DataProvisioningError(
                f"Slab2 slabs: target {target} is {reason}; supply a valid "
                "slab2.zip or rerun with download enabled"
            )

        if (
            archive.stat().st_size != SLAB2["size"]
            or sha256(archive) != SLAB2["sha256"]
        ):
            raise DataProvisioningError(
                f"Slab2 archive: source for {target} does not match the pinned "
                "asset; obtain the supported archive and retry"
            )

        temporary.mkdir()
        try:
            with zipfile.ZipFile(archive) as bundle:
                names = bundle.namelist()
                if (
                    len(names) != SLAB2["file_count"]
                    or any(Path(name).name != name for name in names)
                ):
                    raise DataProvisioningError(
                        f"Slab2 archive: source for {target} has an unexpected "
                        "or unsafe layout; obtain the supported archive and "
                        "retry"
                    )
                bundle.extractall(temporary)
        except zipfile.BadZipFile as exc:
            raise DataProvisioningError(
                f"Slab2 archive: source for {target} is not a readable ZIP: "
                f"{exc}; obtain the supported archive and retry"
            ) from exc

        records = [
            {
                "path": path.name,
                "size": path.stat().st_size,
                "sha256": sha256(path),
            }
            for path in sorted(temporary.iterdir())
            if path.is_file()
        ]
        new_manifest = {
            "schema_version": 1,
            "provisioned_at_utc": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
            "source": SLAB2,
            "files": records,
        }
        manifest_temporary = strec_root / (
            f".slab2-manifest-install-{uuid.uuid4().hex}.json"
        )
        atomic_json(manifest_temporary, new_manifest)

        preserved = None
        if target.exists():
            preserved = strec_root / (
                "slabs.invalid-"
                f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-"
                f"{uuid.uuid4().hex[:8]}"
            )
            os.replace(target, preserved)
        os.replace(temporary, target)
        os.replace(manifest_temporary, strec_root / "slab2-manifest.json")
        manifest_temporary = None
        return {
            "action": action,
            "previous_validation": reason,
            "preserved_invalid_path": str(preserved) if preserved else None,
            "manifest": new_manifest,
        }
    finally:
        archive.unlink(missing_ok=True)
        if temporary.exists():
            shutil.rmtree(temporary)
        if manifest_temporary is not None:
            manifest_temporary.unlink(missing_ok=True)


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
                else "provision the pinned asset or place a valid manual copy "
                "at this path"
            ),
        }

    slab_path = data_root / "global/strec/slabs"
    slab_valid, slab_reason, manifest = validate_slab_directory(slab_path)
    pinned_integrity_valid = (
        all(item["valid"] for item in assets.values()) and slab_valid
    )
    return {
        "validation_scope": "pinned_content_integrity",
        "global_assets": assets,
        "slabs": {
            "path": str(slab_path),
            "valid": slab_valid,
            "reason": slab_reason,
            "manifest": manifest,
            "corrective_action": (
                None
                if slab_valid
                else "provision the pinned Slab2 archive or place a valid "
                "slab tree and manifest at this path"
            ),
        },
        "pinned_integrity_valid": pinned_integrity_valid,
    }


def provision_global_data(
    data_root: Path,
    *,
    vs30_source: Path | None,
    topo_source: Path | None,
    slab_source: Path | None,
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
    return {
        "global_assets": assets,
        "slabs": provision_slabs(data_root, slab_source, allow_download),
    }


def validate_composed_image(
    path: Path, *, minimum_height_width_ratio: float = 0.5
) -> dict[str, Any]:
    """Validate that a required map is readable and not a legend-only strip."""
    record: dict[str, Any] = {
        "path": str(path),
        "minimum_height_width_ratio": minimum_height_width_ratio,
        "passed": False,
    }
    if not path.is_file():
        record["reason"] = "required composed image is missing"
        return record
    try:
        try:
            from PIL import Image

            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                width, height = image.size
                image.load()
                image_format = image.format
        except ModuleNotFoundError:
            data = path.read_bytes()
            if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
                raise ValueError("not a complete JPEG stream")
            width = height = 0
            saw_scan = False
            offset = 2
            while offset < len(data) - 1:
                if data[offset] != 0xFF:
                    offset += 1
                    continue
                while offset < len(data) and data[offset] == 0xFF:
                    offset += 1
                marker = data[offset]
                offset += 1
                if marker == 0xDA:
                    saw_scan = True
                    break
                if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                    continue
                if offset + 2 > len(data):
                    raise ValueError("truncated JPEG segment")
                length = int.from_bytes(data[offset : offset + 2], "big")
                if length < 2 or offset + length > len(data):
                    raise ValueError("invalid JPEG segment length")
                if marker in {
                    0xC0,
                    0xC1,
                    0xC2,
                    0xC3,
                    0xC5,
                    0xC6,
                    0xC7,
                    0xC9,
                    0xCA,
                    0xCB,
                    0xCD,
                    0xCE,
                    0xCF,
                }:
                    if length < 7:
                        raise ValueError("invalid JPEG frame header")
                    height = int.from_bytes(data[offset + 3 : offset + 5], "big")
                    width = int.from_bytes(data[offset + 5 : offset + 7], "big")
                offset += length
            if not saw_scan or width <= 0 or height <= 0:
                raise ValueError("JPEG has no readable frame and scan structure")
            image_format = "JPEG"
    except Exception as exc:
        record["reason"] = f"image is unreadable: {type(exc).__name__}: {exc}"
        return record

    ratio = height / width if width else 0.0
    record.update(
        {
            "format": image_format,
            "width": width,
            "height": height,
            "height_width_ratio": ratio,
        }
    )
    if width <= 0 or height <= 0:
        record["reason"] = "image dimensions are not positive"
    elif ratio < minimum_height_width_ratio:
        record["reason"] = (
            "image aspect is incompatible with the release mapping figure and "
            "matches a legend/key-only strip"
        )
    else:
        record["passed"] = True
        record["reason"] = (
            "readable composed map with spatial-panel-compatible aspect"
        )
    return record


def _validate_pdf(
    path: Path, *, minimum_height_width_ratio: float = 0.5
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "path": str(path),
        "minimum_height_width_ratio": minimum_height_width_ratio,
        "passed": False,
    }
    if not path.is_file():
        record["reason"] = "required composed PDF is missing"
        return record
    try:
        data = path.read_bytes()
    except OSError as exc:
        record["reason"] = f"PDF is unreadable: {exc}"
        return record
    if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-1024:]:
        record["reason"] = "PDF header or end marker is invalid"
        return record
    number = rb"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
    media_box = re.search(
        rb"/MediaBox\s*\[\s*("
        + number
        + rb")\s+("
        + number
        + rb")\s+("
        + number
        + rb")\s+("
        + number
        + rb")\s*\]",
        data,
    )
    if media_box is None:
        record["reason"] = "PDF has no readable page MediaBox"
        return record
    x0, y0, x1, y1 = (float(value) for value in media_box.groups())
    width = x1 - x0
    height = y1 - y0
    ratio = height / width if width else 0.0
    record.update(
        {
            "size": len(data),
            "page_width": width,
            "page_height": height,
            "height_width_ratio": ratio,
        }
    )
    if width <= 0 or height <= 0:
        record["reason"] = "PDF page dimensions are not positive"
    elif ratio < minimum_height_width_ratio:
        record["reason"] = (
            "PDF page aspect is incompatible with the release mapping figure "
            "and matches a legend/key-only strip"
        )
    else:
        record.update(
            {
                "passed": True,
                "reason": (
                    "readable composed map PDF with "
                    "spatial-panel-compatible aspect"
                ),
            }
        )
    return record


def validate_native_products(
    products: Path, expected_event_id: str
) -> dict[str, Any]:
    """Read and operationally validate native structured and map products."""
    from esi_utils_io.smcontainers import ShakeMapOutputContainer
    from esi_shakelib.utils.imt_string import oq_to_file
    from PIL import Image

    evidence: dict[str, Any] = {
        "schema_version": 1,
        "products_path": str(products),
        "expected_event_id": expected_event_id,
        "passed": False,
        "checks": {},
        "errors": [],
    }
    hdf_path = products / "shake_result.hdf"
    container = None
    try:
        container = ShakeMapOutputContainer.load(str(hdf_path))
        metadata = container.getMetadata()
        hdf_event_id = str(metadata["input"]["event_information"]["event_id"])
        if hdf_event_id != expected_event_id:
            raise ValueError(
                f"HDF event_id {hdf_event_id!r} != {expected_event_id!r}"
            )
        if container.getDataType() != "grid":
            raise ValueError(
                f"HDF data type is {container.getDataType()!r}, expected 'grid'"
            )
        imts = sorted({item.split("/", 1)[1] for item in container.getIMTs()})
        if "MMI" not in imts:
            raise ValueError("HDF has no MMI grid required by the mapping plan")
        component = container.getComponents("MMI")[0]
        mmi_metadata = container.getIMTGrids("MMI", component)["mean_metadata"]
        expected_overlay_size = (
            int(mmi_metadata["nx"]),
            int(mmi_metadata["ny"]),
        )
        evidence["checks"]["shake_result_hdf"] = {
            "passed": True,
            "path": str(hdf_path),
            "data_type": "grid",
            "event_id": hdf_event_id,
            "imts": imts,
            "mmi_grid_size": list(expected_overlay_size),
        }
    except Exception as exc:
        evidence["checks"]["shake_result_hdf"] = {
            "passed": False,
            "path": str(hdf_path),
            "reason": f"{type(exc).__name__}: {exc}",
        }
        evidence["errors"].append(
            f"shake_result.hdf: {type(exc).__name__}: {exc}"
        )
        imts = []
        expected_overlay_size = None
    finally:
        if container is not None:
            container.close()

    stems = ["intensity" if imt == "MMI" else oq_to_file(imt) for imt in imts]
    image_checks = {
        stem: validate_composed_image(products / f"{stem}.jpg")
        for stem in stems
    }
    evidence["checks"]["composed_images"] = image_checks
    for stem, check in image_checks.items():
        if not check["passed"]:
            evidence["errors"].append(f"{stem}.jpg: {check['reason']}")

    pdf_checks = {
        stem: _validate_pdf(products / f"{stem}.pdf") for stem in stems
    }
    evidence["checks"]["composed_pdfs"] = pdf_checks
    for stem, check in pdf_checks.items():
        if not check["passed"]:
            evidence["errors"].append(f"{stem}.pdf: {check['reason']}")

    overlay = products / "intensity_overlay.png"
    overlay_check: dict[str, Any] = {"path": str(overlay), "passed": False}
    try:
        with Image.open(overlay) as image:
            image.verify()
        with Image.open(overlay) as image:
            overlay_size = image.size
            image.load()
        overlay_check.update({"size": list(overlay_size)})
        if expected_overlay_size is None or overlay_size != expected_overlay_size:
            raise ValueError(
                f"overlay size {overlay_size} does not match HDF MMI grid "
                f"{expected_overlay_size}"
            )
        overlay_check.update(
            {"passed": True, "reason": "readable spatial overlay matches HDF grid"}
        )
    except Exception as exc:
        overlay_check["reason"] = f"{type(exc).__name__}: {exc}"
        evidence["errors"].append(
            f"intensity_overlay.png: {overlay_check['reason']}"
        )
    evidence["checks"]["spatial_overlay"] = overlay_check

    json_checks: dict[str, Any] = {}
    json_paths = [products / "stationlist.json"] + [
        products / f"cont_{oq_to_file(imt)}.json" for imt in imts
    ]
    for path in json_paths:
        check: dict[str, Any] = {"path": str(path), "passed": False}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("type") != "FeatureCollection" or not isinstance(
                value.get("features"), list
            ):
                raise ValueError("expected a GeoJSON FeatureCollection")
            exposed_event = value.get("metadata", {}).get("eventid")
            if exposed_event is not None and exposed_event != expected_event_id:
                raise ValueError(
                    f"eventid {exposed_event!r} != {expected_event_id!r}"
                )
            check.update(
                {
                    "passed": True,
                    "feature_count": len(value["features"]),
                    "event_id": exposed_event,
                }
            )
        except Exception as exc:
            check["reason"] = f"{type(exc).__name__}: {exc}"
            evidence["errors"].append(f"{path.name}: {check['reason']}")
        json_checks[path.name] = check
    evidence["checks"]["structured_json"] = json_checks

    xml_checks: dict[str, Any] = {}
    for name in ("grid.xml", "uncertainty.xml"):
        path = products / name
        check = {"path": str(path), "passed": False}
        try:
            root = ET.parse(path).getroot()
            event_id = root.attrib.get("event_id") or root.attrib.get(
                "shakemap_id"
            )
            if event_id != expected_event_id:
                raise ValueError(
                    f"event_id {event_id!r} != {expected_event_id!r}"
                )
            check.update(
                {"passed": True, "event_id": event_id, "root": root.tag}
            )
        except Exception as exc:
            check["reason"] = f"{type(exc).__name__}: {exc}"
            evidence["errors"].append(f"{name}: {check['reason']}")
        xml_checks[name] = check
    evidence["checks"]["structured_xml"] = xml_checks

    evidence["required_composed_outputs"] = stems
    evidence["passed"] = not evidence["errors"] and bool(stems)
    return evidence


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    default_data = PROJECT_ROOT / "runtime/shakemap/data"

    inspect = commands.add_parser("inspect")
    inspect.add_argument("--data-root", type=Path, default=default_data)

    validate = commands.add_parser("validate-pinned-global")
    validate.add_argument("--data-root", type=Path, default=default_data)

    provision = commands.add_parser("provision-global")
    provision.add_argument("--data-root", type=Path, default=default_data)
    provision.add_argument("--vs30-source", type=Path)
    provision.add_argument("--topo-source", type=Path)
    provision.add_argument("--slab-source", type=Path)
    provision.add_argument("--no-download", action="store_true")
    return root


def main(argv: Iterable[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "inspect":
            value = inspect_data_assets(args.data_root)
            result = 0
        elif args.command == "validate-pinned-global":
            value = validate_pinned_global_assets(args.data_root)
            result = 0 if value["pinned_integrity_valid"] else 1
        elif args.command == "provision-global":
            value = provision_global_data(
                args.data_root,
                vs30_source=args.vs30_source,
                topo_source=args.topo_source,
                slab_source=args.slab_source,
                allow_download=not args.no_download,
            )
            result = 0
        else:
            return 2
        print(json.dumps(value, indent=2, sort_keys=True))
        return result
    except (OSError, DataProvisioningError, zipfile.BadZipFile) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
