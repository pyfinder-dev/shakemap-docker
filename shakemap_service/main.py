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
  - Remains gated off until the later managed-execution contract exists.

Health reports infrastructure and durable preparation separately. Preparation
readiness is bounded evidence, not managed-calculation or SUCCESS readiness.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse

from .config import settings
from . import paths
from .build_identity import service_identity
from .preparation import load_preparation
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
_WORKER_NOT_READY_SLEEP = 30.0 # seconds to wait while managed execution is disabled


def _worker_loop() -> None:
    """Background worker loop — processes QUEUED events continuously.

    This loop:
    1. Checks whether managed execution is enabled before processing.
    2. Calls ``run_worker_cycle(execute_fn=execute_shakemap)`` to
       claim and execute the next QUEUED event.
    3. Uses adaptive backoff: fast when busy, slow when idle.
    4. Stops when ``_worker_stop`` is set.

    Runs as a daemon thread started by the lifespan handler.
    """
    logger.info("Worker thread started")
    while not _worker_stop.is_set():
        try:
            if not _managed_execution_enabled():
                logger.debug("Worker: managed execution disabled, sleeping %.0fs", _WORKER_NOT_READY_SLEEP)
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
    """Load the durable pre-start preparation record from the mounted runtime."""
    state = load_preparation(paths.service_root())
    return {
        "passed": state["ready"],
        "reason": "" if state["ready"] else state.get("reason", "runtime preparation is invalid"),
        "overrides": [],
        "preparation": state,
    }


def _check_prepared_data() -> dict:
    """Report the external grids and validated base snapshot."""
    vs30 = paths.vs30_grid_path()
    topo = paths.topo_grid_path()
    base_config = paths.global_base_dir() / "install/config"
    return {
        "vs30_file": str(vs30), "vs30_file_exists": vs30.is_file(),
        "vs30_file_non_empty": vs30.is_file() and vs30.stat().st_size > 0,
        "topo_file": str(topo), "topo_file_exists": topo.is_file(),
        "topo_file_non_empty": topo.is_file() and topo.stat().st_size > 0,
        "model_conf_valid": (base_config / "model.conf").is_file(),
        "base_config_dir": str(base_config),
    }


def _managed_execution_enabled() -> bool:
    """Managed calculation execution remains disabled in this correction."""
    return False


def _compute_blocking_reasons(
    shake_cli_available: bool,
    dir_checks: dict,
    sentinel_info: dict,
    prepared_data: dict,
    base_template_readable: bool,
    base_exists: bool,
    base_config_valid: bool,
) -> list[str]:
    """Compute a list of human-readable blocking reasons."""
    reasons: list[str] = []

    for name, info in dir_checks.items():
        if not info.get("exists"):
            reasons.append(f"Directory {name}/ does not exist")
        elif not info.get("writable"):
            reasons.append(f"Directory {name}/ is not writable")

    if not shake_cli_available:
        reasons.append("ShakeMap CLI (shake) not found on PATH")

    if not sentinel_info["passed"]:
        reason = sentinel_info.get("reason", "runtime preparation is unavailable")
        reasons.append(reason)
    else:
        if not prepared_data.get("vs30_file_exists"):
            reasons.append("VS30 grid missing")
        if not prepared_data.get("model_conf_valid"):
            reasons.append("model.conf validation failed")
        if not base_template_readable:
            reasons.append("base profiles.conf template missing")
        if not base_exists:
            reasons.append("global base snapshot missing")
        if not base_config_valid:
            reasons.append("global base config directory missing")

    return reasons


