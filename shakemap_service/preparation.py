# -*- coding: utf-8 -*-
"""Host and container implementation of durable pre-start runtime preparation."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
import urllib.request
import uuid
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


MODULE_PLAN = ["select", "assemble", "model", "contour", "mapping", "stations", "gridxml"]
IMAGE_STREC_DB = "/opt/shakemap-support/strec/moment_tensors.db"
IMAGE_CARTOPY_DIR = "/opt/shakemap-support/cartopy"
CONTAINER_RUNTIME = Path("/home/sysop/runtime")
CONTAINER_SERVICE = CONTAINER_RUNTIME / "shakemap"
PROJECT_ROOT = Path(__file__).resolve().parents[1]

GLOBAL_ASSETS = {
    "vs30": {
        "relative": "shakemap/data/vs30/global_vs30.grd",
        "url": "https://apps.usgs.gov/shakemap_geodata/vs30/global_vs30.grd",
        "size": 610189275,
        "sha256": "b07944c5be332c5a261777d23b3390fe8d5638f25b388b82f5dc1e98c6356011",
        "checksum_authority": "project-verified download pin; USGS publishes no checksum alongside the file",
        "bounds": [-180.00416666666666, -56.00416666666666, 180.00416666666666, 84.00416666666666],
    },
    "topography": {
        "relative": "shakemap/data/topo/topo_30sec.grd",
        "url": "https://apps.usgs.gov/shakemap_geodata/topo/topo_30sec.grd",
        "size": 249661705,
        "sha256": "3aa02a77d56d656deae9bf4539afdb3ce1dd1b7057a67a5c7bdd0573fc97bd4c",
        "checksum_authority": "project-verified download pin; USGS publishes no checksum alongside the file",
        "bounds": [-180.000138888889, -90.000138888889, 179.99985967111104, 83.999860415111],
    },
}
SLAB2 = {
    "url": "https://apps.usgs.gov/shakemap_geodata/slabs/slab2.zip",
    "size": 12028579,
    "sha256": "2258004fd3d8467e894a1bdb3cd4224a40bd3c876b4ec2e35617f265c7047360",
    "checksum_authority": "project-verified download pin; USGS publishes no checksum alongside the file",
    "file_count": 108,
}


class PreparationError(RuntimeError):
    """Raised when preparation cannot produce validated durable state."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_text = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
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


def file_record(path: Path, *, source: dict[str, Any] | None = None) -> dict[str, Any]:
    record = {"path": str(path), "size": path.stat().st_size, "sha256": sha256(path)}
    if source:
        record.update({
            "source_url": source["url"],
            "checksum_authority": source["checksum_authority"],
            "expected_size": source["size"],
            "expected_sha256": source["sha256"],
        })
    return record


def validate_pinned_file(path: Path, spec: dict[str, Any]) -> tuple[bool, str]:
    if not path.is_file():
        return False, "missing"
    if path.stat().st_size != spec["size"]:
        return False, f"size {path.stat().st_size} != {spec['size']}"
    with path.open("rb") as stream:
        signature = stream.read(8)
    if signature != b"\x89HDF\r\n\x1a\n":
        return False, "not an HDF5/netCDF4 grid"
    actual = sha256(path)
    if actual != spec["sha256"]:
        return False, f"SHA-256 {actual} != {spec['sha256']}"
    return True, "valid pinned file"


def download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "shakemap-docker-runtime-preparation/1"})
    with urllib.request.urlopen(request, timeout=180) as response:
        with destination.open("wb") as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)


