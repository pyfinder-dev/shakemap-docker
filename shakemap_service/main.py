# -*- coding: utf-8 -*-
"""ShakeMap service -- FastAPI application.

Provides:
  - ``GET /healthz`` -- comprehensive health and readiness.
  - ``GET /config`` -- active configuration inspection.
  - ``GET /config/profiles`` -- ShakeMap profiles listing.
  - ``POST /events/submit`` -- event submission and staging.
  - ``GET /events`` -- event discovery with filtering.
  - ``GET /events/{event_id}`` -- single event detail.
  - ``GET /events/{event_id}/products`` -- event products listing.
  - ``GET /queue`` -- current queue state.

Background worker:
  - Starts on application startup via ``lifespan``.
  - Recovers interrupted RUNNING events from previous crashes.
  - Continuously processes QUEUED events using ``execute_shakemap``.

Two-stage health model:
  - Stage 1: infrastructure + ShakeMap CLI availability.
  - Stage 2: ShakeMap profile configured, data present, ready to process.
  - Status is ``healthy`` only when both stages pass.
  - Status is ``not_ready`` otherwise (no ``degraded``).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse

from .config import settings
from . import paths
from .submission import submit_event, SubmissionResult
from .worker import recover_interrupted_events, run_worker_cycle, execute_shakemap
from .queue import discover_queue
from .status import read_status, scan_event_records, EventStatus

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Background worker thread
# ------------------------------------------------------------------

_worker_stop = threading.Event()

# Backoff configuration for the worker loop.
_WORKER_IDLE_SLEEP = 5.0       # seconds to sleep when queue is empty
_WORKER_BUSY_SLEEP = 0.5       # seconds between processing events
_WORKER_ERROR_SLEEP = 10.0     # seconds to sleep after an error
_WORKER_NOT_READY_SLEEP = 30.0 # seconds to wait when Stage 2 not ready


def _worker_loop() -> None:
    """Background worker loop — processes QUEUED events continuously.

    This loop:
    1. Checks Stage 2 readiness before processing.
    2. Calls ``run_worker_cycle(execute_fn=execute_shakemap)`` to
       claim and execute the next QUEUED event.
    3. Uses adaptive backoff: fast when busy, slow when idle.
    4. Stops when ``_worker_stop`` is set.

    Runs as a daemon thread started by the lifespan handler.
    """
    logger.info("Worker thread started")
    while not _worker_stop.is_set():
        try:
            # Gate: only process if Stage 2 is ready
            if not _is_stage2_ready():
                logger.debug("Worker: Stage 2 not ready, sleeping %.0fs", _WORKER_NOT_READY_SLEEP)
                _worker_stop.wait(_WORKER_NOT_READY_SLEEP)
                continue

            result = run_worker_cycle(execute_fn=execute_shakemap)

            if result.claimed:
                logger.info(
                    "Worker processed event '%s': outcome=%s, final_status=%s",
                    result.event_id, result.outcome, result.final_status,
                )
                # Short sleep before checking for more work
                _worker_stop.wait(_WORKER_BUSY_SLEEP)
            else:
                # No candidates — idle backoff
                _worker_stop.wait(_WORKER_IDLE_SLEEP)

        except Exception:
            logger.exception("Worker cycle error — sleeping %.0fs", _WORKER_ERROR_SLEEP)
            _worker_stop.wait(_WORKER_ERROR_SLEEP)

    logger.info("Worker thread stopped")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler — startup recovery and worker thread."""
    # -- Startup --
    logger.info("ShakeMap service starting up")

    # Recover any events stuck in RUNNING from a previous crash
    try:
        recovered = recover_interrupted_events()
        if recovered:
            logger.info("Startup recovery: recovered %d interrupted events: %s",
                        len(recovered), recovered)
        else:
            logger.info("Startup recovery: no interrupted events found")
    except Exception:
        logger.exception("Startup recovery failed — continuing anyway")

    # Start background worker thread
    _worker_stop.clear()
    worker_thread = threading.Thread(
        target=_worker_loop,
        name="shakemap-worker",
        daemon=True,
    )
    worker_thread.start()
    logger.info("Background worker thread started")

    yield

    # -- Shutdown --
    logger.info("ShakeMap service shutting down — stopping worker")
    _worker_stop.set()
    worker_thread.join(timeout=30)
    if worker_thread.is_alive():
        logger.warning("Worker thread did not stop within 30s timeout")
    else:
        logger.info("Worker thread stopped cleanly")


