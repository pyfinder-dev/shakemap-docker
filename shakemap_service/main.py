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

Managed execution is disabled. Application startup is therefore inert: it
does not recover queue records or start the calculation worker.

Health reports process liveness, infrastructure, external data inspection, and
managed-calculation readiness as separate concerns.
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
from .preparation import inspect_data_assets
from .submission import submit_event, SubmissionResult
from .worker import recover_interrupted_events, run_worker_cycle, execute_shakemap
from .queue import discover_queue
from .status import (
    EventStatus,
    latest_status_for_event,
    records_for_event,
    scan_event_records,
)

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
    """Keep startup inert until managed execution is explicitly enabled."""
    logger.info("ShakeMap service starting up")
    if not _managed_execution_enabled():
        logger.info(
            "Managed execution disabled; startup recovery and worker startup skipped"
        )
        yield
        return

    try:
        recovered = recover_interrupted_events()
        if recovered:
            logger.info("Startup recovery: failed %d interrupted queue entries without retry: %s",
                        len(recovered), recovered)
        else:
            logger.info("Startup recovery: no interrupted events found")
    except Exception:
        logger.exception("Startup recovery failed — continuing anyway")

    _worker_stop.clear()
    worker_thread = threading.Thread(
        target=_worker_loop,
        name="shakemap-worker",
        daemon=True,
    )
    worker_thread.start()
    logger.info("Background worker thread started")

    yield

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

def _data_inspection() -> dict:
    """Return cheap external-data presence/readability evidence."""
    return inspect_data_assets(paths.shakemap_data_dir())


def _managed_execution_enabled() -> bool:
    """Managed calculation execution remains disabled in this correction."""
    return False


def _compute_blocking_reasons(
    shake_cli_available: bool,
    dir_checks: dict,
) -> list[str]:
    """Compute infrastructure blockers without treating data as readiness."""
    reasons: list[str] = []

    for name, info in dir_checks.items():
        if not info.get("exists"):
            reasons.append(f"Directory {name}/ does not exist")
        elif info.get("required_access") == "read" and not info.get("readable"):
            reasons.append(f"Directory {name}/ is not readable")
        elif info.get("required_access") == "write" and not info.get("writable"):
            reasons.append(f"Directory {name}/ is not writable")

    if not shake_cli_available:
        reasons.append("ShakeMap CLI (shake) not found on PATH")

    reasons.append(
        "Managed calculation execution is disabled because effective "
        "ShakeMap configuration resolution is not implemented"
    )
    return reasons


def _compute_next_action(blocking_reasons: list[str]) -> str:
    """Compute the recommended next action based on blocking reasons."""
    if not blocking_reasons:
        return ""

    for reason in blocking_reasons:
        if "not writable" in reason.lower():
            return "Fix host directory permissions: chown -R 1000:1000 <host-runtime-dir>"
        if "does not exist" in reason.lower() and "directory" in reason.lower():
            return "Restart the container to recreate service directories"
        if "shake" in reason.lower() and "path" in reason.lower():
            return "Rebuild the Docker image -- ShakeMap may not be installed correctly"

    return (
        "Implement and validate effective ShakeMap configuration resolution "
        "before enabling managed calculations"
    )


# ------------------------------------------------------------------
# GET /config -- active ShakeMap configuration inspection
# ------------------------------------------------------------------

@app.get("/config")
def get_config() -> dict:
    """Return identity, contracted data paths, and honest capability state."""
    identity = service_identity()
    data = _data_inspection()

    return {
        "response_schema_version": "1.0",
        "identity": identity,
        "data": data,
        "configurations": data["configurations"],
        "default_configuration": "global",
        "configuration_resolution": {
            "implemented": False,
            "state": "not_implemented",
            "reason": "effective ShakeMap configuration resolution is not implemented",
        },
        "managed_execution_readiness": {
            "ready": False,
            "state": "disabled",
            "reason": "effective ShakeMap configuration resolution is not implemented",
        },
        "overall_readiness": {
            "ready": False,
            "state": "not_ready",
            "reason": "managed calculation execution is disabled",
        },
        "service_root": settings.service_root,
        "shakemap_modules": settings.shakemap_modules,
        "managed_execution": {
            "enabled": False,
            "reason": "effective ShakeMap configuration resolution is not implemented",
        },
    }


# ------------------------------------------------------------------
# GET /config/profiles -- list existing profiles
# ------------------------------------------------------------------