def _compute_next_action(blocking_reasons: list[str], sentinel_info: dict) -> str:
    """Compute the recommended next action based on blocking reasons."""
    if not blocking_reasons:
        return ""

    if not sentinel_info["passed"]:
        reason = sentinel_info.get("reason", "")
        if "not been run" in reason:
            return "Run before starting the service: ./scripts/configure-shakemap.sh"
        if "sentinel" in reason.lower():
            return "Run before starting the service: ./scripts/configure-shakemap.sh"

    for reason in blocking_reasons:
        if "not writable" in reason.lower():
            return "Fix host directory permissions: chown -R 1000:1000 <host-runtime-dir>"
        if "does not exist" in reason.lower() and "directory" in reason.lower():
            return "Restart the container to recreate service directories"
        if "shake" in reason.lower() and "path" in reason.lower():
            return "Rebuild the Docker image -- ShakeMap may not be installed correctly"
        if "vs30" in reason.lower():
            return "Provide the global VS30 grid, then run ./scripts/configure-shakemap.sh"
        if "profile" in reason.lower() or "profiles.conf" in reason.lower():
            return "Run before starting the service: ./scripts/configure-shakemap.sh"
        if "model.conf" in reason.lower():
            return "Run before starting the service: ./scripts/configure-shakemap.sh"
        if "symlink" in reason.lower():
            return "Run before starting the service: ./scripts/configure-shakemap.sh"

    return "Run before starting the service: ./scripts/configure-shakemap.sh"


# ------------------------------------------------------------------
# GET /config -- active ShakeMap configuration inspection
# ------------------------------------------------------------------

@app.get("/config")
def get_config() -> dict:
    """Return the active ShakeMap configuration.

    Read-only inspection of the durable preparation identity, base snapshot,
    external scientific data, and bounded readiness evidence.
    """
    sentinel_info = _read_readiness_sentinel()
    prepared_data = _check_prepared_data()

    readiness_state = "prepared" if sentinel_info["passed"] else "not_ready"
    preparation = sentinel_info["preparation"]
    base_config = paths.global_base_dir() / "install/config"

    return {
        "response_schema_version": 2,
        "identity": service_identity(),
        "scientific_readiness": {
            "ready": sentinel_info["passed"],
            "state": readiness_state,
            "reason": sentinel_info.get("reason", ""),
        },
        "preparation": preparation,
        "global_base_snapshot": str(paths.global_base_dir()),
        "model_conf_path": str(base_config / "model.conf"),
        "model_conf_exists": (base_config / "model.conf").is_file(),
        "products_conf_path": str(base_config / "products.conf"),
        "products_conf_exists": (base_config / "products.conf").is_file(),
        "vs30_file": prepared_data.get("vs30_file", ""),
        "vs30_file_exists": prepared_data.get("vs30_file_exists", False),
        "topo_file": prepared_data.get("topo_file", ""),
        "topo_file_exists": prepared_data.get("topo_file_exists", False),
        "readiness_state": readiness_state,
        "readiness_reason": sentinel_info.get("reason", ""),
        "proof_scope": "fixed California and fixed prepared-global native scenarios only",
        "non_claims": ["durable queue", "REST submission", "concurrency", "recalculation archival", "authoritative service SUCCESS", "universal scientific validity"],
        "service_root": settings.service_root,
        "shakemap_modules": settings.shakemap_modules,
    }


# ------------------------------------------------------------------
# GET /config/profiles -- list existing profiles
# ------------------------------------------------------------------