app = FastAPI(title="ShakeMap Service", version="0.1.0", lifespan=lifespan)


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _read_readiness_sentinel() -> dict:
    """Read the Stage 2 sentinel file and return status info.

    Returns a dict with ``passed`` (bool), ``reason`` (str),
    and ``overrides`` (list of active override flags).

    Sentinel format:
      - ``ready`` — fully ready, no overrides
      - ``ready|uniform_vs30_override`` — ready with uniform VS30 override
      - ``not_ready|<reason>`` — not ready with reason
    """
    sentinel = paths.readiness_sentinel()
    if not sentinel.is_file():
        return {
            "passed": False,
            "reason": "Stage 2 configuration has not been run",
            "overrides": [],
        }
    try:
        content = sentinel.read_text().strip()
    except OSError:
        return {
            "passed": False,
            "reason": "Stage 2 sentinel file unreadable",
            "overrides": [],
        }

    if content.startswith("ready"):
        # Parse override flags: ready|flag1,flag2
        parts = content.split("|", 1)
        overrides = []
        if len(parts) > 1 and parts[1]:
            overrides = [f.strip() for f in parts[1].split(",") if f.strip()]
        return {"passed": True, "reason": "", "overrides": overrides}
    else:
        # Format: not_ready|<reason>
        parts = content.split("|", 1)
        reason = parts[1] if len(parts) > 1 else content
        return {"passed": False, "reason": reason, "overrides": []}


def _check_stage2_data() -> dict:
    """Check Stage 2 data readiness (VS30 and topo files)."""
    # Determine actual VS30 path: custom env var > default grid path
    vs30_file_path = ""
    if settings.vs30_file:
        vs30_file_path = settings.vs30_file
    else:
        vs30_file_path = str(paths.vs30_grid_path())

    vs30_exists = Path(vs30_file_path).is_file() if vs30_file_path else False
    vs30_non_empty = (
        Path(vs30_file_path).stat().st_size > 0
        if vs30_exists
        else False
    )

    # Determine actual topo path: custom env var > default grid path
    topo_file_path = ""
    if settings.topo_file:
        topo_file_path = settings.topo_file
    else:
        topo_file_path = str(paths.topo_grid_path())

    topo_exists = Path(topo_file_path).is_file() if topo_file_path else False
    topo_non_empty = (
        Path(topo_file_path).stat().st_size > 0
        if topo_exists
        else False
    )

    # Check model.conf for vs30file reference
    model_conf_valid = False
    model_conf_path = paths.profile_config_dir() / "model.conf"
    if model_conf_path.is_file():
        try:
            content = model_conf_path.read_text()
            # Valid if: vs30file points to existing file, OR
            # allow_uniform_vs30 is set and vs30file is empty/absent
            if "CA_vs30.grd" in content:
                model_conf_valid = False  # stale template reference
            elif settings.allow_uniform_vs30 == "1":
                model_conf_valid = True
            elif vs30_exists and vs30_non_empty:
                model_conf_valid = True
        except OSError:
            model_conf_valid = False

    result = {
        "vs30_file": vs30_file_path,
        "vs30_file_exists": vs30_exists,
        "vs30_file_non_empty": vs30_non_empty,
        "topo_file": topo_file_path,
        "topo_file_exists": topo_exists,
        "topo_file_non_empty": topo_non_empty,
        "model_conf_valid": model_conf_valid,
    }

    # Allow uniform VS30 if explicitly acknowledged
    if settings.allow_uniform_vs30 == "1":
        result["allow_uniform_vs30"] = True

    return result


def _is_stage2_ready() -> bool:
    """Return True if Stage 2 sentinel says ready."""
    sentinel_info = _read_readiness_sentinel()
    return sentinel_info["passed"]


