# -*- coding: utf-8 -*-
"""ShakeMap CLI invocation and execution bridge.

Phase 01-06: ``run_shake()`` — low-level CLI wrapper.
Phase 07:    ``run_shake_for_event()`` — full execution bridge.

The execution bridge:
1. Validates incoming files exist.
2. Copies files from ``incoming/<event_id>/`` to ShakeMap's expected
   ``<profile>/data/<event_id>/current/`` directory.
3. Invokes ``shake`` with configured modules (stdout/stderr → log file).
4. On success: validates products, writes manifest + provenance,
   publishes atomically, creates audit copy, transitions to SUCCESS.
5. On failure: captures error, transitions to FAILED.

Responsibility boundary:
- Worker owns QUEUED -> RUNNING (claim locking).
- Runner owns RUNNING -> SUCCESS/FAILED (this module).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import subprocess

from . import paths
from .config import settings
from .status import (
    RequestStatus,
    read_status,
    transition_to_failed,
    transition_to_success,
    write_status_atomic,
)

logger = logging.getLogger(__name__)

# Required core products: at least one must exist for SUCCESS.
_REQUIRED_CORE_PRODUCTS = ("grid.xml", "shake_result.hdf")


class ShakeError(RuntimeError):
    """Raised when the 'shake' CLI fails."""
    pass


def run_shake(
    event_id: str,
    modules: Sequence[str] | None = None,
    force: bool = False,
    log_file: Path | None = None,
) -> list[str]:
    """
    Build and run the 'shake' command for a given event_id.

    If ``log_file`` is provided, stdout and stderr are captured to that file
    so operators can diagnose runs from the shared volume.

    Returns the command list that was executed.
    Raises ShakeError on failure.
    """
    cmd: list[str] = ["shake"]

    if force:
        cmd.append("--force")

    cmd.append(event_id)

    if modules:
        cmd.extend(modules)

    try:
        if log_file:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "w") as fh:
                subprocess.run(cmd, check=True, stdout=fh, stderr=subprocess.STDOUT)
        else:
            subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        raise ShakeError(f"'shake' failed with exit code {exc.returncode}") from exc

    return cmd


# ------------------------------------------------------------------
# Product validation
# ------------------------------------------------------------------

def _validate_products(products_dir: Path) -> tuple[bool, str]:
    """Validate that required core products exist and are non-empty.

    Returns:
        (valid, reason) — True if at least one core product exists and
        all found files are non-empty.
    """
    if not products_dir.is_dir():
        return False, "Products directory does not exist"

    all_files = [f for f in products_dir.iterdir() if f.is_file()]
    if not all_files:
        return False, "Products directory is empty"

    # Check for at least one required core product
    found_core = [
        f.name for f in all_files if f.name in _REQUIRED_CORE_PRODUCTS
    ]
    if not found_core:
        expected = " or ".join(_REQUIRED_CORE_PRODUCTS)
        return False, f"No required core product found (need {expected})"

    # Check all product files are non-empty
    empty_files = [f.name for f in all_files if f.stat().st_size == 0]
    if empty_files:
        return False, f"Empty product files found: {', '.join(empty_files)}"

    return True, ""


# ------------------------------------------------------------------
# Manifest and provenance
# ------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    """Compute SHA-256 hex digest for a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_products_manifest(
    event_id: str,
    products_path: Path,
    modules: list[str],
    valid: bool,
    failure_reason: str,
) -> Path:
    """Write products-manifest.json to the products directory.

    Returns the path to the manifest file.
    """
    all_files = sorted(
        (f for f in products_path.iterdir() if f.is_file()),
        key=lambda f: f.name,
    )

    found_products = []
    for f in all_files:
        if f.name == "products-manifest.json":
            continue  # skip self
        entry: dict = {
            "name": f.name,
            "size_bytes": f.stat().st_size,
        }
        # Compute hashes for non-huge files only (< 100 MB)
        if f.stat().st_size < 100_000_000:
            try:
                entry["sha256"] = _sha256_file(f)
            except OSError:
                entry["sha256"] = None
        found_products.append(entry)

    manifest = {
        "event_id": event_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "modules_executed": modules,
        "required_core_products": list(_REQUIRED_CORE_PRODUCTS),
        "found_products": found_products,
        "product_count": len(found_products),
        "validation": {
            "valid": valid,
            "failure_reason": failure_reason or None,
            "required_present": any(
                p["name"] in _REQUIRED_CORE_PRODUCTS for p in found_products
            ),
            "all_non_empty": all(
                p["size_bytes"] > 0 for p in found_products
            ),
        },
    }

    manifest_path = products_path / "products-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    logger.info("Event '%s': wrote products-manifest.json", event_id)
    return manifest_path