def provision_file(
    target: Path,
    spec: dict[str, Any],
    source: Path | None,
    allow_download: bool,
) -> dict[str, Any]:
    valid, reason = validate_pinned_file(target, spec)
    if valid:
        return {"action": "reused", "validation": reason, **file_record(target, source=spec)}
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.prepare-{uuid.uuid4().hex}")
    try:
        if source is not None:
            if not source.is_file():
                raise PreparationError(f"manual source is missing: {source}")
            shutil.copyfile(source, temporary)
            action = "imported"
        elif allow_download:
            download(spec["url"], temporary)
            action = "downloaded"
        else:
            raise PreparationError(f"{target} is {reason}; download disabled and no manual source supplied")
        replacement_valid, replacement_reason = validate_pinned_file(temporary, spec)
        if not replacement_valid:
            raise PreparationError(f"replacement for {target} is invalid: {replacement_reason}")
        preserved = None
        if target.exists():
            preserved = target.with_name(f"{target.name}.invalid-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
            if preserved.exists():
                preserved = target.with_name(f"{preserved.name}-{uuid.uuid4().hex[:8]}")
            os.replace(target, preserved)
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


def validate_slab_directory(root: Path) -> tuple[bool, str, dict[str, Any] | None]:
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
            path = root / record["path"]
            if not path.is_file() or path.stat().st_size != record["size"] or sha256(path) != record["sha256"]:
                return False, f"slab file invalid: {record['path']}", manifest
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return False, f"slab manifest unreadable: {exc}", None
    return True, "valid extracted Slab2 package", manifest


def provision_slabs(runtime: Path, source: Path | None, allow_download: bool) -> dict[str, Any]:
    strec_root = runtime / "shakemap/data/global/strec"
    target = strec_root / "slabs"
    valid, reason, manifest = validate_slab_directory(target)
    if valid:
        return {"action": "reused", "validation": reason, "manifest": manifest}
    strec_root.mkdir(parents=True, exist_ok=True)
    archive = strec_root / f".slab2-{uuid.uuid4().hex}.zip"
    temporary = strec_root / f".slabs-prepare-{uuid.uuid4().hex}"
    try:
        if source:
            if not source.is_file():
                raise PreparationError(f"manual Slab2 archive is missing: {source}")
            shutil.copyfile(source, archive)
            action = "imported"
        elif allow_download:
            download(SLAB2["url"], archive)
            action = "downloaded"
        else:
            raise PreparationError(f"Slab2 is {reason}; download disabled and no manual archive supplied")
        if archive.stat().st_size != SLAB2["size"] or sha256(archive) != SLAB2["sha256"]:
            raise PreparationError("Slab2 archive does not match the pinned size and SHA-256")
        temporary.mkdir()
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
            if len(names) != SLAB2["file_count"] or any(Path(name).name != name for name in names):
                raise PreparationError("Slab2 archive layout is unexpected or unsafe")
            bundle.extractall(temporary)
        records = [
            {"path": path.name, "size": path.stat().st_size, "sha256": sha256(path)}
            for path in sorted(temporary.iterdir()) if path.is_file()
        ]
        new_manifest = {
            "schema_version": 1,
            "prepared_at_utc": utc_now(),
            "source": SLAB2,
            "files": records,
        }
        manifest_temp = strec_root / f".slab2-manifest-{uuid.uuid4().hex}.json"
        atomic_json(manifest_temp, new_manifest)
        preserved = None
        if target.exists():
            preserved = strec_root / f"slabs.invalid-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
            os.replace(target, preserved)
        os.replace(temporary, target)
        os.replace(manifest_temp, strec_root / "slab2-manifest.json")
        return {"action": action, "previous_validation": reason, "preserved_invalid_path": str(preserved) if preserved else None, "manifest": new_manifest}
    finally:
        archive.unlink(missing_ok=True)
        if temporary.exists():
            shutil.rmtree(temporary)


def ensure_host_permissions(runtime: Path) -> dict[str, Any]:
    required = [
        "shakemap/incoming", "shakemap/products", "shakemap/logs",
        "shakemap/data", "shakemap/.service/events", "shakemap/.service/work",
        "shakemap/.service/archive", "shakemap/.service/preparation",
    ]
    checks = []
    for relative in required:
        directory = runtime / relative
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".permission-{uuid.uuid4().hex}"
        try:
            probe.write_text("permission probe\n", encoding="utf-8")
            checks.append({"path": str(directory), "writable": True})
        except OSError as exc:
            raise PreparationError(f"host path is not writable: {directory}: {exc}") from exc
        finally:
            probe.unlink(missing_ok=True)
    return {"checks": checks}


def docker_prepare(runtime: Path, image: str, fixture: Path, package: Path) -> dict[str, Any]:
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:10]}"
    name = f"shakemap-prepare-{uuid.uuid4().hex[:12]}"
    command = [
        "docker", "run", "--rm", "--name", name, "--network", "none",
        "-v", f"{runtime.resolve()}:{CONTAINER_RUNTIME}",
        "-v", f"{(runtime / 'shakemap/data').resolve()}:{CONTAINER_SERVICE / 'data'}:ro",
        "-v", f"{fixture.resolve()}:/verification/request:ro",
        "-v", f"{package.resolve()}:/verification/california:ro",
        "--entrypoint", "python", image, "-m", "shakemap_service.preparation",
        "container-prepare", "--run-id", run_id,
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    attempts = runtime / "shakemap/.service/preparation/attempts"
    attempts.mkdir(parents=True, exist_ok=True)
    (attempts / f"{run_id}.host.stdout.log").write_text(result.stdout, encoding="utf-8")
    (attempts / f"{run_id}.host.stderr.log").write_text(result.stderr, encoding="utf-8")
    attempt_path = attempts / f"{run_id}.json"
    if not attempt_path.is_file():
        raise PreparationError(f"preparation container exited {result.returncode} without a durable attempt record: {result.stderr.strip()}")
    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    if result.returncode != 0 or not attempt.get("ready"):
        raise PreparationError(f"preparation container failed; evidence: {attempt_path}")
    return attempt


def host_prepare(args: argparse.Namespace) -> int:
    runtime = args.runtime.resolve()
    host_permissions = ensure_host_permissions(runtime)
    assets = {}
    manual = {"vs30": args.vs30_source, "topography": args.topo_source}
    for name, spec in GLOBAL_ASSETS.items():
        target = runtime / spec["relative"]
        assets[name] = provision_file(target, spec, manual[name], not args.no_download)
    slabs = provision_slabs(runtime, args.slab_source, not args.no_download)
    attempt = docker_prepare(runtime, args.image, args.fixture, args.california_package)
    print(json.dumps({"host_permissions": host_permissions, "global_assets": assets, "slabs": slabs, "preparation": attempt}, indent=2, sort_keys=True))
    return 0


def inventory(root: Path) -> list[dict[str, Any]]:
    return [
        {"path": path.relative_to(root).as_posix(), "size": path.stat().st_size, "sha256": sha256(path)}
        for path in sorted(root.rglob("*")) if path.is_file()
    ]


def patch_line(path: Path, key: str, value: str) -> None:
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(rf"^(\s*{re.escape(key)}\s*=).*$", re.MULTILINE)
    replaced, count = pattern.subn(rf"\1 {value}", text, count=1)
    if count != 1:
        raise PreparationError(f"could not set {key} in {path}")
    path.write_text(replaced, encoding="utf-8")


def write_strec_config(path: Path, slab_dir: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[DATA]\n"
        f"folder = {Path(IMAGE_STREC_DB).parent}\n"
        f"slabfolder = {slab_dir}\n"
        f"dbfile = {IMAGE_STREC_DB}\n"
        "longest_axis = 3556.9858168964675\n\n"
        "[CONSTANTS]\nminradial_disthist = 0.01\nmaxradial_disthist = 1.0\n"
        "minradial_distcomp = 0.5\nmaxradial_distcomp = 1.0\nstep_distcomp = 0.1\n"
        "depth_rangecomp = 10\nminno_comp = 3\ndefault_szdip = 17\n"
        "dstrike_interf = 30\nddip_interf = 30\ndlambda = 60\n"
        "ddepth_interf = 20\nddepth_intra = 10\n",
        encoding="utf-8",
    )


def parse_module_order(log: str) -> tuple[list[str], list[str]]:
    separator = r"(?:;| -- )"
    running = re.findall(rf"shake\.main{separator}Running command ([A-Za-z0-9_-]+)", log)
    completed = re.findall(rf"shake\.main{separator}Finished running command ([A-Za-z0-9_-]+):", log)
    return running, completed


def create_profile(home: Path, name: str) -> Path:
    env = {**os.environ, "HOME": str(home), "XDG_CACHE_HOME": str(home / ".cache"), "MPLCONFIGDIR": str(home / ".config/matplotlib")}
    home.mkdir(parents=True, exist_ok=True)
    subprocess.run(["sm_profile", "-c", name, "-a", "-n"], env=env, check=True, text=True, capture_output=True)
    subprocess.run(["shake", "init"], env=env, check=True, text=True, capture_output=True)
    return home / "shakemap_profiles" / name


@contextlib.contextmanager
def profile_environment(home: Path):
    values = {
        "HOME": str(home),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "MPLCONFIGDIR": str(home / ".config/matplotlib"),
    }
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def validate_native_config(config_dir: Path, profile_home: Path) -> dict[str, Any]:
    from configobj import ConfigObj
    from shakemap_modules.utils.config import get_configspec, get_custom_validator
    from shakemap_modules.utils.layers import validate_config as validate_select_config

    checked = []
    with profile_environment(profile_home):
        for name in ("logging", "model", "modules", "products", "select", "shake"):
            spec = get_configspec(name)
            config = ConfigObj(str(config_dir / f"{name}.conf"), configspec=str(spec), interpolation=False)
            if name == "select":
                validate_select_config(
                    config,
                    str(config_dir.parent),
                    str(profile_home / "shakemap_profiles/global-base/data"),
                    str(profile_home / "shakemap_data"),
                )
            else:
                result = config.validate(get_custom_validator(), preserve_errors=True)
                if result is not True:
                    raise PreparationError(f"native schema validation failed for {name}.conf: {result}")
            checked.append(name)
    ConfigObj(str(config_dir / "gmpe_sets.conf"), interpolation=False)
    checked.append("gmpe_sets")
    return {"validated_configs": checked}


def validate_grids() -> dict[str, Any]:
    import rasterio

    results = {}
    for name, spec in GLOBAL_ASSETS.items():
        path = CONTAINER_RUNTIME / spec["relative"]
        with rasterio.open(path) as dataset:
            bounds = list(dataset.bounds)
            results[name] = {
                "path": str(path), "driver": dataset.driver, "width": dataset.width,
                "height": dataset.height, "dtype": list(dataset.dtypes), "bounds": bounds,
            }
        if any(abs(actual - expected) > 1e-6 for actual, expected in zip(bounds, spec["bounds"])):
            raise PreparationError(f"{name} grid bounds differ from the validated release input")
    event = ET.parse(Path("/verification/request") / "event.xml").getroot()
    lon, lat = float(event.attrib["lon"]), float(event.attrib["lat"])
    for name, result in results.items():
        xmin, ymin, xmax, ymax = result["bounds"]
        if not (xmin <= lon <= xmax and ymin <= lat <= ymax):
            raise PreparationError(f"SCENARIO lies outside {name} grid")
    results["scenario"] = {"longitude": lon, "latitude": lat, "covered": True}
    return results


def validate_slabs_native() -> dict[str, Any]:
    import rasterio

    root = CONTAINER_SERVICE / "data/global/strec/slabs"
    files = sorted(root.glob("*.grd"))
    if len(files) != SLAB2["file_count"]:
        raise PreparationError(f"expected {SLAB2['file_count']} Slab2 grids, found {len(files)}")
    groups: dict[str, set[str]] = {}
    for path in files:
        parts = path.name.split("_")
        if len(parts) < 4:
            raise PreparationError(f"unexpected Slab2 filename: {path.name}")
        groups.setdefault(parts[0], set()).add(parts[2])
        with rasterio.open(path) as dataset:
            if dataset.width <= 0 or dataset.height <= 0 or dataset.count != 1:
                raise PreparationError(f"unreadable Slab2 grid dimensions: {path}")
    incomplete = {name: sorted(values) for name, values in groups.items() if values != {"dep", "dip", "str", "unc"}}
    if incomplete:
        raise PreparationError(f"incomplete Slab2 region groups: {incomplete}")
    return {"path": str(root), "file_count": len(files), "region_count": len(groups), "all_grids_readable": True}


def prepare_base(attempt_root: Path) -> tuple[Path, dict[str, Any]]:
    profile = create_profile(attempt_root / "base-home", "global-base")
    install = profile / "install"
    patch_line(install / "config/model.conf", "vs30file", str(CONTAINER_SERVICE / "data/vs30/global_vs30.grd"))
    patch_line(install / "config/products.conf", "topography", str(CONTAINER_SERVICE / "data/topo/topo_30sec.grd"))
    snapshot = attempt_root / "global-base"
    (snapshot / "install/data").mkdir(parents=True)
    shutil.copytree(install / "config", snapshot / "install/config")
    shutil.copytree(install / "data/layers", snapshot / "install/data/layers")
    write_strec_config(snapshot / "strec/config.ini", str(CONTAINER_SERVICE / "data/global/strec/slabs"))
    profiles = "profile = global-base\n\n[profiles]\n    [[global-base]]\n        install_path = <PRIVATE_INSTALL>\n        data_path = <PRIVATE_DATA>\n"
    (snapshot / "profiles.conf.template").write_text(profiles, encoding="utf-8")
    validation = validate_native_config(snapshot / "install/config", attempt_root / "base-home")
    return snapshot, validation


def run_scenario(attempt_root: Path, kind: str, source_install: Path, scientific_links: dict[str, str], slab_dir: str) -> dict[str, Any]:
    run_root = attempt_root / "native" / kind
    install = run_root / "install"
    home = run_root / "home"
    events = run_root / "events"
    current = events / "SCENARIO/current"
    shutil.copytree(source_install / "config", install / "config")
    (install / "data").mkdir(parents=True)
    if (source_install / "data/layers").is_dir():
        shutil.copytree(source_install / "data/layers", install / "data/layers")
    for name, target in scientific_links.items():
        link = install / "data" / name
        if link.exists():
            shutil.rmtree(link)
        link.symlink_to(target)
    (install / "logs").mkdir()
    current.mkdir(parents=True)
    for name in ("event.xml", "event_dat.xml"):
        shutil.copyfile(Path("/verification/request") / name, current / name)
    (home / ".shakemap").mkdir(parents=True)
    (home / ".strec").mkdir(parents=True)
    slab_path = Path(slab_dir)
    if str(slab_path).startswith(str(run_root)):
        slab_path.mkdir(parents=True, exist_ok=True)
    (home / ".config/matplotlib").mkdir(parents=True)
    (run_root / "cache").mkdir()
    (run_root / "tmp").mkdir()
    (home / ".shakemap/profiles.conf").write_text(
        f"profile = {kind}\n\n[profiles]\n    [[{kind}]]\n        install_path = {install}\n        data_path = {events}\n",
        encoding="utf-8",
    )
    write_strec_config(home / ".strec/config.ini", slab_dir)
    command = ["shake", "SCENARIO", "select", "assemble", "-c", f"{kind} preparation verification", "model", "contour", "mapping", "stations", "gridxml"]
    env = {**os.environ, "HOME": str(home), "CARTOPY_DATA_DIR": IMAGE_CARTOPY_DIR, "XDG_CACHE_HOME": str(run_root / "cache"), "MPLCONFIGDIR": str(home / ".config/matplotlib"), "TMPDIR": str(run_root / "tmp")}
    started = utc_now()
    result = subprocess.run(command, env=env, text=True, capture_output=True, check=False)
    finished = utc_now()
    (run_root / "stdout.log").write_text(result.stdout, encoding="utf-8")
    (run_root / "stderr.log").write_text(result.stderr, encoding="utf-8")
    running, completed = parse_module_order(result.stderr)
    evidence = {
        "kind": kind, "started_at_utc": started, "finished_at_utc": finished,
        "command": command, "exit_code": result.returncode, "module_plan": MODULE_PLAN,
        "observed_running_order": running, "observed_finished_order": completed,
        "module_plan_completed": result.returncode == 0 and running == MODULE_PLAN and completed == MODULE_PLAN,
        "configuration_inventory": inventory(install / "config"),
        "output_inventory": inventory(current),
        "network": "none", "cartopy_data_dir": IMAGE_CARTOPY_DIR,
        "strec_database": IMAGE_STREC_DB, "slab_directory": slab_dir,
    }
    atomic_json(run_root / "evidence.json", evidence)
    if not evidence["module_plan_completed"]:
        raise PreparationError(f"{kind} native default plan failed; see {run_root}")
    return evidence


def container_permissions() -> dict[str, Any]:
    checks = {"uid": os.getuid(), "gid": os.getgid(), "writable": [], "scientific_data_read_only": False}
    if os.getuid() != 1000 or os.getgid() != 1000:
        raise PreparationError(f"preparation container must run as 1000:1000, got {os.getuid()}:{os.getgid()}")
    for relative in ("incoming", "products", "logs", ".service/events", ".service/work", ".service/archive", ".service/preparation"):
        directory = CONTAINER_SERVICE / relative
        probe = directory / f".container-permission-{uuid.uuid4().hex}"
        probe.write_text("probe\n", encoding="utf-8")
        probe.unlink()
        checks["writable"].append(str(directory))
    try:
        with (CONTAINER_SERVICE / "data/vs30/global_vs30.grd").open("ab"):
            pass
    except OSError:
        checks["scientific_data_read_only"] = True
    if not checks["scientific_data_read_only"]:
        raise PreparationError("scientific data mount is writable inside preparation container")
    return checks


def container_prepare(run_id: str) -> int:
    prep_root = CONTAINER_SERVICE / ".service/preparation"
    attempts = prep_root / "attempts"
    attempts.mkdir(parents=True, exist_ok=True)
    attempt_root = prep_root / "logs" / run_id
    attempt_root.mkdir(parents=True, exist_ok=False)
    record: dict[str, Any] = {"schema_version": 1, "run_id": run_id, "started_at_utc": utc_now(), "ready": False}
    try:
        from shakemap_service.build_identity import load_build_identity
        identity = load_build_identity()
        if not identity.get("immutable_image", {}).get("available"):
            raise PreparationError("immutable image identity is unavailable")
        record["identity"] = identity["immutable_image"]
        record["permissions"] = container_permissions()
        record["grids"] = validate_grids()
        record["strec_slabs"] = validate_slabs_native()
        snapshot, config_validation = prepare_base(attempt_root)
        record["configuration_validation"] = config_validation
        record["california_verification"] = run_scenario(
            attempt_root, "california", Path("/verification/california"),
            {"layers": "/verification/california/data/layers", "mapping": "/verification/california/data/mapping", "vs30": "/verification/california/data/vs30"},
            str(attempt_root / "native/california/home/.strec/slabs"),
        )
        record["global_verification"] = run_scenario(
            attempt_root, "global", snapshot / "install", {},
            str(CONTAINER_SERVICE / "data/global/strec/slabs"),
        )
        record["base_inventory"] = inventory(snapshot)
        record["ready"] = True
        record["finished_at_utc"] = utc_now()

        base = prep_root / "base/global"
        base.parent.mkdir(parents=True, exist_ok=True)
        if base.exists():
            history = prep_root / "history" / f"global-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
            history.parent.mkdir(parents=True, exist_ok=True)
            os.replace(base, history)
            record["previous_base_preserved_at"] = str(history)
        os.replace(snapshot, base)
        record["base_path"] = str(base)
        atomic_json(attempts / f"{run_id}.json", record)
        atomic_json(prep_root / "manifest.json", record)
        report = (
            "# ShakeMap runtime preparation report\n\n"
            f"Prepared: {record['finished_at_utc']}\n\n"
            f"Ready for the fixed preparation scenarios: yes\n\n"
            f"Image: {record['identity']['upstream']['release_tag']} at {record['identity']['upstream']['source_commit']}\n\n"
            f"Base snapshot: `{record['base_path']}`\n\n"
            "The California and prepared-global default plans completed offline. This does not prove queue, REST submission, concurrency, recalculation archival, authoritative service SUCCESS, or universal scientific validity.\n"
        )
        (prep_root / "report.md").write_text(report, encoding="utf-8")
        return 0
    except Exception as exc:
        record["finished_at_utc"] = utc_now()
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["traceback"] = traceback.format_exc()
        atomic_json(attempts / f"{run_id}.json", record)
        print(record["error"], file=sys.stderr)
        return 1


def load_preparation(service_root: Path) -> dict[str, Any]:
    path = service_root / ".service/preparation/manifest.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"available": False, "ready": False, "manifest_path": str(path), "reason": f"durable preparation record unavailable: {exc}"}
    required = ("schema_version", "run_id", "identity", "permissions", "grids", "base_path", "ready")
    missing = [field for field in required if field not in record]
    if missing or record.get("schema_version") != 1 or record.get("ready") is not True:
        return {"available": True, "ready": False, "manifest_path": str(path), "reason": f"invalid preparation record; missing={missing}", "record": record}
    base = service_root / ".service/preparation/base/global"
    if not base.is_dir():
        return {"available": True, "ready": False, "manifest_path": str(path), "reason": f"base snapshot is missing: {base}", "record": record}
    try:
        from shakemap_service.build_identity import load_build_identity
        current = load_build_identity().get("immutable_image", {})
        prepared = record.get("identity", {})
        if current.get("available") and current != prepared:
            return {"available": True, "ready": False, "manifest_path": str(path), "reason": "preparation identity does not match the running image", "record": record}
    except Exception:
        pass
    return {"available": True, "ready": True, "manifest_path": str(path), "record": record}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--runtime", type=Path, default=PROJECT_ROOT / "runtime")
    prepare.add_argument("--image", default="shakemap-docker:latest")
    prepare.add_argument("--fixture", type=Path, default=PROJECT_ROOT / "tests/fixtures/shakemap_scenario")
    prepare.add_argument("--california-package", type=Path, default=PROJECT_ROOT / "runtime/shakemap/data/test/v4.4.9")
    prepare.add_argument("--vs30-source", type=Path)
    prepare.add_argument("--topo-source", type=Path)
    prepare.add_argument("--slab-source", type=Path)
    prepare.add_argument("--no-download", action="store_true")
    internal = commands.add_parser("container-prepare")
    internal.add_argument("--run-id", required=True)
    validate = commands.add_parser("validate-record")
    validate.add_argument("--service-root", type=Path, default=CONTAINER_SERVICE)
    return root


def main(argv: Iterable[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "prepare":
            return host_prepare(args)
        if args.command == "container-prepare":
            return container_prepare(args.run_id)
        if args.command == "validate-record":
            value = load_preparation(args.service_root)
            print(json.dumps(value, indent=2, sort_keys=True))
            return 0 if value["ready"] else 1
    except (OSError, PreparationError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