def _compute_blocking_reasons(
    stage1_passed: bool,
    tier1_ok: bool,
    tier2_ok: bool,
    shake_cli_available: bool,
    shake_cli_responsive: bool,
    dir_checks: dict,
    sentinel_info: dict,
    stage2_data: dict,
    profiles_conf_readable: bool,
    profile_exists: bool,
    profile_config_valid: bool,
    profile_data_bridge_ok: bool,
) -> list[str]:
    """Compute a list of human-readable blocking reasons."""
    reasons: list[str] = []

    # Stage 1 issues
    for name, info in dir_checks.items():
        if not info.get("exists"):
            reasons.append(f"Directory {name}/ does not exist")
        elif not info.get("writable"):
            reasons.append(f"Directory {name}/ is not writable")

    if not shake_cli_available:
        reasons.append("ShakeMap CLI (shake) not found on PATH")
    elif not shake_cli_responsive:
        reasons.append("ShakeMap CLI not responsive (shake --help failed)")

    # Stage 2 issues
    if not sentinel_info["passed"]:
        reason = sentinel_info.get("reason", "Stage 2 not run")
        reasons.append(reason)
    else:
        # Stage 2 sentinel says ready but data checks may reveal issues
        if not stage2_data.get("vs30_file_exists") and not stage2_data.get("allow_uniform_vs30"):
            reasons.append("VS30 grid missing")
        if not stage2_data.get("model_conf_valid"):
            reasons.append("model.conf validation failed")
        if not profiles_conf_readable:
            reasons.append("profiles.conf not found or not readable")
        if not profile_exists:
            reasons.append("ShakeMap profile directory missing")
        if not profile_config_valid:
            reasons.append("Profile config directory missing")
        if not profile_data_bridge_ok:
            reasons.append("Profile data symlink not correct")

    return reasons


def _compute_next_action(blocking_reasons: list[str], sentinel_info: dict) -> str:
    """Compute the recommended next action based on blocking reasons."""
    if not blocking_reasons:
        return ""

    # If Stage 2 hasn't been run, that's the primary recommendation
    if not sentinel_info["passed"]:
        reason = sentinel_info.get("reason", "")
        if "not been run" in reason:
            return "Run: docker exec <container> /app/scripts/configure-shakemap.sh"
        if "sentinel" in reason.lower():
            return "Run: docker exec <container> /app/scripts/configure-shakemap.sh"

    # Check specific issues
    for reason in blocking_reasons:
        if "not writable" in reason.lower():
            return "Fix host directory permissions: chown -R 1000:1000 <host-runtime-dir>"
        if "does not exist" in reason.lower() and "directory" in reason.lower():
            return "Restart the container to recreate service directories"
        if "shake" in reason.lower() and "path" in reason.lower():
            return "Rebuild the Docker image -- ShakeMap may not be installed correctly"
        if "vs30" in reason.lower():
            return "Provide VS30 grid file or set SHAKEMAP_ALLOW_UNIFORM_VS30=1"
        if "profile" in reason.lower() or "profiles.conf" in reason.lower():
            return "Run: docker exec <container> /app/scripts/configure-shakemap.sh"
        if "model.conf" in reason.lower():
            return "Run: docker exec <container> /app/scripts/configure-shakemap.sh"
        if "symlink" in reason.lower():
            return "Run: docker exec <container> /app/scripts/configure-shakemap.sh"

    return "Run: docker exec <container> /app/scripts/configure-shakemap.sh"


# ------------------------------------------------------------------
# GET /config -- active ShakeMap configuration inspection
# ------------------------------------------------------------------