@app.get("/config/profiles")
def get_config_profiles() -> dict:
    """List discovered configuration names without claiming validation."""
    data = _data_inspection()
    return {
        "response_schema_version": "1.0",
        "active_profile": None,
        "shared_mutable_profile_supported": False,
        "default_configuration": "global",
        "configurations": data["configurations"],
        "resolution_implemented": False,
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
        return JSONResponse(
            status_code=503,
            content={
                "detail": "Managed calculation submission is not enabled",
                "reason": "effective ShakeMap configuration resolution and authoritative success semantics are not implemented",
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
        "internal_sequence": result.sequence,
        "validation_errors": result.validation_errors,
    }

    return body


# ------------------------------------------------------------------
# GET /healthz -- comprehensive health and readiness
# ------------------------------------------------------------------

@app.get("/healthz")
def healthz() -> dict:
    """Report liveness, infrastructure, data evidence, and readiness separately."""
    dir_checks: dict[str, dict[str, object]] = {}
    for d in paths.all_service_dirs():
        exists = d.is_dir()
        required_access = "read" if d == paths.shakemap_data_dir() else "write"
        readable = os.access(d, os.R_OK) if exists else False
        writable = os.access(d, os.W_OK) if exists else False
        dir_checks[d.name] = {
            "exists": exists,
            "readable": readable,
            "writable": writable,
            "required_access": required_access,
        }
    directories_ready = all(
        value["exists"]
        and (
            value["readable"]
            if value["required_access"] == "read"
            else value["writable"]
        )
        for value in dir_checks.values()
    )
    shake_cli_available = shutil.which("shake") is not None
    infrastructure_passed = directories_ready and shake_cli_available
    infrastructure = {
        "passed": infrastructure_passed,
        "service_root": str(paths.service_root()),
        "directories": dir_checks,
        "shake_cli_available": shake_cli_available,
        "shake_cli_verification": "PATH presence only in this request",
    }
    identity = service_identity()
    data = _data_inspection()
    status = "live"
    blocking_reasons = _compute_blocking_reasons(
        shake_cli_available=shake_cli_available,
        dir_checks=dir_checks,
    )
    return {
        "response_schema_version": "1.0",
        "identity": identity,
        "status": status,
        "process_liveness": {
            "live": True,
            "state": "live",
            "reason": "the HTTP process is responding",
        },
        "managed_execution_readiness": {
            "ready": False,
            "state": "disabled",
            "reason": "effective ShakeMap configuration resolution is not implemented",
        },
        "overall_readiness": {
            "ready": False,
            "state": "not_ready",
            "reason": "managed calculation execution is disabled",
        },
        "blocking_reasons": blocking_reasons,
        "next_action": _compute_next_action(blocking_reasons),
        "infrastructure": infrastructure,
        "data": data,
        "configuration": {
            "modules": settings.shakemap_modules,
            "service_root": settings.service_root,
            "default": "global",
            "resolution_state": "not_implemented",
        },
        "managed_execution": {
            "enabled": False,
            "reason": "effective ShakeMap configuration resolution is not implemented",
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
    all_records, malformed = scan_event_records()

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
            "kind": record.kind,
            "internal_sequence": record.sequence,
            "requested_region": record.requested_region,
            "effective_region": record.effective_region,
            "module_plan": record.module_plan,
            "failure": record.failure,
            "interruption": record.interruption,
            "mounted_paths": record.mounted_paths,
        }
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
        "malformed_count": len(malformed),
        "malformed": [{"entry": entry, "error": error} for entry, error in malformed],
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
    record = latest_status_for_event(event_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Event '{event_id}' not found")

    # Build the response with full detail
    response: dict = {
        "event_id": record.event_id,
        "user_id": record.user_id,
        "status": record.status,
        "submitted_at": record.submitted_at,
        "queued_at": record.queued_at,
        "started_at": record.started_at,
        "completed_at": record.completed_at,
        "schema_version": record.schema_version,
        "kind": record.kind,
        "internal_sequence": record.sequence,
        "requested_region": record.requested_region,
        "effective_region": record.effective_region,
        "module_plan": record.module_plan,
        "current_child": record.current_child,
        "failure": record.failure,
        "interruption": record.interruption,
        "mounted_paths": record.mounted_paths,
    }

    # Products reference
    products_dir = paths.event_products_dir(record.event_id)
    has_products = products_dir.is_dir()
    response["products"] = {
        "has_products": has_products,
        "products_path": str(products_dir) if has_products else None,
    }

    # Log reference — check shared logs directory
    log_file = paths.event_log_file(record.event_id)
    response["logs"] = {
        "log_file": str(log_file) if log_file.is_file() else None,
        "has_log": log_file.is_file(),
    }

    queue_entries = records_for_event(event_id)
    response["accepted_submissions"] = [
        {
            "internal_sequence": item.sequence,
            "status": item.status,
            "queued_at": item.queued_at,
            "failure": item.failure,
        }
        for item in queue_entries
    ]
    response["status_path"] = (
        f".service/events/{paths.queue_entry_name(record.sequence)}/status.json"
    )

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
    record = latest_status_for_event(event_id)
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
        "native_products_directory": str(paths.event_native_products_dir(event_id)),
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
            "kind": r.kind,
            "internal_sequence": r.sequence,
            "requested_region": r.requested_region,
            "effective_region": r.effective_region,
            "module_plan": r.module_plan,
            "mounted_paths": r.mounted_paths,
        }
        for r in candidates
    ]

    malformed_entries = [
        {"entry": m.entry, "error": m.error}
        for m in malformed
    ]

    return {
        "pending_count": len(candidates),
        "events": events,
        "malformed_count": len(malformed),
        "malformed": malformed_entries,
    }