@app.get("/config/profiles")
def get_config_profiles() -> dict:
    """Report the immutable base snapshot; active mutable profiles are unsupported."""
    state = load_preparation(paths.service_root())
    base = paths.global_base_dir()
    return {
        "active_profile": None,
        "shared_mutable_profile_supported": False,
        "base_snapshot": {
            "name": "global",
            "path": str(base),
            "valid": state["ready"] and base.is_dir(),
        },
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

    Returns HTTP 503 while managed calculation execution remains deferred.
    Returns ``event_id``, ``status``, ``status_path``, and
    ``replaced_previous``.
    """
    if not _managed_execution_enabled():
        sentinel_info = _read_readiness_sentinel()
        return JSONResponse(
            status_code=503,
            content={
                "detail": "Managed calculation execution is not enabled by the preparation correction",
                "reason": (
                    "runtime preparation is valid, but durable queue and authoritative execution semantics remain deferred"
                    if sentinel_info.get("passed")
                    else sentinel_info.get("reason", "runtime preparation is unavailable")
                ),
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
    """Report infrastructure health and bounded preparation readiness."""
    dir_checks: dict[str, dict[str, bool]] = {}
    for d in paths.all_service_dirs():
        exists = d.is_dir()
        writable = os.access(d, os.W_OK) if exists else False
        dir_checks[d.name] = {"exists": exists, "writable": writable}
    directories_ready = all(
        value["exists"] and value["writable"] for value in dir_checks.values()
    )
    shake_cli_available = shutil.which("shake") is not None
    infrastructure_passed = directories_ready and shake_cli_available
    infrastructure = {
        "passed": infrastructure_passed,
        "service_root": str(paths.service_root()),
        "directories": dir_checks,
        "shake_cli_available": shake_cli_available,
        "shake_cli_verification": "immutable image gate plus offline native preparation runs",
    }

    sentinel_info = _read_readiness_sentinel()
    prepared_data = _check_prepared_data()
    base_config = paths.global_base_dir() / "install/config"
    base_template_readable = (paths.global_base_dir() / "profiles.conf.template").is_file()
    base_exists = paths.global_base_dir().is_dir()
    base_config_valid = base_config.is_dir()
    preparation_passed = sentinel_info["passed"] and all((
        prepared_data.get("vs30_file_exists", False),
        prepared_data.get("vs30_file_non_empty", False),
        prepared_data.get("topo_file_exists", False),
        prepared_data.get("topo_file_non_empty", False),
        prepared_data.get("model_conf_valid", False),
        base_template_readable,
        base_exists,
        base_config_valid,
    ))
    preparation_readiness = {
        "passed": preparation_passed,
        **({} if preparation_passed else {"reason": sentinel_info.get("reason", "prepared data or base snapshot is incomplete")}),
        "checks": {
            "vs30_file": prepared_data.get("vs30_file", ""),
            "vs30_file_exists": prepared_data.get("vs30_file_exists", False),
            "vs30_file_non_empty": prepared_data.get("vs30_file_non_empty", False),
            "topo_file": prepared_data.get("topo_file", ""),
            "topo_file_exists": prepared_data.get("topo_file_exists", False),
            "topo_file_non_empty": prepared_data.get("topo_file_non_empty", False),
            "model_conf_valid": prepared_data.get("model_conf_valid", False),
            "base_template_readable": base_template_readable,
            "base_exists": base_exists,
            "base_config_valid": base_config_valid,
        },
        "base_snapshot": str(paths.global_base_dir()),
        "preparation": sentinel_info["preparation"],
    }
    status = "healthy" if infrastructure_passed and preparation_passed else "not_ready"
    blocking_reasons = _compute_blocking_reasons(
        shake_cli_available=shake_cli_available,
        dir_checks=dir_checks,
        sentinel_info=sentinel_info,
        prepared_data=prepared_data,
        base_template_readable=base_template_readable,
        base_exists=base_exists,
        base_config_valid=base_config_valid,
    )
    return {
        "response_schema_version": 2,
        "identity": service_identity(),
        "scientific_readiness": {
            "ready": preparation_passed,
            "state": "prepared" if preparation_passed else "not_ready",
            "reason": sentinel_info.get("reason", ""),
        },
        "status": status,
        "blocking_reasons": blocking_reasons,
        "next_action": _compute_next_action(blocking_reasons, sentinel_info),
        "infrastructure": infrastructure,
        "preparation_readiness": preparation_readiness,
        "configuration": {
            "modules": settings.shakemap_modules,
            "service_root": settings.service_root,
        },
        "managed_execution": {
            "enabled": False,
            "reason": "durable queue and authoritative execution semantics remain deferred",
        },
    }


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