@app.get("/config")
def get_config() -> dict:
    """Return the active ShakeMap configuration.

    Read-only inspection of current profile, paths, data status,
    and readiness state.  Reports override flags (e.g. uniform VS30)
    explicitly.
    """
    sentinel_info = _read_readiness_sentinel()
    stage2_data = _check_stage2_data()

    # Determine readiness state — differentiate override from fully-provisioned
    overrides = sentinel_info.get("overrides", [])
    if sentinel_info["passed"]:
        if overrides:
            readiness_state = "ready_with_overrides"
        else:
            readiness_state = "ready"
    else:
        readiness_state = "not_ready"

    # Build override warnings
    overrides = sentinel_info.get("overrides", [])
    override_warnings = []
    if "uniform_vs30_override" in overrides:
        override_warnings.append(
            "UNIFORM_VS30: No VS30 grid file. Using uniform VS30 (760 m/s). "
            "This is a development/emergency override. "
            "Production deployments should provide a VS30 grid."
        )
    if settings.allow_uniform_vs30 == "1" and not stage2_data.get("vs30_file_exists"):
        if "uniform_vs30_override" not in overrides:
            override_warnings.append(
                "SHAKEMAP_ALLOW_UNIFORM_VS30=1 is set but configure-shakemap.sh "
                "has not been re-run with this setting."
            )

    return {
        "active_profile": settings.shakemap_profile,
        "available_profiles": paths.list_profiles(),
        "profiles_conf_path": str(paths.profiles_conf()),
        "profiles_conf_exists": paths.profiles_conf().is_file(),
        "model_conf_path": str(paths.profile_config_dir() / "model.conf"),
        "model_conf_exists": (paths.profile_config_dir() / "model.conf").is_file(),
        "products_conf_path": str(paths.profile_config_dir() / "products.conf"),
        "products_conf_exists": (paths.profile_config_dir() / "products.conf").is_file(),
        "products_conf_required": False,  # products.conf is optional; ShakeMap uses defaults when absent
        "vs30_file": stage2_data.get("vs30_file", ""),
        "vs30_file_exists": stage2_data.get("vs30_file_exists", False),
        "topo_file": stage2_data.get("topo_file", ""),
        "topo_file_exists": stage2_data.get("topo_file_exists", False),
        "readiness_state": readiness_state,
        "readiness_reason": sentinel_info.get("reason", ""),
        "overrides": overrides,
        "override_warnings": override_warnings,
        "service_root": settings.service_root,
        "shakemap_modules": settings.shakemap_modules,
    }


# ------------------------------------------------------------------
# GET /config/profiles -- list existing profiles
# ------------------------------------------------------------------

@app.get("/config/profiles")
def get_config_profiles() -> dict:
    """List existing ShakeMap profiles with validation status.

    Returns each profile's directory, config existence, and whether
    it has the required model.conf.  Read-only — does not create or
    modify profiles.
    """
    available = paths.list_profiles()
    active = settings.shakemap_profile

    profiles = []
    for name in available:
        profile_root = paths.profile_root(name)
        config_dir = paths.profile_config_dir(name)
        model_conf = config_dir / "model.conf"
        data_dir = paths.profile_data_dir(name)

        profiles.append({
            "name": name,
            "is_active": name == active,
            "profile_root": str(profile_root),
            "config_dir_exists": config_dir.is_dir(),
            "model_conf_exists": model_conf.is_file(),
            "data_dir_is_symlink": data_dir.is_symlink(),
            "valid": config_dir.is_dir() and model_conf.is_file(),
        })

    return {
        "active_profile": active,
        "profile_count": len(profiles),
        "profiles": profiles,
    }