def _write_provenance(
    event_id: str,
    record: RequestStatus,
    modules: list[str],
    start_time: datetime,
    end_time: datetime,
    attempt_number: int,
) -> Path:
    """Write provenance.json under .service/events/<event_id>/.

    Returns the path to the provenance file.
    """
    # Collect input file inventory
    incoming = paths.event_incoming_dir(event_id)
    input_files = []
    if incoming.is_dir():
        for f in sorted(incoming.iterdir()):
            if f.is_file():
                entry: dict = {
                    "filename": f.name,
                    "size_bytes": f.stat().st_size,
                }
                try:
                    entry["sha256"] = _sha256_file(f)
                except OSError:
                    entry["sha256"] = None
                input_files.append(entry)

    # Collect output product inventory
    products_path = paths.event_products_dir(event_id)
    output_products = []
    if products_path.is_dir():
        for f in sorted(products_path.iterdir()):
            if f.is_file() and f.name not in (
                "products-manifest.json",
            ):
                entry_out: dict = {
                    "filename": f.name,
                    "size_bytes": f.stat().st_size,
                }
                if f.stat().st_size < 100_000_000:
                    try:
                        entry_out["sha256"] = _sha256_file(f)
                    except OSError:
                        entry_out["sha256"] = None
                output_products.append(entry_out)

    # Model.conf and products.conf paths
    model_conf = paths.profile_config_dir() / "model.conf"
    products_conf = paths.profile_config_dir() / "products.conf"

    provenance: dict = {
        "event_id": event_id,
        "user_id": record.user_id,
        "profile": settings.shakemap_profile,
        "modules": modules,
        "model_conf_path": str(model_conf) if model_conf.is_file() else None,
        "products_conf_path": str(products_conf) if products_conf.is_file() else None,
        "vs30_file": str(paths.vs30_grid_path()),
        "topo_file": str(paths.topo_grid_path()),
        "input_files": input_files,
        "output_products": output_products,
        "attempt_number": attempt_number,
        "execution_timestamp": start_time.isoformat(),
        "completion_timestamp": end_time.isoformat(),
        "duration_seconds": round((end_time - start_time).total_seconds(), 3),
    }

    # Try to get shakemap version
    try:
        result = subprocess.run(
            ["shake", "--version"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            provenance["shakemap_version"] = result.stdout.strip()
    except Exception:
        provenance["shakemap_version"] = None

    prov_path = paths.event_provenance_file(event_id)
    prov_path.parent.mkdir(parents=True, exist_ok=True)
    prov_path.write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8"
    )
    logger.info("Event '%s': wrote provenance.json", event_id)
    return prov_path


def _copy_audit_record(event_id: str, manifest_path: Path | None) -> None:
    """Copy service records to products/<event_id>/service-record/ for audit.

    Copies:
    - requeststatus.json (snapshot at publication time)
    - provenance.json
    - products-manifest.json
    """
    audit_dir = paths.event_audit_dir(event_id)
    audit_dir.mkdir(parents=True, exist_ok=True)

    # Copy requeststatus.json
    status_file = paths.event_status_file(event_id)
    if status_file.is_file():
        shutil.copy2(str(status_file), str(audit_dir / "requeststatus.json"))

    # Copy provenance.json
    prov_file = paths.event_provenance_file(event_id)
    if prov_file.is_file():
        shutil.copy2(str(prov_file), str(audit_dir / "provenance.json"))

    # Copy products-manifest.json
    if manifest_path and manifest_path.is_file():
        shutil.copy2(
            str(manifest_path), str(audit_dir / "products-manifest.json")
        )

    logger.info("Event '%s': audit record copied to %s", event_id, audit_dir)


# ------------------------------------------------------------------
# Execution bridge
# ------------------------------------------------------------------

def _prepare_shakemap_data(event_id: str) -> Path:
    """Copy incoming files to ShakeMap's expected data directory.

    ShakeMap expects input files at:
        ``<profile>/data/<event_id>/current/``

    Due to the configure symlink (``profile/data -> SERVICE_ROOT/.service/work``),
    this resolves to ``.service/work/<event_id>/current/``.

    Files are copied (not moved) from ``incoming/<event_id>/`` so the
    authoritative inputs in ``incoming/`` are preserved.

    Returns:
        Path to the prepared data directory.

    Raises:
        FileNotFoundError: if incoming directory or required files missing.
    """
    incoming = paths.event_incoming_dir(event_id)
    if not incoming.is_dir():
        raise FileNotFoundError(
            f"No incoming directory for event '{event_id}': {incoming}"
        )

    # Verify event.xml exists (minimum requirement).
    event_xml = incoming / "event.xml"
    if not event_xml.is_file():
        raise FileNotFoundError(
            f"Required file 'event.xml' not found in {incoming}"
        )

    # Target: ShakeMap data directory for this event.
    data_dir = paths.profile_event_data_dir(event_id)
    data_dir.mkdir(parents=True, exist_ok=True)

    # Copy all files from incoming to the ShakeMap data directory.
    for src_file in incoming.iterdir():
        if src_file.is_file():
            dst_file = data_dir / src_file.name
            shutil.copy2(str(src_file), str(dst_file))
            logger.debug(
                "Copied %s -> %s", src_file.name, dst_file,
            )

    logger.info(
        "Prepared ShakeMap data for event '%s' at %s (%d files)",
        event_id, data_dir,
        sum(1 for _ in data_dir.iterdir() if _.is_file()),
    )
    return data_dir


def _publish_products_atomic(event_id: str, source_dir: Path) -> str:
    """Atomically publish products from work area to products/<event_id>/.

    Uses write-to-temporary-then-rename (contract §2.9) to ensure
    consumers never see partial products.

    Returns:
        The relative products path (e.g. ``products/<event_id>``).
    """
    products_root = paths.products_dir()
    products_root.mkdir(parents=True, exist_ok=True)

    target = paths.event_products_dir(event_id)

    # Create a temp directory on the same filesystem for atomic rename.
    tmp_dir = Path(tempfile.mkdtemp(
        dir=str(products_root),
        prefix=f".{event_id}_",
        suffix=".publishing",
    ))

    try:
        # Copy all products to temp directory.
        for item in source_dir.iterdir():
            src = str(item)
            dst = str(tmp_dir / item.name)
            if item.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)

        # Atomic swap: if target exists, remove first then rename.
        if target.exists():
            shutil.rmtree(str(target))
        os.rename(str(tmp_dir), str(target))

    except BaseException:
        shutil.rmtree(str(tmp_dir), ignore_errors=True)
        raise

    relative = f"products/{event_id}"
    logger.info(
        "Published products for event '%s' to %s",
        event_id, target,
    )
    return relative


