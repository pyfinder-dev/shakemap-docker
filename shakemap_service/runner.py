# -*- coding: utf-8 -*-
"""ShakeMap CLI invocation and execution bridge.

Phase 01-06: ``run_shake()`` — low-level CLI wrapper.
Phase 07:    ``run_shake_for_event()`` — full execution bridge.

The execution bridge:
1. Validates incoming files exist.
2. Copies files from ``incoming/<event_id>/`` to ShakeMap's expected
   ``<profile>/data/<event_id>/current/`` directory.
3. Invokes ``shake`` with configured modules.
4. On success: publishes products atomically to ``products/<event_id>/``,
   transitions to SUCCESS.
5. On failure: captures error, transitions to FAILED.

Responsibility boundary:
- Worker owns QUEUED -> RUNNING (claim locking).
- Runner owns RUNNING -> SUCCESS/FAILED (this module).
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Sequence

import subprocess

from . import paths
from .config import settings
from .status import (
    RequestStatus,
    transition_to_failed,
    transition_to_success,
)

logger = logging.getLogger(__name__)


class ShakeError(RuntimeError):
    """Raised when the 'shake' CLI fails."""
    pass


def run_shake(event_id: str, modules: Sequence[str] | None = None, force: bool = False) -> list[str]:
    """
    Build and run the 'shake' command for a given event_id.

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
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        raise ShakeError(f"'shake' failed with exit code {exc.returncode}") from exc

    return cmd


# ------------------------------------------------------------------
# Phase 07 — Execution bridge
# ------------------------------------------------------------------

def _prepare_shakemap_data(event_id: str) -> Path:
    """Copy incoming files to ShakeMap's expected data directory.

    ShakeMap expects input files at:
        ``<profile>/data/<event_id>/current/``

    Due to the entrypoint symlink (``profile/data -> SERVICE_ROOT/work``),
    this resolves to ``work/<event_id>/current/``.

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

    # Step 1: Prepare ShakeMap data directory.
    try:
        _prepare_shakemap_data(event_id)
    except (FileNotFoundError, OSError) as exc:
        reason = f"Data preparation failed: {exc}"
        logger.error("Event '%s': %s", event_id, reason)
        transition_to_failed(event_id, reason)
        return "failed"

    # Step 2: Invoke ShakeMap with configured modules.
    modules = settings.shakemap_modules.split()
    try:
        cmd = run_shake(event_id, modules=modules, force=True)
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

    if products_source is not None:
        try:
            products_path = _publish_products_atomic(event_id, products_source)
        except OSError as exc:
            reason = f"Product publication failed: {exc}"
            logger.error("Event '%s': %s", event_id, reason)
            transition_to_failed(event_id, reason)
            return "failed"
    else:
        # ShakeMap succeeded but produced no products directory.
        # Still mark as success — some module sets may not produce files.
        products_path = None
        logger.warning(
            "Event '%s': ShakeMap succeeded but no products directory found.",
            event_id,
        )

    # Step 4: Transition to SUCCESS.
    transition_to_success(event_id, products_dir=products_path)
    logger.info("Event '%s': completed with SUCCESS", event_id)
    return "success"

