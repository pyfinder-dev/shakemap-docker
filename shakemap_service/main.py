# -*- coding: utf-8 -*-
"""ShakeMap service — FastAPI application with comprehensive health endpoint.

Phase 01 exposes only ``GET /healthz``. The endpoint performs real
infrastructure, ShakeMap-tool, and configuration checks and returns one
of ``healthy``, ``degraded``, or ``not_ready``.
"""
from __future__ import annotations

import os
import shutil
import subprocess

from fastapi import FastAPI

from .config import settings
from . import paths

app = FastAPI(title="ShakeMap Service", version="0.1.0")


# ------------------------------------------------------------------
# GET /healthz — comprehensive health and readiness
# ------------------------------------------------------------------

@app.get("/healthz")
def healthz() -> dict:
    """Comprehensive health and readiness endpoint.

    Tier 1 — Infrastructure: service root + 6 directories exist and are writable.
    Tier 2 — ShakeMap tool readiness: shake CLI, profiles.conf, profile
              structure, data bridge.
    Tier 3 — Configuration reporting: active profile, modules, profiles list.

    Returns one of: ``healthy``, ``degraded``, ``not_ready``.
    """

    # ── Tier 1: Infrastructure ────────────────────────────────────
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

    # ── Tier 2: ShakeMap tool readiness ───────────────────────────
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

    profiles_conf_readable = paths.profiles_conf().is_file()

    active_profile_name = settings.shakemap_profile
    profile_exists = paths.profile_root().is_dir()
    profile_config_valid = paths.profile_config_dir().is_dir()

    data_dir = paths.profile_data_dir()
    profile_data_bridge_ok = (
        data_dir.is_symlink()
        and data_dir.resolve() == paths.work_dir().resolve()
    )

    available_profiles = paths.list_profiles()

    tier2_ok = all([
        shake_cli_available,
        shake_cli_responsive,
        profiles_conf_readable,
        profile_exists,
        profile_config_valid,
        profile_data_bridge_ok,
    ])

    shakemap_info = {
        "shake_cli_available": shake_cli_available,
        "shake_cli_responsive": shake_cli_responsive,
        "profiles_conf_readable": profiles_conf_readable,
        "active_profile": active_profile_name,
        "profile_exists": profile_exists,
        "profile_config_valid": profile_config_valid,
        "profile_data_bridge_ok": profile_data_bridge_ok,
        "available_profiles": available_profiles,
    }

    # ── Tier 3: Configuration reporting ───────────────────────────
    configuration = {
        "modules": settings.shakemap_modules,
        "service_root": settings.service_root,
    }

    # ── Status determination ──────────────────────────────────────
    if not tier1_ok:
        status = "not_ready"
    elif not tier2_ok:
        status = "degraded"
    else:
        status = "healthy"

    return {
        "status": status,
        "infrastructure": infrastructure,
        "shakemap": shakemap_info,
        "configuration": configuration,
    }