@app.post("/events/submit")
async def submit_event_endpoint(
    event_id: Annotated[str, Form()],
    user_id: Annotated[str, Form()],
    files: list[UploadFile] = File(...),
) -> dict:
    """Submit an event for ShakeMap processing.

    Accepts ``event_id``, ``user_id`` as form fields, and one or more
    input files as multipart file uploads. Delegates all logic to
    ``submission.submit_event()``.

    Returns HTTP 503 if Stage 2 configuration is not complete.
    Returns ``event_id``, ``status``, ``status_path``, and
    ``replaced_previous``.
    """
    # -- Stage 2 readiness gate --
    if not _is_stage2_ready():
        sentinel_info = _read_readiness_sentinel()
        return JSONResponse(
            status_code=503,
            content={
                "detail": "Service not ready: Stage 2 configuration is not complete",
                "reason": sentinel_info.get("reason", "Stage 2 not run"),
                "status": "not_ready",
            },
        )

    # Read file payloads into memory
    file_payloads: dict[str, bytes] = {}
    for upload in files:
        if upload.filename:
            content = await upload.read()
            file_payloads[upload.filename] = content

    if not file_payloads:
        raise HTTPException(status_code=400, detail="No files provided.")

    try:
        result: SubmissionResult = submit_event(
            event_id=event_id,
            user_id=user_id,
            files=file_payloads,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Submission failed for event_id=%s", event_id)
        raise HTTPException(status_code=500, detail=str(exc))

    body = {
        "event_id": result.event_id,
        "status": result.status,
        "status_path": result.status_path,
        "replaced_previous": result.replaced_previous,
        "validation_errors": result.validation_errors,
    }

    if result.status == "VALIDATION_FAILED":
        return JSONResponse(content=body, status_code=422)

    return body


# ------------------------------------------------------------------
# GET /healthz -- comprehensive health and readiness
# ------------------------------------------------------------------

@app.get("/healthz")
def healthz() -> dict:
    """Comprehensive health and readiness endpoint.

    Two-stage health model:
      - Stage 1: infrastructure dirs + ShakeMap CLI availability.
      - Stage 2: profile configured, data present, sentinel file ready.
      - Status is ``healthy`` only when both stages pass.
      - Status is ``not_ready`` otherwise.

    Returns ``blocking_reasons`` (list of strings) and ``next_action``
    (recommended fix) when status is ``not_ready``.

    No ``degraded`` status exists.
    """

    # -- Tier 1: Infrastructure --
    dir_checks: dict[str, dict[str, bool]] = {}
    tier1_ok = True

    for d in paths.all_service_dirs():
        exists = d.is_dir()
        writable = os.access(d, os.W_OK) if exists else False
        dir_checks[d.name] = {"exists": exists, "writable": writable}
        if not exists or not writable:
            tier1_ok = False

    infrastructure = {
        "service_root": str(paths.service_root()),
        "directories": dir_checks,
    }

    # -- Tier 2: ShakeMap CLI availability --
    shake_cli_available = shutil.which("shake") is not None

    # Invoke shake with a lightweight command to verify it actually works.
    shake_cli_responsive = False
    if shake_cli_available:
        try:
            result = subprocess.run(
                ["shake", "--help"],
                capture_output=True,
                timeout=15,
            )
            shake_cli_responsive = result.returncode == 0
        except Exception:
            shake_cli_responsive = False

    tier2_ok = shake_cli_available and shake_cli_responsive

    shakemap_info = {
        "shake_cli_available": shake_cli_available,
        "shake_cli_responsive": shake_cli_responsive,
    }

    # -- Stage 1 aggregate --
    stage1_passed = tier1_ok and tier2_ok
    stage1 = {
        "passed": stage1_passed,
        "checks": {
            "directories_exist": all(v["exists"] for v in dir_checks.values()),
            "directories_writable": all(v["writable"] for v in dir_checks.values()),
            "shake_cli_available": shake_cli_available,
            "shake_cli_responsive": shake_cli_responsive,
        },
    }

    # -- Stage 2: Profile + Data readiness --
    sentinel_info = _read_readiness_sentinel()
    stage2_data = _check_stage2_data()

    # Profile structure checks
    profiles_conf_readable = paths.profiles_conf().is_file()
    profile_exists = paths.profile_root().is_dir()
    profile_config_valid = paths.profile_config_dir().is_dir()

    data_dir = paths.profile_data_dir()
    profile_data_bridge_ok = (
        data_dir.is_symlink()
        and data_dir.resolve() == paths.work_dir().resolve()
    )
    # Note: work_dir() now returns .service/work/ and the symlink
    # target in configure-shakemap.sh matches.

    available_profiles = paths.list_profiles()
    active_profile_name = settings.shakemap_profile

    stage2_passed = sentinel_info["passed"]

    stage2 = {
        "passed": stage2_passed,
        **({} if stage2_passed else {"reason": sentinel_info["reason"]}),
        "checks": {
            "vs30_file": stage2_data.get("vs30_file", ""),
            "vs30_file_exists": stage2_data.get("vs30_file_exists", False),
            "vs30_file_non_empty": stage2_data.get("vs30_file_non_empty", False),
            "model_conf_valid": stage2_data.get("model_conf_valid", False),
            "topo_file": stage2_data.get("topo_file", ""),
            "topo_file_exists": stage2_data.get("topo_file_exists", False),
            "topo_file_non_empty": stage2_data.get("topo_file_non_empty", False),
            "profiles_conf_readable": profiles_conf_readable,
            "profile_exists": profile_exists,
            "profile_config_valid": profile_config_valid,
            "profile_data_bridge_ok": profile_data_bridge_ok,
        },
        "active_profile": active_profile_name,
        "available_profiles": available_profiles,
    }

    if stage2_data.get("allow_uniform_vs30"):
        stage2["checks"]["allow_uniform_vs30"] = True

    # -- Override reporting --
    overrides = sentinel_info.get("overrides", [])
    override_warnings = []
    if "uniform_vs30_override" in overrides:
        override_warnings.append(
            "UNIFORM_VS30: No VS30 grid file. Using uniform VS30 (760 m/s). "
            "This is a development/emergency override."
        )
        stage2["checks"]["uniform_vs30_override_active"] = True
    if settings.allow_uniform_vs30 == "1":
        stage2["checks"]["allow_uniform_vs30_env"] = True

    # -- Tier 5: Configuration reporting --
    configuration = {
        "modules": settings.shakemap_modules,
        "service_root": settings.service_root,
    }

    # -- Status determination --
    # Differentiate: fully-provisioned vs. running with operator overrides.
    # A container with uniform VS30 override MUST NOT appear identical
    # to a fully-provisioned installation.
    if stage1_passed and stage2_passed:
        if overrides:
            status = "healthy_with_overrides"
        else:
            status = "healthy"
    else:
        status = "not_ready"

    # -- Blocking reasons and next action --
    blocking_reasons = _compute_blocking_reasons(
        stage1_passed=stage1_passed,
        tier1_ok=tier1_ok,
        tier2_ok=tier2_ok,
        shake_cli_available=shake_cli_available,
        shake_cli_responsive=shake_cli_responsive,
        dir_checks=dir_checks,
        sentinel_info=sentinel_info,
        stage2_data=stage2_data,
        profiles_conf_readable=profiles_conf_readable,
        profile_exists=profile_exists,
        profile_config_valid=profile_config_valid,
        profile_data_bridge_ok=profile_data_bridge_ok,
    )

    next_action = _compute_next_action(blocking_reasons, sentinel_info)

    response = {
        "status": status,
        "blocking_reasons": blocking_reasons,
        "next_action": next_action,
        "overrides": overrides,
        "override_warnings": override_warnings,
        "stage1": stage1,
        "stage2": stage2,
        "infrastructure": infrastructure,
        "shakemap": shakemap_info,
        "configuration": configuration,
    }

    return response


# ------------------------------------------------------------------
# GET /events -- event discovery with filtering
# ------------------------------------------------------------------

@app.get("/events")
def list_events(
    status: Optional[str] = Query(None, description="Filter by event status (e.g. QUEUED, RUNNING, SUCCESS, FAILED)"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum number of events to return"),
    offset: int = Query(0, ge=0, description="Number of events to skip"),
) -> dict:
    """List all events with status, timestamps, and product references.

    Supports filtering by status, pagination via limit/offset,
    and returns total and filtered counts.

    Query parameters:
        - ``status``: filter to events with this status (case-insensitive)
        - ``limit``: max events to return (default 100, max 1000)
        - ``offset``: skip this many events (default 0)
    """
    all_records = scan_event_records()

    # Sort by submitted_at descending (newest first)
    all_records.sort(key=lambda r: r.submitted_at or "", reverse=True)

    total_count = len(all_records)

    # Filter by status if requested
    if status:
        status_upper = status.upper()
        filtered = [r for r in all_records if r.status == status_upper]
    else:
        filtered = all_records

    filtered_count = len(filtered)

    # Apply pagination
    page = filtered[offset:offset + limit]

    events = []
    for record in page:
        event_entry = {
            "event_id": record.event_id,
            "user_id": record.user_id,
            "status": record.status,
            "submitted_at": record.submitted_at,
            "queued_at": record.queued_at,
            "started_at": record.started_at,
            "completed_at": record.completed_at,
            "current_attempt": record.current_attempt,
            "max_attempts": record.max_attempts,
            "failure_reason": record.failure_reason,
        }
        # Include products path if available
        if record.published_products_directory:
            event_entry["products_path"] = record.published_products_directory
        # Include whether products directory exists on disk
        products_dir = paths.event_products_dir(record.event_id)
        event_entry["has_products"] = products_dir.is_dir()

        events.append(event_entry)

    return {
        "total_count": total_count,
        "filtered_count": filtered_count,
        "limit": limit,
        "offset": offset,
        "status_filter": status.upper() if status else None,
        "events": events,
    }


# ------------------------------------------------------------------
# GET /events/{event_id} -- single event detail
# ------------------------------------------------------------------

@app.get("/events/{event_id}")
def get_event(event_id: str) -> dict:
    """Return detailed status for a single event.

    Includes full status record, execution context from the latest
    attempt, products reference, and log reference.

    Users should not need to browse runtime folders to discover
    event state.

    Returns HTTP 404 if the event does not exist.
    """
    record = read_status(event_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Event '{event_id}' not found")

    # Build the response with full detail
    response: dict = {
        "event_id": record.event_id,
        "user_id": record.user_id,
        "status": record.status,
        "submitted_at": record.submitted_at,
        "validated_at": record.validated_at,
        "queued_at": record.queued_at,
        "started_at": record.started_at,
        "completed_at": record.completed_at,
        "current_attempt": record.current_attempt,
        "max_attempts": record.max_attempts,
        "failure_reason": record.failure_reason,
        "validation_errors": record.validation_errors,
    }

    # Execution context from the latest attempt
    execution_context = None
    if record.attempt_history:
        latest = record.attempt_history[-1]
        execution_context = latest.execution_context
    response["execution_context"] = execution_context

    # Full attempt history
    response["attempt_history"] = [
        {
            "attempt_number": a.attempt_number,
            "started_at": a.started_at,
            "completed_at": a.completed_at,
            "status": a.status,
            "failure_reason": a.failure_reason,
            "duration_seconds": a.duration_seconds,
            "execution_context": a.execution_context,
        }
        for a in record.attempt_history
    ]

    # Products reference
    products_dir = paths.event_products_dir(record.event_id)
    has_products = products_dir.is_dir()
    response["products"] = {
        "published_products_directory": record.published_products_directory,
        "has_products": has_products,
        "products_path": str(products_dir) if has_products else None,
    }

    # Log reference — check shared logs directory
    log_file = paths.event_log_file(record.event_id)
    response["logs"] = {
        "log_file": str(log_file) if log_file.is_file() else None,
        "has_log": log_file.is_file(),
    }

    # Incoming files reference
    incoming = paths.event_incoming_dir(record.event_id)
    incoming_files: list[str] = []
    if incoming.is_dir():
        incoming_files = sorted(f.name for f in incoming.iterdir() if f.is_file())
    response["incoming_files"] = incoming_files

    # Status file path (for debugging)
    response["status_path"] = f".service/events/{record.event_id}/requeststatus.json"

    return response


# ------------------------------------------------------------------
# GET /events/{event_id}/products -- event product files listing
# ------------------------------------------------------------------

@app.get("/events/{event_id}/products")
def get_event_products(event_id: str) -> dict:
    """List product files for a completed event.

    Returns the list of files in the products directory for the
    given event.  Returns HTTP 404 if the event does not exist.
    Returns an empty file list if no products have been published.
    """
    record = read_status(event_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Event '{event_id}' not found")

    products_dir = paths.event_products_dir(event_id)
    files: list[dict] = []

    if products_dir.is_dir():
        for item in sorted(products_dir.iterdir()):
            if item.name.startswith("."):
                continue  # skip hidden files
            entry: dict = {
                "name": item.name,
                "is_dir": item.is_dir(),
            }
            if item.is_file():
                try:
                    entry["size_bytes"] = item.stat().st_size
                except OSError:
                    entry["size_bytes"] = None
            files.append(entry)

    return {
        "event_id": event_id,
        "status": record.status,
        "products_directory": str(products_dir) if products_dir.is_dir() else None,
        "published_products_directory": record.published_products_directory,
        "file_count": len(files),
        "files": files,
    }


# ------------------------------------------------------------------
# GET /queue -- current queue state
# ------------------------------------------------------------------

@app.get("/queue")
def get_queue() -> dict:
    """Return the current queue state.

    Shows pending QUEUED events in FIFO order, any malformed records
    encountered during discovery, and the queue size.
    """
    candidates, malformed = discover_queue()

    events = [
        {
            "event_id": r.event_id,
            "user_id": r.user_id,
            "queued_at": r.queued_at,
            "submitted_at": r.submitted_at,
            "current_attempt": r.current_attempt,
            "max_attempts": r.max_attempts,
        }
        for r in candidates
    ]

    malformed_entries = [
        {"event_id": m.event_id, "error": m.error}
        for m in malformed
    ]

    return {
        "pending_count": len(candidates),
        "events": events,
        "malformed_count": len(malformed),
        "malformed": malformed_entries,
    }
