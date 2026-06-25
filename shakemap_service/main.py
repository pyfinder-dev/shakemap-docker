# -*- coding: utf-8 -*-
"""ShakeMap service -- FastAPI application.

Phase 01: ``GET /healthz`` -- comprehensive health and readiness.
Phase 03: ``POST /events/submit`` -- event submission and staging.

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
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from .config import settings
from . import paths
from .submission import submit_event, SubmissionResult

logger = logging.getLogger(__name__)

app = FastAPI(title="ShakeMap Service", version="0.1.0")


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

