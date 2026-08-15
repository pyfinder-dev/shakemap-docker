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

import os
import shutil
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .config import settings
from . import paths
from .build_identity import service_identity
from .preparation import inspect_data_assets


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run without startup or shutdown side effects."""
    yield


app = FastAPI(title="ShakeMap Service", version="0.1.0", lifespan=lifespan)


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _data_inspection() -> dict:
    """Return cheap external-data presence/readability evidence."""
    return inspect_data_assets(paths.shakemap_data_dir())


def _disabled_calculation_response() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "detail": "Managed calculation operations are not enabled",
            "reason": "the public calculation interface and native worker are disabled",
            "status": "not_ready",
        },
    )


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
    """Return identity, configured data paths, and honest capability state."""
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
        "service_root": settings.shared_service_root,
        "shakemap_modules": list(settings.module_plan),
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
async def submit_event_endpoint() -> JSONResponse:
    """Reject this legacy route; no public request-acceptance path is exposed."""
    return _disabled_calculation_response()


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
            "modules": list(settings.module_plan),
            "service_root": settings.shared_service_root,
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
def list_events() -> JSONResponse:
    """Return the disabled calculation response."""
    return _disabled_calculation_response()


# ------------------------------------------------------------------
# GET /events/{event_id} -- single event detail
# ------------------------------------------------------------------

@app.get("/events/{event_id}")
def get_event(event_id: str) -> JSONResponse:
    """Return the disabled calculation response."""
    return _disabled_calculation_response()


# ------------------------------------------------------------------
# GET /events/{event_id}/products -- event product files listing
# ------------------------------------------------------------------

@app.get("/events/{event_id}/products")
def get_event_products(event_id: str) -> JSONResponse:
    """Return the disabled calculation response."""
    return _disabled_calculation_response()


# ------------------------------------------------------------------
# GET /queue -- current queue state
# ------------------------------------------------------------------

@app.get("/queue")
def get_queue() -> JSONResponse:
    """Return the disabled calculation response."""
    return _disabled_calculation_response()