def _find_products_dir(event_id: str) -> Path | None:
    """Locate ShakeMap output products after execution.

    ShakeMap writes products to ``<data>/<event_id>/current/products/``.
    Returns the path if it exists and is non-empty, else None.
    """
    products = paths.profile_event_data_dir(event_id) / "products"
    if products.is_dir() and any(products.iterdir()):
        return products
    return None


def run_shake_for_event(record: RequestStatus) -> str:
    """Full execution bridge: prepare data, run ShakeMap, collect results.

    This function owns the RUNNING -> SUCCESS/FAILED transitions.
    The caller (worker) owns QUEUED -> RUNNING.

    Args:
        record: The claimed RequestStatus (already in RUNNING state).

    Returns:
        ``"success"`` or ``"failed"`` as outcome string for the worker.
    """
    event_id = record.event_id
    modules = settings.shakemap_modules.split()
    start_time = datetime.now(timezone.utc)

    # Step 0: Record execution context on the current attempt.
    execution_context = {
        "profile": settings.shakemap_profile,
        "modules": modules,
    }
    try:
        current_record = read_status(event_id)
        if current_record and current_record.attempt_history:
            current_record.attempt_history[-1].execution_context = execution_context
            write_status_atomic(event_id, current_record)
            logger.info(
                "Event '%s': execution_context recorded (profile=%s, modules=%s)",
                event_id, settings.shakemap_profile, modules,
            )
    except Exception as exc:
        # Non-fatal: continue execution even if context recording fails
        logger.warning(
            "Event '%s': could not record execution_context: %s",
            event_id, exc,
        )

    # Step 1: Prepare ShakeMap data directory.
    try:
        _prepare_shakemap_data(event_id)
    except (FileNotFoundError, OSError) as exc:
        reason = f"Data preparation failed: {exc}"
        logger.error("Event '%s': %s", event_id, reason)
        transition_to_failed(event_id, reason)
        return "failed"

    # Step 2: Invoke ShakeMap with configured modules.
    # Capture stdout/stderr to logs/<event_id>.log for operator access.
    event_log = paths.event_log_file(event_id)
    try:
        cmd = run_shake(event_id, modules=modules, force=True, log_file=event_log)
        logger.info(
            "ShakeMap completed successfully for event '%s': %s",
            event_id, " ".join(cmd),
        )
    except ShakeError as exc:
        reason = f"ShakeMap execution failed: {exc}"
        logger.error("Event '%s': %s", event_id, reason)
        transition_to_failed(event_id, reason)
        return "failed"
    except Exception as exc:
        reason = f"Unexpected error during ShakeMap execution: {exc}"
        logger.error("Event '%s': %s", event_id, reason)
        transition_to_failed(event_id, reason)
        return "failed"

    # Step 3: Collect and publish products.
    products_source = _find_products_dir(event_id)

    if products_source is None:
        reason = "ShakeMap completed but no products directory found"
        logger.error("Event '%s': %s", event_id, reason)
        transition_to_failed(event_id, reason)
        return "failed"

    # Step 3a: Validate products before publishing.
    valid, validation_reason = _validate_products(products_source)
    if not valid:
        reason = f"Product validation failed: {validation_reason}"
        logger.error("Event '%s': %s", event_id, reason)
        transition_to_failed(event_id, reason)
        return "failed"

    # Step 3b: Publish atomically.
    try:
        products_path = _publish_products_atomic(event_id, products_source)
    except OSError as exc:
        reason = f"Product publication failed: {exc}"
        logger.error("Event '%s': %s", event_id, reason)
        transition_to_failed(event_id, reason)
        return "failed"

    end_time = datetime.now(timezone.utc)

    # Step 4: Write products-manifest.json.
    manifest_path = None
    try:
        published_dir = paths.event_products_dir(event_id)
        manifest_path = _write_products_manifest(
            event_id, published_dir, modules, valid, validation_reason,
        )
    except Exception as exc:
        logger.warning("Event '%s': could not write products-manifest: %s", event_id, exc)

    # Step 5: Transition to SUCCESS.
    transition_to_success(event_id, products_dir=products_path)
    logger.info("Event '%s': completed with SUCCESS", event_id)

    # Step 6: Write provenance.json (post-success, non-fatal).
    try:
        _write_provenance(
            event_id, record, modules, start_time, end_time,
            attempt_number=record.current_attempt,
        )
    except Exception as exc:
        logger.warning("Event '%s': could not write provenance: %s", event_id, exc)

    # Step 7: Copy audit record to products/<event_id>/service-record/.
    try:
        _copy_audit_record(event_id, manifest_path)
    except Exception as exc:
        logger.warning("Event '%s': could not copy audit record: %s", event_id, exc)

    return "success"
